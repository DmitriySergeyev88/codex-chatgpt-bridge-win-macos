from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from .task_packet import validate_task_packet

MAX_BYTES = 256_000
ID = re.compile(r"^[A-Z][A-Z0-9_-]{2,63}$")
TEXT_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".css", ".scss", ".html", ".sql", ".sh", ".xml", ".svg", ".rs", ".go", ".java", ".cs", ".vue", ".svelte"}
DENIED = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".codex", "node_modules", ".next", ".pnpm-store", ".venv", "venv", "storage", "logs", "credentials", "secrets", "private", ".npmrc", ".pypirc", ".netrc", ".ds_store"}
SECRET = re.compile(r"(?i)(?:\b(?:sk-(?:proj-)?[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{20,}|AKIA[A-Z0-9]{16})\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)\b\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]{6,}|https?://[^\s/@]+:[^\s/@]+@)")


def public_text(value: str) -> str:
    value = re.sub(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "[REDACTED JWT]", value)
    return SECRET.sub("[REDACTED]", value)


def scrub(value):
    if isinstance(value,str): return public_text(value)
    if isinstance(value,list): return [scrub(v) for v in value]
    if isinstance(value,dict): return {k:scrub(v) for k,v in value.items()}
    return value


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        d = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


@contextlib.contextmanager
def lock(path: Path, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError("Symlink lock forbidden")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def denied_component(name: str) -> bool:
    n = name.lower()
    return (n in DENIED or n.startswith(".env") or any(x in n for x in ("credential", "secret", "password", "token", "id_rsa", "id_ed25519", "oauth", "auth.json")) or n.endswith((".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".log", ".jsonl")))


class Registry:
    def __init__(self, config: Path):
        self.config = config.resolve()
        self.root = self.config.parent.parent
        self.projects = {}
        for file in sorted(self.config.glob("*.json")):
            p = json.loads(file.read_text())
            if not p.get("enabled", True): continue
            pid = p["bridge_project_id"]
            if not ID.fullmatch(pid) or pid in self.projects: raise ValueError("Invalid/duplicate project id")
            if p["mode"] != "CHATGPT_ARCHITECT" or p["chatgpt_access"] != "READ_ONLY": raise ValueError("Only Architect READ_ONLY supported")
            workspace = Path(p["workspace"]).expanduser().resolve(strict=True)
            codex = Path(p["codex_workspace"]).expanduser().resolve(strict=True)
            if workspace != codex: raise ValueError("v1 requires one shared workspace for source and queue")
            if not workspace.is_dir() or workspace == Path.home() or workspace in Path.home().parents: raise ValueError("Workspace too broad")
            if not p.get("chatgpt_thread"): raise ValueError("Architect thread required")
            start=p.get("task_number_start",1)
            if type(start) is not int or start < 1 or start > 999999: raise ValueError("Invalid task_number_start")
            for old in self.projects.values():
                if workspace == old["root"] or workspace in old["root"].parents or old["root"] in workspace.parents: raise ValueError("Overlapping workspaces forbidden")
                if p["chatgpt_thread"] == old["chatgpt_thread"]: raise ValueError("Architect thread already assigned")
            p["root"] = workspace
            self.projects[pid] = p
        if not self.projects: raise ValueError("No enabled projects")

    def project(self, pid):
        if pid not in self.projects: raise ValueError("Unknown project")
        return self.projects[pid]


class SafeFiles:
    def __init__(self, project):
        self.root = project["root"]
        self.extra_deny = project.get("deny_paths", [])

    def parts(self, path, directory=False):
        if not isinstance(path, str) or len(path) > 1024 or "\x00" in path or "\\" in path: raise ValueError("Invalid path")
        parts = PurePosixPath(path).parts
        if PurePosixPath(path).is_absolute() or ".." in parts: raise ValueError("Path escape forbidden")
        if any(denied_component(x) for x in parts): raise ValueError("Restricted path")
        if parts and parts[0] == ".ai-bridge": raise ValueError("Use queue tools for bridge metadata")
        for blocked in self.extra_deny:
            if path == blocked or path.startswith(blocked.rstrip("/") + "/"): raise ValueError("Restricted path")
        if not directory and (not parts or (Path(parts[-1]).suffix.lower() not in TEXT_EXT and parts[-1] not in {"Dockerfile", "Makefile", "LICENSE"})): raise ValueError("Only approved text files readable")
        return parts

    @contextlib.contextmanager
    def open_fd(self, path, directory=False):
        parts = self.parts(path, directory)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for i, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if i < len(parts)-1 or directory: flags |= os.O_DIRECTORY
                nxt = os.open(part, flags, dir_fd=fd)
                os.close(fd); fd = nxt
            info = os.fstat(fd)
            if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1): raise ValueError("Only regular, non-hardlinked files allowed")
            yield fd
        finally: os.close(fd)

    def read(self, path):
        with self.open_fd(path) as fd:
            if os.fstat(fd).st_size > MAX_BYTES: raise ValueError("File too large; split it locally")
            data = os.read(fd, MAX_BYTES+1)
            if len(data) > MAX_BYTES or b"\x00" in data: raise ValueError("Binary/large content forbidden")
            return public_text(data.decode("utf-8"))

    def list(self, path="."):
        with self.open_fd(path, directory=True) as fd:
            items = []
            for name in sorted(os.listdir(fd)):
                rel = str(PurePosixPath(path) / name)
                try:
                    self.parts(rel, directory=True)
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode): continue
                    if stat.S_ISDIR(info.st_mode): items.append({"path": rel, "type": "directory"})
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        self.parts(rel)
                        items.append({"path": rel, "type": "file"})
                except (ValueError, OSError): continue
                if len(items) >= 1000: break
            return items

    def walk(self, path="."):
        stack = [path]; visited = 0
        while stack:
            folder = stack.pop()
            for item in self.list(folder):
                visited += 1
                if visited > 10000: return
                if item["type"] == "directory": stack.append(item["path"])
                else: yield item["path"]

    def search(self, query, path="."):
        if not query or len(query) > 256: raise ValueError("Query must have 1..256 characters")
        results = []
        for file in self.walk(path):
            try: content = self.read(file)
            except (ValueError, OSError, UnicodeError): continue
            for line, text in enumerate(content.splitlines(), 1):
                if query.casefold() in text.casefold():
                    results.append({"path": file, "line": line, "text": text[:1000]})
                    if len(results) >= 100: return {"matches": results, "truncated": True}
        return {"matches": results, "truncated": False}

    def git(self, action):
        # No caller-provided revision, arguments, shell, external diff or fsmonitor.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
        prefix = ["/usr/bin/git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "diff.external="]
        def run(args):
            r = subprocess.run(prefix + args, cwd=self.root, env=env, capture_output=True, timeout=15)
            if r.returncode: raise ValueError("Git operation failed")
            if len(r.stdout) > 4_000_000: raise ValueError("Git output too large")
            return r.stdout
        # git metadata itself may live outside workspace via worktrees; only fixed commands run.
        root = run(["rev-parse", "--show-toplevel"]).decode().strip()
        if Path(root).resolve() != self.root: raise ValueError("Workspace must be repository root")
        def allowed(file):
            try:
                self.parts(file)
                # Check parent chain and existing files, including symlinks/hardlinks.
                try:
                    with self.open_fd(file): pass
                except FileNotFoundError:
                    with self.open_fd(str(PurePosixPath(file).parent), directory=True): pass
                return True
            except (OSError, ValueError): return False
        if action == "status":
            entries = run(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"]).split(b"\0")
            return [public_text(e.decode()) for e in entries if e and allowed(e[3:].decode())][:1000]
        if action != "diff": raise ValueError("Unsupported Git operation")
        names = run(["diff", "--name-only", "-z", "--no-renames", "HEAD", "--"]).split(b"\0")
        diffs = []; size = 0
        for raw in names:
            if not raw: continue
            file = raw.decode()
            if not allowed(file): continue
            content = run(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD", "--", file]).decode()
            size += len(content)
            if size > MAX_BYTES: return {"diff": public_text("\n".join(diffs)), "truncated": True}
            diffs.append(content)
        return {"diff": public_text("\n".join(diffs)), "truncated": False}


class Queue:
    """SQLite is authoritative; JSON files are recoverable projections."""
    def __init__(self, project):
        self.project = project
        self.root = project["root"] / ".ai-bridge"
        self.pid = project["bridge_project_id"]
        if self.root.is_symlink(): raise ValueError("Bridge metadata must not be symlinked")
        self.root.mkdir(mode=0o700, exist_ok=True)
        for sub in ("tasks", "results", "reviews"):
            path = self.root / sub
            if path.is_symlink(): raise ValueError("Bridge metadata must not be symlinked")
            path.mkdir(mode=0o700, exist_ok=True)
        if (self.root / "queue.sqlite3").is_symlink(): raise ValueError("Symlink database forbidden")
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL, packet TEXT NOT NULL, result TEXT, review TEXT, run_id TEXT, child_pid INTEGER);
            CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history(seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, revision INTEGER, kind TEXT, payload TEXT, created REAL);
            """)
            if "child_pid" not in [r[1] for r in db.execute("PRAGMA table_info(tasks)")]:
                db.execute("ALTER TABLE tasks ADD COLUMN child_pid INTEGER")
            row = db.execute("SELECT value FROM meta WHERE key='project'").fetchone()
            if row and row[0] != self.pid: raise ValueError("Queue belongs to different project")
            db.execute("INSERT OR IGNORE INTO meta VALUES('project', ?)", (self.pid,))
        os.chmod(self.root / "queue.sqlite3", 0o600)
        arch = self.root / "architecture.md"
        if arch.is_symlink(): raise ValueError("Symlink architecture forbidden")
        if not arch.exists():
            arch.write_text(f"# {project.get('name', self.pid)}\n\nBridge: {self.pid}\nArchitect: {project['chatgpt_thread']}\n\nChatGPT designs and reviews. Codex edits source and runs tests.\nExisting accepted project specifications remain authoritative.\n")
        self.export()

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "queue.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    def event(self, db, task, revision, kind, payload):
        db.execute("INSERT INTO history(task, revision, kind, payload, created) VALUES(?,?,?,?,?)", (task, revision, kind, json.dumps(payload, ensure_ascii=False), time.time()))

    def idempotent(self, db, key, payload):
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", key): raise ValueError("Idempotency key required (8..128 safe characters)")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        row = db.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
        if row:
            if row["digest"] != digest: raise ValueError("Idempotency key reused with different payload")
            return digest, json.loads(row["response"])
        return digest, None

    def submit(self, title, instructions, acceptance, key):
        validate_task_packet(title,instructions,acceptance,key)
        payload = {"title": title, "instructions": instructions, "acceptance": acceptance}
        for field, value in payload.items():
            encoded=json.dumps(value)
            if public_text(encoded) != encoded: raise ValueError(f"Invalid task packet: {field}: potential credential detected; remove credential values, not specification text")
        with self.db() as db:
            digest, old = self.idempotent(db, key, payload)
            if old: return old
            if db.execute("SELECT 1 FROM tasks WHERE status != 'ACCEPTED'").fetchone(): raise ValueError("Accept the current task before next task")
            n = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] + self.project.get("task_number_start",1)
            tid = f"TASK-{n:03d}"
            packet = {"bridge_project_id": self.pid, "task_id": tid, "revision": 1, **payload}
            db.execute("INSERT INTO tasks(id,revision,status,packet) VALUES(?,1,'QUEUED',?)", (tid,json.dumps(packet,ensure_ascii=False)))
            self.event(db, tid, 1, "TASK", packet)
            response = {"task_id": tid, "revision": 1, "status": "QUEUED"}
            db.execute("INSERT INTO requests VALUES(?,?,?)", (key,digest,json.dumps(response)))
        self.export(); return response

    def claim(self):
        with self.db() as db:
            if db.execute("SELECT 1 FROM tasks WHERE status='RUNNING'").fetchone(): return None
            row = db.execute("SELECT * FROM tasks WHERE status='QUEUED' ORDER BY id LIMIT 1").fetchone()
            if not row: return None
            import uuid
            run = str(uuid.uuid4())
            db.execute("UPDATE tasks SET status='RUNNING',child_pid=NULL,run_id=? WHERE id=?", (run,row["id"]))
            self.event(db,row["id"],row["revision"],"CLAIM",{"run_id":run})
            packet = json.loads(row["packet"])
            packet["run_id"] = run
        self.export(); return packet

    def child(self, task, pid):
        with self.db() as db:
            db.execute("UPDATE tasks SET child_pid=? WHERE id=? AND run_id=? AND status='RUNNING'",(pid,task["task_id"],task["run_id"]))

    def finish(self, packet, result, success=True):
        # Only the local executor calls this; never exposed as an MCP write tool.
        if result.get("task_id") != packet["task_id"] or result.get("revision") != packet["revision"]: raise ValueError("Result task/revision mismatch")
        result = scrub(result)
        if len(json.dumps(result)) > MAX_BYTES: raise ValueError("Result too large")
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (packet["task_id"],)).fetchone()
            if not row or row["status"] != "RUNNING" or row["run_id"] != packet["run_id"] or row["revision"] != packet["revision"]: raise ValueError("Stale execution result")
            status = "AWAITING_REVIEW" if success else "EXECUTION_FAILED"
            db.execute("UPDATE tasks SET status=?,result=? WHERE id=?",(status,json.dumps(result,ensure_ascii=False),packet["task_id"]))
            self.event(db,packet["task_id"],packet["revision"],"RESULT",result)
        self.export()

    def review(self, tid, revision, verdict, feedback, key):
        if verdict not in {"ACCEPT", "FIX"} or not feedback or len(feedback)>12000: raise ValueError("Review requires ACCEPT/FIX and feedback")
        payload = {"task_id":tid,"revision":revision,"verdict":verdict,"feedback":feedback}
        if public_text(json.dumps(payload)) != json.dumps(payload): raise ValueError("Potential secret in review")
        with self.db() as db:
            digest, old = self.idempotent(db,key,payload)
            if old: return old
            row = db.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
            if not row or row["revision"]!=revision or row["status"] not in {"AWAITING_REVIEW","EXECUTION_FAILED"}: raise ValueError("Stale or premature review")
            if verdict == "ACCEPT" and row["status"] != "AWAITING_REVIEW": raise ValueError("Failed execution cannot be accepted")
            db.execute("UPDATE tasks SET review=?,status=? WHERE id=?",(json.dumps(payload,ensure_ascii=False),"ACCEPTED" if verdict=="ACCEPT" else "QUEUED",tid))
            self.event(db,tid,revision,"REVIEW",payload)
            if verdict == "FIX":
                packet = json.loads(row["packet"])
                packet["revision"] += 1
                packet["review_feedback"] = feedback
                db.execute("UPDATE tasks SET revision=?,packet=?,result=NULL,run_id=NULL WHERE id=?",(revision+1,json.dumps(packet,ensure_ascii=False),tid))
                self.event(db,tid,revision+1,"TASK",packet)
            response={"task_id":tid,"revision":revision+(verdict=="FIX"),"status":"ACCEPTED" if verdict=="ACCEPT" else "QUEUED"}
            db.execute("INSERT INTO requests VALUES(?,?,?)",(key,digest,json.dumps(response)))
        self.export(); return response

    def recover(self):
        # Caller holds project executor lock. Never automatically rerun ambiguous source changes.
        with self.db() as db:
            rows = db.execute("SELECT * FROM tasks WHERE status='RUNNING'").fetchall()
            for row in rows:
                if row["child_pid"]:
                    try:
                        os.kill(row["child_pid"],0)
                        continue  # Surviving executor or reused PID: conservatively wait.
                    except ProcessLookupError: pass
                db.execute("UPDATE tasks SET status='INTERRUPTED' WHERE id=?",(row["id"],))
                self.event(db,row["id"],row["revision"],"INTERRUPTED",{"reason":"Executor disappeared; inspect workspace before retry"})
        self.export()

    def retry(self, tid):
        with self.db() as db:
            row=db.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
            if not row or row["status"] not in {"INTERRUPTED","EXECUTION_FAILED"}: raise ValueError("Only interrupted/failed task can be retried")
            db.execute("UPDATE tasks SET status='QUEUED',run_id=NULL WHERE id=?",(tid,))
            self.event(db,tid,row["revision"],"RETRY",{})
        self.export()

    def status(self):
        with self.db() as db:
            return {"bridge_project_id":self.pid,"tasks":[dict(r) for r in db.execute("SELECT id,revision,status FROM tasks ORDER BY id")]}

    def details(self, tid):
        with self.db() as db:
            row=db.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
            if not row: raise ValueError("Unknown task")
            return {"task":json.loads(row["packet"]),"status":row["status"],"result":json.loads(row["result"]) if row["result"] else None,"history":[{"revision":r["revision"],"kind":r["kind"],"payload":json.loads(r["payload"])} for r in db.execute("SELECT * FROM history WHERE task=? ORDER BY seq",(tid,))]}

    def export(self):
        with lock(self.root / ".export.lock"):
            with self.db() as db:
                rows=db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
                history=db.execute("SELECT * FROM history WHERE kind IN ('TASK','RESULT','REVIEW') ORDER BY seq").fetchall()
            for event in history:
                folder={"TASK":"tasks","RESULT":"results","REVIEW":"reviews"}[event["kind"]]
                path=self.root/folder/f"{event['task']}-R{event['revision']}.json"
                if path.is_symlink() or path.parent.is_symlink(): raise ValueError("Symlink projection forbidden")
                atomic_json(path,json.loads(event["payload"]))
            path=self.root/"state.json"
            if path.is_symlink(): raise ValueError("Symlink state forbidden")
            atomic_json(path,{"schema_version":1,"bridge_project_id":self.pid,"architect_thread":self.project["chatgpt_thread"],"mode":"CHATGPT_ARCHITECT","chatgpt_access":"READ_ONLY","tasks":[{"id":r["id"],"revision":r["revision"],"status":r["status"]} for r in rows]})
