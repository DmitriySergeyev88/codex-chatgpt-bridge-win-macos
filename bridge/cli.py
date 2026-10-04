import argparse
import asyncio
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from .core import Registry, Queue, atomic_json, lock
from .auth import Vault

ROOT=Path(__file__).resolve().parent.parent
LABEL="org.codex.ai-bridge"


def load(args):
    root=Path(args.root).resolve()
    instance=json.loads((root/"config/instance.local.json").read_text())
    return root,instance


def desired(root):
    path=root/".runtime/desired-state.json"
    return json.loads(path.read_text())["state"] if path.exists() else "off"


def status(root,instance):
    url=f"http://127.0.0.1:{instance['port']}"
    health=False; protected=False
    try:
        with urllib.request.urlopen(url+"/health",timeout=3) as r:
            health=json.load(r)=={"service":"ai-bridge","status":"ok"}
        request=urllib.request.Request(url+"/mcp",data=b'{}',headers={"Content-Type":"application/json"})
        try: urllib.request.urlopen(request,timeout=3)
        except urllib.error.HTTPError as e: protected=e.code==401
    except (OSError,ValueError): pass
    return {"desired":desired(root),"healthy":health and protected,"oauth_required":protected,"local_mcp_url":url+"/mcp","public_mcp_url":instance["public_base_url"]+"/mcp"}


async def serve(root,instance):
    from .server import build
    import uvicorn
    # One process lock for every instance in this checkout; launchd supervises it.
    with lock(root/".runtime/service.lock",blocking=False):
        if desired(root)!="on": return
        mcp,oauth=build(root/"config/projects",instance,root/".runtime")
        server=uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(),host="127.0.0.1",port=instance["port"],log_level="warning",access_log=False))
        await server.serve()


def execute_loop(root,instance):
    from .executor import run_once
    registry=Registry(root/"config/projects")
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    # Serial within a project; separate threads preserve fairness across projects.
    def worker(project):
        while not stop.is_set() and desired(root)=="on":
            if project.get("auto_execute",False):
                try: run_once(project,instance,root/".runtime/executions")
                except BlockingIOError: pass
                except Exception:
                    # Detailed raw errors are deliberately not logged into a remotely readable tree.
                    print(f"Executor attention needed for {project['bridge_project_id']}",file=sys.stderr)
            stop.wait(3)
    threads=[threading.Thread(target=worker,args=(p,)) for p in registry.projects.values()]
    for t in threads: t.start()
    for t in threads: t.join()


def plists(root,instance,output):
    output.mkdir(parents=True,exist_ok=True)
    runtime=root/".runtime"; runtime.mkdir(mode=0o700,exist_ok=True)
    # The generated LaunchAgents call absolute paths, including paths with spaces.
    for suffix,action in [("server","serve"),("executor","execute-loop")]:
        label=LABEL+"."+suffix
        data={"Label":label,"ProgramArguments":[str(root/".venv/bin/python"),"-m","bridge.cli","--root",str(root),action],"WorkingDirectory":str(root),"RunAtLoad":True,"KeepAlive":{"SuccessfulExit":False},"ThrottleInterval":10,"ExitTimeOut":60,"ProcessType":"Background","StandardOutPath":str(runtime/(suffix+".log")),"StandardErrorPath":str(runtime/(suffix+".log")),"EnvironmentVariables":{"PATH":str(Path(instance["codex_binary"]).parent)+":/usr/bin:/bin:/usr/sbin:/sbin","PYTHONUNBUFFERED":"1"}}
        (output/(label+".plist")).write_bytes(plistlib.dumps(data))
    return output


