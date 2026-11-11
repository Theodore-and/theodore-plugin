"""Hardening, in process where the behaviour is a pure function of the client's state: signing keys,
credentials leaving their origin, redirects, pack metadata, persisted restrictions, credential storage."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import CLIENT_DIR, ROOT  # noqa: E402,F401
from fake_api import b64u, canonical_json, public_key, sign  # noqa: E402

from theodore_client import account, api, core, ed25519  # noqa: E402

SEED = hashlib.sha256(b"hardening test key").digest()
ZERO_X = "A" * 43


@contextlib.contextmanager
def client_home(**config):
    """A scratch THEODORE_HOME with a launcher.json in bin/, and the client's caches pointed at it."""
    tmp = Path(tempfile.mkdtemp(prefix="theodore-hardening-"))
    base = {"api_base": "https://api.example.test", "dl_base": "https://dl.example.test",
            "manifest_key_1_kid": "k1", "manifest_key_1_x": b64u(public_key(SEED))}
    base.update(config)
    (tmp / "bin").mkdir()
    (tmp / "bin" / "launcher.json").write_text(json.dumps(base))
    saved = (core._HOME, core._CONFIG)
    core._HOME, core._CONFIG = core.Home(tmp), None
    try:
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(tmp), "THEODORE_CREDENTIAL_STORE": "file"}):
            yield tmp
    finally:
        core._HOME, core._CONFIG = saved
        shutil.rmtree(tmp, ignore_errors=True)


def envelope(manifest: dict, seed: bytes = SEED, kid: str = "k1") -> dict:
    text = canonical_json(manifest)
    return {"manifest": text, "signature": b64u(sign(seed, text.encode("utf-8"))), "kid": kid}


def small_order_forgery() -> tuple[bytes, bytes]:
    """A message and a 'signature' that the all-zero public key accepts unless small-order keys are refused:
    R is the identity, s = 0, and the message is chosen so that h is a multiple of the key's order (4)."""
    zero = b"\x00" * 32
    identity = (1).to_bytes(32, "little")
    for nonce in range(1000):
        message = canonical_json({"schema": "x", "nonce": nonce}).encode()
        h = int.from_bytes(hashlib.sha512(identity + zero + message).digest(), "little") % ed25519.L
        if h % 8 == 0:
            return message, identity + b"\x00" * 32
    raise AssertionError("no forgery found")


class PlaceholderKeys(unittest.TestCase):
    def test_small_order_public_keys_are_refused_by_both_verifiers(self):
        message, signature = small_order_forgery()
        zero = b"\x00" * 32
        # The arithmetic without the guard accepts it: this is the forgery the review found.
        A, R = ed25519.point_decompress(zero), ed25519.point_decompress(signature[:32])
        h = int.from_bytes(hashlib.sha512(signature[:32] + zero + message).digest(), "little") % ed25519.L
        self.assertTrue(ed25519.point_equal(ed25519.point_mul(0, ed25519.G), ed25519.point_add(R, ed25519.point_mul(h, A))))
        self.assertFalse(ed25519.verify(zero, message, signature))
        with mock.patch.dict(sys.modules, {"cryptography": None, "cryptography.exceptions": None,
                                           "cryptography.hazmat.primitives.asymmetric.ed25519": None}):
            self.assertFalse(ed25519.verify(zero, message, signature), "the pure-Python verifier refuses it too")

    def test_a_launcher_json_with_the_placeholder_key_refuses_every_manifest(self):
        message, signature = small_order_forgery()
        forged = {"manifest": message.decode(), "signature": b64u(signature), "kid": "manifest-unset"}
        for config in ({"manifest_key_1_kid": "manifest-unset", "manifest_key_1_x": ZERO_X},
                       {"manifest_key_1_kid": "manifest-unset", "manifest_key_1_x": b64u(public_key(SEED))},
                       {"manifest_key_1_kid": "anything", "manifest_key_1_x": ZERO_X}):
            with client_home(**config):
                self.assertEqual(core.manifest_keys(), {})
                for candidate in (forged, dict(forged, kid=config["manifest_key_1_kid"]),
                                  envelope({"schema": "x"}, kid=config["manifest_key_1_kid"])):
                    with self.assertRaises(core.Status) as caught:
                        account.verify_signed(candidate, "release manifest")
                    self.assertEqual(caught.exception.name, "INTERNAL")
                    self.assertIn("no release signing key", caught.exception.summary)

    def test_a_real_key_beside_a_placeholder_still_works(self):
        with client_home(manifest_key_2_kid="manifest-unset", manifest_key_2_x=ZERO_X):
            self.assertEqual(account.verify_signed(envelope({"schema": "x"}), "test"), {"schema": "x"})
            self.assertEqual(sorted(core.manifest_keys()), ["k1"])


