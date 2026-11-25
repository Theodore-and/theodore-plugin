"""Engine workers (DESIGN §3.2, §3.5, §4.4, §4.6; contract C2) and the one-shot path.

A worker is `<runtime python> -I <engine>/skills/pptx/scripts/engine.py serve --protocol 1`, warm, one engine version,
one call at a time, spoken to in C2 frames over its stdin and stdout. The pool keeps one worker of the current engine
warm and grows under load to one worker per core (a build is CPU-bound; the service, the relay and the host mostly
wait), within MEMORY_SHARE of the computer's memory at WORKER_MB a worker; it shrinks back after 5 idle minutes.
Several calls waiting at once start several workers at once, and a deck's `prepare` (its parts are coming) starts
them ahead of the designers' first applies. Waiting calls take workers in order of expected cost (reads, then writes
and a part's apply, then long verbs; B-A-worker 12), and long calls never hold every worker: one is kept for short
calls. A workspace key goes to the worker that served it last when that one is free; writes on one key run one at a
time in arrival order (the disk is the truth). A worker that dies answers its call with 75 (RETRY), or re-runs it once
when that is safe (C2 "Crashes"); one past 800 MB or 2,000 calls is replaced by a spare.

An engine without `engine.py` (up to rc.34), or a worker that cannot start, runs each command one-shot, as the client
runs it today, but started from the warm service (no launcher, no client start).
"""

from __future__ import annotations

import base64
import codecs
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import core, install
from . import frames

IS_WINDOWS = os.name == "nt"
READY_WAIT_S = 90.0
IDLE_SHRINK_S = 300.0
RECYCLE_RSS_MB = 800.0
WORKER_MB = 600.0                 # one warm worker with its Node and renderer, for the pool's memory cap
RENDER_THREAD_MB = 250.0          # each warm render thread beyond the first (lane R's budget, 25 Nov)
MEMORY_SHARE = 0.4                # at most this share of the computer's memory goes to workers
MOST_WORKERS = 16
RANKS = {"read": 0, "write": 1, "long": 2, "ahead": 3}      # the queue's order: expected cost (B-A-worker 12);
# "ahead": a Studio part built before its designer applies it (ahead.py), never queued: it takes an idle worker or none
WARM_WAIT_S = 0.5                 # a long call waits this long for the always-warm worker to finish a short call
RECYCLE_CALLS = 2000
EXIT_GRACE_MS = 30000
STATS_AFTER_S = 5.0
CREATE_NO_WINDOW = 0x08000000
# A call never waits for a worker that is gone or stuck (the coordinator, 25 Nov, after K's A3b hang on rig 1):
CALL_CEILING_S = float(os.environ.get("THEODORE_CALL_CEILING_S") or 900.0)   # no engine command runs longer
HEARTBEAT_S = float(os.environ.get("THEODORE_WORKER_HEARTBEAT_S") or 60.0)   # silent this long during a call: stats
HEARTBEAT_GRACE_S = float(os.environ.get("THEODORE_WORKER_HEARTBEAT_GRACE_S") or 30.0)   # no answer: it is stuck


def lost_text(how: str, wrote: bool) -> str:
    """The answer to a call whose worker ended or stuck and that is not run again (exit 78)."""
    if wrote:
        return (f"THEODORE INTERNAL: Theodore's engine {how} while this command was writing; part of its result may be "
                f"on disk.\nNEXT: run the same command again: it starts from what is on disk.\n"
                f"TELL THE USER: nothing unless it happens again.\n")
    return (f"THEODORE INTERNAL: Theodore's engine {how} during this command; nothing of it was written.\n"
            f"NEXT: run the same command again; if it happens again, run theodore doctor.\n"
            f"TELL THE USER: nothing unless it happens again.\n")

# --- what a call is -----------------------------------------------------------------------------------------------------

LONG_VERBS = ("start", "apply", "finish", "render", "prepare", "split", "check", "rebase", "project", "task")
READ_VERBS = ("list", "assets", "doctor", "cache", "--help", "-h", "help", "--version", "version")


def classify(argv: list[str]) -> str:
    """read, write or long (C2 `priority`)."""
    verb = argv[0] if argv else "--help"
    if verb in ("--help", "-h", "help") or "--help" in argv[:3] or "-h" in argv[:2]:
        return "read"
    if verb == "slide":
        sub = argv[1] if len(argv) > 1 else ""
        if sub in ("list", "show", "open"):
            return "read"
        return "long" if sub == "import" else "write"
    if verb == "comments":
        return "long" if "--translate" in argv else ("read" if len(argv) < 2 or argv[1] in ("list", "show") else "write")
    if verb == "doctor":
        return "long" if "--powerpoint" in argv else "read"
    if verb == "cache":
        return "write" if len(argv) > 1 and argv[1] == "prune" else "read"
    if verb in READ_VERBS:
        return "read"
    if verb in LONG_VERBS:
        return "long"
    return "write"


def rank(argv: list[str], cwd: str | None) -> int:
    """The order a waiting call takes a worker in: reads, then writes and a part's apply (lane G's parts: about 100 ms,
    the burst of a Studio deck's designers), then long verbs."""
    priority = classify(argv)
    if priority == "long" and part_apply(argv, cwd):
        return RANKS["write"]
    return RANKS.get(priority, RANKS["write"])


def part_apply(argv: list[str], cwd: str | None) -> bool:
    """`apply <lead>/parts/<part>`: a Studio part's apply (lane G)."""
    if not argv or argv[0] != "apply":
        return False
    target = next((arg for arg in argv[1:] if not arg.startswith("-")), None)
    if not target:
        return False
    path = Path(target) if os.path.isabs(target) else Path(cwd or ".") / target
    return path.parent.name == "parts"


def _is_workspace(folder: Path) -> str | None:
    """The workspace key of a folder (C3 marker key, or the 1.x key: sha256 of the real path), or None."""
    try:
        marker = folder / ".theodore.json"
        if marker.is_file():
            key = json.loads(marker.read_text(encoding="utf-8")).get("key")
            if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{16}", key):
                return key
        if (folder / ".pptx-state").is_dir() or (folder / "task.json").is_file():
            return hashlib.sha256(os.path.realpath(str(folder)).encode("utf-8")).hexdigest()[:16]
    except (OSError, ValueError):
        return None
    return None


