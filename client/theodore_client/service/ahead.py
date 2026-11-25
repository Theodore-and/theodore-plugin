"""Build-ahead for a Studio deck's parts (the coordinator, 25 Nov; lanes S2 and G).

When `prepare` announced that a deck's parts come at once (Studio), the service watches each part's deck.js while a
relay is attached. Parts whose deck.js changed and then held still for one poll (or that a write verb through the
service just changed) are built ahead: as one several-part call per idle worker allowed, `pptx apply <p1> <p2> ...
--ahead` (lane G: one build for its parts, apply_many's answer form), so a burst's parts all start building at once
instead of a few at a time (lane S2's rig 7: single-part aheads ran three at a time and 12 of 15 were still queued when
their designers applied). The engine builds each part into its content store and writes nothing visible (no record,
no built.json; at the end the part's ahead.json), so the designer's own apply a moment later finds the build done and
reuses it on an exact key match (the engine's check, never the service's).

The conditions (the coordinator):
- only for an engine whose workers list "apply-ahead" in `ready.features` (C2); otherwise nothing is watched or sent;
- aheads take workers only while another stays idle and no call waits for one (Pool.take_for_ahead), and never a part
  whose workspace another command holds;
- a part is in one ahead at a time; a newer save of a part alone in its ahead cancels it (C2 `cancel`), and a part
  saved again while others are built with it is built again after them;
- a foreground call that finds no idle worker cancels the youngest ahead running (Pool's `on_starved`);
- the designer's apply of a part takes it out of the next ahead; it waits for an ahead running on the same deck.js
  (it is that part's own work: the apply then reuses it), not counted meanwhile as on its way in the gathering; an
  ahead on an older deck.js it does not wait for;
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
SETTLE_S = 0.3                   # a burst's saves still coming: its ready parts wait at most this to go together
WATCH_FOR_S = 20 * 60.0          # a burst is watched this long after its prepare or its last change
WAIT_OWN_S = 15.0                # the designer's apply waits at most this for its part's own ahead
MOST_IN_AHEAD = 32


def _stat(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


class _Call:
    """What a worker needs of a call, for an ahead of one or several parts: nothing it prints is shown to anyone; its
    answer is read for the log (lane G: one part's {ok, ahead: built | kept | skipped, reason, part, slides, ms}, or
    apply_many's {ok, summary, builds, parts: [{part, exit, stdout}]} with each stdout one part's object)."""

    def __init__(self, members: list["_Part"]):
        self.members = members
        self.stats = {part.path: part.seen for part in members}     # the deck.js each part is built from
        first = members[0]
        self.id = f"ahead-{os.path.basename(first.path)}-{len(members)}-{int(time.monotonic() * 1000)}"
        self.cwd = first.cwd
        self.images = False
        self.deadline_ms = None
        self.cancelled = threading.Event()
        self.worker = None
        self.worker_images = False
        self.counters: dict = {}
        self.stdout: list[str] = []
        self.engine_timings: list[str] = []   # the engine's own `timings: ...` lines (THEODORE_TIMINGS=1), for the log
        self.phase = "ahead"
        self.started = time.monotonic()
        self.done = threading.Event()

    def out(self, text: str, stream: str = "stdout") -> None:
        if stream == "stdout":
            if sum(len(t) for t in self.stdout) < 1 << 20:
                self.stdout.append(text)
        elif ("timings: " in text or "gc: " in text) and len(self.engine_timings) < 40:      # router.TIMING_LINES
            self.engine_timings.extend(line for line in text.splitlines() if line.startswith(("timings: ", "gc: ")))

    def image(self, *_args, **_kwargs) -> bool:
        return False

    def set_phase(self, phase: str, done: int | None = None, total: int | None = None) -> None:
        self.phase = phase

    def count(self, name: str, value: int = 1) -> None:
        self.counters[name] = int(self.counters.get(name, 0)) + int(value)

    def outcomes(self, code: int) -> dict[str, str]:
        """Each member's outcome: built, kept, skipped, or failed."""
        from .tools import _last_json
        answer = _last_json("".join(self.stdout)) if code == 0 else {}
        found: dict[str, str] = {}
        rows = answer.get("parts") if isinstance(answer.get("parts"), list) else None
        if rows is None:
            if len(self.members) == 1 and isinstance(answer.get("ahead"), str):
                found[self.members[0].path] = answer["ahead"]
        else:
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("part"), str):
                    continue
                path = os.path.realpath(row["part"] if os.path.isabs(row["part"]) else os.path.join(self.cwd or ".", row["part"]))
                one = {}
                with contextlib.suppress(ValueError, TypeError):
                    one = json.loads(row.get("stdout") or "{}")
                if int(row.get("exit") or 0) == 0 and isinstance(one, dict) and isinstance(one.get("ahead"), str):
                    found[path] = one["ahead"]
        return {part.path: found.get(part.path, "failed") for part in self.members}