class CredentialBoundary(unittest.TestCase):
    """The installation bearer goes to the configured API origin over HTTPS and nowhere else; no redirect leaves the
    origin it started on or drops to plain HTTP."""

    def setUp(self):
        from fake_api import FakeApi
        self.api = FakeApi().start()
        self.addCleanup(self.api.stop)
        self.api.files["thing.bin"] = b"payload"
        self.sha = hashlib.sha256(b"payload").hexdigest()

    def test_only_the_api_origin_over_https_may_carry_the_bearer(self):
        with client_home(api_base="https://api.example.test"):
            self.assertTrue(api.may_carry_bearer("https://api.example.test/v1/packs/icons/1.0.0"))
            for url in ("http://api.example.test/v1/x", "https://api.example.test:8443/v1/x", "https://dl.example.test/x",
                        "https://attacker.example/v1/x", "https://api.example.test.attacker.example/v1/x", "ftp://api.example.test/x"):
                self.assertFalse(api.may_carry_bearer(url), url)
        with client_home(api_base="http://api.example.test"):
            self.assertFalse(api.may_carry_bearer("http://api.example.test/v1/x"), "plain HTTP never carries it, loopback aside")
        with client_home(api_base=self.api.base):
            self.assertTrue(api.may_carry_bearer(self.api.base + "/v1/x"), "the tests' loopback API over HTTP")
            self.assertFalse(api.may_carry_bearer(self.api.base.replace("127.0.0.1", "localhost") + "/v1/x"))

    def test_a_download_from_another_origin_gets_no_bearer(self):
        with client_home(api_base=self.api.base) as home:
            other = self.api.base.replace("127.0.0.1", "localhost") + "/dl/thing.bin"
            api.download(other, home / "out.bin", sha256=self.sha, bearer="secret-value")
            api.download(self.api.base + "/dl/thing.bin", home / "out2.bin", sha256=self.sha, bearer="secret-value")
        self.assertEqual([r["auth"] for r in self.api.requests if r["path"] == "/dl/thing.bin"], [False, True])

    def test_a_redirect_to_another_host_is_refused_and_one_on_the_same_origin_is_followed(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = []
        target = {"location": ""}

        class Redirector(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.path, bool(self.headers.get("Authorization"))))
                if self.path == "/same":
                    self.send_response(302)
                    self.send_header("Location", "/dl")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/dl":
                    self.send_response(200)
                    self.send_header("Content-Length", "7")
                    self.end_headers()
                    self.wfile.write(b"payload")
                else:
                    self.send_response(302)
                    self.send_header("Location", target["location"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with client_home(api_base=base) as home:
            api.download(base + "/same", home / "a.bin", sha256=self.sha, bearer="secret-value")
            self.assertEqual(seen, [("/same", True), ("/dl", True)])
            for elsewhere in (self.api.base + "/dl/thing.bin", base.replace("127.0.0.1", "localhost") + "/dl"):
                target["location"] = elsewhere
                with self.assertRaises(api.NetworkError) as caught:
                    api.download(base + "/elsewhere", home / "b.bin", sha256=self.sha, bearer="secret-value")
                self.assertIn("redirect", caught.exception.detail)
                with self.assertRaises(api.NetworkError):
                    api.request("GET", base + "/elsewhere", bearer="secret-value")
        self.assertEqual([r for r in self.api.requests if r["path"] == "/dl/thing.bin"], [], "the other host was never contacted")

    def test_a_redirect_from_https_to_http_is_refused(self):
        handler = api._Redirects()
        request = __import__("urllib.request").request.Request("https://api.example.test/v1/x", headers={"Authorization": "Bearer s"})
        with self.assertRaises(Exception) as caught:
            handler.redirect_request(request, None, 302, "Found", {}, "http://api.example.test/v1/x")
        self.assertIn("redirect", str(caught.exception))


class CredentialStorage(unittest.TestCase):
    def test_a_credential_saved_while_the_keychain_is_locked_wins_over_the_old_keychain_item(self):
        from theodore_client import credentials
        keychain = {"item": json.dumps({"installation_id": "old", "installation_secret": "old-secret"})}
        locked = {"value": True}

        def store(secret):
            if locked["value"]:
                return False
            keychain["item"] = secret
            return True

        with client_home(), mock.patch.dict(os.environ, {"THEODORE_CREDENTIAL_STORE": "keychain"}), \
                mock.patch.object(credentials, "_keychain_store", store), \
                mock.patch.object(credentials, "_keychain_load", lambda: keychain.get("item")), \
                mock.patch.object(credentials, "_keychain_delete", lambda: locked["value"] or keychain.pop("item", None)):
            core.home().ensure()
            self.assertEqual(credentials.store("new", "new-secret"), "file")
            self.assertEqual(credentials.load()["installation_id"], "new", "the newer credential, not the stale keychain item")
            locked["value"] = False                                     # the keychain is unlocked later
            self.assertEqual(credentials.load()["installation_id"], "new")
            self.assertEqual(credentials.store("newer", "newer-secret"), "keychain")
            self.assertFalse((core.home().state / "credentials").exists())
            self.assertEqual(credentials.load()["installation_id"], "newer")

    def test_a_pending_activation_is_kept_until_the_credential_is_stored(self):
        from theodore_client import credentials
        with client_home() as home:
            core.home().ensure()
            pending = home / "state" / "activation.pending"
            bundle = {"installation_id": "i-1", "installation_secret": "s-1", "entitlement_token": "a.b.c"}
            pending.write_text(json.dumps(bundle))
            with mock.patch.object(credentials, "store", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(OSError):
                    account.import_pending_activation()
            self.assertTrue(pending.exists(), "the only copy of the installation secret survives a full disk")
            account.import_pending_activation()
            self.assertFalse(pending.exists())
            self.assertEqual(credentials.load()["installation_id"], "i-1")


class Restrictions(unittest.TestCase):
    def test_sub_second_order_and_equal_instants(self):
        from theodore_client import install
        base = {"schema": "x", "engine": {"version": "1.0.0", "revoked": []}, "shim_min": None, "min_version": None}
        with client_home():
            core.home().ensure()
            install.save_restrictions({**base, "kill": True, "kill_message": "stop", "server_time": "2026-11-10T12:00:00.900Z"})
            install.save_restrictions({**base, "kill": False, "server_time": "2026-11-10T12:00:00.100Z"})     # older, same second
            self.assertTrue(install.update_state()["restrictions"]["kill"])
            install.save_restrictions({**base, "kill": False, "min_version": "2.0.0", "server_time": "2026-11-10T12:00:00.900Z"})
            rules = install.update_state()["restrictions"]
            self.assertTrue(rules["kill"], "the same instant cannot lift the kill switch")
            self.assertEqual(rules["min_version"], "2.0.0", "and the stricter minimum holds")
            install.save_restrictions({**base, "kill": False, "server_time": "2026-11-10T12:00:01.000Z"})
            self.assertFalse(install.update_state()["restrictions"]["kill"], "a newer document lifts it")


class Round5(unittest.TestCase):
    def test_server_time_is_utc_whatever_the_local_time_zone(self):
        # In a fresh interpreter per zone: TZ is read at start, and not every Python has time.tzset.
        import subprocess
        seen = []
        for zone in ("UTC", "America/New_York", "Asia/Dubai"):
            done = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from theodore_client import install; "
                                   "print(repr(install._manifest_time('2026-07-01T12:00:00.250Z')))", str(CLIENT_DIR)],
                                  env={**os.environ, "TZ": zone}, capture_output=True, text=True, timeout=60)
            seen.append(float(done.stdout.strip() or "nan"))
        self.assertEqual(seen, [1782907200.25] * 3)

    def test_semantic_version_precedence(self):
        from theodore_client.core import version_key
        ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                   "1.0.0-rc.1", "1.0.0", "1.0.1-rc.1", "1.0.1", "2.0.0"]
        self.assertEqual(sorted(reversed(ordered), key=version_key), ordered)
        self.assertEqual(version_key("1.0.1+build.5"), version_key("1.0.1"), "build metadata is ignored")
        self.assertGreater(version_key("1.0.1+build"), version_key("1.0.0"))
        self.assertLess(version_key("1.0.0-x-y"), version_key("1.0.0-x-z"), "a hyphen inside an identifier is part of it")

    def test_a_cached_on_switch_is_good_for_an_hour(self):
        from theodore_client import account
        with client_home() as home:
            core.home().ensure()
            account.save_token("a.e30.c", auto_reports=True)
            record = json.loads((home / "state" / "entitlement.json").read_text())
            self.assertEqual(account.automatic_reports()[1], "plan")      # no agent-reports in an empty token
            token = "x." + b64u(json.dumps({"feat": ["agent-reports"], "exp": 9999999999}).encode()) + ".y"
            account.save_token(token, auto_reports=True)
            self.assertEqual(account.automatic_reports(), (True, ""))
            record = json.loads((home / "state" / "entitlement.json").read_text())
            record["auto_reports"]["at"] -= 3601
            (home / "state" / "entitlement.json").write_text(json.dumps(record))
            self.assertEqual(account.automatic_reports(), (False, "unknown"), "an hour-old 'on' must be asked again")


