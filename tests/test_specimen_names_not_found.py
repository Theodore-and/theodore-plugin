"""0.4.30: a specimen name that is not a sheet is said beside the sheets that came, with the nearest real names.

Seen on 2026-11-22 in the conversations of first-deck runs: one name the service has no sheet for (a page's title
slugged, a code in a gap of its family's numbers) failed the whole `specimen` call with the generic USAGE status, which
pointed at `help` (it lists no sheets) and threw away the valid sheets named in the same call. Now every valid sheet is
delivered, each name that is not a sheet gets one line naming the nearest real names (from the published index, the
list the tool server's schema is built from), and the call fails only when nothing at all was delivered.
"""

from __future__ import annotations

import base64
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase, IS_WINDOWS  # noqa: F401,E402  (support puts the client on sys.path)
from test_mcp import McpCase  # noqa: E402

from theodore_client import specimens  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"stand-in sheet " * 30 + b"\xff\xd9"
PAGES = ["range-tables.jpg", "range-edges.jpg", "range-seams.jpg", "cover-01.jpg", "table-heads-and-key-columns.jpg"]
CODES = [f"QX{n}.jpg" for n in (1, 2, 3, 4, 5, 6, 7, 8, 11)] + ["ZB2.jpg", "ZB3.jpg"]


def index(names):
    return {"version": "g1", "sections": [{"name": "craft"}], "specimens": [{"file": name} for name in names]}


class Nearest(unittest.TestCase):
    """In process, so it also runs on Windows."""

    def test_the_same_words_in_another_order_come_first(self):
        self.assertEqual(specimens.nearest("tables-the-range.jpg", PAGES + CODES)[0], "range-tables.jpg")
        self.assertEqual(specimens.nearest("edges-range.jpg", PAGES)[0], "range-edges.jpg")

    def test_a_close_spelling_is_found_when_no_words_are_shared(self):
        self.assertEqual(specimens.nearest("cover01.jpg", PAGES)[0], "cover-01.jpg")

    def test_a_code_in_a_gap_names_its_family(self):
        self.assertEqual(sorted(specimens.nearest("QX9.jpg", PAGES + CODES)), sorted(n for n in CODES if n.startswith("QX")))
        note = specimens.missing_note("QX9.jpg", PAGES + CODES)
        self.assertEqual(note, "no such sheet: QX9.jpg; nearest: QX8.jpg, QX11.jpg "
                               "(the QX codes that exist: QX1, QX2, QX3, QX4, QX5, QX6, QX7, QX8, QX11)")
        self.assertTrue(specimens.missing_note("QX12.jpg", CODES).startswith("no such sheet: QX12.jpg; nearest: QX11.jpg (the QX codes that exist: QX1,"))

    def test_nothing_near_and_no_list(self):
        self.assertEqual(specimens.missing_note("utterly-unlike.jpg", CODES), "no such sheet: utterly-unlike.jpg; no sheet has a similar name")
        self.assertEqual(specimens.missing_note("range-tablez.jpg", None), "no such sheet: range-tablez.jpg")

    def test_the_index_gives_valid_names_only(self):
        import json
        from theodore_client.service import texts
        raw = json.dumps({"specimens": [{"file": "a.jpg"}, {"file": "../b.jpg"}, {"file": "NUL.jpg"}, "c.jpg", {"file": "a.jpg"}]}).encode()
        self.assertEqual(texts.parse_index(raw)["specimens"], ["a.jpg", "c.jpg"])
        self.assertIsNone(texts.parse_index(b"# a text", content_type="text/markdown"))


