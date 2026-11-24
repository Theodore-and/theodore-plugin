"""Helpers for the service's tests: a home with a stand-in engine whose `engine.py serve` speaks contract C2, the
service started in it, and a client that talks C1 to it.

The stand-in engine's verbs:
  echo ...          stdout: one JSON line {argv, cwd, pid, env (the overlay's names)}; exit 0
  sleep S           progress frames, then exit 0 after S seconds (130 when cancelled first)
  crash             the worker dies at once
  crash-once FLAG   dies when FLAG does not exist (and makes it), else echoes
  fail N            exit N
  licence           today's licence JSON line, exit 77
  image PATH        stdout names PATH (the service attaches it); exit 0
  write FILE        `inputs` for FILE, `committed`, then writes it; exit 0
  slide list WS     a listing; exit 0
FAKE_WORKER_FATAL in the worker's environment makes it answer `fatal` instead of `ready`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import CLIENT_DIR  # noqa: E402

FAKE_ENGINE_PY = r'''
import json, os, struct, sys, threading, time, queue
VERSION = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "VERSION")).read().strip()
out = sys.stdout.buffer
lock = threading.Lock()
IN = sys.stdin.fileno()
def _read_exact(n):
    # os.read on the descriptor, never sys.stdin: a daemon thread blocked inside a buffered read holds the buffer's
    # lock, and the interpreter's shutdown then aborts ("could not acquire lock for <stdin>").
    chunks = []
    while n:
        chunk = os.read(IN, n)
        if not chunk:
            return None
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)
def send(frame):
    data = json.dumps(frame).encode("utf-8")
    with lock:
        out.write(struct.pack("<I", len(data)) + data)
        out.flush()
def read():
    head = _read_exact(4)
    if head is None:
        return None
    (n,) = struct.unpack("<I", head)
    body = _read_exact(n)
    return None if body is None else json.loads(body.decode("utf-8"))
if os.environ.get("FAKE_WORKER_FATAL"):
    send({"t": "fatal", "code": "IMPORT", "message": "a stand-in failure", "exit": 78})
    sys.exit(78)
send({"t": "ready", "v": 1, "protocol": 1, "engine": {"version": VERSION, "build": "test", "semantics": "b1"},
      "pid": os.getpid(), "warm_ms": 1, "rss_mb": 20.0, "verbs": ["echo", "sleep", "slide"]})
runs, cancelled, state = queue.Queue(), set(), {"calls": 0}
def reader():
    while True:
        frame = read()
        if frame is None:
            os._exit(0)                     # the service is gone: so is this worker
        kind = frame.get("t")
        if kind == "run":
            runs.put(frame)
        elif kind == "cancel":
            cancelled.add(frame.get("id"))
        elif kind == "stats":
            send({"t": "stats", "rss_mb": float(os.environ.get("FAKE_WORKER_RSS") or 20.0), "workspaces": [],
                  "requests": state["calls"], "uptime_s": 1})
        elif kind == "exit":
            runs.put(None)
threading.Thread(target=reader, daemon=True).start()
while True:
    run = runs.get()
    if run is None:
        sys.stdout.flush()
        os._exit(0)
    state["calls"] += 1
    call, argv, started = run["id"], run["argv"], time.monotonic()
    saved = {k: os.environ.get(k) for k in run.get("env") or {}}
    for k, v in (run.get("env") or {}).items():
        if v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    code = 0
    def say(text, stream="stdout"):
        send({"t": "out", "id": call, "stream": stream, "data": text})
    verb = argv[0] if argv else "--help"
    if "--crash-once" in argv:              # dies the first time (and leaves the flag), works the second
        flag = argv[argv.index("--crash-once") + 1]
        if verb == "write":
            path = argv[1]
            try:
                st = os.stat(path)
                tup = [path, st.st_size, st.st_mtime_ns, st.st_ctime_ns, str(st.st_ino)]
            except FileNotFoundError:
                tup = [path, -1, 0, 0, "0"]
            send({"t": "inputs", "id": call, "files": [tup]})
        if not os.path.exists(flag):
            open(flag, "w").close()
            if "--touch-first" in argv:
                with open(argv[1], "a") as handle:
                    handle.write("changed")
            os._exit(9)
    if verb == "crash":
        os._exit(9)
    if verb == "sleep":
        end = time.monotonic() + float(argv[1])
        while time.monotonic() < end:
            if call in cancelled:
                code = 130
                break
            send({"t": "progress", "id": call, "elapsed_ms": int((time.monotonic() - started) * 1000), "phase": "sleeping"})
            time.sleep(0.05)
        say(json.dumps({"slept": argv[1], "cancelled": code == 130}) + "\n")
    elif verb == "fail":
        code = int(argv[1])
        say(json.dumps({"ok": False}) + "\n")
    elif verb == "licence":
        say(json.dumps({"ok": False, "error": "refused for the test", "code": "entitlement_missing"}) + "\n")
        code = 77
    elif verb == "image":
        say(json.dumps({"ok": True, "rendered_images": [argv[1]]}) + "\n")
    elif verb in ("render", "apply") and len(argv) > 1 and argv[1].endswith(".jpg"):
        # as the engine answers: every page listed, the ones drawn anew named apart (apply), or the pages asked (render)
        pictures = argv[1:]
        answer = {"ok": True, "rendered_images": pictures}
        if verb == "apply":
            answer["render_summary"] = {"regenerated_pages": [int(n) for n in os.environ.get("FAKE_DRAWN", "2").split(",")]}
        say(json.dumps(answer, indent=2) + "\n")
    elif verb == "write":
        path = argv[1]
        try:
            st = os.stat(path)
            tup = [path, st.st_size, st.st_mtime_ns, st.st_ctime_ns, str(st.st_ino)]
        except FileNotFoundError:
            tup = [path, -1, 0, 0, "0"]
        send({"t": "inputs", "id": call, "files": [tup]})
        with open(path, "a") as handle:
            handle.write("x")
        send({"t": "committed", "id": call, "key": "0123456789abcdef"})
        say(json.dumps({"ok": True}) + "\n")
    else:
        names = sorted(k for k in os.environ if k.startswith(("THEODORE_", "CLAUDE")))
        say(json.dumps({"argv": argv, "cwd": run.get("cwd"), "pid": os.getpid(), "priority": run.get("priority"),
                        "key": run.get("key"), "env": {k: os.environ[k] for k in names if k in (run.get("env") or {})},
                        "resident": os.environ.get("THEODORE_RESIDENT"), "home": os.environ.get("THEODORE_HOME"),
                        "node_options": os.environ.get("NODE_OPTIONS"), "work": os.environ.get("THEODORE_WORK_DIR")}) + "\n")
        say("a line on standard error\n", "stderr")
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    send({"t": "done", "id": call, "exit": code, "elapsed_ms": int((time.monotonic() - started) * 1000), "key": run.get("key"),
          "counters": {"files_written": 0, "deckjs_full_parses": 0}})
'''

FAKE_ONESHOT_PY = r'''
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["refuse"]:
    print(json.dumps({"ok": False, "error": "refused", "code": "entitlement_missing"})); sys.exit(1)
print(json.dumps({"oneshot": True, "argv": args, "pid": os.getpid(), "cwd": os.getcwd()}))
'''


def install_engine(home: Path, version: str, serve: bool = True) -> Path:
    """A stand-in engine in <home>/engines/<version>, made current; with `serve`, its engine.py speaks C2."""
    root = home / "engines" / version
    scripts = root / "skills" / "pptx" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(version)
    (root / ".theodore-engine.json").write_text(json.dumps({"version": version, "sha256": "0" * 64, "kind": "test"}))
    (scripts / "pptx.py").write_text(FAKE_ONESHOT_PY)
    if serve:
        (scripts / "engine.py").write_text(FAKE_ENGINE_PY)
    venv = root / ".venv" / "bin"
    venv.mkdir(parents=True, exist_ok=True)
    python = venv / "python"
    if not python.exists():
        python.symlink_to(sys.executable)
    (home / "engines" / "current.json").write_text(json.dumps({"version": version}))
    return root


def make_runtime(home: Path) -> None:
    """runtime/current.txt naming a complete runtime (no interpreter: the engine's own is used)."""
    (home / "runtime" / "rt-test").mkdir(parents=True, exist_ok=True)
    (home / "runtime" / "rt-test" / ".ready").write_text("1")
    (home / "runtime" / "current.txt").write_text("rt-test")


_STARTED: list = []          # kept, so no "still running" warning is printed for a service the test stops itself


def start_service(env: dict, home: Path, wait: float = 20.0) -> dict:
    """The service, started detached as the start rule does, and its service.json once it listens."""
    (home / "logs").mkdir(parents=True, exist_ok=True)
    errors = open(home / "logs" / "service-test-stderr.log", "ab")
    proc = subprocess.Popen([sys.executable, "-I", str(CLIENT_DIR), "service", "--home", str(home)], env=env,
                            cwd=str(home), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                            start_new_session=True)
    _STARTED.append(proc)
    threading.Thread(target=proc.wait, daemon=True).start()        # reaped as the start rule's reaper does
    errors.close()
    deadline = time.monotonic() + wait
    path = home / "state" / "run" / "service.json"
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            time.sleep(0.02)
    logs = "".join(p.read_text(errors="replace")[-3000:] for p in (home / "logs").glob("service*.log"))
    raise AssertionError(f"the service did not write service.json:\n{logs}")


def alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(out) and not out.startswith("Z")


def wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def children_of(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    return [int(a) for a, b in (line.split() for line in out.splitlines() if line.strip()) if int(b) == int(pid)]


class Collector:
    """Frames by call id, and an event per call that sets on its done or error."""

    def __init__(self):
        self.frames: dict[str, list[dict]] = {}
        self.events: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.notifies: list[dict] = []

    def event(self, call_id: str) -> threading.Event:
        with self.lock:
            return self.events.setdefault(call_id, threading.Event())

    def __call__(self, frame: dict) -> None:
        call_id = frame.get("id")
        with self.lock:
            self.frames.setdefault(call_id, []).append(frame)
        if frame.get("t") in ("done", "error"):
            self.event(call_id).set()

    def notify(self, frame: dict) -> None:
        self.notifies.append(frame)

    def wait(self, call_id: str, timeout: float = 30.0) -> list[dict]:
        if not self.event(call_id).wait(timeout):
            raise AssertionError(f"no answer to {call_id}: {self.frames.get(call_id)}")
        return self.frames[call_id]

    @staticmethod
    def stdout(frames: list[dict]) -> str:
        return "".join(f.get("data", "") for f in frames if f["t"] == "out" and f.get("stream") == "stdout")

    @staticmethod
    def done(frames: list[dict]) -> dict:
        return next(f for f in frames if f["t"] in ("done", "error"))