def workspace_key(argv: list[str], cwd: str | None) -> str | None:
    """The workspace a call works on: the first argument that is a workspace folder (absolute or under cwd), else None."""
    for arg in argv[1:]:
        if not arg or arg.startswith("-"):
            continue
        try:
            path = Path(arg)
            if not path.is_absolute() and cwd:
                path = Path(cwd) / path
            if path.is_dir():
                key = _is_workspace(path)
                if key:
                    return key
        except (OSError, ValueError):
            continue
    return None


# --- one engine version ---------------------------------------------------------------------------------------------------

class Spec:
    def __init__(self, version: str):
        self.version = version
        self.directory = install.engine_dir(version)
        scripts = self.directory / "skills" / "pptx" / "scripts"
        self.serve_script = scripts / "engine.py"
        self.oneshot_script = scripts / "pptx.py"

    def resident(self) -> bool:
        if (os.environ.get("THEODORE_RESIDENT") or "").strip() == "0":
            return False
        return self.serve_script.is_file()

    def python(self) -> str:
        """The runtime's base interpreter (no venv redirector), as C2 says; else the interpreter this service runs on
        (the runtime's own when installed; a checkout's when run from source)."""
        runtime = core.home().runtime_python()
        if runtime is not None and runtime.is_file():
            return str(runtime)
        return sys.executable

    def oneshot_python(self) -> str:
        """Today's interpreter for an engine run one-shot: the engine's venv, else the service's own."""
        venv = install.engine_python(self.directory)
        return str(venv) if venv.is_file() else sys.executable


def _child_flags() -> dict:
    return {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}


class _Job:
    """A Windows job object with KILL_ON_JOB_CLOSE: every worker, and what it starts, ends with the service."""

    def __init__(self):
        self.handle = None
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000 | 0x800     # KILL_ON_JOB_CLOSE, BREAKAWAY_OK (Office stays out)
            if kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.handle = job
                self._kernel32 = kernel32
        except Exception:       # noqa: BLE001  without a job the workers still end on their stdin's EOF
            self.handle = None

    def assign(self, process: subprocess.Popen) -> None:
        if self.handle is None:
            return
        with contextlib.suppress(Exception):
            self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle))      # noqa: SLF001


class WorkerFailed(Exception):
    """The worker died, or never became ready."""


