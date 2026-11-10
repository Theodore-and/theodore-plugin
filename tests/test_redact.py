"""The free-text redactor (client/theodore_client/redact.py), a port of the server's: the shared case table in
tests/fixtures/redact-cases.json, credentials, generated secrets, pathological inputs, hit counts, and the table
against the server's test when the backend repository is checked out beside this one."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
from theodore_client.redact import redact_free_text, redact_with_hits

CASES = ROOT / "tests" / "fixtures" / "redact-cases.json"

# Built from parts so that no literal in this file matches a secret scanner rule; tools/snapshot_redact_cases.mjs
# has the same.
PLACEHOLDERS = {
    "{SK_LIVE}": "sk_" + "live_" + "0123456789abcdef" * 2,
    "{JWT}": ".".join(["eyJ" + "hbGciOiJFZERTQSJ9", "eyJ" + "zdWIiOiJ4In0", "c2lnbmF0dXJl" + "LWJ5dGVz"]),
}


def expand(text: str) -> str:
    for placeholder, value in PLACEHOLDERS.items():
        text = text.replace(placeholder, value)
    return text


def kinds(hits: dict) -> list:
    return [hit["kind"] for hit in hits.values()]


class SharedCases(unittest.TestCase):
    def test_every_case(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 40)
        for case in cases:
            text, expected = expand(case["text"]), expand(case["expected"])
            with self.subTest(case["name"]):
                hits: dict = {}
                self.assertEqual(redact_with_hits(text, "f", hits), expected)
                self.assertEqual(sorted(kinds(hits)), sorted(case["kinds"]))
                self.assertEqual(redact_free_text(text), expected)

    def test_placeholders_are_expanded(self):
        cases = {case["name"]: case for case in json.loads(CASES.read_text(encoding="utf-8"))}
        self.assertIn("{SK_LIVE}", cases["key material"]["text"])
        self.assertIn("{JWT}", cases["a JWT"]["text"])


class Credentials(unittest.TestCase):
    value = "-".join(["short", "secret"])

    def test_removes_the_userinfo_of_a_url(self):
        url = "https://" + "ci" + "user" + ":" + "s3" + "cr3t" + "@localhost/api"
        self.assertEqual(redact_free_text("GET %s failed" % url), "GET https://[redacted]@localhost/api failed")
        self.assertEqual(redact_free_text("clone " + "git" + "://" + "bot" + "@example.org/repo"),
                         "clone git://[redacted]@example.org/repo")

    def test_removes_the_value_of_each_credential_name(self):
        value = self.value
        for name in ["password", "passwd", "pwd", "token", "secret", "apikey", "api_key", "api-key", "key",
                     "access_token", "client_secret", "PASSWORD"]:
            with self.subTest(name):
                self.assertEqual(redact_free_text("%s=%s next" % (name, value)), "%s=[redacted] next" % name)
                self.assertEqual(redact_free_text("%s: %s next" % (name, value)), "%s: [redacted] next" % name)
                self.assertEqual(redact_free_text('%s="%s with spaces" next' % (name, value)),
                                 '%s="[redacted]" next' % name)
                self.assertEqual(redact_free_text('{"%s": "%s"}' % (name, value)), '{"%s": "[redacted]"}' % name)
                hits: dict = {}
                redact_with_hits("%s=%s" % (name, value), "f", hits)
                self.assertEqual(kinds(hits), ["long_token"])

    def test_keeps_an_ordinary_pair(self):
        for text in ["mode=draft", "layout: two-column", "slide=4 of 9", "keys=3", "tokens: 1200"]:
            self.assertEqual(redact_free_text(text), text)


class GeneratedSecrets(unittest.TestCase):
    def test_never_lets_a_generated_secret_through(self):
        # secrets.token_urlsafe(32) is the server's randomToken(32): 32 random bytes in unpadded base64url.
        for _ in range(2000):
            secret = secrets.token_urlsafe(32)
            for text in ["Authorization: Bearer " + secret, "secret %s end" % secret, secret, "x=%s;" % secret,
                         '"%s"' % secret]:
                out = redact_free_text(text)
                self.assertNotIn(secret, out, text)
                self.assertNotIn(secret[:16], out, text)


PATHOLOGICAL = [
    ("one 64 KiB run", "a" * 65_536),
    ("64 KiB of slashes", "/" * 65_536),
    ("64 KiB of backslashes", "\\" * 65_536),
    ("home-path starts end to end", "~/" * 32_768),
    ("user folders end to end", "/Users/" * 9_362),
    ("at signs between letters", "a@" * 32_768),
    ("extensions end to end", "x.pptx" * 10_922),
    ("one long clause of names before an extension", "Acme " * 13_107 + "Board.pptx"),
    ("dots and spaces", ". " * 32_768),
    ("bearer words", "Bearer " * 9_362),
    ("paths with spaces end to end", "/a b " * 13_107),
    ("drive paths with spaces end to end", "C:\\a b " * 9_362),
    ("one path with a separator at the end of a long clause", "/a " + "word " * 13_000 + "x/y"),
    ("credential names end to end", "password=" * 7_281),
    ("URL starts end to end", "://a:b@" * 9_362),
    ("key names without values", "key" * 21_845),
    ("many short pairs", "key=a " * 10_922),
    ("many URLs with userinfo", "x://a@b " * 8_192),
]


class Pathological(unittest.TestCase):
    def test_each_finishes_in_under_a_second(self):
        for name, text in PATHOLOGICAL:
            with self.subTest(name):
                started = time.perf_counter()
                redact_free_text(text)
                self.assertLess(time.perf_counter() - started, 1.0, name)


class Hits(unittest.TestCase):
    def test_counts_hits_per_kind_for_one_field(self):
        hits: dict = {}
        redact_with_hits("a@b.co and c@d.co in /Users/x", "summary", hits)
        self.assertEqual(list(hits.values()), [
            {"kind": "home_path", "field": "summary", "count": 1},
            {"kind": "email", "field": "summary", "count": 2},
        ])


class AgainstTheServer(unittest.TestCase):
    def test_fixture_matches_the_server_test(self):
        backend = Path(os.environ.get("THEODORE_BACKEND") or ROOT.parent / "theodore")
        node = shutil.which("node")
        if not (backend / "apps" / "worker" / "test" / "integration" / "redact.test.ts").is_file():
            self.skipTest("backend repository not checked out at %s" % backend)
        if node is None:
            self.skipTest("node is not on PATH")
        result = subprocess.run([node, str(ROOT / "tools" / "snapshot_redact_cases.mjs"), str(backend), "--check"],
                                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
