import json
import os
from pathlib import Path
import subprocess
import pytest
from bridge.core import Registry,SafeFiles,Queue


def project(tmp_path,pid="TEST-PROJECT"):
    root=tmp_path/pid; root.mkdir()
    subprocess.run(["git","init","-q",str(root)],check=True)
    subprocess.run(["git","-C",str(root),"config","user.email","bridge-test@example.invalid"],check=True)
    subprocess.run(["git","-C",str(root),"config","user.name","Bridge test"],check=True)
    (root/"README.md").write_text("hello\n")
    subprocess.run(["git","-C",str(root),"add","README.md"],check=True)
    subprocess.run(["git","-C",str(root),"commit","-qm","fixture"],check=True)
    return {"root":root,"workspace":str(root),"codex_workspace":str(root),"chatgpt_thread":"architect-"+pid,"bridge_project_id":pid,"mode":"CHATGPT_ARCHITECT","chatgpt_access":"READ_ONLY"}


def result(task): return {"task_id":task["task_id"],"revision":task["revision"],"summary":"test result","tests":[{"status":"PASS"}],"acceptance":[{"status":"PASS"}]}


def test_queue_fix_accept_restart(tmp_path):
    p=project(tmp_path); q=Queue(p)
    packet=q.submit("Title","Instructions",["criterion"],"request-0001")
    assert q.submit("Title","Instructions",["criterion"],"request-0001")==packet
    with pytest.raises(ValueError): q.submit("Different","Instructions",["criterion"],"request-0001")
    with pytest.raises(ValueError): q.submit("Next","Instructions",["criterion"],"request-0002")
    task=q.claim(); q.finish(task,result(task))
    fix=q.review("TASK-001",1,"FIX","Improve it","review-0001")
    assert fix["revision"]==2
    with pytest.raises(ValueError): q.review("TASK-001",1,"ACCEPT","Stale","review-0002")
    q=Queue(p); task=q.claim(); assert task["review_feedback"]=="Improve it"
    q.finish(task,result(task)); q.review("TASK-001",2,"ACCEPT","Verified","review-0003")
    next=q.submit("Next","Instructions",["criterion"],"request-0002")
    assert next["task_id"]=="TASK-002"
    assert (q.root/"results/TASK-001-R1.json").exists()
    assert (q.root/"reviews/TASK-001-R2.json").exists()
    (q.root/"state.json").write_text("corrupt")
    assert Queue(p).status()["tasks"][0]["status"]=="ACCEPTED"
    assert json.loads((q.root/"state.json").read_text())["tasks"][1]["id"]=="TASK-002"


def test_interrupted_never_replayed(tmp_path):
    q=Queue(project(tmp_path)); q.submit("T","I",["C"],"request-0001"); task=q.claim()
    q.recover(); assert q.status()["tasks"][0]["status"]=="INTERRUPTED"
    assert q.claim() is None
    with pytest.raises(ValueError): q.finish(task,result(task))
    q.retry("TASK-001"); assert q.claim()["run_id"]!=task["run_id"]


def test_isolated_queues(tmp_path):
    a=Queue(project(tmp_path,"PROJECT-AAA")); b=Queue(project(tmp_path,"PROJECT-BBB"))
    a.submit("A","A",["A"],"same-request"); b.submit("B","B",["B"],"same-request")
    assert a.claim()["title"]=="A" and b.claim()["title"]=="B"


@pytest.mark.parametrize("path",["../README.md","/etc/passwd",".env",".env.example",".git/config",".npmrc","config/credentials.json","secrets.json","token.txt",".ssh/id_rsa","storage/data.txt",".ai-bridge/state.json"])
def test_denied(tmp_path,path):
    p=project(tmp_path); f=SafeFiles(p)
    with pytest.raises((ValueError,OSError)): f.read(path)


def test_symlink_hardlink_fifo_and_search(tmp_path):
    p=project(tmp_path); root=p["root"]; f=SafeFiles(p)
    secret=tmp_path/"other.md"; secret.write_text("private secret\n")
    (root/"linked.md").symlink_to(secret)
    (root/"linked-dir").symlink_to(tmp_path,target_is_directory=True)
    os.link(secret,root/"hard.md"); os.mkfifo(root/"fifo.txt")
    for file in ["linked.md","linked-dir/other.md","hard.md","fifo.txt"]:
        with pytest.raises((ValueError,OSError)): f.read(file)
    assert not f.search("private")["matches"]
    assert f.search("hello")["matches"][0]["path"]=="README.md"
    (root/"api.ts").write_text('const password = "topsecretvalue";\n')
    assert "topsecretvalue" not in f.read("api.ts")


def test_git_excludes_env_and_disables_external_diff(tmp_path):
    p=project(tmp_path); root=p["root"]; f=SafeFiles(p)
    (root/".env").write_text("PRIVATE_MARKER=forbidden\n")
    subprocess.run(["git","-C",str(root),"add",".env"],check=True)
    subprocess.run(["git","-C",str(root),"commit","-qm","fixture env"],check=True)
    (root/".env").write_text("PRIVATE_MARKER=newforbidden\n")
    (root/"README.md").write_text("hello changed\n")
    script=tmp_path/"diff.sh"; script.write_text('#!/bin/sh\ntouch "'+str(tmp_path/"executed")+'"\n'); script.chmod(0o755)
    subprocess.run(["git","-C",str(root),"config","diff.external",str(script)],check=True)
    assert "changed" in f.git("diff")["diff"]
    assert "forbidden" not in f.git("diff")["diff"]
    assert not (tmp_path/"executed").exists()
    assert all(".env" not in entry for entry in f.git("status"))


def test_registry_overlap_and_mode(tmp_path):
    p=project(tmp_path); config=tmp_path/"config/projects";config.mkdir(parents=True)
    serial={k:v for k,v in p.items() if k!="root"}; (config/"p.json").write_text(json.dumps(serial))
    assert list(Registry(config).projects)==["TEST-PROJECT"]
    serial["bridge_project_id"]="ANOTHER-PROJECT"; serial["chatgpt_thread"]="another"
    (config/"q.json").write_text(json.dumps(serial))
    with pytest.raises(ValueError): Registry(config)


def test_failed_accept_and_symlink_metadata(tmp_path):
    p=project(tmp_path); q=Queue(p);q.submit("T","I",["C"],"request-0001"); t=q.claim(); q.finish(t,result(t),False)
    with pytest.raises(ValueError): q.review("TASK-001",1,"ACCEPT","No","review-0001")
    other=tmp_path/"outside";other.mkdir(); (q.root/"results/TASK-001-R1.json").unlink(); (q.root/"results/TASK-001-R1.json").symlink_to(other/"bad.json")
    with pytest.raises(ValueError): q.export()


def test_surviving_child_blocks_recovery(tmp_path):
    q=Queue(project(tmp_path));q.submit("T","I",["C"],"request-0001"); task=q.claim()
    q.child(task,os.getpid());q.recover()
    assert q.status()["tasks"][0]["status"]=="RUNNING"
    assert q.claim() is None


def test_numbering_and_result_scrub(tmp_path):
    p=project(tmp_path);p["task_number_start"]=3;q=Queue(p)
    assert q.submit("T","I",["C"],"request-0001")["task_id"]=="TASK-003"
    task=q.claim(); r=result(task);r["summary"]='password="longsecretvalue"'
    q.finish(task,r)
    assert "longsecretvalue" not in json.dumps(q.details("TASK-003"))
