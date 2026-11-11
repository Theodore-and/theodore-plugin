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


AGENT_TOKEN = "x." + b64u(json.dumps({"feat": ["agent-reports"], "exp": 9999999999}).encode()) + ".y"


class ReportSwitch(unittest.TestCase):
    """Ruling 2: a cached switch, on or off, older than an hour or dated ahead of the clock is unknown: the client asks
    again and sends nothing unless the fresh answer says on."""

    def cache(self, home: Path, enabled: bool, age: float, source: str = "server") -> None:
        account.save_token(AGENT_TOKEN, auto_reports=enabled)
        record = json.loads((home / "state" / "entitlement.json").read_text())
        record["auto_reports"] = {"enabled": enabled, "source": source, "at": int(core.now() - age)}
        (home / "state" / "entitlement.json").write_text(json.dumps(record))

    def test_on_or_off_outside_zero_to_an_hour_is_unknown(self):
        with client_home() as home:
            core.home().ensure()
            for enabled in (True, False):
                for source in ("server", "refused"):
                    for age, fresh in ((-1, False), (3601, False), (86399, False), (0, True), (10, True), (3599, True)):
                        with self.subTest(enabled=enabled, source=source, age=age):
                            self.cache(home, enabled, age, source)
                            expected = ((True, "") if enabled else (False, "off")) if fresh else (False, "unknown")
                            if source == "refused" and enabled and fresh:
                                expected = (False, "unknown")        # a refusal never records "on"
                            self.assertEqual(account.automatic_reports(), expected)

    def report(self, refresh_answer):
        from theodore_client import commands
        sent = []
        with mock.patch.object(account, "refresh", side_effect=refresh_answer) as refreshed, \
                mock.patch.object(commands, "_api", side_effect=lambda *a, **k: sent.append(a) or (200, {}, b'{"ref": "TR-1"}')), \
                mock.patch("builtins.print"):
            code = commands.report("bug", "A title", "Some details", None, None, None, "automatic", False)
        return code, refreshed.call_count, sent

    def test_an_expired_off_is_asked_again_and_only_a_fresh_on_sends(self):
        with client_home() as home:
            core.home().ensure()
            self.cache(home, False, 3601)
            code, asked, sent = self.report(lambda timeout=0: account.save_token(AGENT_TOKEN, auto_reports=True))
            self.assertEqual((code, asked, len(sent)), (0, 1, 1), "a stale off is asked again; the fresh on sends")
            for answer in (lambda timeout=0: account.save_token(AGENT_TOKEN, auto_reports=False),
                           lambda timeout=0: account.save_token(AGENT_TOKEN),
                           api.NetworkError("down")):
                self.cache(home, False, -1)
                code, asked, sent = self.report(answer)
                self.assertEqual((code, asked, sent), (73, 1, []), "off, no answer or no connection: nothing is sent")


class BoundedBodies(unittest.TestCase):
    """Ruling 3: every answer body is read up to a bound, success or error (errors at 64 KiB); a longer one is refused."""

    class Body:
        """An answer of `size` bytes that records how much was asked for (never more than it holds is produced)."""

        def __init__(self, size: int, headers: dict | None = None):
            self.size, self.pos, self.asked = size, 0, []
            self.status, self.headers = 200, headers or {}

        def read(self, n=-1):
            self.asked.append(n)
            if n is None or n < 0:
                n = self.size - self.pos
            n = min(n, self.size - self.pos)
            self.pos += n
            return b"x" * n

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

    def opener(self, body, error: int | None = None):
        import email.message
        import urllib.error

        class Opener:
            def open(self, req, timeout=0):
                if error:
                    headers = email.message.Message()
                    raise urllib.error.HTTPError(req.full_url, error, "error", headers, body)
                return body
        return Opener()

    def test_an_error_body_is_read_to_64_kib_and_a_longer_one_is_refused(self):
        with client_home():
            for size, refused in ((64 * 1024, False), (64 * 1024 + 1, True), (1_048_577, True)):
                with self.subTest(size=size):
                    body = self.Body(size)
                    with mock.patch.object(api, "_opener", return_value=self.opener(body, error=503)):
                        with self.assertRaises(api.TooLarge if refused else api.ApiError):
                            api.request("GET", "https://api.example.test/v1/x", limit=1_048_576)
                    self.assertTrue(body.asked and all(0 <= n <= 64 * 1024 + 1 for n in body.asked), body.asked)

    def test_a_success_body_without_an_explicit_limit_is_still_bounded(self):
        with client_home():
            body = self.Body(api.DEFAULT_LIMIT + 1)
            with mock.patch.object(api, "_opener", return_value=self.opener(body)):
                with self.assertRaises(api.TooLarge):
                    api.request("GET", "https://api.example.test/v1/x")
            self.assertTrue(all(0 <= n <= api.DEFAULT_LIMIT + 1 for n in body.asked), body.asked)

    def test_a_download_stops_at_its_size_and_its_error_body_is_bounded(self):
        with client_home() as home:
            dest = home / "staging" / "file.bin"
            body = self.Body(10_000)
            with mock.patch.object(api, "_opener", return_value=self.opener(body)):
                with self.assertRaises(core.Status) as raised:
                    api.download("https://dl.example.test/f", dest, sha256="0" * 64, size=100)
            self.assertEqual(raised.exception.name, "SETUP_FAILED")
            self.assertLessEqual(body.pos, 100 + (1 << 18), "reading stopped once more than the size had arrived")
            self.assertFalse(dest.exists() or dest.with_name("file.bin.part").exists())
            body = self.Body(1_048_577)
            with mock.patch.object(api, "_opener", return_value=self.opener(body, error=500)):
                with self.assertRaises(api.TooLarge):
                    api.download("https://dl.example.test/f", dest, sha256="0" * 64)
            self.assertTrue(all(0 <= n <= 64 * 1024 + 1 for n in body.asked), body.asked)


