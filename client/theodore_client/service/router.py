"""Calls: their frames, their replay and their routing to the tools (contract C1, "Frames").

A call runs on its own thread. Everything it produces is a frame sent to the connection that asked for it (its
subscriber) and kept for a `resend`: a client that lost its connection (a service hand-over, a crash of the relay's
pipe) sends the call again with `resend: true` and gets the frames replayed (output up to 1 MiB, pictures by path), or
is attached to the call still running. Finished calls are remembered for 10 minutes by (session id, call id).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from . import frames as framing

REMEMBER_S = 600.0
REPLAY_BYTES = 1 << 20
OUT_CHUNK = 1 << 20               # characters per `out` frame (C1: data ≤ 1 MiB)
KEEP_STDOUT = 4 << 20             # stdout kept to find the pictures an answer names
PROGRESS_AFTER_S = 1.0
PROGRESS_EVERY_S = 2.0
EXIT_RETRY = 75
EXIT_INTERNAL = 78
EXIT_STILL_RUNNING = 124
EXIT_CANCELLED = 130
TOOLS = ("pptx", "theodore", "guide", "specimen", "list", "chat")
COUNTERS = ("spawned", "deck_hashes", "deckjs_full_parses", "packages_written", "files_written", "user_files_written",
            "http_requests", "tls_handshakes")


TIMING_LINES = ("timings: ",)       # the engine's own, under THEODORE_TIMINGS=1 (lanes F and G: one line a verb)


SLOW_LOG_S = 30.0                  # a call running longer: its thread's stack in the log, once a minute
SLOW_LOG_EVERY_S = 60.0
# However a call is stuck, its caller gets an answer: past the workers' own ceiling (CALL_CEILING_S) by a margin.
_CEILING = float(os.environ.get("THEODORE_CALL_CEILING_S") or 900.0)
ROUTER_CEILING_S = _CEILING + min(30.0, _CEILING)


class Call:
    """One `call` frame being answered."""

    def __init__(self, router: "Router", client, frame: dict):
        self.router = router
        self.id = frame["id"]
        self.session = client.session_id
        self.host = client.host
        self.client_role = client.role
        self.tool = frame["tool"]
        self.args = list(frame.get("args") or [])
        self.params = dict(frame.get("params") or {})
        self.cwd = frame.get("cwd")
        self.env = dict(frame.get("env") or {})
        self.stdin = frame.get("stdin")
        self.images = frame.get("images", True) is not False
        self.deadline_ms = frame.get("deadline_ms")
        self.started = time.monotonic()
        self.started_perf = time.perf_counter()   # elapsed_ms: Windows' monotonic clock ticks only every 15.6 ms
        self.ended: float | None = None
        self.exit: int | None = None
        self.pending: list = []
        self.phase = "starting"
        self.progress_done: int | None = None
        self.progress_total: int | None = None
        self.last_progress = 0.0
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.subscriber = client
        self.alias: str | None = None      # the id the subscriber knows this call by, when not its own
        self.replay: list[dict] = []
        self.replay_bytes = 0
        self.image_count = 0
        self.lock = threading.Lock()
        self.worker = None                 # set by the pool while a worker runs it (for cancel)
        self.fingerprint = self._fingerprint()
        self.detached_by_deadline = False
        self.worker_images = False         # a worker sent its own image frames
        self.counters: dict = {}           # the proof harness's counts for this call (C1 done.counters, request K-1)
        self.stdout_kept: list[str] = []   # stdout, kept (up to KEEP_STDOUT) for the pictures it names
        self.stdout_kept_size = 0
        self.engine_timings: list[str] = []   # the engine's `theodore-timings` lines (THEODORE_TIMINGS=1), for the log

    def _fingerprint(self) -> str:
        """What makes two shell commands "the same command on the same inputs" (C1 "Deadline")."""
        stdin = self.stdin or {}
        return hashlib.sha256(json.dumps([self.session, self.tool, self.args, self.params, self.cwd,
                                          hashlib.sha256(str(stdin.get("data") or "").encode("utf-8")).hexdigest()],
                                         sort_keys=True).encode("utf-8")).hexdigest()

    # --- output ------------------------------------------------------------------------------------------------------

    def _emit(self, frame: dict, keep: bool = True) -> None:
        if self.finished.is_set() and frame.get("t") != "done":
            return                          # answered already (the ceiling): what a stuck thread produces after is dropped
        with self.lock:
            if keep:
                size = len(frame.get("data") or "") if frame["t"] == "out" else 0
                if frame["t"] != "out" or self.replay_bytes + size <= REPLAY_BYTES:
                    stored = frame if frame["t"] != "image" else {k: v for k, v in frame.items() if k != "data"}
                    self.replay.append(stored)
                    self.replay_bytes += size
            subscriber, alias = self.subscriber, self.alias
        if subscriber is not None:
            subscriber.send(frame if alias is None else {**frame, "id": alias})

    def out(self, text: str, stream: str = "stdout") -> None:
        if not text:
            return
        if stream == "stderr" and ("timings" in text) and len(self.engine_timings) < 64:
            self.engine_timings.extend(line for line in text.splitlines() if line.startswith(TIMING_LINES))
        if stream == "stdout" and self.stdout_kept_size < KEEP_STDOUT:
            self.stdout_kept.append(text)
            self.stdout_kept_size += len(text)
        for start in range(0, len(text), OUT_CHUNK):
            self._emit({"t": "out", "id": self.id, "stream": stream, "data": text[start:start + OUT_CHUNK]})

    def image(self, mime: str, path: str, data: str | None = None, slide: str | None = None, page: int | None = None,
              key: str | None = None) -> bool:
        """One picture of the answer (at most 12); False when the answer already has 12."""
        with self.lock:
            if self.image_count >= 12:
                return False
            index = self.image_count
            self.image_count += 1
        frame = {"t": "image", "id": self.id, "index": index, "mime": mime, "path": path}
        if slide:
            frame["slide"] = slide
        if page:
            frame["page"] = page
        if key:
            frame["key"] = key
        if data is not None and self.images:
            frame["data"] = data
        self._emit(frame)
        return True

    def set_phase(self, phase: str, done: int | None = None, total: int | None = None) -> None:
        self.phase = str(phase)[:200]
        self.progress_done, self.progress_total = done, total

    def progress_due(self, now: float) -> bool:
        return self.exit is None and now - self.started >= PROGRESS_AFTER_S and now - self.last_progress >= PROGRESS_EVERY_S

    def send_progress(self, now: float) -> None:
        self.last_progress = now
        frame = {"t": "progress", "id": self.id, "elapsed_ms": int((time.perf_counter() - self.started_perf) * 1000),
                 "phase": self.phase}
        if self.progress_done is not None:
            frame["done"] = max(0, int(self.progress_done))
        if self.progress_total is not None:
            frame["total"] = max(0, int(self.progress_total))
        self._emit(frame, keep=False)

    def finish(self, exit_code: int, pending: list | None = None) -> None:
        if self.finished.is_set():
            return
        self.exit = max(0, min(255, int(exit_code)))
        self.ended = time.monotonic()
        self.pending = list(pending or [])
        frame = {"t": "done", "id": self.id, "exit": self.exit,
                 "elapsed_ms": int((time.perf_counter() - self.started_perf) * 1000)}
        if self.pending:
            frame["pending"] = self.pending
        counters = {k: int(v) for k, v in self.counters.items() if k in COUNTERS and isinstance(v, int) and v >= 0}
        if counters:
            frame["counters"] = counters
        self._emit(frame)
        self.finished.set()
        self.router.call_ended(self)

    def count(self, name: str, value: int = 1) -> None:
        """Add to one of the harness's counters (the worker's own are added as its done gives them)."""
        with self.lock:
            self.counters[name] = int(self.counters.get(name, 0)) + int(value)

    # --- subscribers ---------------------------------------------------------------------------------------------------

    def attach(self, client, replayed: bool, as_id: str | None = None) -> None:
        """A client resending this call (or, `as_id`, a shell command sent again under its own new id): what was
        produced so far, then the rest as it comes."""
        with self.lock:
            history = list(self.replay)
            self.subscriber = client
            self.alias = as_id if as_id != self.id else None
        for frame in history:
            if frame["t"] == "done":
                frame = {**frame, "replayed": True} if replayed else frame
            client.send(frame if as_id is None else {**frame, "id": as_id})

    def drop_subscriber(self, client) -> None:
        with self.lock:
            if self.subscriber is client:
                self.subscriber = None


class Router:
    """Calls by (session, id); dispatch to the tools; cancel; the deadline of the shell route; progress."""

    def __init__(self, service):
        self.service = service
        self.calls: dict[tuple[str, str], Call] = {}
        self.lock = threading.Lock()
        self.running = 0
        self.last_request = time.monotonic()

    def submit(self, client, frame: dict) -> None:
        call_id = frame.get("id")
        if frame.get("tool") not in TOOLS:
            client.send({"t": "error", "id": call_id, "code": "UNKNOWN_TOOL", "message": f"unknown tool {frame.get('tool')!r}"})
            return
        if not isinstance(frame.get("cwd"), str) or not frame.get("cwd"):
            client.send({"t": "error", "id": call_id, "code": "BAD_CALL", "message": "a call needs cwd, an absolute folder"})
            return
        key = (client.session_id, call_id)
        with self.lock:
            known = self.calls.get(key)
            if known is not None:
                if not frame.get("resend"):
                    client.send({"t": "error", "id": call_id, "code": "BAD_CALL", "message": "a call with this id is already known"})
                    return
            elif self.service.draining:
                client.send({"t": "error", "id": call_id, "code": "SHUTTING_DOWN",
                             "message": "this service is handing over to its update; send the call again after reconnecting",
                             "retry_after_ms": 100})
                return
        if known is not None:                       # a resend: replay, or attach to the running call
            known.attach(client, replayed=known.finished.is_set())
            return
        call = Call(self, client, frame)
        same = self._same_running(call) if client.role == "front" else None
        if same is not None:                        # the same shell command again after its deadline: wait for it
            same.detached_by_deadline = False
            same.attach(client, replayed=False, as_id=call_id)
            return
        with self.lock:
            self.calls[key] = call
            self.running += 1
            self.last_request = time.monotonic()
            if self.service.relays_attached() == 0:
                self.service.note_unattached_request()
        if call.deadline_ms:
            timer = threading.Timer(call.deadline_ms / 1000.0, self._deadline, args=(call,))
            timer.daemon = True
            timer.start()
        threading.Thread(target=self._run, args=(call,), name=f"call-{call.tool}-{call_id}", daemon=True).start()

    def _same_running(self, call: Call) -> Call | None:
        with self.lock:
            for other in self.calls.values():
                if other.detached_by_deadline and not other.finished.is_set() and other.fingerprint == call.fingerprint:
                    return other
        return None

    def _deadline(self, call: Call) -> None:
        """The shell route's limit: the caller gets 124 and STILL_RUNNING; the work carries on."""
        if call.finished.is_set():
            return
        call.detached_by_deadline = True
        with call.lock:
            subscriber, call.subscriber = call.subscriber, None
        if subscriber is not None:
            subscriber.send({"t": "out", "id": call.id, "stream": "stdout",
                             "data": json.dumps({"code": "STILL_RUNNING", "call": call.id}) + "\n"})
            subscriber.send({"t": "done", "id": call.id, "exit": EXIT_STILL_RUNNING,
                             "elapsed_ms": int((time.perf_counter() - call.started_perf) * 1000)})

    def _run(self, call: Call) -> None:
        from . import tools
        call.thread = threading.get_ident()
        try:
            code, pending = tools.run(self.service, call)
        except Exception as exc:  # noqa: BLE001  a call answers; it never ends the service
            self.service.log(f"call {call.tool} {call.id}: {exc.__class__.__name__}: {exc}\n{traceback.format_exc()}")
            with contextlib.suppress(Exception):
                call.out(f"THEODORE INTERNAL: the Theodore service failed on this call ({exc.__class__.__name__}).\n"
                         "NEXT: run the same command again once. If it fails again, run theodore doctor.\n"
                         "TELL THE USER: Theodore hit an internal problem; I am retrying.\n")
            code, pending = EXIT_INTERNAL, []
        call.finish(code, pending)

    def call_ended(self, call: Call) -> None:
        with self.lock:
            if self.calls.get((call.session, call.id)) is call:
                self.running = max(0, self.running - 1)
                self.last_request = time.monotonic()
        self.service.call_ended(call)

    def cancel(self, client, call_id: str) -> None:
        with self.lock:
            call = self.calls.get((client.session_id, call_id))
        if call is None or call.finished.is_set():
            return
        call.cancelled.set()
        worker = call.worker
        if worker is not None:
            with contextlib.suppress(Exception):
                worker.cancel(call)

    def client_gone(self, client) -> None:
        """A connection ended. A shell command's call keeps running (nothing half-written), and the same command run
        again attaches to it instead of running twice."""
        with self.lock:
            calls = list(self.calls.values())
        for call in calls:
            if call.subscriber is client and client.role == "front" and not call.finished.is_set():
                call.detached_by_deadline = True
            call.drop_subscriber(client)

    def tick(self) -> None:
        """Progress for long calls; a slow call's stack in the log; the ceiling; forget finished calls after
        REMEMBER_S."""
        now = time.monotonic()
        with self.lock:
            calls = list(self.calls.items())
        for key, call in calls:
            if call.finished.is_set():
                if call.ended is not None and now - call.ended > REMEMBER_S:
                    with self.lock:
                        self.calls.pop(key, None)
                continue
            if call.progress_due(now):
                with contextlib.suppress(Exception):
                    call.send_progress(now)
            age = now - call.started
            if age > SLOW_LOG_S and now - getattr(call, "slow_logged", 0.0) > SLOW_LOG_EVERY_S:
                call.slow_logged = now
                self._log_slow(call, age)
            if age > ROUTER_CEILING_S and not getattr(call, "past_ceiling", False):
                call.past_ceiling = True
                self._past_ceiling(call, age)

    def _log_slow(self, call: Call, age: float) -> None:
        """Where a slow call's thread is (one line a minute): what a stall's report reads."""
        with contextlib.suppress(Exception):
            frame = sys._current_frames().get(getattr(call, "thread", None))        # noqa: SLF001
            where = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                for f in reversed(traceback.extract_stack(frame)[-8:])) if frame else "not started"
            self.service.log(f"slow call {call.tool} {' '.join(call.args[:2])} ({call.id}): {int(age)} s, at {where}")

    def _past_ceiling(self, call: Call, age: float) -> None:
        """However a call is stuck (a worker, a lock, a wait), its caller is answered by the ceiling: 78 and what to do.
        Its thread is left to end; what it produces after is not sent."""
        from .workers import lost_text
        self._log_slow(call, age)
        self.service.log(f"call {call.tool} {' '.join(call.args[:2])} ({call.id}) past the ceiling ({int(age)} s): "
                         f"answered 78")
        with contextlib.suppress(Exception):
            call.out(lost_text(f"did not finish this command within {int(ROUTER_CEILING_S // 60)} minutes", False))
            call.finish(EXIT_INTERNAL)

    def running_calls(self) -> list[Call]:
        with self.lock:
            return [c for c in self.calls.values() if not c.finished.is_set()]


def check_call(frame: dict) -> str | None:
    """Why a call frame is malformed (C1 schema, the parts the service relies on), or None."""
    call_id = frame.get("id")
    if not isinstance(call_id, str) or not call_id or len(call_id) > 64 or not all(c.isalnum() or c in "._:-" for c in call_id):
        return "a call id is 1 to 64 of A-Z a-z 0-9 . _ : -"
    if frame.get("tool") in ("pptx", "theodore"):
        if not isinstance(frame.get("args"), list) or not all(isinstance(a, str) for a in frame["args"]):
            return "args is a list of strings"
    elif frame.get("tool") in ("guide", "specimen", "list", "chat") and not isinstance(frame.get("params"), dict):
        return "params is an object"
    stdin = frame.get("stdin")
    if stdin is not None and (not isinstance(stdin, dict) or stdin.get("encoding") not in ("utf-8", "base64")
                              or not isinstance(stdin.get("data"), str)):
        return "stdin is {encoding: utf-8 | base64, data}"
    if frame.get("env") is not None and not isinstance(frame.get("env"), dict):
        return "env is an object"
    return None


__all__ = ["Call", "Router", "check_call", "TOOLS", "framing"]
