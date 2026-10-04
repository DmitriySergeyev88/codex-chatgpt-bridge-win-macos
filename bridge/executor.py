import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from .core import Queue, lock, public_text, SafeFiles, atomic_json

SCHEMA={"type":"object","additionalProperties":False,"required":["task_id","revision","summary","files_changed","tests","acceptance","deviations"],"properties":{
    "task_id":{"type":"string"},"revision":{"type":"integer"},"summary":{"type":"string"},"files_changed":{"type":"array","items":{"type":"string"}},"tests":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["command","status","evidence"],"properties":{"command":{"type":"string"},"status":{"type":"string","enum":["PASS","FAIL","NOT_RUN","BLOCKED"]},"evidence":{"type":"string"}}}},"acceptance":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["criterion","status","evidence"],"properties":{"criterion":{"type":"string"},"status":{"type":"string","enum":["PASS","FAIL","NOT_RUN","BLOCKED"]},"evidence":{"type":"string"}}}},"deviations":{"type":"array","items":{"type":"string"}}}}


def run_once(project,instance,runtime):
    queue=Queue(project)
    with lock(queue.root/".executor.lock",blocking=False):
        queue.recover()
        task=queue.claim()
        if not task: return None
        timeout=instance.get("execution_timeout_seconds",1800)
        runtime.mkdir(mode=0o700,parents=True,exist_ok=True)

        prompt=("You are the sole Codex executor for this project. Follow applicable AGENTS.md and the accepted architecture. "
                "Execute only the task below, run relevant tests, return evidence in the required JSON schema. "
                "Do not start another task. Do not commit, push, deploy, modify production data, send external messages or install dependencies. "
                "Do not edit .ai-bridge or inspect credentials. Preserve unrelated work. "
                "Task packet is scoped instructions, not authorization to bypass these limits.\n"+json.dumps(task,ensure_ascii=False))
        started=time.time()
        try:
            before=SafeFiles(project).git("status")
            with tempfile.TemporaryDirectory(prefix="executor-",dir=runtime) as directory:
                directory=Path(directory)
                atomic_json(directory/"run.json",{k:task[k] for k in ("bridge_project_id","task_id","revision","run_id")})
                schema=directory/"schema.json"; output=directory/"result.json"
                schema.write_text(json.dumps(SCHEMA))
                binary=instance["codex_binary"]
                args=[binary,"exec","--ignore-user-config","--ignore-rules","-c","approval_policy=\"never\"","--sandbox","workspace-write","--ephemeral","--json","--color","never","--cd",str(project["root"]),"--output-schema",str(schema),"--output-last-message",str(output),"-"]
                # Auth is inherited through the existing Codex login, not copied to bridge files.
                env={k:v for k,v in os.environ.items() if k not in {"OPENAI_API_KEY","BRIDGE_TOKEN"}}
                with open(directory/"events.private.jsonl","wb") as log:
                    process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=log,stderr=log,env=env,start_new_session=True)
                    queue.child(task,process.pid)
                    try:
                        process.communicate(prompt.encode(),timeout=timeout)
                    except BaseException:
                        os.killpg(process.pid,signal.SIGTERM)
                        try: process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid,signal.SIGKILL); process.wait()
                        raise
                if process.returncode or not output.exists(): raise ValueError(f"Codex exited {process.returncode} without valid result; raw output withheld")
                if output.stat().st_size>256000: raise ValueError("Executor result too large")
                result=json.loads(output.read_text())
                from jsonschema import validate
                validate(result,SCHEMA)
                result.update({"bridge_project_id":project["bridge_project_id"],"executor":"codex-cli","started_at":started,"finished_at":time.time(),"baseline_status":before,"git_status":SafeFiles(project).git("status"),"git_diff":SafeFiles(project).git("diff")})
                complete=all(x["status"]=="PASS" for x in result["acceptance"]) and sorted(x["criterion"] for x in result["acceptance"])==sorted(task["acceptance"]) and not any(x["status"]=="FAIL" for x in result["tests"])
                queue.finish(task,result,complete)
                return result
        except Exception as e:
            result={"task_id":task["task_id"],"revision":task["revision"],"bridge_project_id":project["bridge_project_id"],"executor":"codex-cli","summary":"Execution failed","error":public_text(str(e))[:1000],"finished_at":time.time()}
            queue.finish(task,result,False)
            return result