class Worker:
    _counter = 0
    _counter_lock = threading.Lock()

    def __init__(self, pool: "Pool", spec: Spec, group: str | None = None, env: dict | None = None):
        self.pool = pool
        self.spec = spec
        self.group = group or spec.version          # the engine version, or a chat folder's confined workers
        self.base_env = env                         # that confinement's environment (chatbuild.restricted_env)
        self.process: subprocess.Popen | None = None
        self.channel: frames.Channel | None = None
        self.ready = threading.Event()
        self.dead = threading.Event()
        self.fatal: dict | None = None
        self.info: dict = {}
        self.busy = False
        self.rank: int | None = None                # the rank of the call it runs (RANKS)
        self.build_threads: int | None = None       # THEODORE_BUILD_THREADS for its Node builds (lane F), when set
        self.render_threads: int | None = None      # THEODORE_RENDER_THREADS for its drawing pool (lane R), when set
        self.started_perf = time.perf_counter()
        self.retiring = False
        self.calls = 0
        self.rss_mb = 0.0
        self.last_used = time.monotonic()
        self.keys: set[str] = set()
        self._run_id = None
        self._result: dict | None = None
        self._result_ready = threading.Event()
        self._call = None
        self._inputs: list | None = None
        self._committed = False
        self._lock = threading.Lock()
        self._gone_lock = threading.Lock()
        self._gone_done = False
        self.exit_code: int | None = None
        self.last_frame = time.monotonic()
        self.lost: str | None = None                # why a call it held was lost (ended, stuck, past the ceiling)

    @classmethod
    def _next_id(cls) -> str:
        with cls._counter_lock:
            cls._counter += 1
            return f"w{cls._counter}"

    # --- life ---------------------------------------------------------------------------------------------------------

    def start(self) -> None:
        env = dict(self.base_env) if self.base_env is not None else install.engine_env(self.spec.directory)
        # A confined worker keeps its store under its private home (the folders its Node may touch).
        work = Path(env["THEODORE_HOME"]) / "work" if self.base_env is not None and env.get("THEODORE_HOME") \
            else core.home().root / "work"           # its own folder between calls (lane B: B-A-worker.md, 1)
        env["THEODORE_RESIDENT"] = "1"
        env["THEODORE_WORK_DIR"] = str(work)
        if self.build_threads:
            env["THEODORE_BUILD_THREADS"] = str(self.build_threads)
        if self.render_threads:
            env["THEODORE_RENDER_THREADS"] = str(self.render_threads)     # 1: draws on its own thread, no pool
        env["PYTHONUNBUFFERED"] = "1"
        command = [self.spec.python(), "-I", str(self.spec.serve_script), "serve", "--protocol", str(frames.PROTOCOL)]
        self.started_perf = time.perf_counter()
        work.mkdir(parents=True, exist_ok=True)
        # Unbuffered pipes: no buffer lock a reader thread could hold when the service exits (Python aborts at
        # shutdown on a daemon thread blocked inside a buffered read).
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=env, cwd=str(work), close_fds=True, bufsize=0, **_child_flags())
        self.pool.job.assign(self.process)
        self.channel = frames.Channel(self.process.stdout, writer=self.process.stdin, name=f"worker {self.spec.version}")
        threading.Thread(target=self._read_loop, name=f"worker-read-{self.process.pid}", daemon=True).start()
        threading.Thread(target=self._stderr_loop, name=f"worker-err-{self.process.pid}", daemon=True).start()
        # Its end is seen from its process, not only from its output: a child of it (an office it started) may hold
        # that pipe open after it is gone (K's A3b on rig 1: a LibreOffice crash, the worker gone, its call waiting).
        threading.Thread(target=self._watch_exit, name=f"worker-exit-{self.process.pid}", daemon=True).start()

    def _watch_exit(self) -> None:
        with contextlib.suppress(Exception):
            self.process.wait()
        self._gone("its process ended")

    def wait_ready(self, timeout: float = READY_WAIT_S) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.ready.wait(0.05):
                return True
            if self.dead.is_set():
                return False
        return self.ready.is_set()

    def _stderr_loop(self) -> None:
        stream = self.process.stderr
        pid = self.process.pid
        pending = b""
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line:
                        self.pool.service.log(f"[worker {self.spec.version} {pid}] {line}")
        except (OSError, ValueError):
            pass
        if pending.strip():
            self.pool.service.log(f"[worker {self.spec.version} {pid}] {pending.decode('utf-8', 'replace').rstrip()}")

    def _read_loop(self) -> None:
        try:
            while True:
                frame = self.channel.read()
                if frame is None:
                    break
                self._on_frame(frame)
        except (frames.FrameError, OSError, ValueError) as exc:
            self.pool.service.log(f"[worker {self.spec.version}] frames ended: {exc}")
        finally:
            with contextlib.suppress(Exception):
                self.process.wait(timeout=5)
            with contextlib.suppress(Exception):
                self.process.stdout.close()
            self._gone("its output ended")

    def _gone(self, why: str) -> None:
        """The worker is gone (its process ended, or its output did): once, whichever is seen first. Logged with its
        exit code; a call waiting on it wakes (Worker.run raises WorkerFailed) and is run again or answered."""
        with self._gone_lock:
            if self._gone_done:
                return
            self._gone_done = True
        self.dead.set()
        with contextlib.suppress(Exception):
            self.exit_code = self.process.poll()
        if not self.ready.is_set() and self.fatal is None and self.exit_code == 77:
            # A release build refuses the licence as its compiled container loads, before any frame: today's
            # licence JSON line and exit 77 (lane B, B-A-worker.md 3).
            self.fatal = {"t": "fatal", "code": "LICENCE", "message": "the engine refused the licence", "exit": 77}
        with contextlib.suppress(Exception):
            running = self._call is not None
            self.pool.service.log(f"worker {self.spec.version} (pid {self.process.pid}) ended: exit {self.exit_code}, "
                                  f"{why}{'; a call was running on it' if running else ''}")
        with contextlib.suppress(Exception):
            self.process.stdin.close()
        self._result_ready.set()            # a call waiting for its done learns the worker is gone
        self.pool.worker_died(self)

    def _on_frame(self, frame: dict) -> None:
        self.last_frame = time.monotonic()
        kind = frame.get("t")
        if kind == "ready":
            self.info = frame
            self.rss_mb = float(frame.get("rss_mb") or 0)
            self.ready.set()
            with contextlib.suppress(Exception):     # what a proof reads: when a worker was warm, and what it can do
                self.pool.service.log(
                    f"worker {self.spec.version} ready {int((time.perf_counter() - self.started_perf) * 1000)} ms after "
                    f"its start (its warm-up {frame.get('warm_ms')} ms, {self.rss_mb:.0f} MB, pid {frame.get('pid')}"
                    f"{', ' + self.group if self.group != self.spec.version else ''}; features: "
                    f"{', '.join(frame.get('features') or []) or 'none'})")
            self.pool.worker_ready(self)
            return
        if kind == "fatal":
            self.fatal = frame
            self.pool.service.log(f"[worker {self.spec.version}] fatal {frame.get('code')}: {frame.get('message')}")
            return
        if kind == "stats":
            self.rss_mb = float(frame.get("rss_mb") or self.rss_mb)
            return
        if kind == "log":
            self.pool.service.log(f"[worker {self.spec.version} {frame.get('level')}] {frame.get('message')}")
            return
        call = self._call
        if call is None or frame.get("id") != self._run_id:
            return
        if kind == "out":
            call.out(str(frame.get("data") or ""), "stderr" if frame.get("stream") == "stderr" else "stdout")
        elif kind == "progress":
            call.set_phase(str(frame.get("phase") or call.phase), frame.get("done"), frame.get("total"))
        elif kind == "image":
            call.image(str(frame.get("mime") or "image/jpeg"), str(frame.get("path") or ""), frame.get("data"),
                       frame.get("slide"), frame.get("page"), frame.get("key"))
            call.worker_images = True
        elif kind == "inputs":
            self._inputs = list(frame.get("files") or [])
        elif kind == "committed":
            self._committed = True
        elif kind == "done":
            self._result = frame
            self._result_ready.set()

    # --- calls ----------------------------------------------------------------------------------------------------------

    def run(self, call, argv: list[str], env: dict, stdin: dict | None, key: str | None, priority: str) -> dict:
        """Send one `run` and wait for its `done`. Returns the done frame plus `inputs` and `committed`; raises
        WorkerFailed when the worker died before its done."""
        with self._lock:
            self._run_id = self._next_id()
            self._result, self._inputs, self._committed = None, None, False
            self._result_ready.clear()
            self._call = call
        frame = {"t": "run", "id": self._run_id, "argv": argv, "cwd": call.cwd, "env": env, "key": key,
                 "priority": priority, "images": bool(call.images)}
        if stdin is not None:
            frame["stdin"] = stdin
        if call.deadline_ms:
            frame["deadline_ms"] = int(call.deadline_ms)
        try:
            self.channel.send(frame)
        except (OSError, ValueError, frames.FrameError) as exc:
            self._call = None
            raise WorkerFailed(f"the worker could not take the call ({exc})") from None
        call.worker = self
        if call.cancelled.is_set():
            # Cancelled after the pool chose this worker and before the router could see it here: the cancel goes now
            # (a second one, when the router's also went, is harmless).
            self.cancel(call)
        self._wait_done()
        call.worker = None
        self._call = None
        result = self._result
        if result is None:
            raise WorkerFailed("the worker ended during the call")
        self.calls += 1
        self.last_used = time.monotonic()
        if result.get("key"):
            self.keys.add(result["key"])
        return {**result, "inputs": self._inputs, "committed": self._committed}

    def _wait_done(self) -> None:
        """Wait for this run's done, never past CALL_CEILING_S, and never on a worker that stopped answering: one
        silent for HEARTBEAT_S is asked for its stats, and one that does not answer them within HEARTBEAT_GRACE_S
        is stopped. Either way the call is answered (Pool.run), and the next runs on a fresh worker."""
        started, asked = time.monotonic(), None
        while not self._result_ready.wait(1.0):
            now = time.monotonic()
            if now - started > CALL_CEILING_S:
                self._stop(f"did not finish within {int(CALL_CEILING_S)} s", "was stopped: it did not finish in time")
                break
            if asked is None and now - max(self.last_frame, started) > HEARTBEAT_S:     # silent during this call
                asked = now
                self.ask_stats()
            elif asked is not None and self.last_frame >= asked:
                asked = None                # it answered: alive
            elif asked is not None and now - asked > HEARTBEAT_GRACE_S:
                self._stop("stopped answering", "stopped answering")
                break

    def _stop(self, why: str, how: str) -> None:
        self.lost = how
        with contextlib.suppress(Exception):
            self.pool.service.log(f"worker {self.spec.version} (pid {self.process.pid}) {why} during a call; stopping it")
        self.kill()
        self._result_ready.set()

    def cancel(self, call) -> None:
        if self._call is call and self._run_id:
            with contextlib.suppress(Exception):
                self.channel.send({"t": "cancel", "id": self._run_id})

    def ask_stats(self) -> None:
        with contextlib.suppress(Exception):
            self.channel.send({"t": "stats"})

    def drop(self, key: str | None) -> None:
        with contextlib.suppress(Exception):
            self.channel.send({"t": "drop", "key": key})

    def retire(self, grace_ms: int = EXIT_GRACE_MS, final: bool = False) -> None:
        """`exit`. A recycle or a shrink lets go of the office the workers share (R's one office per user); the
        service's own end (`final`) says `"offices": "stop"`, and the worker stops it (C2; B and R, 25 Nov)."""
        self.retiring = True
        frame = {"t": "exit", "grace_ms": int(grace_ms)}
        if final:
            frame["offices"] = "stop"
        with contextlib.suppress(Exception):
            self.channel.send(frame)

    def kill(self) -> None:
        """End the process; its reader thread then sees the end of its stdout and closes it (a buffered stream closed
        from another thread while one reads it would wait for that read forever)."""
        with contextlib.suppress(Exception):
            if self.process and self.process.poll() is None:
                self.process.kill()
        with contextlib.suppress(Exception):
            self.process.stdin.close()