def _iso(epoch: float) -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".000Z"


class Round6(unittest.TestCase):
    """Activation decided under one lock against the newest accepted document and the floor; a cache 'from the future'."""

    def engine_on_disk(self, version: str) -> dict:
        from theodore_client import install
        entry = {"version": version, "sha256": hashlib.sha256(version.encode()).hexdigest(), "kind": "engine-source"}
        folder = core.home().engines / version
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".theodore-engine.json").write_text(json.dumps(entry))
        return entry

    def manifest(self, issued: float, entry: dict) -> dict:
        return {"server_time": _iso(issued), "engine": entry}

    def check(self, fetch):
        from theodore_client import install
        with mock.patch.object(install, "fetch_channel_manifest", side_effect=lambda timeout=0: fetch()), \
                mock.patch.object(install, "engine_artifact", side_effect=lambda m: m["engine"]), \
                mock.patch.object(install, "runtime_ready", return_value=True), \
                mock.patch.object(install, "start_packs_job", return_value=False), \
                mock.patch.object(install, "collect_garbage", return_value=[]), \
                mock.patch.object(install.Job, "spawn", side_effect=AssertionError("no download is needed here")):
            return install.check_updates(force=True, wait=0)

    def current(self, version: str) -> None:
        core.write_json(core.home().engines / "current.json", {"version": version})

    def test_two_checks_at_once_never_activate_the_older_document_last(self):
        from theodore_client import install
        with client_home():
            core.home().ensure()
            two, three = self.engine_on_disk("2.0.0"), self.engine_on_disk("3.0.0")
            self.engine_on_disk("1.9.0")
            self.current("1.9.0")                   # both checks see an update to make
            now = core.now()
            older, newer = self.manifest(now - 60, two), self.manifest(now - 1, three)
            activated = []
            real = install.activate_engine
            def recording(version):
                activated.append(version)
                return real(version)

            def fetch_b():
                install.accept_fresh("channel", newer, "release manifest")
                return newer

            def fetch_a():
                # check A accepts the 12:00 document; before it activates, check B accepts and applies 12:01
                install.accept_fresh("channel", older, "release manifest")
                self.check(fetch_b)
                return older

            with mock.patch.object(install, "activate_engine", side_effect=recording):
                self.check(fetch_a)
            self.assertEqual(install.current_engine(), "3.0.0", f"activations in order: {activated}")
            self.assertNotEqual(activated[-1:], ["2.0.0"])

    def test_an_older_check_may_activate_only_the_engine_the_newest_document_chose(self):
        # Setup and a guide fetch one after the other: the first to finish its download may hold the older document.
        # It activates the engine only when the newest document names that same engine; any other engine is refused.
        from theodore_client import install
        with client_home(), mock.patch.object(install, "collect_garbage", return_value=[]):
            core.home().ensure()
            two, three = self.engine_on_disk("2.0.0"), self.engine_on_disk("3.0.0")
            now = core.now()
            install.accept_fresh("channel", self.manifest(now - 60, three), "release manifest")
            install.accept_fresh("channel", self.manifest(now - 1, three), "release manifest")
            self.assertFalse(install.activate_checked("2.0.0", now - 60, "test"))
            self.assertIsNone(install.current_engine())
            self.assertTrue(install.activate_checked("3.0.0", now - 60, "test"), "the same engine as the newest document")
            self.assertEqual(install.current_engine(), "3.0.0")
            install.accept_fresh("channel", self.manifest(now, two), "release manifest")       # a newer document rolls back
            self.assertFalse(install.activate_checked("3.0.0", now - 1, "test"))
            self.assertTrue(install.activate_checked("2.0.0", now, "test"))
            self.assertEqual(install.current_engine(), "2.0.0")

    def test_the_comparison_and_the_activation_happen_under_one_lock(self):
        # While another process holds the activate lock, a check can neither compare nor activate; a newer document
        # accepted in that time is what it compares against once the lock is free.
        import threading
        from theodore_client import install
        with client_home():
            core.home().ensure()
            two, three = self.engine_on_disk("2.0.0"), self.engine_on_disk("3.0.0")
            now = core.now()
            install.accept_fresh("channel", self.manifest(now - 60, two), "release manifest")
            answer = []
            worker = threading.Thread(target=lambda: answer.append(install.activate_checked("2.0.0", now - 60, "test")))
            with core.lock(install.UPDATE_LOCK, timeout=10):
                worker.start()
                worker.join(1.0)
                self.assertTrue(worker.is_alive(), "the check must wait for the activate lock")
                self.assertIsNone(install.current_engine())
                install.accept_fresh("channel", self.manifest(now - 1, three), "release manifest")
            worker.join(30)
            self.assertEqual(answer, [False])
            self.assertIsNone(install.current_engine())

    def test_a_pending_engine_below_the_floor_is_never_activated_after_a_reset(self):
        from theodore_client import install
        with client_home():
            core.home().ensure()
            one, two = self.engine_on_disk("1.0.0"), self.engine_on_disk("2.0.0")
            self.current("2.0.0")
            install.save_update_state(pending={"version": "1.0.0", "sha256": one["sha256"]})
            install.reset_update_state("test")
            self.assertFalse(install.update_state().get("pending"), "a reset drops what was waiting to be activated")
            install.save_update_state(pending={"version": "1.0.0", "sha256": one["sha256"]})     # an older client's leftover
            before = float(install.update_state()["floor"]["since"]) - 60
            self.check(lambda: install.accept_fresh("channel", self.manifest(before, one), "release manifest") or self.manifest(before, one))
            self.assertEqual(install.current_engine(), "2.0.0")
            self.assertFalse(install.update_state().get("pending"))

    def test_a_cached_on_switch_from_the_future_is_not_trusted(self):
        with client_home() as home:
            core.home().ensure()
            token = "x." + b64u(json.dumps({"feat": ["agent-reports"], "exp": 9999999999}).encode()) + ".y"
            account.save_token(token, auto_reports=True)
            record = json.loads((home / "state" / "entitlement.json").read_text())
            record["auto_reports"]["at"] += 86400          # cached while the clock was a day fast, then corrected
            (home / "state" / "entitlement.json").write_text(json.dumps(record))
            self.assertEqual(account.automatic_reports(), (False, "unknown"))