class LauncherSpecimen(ClientCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {name: JPEG + name.encode() for name in PAGES + CODES}
        self.api.guide_index = index(PAGES + CODES)
        self.work = self.box.tmp / "work"
        self.work.mkdir()

    def client(self, *args, **kwargs):
        kwargs.setdefault("cwd", getattr(self, "work", None))
        return super().client(*args, **kwargs)

    def test_valid_sheets_are_delivered_and_each_wrong_name_gets_a_line(self):
        result = self.client("specimen", "cover-01.jpg", "tables-the-range.jpg", "QX2", "QX9.jpg", "range-edges.jpg")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual([Path(line).name.split("-", 1)[1] for line in lines[:3]], ["cover-01.jpg", "QX2.jpg", "range-edges.jpg"])
        for line, name in zip(lines[:3], ("cover-01.jpg", "QX2.jpg", "range-edges.jpg")):
            self.assertEqual(Path(line).read_bytes(), JPEG + name.encode())
        self.assertEqual(lines[3], "no such sheet: tables-the-range.jpg; nearest: range-tables.jpg")
        self.assertTrue(lines[4].startswith("no such sheet: QX9.jpg; nearest: QX8.jpg, QX11.jpg (the QX codes that exist: QX1,"), lines[4])
        self.assertEqual(len(lines), 5, "the notes right after the paths, on the same stream")
        self.assertEqual(result.stderr.strip(), "")
        self.assertNotIn("wrong command", result.stdout)
        self.assertNotIn("help", result.stdout)
        self.assertEqual(self.api.served["index", "", 200], 1, "the list is asked for once, only because a name was not found")

    def test_no_index_is_asked_for_when_every_name_is_a_sheet(self):
        result = self.client("specimen", "cover-01.jpg", "QX2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.api.served["index", "", 200], 0)

    def test_nothing_delivered_is_a_usage_status_with_the_nearest_names(self):
        result = self.client("specimen", "tables-the-range.jpg", "QX10")
        first, nxt, tell = self.assertStatus(result, "USAGE", 64)
        self.assertIn("no such sheet: tables-the-range.jpg; nearest: range-tables.jpg", first)
        self.assertIn("no such sheet: QX10.jpg; nearest: QX8.jpg, QX11.jpg", first)
        self.assertIn("specimen with the nearest name", nxt)
        for said in (first, nxt, tell):
            self.assertNotIn("wrong command", said)
            self.assertNotIn("help", said)
        cache = [p for p in self.box.user_home.rglob("theodore-specimens-*") if p.is_dir()]
        self.assertEqual(cache, [], "no folder is left when nothing was delivered")

    def test_an_older_service_without_the_index_still_delivers_and_names_the_miss(self):
        self.api.guide_index = None
        result = self.client("specimen", "cover-01.jpg", "tables-the-range.jpg")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(Path(lines[0]).name.split("-", 1)[1], "cover-01.jpg")
        self.assertEqual(lines[1:], ["no such sheet: tables-the-range.jpg"])

    def test_a_name_that_is_not_a_file_name_is_still_refused_before_any_request(self):
        before = len(self.api.requests)
        for names in (["cover-01.jpg", "../range-tables.jpg"], ["cover-01.jpg", "bad name.jpg"], ["NUL.jpg"]):
            self.assertStatus(self.client("specimen", *names), "USAGE", 64)
        self.assertEqual(len(self.api.requests), before)


class ToolServerSpecimen(McpCase):
    def test_the_sheets_come_with_a_line_for_the_name_that_is_not_one(self):
        self.sign_in()
        self.api.specimens = {name: JPEG + name.encode() for name in PAGES + CODES}
        self.api.guide_index = index(PAGES + CODES)
        server = self.server()
        content = server.call("specimen", file=["cover-01.jpg", "QX9", "range-edges.jpg"])["content"]
        self.assertEqual([b["type"] for b in content], ["text", "image", "text", "text", "image"])
        self.assertEqual(content[0]["text"], "cover-01.jpg")
        self.assertTrue(content[2]["text"].startswith("no such sheet: QX9.jpg; nearest: QX8.jpg, QX11.jpg"), content[2]["text"])
        self.assertEqual(base64.b64decode(content[4]["data"]), JPEG + b"range-edges.jpg")
        alone = server.call("specimen", file="tables-the-range.jpg")
        self.assertTrue(alone.get("isError"))
        text = alone["content"][0]["text"]
        self.assertTrue(text.startswith("THEODORE USAGE: no sheet was delivered: no such sheet: tables-the-range.jpg; nearest: range-tables.jpg"), text)
        self.assertNotIn("wrong command", text)
        self.assertNotIn("help", text)
        server.close()


if __name__ == "__main__":
    unittest.main()
