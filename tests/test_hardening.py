"""Round 3 hardening, in process where the behaviour is a pure function of the client's state: signing keys,
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
