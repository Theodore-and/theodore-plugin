"""How the service reads an engine's answer (tools._last_json): the object that `prepare`, `apply` and `render` print,
in the forms the engine prints it, so a Studio deck's burst is announced and an answer's pictures are found."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import CLIENT_DIR  # noqa: E402

sys.path.insert(0, str(CLIENT_DIR))
from theodore_client.service import tools  # noqa: E402


def prepare_output(parts: int, in_turn: bool = False) -> str:
    """What the engine's `pptx prepare` prints to a pipe (cli_output.print_payload): the object indented, then one
    SLIDE line per part, the last lines a reader sees. Invented paths and titles."""
    rows = [{"slide": 10 + i, "title": f"Invented view {i}", "part": f"s{i:02d}", "slide_id": f"s{10 + i:03d}",
             "workspace": f"/w/deck/parts/s{i:02d}", "status": "created"} for i in range(1, parts + 1)]
    lines = [f"SLIDE {row['slide']} of 40 (redesigned): \"{row['title']}\"  WORKSPACE: {row['workspace']}  NOTE: "
             f"{row['workspace']}/design.md  SPINE: /w/deck/spine.md" for row in rows]
    answer = {"ok": True, "workspace": "/w/deck", "prepared": "parts", "parts": rows, "created": [r["part"] for r in rows],
              "dropped": [], "lines": lines, "message": f"{parts} part(s).", "in_turn": in_turn, "elapsed_seconds": 0.03}
    return json.dumps(answer, indent=2) + "\n" + "".join(line + "\n" for line in lines)


class LastJson(unittest.TestCase):
    def test_prepare_s_answer_is_read_with_its_slide_lines_after_it(self):
        answer = tools._last_json(prepare_output(15))       # noqa: SLF001
        self.assertEqual(answer.get("prepared"), "parts")
        self.assertIs(answer.get("in_turn"), False)
        self.assertEqual(len(answer.get("parts") or []), 15)
        self.assertIs(tools._last_json(prepare_output(3, in_turn=True)).get("in_turn"), True)   # noqa: SLF001

    def test_the_forms_read_before_are_read_the_same(self):
        compact = {"ok": True, "pictures": [{"slide": "s011", "path": "/w/p.png"}], "pending": ["s012"]}
        indented = json.dumps(compact, indent=2)
        cases = {
            "one line": json.dumps(compact),
            "indented alone": indented,
            "lines before a compact object": "WORKSPACE: /w/deck\nDECK: /w/deck.pptx\n" + json.dumps(compact),
            "lines before an indented object": "WORKSPACE: /w/deck\n" + indented,
            "two objects (the last is the answer)": json.dumps({"ok": False, "first": True}) + "\n" + json.dumps(compact),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(tools._last_json(text + "\n"), compact)          # noqa: SLF001

    def test_no_object_reads_as_empty(self):
        for text in ("", "\n", "THEODORE OK: nothing", "SLIDE 1 of 2: {not json}", "[1, 2]", "{\"cut\": "):
            with self.subTest(text):
                self.assertEqual(tools._last_json(text), {})              # noqa: SLF001

    def test_pending_and_pictures_read_from_the_answer(self):
        text = "WORKSPACE: /w/deck\n" + json.dumps({"ok": True, "pending": ["s012", 3],
                                                   "pictures": [{"slide": "s011", "path": "/w/p.png"}]}, indent=2)
        self.assertEqual(tools.pending_of(text), [{"kind": "picture", "slide": "s012"}])
        self.assertEqual(tools.pictures_of(["apply", "/w/deck"], text), ["/w/p.png"])


if __name__ == "__main__":
    unittest.main()
