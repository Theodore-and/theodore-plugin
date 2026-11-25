"""Build-ahead for a Studio deck's parts (the coordinator, 25 Nov; lanes S2 and G).

When `prepare` announced that a deck's parts come at once (Studio), the service watches each part's deck.js while a
relay is attached. A part whose deck.js changed and then held still for one poll (or that a write verb through the
service just changed) gets `pptx apply <part> --ahead` on an idle worker at the lowest priority. The engine builds the
part into its content store and writes nothing visible (lane G: no record, no built.json, at the end the part's
ahead.json), so the designer's own apply a moment later finds the build done and reuses it on an exact key match (the
engine's check, never the service's).

The conditions (the coordinator):
- only for an engine whose workers list "apply-ahead" in `ready.features` (C2); otherwise nothing is watched or sent;
- an ahead takes a worker only when another stays idle and no call waits for one (Pool.run_ahead);
- one ahead per part; a newer change of the part cancels its ahead in flight (C2 `cancel`) and waits for the next still
  poll;
- a foreground call that finds no idle worker cancels the youngest ahead running (Pool's `on_starved`);
- the designer's apply of a part drops that part's ahead not started yet; one running on the same deck.js is waited
  for (it is that part's own work: the apply then reuses it); one running on another deck.js is cancelled;
- no Studio deck announced, or no relay attached: no thread, no polling.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path

from . import workers

FEATURE = "apply-ahead"
FLAG = "--ahead"
POLL_S = 0.1                     # lane S2: a save seen 100 to 200 ms after it, for an apply 1.5 s later
WATCH_FOR_S = 20 * 60.0          # a burst is watched this long after its prepare or its last change
WAIT_OWN_S = 15.0                # the designer's apply waits at most this for its part's own ahead


def _stat(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


class _Call:
    """What a worker needs of a call, for an ahead: nothing it prints is shown to anyone; its answer is read for the
    log (lane G: {ok, ahead: built | kept | skipped, reason, part, slides, ms})."""

    def __init__(self, part: "_Part", stat: tuple):
        self.part = part
        self.stat = stat
        self.id = f"ahead-{os.path.basename(part.path)}-{int(time.monotonic() * 1000)}"
        self.cwd = part.cwd
        self.images = False
        self.deadline_ms = None
        self.cancelled = threading.Event()
        self.worker = None
        self.worker_images = False
        self.counters: dict = {}
        self.stdout: list[str] = []
        self.phase = "ahead"
        self.queued = time.monotonic()
        self.started: float | None = None
        self.done = threading.Event()

    def out(self, text: str, stream: str = "stdout") -> None:
        if stream == "stdout" and sum(len(t) for t in self.stdout) < 65536:
            self.stdout.append(text)

    def image(self, *_args, **_kwargs) -> bool:
        return False

    def set_phase(self, phase: str, done: int | None = None, total: int | None = None) -> None:
        self.phase = phase

    def count(self, name: str, value: int = 1) -> None:
        self.counters[name] = int(self.counters.get(name, 0)) + int(value)

    def answer(self) -> dict:
        from .tools import _last_json
        return _last_json("".join(self.stdout))


class _Part:
    def __init__(self, path: str, version: str, env: dict, cwd: str | None, burst: "_Burst"):
        self.path = path
        self.deck = Path(path) / "deck.js"
        self.version, self.env, self.cwd, self.burst = version, env, cwd, burst
        self.seen = _stat(self.deck)          # the stat at the last poll
        self.built = self.seen                # the stat an ahead or the designer's apply last took (nothing to do)
        self.still = 0                        # polls the stat has held since it changed
        self.ahead: _Call | None = None       # queued or running
        self.key: str | None = None           # its workspace key, once read ("" when it has none)


class _Burst:
    def __init__(self, lead: str):
        self.lead = lead
        self.until = time.monotonic() + WATCH_FOR_S


class Ahead:
    def __init__(self, service, pool):
        self.service, self.pool = service, pool
        self.lock = threading.Lock()
        self.parts: dict[str, _Part] = {}            # real path of a part -> its watch
        self.bursts: dict[str, _Burst] = {}          # real path of a lead -> its burst
        self.thread: threading.Thread | None = None
        self.count_lock = threading.Lock()
        self.counts = {"ahead_queued": 0, "ahead_started": 0, "ahead_built": 0, "ahead_kept": 0, "ahead_skipped": 0,
                       "ahead_cancelled": 0, "ahead_dropped": 0, "ahead_failed": 0, "ahead_waited": 0, "ahead_reused": 0}
        pool.on_starved.append(self.starved)

    # --- what the service tells it ------------------------------------------------------------------------------------

    def announced(self, version: str, lead: str, rows, env: dict, cwd: str | None) -> None:
        """`prepare` said its parts come at once: watch each part's deck.js (only for an engine that builds ahead)."""
        if not self.pool.has_feature(version, FEATURE) or not isinstance(rows, list):
            return
        real_lead = os.path.realpath(lead)
        paths = []
        for row in rows:
            where = row.get("workspace") if isinstance(row, dict) else None
            if isinstance(where, str) and where:
                paths.append(os.path.realpath(where if os.path.isabs(where) else os.path.join(cwd or ".", where)))
        if not paths:
            return
        with self.lock:
            for path, part in list(self.parts.items()):          # the deck's next prepare replaces its burst
                if part.burst.lead == real_lead:
                    self._drop(part, "the deck was prepared again")
                    del self.parts[path]
            burst = self.bursts[real_lead] = _Burst(real_lead)
            for path in paths:
                self.parts[path] = _Part(path, version, dict(env), cwd, burst)
        self.service.log(f"ahead: watching {len(paths)} part(s) of {os.path.basename(real_lead)}")
        self._ensure_thread()

    def wrote(self, argv: list[str], cwd: str | None) -> None:
        """A write verb through the service answered 0 on a part: its deck.js is still now, no poll to wait for."""
        part = self._part_of(argv, cwd)
        if part is None:
            return
        with self.lock:
            part.seen = _stat(part.deck)
            part.still = 1
            part.burst.until = time.monotonic() + WATCH_FOR_S
        self._schedule(part)

    def before_apply(self, argv: list[str], cwd: str | None) -> None:
        """The designer's own apply of a part: an ahead not started yet is dropped; one running on the deck.js the
        apply will read is waited for (the apply then reuses it); one running on another is cancelled."""
        part = self._part_of(argv, cwd)
        if part is None:
            return
        now = _stat(part.deck)
        with self.lock:
            call = part.ahead
            part.built = now
            part.burst.until = time.monotonic() + WATCH_FOR_S
            if call is None:
                return
            if call.started is None:
                part.ahead = None
                self._count("ahead_dropped")
                self._log(call, "dropped: the designer's apply came first")
                return
            same = call.stat == now
        if not same:
            self._cancel(call, "the designer's apply reads a newer deck.js")
            return
        self._count("ahead_waited")
        if not call.done.wait(WAIT_OWN_S):
            self._cancel(call, f"the designer's apply waited {int(WAIT_OWN_S)} s for it")

    def applied(self, argv: list[str], cwd: str | None, answer: dict) -> None:
        """The designer's apply of a part answered: when it reused its ahead (lane G: `part.built_ahead`), counted."""
        part = self._part_of(argv, cwd)
        if part is None or not isinstance(answer, dict):
            return
        row = answer.get("part") if isinstance(answer.get("part"), dict) else answer
        if row.get("built_ahead") is True:
            self._count("ahead_reused")
            with contextlib.suppress(Exception):
                self.service.log(f"ahead: {os.path.basename(part.path)} reused by its apply; "
                                 + json.dumps({k: v for k, v in self.counts.items() if v}, separators=(",", ":")))

    def starved(self, group: str) -> None:
        """A foreground call found no idle worker: the youngest ahead running on that engine is cancelled."""
        with self.lock:
            running = [p.ahead for p in self.parts.values()
                       if p.ahead is not None and p.ahead.started is not None and not p.ahead.done.is_set()
                       and p.version == group]
            youngest = max(running, key=lambda c: c.started or 0, default=None)
        if youngest is not None:
            self._cancel(youngest, "a call waits for a worker")

    # --- the watch ----------------------------------------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._loop, name="service-ahead", daemon=True)
            self.thread.start()

    def _loop(self) -> None:
        while not self.service.stopping.is_set():
            now = time.monotonic()
            with self.lock:
                for path, part in list(self.parts.items()):
                    if now > part.burst.until:
                        self._drop(part, "its burst ended")
                        del self.parts[path]
                for lead, burst in list(self.bursts.items()):
                    if now > burst.until:
                        del self.bursts[lead]
                parts = list(self.parts.values())
            if not parts:
                with self.lock:
                    if not self.parts:
                        self.thread = None
                        return
                time.sleep(POLL_S)
                continue
            if self.service.relays_attached():
                for part in parts:
                    self._poll(part)
                for part in parts:
                    self._start_queued(part)
            time.sleep(POLL_S)

    def _poll(self, part: _Part) -> None:
        stat = _stat(part.deck)
        with self.lock:
            if stat != part.seen:
                part.seen, part.still = stat, 0
                call = part.ahead
                part.burst.until = time.monotonic() + WATCH_FOR_S
            else:
                part.still += 1
                call = None
        if call is not None:                        # changed again while its ahead was queued or running
            if call.started is None:
                with self.lock:
                    if part.ahead is call:
                        part.ahead = None
                self._count("ahead_dropped")
                self._log(call, "dropped: deck.js changed again")
            else:
                self._cancel(call, "deck.js changed again")
            return
        if part.still >= 1 and stat is not None and stat != part.built:
            self._schedule(part)

    def _schedule(self, part: _Part) -> None:
        with self.lock:
            stat = part.seen
            if stat is None or stat == part.built or (part.ahead is not None and part.ahead.stat == stat):
                return
            if part.ahead is not None:              # an older one: the poll cancels or drops it first
                return
            part.ahead = _Call(part, stat)
            part.built = stat
            self._count("ahead_queued")
        self._start_queued(part)

    def _start_queued(self, part: _Part) -> None:
        if part.ahead is None or part.ahead.started is not None:
            return
        argv = ["apply", part.path, FLAG]
        if part.key is None:
            part.key = workers.workspace_key(argv, part.cwd) or ""
        with self.lock:                             # the pool never calls in here while it holds its own locks
            call = part.ahead
            if call is None or call.started is not None:
                return
            taken = self.pool.take_for_ahead(part.version, part.key or None)
            if taken is None:
                return                              # not now: tried again at the next poll
            call.started = time.monotonic()
        self._count("ahead_started")
        self._log(call, f"started (queued {int((call.started - call.queued) * 1000)} ms)")
        threading.Thread(target=self._run, args=(part, call, taken, argv), name="service-ahead-run", daemon=True).start()

    def _run(self, part: _Part, call: _Call, taken, argv: list[str]) -> None:
        code = 78
        try:
            code = self.pool.run_ahead(call, taken, argv, part.env)
        finally:
            elapsed = int((time.monotonic() - (call.started or call.queued)) * 1000)
            answer = call.answer() if code == 0 else {}
            outcome = answer.get("ahead") if isinstance(answer.get("ahead"), str) else None
            if call.cancelled.is_set():
                self._count("ahead_cancelled")
                self._log(call, f"cancelled after {elapsed} ms (exit {code})")
            elif code == 0 and outcome in ("built", "kept", "skipped"):
                self._count(f"ahead_{outcome}")
                reason = f" ({answer.get('reason')})" if answer.get("reason") else ""
                self._log(call, f"done: {outcome}{reason} in {elapsed} ms")
            else:                                   # not tried again on the same deck.js: its designer's apply builds it
                self._count("ahead_failed")
                self._log(call, f"failed: exit {code} in {elapsed} ms")
            with self.lock:
                if part.ahead is call:
                    part.ahead = None
            call.done.set()

    # --- helpers --------------------------------------------------------------------------------------------------------

    def _part_of(self, argv: list[str], cwd: str | None) -> _Part | None:
        if not self.parts:
            return None
        for arg in argv[1:] if argv and argv[0] != "slide" else argv[2:]:
            if arg and not arg.startswith("-"):
                path = os.path.realpath(arg if os.path.isabs(arg) else os.path.join(cwd or ".", arg))
                with self.lock:
                    return self.parts.get(path)
        return None

    def _cancel(self, call: _Call, why: str) -> None:
        if call.done.is_set() or call.cancelled.is_set():
            return
        call.cancelled.set()
        worker = call.worker
        if worker is not None:
            with contextlib.suppress(Exception):
                worker.cancel(call)
        self._log(call, f"cancelling: {why}")

    def _drop(self, part: _Part, why: str) -> None:
        call = part.ahead
        if call is None:
            return
        if call.started is None:
            part.ahead = None
            self._count("ahead_dropped")
            self._log(call, f"dropped: {why}")
        else:
            threading.Thread(target=self._cancel, args=(call, why), daemon=True).start()

    def _count(self, name: str) -> None:
        with self.count_lock:
            self.counts[name] = self.counts.get(name, 0) + 1

    def _log(self, call: _Call, what: str) -> None:
        with contextlib.suppress(Exception):
            size, mtime = call.stat if call.stat else (None, None)
            self.service.log(f"ahead: {os.path.basename(call.part.path)} (deck.js {size} bytes, {mtime}) {what}; "
                             + json.dumps({k: v for k, v in self.counts.items() if v}, separators=(",", ":")))
