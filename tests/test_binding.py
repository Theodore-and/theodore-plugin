"""Device binding, client side (P039): the P-256 signature, the machine value, the key kept with the credential, the
bind at log in and at the first refresh of an older installation, the proof at refresh, an older server, and what a
copied installation is told.

The service's side is in the backend (apps/worker/test/integration/device-binding.test.ts); the fake API here checks
proofs the way it does."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase  # noqa: E402

from theodore_client import device  # noqa: E402

CLIENT = Path(__file__).resolve().parents[1] / "client"
MACHINE = "4C4C4544-0042-3510-8051-B7C04F4E4E32"


class Signature(unittest.TestCase):
    def test_rfc6979_p256_sha256_vectors(self):
        # RFC 6979, A.2.5: the key, its public point, and the signatures of "sample" and "test" with SHA-256
        d = int("C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721", 16)
        self.assertEqual(device.public_point(d), (int("60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6", 16),
                                                  int("7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299", 16)))
        vectors = {b"sample": ("EFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716",
                               "F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8"),
                   b"test": ("F1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367",
                             "019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083")}
        for message, (r, s) in vectors.items():
            signature = device.sign(d, message)
            self.assertEqual(signature.hex().upper(), r + s)
            self.assertTrue(device.verify(device.public_point(d), message, signature))
            self.assertFalse(device.verify(device.public_point(d), message + b".", signature))

    def test_a_new_key_signs_a_proof_the_service_can_check(self):
        d = device.new_private_key()
        key = device.jwk(d)
        self.assertEqual(set(key), {"kty", "crv", "x", "y"})
        self.assertEqual(len(device.b64u_decode(key["x"])), 32)
        self.assertEqual(device.decode_key(device.encode_key(d)), d)
        for nothing in (None, "", "None", 7, "x" * 42, device.b64u(b"\x00" * 32), device.b64u(device.N.to_bytes(32, "big"))):
            self.assertIsNone(device.decode_key(nothing), nothing)
        text = device.proof(d, htu="https://api.example.test/v1/licence/refresh", nonce="n" * 43, bearer="secret", now=1_700_000_000)
        head, payload, signature = text.split(".")
        self.assertEqual(json.loads(device.b64u_decode(head)), {"alg": "ES256", "typ": "theodore-proof+jwt"})
        claims = json.loads(device.b64u_decode(payload))
        self.assertEqual({k: claims[k] for k in ("htm", "htu", "iat", "nonce")},
                         {"htm": "POST", "htu": "https://api.example.test/v1/licence/refresh", "iat": 1_700_000_000, "nonce": "n" * 43})
        self.assertEqual(claims["ath"], device.b64u(hashlib.sha256(b"secret").digest()))
        self.assertTrue(16 <= len(claims["jti"]) <= 64)
        point = (int.from_bytes(device.b64u_decode(key["x"]), "big"), int.from_bytes(device.b64u_decode(key["y"]), "big"))
        self.assertTrue(device.verify(point, f"{head}.{payload}".encode(), device.b64u_decode(signature)))

    def test_openssl_accepts_the_signature(self):
        # an independent verifier, where one is installed: the DER form of r || s against the SubjectPublicKeyInfo
        import shutil
        import tempfile
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl is not installed")
        d = device.new_private_key()
        x, y = device.public_point(d)
        spki = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200") + b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
        signature = device.sign(d, b"theodore")

        def der_int(value: int) -> bytes:
            raw = value.to_bytes(33, "big").lstrip(b"\x00")
            raw = b"\x00" + raw if raw[0] & 0x80 else raw
            return b"\x02" + bytes([len(raw)]) + raw
        body = der_int(int.from_bytes(signature[:32], "big")) + der_int(int.from_bytes(signature[32:], "big"))
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "key.pem").write_text("-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(spki).decode() + "\n-----END PUBLIC KEY-----\n")
            Path(folder, "sig.der").write_bytes(b"\x30" + bytes([len(body)]) + body)
            Path(folder, "msg").write_bytes(b"theodore")
            done = subprocess.run([openssl, "dgst", "-sha256", "-verify", str(Path(folder, "key.pem")), "-signature",
                                   str(Path(folder, "sig.der")), str(Path(folder, "msg"))], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_the_thumbprint_is_rfc7638(self):
        key = {"kty": "EC", "crv": "P-256", "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU", "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0"}
        expected = device.b64u(hashlib.sha256(('{"crv":"P-256","kty":"EC","x":"%s","y":"%s"}' % (key["x"], key["y"])).encode()).digest())
        self.assertEqual(device.thumbprint(dict(reversed(list(key.items())))), expected)


class MachineValue(unittest.TestCase):
    def test_the_known_answer_shared_with_the_engine(self):
        # The engine (rc.10) computes the same value from the same source; this pair is the shared known answer.
        device._MACHINE[:] = [MACHINE]
        self.addCleanup(device._MACHINE.clear)
        self.assertEqual(device.mid("11111111-2222-4333-8444-555555555555"), "YlNgiiKWUmhR4Yed9IxJshxhEnM3sRL3VEP6P7yi040")
        self.assertNotEqual(device.mid("11111111-2222-4333-8444-555555555555"), device.mid("11111111-2222-4333-8444-555555555556"))
        device._MACHINE[:] = [None]
        self.assertIsNone(device.mid("11111111-2222-4333-8444-555555555555"), "no identifier: no value, and the token gets none")

    def test_this_computer_has_an_identifier(self):
        device._MACHINE.clear()
        self.addCleanup(device._MACHINE.clear)
        value = device.machine_id()
        if value is None and sys.platform.startswith("linux") and not any(os.path.exists(p) for p in ("/etc/machine-id", "/var/lib/dbus/machine-id")):
            self.skipTest("a container without a machine id: accepted without one")
        self.assertTrue(value)


def _claims(home: Path) -> dict:
    token = json.loads((home / "state" / "entitlement.json").read_text())["entitlement_token"]
    return json.loads(device.b64u_decode(token.split(".")[1]))


def _stored(home: Path) -> dict:
    return json.loads((home / "state" / "credentials").read_text())


class Binding(ClientCase):
    def setUp(self):
        super().setUp()
        self.box.env["THEODORE_TEST_MACHINE_ID"] = MACHINE

    def refresh(self) -> subprocess.CompletedProcess:
        """A licence refresh as the client makes one when the licence is due, in a process of its own."""
        code = "import sys; sys.path.insert(0, sys.argv[1]); from theodore_client import account; account.refresh(timeout=10)"
        return subprocess.run([sys.executable, "-I", "-c", code, str(CLIENT)], env=self.box.env, capture_output=True, text=True, timeout=60)

    def test_a_server_before_binding_changes_nothing(self):
        self.ready()
        self.assertNotIn("device_key", self.api.binds)
        self.assertEqual(self.refresh().returncode, 0)
        self.assertEqual(self.refresh().returncode, 0)
        self.assertEqual(self.api.binds, [])
        binds_tried = [r for r in self.api.requests if r["path"] == "/v1/installation/bind"]
        self.assertEqual(len(binds_tried), 1, "tried at log in, then left alone for a day")
        self.assertNotIn("mid", _claims(self.box.home))
        self.assertTrue(_stored(self.box.home).get("device_key"), "the key is kept for when the service binds")

    def test_log_in_binds_and_every_refresh_carries_a_proof(self):
        self.api.binding = "observe"
        self.ready()
        stored = _stored(self.box.home)
        self.assertEqual(len(self.api.binds), 1)
        self.assertEqual(self.api.binds[0]["device_key"]["hw"], "file")
        self.assertEqual(self.api.binds[0]["device_key"]["jwk"], device.jwk(device.decode_key(stored["device_key"])))
        self.assertNotIn(stored["device_key"], json.dumps(self.api.binds), "the private key never leaves the computer")
        for _ in range(2):
            result = self.refresh()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.api.proofs, ["ok", "ok"])
        expected = device.b64u(__import__("hmac").new(("theodore-mid-v1" + stored["installation_id"]).encode(), MACHINE.encode(), hashlib.sha256).digest())
        self.assertEqual(self.api.mids, [expected, expected])
        claims = _claims(self.box.home)
        self.assertEqual(claims["mid"], expected)
        self.assertEqual(claims["cnf"], {"jkt": device.thumbprint(device.jwk(device.decode_key(stored["device_key"])))})
        self.assertEqual(len(self.api.binds), 1, "bound once")
        self.assertStatus(self.client("status"), "OK", 0)

    def test_an_installation_from_before_binding_binds_at_its_next_refresh(self):
        self.ready()                         # an older server at log in: nothing bound, nothing to retry for a day
        self.api.binding = "enforce"
        state = self.box.home / "state" / "binding.json"
        state.unlink()                       # 0.3.8 had no such file: the next refresh tries
        stored = _stored(self.box.home)
        stored.pop("device_key")
        (self.box.home / "state" / "credentials").write_text(json.dumps(stored))
        result = self.refresh()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.api.proofs, ["ok"])
        self.assertTrue(_stored(self.box.home).get("device_key"))
        self.assertIn("mid", _claims(self.box.home))

    def test_a_copied_credential_with_another_key_is_refused_and_told_to_log_in(self):
        self.api.binding = "enforce"
        self.ready()
        # The copy's key is not the one the service holds: binding again is refused, so no proof: told to try again,
        # and the licence on disk is kept until it expires.
        stored = _stored(self.box.home)
        other = device.new_private_key()
        stored["device_key"] = device.encode_key(other)
        (self.box.home / "state" / "credentials").write_text(json.dumps(stored))
        (self.box.home / "state" / "binding.json").unlink()
        result = self.refresh()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("device_proof_required", result.stderr)
        self.assertTrue((self.box.home / "state" / "entitlement.json").exists())
        # A proof made with that key (a copy that also believes it is bound): the service refuses it as another
        # computer's, and the credential goes.
        (self.box.home / "state" / "binding.json").write_text(json.dumps({"jkt": device.thumbprint(device.jwk(other))}))
        result = self.refresh()
        self.assertIn("device_proof_invalid", result.stderr)
        self.assertEqual(self.api.proofs[-1], "invalid")
        self.assertFalse((self.box.home / "state" / "credentials").exists(), "the copied credential is removed")
        first, _n, _t = self.assertStatus(self.client("whoami"), "SIGN_IN_REQUIRED", 65)

    def test_the_engine_refusing_another_computer_asks_for_a_log_in(self):
        self.ready()
        marker = self.box.home / "engines" / self.ENGINE_VERSION / "REFUSE"
        marker.write_text("entitlement_wrong_machine")
        first, nxt, _t = self.assertStatus(self.client("pptx", "apply", "x"), "SIGN_IN_REQUIRED", 65)
        self.assertIn("set up on another computer", first)


if __name__ == "__main__":
    unittest.main()