# --- the pool -------------------------------------------------------------------------------------------------------------

def _ram_bytes() -> int:
    try:
        if IS_WINDOWS:
            import ctypes

            class Memory(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            memory = Memory()
            memory.dwLength = ctypes.sizeof(Memory)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
            return int(memory.ullTotalPhys)
        if sys.platform == "darwin":
            out = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, timeout=5)
            return int(out.stdout.strip() or 0)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:       # noqa: BLE001
        return 8 << 30


def pool_sizes() -> tuple[int, int]:
    """(base, most) workers per engine version: one kept warm, and under load one per core within MEMORY_SHARE of the
    memory at WORKER_MB each (2 at least, MOST_WORKERS at most). THEODORE_SERVICE_WORKERS sets both."""
    forced = os.environ.get("THEODORE_SERVICE_WORKERS")
    if forced and forced.isdigit() and int(forced) > 0:
        return int(forced), int(forced)
    cores = os.cpu_count() or 2
    budget = _ram_bytes() * MEMORY_SHARE / (1 << 20)
    most = max(2, min(cores, int(budget // WORKER_MB), MOST_WORKERS))

    def worker_mb(threads: int) -> float:       # each render thread beyond a worker's first costs memory too
        return WORKER_MB + (threads - 1) * RENDER_THREAD_MB
    # The warm worker at the machine's full thread counts, those a burst adds at the per-worker cap.
    while most > 2 and worker_mb(full_threads()) + (most - 1) * worker_mb(worker_threads(most)) > budget:
        most -= 1
    return 1, most


def full_threads() -> int:
    """The always-warm worker's threads, build and render: one fewer than the processors, at most 4 (the engine's own
    rule): it serves the lone designer and the big renders (the coordinator, 25 Nov)."""
    return min(4, max(1, (os.cpu_count() or 2) - 1))


def worker_threads(most: int) -> int:
    """Threads of a worker started under load, in a pool of `most`: max(1, cores // most), at most 4 (lanes F, R)."""
    return min(4, max(1, (os.cpu_count() or 2) // max(1, most)))


class _KeyLock:
    """Writes on one workspace key run one at a time, in arrival order."""

    def __init__(self):
        self._lock = threading.Lock()
        self._held: dict[str, list[threading.Event]] = {}

    @contextlib.contextmanager
    def held(self, key: str | None):
        if not key:
            yield
            return
        mine = threading.Event()
        with self._lock:
            queue = self._held.setdefault(key, [])
            queue.append(mine)
            before = queue[-2] if len(queue) > 1 else None
        if before is not None:
            while not before.wait(0.5):
                with self._lock:
                    if before not in self._held.get(key, []):
                        break
        try:
            yield
        finally:
            with self._lock:
                queue = self._held.get(key, [])
                if mine in queue:
                    queue.remove(mine)
                if not queue:
                    self._held.pop(key, None)
            mine.set()

    def try_take(self, key: str | None) -> threading.Event | None:
        """The key at once when no command holds it or waits for it (an ahead never waits behind a write); else None.
        Given back with give()."""
        mine = threading.Event()
        if not key:
            return mine
        with self._lock:
            if self._held.get(key):
                return None
            self._held[key] = [mine]
        return mine

    def give(self, key: str | None, mine: threading.Event) -> None:
        if key:
            with self._lock:
                queue = self._held.get(key, [])
                if mine in queue:
                    queue.remove(mine)
                if not queue:
                    self._held.pop(key, None)
        mine.set()


class _Taken:
    """An idle worker and a part's workspace key, taken together for an ahead (Pool.take_for_ahead)."""

    def __init__(self, worker: "Worker", key: str | None, token: threading.Event):
        self.worker, self.key, self.token = worker, key, token


class Pool:
    def __init__(self, service):
        self.service = service
        self.job = _Job()
        self.base, self.most = pool_sizes()
        self.workers: dict[str, list[Worker]] = {}
        self.affinity: dict[str, Worker] = {}
        self.cond = threading.Condition()
        self.waiting: dict[str, list[tuple[int, int]]] = {}    # per group: the waiting calls' (rank, arrival)
        self._arrivals = 0
        self.on_release: list = []                  # called with the version when a worker is free again (parts.py)
        self.on_starved: list = []                  # called with the version when a call finds no idle worker (ahead.py)
        self.features: dict[str, set] = {}          # per engine version: what a ready worker of it said it can do
        self.keys = _KeyLock()
        self.closed = False
        self.oneshot_until: dict[str, float] = {}    # versions whose workers failed to start: one-shot until then
        self._engine_run = None

    # --- facts ------------------------------------------------------------------------------------------------------

    def engines_summary(self) -> list[dict]:
        current = install.current_engine()
        with self.cond:
            warm = {v for v, ws in self.workers.items() if any(w.ready.is_set() and not w.dead.is_set() for w in ws)}
        versions = sorted(set(install.installed_versions()) | warm) if current else sorted(warm)
        return [{"version": v, "current": v == current, "warm": v in warm} for v in versions[-8:]]

    # --- workers ----------------------------------------------------------------------------------------------------

    def _hold_engine_run(self) -> None:
        """While any worker lives, the service holds one engine-run lock (withdraw.engine_run): nothing of the engine
        is removed under a warm worker."""
        if self._engine_run is not None:
            return
        from .. import withdraw
        try:
            run = withdraw.engine_run()
            run.__enter__()
            self._engine_run = run
        except Exception as exc:        # noqa: BLE001
            self.service.log(f"engine-run lock not taken: {exc}")

    def _release_engine_run(self) -> None:
        run, self._engine_run = self._engine_run, None
        if run is not None:
            with contextlib.suppress(Exception):
                run.__exit__(None, None, None)

    def _spawn(self, spec: Spec, group: str | None = None, env: dict | None = None, limit: int | None = None) -> Worker | None:
        """A new worker of the group, started; None when `limit` workers of it live already. The first of an engine
        (the always-warm one) builds and draws on the machine's full thread counts; one started under load, beside
        others, on max(1, cores // pool size) (THEODORE_BUILD_THREADS, lane F, and THEODORE_RENDER_THREADS, lane R;
        the coordinator, 25 Nov), so a burst's builds and pictures share the cores instead of oversubscribing them."""
        worker = Worker(self, spec, group, env)
        with self.cond:
            live = self._live(worker.group)
            if limit is not None and len(live) >= limit:
                return None
            if env is None:
                worker.build_threads = worker.render_threads = self._threads_for_new(live)     # lanes F and R
            self.workers.setdefault(worker.group, []).append(worker)
        self._hold_engine_run()
        try:
            worker.start()
        except OSError as exc:
            with self.cond:
                self.workers[worker.group].remove(worker)
                self.cond.notify_all()
            raise WorkerFailed(f"the worker could not start ({exc})") from None
        threading.Thread(target=self._watch_start, args=(worker,), name="worker-start", daemon=True).start()
        return worker

    def _watch_start(self, worker: Worker) -> None:
        ok = worker.wait_ready(READY_WAIT_S)
        with self.cond:
            if not ok or worker.fatal:
                self.service.log(f"worker {worker.spec.version} did not become ready"
                                 f"{': ' + str(worker.fatal.get('code')) if worker.fatal else ''}")
                # One-shot for a while: a licence refusal answers through the one-shot engine's own JSON (and a new
                # sign-in brings workers back within a minute); anything else is tried again after 10 minutes.
                wait = 60.0 if worker.fatal and worker.fatal.get("code") == "LICENCE" else 600.0
                self.oneshot_until[worker.spec.version] = time.monotonic() + wait
                worker.kill()
            self.cond.notify_all()

    def warm_current(self) -> None:
        """At session open: a worker of the current engine, started in the background, so the first command finds it."""
        def warm() -> None:
            with contextlib.suppress(Exception):
                version = install.selected_engine()
                if not version:
                    return
                spec = Spec(version)
                if not spec.resident() or self._oneshot(version):
                    return
                with self.cond:
                    live = [w for w in self.workers.get(version, []) if not w.dead.is_set() and not w.retiring]
                if not live:
                    self._spawn(spec)
        threading.Thread(target=warm, name="pool-warm", daemon=True).start()

    def _choose(self, live: list, idle: list, key: str | None, rank: int, waited: float):
        """Which idle worker takes a call, or None to wait a moment. The always-warm worker (the machine's full thread
        counts: lane R's warm render threads) is kept for long calls, a lead's apply and a render: a long call takes it
        when it is idle, and waits up to WARM_WAIT_S for it while it runs a short call; short calls take another idle
        worker first, and it only when it is the one idle. Otherwise the workspace's own worker, else the least used."""
        def warm(worker) -> bool:
            return getattr(worker, "build_threads", None) == full_threads() and \
                any(getattr(w, "build_threads", None) != full_threads() for w in live)
        if rank == RANKS["long"]:
            for worker in idle:
                if warm(worker):
                    return worker
            if waited < WARM_WAIT_S and any(warm(w) and w.busy and (w.rank or 0) < RANKS["long"] for w in live):
                return None
        else:
            others = [w for w in idle if not warm(w)]
            if others:
                idle = others
        if key and self.affinity.get(key) in idle:
            return self.affinity[key]
        return min(idle, key=lambda w: (len(w.keys), w.calls))

    def _threads_for_new(self, live: list) -> int:
        """Full counts for the only worker of an engine (or the first again after one died); the per-worker cap for
        one started beside others, unless none of those holds the full counts (it went: the new one takes them)."""
        if not any(getattr(w, "build_threads", None) == full_threads() for w in live) or not live:
            return full_threads()
        return worker_threads(self.most)

    def warm_up(self, version: str, count: int | None = None) -> None:
        """Workers of the engine started now, side by side and in the background, up to `count` (the pool's size):
        a Studio deck's `prepare` means its designers' applies are coming, and each should find a warm worker."""
        count = self.most if count is None else min(count, self.most)

        def one(spec: Spec) -> None:
            with contextlib.suppress(Exception):
                self._spawn(spec, limit=count)
        with contextlib.suppress(Exception):
            spec = Spec(version)
            if not spec.resident() or self._oneshot(version):
                return
            with self.cond:
                missing = count - len(self._live(version))
            for _ in range(max(0, missing)):
                threading.Thread(target=one, args=(spec,), name="pool-warm-up", daemon=True).start()

    def worker_ready(self, worker: "Worker") -> None:
        said = [f for f in (worker.info.get("features") or []) if isinstance(f, str)]
        if said:
            self.features.setdefault(worker.spec.version, set()).update(said)
        with self.cond:
            self.cond.notify_all()
        for hook in list(self.on_release):
            with contextlib.suppress(Exception):
                hook(worker.group)

    def parts_capability(self, version: str) -> bool | None:
        """Whether the engine takes several Studio parts in one apply: True once a worker of it said so (remembered
        for the service's life), False when a ready worker did not, None while none has been ready yet."""
        if "apply-parts" in self.features.get(version, ()):
            return True
        with self.cond:
            ready = [w for w in self._live(version) if w.ready.is_set()]
        return False if ready else None

    def has_feature(self, version: str, name: str) -> bool:
        """A ready worker of the engine listed `name` in its `ready.features` (C2), remembered for the service's life."""
        return name in self.features.get(version, ())

    def can_apply_parts(self, version: str) -> bool:
        """A ready worker of the engine said it takes several Studio parts in one apply (`ready.features`)."""
        with self.cond:
            return any("apply-parts" in (w.info.get("features") or []) for w in self._live(version) if w.ready.is_set())

    def idle_count(self, version: str) -> int:
        with self.cond:
            return sum(1 for w in self._live(version) if w.ready.is_set() and not w.busy and not w.fatal)

    def _oneshot(self, version: str) -> bool:
        until = self.oneshot_until.get(version)
        if until is None:
            return False
        if time.monotonic() >= until:
            self.oneshot_until.pop(version, None)
            return False
        return True

    def _live(self, version: str) -> list[Worker]:
        return [w for w in self.workers.get(version, []) if not w.dead.is_set() and not w.retiring]

    def _acquire(self, spec: Spec, key: str | None, priority: str, call, group: str | None = None, env: dict | None = None,
                 most: int | None = None, rank: int | None = None) -> Worker:
        """A ready, idle worker of the version (or of a confined group) for this call. Waiting calls take workers in
        order of (rank, arrival); a long call never takes the last worker long calls could hold (one stays for short
        calls); with no idle worker the pool grows, as many starts at once as calls wait, up to `most`."""
        group = group or spec.version
        most = self.most if most is None else most
        rank = RANKS.get(priority, RANKS["write"]) if rank is None else rank
        long_most = max(1, most - 1)
        spawned = starved_told = False
        waited_from = time.monotonic()
        with self.cond:
            self._arrivals += 1
            ticket = (rank, self._arrivals)
            waiting = self.waiting.setdefault(group, [])
            waiting.append(ticket)
            try:
                while True:
                    if self.closed:
                        raise WorkerFailed("the service is stopping")
                    if self._oneshot(spec.version):
                        raise WorkerFailed("workers of this engine do not start")
                    live = self._live(group)
                    idle = [w for w in live if w.ready.is_set() and not w.busy and not w.fatal]
                    ahead = sum(1 for other in waiting if other < ticket)
                    allowed = rank < RANKS["long"] or \
                        sum(1 for w in live if w.busy and w.rank == RANKS["long"]) < long_most
                    chosen = None
                    if idle and ahead < len(idle) and allowed:
                        chosen = self._choose(live, idle, key, rank, time.monotonic() - waited_from)
                    if chosen is not None:
                        chosen.busy = True
                        chosen.rank = rank
                        if key:
                            self.affinity[key] = chosen
                        if rank == RANKS["long"] and len(idle) == 1 and len(live) < most:
                            # the last warm worker went to a long call: another starts for the short ones to come
                            threading.Thread(target=self._spawn, args=(spec, group, env, most), name="pool-spare",
                                             daemon=True).start()
                        return chosen
                    if idle and ahead < len(idle) and allowed:
                        self.cond.wait(0.02)             # a long call waiting a moment for the warm worker
                        continue
                    starting = [w for w in live if not w.ready.is_set()]
                    if not spawned and len(live) < most and len(starting) < len(waiting):
                        spawned = True
                        self.cond.release()
                        try:
                            if self._spawn(spec, group, env, most) is not None:
                                call.count("spawned")   # a worker started for this call: a process start the harness counts
                        finally:
                            self.cond.acquire()
                        continue
                    if call.cancelled.is_set():
                        raise WorkerFailed("cancelled while waiting for a worker")
                    if not starved_told and any(w.busy and w.rank == RANKS["ahead"] for w in live):
                        starved_told = True             # an ahead gives its worker to a call that has none (ahead.py)
                        self.cond.release()
                        try:
                            for hook in list(self.on_starved):
                                with contextlib.suppress(Exception):
                                    hook(group)
                        finally:
                            self.cond.acquire()
                        continue
                    self.cond.wait(0.25)
            finally:
                waiting.remove(ticket)
                self.cond.notify_all()

    def _release(self, worker: Worker) -> None:
        with self.cond:
            worker.busy = False
            worker.rank = None
            self.cond.notify_all()
        for hook in list(self.on_release):
            with contextlib.suppress(Exception):
                hook(worker.group)
        if worker.calls >= RECYCLE_CALLS or worker.rss_mb >= RECYCLE_RSS_MB:
            self.service.log(f"worker {worker.spec.version} recycled after {worker.calls} calls, {worker.rss_mb:.0f} MB")
            threading.Thread(target=self._replace, args=(worker,), daemon=True).start()

    def _replace(self, old: Worker) -> None:
        with contextlib.suppress(Exception):
            spare = self._spawn(old.spec, old.group, old.base_env)
            spare.wait_ready(READY_WAIT_S)
        old.retire()

    def worker_died(self, worker: Worker) -> None:
        with self.cond:
            for key, owner in list(self.affinity.items()):
                if owner is worker:
                    self.affinity.pop(key, None)
            versions = self.workers.get(worker.group, [])
            if worker in versions:
                versions.remove(worker)
            empty = not any(self.workers.values())
            chats_left = any(ws for group, ws in self.workers.items() if group.startswith(CHAT_GROUP))
            self.cond.notify_all()
        if worker.group.startswith(CHAT_GROUP) and not chats_left:
            from .. import chatbuild
            chatbuild.drop_licence_copy()           # the copy a chat's workers read goes with the last of them
        if empty:
            self._release_engine_run()

    def tick(self) -> None:
        """Shrink to one worker per version after IDLE_SHRINK_S; stats from busy workers; versions no longer used go."""
        now = time.monotonic()
        current = install.selected_engine()
        with self.cond:
            for group, workers in list(self.workers.items()):
                live = [w for w in workers if not w.dead.is_set() and not w.retiring]
                # The worker kept warm is the one at the machine's full thread counts, else the most recently used.
                idle = sorted([w for w in live if not w.busy and w.ready.is_set()],
                              key=lambda w: (getattr(w, "build_threads", None) == full_threads(), w.last_used))
                keep = 1 if group == current else 0
                for worker in idle[:max(0, len(live) - keep)]:
                    if now - worker.last_used > IDLE_SHRINK_S:
                        worker.retire(0)
                for worker in live:
                    if worker.busy and now - worker.last_used > 60:
                        worker.ask_stats()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            workers = [w for ws in self.workers.values() for w in ws]
            self.cond.notify_all()
        for worker in workers:
            worker.retire(2000, final=True)     # the service ends: no office stays behind it
        end = time.monotonic() + 3.0
        for worker in workers:
            with contextlib.suppress(Exception):
                worker.process.wait(timeout=max(0.05, end - time.monotonic()))
            worker.kill()
        if any(worker.group.startswith(CHAT_GROUP) for worker in workers):
            # The licence copy a chat's workers read goes with them, here: the service may exit before their reader
            # threads report them gone (worker_died).
            from .. import chatbuild
            chatbuild.drop_licence_copy()
        self._release_engine_run()

    # --- running a call ------------------------------------------------------------------------------------------------

    def run(self, call, version: str, argv: list[str], env: dict, stdin: dict | None, keys: list[str] | None = None,
            order: int | None = None, acquired=None) -> tuple[int, list]:
        """Run one engine command for `call`. Returns (exit code, pending). `keys`: the workspace keys a batch of
        Studio parts holds (parts.py), instead of the one its argv names; `acquired`: called once a worker took it,
        or once it will not get one."""
        spec = Spec(version)
        priority = classify(argv)
        order = rank(argv, call.cwd) if order is None else order
        key = workspace_key(argv, call.cwd) if keys is None else None
        if not spec.resident() or self._oneshot(version):
            return run_oneshot(self.service, call, spec, argv, env, stdin), []
        held = sorted(set(keys)) if keys is not None else [key if priority != "read" else None]
        entered = time.perf_counter()
        with contextlib.ExitStack() as stack:
            for one in held:
                stack.enter_context(self.keys.held(one))
            for attempt in (1, 2):
                try:
                    worker = self._acquire(spec, key, priority, call, rank=order)
                except WorkerFailed as exc:
                    if acquired is not None:
                        acquired()
                        acquired = None
                    if call.cancelled.is_set():
                        return 130, []
                    self.service.log(f"no worker for engine {version} ({exc}); one-shot")
                    return run_oneshot(self.service, call, spec, argv, env, stdin), []
                if acquired is not None:
                    acquired()
                    acquired = None
                if call.cancelled.is_set():         # cancelled while a worker was being found: nothing starts
                    self._release(worker)
                    return 130, []
                started = time.monotonic()
                with contextlib.suppress(Exception):     # the call's time, for the service's timings (tools.py)
                    call.timing = {"queue_ms": int((time.perf_counter() - entered) * 1000), "worker": worker.process.pid,
                                   "worker_calls": worker.calls}
                try:
                    result = worker.run(call, argv, env, stdin, key, priority)
                except WorkerFailed as exc:
                    self._release(worker)
                    committed = bool(getattr(worker, "_committed", False))
                    lost = getattr(worker, "lost", None)
                    if lost is None and attempt == 1 and _may_rerun(priority, getattr(worker, "_inputs", None), committed):
                        self.service.log(f"worker {version} lost {argv[:2]} ({exc}); nothing of it was written: run again "
                                         f"on a fresh worker")
                        continue
                    how = lost or f"ended (exit {getattr(worker, 'exit_code', None)})"
                    self.service.log(f"worker {version} lost {argv[:2]} ({exc}); answered 78"
                                     f"{', part of it may be written' if committed else ''}")
                    call.out(lost_text(how, committed))
                    return 78, []
                self._release(worker)
                with contextlib.suppress(Exception):
                    call.timing.update(run_ms=int((time.monotonic() - started) * 1000), engine_ms=result.get("elapsed_ms"))
                if time.monotonic() - started > STATS_AFTER_S:
                    worker.ask_stats()
                if result.get("key") and key and result["key"] != key:
                    with self.cond:
                        self.affinity[result["key"]] = worker
                for name, value in (result.get("counters") or {}).items():
                    if isinstance(value, int):
                        call.count(name, value)
                return int(result.get("exit", 78)), list(result.get("pending") or [])
        return 75, []


    def run_confined(self, call, version: str, argv: list[str], env: dict, stdin: dict | None, folder: str,
                     base_env: dict) -> tuple[int, list] | None:
        """A chat's engine command on a warm worker of its own folder, started in that chat's confinement (`base_env`:
        Node's permission model on the folder, a private home holding the licence copy). None when the engine has no
        worker: the chat then runs a process of its own, as before."""
        spec = Spec(version)
        if not spec.resident() or self._oneshot(version):
            return None
        group = f"{CHAT_GROUP}{version}:{folder}"
        try:
            worker = self._acquire(spec, None, classify(argv), call, group=group, env=base_env, most=1)
        except WorkerFailed:
            return None
        try:
            result = worker.run(call, argv, env, stdin, None, classify(argv))
        except WorkerFailed:
            self._release(worker)
            call.out(lost_text(getattr(worker, "lost", None) or f"ended (exit {getattr(worker, 'exit_code', None)})",
                               bool(getattr(worker, "_committed", False))))
            return 78, []
        self._release(worker)
        return int(result.get("exit", 78)), list(result.get("pending") or [])

    def take_for_ahead(self, version: str, key: str | None) -> _Taken | None:
        """An idle worker for a build-ahead (ahead.py), or None: only when no call waits for a worker of the engine,
        another worker stays idle after it, and no command holds or waits for the part's workspace. Never the
        always-warm worker while another is idle (it serves leads' applies and renders)."""
        token = self.keys.try_take(key)
        if token is None:
            return None
        with self.cond:
            live = self._live(version)
            idle = [w for w in live if w.ready.is_set() and not w.busy and not w.fatal]
            if not self.closed and not self.waiting.get(version) and len(idle) >= 2:
                others = [w for w in idle if getattr(w, "build_threads", None) != full_threads()] or idle
                chosen = min(others, key=lambda w: (len(w.keys), w.calls))
                chosen.busy = True
                chosen.rank = RANKS["ahead"]
                return _Taken(chosen, key, token)
        self.keys.give(key, token)
        return None

    def run_ahead(self, call, taken: _Taken, argv: list[str], env: dict) -> int:
        """Run a build-ahead on the worker take_for_ahead gave; never re-run, never one-shot. Returns its exit."""
        try:
            try:
                result = taken.worker.run(call, argv, env, None, taken.key, "write")
            except WorkerFailed:
                self._release(taken.worker)
                return 78
            self._release(taken.worker)
            return int(result.get("exit", 78))
        finally:
            self.keys.give(taken.key, taken.token)

    def close_confined(self) -> None:
        """Every chat's workers end now (the licence was refused or withdrawn)."""
        with self.cond:
            workers = [w for group, ws in self.workers.items() if group.startswith(CHAT_GROUP) for w in ws]
        for worker in workers:
            worker.kill()


CHAT_GROUP = "chat:"


def _may_rerun(priority: str, inputs, committed: bool) -> bool:
    """C2: a read is re-run once; a write only with no `committed` and every `inputs` stat tuple unchanged."""
    if priority == "read":
        return True
    if committed or not inputs:
        return False
    for entry in inputs:
        try:
            path, size, mtime_ns, ctime_ns, file_id = entry
            info = os.stat(path)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns, str(info.st_ino)) != (size, mtime_ns, ctime_ns, str(file_id)):
                return False
        except FileNotFoundError:
            if entry[1] != -1:
                return False
        except (OSError, ValueError, TypeError):
            return False
    return True


# --- one-shot ---------------------------------------------------------------------------------------------------------------

def run_oneshot(service, call, spec: Spec, argv: list[str], env_overlay: dict, stdin: dict | None) -> int:
    """Today's engine process for one command, its output streamed into the call. Nothing of the engine is removed
    while it runs (withdraw.engine_run, as install.run_engine)."""
    from .. import withdraw
    call.count("spawned")
    with withdraw.engine_run():
        return _run_oneshot(service, call, spec, argv, env_overlay, stdin)


def _run_oneshot(service, call, spec: Spec, argv: list[str], env_overlay: dict, stdin: dict | None) -> int:
    del service
    env = install.engine_env(spec.directory)
    for name, value in env_overlay.items():
        if value in (None, ""):
            env.pop(name, None)
        else:
            env[name] = value
    command = [spec.oneshot_python(), str(spec.oneshot_script), *argv]
    data = None
    if stdin is not None:
        data = base64.b64decode(stdin["data"]) if stdin.get("encoding") == "base64" else str(stdin.get("data") or "").encode("utf-8")
    try:
        proc = subprocess.Popen(command, cwd=call.cwd if call.cwd and os.path.isdir(call.cwd) else None, env=env,
                                stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, bufsize=0, **_child_flags())
    except OSError as exc:
        blocked = isinstance(exc, PermissionError)
        call.out(("THEODORE BLOCKED_BY_POLICY: " if blocked else "THEODORE INTERNAL: ")
                 + f"the engine could not be started: {exc}\n"
                 + ("NEXT: do not retry. Tell the user; a security policy on this computer may block programs in the user folder.\n"
                    "TELL THE USER: This computer's security policy blocked Theodore's engine.\n" if blocked else
                    "NEXT: run theodore doctor.\nTELL THE USER: Theodore's engine could not start. I am checking why.\n"))
        return 73 if blocked else 78
    def pump(stream, name: str) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    call.out(text, name)
            tail = decoder.decode(b"", final=True)
            if tail:
                call.out(tail, name)
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True)]
    for reader in readers:
        reader.start()
    if data is not None:
        with contextlib.suppress(OSError):
            view = memoryview(data)
            while view:
                view = view[proc.stdin.write(view) or 0:]
        with contextlib.suppress(OSError):
            proc.stdin.close()
    call.set_phase(f"pptx {argv[0] if argv else ''} running")
    code = proc.wait()
    for reader in readers:
        reader.join(timeout=10)
    return code if code >= 0 else 78
