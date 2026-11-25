"""A Studio deck's parts applied together (lanes G and F; the coordinator's decision, 25 Nov).

A part's apply (`apply <lead>/parts/<part>`) that arrives alone is dispatched at once and never waits. Part applies
of the same lead that arrive while others of it are waiting or were dispatched less than GATHER_S ago are gathered
(at most GATHER_S), divided over the free workers of the engine (about four parts a worker on 4 vCPU: lane F), and
sent as one call per worker, `pptx apply <part> <part> ...` (lane G): one build for its parts. When no worker is
free, they wait here, so the parts that arrive meanwhile join them, and each worker that comes free takes an equal
share. The engine answers one JSON object whose `parts` rows hold each part's `exit` and `stdout`, exactly what an
apply of that part alone prints (lane G); each designer's call gets those as its own, and its pictures attach from
them. A run refused as a whole (exit 2: not parts of one deck; licence 77; internal 78) answers every designer with
its exit and output. A part already waiting in the gathering is never named twice: a second apply of it runs after.

Only for a lead whose `prepare` answered that its parts come at once (`in_turn` false), and only when a ready worker
of the engine lists "apply-parts" in `ready.features` (C2); otherwise each part's apply runs alone, as before.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path

from . import workers

GATHER_S = 0.015                 # how long a burst's later part applies wait for each other once all have come
GATHER_CAP_S = 0.25              # at most this long while others of the burst are still on their way (admission)
WAIT_FREE_S = 1.0                # waiting here longer for a worker to come free, they go to the pool's queue instead
BURST_FOR_S = 6 * 3600.0         # a deck's `prepare` announces its parts' burst for this long
MOST_IN_BATCH = 32


def _lead_and_part(argv: list[str], cwd: str | None) -> tuple[str, str, int] | None:
    """(lead folder, part folder, index of the part in argv), as real paths, for `apply <lead>/parts/<part>`."""
    if not workers.part_apply(argv, cwd):
        return None
    for index, arg in enumerate(argv[1:], 1):
        if not arg.startswith("-"):
            path = Path(arg) if os.path.isabs(arg) else Path(cwd or ".") / arg
            real = os.path.realpath(path)
            return os.path.dirname(os.path.dirname(real)), real, index
    return None


class _Member:
    def __init__(self, call, part: str, key: str | None):
        self.call, self.part, self.key = call, part, key
        self.done = threading.Event()
        self.exit = 78
        self.output = ""
        self.counters: dict = {}
        self.pending: list = []
        self.timing: dict = {}
        self.entered = time.perf_counter()    # into the gathering
        self.sent: float | None = None        # its build went out (the timing line's "gathering")


class _BatchCall:
    """What the pool needs of a call, for a batch: its output collected, its phase passed to every designer's call."""

    def __init__(self, members: list[_Member]):
        self.members = members
        first = members[0].call
        self.id = f"parts-{first.id}"
        self.cwd = first.cwd
        self.images = False             # each part's pictures attach from its own answer (lane J's rule)
        self.deadline_ms = None
        self.cancelled = threading.Event()
        self.worker = None
        self.worker_images = False
        self.counters: dict = {}
        self.stdout: list[str] = []
        self.engine_timings: list[str] = []
        self.phase = "starting"

    def out(self, text: str, stream: str = "stdout") -> None:
        if stream == "stdout":
            self.stdout.append(text)
        elif "timings: " in text or "gc: " in text:                                          # router.TIMING_LINES
            self.engine_timings.extend(line for line in text.splitlines() if line.startswith(("timings: ", "gc: ")))

    def image(self, *_args, **_kwargs) -> bool:
        return False

    def set_phase(self, phase: str, done: int | None = None, total: int | None = None) -> None:
        self.phase = phase
        for member in self.members:
            member.call.set_phase(phase, done, total)

    def count(self, name: str, value: int = 1) -> None:
        self.counters[name] = int(self.counters.get(name, 0)) + int(value)


class _Lead:
    def __init__(self):
        self.pending: list[_Member] = []
        self.first_pending = 0.0
        self.last_dispatch = -1e9
        self.timer: threading.Timer | None = None
        self.argv: list[str] = []
        self.env: dict = {}


