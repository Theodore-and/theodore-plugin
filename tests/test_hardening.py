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