class KeychainTombstone(unittest.TestCase):
    """Ruling 5: a keychain deletion that fails or times out after a revocation leaves a local tombstone: the old item
    is never read again, and its deletion is retried on later runs."""

    def test_a_credential_whose_deletion_failed_never_loads_again(self):
        import io
        import subprocess
        from theodore_client import credentials
        keychain = {"item": None}
        calls = []
        behaviour = {"delete": "hang"}
        clock = {"offset": 0.0}

        def security(command, **kwargs):
            verb = command[1] if command[1] != "-i" else "add"
            calls.append(verb)
            if verb == "add":
                keychain["item"] = kwargs["input"].split('-w "', 1)[1].split('"', 1)[0]
                return subprocess.CompletedProcess(command, 0, "", "")
            if verb == "find-generic-password":
                return subprocess.CompletedProcess(command, 0 if keychain["item"] else 44, (keychain["item"] or "") + "\n", "")
            if verb == "delete-generic-password":
                if behaviour["delete"] == "hang":
                    raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
                if behaviour["delete"] == "fail":
                    return subprocess.CompletedProcess(command, 1, "", "SecKeychainItemDelete: denied")
                keychain["item"] = None
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(command)
        real_now = core.now
        with client_home() as home, mock.patch.dict(os.environ, {"THEODORE_CREDENTIAL_STORE": "keychain"}), \
                mock.patch.object(subprocess, "run", side_effect=security), mock.patch("sys.stderr", new=io.StringIO()), \
                mock.patch.object(core, "now", side_effect=lambda: real_now() + clock["offset"]):
            core.home().ensure()
            self.assertEqual(credentials.store("inst-1", "secret-1"), "keychain")
            self.assertEqual(credentials.load()["installation_secret"], "secret-1")
            for failure in ("hang", "fail"):
                with self.subTest(failure=failure):
                    behaviour["delete"] = failure
                    if failure == "fail":
                        self.assertEqual(credentials.store("inst-2", "secret-2"), "keychain")
                        self.assertEqual(credentials.load()["installation_secret"], "secret-2", "a new sign-in replaces the tombstone")
                    self.assertFalse(credentials.delete(), "the item could not be confirmed deleted")
                    self.assertTrue((home / "state" / "keychain-revoked").exists())
                    self.assertIsNotNone(keychain["item"], "the item is still in the keychain")
                    clock["offset"] += 86401              # past the day the keychain is left alone after a timeout
                    calls.clear()
                    self.assertIsNone(credentials.load(), "the old credential is unusable from then on")
                    self.assertNotIn("find-generic-password", calls, "the old item is not even read")
                    self.assertIn("delete-generic-password", calls, "its deletion is retried")
            behaviour["delete"] = "ok"
            clock["offset"] += 86401
            self.assertIsNone(credentials.load())
            self.assertIsNone(keychain["item"], "the retried deletion removed it")
            self.assertFalse((home / "state" / "keychain-revoked").exists(), "the tombstone goes once the item is gone")
            self.assertEqual(credentials.store("inst-3", "secret-3"), "keychain")
            self.assertEqual(credentials.load()["installation_secret"], "secret-3")


if __name__ == "__main__":
    unittest.main()
