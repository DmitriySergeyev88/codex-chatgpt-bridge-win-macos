"""Supervise the official private tunnel; honor the bridge On/Off state."""
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading

from .cli import ROOT, desired
from .core import lock


def runtime_environment(root):
    path=root/".env.local"
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe local key file")
        content=os.read(fd,65536).decode()
    finally:
        os.close(fd)
    value=next(line.partition("=")[2].strip().strip("\"'") for line in content.splitlines() if line.startswith("OPENAI_API_KEY="))
    if not value.startswith("sk-") or any(c.isspace() for c in value):
        raise ValueError("Missing runtime key")
    env=os.environ.copy()
    env["CONTROL_PLANE_API_KEY"]=value
    return env


def main():
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    runtime=ROOT/".runtime"
    with lock(runtime/"tunnel.lock",blocking=False):
        while not stop.is_set():
            if desired(ROOT)!="on":
                stop.wait(2)
                continue
            child=None
            try:
                fd=os.open(runtime/"tunnel.log",os.O_CREAT|os.O_APPEND|os.O_WRONLY|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,"ab",buffering=0) as log:
                    child=subprocess.Popen([str(runtime/"tunnel-client"),"run","--config",str(runtime/"tunnel.yaml")],
                                           env=runtime_environment(ROOT),stdout=log,stderr=log)
                    while child.poll() is None and not stop.wait(2) and desired(ROOT)=="on":
                        pass
            except Exception:
                print("Tunnel needs attention; credentials withheld",flush=True)
            finally:
                if child and child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=15)
                    except subprocess.TimeoutExpired:child.kill();child.wait()
            stop.wait(10)


if __name__=="__main__":
    main()
