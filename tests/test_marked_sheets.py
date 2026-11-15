"""Lane PROTECT2: the service marks every sheet it hands out for the account it went to (a JPEG comment segment after
the start-of-image marker). The client keeps integrity real (the bytes as sent, and the published sheet under the mark)
and still revalidates a held sheet with 304. Synthetic images only."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase, status_lines
from test_mcp import McpCase

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"synthetic pixels " * 40 + b"\xff\xd9"
TOKEN = b"A1b2C3d4-opaque_token"


def marked(data: bytes, token: bytes = TOKEN) -> bytes:
    payload = b"theodore-mark/1 " + token
    return data[:2] + b"\xff\xfe" + (len(payload) + 2).to_bytes(2, "big") + payload + data[2:]


class LauncherSheets(ClientCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {"cover-01.jpg": JPEG}
        self.api.specimen_mark = TOKEN
        self.work = self.box.tmp / "work"           # a working folder that does not contain the temporary folder
        self.work.mkdir()

    def client(self, *args, **kwargs):
        kwargs.setdefault("cwd", getattr(self, "work", None))
        return super().client(*args, **kwargs)

    def test_a_marked_sheet_is_kept_as_sent_and_revalidated_with_the_published_hash(self):
        first = self.client("specimen", "cover-01.jpg")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        path = Path(first.stdout.strip())
        self.assertEqual(path.read_bytes(), marked(JPEG), "kept exactly as the service sent it")
        again = self.client("specimen", "cover-01.jpg")
        self.assertEqual(Path(again.stdout.strip()), path)
        self.assertEqual(self.api.served["specimen", "cover-01.jpg", 200], 1)
        self.assertEqual(self.api.served["specimen", "cover-01.jpg", 304], 1, "the held copy is current: 304")

    def test_a_marked_sheet_whose_content_is_not_the_published_one_is_refused(self):
        # The bytes under the mark differ from the sheet the ETag names (a sheet damaged or swapped on the way, with a
        # transport hash recomputed to match): refused.
        original = self.api._send

        def send(h, status, body=None, headers=None, content_type="application/json"):
            if headers and "ETag" in headers and status == 200:
                headers = {**headers, "ETag": '"' + hashlib.sha256(b"another sheet").hexdigest() + '"'}
            return original(h, status, body, headers, content_type)
        self.api._send = send
        try:
            result = self.client("specimen", "cover-01.jpg")
        finally:
            self.api._send = original
        first, _nxt, _tell = status_lines(result.stdout)
        self.assertTrue(first.startswith("THEODORE RETRY: "), result.stdout)
        self.assertIn("did not arrive as the service sent it", first)

    def test_an_unmarked_sheet_from_an_older_service_still_works(self):
        self.api.specimen_mark = None
        first = Path(self.client("specimen", "cover-01.jpg").stdout.strip())
        self.assertEqual(first.read_bytes(), JPEG)
        self.client("specimen", "cover-01.jpg")
        self.assertEqual(self.api.served["specimen", "cover-01.jpg", 304], 1)


class LocalServerSheets(McpCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {"a.jpg": JPEG, "b.jpg": JPEG + b"b"}
        self.api.specimen_mark = TOKEN
        self.api.guide_index = None

    def test_the_held_marked_sheet_is_checked_with_the_published_hash(self):
        server = self.server()
        content = server.call("specimen", file=["a.jpg", "b.jpg"])["content"]
        images = [base64.b64decode(b["data"]) for b in content if b["type"] == "image"]
        self.assertEqual(images, [marked(JPEG), marked(JPEG + b"b")], "the image is the one the service sent")
        server.call("specimen", file=["a.jpg", "b.jpg"])
        self.assertEqual(self.api.served["specimen", "a.jpg", 200], 1)
        self.assertEqual(self.api.served["specimen", "a.jpg", 304], 1)
        self.assertEqual(self.api.served["specimen", "b.jpg", 304], 1)


if __name__ == "__main__":
    unittest.main()