class _Part:
    def __init__(self, path: str, version: str, env: dict, cwd: str | None, burst: "_Burst"):
        self.path = path
        self.deck = Path(path) / "deck.js"
        self.version, self.env, self.cwd, self.burst = version, env, cwd, burst
        self.seen = _stat(self.deck)          # the stat at the last poll
        self.built = self.seen                # the stat an ahead or the designer's apply last took (nothing to do)
        self.still = 0                        # polls the stat has held since it changed
        self.ready_since: float | None = None # ready for an ahead since then (still, and not built from this stat)
        self.ahead: _Call | None = None       # the ahead running with it
        self.key: str | None = None           # its workspace key, once read ("" when it has none)


class _Burst:
    def __init__(self, lead: str):
        self.lead = lead
        self.until = time.monotonic() + WATCH_FOR_S
        self.changed = 0.0                    # the last time one of its parts' deck.js changed


class Ahead:
    def __init__(self, service, pool):
        self.service, self.pool = service, pool
        self.lock = threading.Lock()
        self.parts: dict[str, _Part] = {}            # real path of a part -> its watch
        self.bursts: dict[str, _Burst] = {}          # real path of a lead -> its burst
        self.running: list[_Call] = []
        self.thread: threading.Thread | None = None
        self.count_lock = threading.Lock()
        self.counts = {"ahead_calls": 0, "ahead_parts": 0, "ahead_built": 0, "ahead_kept": 0, "ahead_skipped": 0,
                       "ahead_failed": 0, "ahead_cancelled": 0, "ahead_dropped": 0, "ahead_waited": 0,
                       "ahead_reused": 0}
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
        stale = []
        with self.lock:
            for path, part in list(self.parts.items()):          # the deck's next prepare replaces its burst
                if part.burst.lead == real_lead:
                    if part.ahead is not None:
                        stale.append(part.ahead)
                    del self.parts[path]
            burst = self.bursts[real_lead] = _Burst(real_lead)
            for path in paths:
                self.parts[path] = _Part(path, version, dict(env), cwd, burst)
        for call in stale:
            self._cancel(call, "the deck was prepared again")
        self.service.log(f"ahead: watching {len(paths)} part(s) of {os.path.basename(real_lead)}")
        self._ensure_thread()

    def wrote(self, argv: list[str], cwd: str | None) -> None:
        """A write verb through the service answered 0 on a part: its deck.js is still now, no poll to wait for."""
        part = self._part_of(argv, cwd)
        if part is None:
            return
        with self.lock:
            stat = _stat(part.deck)
            if stat != part.seen:
                part.burst.changed = time.monotonic()
            part.seen, part.still = stat, 1
            part.burst.until = time.monotonic() + WATCH_FOR_S
            if stat is not None and stat != part.built and part.ready_since is None:
                part.ready_since = time.monotonic()
        self._dispatch()

    def before_apply(self, argv: list[str], cwd: str | None, on_wait=None) -> None:
        """The designer's own apply of a part: the part leaves the next ahead; an ahead running with it on the
        deck.js the apply will read is waited for (the apply then reuses it), one on an older deck.js is not (and,
        when the part is its only one, is cancelled). `on_wait` is called before it waits: the gathering then stops
        counting this part as on its way, so the parts that are ready go out without it (lane S2's rig 7)."""
        part = self._part_of(argv, cwd)
        if part is None:
            return
        now = _stat(part.deck)
        with self.lock:
            if part.ready_since is not None:
                part.ready_since = None
                self._count("ahead_dropped")
            part.built = now
            part.burst.until = time.monotonic() + WATCH_FOR_S
            call = part.ahead
            if call is None:
                return
            same = call.stats.get(part.path) == now
            alone = len(call.members) == 1
        if not same:
            if alone:
                self._cancel(call, "the designer's apply reads a newer deck.js")
            return
        self._count("ahead_waited")
        if on_wait is not None:
            with contextlib.suppress(Exception):
                on_wait()
        if not call.done.wait(WAIT_OWN_S) and alone:
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
                self.service.log(f"ahead: {os.path.basename(part.path)} reused by its apply; {self._counts_text()}")

    def starved(self, group: str) -> None:
        """A foreground call found no idle worker: the youngest ahead running on that engine is cancelled."""
        with self.lock:
            mine = [c for c in self.running if not c.done.is_set() and not c.cancelled.is_set()
                    and c.members[0].version == group]
            youngest = max(mine, key=lambda c: c.started, default=None)
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
                    if now > part.burst.until and part.ahead is None:
                        del self.parts[path]
                for lead, burst in list(self.bursts.items()):
                    if now > burst.until:
                        del self.bursts[lead]
                parts = list(self.parts.values())
                if not parts:
                    self.thread = None
                    return
            if self.service.relays_attached():
                for part in parts:
                    self._poll(part)
                self._dispatch()
            time.sleep(POLL_S)

    def _poll(self, part: _Part) -> None:
        stat = _stat(part.deck)
        cancel = None
        with self.lock:
            if stat != part.seen:
                part.seen, part.still, part.ready_since = stat, 0, None
                part.burst.changed = part.burst.until = time.monotonic()
                part.burst.until += WATCH_FOR_S
                call = part.ahead
                if call is not None and len(call.members) == 1:
                    cancel = call                   # saved again while built alone: that build is of no use now
            else:
                part.still += 1
                if part.still >= 1 and stat is not None and stat != part.built and part.ahead is None \
                        and part.ready_since is None:
                    part.ready_since = time.monotonic()
        if cancel is not None:
            self._cancel(cancel, "deck.js changed again")

    def _dispatch(self) -> None:
        """The ready parts of each burst, once its saves have settled (no change in the last poll, or the oldest ready
        for SETTLE_S), or at once when every part of the burst has been saved since its last ahead or apply (lane
        S2's S2-7: all fifteen land within milliseconds, and the still poll cost each ahead ~100 ms of its head
        start): one several-part ahead per worker allowed, in shares by the workers' build threads."""
        now = time.monotonic()
        launches: list[tuple[_Call, object, list]] = []
        with self.lock:
            every: dict[str, list[_Part]] = {}
            for part in self.parts.values():
                every.setdefault(part.burst.lead, []).append(part)
            complete = set()                        # bursts whose every part is saved anew and in no ahead
            for lead, members in every.items():
                if len(members) > 1 and all(p.seen is not None and p.seen != p.built and p.ahead is None for p in members):
                    complete.add(lead)
                    for part in members:
                        if part.ready_since is None:
                            part.ready_since = now
            groups: dict[tuple[str, str], list[_Part]] = {}
            for part in self.parts.values():
                if part.ready_since is not None and part.ahead is None and part.seen == _stat(part.deck):
                    groups.setdefault((part.version, part.burst.lead), []).append(part)
            for (version, lead), ready in groups.items():
                burst = ready[0].burst
                oldest = min(p.ready_since for p in ready)
                if lead not in complete and now - burst.changed < POLL_S and now - oldest < SETTLE_S:
                    continue                        # its saves are still coming: they go together
                slots = self.pool.ahead_slots(version)
                if slots <= 0:
                    continue
                for part in ready:                  # a part whose workspace another command holds waits
                    if part.key is None:
                        part.key = workers.workspace_key(["apply", part.path], part.cwd) or ""
                # A part whose workspace another command holds (a write verb on it, its own apply) waits for the next
                # ahead. An ahead does not hold the part's key itself: the designer's apply must not wait on a key for
                # an ahead on an older deck.js (it waits for its own ahead in before_apply only on the same one).
                free = [part for part in ready if not self.pool.keys.busy(part.key or None)]
                if not free:
                    continue
                taken = []
                for _ in range(max(1, min(slots, len(free)))):
                    worker = self.pool.take_for_ahead(version)
                    if worker is None:
                        break
                    taken.append(worker)
                # Shares by each worker's build threads (the engine builds a call's parts on its thread pool): the
                # always-warm worker's 3 threads take three parts in the time a 1-thread worker takes one.
                sizes = shares(len(free), [getattr(w, "build_threads", None) or 1 for w in taken], MOST_IN_AHEAD)
                start = 0
                for worker, size in zip(taken, sizes):
                    chunk, start = free[start:start + size], start + size
                    call = _Call(chunk)
                    for part in chunk:
                        part.ahead, part.built, part.ready_since = call, part.seen, None
                    self.running.append(call)
                    launches.append((call, worker, []))
        for call, worker, held in launches:
            self._count("ahead_calls")
            for _ in call.members:
                self._count("ahead_parts")
            self._log(call, "started")
            threading.Thread(target=self._run, args=(call, worker, held), name="service-ahead-run", daemon=True).start()

    def _run(self, call: _Call, worker, held: list) -> None:
        first = call.members[0]
        argv = ["apply", *[part.path for part in call.members], FLAG]
        code = 78
        try:
            code = self.pool.run_ahead(call, worker, argv, first.env)
        finally:
            elapsed = int((time.monotonic() - call.started) * 1000)
            if call.cancelled.is_set():
                self._count("ahead_cancelled")
                self._log(call, f"cancelled after {elapsed} ms (exit {code})")
            else:
                outcomes = call.outcomes(code)
                for outcome in outcomes.values():
                    self._count(f"ahead_{outcome}" if outcome in ("built", "kept", "skipped") else "ahead_failed")
                # held in this worker's memory: their applies go there, together once all are in (lane S2's S2-6, S2-9)
                held = [part.key for part in call.members if outcomes.get(part.path) in ("built", "kept") and part.key]
                if held:
                    self.pool.remember_group(held, worker)
                summary = ", ".join(f"{n} {o}" for o, n in sorted(_tally(outcomes.values()).items()))
                # the engine's own lines under it (lane S2: where a several-part ahead's time goes, for F and R2)
                self._log(call, f"done in {elapsed} ms (exit {code}): {summary}",
                          "".join(f"\n    {line}" for line in call.engine_timings[:20]))
            with self.lock:
                for part in call.members:
                    if part.ahead is call:
                        part.ahead = None
                        if part.seen != call.stats.get(part.path) and part.seen is not None:
                            part.ready_since = None     # saved again meanwhile: ready again at its next still poll
                with contextlib.suppress(ValueError):
                    self.running.remove(call)
            call.done.set()
            self._dispatch()

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

    def _count(self, name: str) -> None:
        with self.count_lock:
            self.counts[name] = self.counts.get(name, 0) + 1

    def _counts_text(self) -> str:
        with self.count_lock:
            return json.dumps({k: v for k, v in self.counts.items() if v}, separators=(",", ":"))

    def _log(self, call: _Call, what: str, below: str = "") -> None:
        with contextlib.suppress(Exception):
            names = ", ".join(os.path.basename(p.path) for p in call.members[:8])
            more = f" and {len(call.members) - 8} more" if len(call.members) > 8 else ""
            self.service.log(f"ahead: {len(call.members)} part(s) ({names}{more}) {what}; {self._counts_text()}{below}")


def shares(parts: int, weights: list[int], most: int) -> list[int]:
    """`parts` split over workers in proportion to their weights (largest remainders), each at least one part when
    there are as many parts as workers, at most `most` (what the cap leaves waits for the next ahead)."""
    n = len(weights)
    if not n or parts <= 0:
        return [0] * n
    floor = 1 if parts >= n else 0
    exact = [parts * w / sum(weights) for w in weights]
    sizes = [max(floor, int(x)) for x in exact]
    while sum(sizes) > parts:
        j = max((k for k in range(n) if sizes[k] > floor), key=lambda k: sizes[k] - exact[k])
        sizes[j] -= 1
    while sum(sizes) < parts:
        j = max(range(n), key=lambda k: exact[k] - sizes[k])
        sizes[j] += 1
    return [min(most, size) for size in sizes]


def _tally(values) -> dict[str, int]:
    found: dict[str, int] = {}
    for value in values:
        found[value] = found.get(value, 0) + 1
    return found
