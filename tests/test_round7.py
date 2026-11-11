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
            version = __import__("theodore_client").__version__
            (core.home().client / version).mkdir(parents=True)
            (core.home().client / version / "__main__.py").write_text("")          # on disk: only the switch is left to do
            files = [core.home().state / "manifest-times.json", core.home().state / "update.json", core.home().engines / "current.json",
                     core.home().client / "current.txt"]
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
                "restrictions": lambda: install.save_restrictions({"server_time": _iso(now + 1), "engine": one, "kill": True}),
                "client": lambda: install.update_client(__import__("theodore_client").__version__, "0" * 64, None, now),
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

    def test_a_reset_between_reading_the_floor_and_clearing_it_is_not_undone(self):
        # Round 8 (review 1): A reads an old reset's floor and decides its newer document may clear it; B resets
        # again (engine 3 is the floor) and returns; A must not clear B's floor and activate engine 2.
        from theodore_client import install
        with client_home():
            core.home().ensure()
            two, _three = engine_on_disk("2.0.0"), engine_on_disk("3.0.0")
            set_current("3.0.0")
            install.reset_update_state("first reset")
            issued = core.now() + 2         # issued after the first reset (whole seconds: _iso drops the fraction)
            doc = {"server_time": _iso(issued), "engine": two}
            real = install.save_update_state
            reached, go, returned = threading.Event(), threading.Event(), {}

            def pausing(**fields):
                if threading.current_thread().name == "A" and fields == {"floor": None} and not reached.is_set():
                    reached.set()
                    go.wait(3.0)
                return real(**fields)

            def fetch_a():
                install.accept_fresh("channel", doc, "release manifest")
                return doc

            def check():
                install.check_updates(force=True, wait=0)

            def reset():
                install.reset_update_state("second reset")
                returned["floor"] = (install.update_state().get("floor") or {}).get("engine")
            with self.patched({"A": fetch_a}), mock.patch.object(install, "save_update_state", side_effect=pausing):
                a, b = threading.Thread(target=check, name="A"), threading.Thread(target=reset, name="B")
                a.start()
                self.assertTrue(reached.wait(10))
                b.start()
                b.join(1.5)
                go.set()
                a.join(30)
                b.join(30)
            self.assertIn("floor", returned)
            current = install.current_engine()
            self.assertGreaterEqual(core.version_key(current), core.version_key(str(returned["floor"])),
                                    f"engine {current} activated below the floor {returned['floor']} of a reset that had returned")

    def test_the_client_switch_is_checked_under_the_lock(self):
        from theodore_client import install
        with client_home() as home:
            core.home().ensure()
            for version in ("0.1.0", "0.2.5", "0.3.0"):
                (home / "client" / version).mkdir(parents=True)
                (home / "client" / version / "__main__.py").write_text("")
            (home / "client" / "current.txt").write_text("0.2.0")
            now = core.now()
            install.save_update_state(floor={"engine": None, "client": "0.2.0", "since": now})
            install.update_client("0.1.0", "0" * 64, None, now - 60)
            self.assertEqual((home / "client" / "current.txt").read_text().strip(), "0.2.0", "below the reset's floor: not switched")
            install.save_update_state(floor=None)
            install.accept_fresh("channel", {"server_time": _iso(now), "engine": {"version": "1.0.0"}, "client": {"version": "0.3.0"}}, "release manifest")
            install.update_client("0.2.5", "0" * 64, None, now - 30)
            self.assertEqual((home / "client" / "current.txt").read_text().strip(), "0.2.0", "a newer document names another client")
            install.update_client("0.3.0", "0" * 64, None, now - 30)
            self.assertEqual((home / "client" / "current.txt").read_text().strip(), "0.3.0", "the client the newest document names")

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