class Specimens(unittest.TestCase):
    """Deck-design specimen sheets: fetched on demand into a private temporary folder, never into Theodore's folder.
    In process, so it also runs on Windows (the client suite against the fake server runs on macOS and Linux)."""

    JPEG = b"\xff\xd8\xff\xe0 sheet \xff\xd9"

    @contextlib.contextmanager
    def temp(self):
        from theodore_client import specimens
        root = Path(tempfile.mkdtemp(prefix="theodore-hardening-tmp-")).resolve()
        work = Path(tempfile.mkdtemp(prefix="theodore-hardening-cwd-")).resolve()
        before = os.getcwd()
        os.chdir(work)          # the runner's temporary folder may lie inside its working folder, which is never used
        try:
            with mock.patch.object(specimens, "roots", return_value=[root]):
                yield root
        finally:
            os.chdir(before)
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)

    def answer(self, body: bytes, sha: str | None = None, kind: str = "image/jpeg"):
        import email.message
        headers = email.message.Message()
        headers["Content-Type"] = kind
        headers["X-Theodore-Sha256"] = sha or hashlib.sha256(body).hexdigest()
        return 200, headers, body

    def test_the_folder_and_the_files_are_the_current_users_alone(self):
        from theodore_client import commands, specimens
        with client_home() as home, self.temp() as root:
            core.home().ensure()
            calls = []

            def fake(route, what, **kwargs):
                calls.append((route, kwargs))
                return self.answer(self.JPEG)
            out = []
            with mock.patch.object(account, "require", return_value={"plan": "beta"}), mock.patch.object(commands, "_api", side_effect=fake), \
                    mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(a[0])):
                self.assertEqual(commands.specimen(["cover-01.jpg", "cover-01.jpg", "b.jpg"]), 0)
            self.assertEqual([c[0] for c in calls], ["guide_specimen", "guide_specimen"], "a name given twice is fetched once")
            for _route, kwargs in calls:
                self.assertEqual((kwargs["query"], kwargs["redirects"], kwargs["limit"]), ({"skill": "deck-design"}, False, 1 << 20))
            self.assertEqual(len(out), 3)
            self.assertEqual(out[0], out[1])
            self.assertNotIn("\\", out[0], "forward slashes on every system")
            path = Path(out[0])
            self.assertTrue(path.is_absolute() and path.read_bytes() == self.JPEG)
            self.assertEqual(path.parent.parent, root)
            self.assertTrue(path.parent.name.startswith("theodore-specimens-"))
            self.assertNotIn(home.resolve(), path.resolve().parents)
            if sys.platform == "win32":
                import subprocess
                for target in (path.parent, path):
                    acl = subprocess.run(["icacls", str(target)], capture_output=True, text=True).stdout
                    for other in ("BUILTIN\\Users", "Everyone", "Authenticated Users", "BUILTIN\\Administrators", "NT AUTHORITY\\SYSTEM"):
                        self.assertNotIn(other, acl, acl)
                    self.assertEqual(len([line for line in acl.splitlines() if ":(" in line]), 1, acl)
                self.assertNotIn("(I)", subprocess.run(["icacls", str(path.parent)], capture_output=True, text=True).stdout)
            else:
                import stat
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(specimens.sweep(everything=True), [])
            self.assertFalse(path.parent.exists(), "uninstall removes them all")

    def test_a_folder_that_cannot_be_made_private_is_not_used(self):
        from theodore_client import specimens
        with client_home(), self.temp() as root:
            core.home().ensure()
            with mock.patch.object(specimens, "_make_private", side_effect=OSError("icacls failed")):
                with self.assertRaises(core.Status) as raised:
                    specimens.make_folder()
            self.assertEqual(raised.exception.name, "INTERNAL")
            self.assertEqual(list(root.iterdir()), [])

    def test_a_refused_sheet_leaves_nothing(self):
        from theodore_client import commands
        for answer, status in ((lambda: self.answer(self.JPEG, sha="1" * 64), "RETRY"), (lambda: self.answer(self.JPEG, kind="text/html"), "INTERNAL"),
                               (lambda: (_ for _ in ()).throw(api.TooLarge(1 << 20)), "INTERNAL")):
            with client_home(), self.temp() as root:
                core.home().ensure()
                with mock.patch.object(account, "require", return_value={}), mock.patch.object(commands, "_api", side_effect=lambda *a, **k: answer()):
                    with self.assertRaises(core.Status) as raised:
                        commands.specimen(["a.jpg"])
                self.assertEqual(raised.exception.name, status)
                self.assertEqual(list(root.iterdir()), [])

    def test_a_response_is_read_up_to_its_limit_and_no_further(self):
        import io

        class Response(io.BytesIO):
            status, headers = 200, {}
            asked = []

            def read(self, n=-1):
                self.asked.append(n)
                return super().read(n)

        class Opener:
            def __init__(self, body, headers):
                self.body, self.headers = body, headers

            def open(self, req, timeout=0):
                response = Response(self.body)
                response.headers = self.headers
                return response
        with client_home():
            for body, headers, fits in ((b"x" * 100, {}, True), (b"x" * 101, {}, False), (b"", {"Content-Length": "101"}, False)):
                Response.asked = []
                with mock.patch.object(api, "_opener", return_value=Opener(body, headers)):
                    if fits:
                        self.assertEqual(api.request("GET", "https://api.example.test/v1/x", limit=100)[2], body)
                    else:
                        with self.assertRaises(api.TooLarge):
                            api.request("GET", "https://api.example.test/v1/x", limit=100)
                self.assertTrue(all(0 <= n <= 101 for n in Response.asked), Response.asked)

    def test_names(self):
        from theodore_client import specimens
        for good in ("cover-01.jpg", "Grid_2.b.jpg", "x" * 80 + ".jpg", "..jpg"):
            self.assertTrue(specimens.valid_name(good), good)
        for bad in ("", ".jpg", "a.png", "a.JPG", "../a.jpg", "a/b.jpg", "a\\b.jpg", "a b.jpg", "x" * 81 + ".jpg", "a.jpg\n", "NUL.jpg", "con.jpg",
                    "COM1.a.jpg", "lpt9.jpg", "a:b.jpg", "é.jpg", "a.jpg.exe"):
            self.assertFalse(specimens.valid_name(bad), bad)

    def test_guide_links_name_the_file_and_one_direction_follows(self):
        from theodore_client import specimens
        untouched = ("[up](specimens/../x.jpg) [sub](specimens/a/b.jpg) [png](specimens/a.png) [abs](/specimens/a.jpg) "
                     "[web](https://x.test/specimens/a.jpg) [long](specimens/" + "x" * 81 + ".jpg) [sp](specimens/a b.jpg) specimens/a.jpg\n")
        text = "# Covers\n![Cover](specimens/cover-01.jpg) [grid](specimens/Grid_2.b.jpg)\n" + untouched
        out = specimens.guide_links(text, '"/x y/theodore"')
        self.assertTrue(out.startswith("# Covers\n![Cover](cover-01.jpg) [grid](Grid_2.b.jpg)\n" + untouched))
        tail = out[out.index(untouched) + len(untouched):]
        self.assertEqual(tail.count('"/x y/theodore" specimen <file>'), 1)
        for word in ("open the path it prints", "only the sheets", "Do not copy"):
            self.assertIn(word, tail)
        self.assertEqual(specimens.guide_links(untouched, "theodore"), untouched, "no sheet link: nothing is added")
        self.assertTrue(specimens.guide_links("no newline ![c](specimens/c.jpg)", "t").startswith("no newline ![c](c.jpg)\n\n---"))

    def test_old_folders_go_and_only_theodores_own(self):
        from theodore_client import specimens
        import time
        with client_home(), self.temp() as root:
            now = time.time()
            for name, age in (("theodore-specimens-old", 3601), ("theodore-specimens-new", 3500), ("theodore-specimens-ahead", -3600), ("other-old", 99999)):
                (root / name).mkdir()
                (root / name / "f.jpg").write_bytes(b"x")
                os.utime(root / name, (now - age, now - age))
            (root / "theodore-specimens-file").write_bytes(b"x")
            specimens.sweep()
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["other-old", "theodore-specimens-file", "theodore-specimens-new"])
            self.assertEqual(specimens.sweep(everything=True), [])
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["other-old", "theodore-specimens-file"])


