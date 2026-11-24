"""The service's clock: everything today's commands did on their way that no command needs to wait for (DESIGN §3.1).

- the usage counts (usage.flush, which sends only what is due) and the reports kept for later (outbox.flush);
- the update check at each session's open and every 6 hours (install.check_updates, which keeps its own six-hour
  floor), and the hourly heartbeat;
- a withdrawal of this computer's access (withdraw.resume), after the workers are stopped;
- the sweeps: specimen folders older than an hour, transient sheet files, dead MCP servers' markers;
- the pool: idle workers retired, statistics of busy ones.
Nothing here is ever on a call's path, and nothing here raises.
"""

from __future__ import annotations

import contextlib
import threading
import time

from .. import core

USAGE_EVERY_S = 300.0
OUTBOX_EVERY_S = 300.0
WITHDRAW_EVERY_S = 60.0
SWEEP_EVERY_S = 3600.0
TEXTS_SWEEP_EVERY_S = 60.0
KEEP_WARM_EVERY_S = 45.0           # the texts' kept-alive connection, while a relay is attached (lane D's keep_warm)
UPDATE_EVERY_S = 6 * 3600.0
HEARTBEAT_EVERY_S = 600.0
POOL_EVERY_S = 10.0
MARKERS_EVERY_S = 86400.0


class Timers:
    def __init__(self, service):
        self.service = service
        self._stop = threading.Event()
        now = time.monotonic()
        self._jobs = [
            [POOL_EVERY_S, self._pool, now + POOL_EVERY_S],
            [TEXTS_SWEEP_EVERY_S, self._texts_sweep, now + TEXTS_SWEEP_EVERY_S],
            [KEEP_WARM_EVERY_S, self._keep_warm, now + KEEP_WARM_EVERY_S],
            [WITHDRAW_EVERY_S, self._withdraw, now + 5.0],
            [USAGE_EVERY_S, self._usage, now + 30.0],
            [OUTBOX_EVERY_S, self._outbox, now + 60.0],
            [HEARTBEAT_EVERY_S, self._heartbeat, now + 120.0],
            [UPDATE_EVERY_S, self._updates, now + UPDATE_EVERY_S],
            [SWEEP_EVERY_S, self._sweep, now + 10.0],
            [MARKERS_EVERY_S, self._markers, now + 15.0],
        ]

    def start(self) -> None:
        threading.Thread(target=self._loop, name="service-timers", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            now = time.monotonic()
            for job in self._jobs:
                every, run, due = job
                if now >= due:
                    job[2] = now + every
                    self._safely(run)

    def _safely(self, run) -> None:
        try:
            run()
        except Exception as exc:  # noqa: BLE001  a timer never ends the service
            self.service.log(f"timer {getattr(run, '__name__', run)}: {exc.__class__.__name__}: {exc}")

    # --- jobs -----------------------------------------------------------------------------------------------------------

    def _pool(self) -> None:
        self.service.pool.tick()

    def _texts_sweep(self) -> None:
        sweep = getattr(self.service.texts, "sweep", None)
        if sweep is not None:
            sweep()

    def _keep_warm(self) -> None:
        keep_warm = getattr(self.service.texts, "keep_warm", None)
        if keep_warm is not None and self.service.relays_attached():
            keep_warm()

    def _withdraw(self) -> None:
        from .. import withdraw
        if not withdraw.pending():
            return
        self.service.log("access withdrawn: stopping the workers, then removing Theodore's files")
        self.service.pool.close()
        with contextlib.suppress(Exception):
            self.service.texts.forget(refused=True)
        withdraw.resume()
        self.service.stop("this computer's access was withdrawn")

    def _usage(self) -> None:
        from .. import usage
        usage.flush()

    def _outbox(self) -> None:
        from .. import outbox
        outbox.flush()

    def _heartbeat(self) -> None:
        from .. import commands, install
        version = install.selected_engine()
        if version:
            commands._heartbeat(version)            # noqa: SLF001  hourly at most, by its own record

    def _updates(self, session_id: str | None = None) -> None:
        """install.check_updates as the first command of a session runs it (that session's id names it new)."""
        from .. import install
        from . import context
        ctx = context.CallContext(session=session_id or "", env={"CLAUDE_CODE_SESSION_ID": session_id} if session_id else {})
        with context.entered(ctx):
            with contextlib.suppress(core.Status):
                install.check_updates(force=False, wait=0.0, timeout=3.0)

    def session_opened(self, session) -> None:
        threading.Thread(target=self._safely, args=(lambda: self._updates(session.id),), name="update-check",
                         daemon=True).start()

    def _sweep(self) -> None:
        from .. import specimens
        specimens.sweep()

    def _markers(self) -> None:
        """state/servers/<pid>: the markers today's MCP servers left; one naming a dead process goes."""
        from . import endpoint
        folder = core.home().state / "servers"
        if not folder.is_dir():
            return
        for marker in folder.iterdir():
            with contextlib.suppress(OSError, ValueError):
                if marker.name.isdigit() and not endpoint.pid_alive(int(marker.name)):
                    marker.unlink()