class Round8ReportSwitch(unittest.TestCase):
    """Round 8 (review 2): a refresh never restores a stale ON over an OFF another process recorded meanwhile."""

    def run_race(self, switch, pause_in: str):
        from theodore_client import credentials
        with client_home() as home:
            core.home().ensure()
            credentials.store("inst-1", "secret-1")
            account.save_token(AGENT_TOKEN, auto_reports=True)            # a fresh ON
            reached, go = threading.Event(), threading.Event()
            answer = {"entitlement_token": AGENT_TOKEN, **({"auto_report_enabled": switch} if switch is not None else {})}
            real_write = core.write_json

            def network(*a, **k):
                if pause_in == "network":
                    reached.set()
                    go.wait(3.0)
                return answer

            def writing(path, value, mode=0o644):
                if pause_in == "write" and threading.current_thread().name == "A" and Path(path).name == "entitlement.json" and not reached.is_set():
                    reached.set()
                    go.wait(3.0)
                return real_write(path, value, mode)
            with mock.patch.object(api, "call_json", side_effect=network), mock.patch.object(core, "write_json", side_effect=writing):
                a = threading.Thread(target=lambda: account.refresh(timeout=1), name="A")
                a.start()
                self.assertTrue(reached.wait(10))
                b = threading.Thread(target=account.remember_automatic_reports_off, name="B")
                b.start()
                b.join(1.5)
                go.set()
                a.join(30)
                b.join(30)
            return json.loads((home / "state" / "entitlement.json").read_text())["auto_reports"]

    def test_an_off_recorded_during_the_refresh_wins(self):
        for switch, pause_in in ((None, "write"), (None, "network"), (True, "network")):
            with self.subTest(switch=switch, pause_in=pause_in):
                record = self.run_race(switch, pause_in)
                self.assertIs(record["enabled"], False, record)


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

    def test_the_body_of_a_followed_redirect_is_bounded_too(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        sizes = {"/small": 100, "/large": 64 * 1024 + 1}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path in sizes:
                    body = b"x" * sizes[self.path]
                    self.send_response(302)
                    self.send_header("Location", "/end")
                else:
                    body = b"ok"
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with client_home():
            self.assertEqual(api.request("GET", base + "/small")[2], b"ok")
            with self.assertRaises(api.TooLarge):
                api.request("GET", base + "/large")

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


class SpecimenRegistry(unittest.TestCase):
    """Ruling 4: every specimen folder is recorded in the Theodore home; the sweeps work from the record and from the
    current roots; a folder is never put under the working folder, whatever it contains."""

    @contextlib.contextmanager
    def world(self):
        import shutil
        import tempfile
        from theodore_client import specimens
        base = Path(tempfile.mkdtemp(prefix="theodore-r7-")).resolve()
        elsewhere = Path(tempfile.mkdtemp(prefix="theodore-r7-cache-")).resolve()      # not under the working folder's parent
        dirs = {name: base / name for name in ("tmp-a", "tmp-b", "work", "user", "local", "local/Temp")}
        dirs["cache"] = elsewhere / "cache"
        for path in dirs.values():
            path.mkdir()
        before = os.getcwd()
        os.chdir(dirs["work"])
        cache = dirs["cache"] / "specimens"
        try:
            with client_home() as home, mock.patch.object(specimens, "cache_root", return_value=cache), \
                    mock.patch.object(tempfile, "tempdir", str(dirs["tmp-a"])), mock.patch.object(Path, "home", return_value=dirs["user"]), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": str(dirs["local"])}):      # Windows' second temporary root
                core.home().ensure()
                yield specimens, home, dirs
        finally:
            os.chdir(before)
            shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(elsewhere, ignore_errors=True)

    def age(self, specimens, folder: Path, seconds: float) -> None:
        lines = []
        for made, path in specimens._read_registry():
            lines.append((made - seconds if path == folder else made, path))
        specimens._write_registry(lines)
        os.utime(folder, (time.time() - seconds, time.time() - seconds))

    def test_every_folder_is_recorded_one_line_each(self):
        with self.world() as (specimens, home, dirs):
            one, two = specimens.make_folder(), specimens.make_folder()
            lines = (home / "state" / "specimens.txt").read_text().splitlines()
            self.assertEqual(lines.pop(0), "#v2", "the encoded format says so on its first line")
            self.assertEqual(len(lines), 2)
            for line, folder in zip(lines, (one, two)):
                made, path = line.split(" ", 1)
                self.assertTrue(made.isdigit())
                self.assertEqual(Path(path), folder)
                self.assertEqual(folder.parent, dirs["tmp-a"])

    def test_a_changed_temporary_folder_cannot_strand_a_folder(self):
        import tempfile
        with self.world() as (specimens, home, dirs):
            old, fresh = specimens.make_folder(), specimens.make_folder()
            self.age(specimens, old, 3601)
            with mock.patch.object(tempfile, "tempdir", str(dirs["tmp-b"])):
                self.assertEqual(specimens.sweep(), [])
                self.assertFalse(old.exists(), "older than an hour: removed although TMPDIR changed")
                self.assertTrue(fresh.exists())
                self.assertEqual(specimens.sweep(everything=True), [])
                self.assertFalse(fresh.exists(), "uninstall removes every recorded folder")
            self.assertFalse((home / "state" / "specimens.txt").exists())

    def test_the_record_never_deletes_what_is_not_a_specimen_folder(self):
        with self.world() as (specimens, home, dirs):
            precious = dirs["user"] / "Documents"
            precious.mkdir()
            (precious / "keep.txt").write_text("keep")
            link = dirs["tmp-b"] / "theodore-specimens-link"
            try:
                os.symlink(precious, link, target_is_directory=True)
            except OSError:             # Windows without the privilege to make links: the plain case still runs
                link = None
            (home / "state" / "specimens.txt").write_text(f"0 {precious}\n" + (f"0 {link}\n" if link else ""))
            self.assertEqual(specimens.sweep(everything=True), [])
            self.assertTrue((precious / "keep.txt").exists())
            if link:
                self.assertTrue(os.path.lexists(link))

    @unittest.skipIf(os.name == "nt", "Windows paths cannot hold a line feed")
    def test_a_path_with_a_line_feed_is_recorded_and_found_again(self):
        import tempfile
        with self.world() as (specimens, home, dirs):
            odd = dirs["tmp-a"].parent / "tmp\nwith a line feed %0A"
            odd.mkdir()
            with mock.patch.object(tempfile, "tempdir", str(odd)):
                folder = specimens.make_folder()
            self.assertEqual(folder.parent, odd)
            self.assertEqual([path for _made, path in specimens._read_registry()], [folder])
            self.age(specimens, folder, 3601)
            self.assertEqual(specimens.sweep(), [])          # TMPDIR is back to tmp-a: only the record finds it
            self.assertFalse(folder.exists())

    def test_the_round_7_record_format_is_still_read(self):
        with self.world() as (specimens, home, dirs):
            folder = dirs["tmp-b"] / "theodore-specimens-0123456789abcdef"
            folder.mkdir()
            (home / "state" / "specimens.txt").write_text(f"1700000000 {folder}\n")
            self.assertEqual(specimens._read_registry(), [(1700000000.0, folder)])
            self.assertEqual(specimens.sweep(), [])
            self.assertFalse(folder.exists())

    def test_never_under_the_working_folder_even_when_it_holds_the_temporary_folder_or_the_home(self):
        with self.world() as (specimens, home, dirs):
            base = dirs["work"].parent
            os.chdir(base)                      # the working folder holds the temporary folder and the user's home
            folder = specimens.make_folder()
            self.assertEqual(folder.parent, dirs["cache"] / "specimens", "the cache root is used instead")
            self.assertTrue(os.path.lexists(folder))
            os.chdir(os.path.commonpath([dirs["work"], dirs["cache"]]))     # ...and now the cache root too: refused, nothing made
            before = (home / "state" / "specimens.txt").read_text()
            with self.assertRaises(core.Status) as raised:
                specimens.make_folder()
            self.assertEqual(raised.exception.name, "BLOCKED_BY_POLICY")
            self.assertIn("working folder", raised.exception.summary)
            self.assertEqual((home / "state" / "specimens.txt").read_text(), before)
            self.assertEqual([p.name for p in (dirs["cache"] / "specimens").iterdir()], [folder.name])


class Round8Keychain(unittest.TestCase):
    def test_a_repeated_deletion_never_drops_the_tombstone_while_the_item_may_exist(self):
        # An upgraded installation: an old keychain item, a fallback file, no keychain-holds marker. The deletion after
        # a revocation times out; a second deletion (the keychain now left alone) must keep the tombstone.
        import io
        import subprocess
        from theodore_client import credentials
        keychain = {"item": "c2VjcmV0"}
        clock = {"offset": 0.0}

        def security(command, **kwargs):
            if command[1] == "delete-generic-password":
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            if command[1] == "find-generic-password":
                return subprocess.CompletedProcess(command, 0, '{"installation_id":"old","installation_secret":"old-secret"}', "")
            raise AssertionError(command)
        real_now = core.now
        with client_home() as home, mock.patch.dict(os.environ, {"THEODORE_CREDENTIAL_STORE": "keychain"}), \
                mock.patch.object(subprocess, "run", side_effect=security), mock.patch("sys.stderr", new=io.StringIO()), \
                mock.patch.object(core, "now", side_effect=lambda: real_now() + clock["offset"]):
            core.home().ensure()
            (home / "state" / "credentials").write_text('{"installation_id":"new","installation_secret":"file-secret"}')
            self.assertFalse(credentials.delete())
            self.assertTrue((home / "state" / "keychain-revoked").exists())
            self.assertFalse(credentials.delete(), "still not confirmed")
            self.assertTrue((home / "state" / "keychain-revoked").exists(), "the tombstone stays while the item may exist")
            clock["offset"] += 86401
            self.assertIsNone(credentials.load(), "the old keychain credential never loads again")


class Round8Environment(unittest.TestCase):
    """Round 8 (review 6): claiming a folder is one exclusive step; a credential belongs to one service."""

    def test_of_many_builds_racing_on_an_empty_folder_exactly_one_service_wins(self):
        with client_home() as home:
            (home / "bin" / "launcher.json").unlink()          # an empty folder: no record, no stable copy
            (home / "state").mkdir()
            outcomes = {}
            start = threading.Barrier(8)

            def build(name, base):          # what check_environment does with its own stamp: claim, then read back
                start.wait(10)
                core._claim_environment(base)
                outcomes[name] = ("ok" if core.home_environment() == base else "USAGE", base)
            threads = [threading.Thread(target=build, args=(f"b{i}", f"https://api.{'staging' if i % 2 else 'prod'}.example.test")) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            winner = (home / "state" / "environment").read_text().strip()
            for name, (result, base) in outcomes.items():
                self.assertEqual(result, "ok" if base == winner else "USAGE", (name, base, winner))
            self.assertEqual(len(outcomes), 8)

    def test_a_credential_of_another_service_is_never_loaded(self):
        import subprocess
        from theodore_client import credentials
        items = {}

        def security(command, **kwargs):
            if command[1] == "-i":
                account = kwargs["input"].split('-a "', 1)[1].split('"', 1)[0]
                items[account] = kwargs["input"].split('-w "', 1)[1].split('"', 1)[0]
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[1] == "find-generic-password":
                account = command[command.index("-a") + 1]
                return subprocess.CompletedProcess(command, 0 if account in items else 44, items.get(account, "") + "\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")
        for store in ("file", "keychain"):
            with self.subTest(store=store), client_home() as home, mock.patch.dict(os.environ, {"THEODORE_CREDENTIAL_STORE": store}), \
                    mock.patch.object(subprocess, "run", side_effect=security):
                core.home().ensure()
                credentials.store("inst-prod", "secret-prod")
                self.assertEqual(credentials.load()["installation_secret"], "secret-prod")
                with mock.patch.object(core, "running_stamp", return_value={"api_base": "https://api.staging.example.test"}):
                    self.assertIsNone(credentials.load(), "a staging build never loads the production credential")


class Round8Refusals(unittest.TestCase):
    """Round 8, rulings 10 and 14: secrets never reach a printed line or the log; the central refusal handler cannot raise."""

    def setUp(self):
        core._REFUSAL, core._REFUSAL_EMITTED, account._REFUSAL_STATUS = None, False, None
        self.addCleanup(lambda: setattr(core, "_REFUSAL", None))
        self.addCleanup(lambda: setattr(core, "_REFUSAL_EMITTED", False))
        self.addCleanup(lambda: setattr(account, "_REFUSAL_STATUS", None))

    def test_the_handler_answers_the_fixed_block_when_starting_a_sign_in_fails(self):
        import io
        from theodore_client import cli, commands
        with client_home():
            core.home().ensure()
            core.mark_refused("revoked", "installation_revoked")
            out = io.StringIO()

            def broken(*a, **k):
                raise api.TooLarge(1 << 20)
            with mock.patch.object(api, "call_json", side_effect=broken), mock.patch.object(commands, "version", side_effect=broken), \
                    mock.patch("sys.stdout", new=out):
                code = cli.main(["version"])
            self.assertEqual(code, 65)
            lines = [l for l in out.getvalue().splitlines() if l.strip()]
            self.assertEqual(len(lines), 3, out.getvalue())
            self.assertTrue(lines[0].startswith("THEODORE SIGN_IN_REQUIRED: "), lines[0])
            self.assertIn("login --start", lines[1])

    def test_a_suspension_is_answered_centrally_too(self):
        import io
        with client_home():
            core.mark_refused("suspended", "account_suspended")
            out = io.StringIO()
            with mock.patch("sys.stdout", new=out):
                self.assertEqual(core.emit(core.Status("OK", "fine", "go on", "ok")), 70)
                self.assertEqual(core.emit(core.Status("OK", "again", "go on", "ok")), 70)
            self.assertEqual(out.getvalue().count("THEODORE NO_ENTITLEMENT"), 1, "once")

    def test_server_diagnostics_are_scrubbed_before_log_or_print(self):
        import io
        from theodore_client import commands, credentials, install
        secret = "sec-" + "a1" * 20
        with client_home() as home:
            core.home().ensure()
            credentials.store("inst-1", secret)
            echo = api.ApiError(500, "internal_error", f"lookup failed for installation_secret={secret} and Bearer {secret}")
            with mock.patch.object(install, "install_runtime", return_value="rt"), \
                    mock.patch.object(install, "fetch_channel_manifest", side_effect=echo):
                self.assertEqual(commands.setup_job(), 0)
            log = (home / "logs" / "client.log").read_text()
            self.assertIn("setup: engine not installed yet", log)
            self.assertNotIn(secret, log)
            self.assertNotIn("a1a1a1a1", log)
            printed = io.StringIO()
            refused = api.ApiError(400, "invalid_grant", "code WXYZ-9876 is not valid")         # the value login_code registered
            with mock.patch.object(api, "call_json", side_effect=api.ApiError(422, "validation_failed", "bad code=WXYZ-9876 for installation_secret: " + secret)), \
                    mock.patch("sys.stdout", new=printed):
                core.emit(account.login_code("WXYZ-9876"))
                core.emit(api.status_from(refused, "activation"))
            self.assertNotIn("WXYZ-9876", printed.getvalue())
            self.assertNotIn(secret, printed.getvalue())
            self.assertIn("[redacted]", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
