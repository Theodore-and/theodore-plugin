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

import json
import os
import threading
import time
from pathlib import Path

from . import workers

GATHER_S = 0.015                 # how long a burst's later part applies wait for each other, at most
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
        self.phase = "starting"

    def out(self, text: str, stream: str = "stdout") -> None:
        if stream == "stdout":
            self.stdout.append(text)

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
        self.last_dispatch = -1e9
        self.timer: threading.Timer | None = None
        self.argv: list[str] = []
        self.env: dict = {}


class Gatherer:
    def __init__(self, pool):
        self.pool = pool
        self.lock = threading.Lock()
        self.bursts: dict[str, float] = {}          # lead (real path) -> when its prepare announced a burst
        self.leads: dict[tuple, _Lead] = {}          # (version, lead, the rest of argv, env) -> its gathering
        self.claimed: dict[str, int] = {}            # per version: free workers promised to runs not yet started
        pool.on_release.append(self._worker_free)

    # --- what prepare said -----------------------------------------------------------------------------------------

    def prepared(self, lead: str, at_once: bool) -> None:
        real = os.path.realpath(lead)
        with self.lock:
            if at_once:
                self.bursts[real] = time.monotonic()
            else:
                self.bursts.pop(real, None)

    # --- a part's apply --------------------------------------------------------------------------------------------

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
        if since is None or time.monotonic() - since > BURST_FOR_S or not self.pool.can_apply_parts(version):
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
            if twice:
                pass
            elif not lead.pending and now - lead.last_dispatch > GATHER_S and self._free(version) > 0:
                alone = True                            # a lone part apply: at once, never a window
                lead.last_dispatch = now
                self._claim(version)
            else:
                lead.pending.append(member)
                if lead.timer is None:
                    lead.timer = threading.Timer(GATHER_S, self._cut, args=(shape,))
                    lead.timer.daemon = True
                    lead.timer.start()
        if twice:                               # after the waiting one: the part's key lock keeps them in turn
            return self.pool.run(call, version, argv, env, None)
        if alone:
            return self._alone(call, version, argv, env)
        waited_since = time.monotonic()
        while not member.done.wait(0.25):
            with self.lock:
                lead = self.leads.get(shape)
                still = lead is not None and member in lead.pending
                if still and call.cancelled.is_set():   # still gathering: it simply leaves
                    lead.pending.remove(member)
                    return 130, []
                stuck = still and lead.timer is None and time.monotonic() - waited_since > WAIT_FREE_S
            if stuck:                                   # no worker came free (one died, or long calls hold them)
                self._cut(shape, force=True)
        call.out(member.output)
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
        it). With none free they wait for the next (_worker_free), or with `force` go to the pool's queue as one."""
        version = shape[0]
        launch = []
        with self.lock:
            lead = self.leads.get(shape)
            if lead is None:
                return
            lead.timer = None
            if not lead.pending:
                return
            free = self._free(version)
            if free <= 0:
                if not force:
                    return
                free = 1
            share = max(1, min(MOST_IN_BATCH, -(-len(lead.pending) // free)))
            while lead.pending and free > 0:
                batch, lead.pending = lead.pending[:share], lead.pending[share:]
                launch.append(batch)
                self._claim(version)
                free -= 1
            lead.last_dispatch = time.monotonic()
            argv, env = list(lead.argv), dict(lead.env)
        for batch in launch:
            threading.Thread(target=self._run_batch, args=(version, argv, env, batch), name="parts-batch",
                             daemon=True).start()

    def _worker_free(self, version: str) -> None:
        with self.lock:
            waiting = [shape for shape, lead in self.leads.items()
                       if shape[0] == version and lead.pending and lead.timer is None]
        for shape in waiting:
            self._cut(shape)

    def _alone(self, call, version: str, argv: list[str], env: dict, keys: list[str] | None = None) -> tuple[int, list]:
        """One part on its own: the plain apply, its output streamed to its own call."""
        released = []

        def acquired() -> None:
            if not released:
                released.append(True)
                self._unclaim(version)
        try:
            return self.pool.run(call, version, argv, env, None, keys=keys, acquired=acquired)
        finally:
            acquired()

    def _run_batch(self, version: str, argv: list[str], env: dict, batch: list[_Member]) -> None:
        if len(batch) == 1:                     # a share of one: the plain apply (one part's answer, as alone)
            member = batch[0]
            index = next(i for i, arg in enumerate(argv[1:], 1) if not arg.startswith("-"))
            try:
                member.exit, member.pending = self._alone(member.call, version, argv[:index] + [member.part] + argv[index + 1:],
                                                          env, keys=[member.key] if member.key else [])
            except Exception as exc:  # noqa: BLE001
                member.exit, member.output = 78, f"THEODORE INTERNAL: the Studio part's apply failed: {exc}\n"
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
                                           order=workers.RANKS["write"], acquired=acquired)
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
            member.done.set()
