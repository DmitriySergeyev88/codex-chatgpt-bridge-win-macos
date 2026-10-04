"""Read-only execution summaries for delivery by a Codex thread heartbeat.

No messaging credentials, executor launch, source reads or queue mutations.
Delivery is acknowledged separately, only after the app confirms the send.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .core import Registry, atomic_json, public_text


def snapshot(root, project):
    database=project["root"]/".ai-bridge/queue.sqlite3"
    if database.is_symlink(): raise ValueError("Unsafe queue database")
    with sqlite3.connect(database.as_uri()+"?mode=ro",uri=True) as db:
        db.row_factory=sqlite3.Row
        row=db.execute("SELECT id,revision,status,run_id,result FROM tasks ORDER BY rowid DESC LIMIT 1").fetchone()
    if row is None: return {"changed":False,"task":None}
    task={"bridge_project_id":project["bridge_project_id"],"task_id":row["id"],"revision":row["revision"],"status":row["status"],"run_id":row["run_id"]}
    if row["result"]:
        result=json.loads(row["result"])
        task.update(summary=public_text(str(result.get("summary","")))[:1500],
                    error=public_text(str(result.get("error","")))[:500],
                    tests=dict(Counter(x.get("status","UNKNOWN") for x in result.get("tests",[]))),
                    acceptance=dict(Counter(x.get("status","UNKNOWN") for x in result.get("acceptance",[]))))
    if row["status"]=="RUNNING":
        # Only metadata from this exact run is used; never forward raw event text.
        for metadata in (root/".runtime/executions").glob("executor-*/run.json"):
            if metadata.is_symlink(): continue
            run=json.loads(metadata.read_text())
            if run.get("run_id")!=row["run_id"]: continue
            events=metadata.parent/"events.private.jsonl"
            if not events.exists() or events.is_symlink(): continue
            counts=Counter()
            with events.open() as stream:
                for line in stream:
                    try: event=json.loads(line)
                    except json.JSONDecodeError: continue  # Last line may still be streaming.
                    kind=event.get("type")
                    if kind in {"item.started","item.completed","turn.completed","turn.failed"}: counts[kind]+=1
            task["activity"]=dict(counts)
            break
    fingerprint=hashlib.sha256(json.dumps(task,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    marker=root/".runtime/notifications"/(project["bridge_project_id"]+".json")
    if marker.is_symlink(): raise ValueError("Unsafe notification state")
    delivered=json.loads(marker.read_text()).get("fingerprint") if marker.exists() else None
    return {"changed":fingerprint!=delivered,"fingerprint":fingerprint,"target_thread":project.get("codex_thread_reference"),"task":task}


def acknowledge(root, project, fingerprint):
    if not re.fullmatch(r"[a-f0-9]{64}",fingerprint): raise ValueError("Invalid delivery fingerprint")
    path=root/".runtime/notifications"/(project["bridge_project_id"]+".json")
    if path.is_symlink() or path.parent.is_symlink(): raise ValueError("Unsafe notification state")
    atomic_json(path,{"fingerprint":fingerprint})
    path.chmod(0o600)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("action",choices=["snapshot","ack"])
    parser.add_argument("project")
    parser.add_argument("fingerprint",nargs="?")
    args=parser.parse_args()
    root=args.root.resolve();project=Registry(root/"config/projects").project(args.project)
    if args.action=="snapshot": print(json.dumps(snapshot(root,project),ensure_ascii=False,indent=2))
    else:
        acknowledge(root,project,args.fingerprint or "")
        print("Delivery acknowledged")


if __name__=="__main__": main()