def main():
    parser=argparse.ArgumentParser(description="macOS multi-project Architect bridge")
    parser.add_argument("--root",default=str(ROOT))
    sub=parser.add_subparsers(dest="action",required=True)
    for action in ["init","serve","execute-loop","on","off","reboot","status","doctor","keychain-init","owner-password","rotate"]: sub.add_parser(action)
    for action in ["execute-once","recover","retry"]:
        p=sub.add_parser(action); p.add_argument("project")
        if action=="retry": p.add_argument("task")
    p=sub.add_parser("launchd-generate"); p.add_argument("--output",default=str(ROOT/"macos/generated"))
    p=sub.add_parser("launchd-install"); p.add_argument("--directory",default=str(Path.home()/"Library/LaunchAgents"))
    args=parser.parse_args(); root,instance=load(args)
    runtime=root/".runtime"; runtime.mkdir(mode=0o700,exist_ok=True)
    runtime.chmod(0o700)
    if args.action=="keychain-init": Vault(instance["instance_id"]).init(); print("Keychain initialized; secrets withheld"); return
    if args.action=="owner-password":
        if not sys.stdout.isatty(): raise ValueError("Use this command in your own interactive terminal")
        print(Vault(instance["instance_id"]).get("owner")); return
    if args.action=="serve": asyncio.run(serve(root,instance)); return
    if args.action=="execute-loop": execute_loop(root,instance); return
    if args.action in {"status","doctor"}:
        s=status(root,instance)
        if args.action=="doctor":
            s["codex_available"]=Path(instance["codex_binary"]).is_file()
            s["projects"]=list(Registry(root/"config/projects").projects)
        print(json.dumps(s,ensure_ascii=False,indent=2))
        if args.action=="doctor" and not s["healthy"]: sys.exit(1)
        return
    if args.action=="init":
        registry=Registry(root/"config/projects")
        for project in registry.projects.values(): Queue(project)
        if not (runtime/"desired-state.json").exists(): atomic_json(runtime/"desired-state.json",{"state":"off"})
        print("Project queues initialized"); return
    if args.action in {"launchd-generate","launchd-install"}:
        folder=plists(root,instance,Path(args.output if args.action=="launchd-generate" else args.directory))
        if args.action=="launchd-install":
            # Installing agents does not turn the bridge on; desired state controls reachability.
            for file in sorted(folder.glob(LABEL+".*.plist")):
                target=f"gui/{os.getuid()}"
                subprocess.run(["/bin/launchctl","bootout",target,str(file)],capture_output=True)
                subprocess.run(["/bin/launchctl","bootstrap",target,str(file)],check=True)
        print("LaunchAgents prepared" if args.action=="launchd-generate" else "LaunchAgents installed"); return
    if args.action in {"on","off","reboot","rotate"}:
        with lock(runtime/"controller.lock"):
            previous=desired(root)
            if args.action=="reboot" and previous!="on": raise ValueError("Intentionally off; use on")
            if args.action=="rotate":
                # First close reachability, then invalidate encrypted OAuth state and owner.
                atomic_json(runtime/"desired-state.json",{"state":"off","updated_at":time.time()})
            if args.action in {"off","reboot","rotate"}:
                if args.action=="off": atomic_json(runtime/"desired-state.json",{"state":"off","updated_at":time.time()})
                for suffix in ("executor","server"):
                    subprocess.run(["/bin/launchctl","kill","SIGTERM",f"gui/{os.getuid()}/{LABEL}.{suffix}"],capture_output=True)
                deadline=time.time()+10
                while status(root,instance)["healthy"] and time.time()<deadline: time.sleep(.2)
                if status(root,instance)["healthy"]: raise ValueError("Service still running; stop foreground service first")
            if args.action=="rotate":
                import secrets
                Vault(instance["instance_id"]).set("owner",secrets.token_urlsafe(32))
                with sqlite_connection(runtime/"oauth.sqlite3") as db:
                    db.execute("DELETE FROM items WHERE kind!='client'")
                print("Bridge off; OAuth tokens revoked; owner password rotated"); return
            if args.action in {"on","reboot"}:
                atomic_json(runtime/"desired-state.json",{"state":"on","updated_at":time.time()})
                for suffix in ("server","executor"):
                    subprocess.run(["/bin/launchctl","kickstart",f"gui/{os.getuid()}/{LABEL}.{suffix}"],check=True)
                deadline=time.time()+10
                while not status(root,instance)["healthy"] and time.time()<deadline: time.sleep(.2)
                if not status(root,instance)["healthy"]: raise ValueError("Launchd started but health/OAuth checks failed")
            print(json.dumps(status(root,instance),indent=2)); return
    registry=Registry(root/"config/projects"); project=registry.project(args.project)
    if args.action=="execute-once":
        from .executor import run_once
        print(json.dumps(run_once(project,instance,runtime/"executions"),ensure_ascii=False,indent=2))
    else:
        queue=Queue(project)
        with lock(queue.root/".executor.lock",blocking=False):
            if args.action=="recover": queue.recover()
            else: queue.retry(args.task)
        print(json.dumps(queue.status(),ensure_ascii=False))


def sqlite_connection(path):
    import sqlite3
    return sqlite3.connect(path)


if __name__=="__main__":
    try: main()
    except (ValueError,FileNotFoundError,BlockingIOError) as error:
        print(str(error),file=sys.stderr)
        sys.exit(1)
