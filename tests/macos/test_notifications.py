import json

from bridge.core import Queue
from bridge.notifications import snapshot, acknowledge
from test_core import project, result


def test_delivery_ack_restart_and_new_state(tmp_path):
    p=project(tmp_path);q=Queue(p)
    assert snapshot(tmp_path,p)=={"changed":False,"task":None}
    q.submit("Private title","Private instructions",["Criterion"],"notify-0001")
    first=snapshot(tmp_path,p)
    assert first["changed"] and "Private" not in json.dumps(first)
    assert snapshot(tmp_path,p)["changed"]  # Failed send must remain pending.
    acknowledge(tmp_path,p,first["fingerprint"])
    assert not snapshot(tmp_path,p)["changed"]
    task=q.claim();running=snapshot(tmp_path,p)
    assert running["changed"] and running["task"]["run_id"]==task["run_id"]
    acknowledge(tmp_path,p,running["fingerprint"])
    q.finish(task,result(task))
    finished=snapshot(tmp_path,p)
    assert finished["changed"] and finished["task"]["status"]=="AWAITING_REVIEW"
    assert finished["task"]["tests"]=={"PASS":1}
    # Acknowledging an earlier sent snapshot cannot swallow a later change.
    acknowledge(tmp_path,p,running["fingerprint"])
    assert snapshot(tmp_path,p)["changed"]


def test_progress_does_not_forward_raw_events_or_other_projects(tmp_path):
    p=project(tmp_path);q=Queue(p);q.submit("T","I",["C"],"notify-0001");task=q.claim()
    directory=tmp_path/".runtime/executions/executor-test";directory.mkdir(parents=True)
    (directory/"run.json").write_text(json.dumps({"run_id":task["run_id"]}))
    (directory/"events.private.jsonl").write_text(json.dumps({"type":"item.completed","item":{"text":"password=veryprivate"}})+'\n'+ '{"type":')
    current=snapshot(tmp_path,p)
    assert current["task"]["activity"]=={"item.completed":1}
    assert "veryprivate" not in json.dumps(current)
    acknowledge(tmp_path,p,current["fingerprint"])
    assert not snapshot(tmp_path,p)["changed"]
    (directory/"run.json").write_text(json.dumps({"run_id":"foreign"}))
    assert "activity" not in snapshot(tmp_path,p)["task"]