class HostTest(unittest.TestCase):
    """Hand-back H: a keychain that does not answer, statuses of three lines."""

    def test_every_keychain_call_has_a_time_limit_and_falls_back_to_the_file(self):
        import io
        import subprocess
        from theodore_client import credentials
        calls = []

        def hang(command, **kwargs):
            calls.append((command, kwargs.get("timeout")))
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        with client_home() as home, mock.patch.dict(os.environ, {"THEODORE_CREDENTIAL_STORE": "keychain"}), \
                mock.patch.object(subprocess, "run", side_effect=hang), mock.patch("sys.stderr", new=io.StringIO()) as err:
            core.home().ensure()
            self.assertEqual(credentials.store("inst-1", "secret-1"), "file")
            path = home / "state" / "credentials"
            self.assertTrue(path.exists())
            if sys.platform != "win32":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(credentials.load(), {"installation_id": "inst-1", "installation_secret": "secret-1"})
            self.assertTrue(calls)
            self.assertEqual({limit for _c, limit in calls}, {10}, calls)
            note = [line for line in err.getvalue().splitlines() if "keychain" in line]
            self.assertEqual(len(note), 1, err.getvalue())
            self.assertIn("10 seconds", note[0])
            self.assertNotIn("secret-1", err.getvalue())
            before = len(calls)
            path.unlink()
            self.assertIsNone(credentials.load(), "no item, and the keychain is not asked again while it does not answer")
            self.assertEqual(credentials.store("inst-2", "secret-2"), "file")
            self.assertEqual(len(calls), before, calls[before:])
            self.assertTrue(credentials.delete())
            self.assertFalse(path.exists())

    def test_a_keychain_that_answers_is_still_used(self):
        import subprocess
        from theodore_client import credentials
        seen = []

        def fine(command, **kwargs):
            seen.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(command, 0, "", "")
        with client_home() as home, mock.patch.dict(os.environ, {"THEODORE_CREDENTIAL_STORE": "keychain"}), mock.patch.object(subprocess, "run", side_effect=fine):
            core.home().ensure()
            self.assertEqual(credentials.store("inst-1", "secret-1"), "keychain")
            self.assertFalse((home / "state" / "credentials").exists())
            self.assertEqual(set(seen), {10})

    def test_every_status_is_three_lines(self):
        silent = core.Status("OK", "automatic report filed as TR-1.", "continue.", None).render().splitlines()
        self.assertEqual(len(silent), 3)
        self.assertTrue(silent[2].startswith("TELL THE USER: nothing"), silent[2])
        self.assertEqual(len(core.Status("OK", "a", "b", "c").render().splitlines()), 3)

    def test_the_site_named_in_texts_is_the_stamped_one(self):
        with client_home(site_base="https://staging.example.test/"):
            self.assertEqual(core.site_url("/app/plan"), "https://staging.example.test/app/plan")
            self.assertEqual(core.site_name("/install"), "staging.example.test/install")
        with client_home(marketplace_url="https://older.example.test/marketplace.json"):      # a launcher.json from before site_base
            self.assertEqual(core.site_url("/app/plan"), "https://older.example.test/app/plan")
        with client_home():
            self.assertEqual(core.site_url("/install"), "the Theodore site")
            self.assertNotIn("theodoreand.ai", core.no_release_keys().render().replace("@theodoreand.ai", ""))
