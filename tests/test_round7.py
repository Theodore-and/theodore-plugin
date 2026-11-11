"""Round 7, in process: the update state machine under one lock (forced interleavings), the report switch, bounded
answers, specimen folders, revocation, environment isolation, keychain tombstones and host settings."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from test_hardening import _iso, client_home  # noqa: E402
from fake_api import b64u  # noqa: E402

from theodore_client import account, api, core  # noqa: E402


def engine_on_disk(version: str) -> dict:
    entry = {"version": version, "sha256": hashlib.sha256(version.encode()).hexdigest(), "kind": "engine-source"}
    folder = core.home().engines / version
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".theodore-engine.json").write_text(json.dumps(entry))
    return entry


def set_current(version: str) -> None:
    core.write_json(core.home().engines / "current.json", {"version": version})


class Pause:
    """Stops thread `who` when it calls `name` from inside `inside` (once the real function has run), until released
    or for 3 seconds."""

    def __init__(self, module, name: str, who: str, inside: str = "activate_checked"):
        self.module, self.name, self.who, self.inside = module, name, who, inside
        self.real = getattr(module, name)
        self.reached, self.go = threading.Event(), threading.Event()

    def called_inside(self) -> bool:
        frame = sys._getframe(2)
        for _ in range(8):          # through the mock's own frames
            if frame is None:
                return False
            if frame.f_code.co_name == self.inside:
                return True
            frame = frame.f_back
        return False

    def __call__(self, *args, **kwargs):
        result = self.real(*args, **kwargs)
        if threading.current_thread().name == self.who and not self.reached.is_set() and self.called_inside():
            self.reached.set()
            self.go.wait(3.0)
        return result


class UpdateStateMachine(unittest.TestCase):
    """Ruling 1: one lock covers accepting a document, activating, the pending activation and the reset."""

    @contextlib.contextmanager
    def patched(self, fetches: dict, pause: Pause | None = None):
        from theodore_client import install
        def fetch(timeout=0):
            return fetches[threading.current_thread().name]()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(install, "fetch_channel_manifest", side_effect=fetch))
            stack.enter_context(mock.patch.object(install, "engine_artifact", side_effect=lambda m: m["engine"]))
            stack.enter_context(mock.patch.object(install, "runtime_ready", return_value=True))
            stack.enter_context(mock.patch.object(install, "start_packs_job", return_value=False))
            stack.enter_context(mock.patch.object(install, "collect_garbage", return_value=[]))
            stack.enter_context(mock.patch.object(install.Job, "spawn", side_effect=AssertionError("no download here")))
            if pause:
                stack.enter_context(mock.patch.object(pause.module, pause.name, side_effect=pause))
            yield

    def run_threads(self, first: threading.Thread, second: threading.Thread, pause: Pause) -> bool:
        """Start `first`, wait until it is paused, start `second`, give it a moment, release `first`. True when the
        second finished while the first was paused (it was not held by the lock)."""
        first.start()
        self.assertTrue(pause.reached.wait(10), "the first thread never reached the pause")
        second.start()
        second.join(1.5)
        finished_during_pause = not second.is_alive()
        pause.go.set()
        first.join(30)
        second.join(30)
        return finished_during_pause

    def test_an_older_document_paused_after_its_freshness_read_never_wins(self):
        # The review's interleaving: engine 3 is current; A accepts an older document choosing 2 and pauses after its
        # freshness read; B accepts a newer one choosing 3 (nothing to activate); A resumes. Engine 3 must stay.
        from theodore_client import install
        with client_home():
            core.home().ensure()
            two, three = engine_on_disk("2.0.0"), engine_on_disk("3.0.0")
            set_current("3.0.0")
            now = core.now()
            older, newer = {"server_time": _iso(now - 60), "engine": two}, {"server_time": _iso(now - 1), "engine": three}

            def fetch_a():
                install.accept_fresh("channel", older, "release manifest")
                return older

            def fetch_b():
                install.accept_fresh("channel", newer, "release manifest")
                return newer
            pause = Pause(install, "below_floor", "A")
            errors = []

            def body():
                try:
                    install.check_updates(force=True, wait=0)
                except Exception as exc:       # noqa: BLE001
                    errors.append(exc)
            with self.patched({"A": fetch_a, "B": fetch_b}, pause):
                self.run_threads(threading.Thread(target=body, name="A"), threading.Thread(target=body, name="B"), pause)
            self.assertEqual(errors, [])
            seen = json.loads((core.home().state / "manifest-times.json").read_text())
            self.assertEqual(seen["channel_engine"], "3.0.0", "the newest accepted document chose 3")
            self.assertEqual(install.current_engine(), "3.0.0", "the active engine is never older than the newest accepted document names")

    def test_nothing_below_the_floor_activates_after_a_reset_has_returned(self):
        # Current 2, a completed download of 1 pending; the activation pauses after its floor check; a reset runs.
        from theodore_client import install
        with client_home():
            core.home().ensure()
            one, _two = engine_on_disk("1.0.0"), engine_on_disk("2.0.0")
            set_current("2.0.0")
            now = core.now()
            install.accept_fresh("channel", {"server_time": _iso(now - 60), "engine": one}, "release manifest")
            install.save_update_state(pending={"version": "1.0.0", "sha256": one["sha256"], "issued": now - 60},
                                      last_check=int(now), last_attempt=int(now))
            pause = Pause(install, "below_floor", "A")
            returned = {}

            def check():
                install.check_updates(force=False, wait=0)

            def reset():
                install.reset_update_state("test")
                returned["at"] = time.monotonic()
                returned["engine"] = install.current_engine()
            with self.patched({}, pause):
                self.run_threads(threading.Thread(target=check, name="A"), threading.Thread(target=reset, name="R"), pause)
            self.assertIn("at", returned, "the reset finished")
            floor = install.update_state().get("floor") or {}
            current = install.current_engine()
            self.assertGreaterEqual(core.version_key(current), core.version_key(str(floor.get("engine"))),
                                    f"engine {current} is below the floor {floor.get('engine')} set by a reset that has returned")
            self.assertFalse(install.update_state().get("pending"))

    def test_a_lock_that_cannot_be_had_is_retry_and_nothing_is_written(self):
        from theodore_client import install
        with client_home():
            core.home().ensure()
            one = engine_on_disk("1.0.0")
            now = core.now()
            install.accept_fresh("channel", {"server_time": _iso(now - 60), "engine": one}, "release manifest")
            files = [core.home().state / "manifest-times.json", core.home().state / "update.json", core.home().engines / "current.json"]
            install.save_update_state(last_check=1)

            def snapshot():
                return [p.read_bytes() if p.exists() else None for p in files]
            before = snapshot()
            attempts = {
                "accept": lambda: install.accept_fresh("channel", {"server_time": _iso(now), "engine": one}, "release manifest"),
                "activate": lambda: install.activate_checked("1.0.0", now, "test"),
                "reset": lambda: install.reset_update_state("test"),
                "save": lambda: install.save_update_state(last_check=2),
                "pending": lambda: install.check_updates(force=False, wait=0),
            }
            install.save_update_state(pending={"version": "1.0.0", "sha256": one["sha256"], "issued": now}, last_check=int(now))
            before = snapshot()
            outcomes = {}
            holding, release = threading.Event(), threading.Event()

            def holder():
                with core.lock(install.UPDATE_LOCK, timeout=5):
                    holding.set()
                    release.wait(30)
            other = threading.Thread(target=holder)
            other.start()
            self.assertTrue(holding.wait(10))
            try:
                with mock.patch.object(install, "UPDATE_LOCK_TIMEOUT_S", 0.3):
                    for name, attempt in attempts.items():
                        try:
                            attempt()
                            outcomes[name] = "done"
                        except core.Status as status:
                            outcomes[name] = status.name
            finally:
                release.set()
                other.join(10)
            self.assertEqual(outcomes, {name: "RETRY" for name in attempts})
            self.assertEqual(snapshot(), before, "no write without the lock")

    def test_the_lock_is_one_and_reentrant_within_a_thread(self):
        from theodore_client import install
        with client_home():
            core.home().ensure()
            with core.lock(install.UPDATE_LOCK, timeout=1):
                install.save_update_state(last_check=5)         # the same thread may write while it holds the lock
            self.assertEqual(install.update_state()["last_check"], 5)


if __name__ == "__main__":
    unittest.main()