class Gatherer:
    def __init__(self, pool):
        self.pool = pool
        self.lock = threading.Lock()
        self.bursts: dict[str, float] = {}          # lead (real path) -> when its prepare announced a burst
        self.sizes: dict[str, int] = {}             # lead (real path) -> how many parts its prepare announced
        self.leads: dict[tuple, _Lead] = {}          # (version, lead, the rest of argv, env) -> its gathering
        self.claimed: dict[str, int] = {}            # per version: free workers promised to runs not yet started
        self.on_way: dict[str, int] = {}             # per lead: part applies arrived at the service, not yet here
        pool.on_release.append(self._worker_free)

    # --- what prepare said -----------------------------------------------------------------------------------------

    def prepared(self, lead: str, at_once: bool, count: int | None = None) -> None:
        real = os.path.realpath(lead)
        with self.lock:
            if at_once:
                self.bursts[real] = time.monotonic()
                if isinstance(count, int) and count > 0:
                    self.sizes[real] = count
                else:
                    self.sizes.pop(real, None)
            else:
                self.bursts.pop(real, None)
                self.sizes.pop(real, None)

    def _cap(self, lead: str) -> int:
        """The most parts one build takes: the burst over the pool, ceil(parts / workers) (4 for 15 parts on 4 workers;
        lane S2's rig 7: with build-aheads holding three workers, 12 dropped parts went to the one free worker as one
        build of 2 to 4 s while the others came free within 0.5 s). The rest wait for the next worker to come free."""
        size = self.sizes.get(lead)
        if not size:
            return MOST_IN_BATCH
        return max(1, min(MOST_IN_BATCH, -(-size // max(1, int(getattr(self.pool, "most", 1) or 1)))))

    # --- a part's apply --------------------------------------------------------------------------------------------

    def arriving(self, argv: list[str], call) -> str | None:
        """A part apply of a deck whose parts come at once reached the service (before its admission: the licence,
        the engine's choice): counted, so the first of a burst to reach the gathering knows others are on their way
        and does not run alone. Returns its lead, to give back to `arrived`."""
        if call.stdin is not None:
            return None
        found = _lead_and_part(argv, call.cwd)
        if found is None:
            return None
        with self.lock:
            if found[0] not in self.bursts:
                return None
            self.on_way[found[0]] = self.on_way.get(found[0], 0) + 1
        return found[0]

    def arrived(self, lead: str | None) -> None:
        if lead is None:
            return
        with self.lock:
            left = self.on_way.get(lead, 0) - 1
            if left > 0:
                self.on_way[lead] = left
            else:
                self.on_way.pop(lead, None)

    def wants(self, version: str, argv: list[str], call) -> tuple | None:
        """(shape, part) when this call is a part apply of a deck whose parts come at once, on an engine that takes
        several; None to run it as before."""
        if call.stdin is not None:
            return None
        found = _lead_and_part(argv, call.cwd)
        if found is None:
            return None
        lead, part, index = found
        with self.lock:
            since = self.bursts.get(lead)
        # Gathered while the engine can take several parts, or while no worker of it has been ready yet: a cold
        # service's burst waits for a worker anyway, and its first ready worker says which (_cut).
        if since is None or time.monotonic() - since > BURST_FOR_S or self.pool.parts_capability(version) is False:
            return None
        rest = tuple(arg for i, arg in enumerate(argv) if i != index)
        return (version, lead, rest, tuple(sorted((call.env or {}).items()))), part

    def run(self, call, version: str, argv: list[str], env: dict, wanted: tuple) -> tuple[int, list]:
        shape, part = wanted
        member = _Member(call, part, workers.workspace_key(["apply", part], call.cwd))
        alone = False
        with self.lock:
            lead = self.leads.setdefault(shape, _Lead())
            if any(other.part == part for other in lead.pending):
                twice = True                    # the same part already waits: a run names each part once (lane G)
            else:
                twice = False
            lead.argv, lead.env = list(argv), dict(env)
            now = time.monotonic()
            others_coming = self.on_way.get(shape[1], 0) > 0
            if twice:
                pass
            elif not lead.pending and not others_coming and now - lead.last_dispatch > GATHER_S and self._free(version) > 0:
                alone = True                            # a lone part apply: at once, never a window
                lead.last_dispatch = now
                self._claim(version)
            else:
                if not lead.pending:
                    lead.first_pending = now
                lead.pending.append(member)
                if lead.timer is None:
                    lead.timer = threading.Timer(GATHER_S, self._cut, args=(shape,))
                    lead.timer.daemon = True
                    lead.timer.start()
        if twice:                               # after the waiting one: the part's key lock keeps them in turn
            return self.pool.run(call, version, argv, env, None)
        if alone:
            self.pool.leave_group(member.key)
            answer = self._alone(call, version, argv, env)
            with contextlib.suppress(Exception):
                if isinstance(getattr(call, "timing", None), dict):
                    call.timing["gathering_ms"] = 0     # a lone part apply: at once
            return answer
        waited_since = time.monotonic()
        while not member.done.wait(0.25):
            with self.lock:
                lead = self.leads.get(shape)
                still = lead is not None and member in lead.pending
                if still and call.cancelled.is_set():   # still gathering: it simply leaves
                    lead.pending.remove(member)
                    return 130, []
                waited = time.monotonic() - waited_since
                known = self.pool.parts_capability(version) is not None
                stuck = still and lead.timer is None and (waited > WAIT_FREE_S if known else waited > workers.READY_WAIT_S)
            if stuck:                                   # no worker came free (one died, or long calls hold them)
                self._cut(shape, force=True)
        call.out(member.output)
        if member.timing:
            call.timing = dict(member.timing, batch=member.timing.get("batch"))
        for name, value in member.counters.items():
            if isinstance(value, int):
                call.count(name, value)
        return member.exit, member.pending

    # --- cutting batches -----------------------------------------------------------------------------------------------

    def _free(self, version: str) -> int:
        return self.pool.idle_count(version) - self.claimed.get(version, 0)

    def _claim(self, version: str) -> None:
        self.claimed[version] = self.claimed.get(version, 0) + 1

    def _unclaim(self, version: str) -> None:
        with self.lock:
            self.claimed[version] = max(0, self.claimed.get(version, 0) - 1)

    def _cut(self, shape: tuple, force: bool = False) -> None:
        """The window closed: every waiting part of the lead goes out now, in equal shares over the free workers
        (waiting for a busy one to come free costs a whole build; one more part in a batch costs its own share of
        it). With none free they wait for the next (_worker_free), or with `force` go to the pool's queue as one.
        Until a worker of the engine has been ready they wait for it (a cold service's burst), and when its workers
        take one part a call each runs alone."""
        version = shape[0]
        launch, alone = [], []
        with self.lock:
            lead = self.leads.get(shape)
            if lead is None:
                return
            lead.timer = None
            if not lead.pending:
                return
            argv, env = list(lead.argv), dict(lead.env)
            early = self._home_groups(version, lead)
            if early:                               # complete home groups: out now, not at the window's end
                self._launch(version, argv, env, early, len(lead.pending), "at once: every part of their ahead is in")
                if not lead.pending:
                    return
            if not force and self.on_way.get(shape[1], 0) > 0 and \
                    time.monotonic() - lead.first_pending < GATHER_CAP_S:
                lead.timer = threading.Timer(GATHER_S, self._cut, args=(shape,))     # others of it are on their way
                lead.timer.daemon = True
                lead.timer.start()
                return
            capability = self.pool.parts_capability(version)
            argv, env = list(lead.argv), dict(lead.env)
            if capability is None and not force:
                starting = self._starting(version)
            elif capability is not True:
                alone, lead.pending = lead.pending, []
                lead.last_dispatch = time.monotonic()
            else:
                free = self._free(version)
                if free <= 0 and force:
                    free = 1
                # The parts whose ahead an idle worker holds in memory go to it, all in one call, with no cap: a reused
                # apply is cheap there and only there (lane S2's S2-6: the reuse check took ~1 ms on the worker that
                # built the ahead, 96 to 430 ms on another, reading its ahead.json afresh). The others as before.
                homes: dict[int, tuple] = {}
                rest = []
                for member in lead.pending:
                    home = self.pool.idle_home(member.key)
                    if home is not None:
                        homes.setdefault(id(home), (home, []))[1].append(member)
                    else:
                        rest.append(member)
                for home, members in homes.values():
                    if free <= 0:
                        rest.extend(members)
                        continue
                    launch.append((members, home))
                    self._claim(version)
                    free -= 1
                lead.pending = rest
                if free > 0 and lead.pending:
                    share = max(1, min(self._cap(shape[1]), -(-len(lead.pending) // free)))
                    while lead.pending and free > 0:
                        batch, lead.pending = lead.pending[:share], lead.pending[share:]
                        launch.append((batch, None))
                        self._claim(version)
                        free -= 1
                if launch:
                    lead.last_dispatch = time.monotonic()
            left = len(lead.pending)
        if capability is None and not force:
            if not starting:                    # nothing is starting a worker for them: one starts now
                self.pool.warm_up(version, 1)
            return
        if alone:
            with contextlib.suppress(Exception):
                self.pool.service.log(f"parts: {len(alone)} of a deck, each alone (this engine's workers take one part "
                                      f"a call)")
            for member in alone:
                threading.Thread(target=self._run_batch, args=(version, argv, env, [member]), name="parts-alone",
                                 daemon=True).start()
            return
        if not launch:
            return
        with contextlib.suppress(Exception):     # what the proof reads: a burst's parts, in how many builds
            homed = sum(1 for _b, home in launch if home is not None)
            self.pool.service.log(f"parts: {sum(len(b) for b, _h in launch)} of a deck in {len(launch)} build(s) "
                                  f"({', '.join(str(len(b)) for b, _h in launch)})"
                                  f"{f', {homed} on the worker that built their ahead' if homed else ''}"
                                  f"{f', {left} waiting' if left else ''}")
        sent = time.perf_counter()
        for batch, home in launch:
            for member in batch:
                member.sent = sent
            threading.Thread(target=self._run_batch, args=(version, argv, env, batch, home), name="parts-batch",
                             daemon=True).start()

    def _home_groups(self, version: str, lead: _Lead) -> list:
        """(members, worker) for each idle worker holding an ahead group whose every part not yet applied is waiting
        here (lane S2's S2-9: home groups waited 32 to 118 ms for the window while the rest of the burst came). They
        leave lead.pending; the caller holds self.lock."""
        found = []
        homes: dict[int, tuple] = {}
        for member in lead.pending:
            home = self.pool.idle_home(member.key)
            if home is not None:
                homes.setdefault(id(home), (home, []))[1].append(member)
        for home, members in homes.values():
            have = {m.key for m in members}
            left = set().union(*(self.pool.group_left(m.key) for m in members))
            if not left or not left <= have or self._free(version) <= 0:
                continue
            found.append((members, home))
            self._claim(version)
            lead.pending = [m for m in lead.pending if m not in members]
        if found:
            lead.last_dispatch = time.monotonic()
        return found

    def _launch(self, version: str, argv: list[str], env: dict, launch: list, left: int, how: str) -> None:
        """Start these builds (the caller holds self.lock: the threads do the rest)."""
        with contextlib.suppress(Exception):
            self.pool.service.log(f"parts: {sum(len(b) for b, _h in launch)} of a deck in {len(launch)} build(s) "
                                  f"({', '.join(str(len(b)) for b, _h in launch)}) on the worker that built their "
                                  f"ahead, {how}{f', {left} waiting' if left else ''}")
        sent = time.perf_counter()
        for batch, home in launch:
            for member in batch:
                member.sent = sent
            threading.Thread(target=self._run_batch, args=(version, argv, env, batch, home), name="parts-batch",
                             daemon=True).start()

    def _starting(self, version: str) -> bool:
        with self.pool.cond:
            return any(not w.ready.is_set() for w in self.pool._live(version))     # noqa: SLF001

    def _worker_free(self, version: str) -> None:
        with self.lock:
            waiting = [shape for shape, lead in self.leads.items()
                       if shape[0] == version and lead.pending and lead.timer is None]
        for shape in waiting:
            self._cut(shape)

    def _alone(self, call, version: str, argv: list[str], env: dict, keys: list[str] | None = None,
               prefer=None) -> tuple[int, list]:
        """One part on its own: the plain apply, its output streamed to its own call."""
        released = []

        def acquired() -> None:
            if not released:
                released.append(True)
                self._unclaim(version)
        try:
            return self.pool.run(call, version, argv, env, None, keys=keys, acquired=acquired, prefer=prefer)
        finally:
            acquired()

    def _run_batch(self, version: str, argv: list[str], env: dict, batch: list[_Member], prefer=None) -> None:
        for member in batch:                    # out now: the rest of its ahead group no longer waits for it
            self.pool.leave_group(member.key)
        if len(batch) == 1:                     # a share of one: the plain apply (one part's answer, as alone)
            member = batch[0]
            index = next(i for i, arg in enumerate(argv[1:], 1) if not arg.startswith("-"))
            try:
                member.exit, member.pending = self._alone(member.call, version, argv[:index] + [member.part] + argv[index + 1:],
                                                          env, keys=[member.key] if member.key else [],
                                                          prefer=prefer or self.pool.idle_home(member.key))
            except Exception as exc:  # noqa: BLE001
                member.exit, member.output = 78, f"THEODORE INTERNAL: the Studio part's apply failed: {exc}\n"
            with contextlib.suppress(Exception):
                if member.sent is not None and isinstance(getattr(member.call, "timing", None), dict):
                    member.call.timing["gathering_ms"] = int((member.sent - member.entered) * 1000)
            member.done.set()
            return
        call = _BatchCall(batch)
        index = next(i for i, arg in enumerate(argv[1:], 1) if not arg.startswith("-"))
        run_argv = argv[:index] + [m.part for m in batch] + argv[index + 1:]
        released = []

        def acquired() -> None:
            if not released:
                released.append(True)
                self._unclaim(version)
        try:
            code, _pending = self.pool.run(call, version, run_argv, env, None, keys=[m.key for m in batch if m.key],
                                           order=workers.RANKS["write"], acquired=acquired, prefer=prefer)
        except Exception as exc:  # noqa: BLE001  every designer gets an answer
            code = 78
            call.out(f"THEODORE INTERNAL: the Studio parts applied together failed: {exc}\n")
        finally:
            acquired()
        output = "".join(call.stdout)
        rows: dict[str, dict] = {}
        if code == 0:
            try:
                answer = json.loads(output)
            except ValueError:
                answer = {}
            for row in (answer.get("parts") if isinstance(answer, dict) else None) or []:
                if isinstance(row, dict) and isinstance(row.get("part"), str):
                    rows[os.path.realpath(os.path.join(call.cwd or ".", row["part"]))] = row
        for position, member in enumerate(batch):
            row = rows.get(member.part)
            if code != 0:                       # the call itself refused (2), licence (77), internal (78): everyone
                member.exit, member.output = code, output
            elif row is None or not isinstance(row.get("stdout"), str):
                member.exit = 78
                member.output = ("THEODORE INTERNAL: the engine's answer for this part was missing from the parts "
                                 "applied together.\nNEXT: run the same apply again.\n")
            else:
                member.exit = int(row.get("exit") or 0)
                member.output = row["stdout"]   # exactly what `pptx apply <that part>` alone prints (lane G)
            if position == 0:
                member.counters = dict(call.counters)   # the run's own counts, once
            member.timing = dict(getattr(call, "timing", None) or {}, batch=len(batch))
            if member.sent is not None:
                member.timing["gathering_ms"] = int((member.sent - member.entered) * 1000)
            # the run's own line (`timings: apply-parts ...`); each part's phases are in its own answer (`timings`)
            member.timing["engine_lines"] = [line for line in call.engine_timings
                                             if line.startswith(("timings: apply-parts", "gc: "))]
            member.done.set()
