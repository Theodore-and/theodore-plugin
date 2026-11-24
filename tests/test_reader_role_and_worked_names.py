"""0.4.32: what a slide's designer is told names only what a designer reads, and a worked example is asked for by name.

A designer (`--role slide`) who named a section that is not one was shown the sections of the lead's entry (measured:
"Sections: the-deck, critique", the first lead-only, which the service then refused to that reader), and the pptx
manual's Bindings line taught it `guide pptx <name>` without its role, so following it gave the lead's text. Now the
list is fetched with the reader's role and the line carries `--role slide`; the lead's output is unchanged.

The published index does not list the worked examples (images `worked-<...>.jpg`, sections `worked-<...>` other than
`worked-notes`); the service still serves each by its exact name. The client asks the service for such a name, the
tool server's schema lets one through, a mistyped one names no wrong sheet, and the forms a text links them by
(`specimens/<file>.jpg`, `<name>.md`) are taken to the bare name.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase, IS_WINDOWS, status_lines  # noqa: F401,E402  (support puts the client on sys.path)
from test_mcp import McpCase  # noqa: E402

from theodore_client import mcp, specimens  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"stand-in sheet " * 30 + b"\xff\xd9"
ENTRY = ("# Deck design\n\n<!-- theodore:if lead -->\nPlan [the deck](references/the-deck.md).\n<!-- theodore:end -->\n"
         "Then [critique](references/critique.md).\n")
WORKED_SHEET = "worked-02-summary-chain.jpg"
WORKED_TEXT = "worked-37-roles"
LISTED = ["cover-01.jpg", "range-tables.jpg", "summary-chain.jpg"]


def index():
    """The index as the service publishes it from 0.4.32: no worked name in it."""
    return {"version": "g1", "sections": [{"name": "critique"}, {"name": "worked-notes"}],
            "specimens": [{"file": name} for name in LISTED]}


def launcher_of(case) -> str:
    return f'"{case.box.home / "bin" / "theodore"}"'


class SectionListForTheReader(ClientCase):
    def test_a_designer_is_shown_the_sections_its_own_entry_links(self):
        self.sign_in()
        self.api.guides["deck-design"][""] = ENTRY
        result = self.client("guide", "deck-design", "no-such-text", "--role", "slide")
        first, nxt, _tell = self.assertStatus(result, "USAGE", 64)
        self.assertIn("Sections: critique.", first)
        self.assertNotIn("the-deck", first, "a lead-only section is not offered to a designer")
        self.assertIn("--role slide", nxt)
        lead = self.client("guide", "deck-design", "no-such-text")
        first, nxt, _tell = self.assertStatus(lead, "USAGE", 64)
        self.assertEqual(first, "THEODORE USAGE: guide deck-design has no section 'no-such-text'. Sections: the-deck, critique.",
                         "the lead's answer is as before")
        self.assertNotIn("--role", nxt)

    def test_the_pptx_index_is_asked_with_the_readers_role(self):
        self.ready()
        self.api.guide_index = index()
        for args, expected in ((("--role", "slide"), {"skill": "pptx", "role": "slide"}), ((), {"skill": "pptx", "role": "lead"})):
            before = len(self.api.batches)
            self.assertStatus(self.client("guide", "pptx", "helpers", *args), "USAGE", 64)
            asked = [{"skill": item["skill"], "role": item.get("role")} for items in self.api.batches[before:] for item in items
                     if item["type"] == "index"]
            self.assertEqual(asked, [expected], args)


class BindingsLineForTheReader(ClientCase):
    LINE = "          a text this manual names  ->  {launcher} guide pptx <name>{role}   (a name that is not one lists them)"

    def test_a_designers_line_carries_the_role_and_the_leads_is_unchanged(self):
        self.ready()
        launcher = launcher_of(self)
        slide = self.client("guide", "pptx", "--role", "slide")
        self.assertEqual(slide.returncode, 0, slide.stdout + slide.stderr)
        self.assertIn(self.LINE.format(launcher=launcher, role=" --role slide"), slide.stdout.splitlines())
        lead = self.client("guide", "pptx")
        self.assertIn(self.LINE.format(launcher=launcher, role=""), lead.stdout.splitlines())
        self.assertNotIn("--role", lead.stdout)
        loaded = self.client("skill", "pptx")                   # the skill's load is the lead's
        self.assertIn(self.LINE.format(launcher=launcher, role=""), loaded.stdout.splitlines())


class BindingsLineInTheToolServer(McpCase):
    def test_the_guide_tool_gives_a_designer_the_line_with_its_role(self):
        self.ready()
        server = self.server()
        slide = server.call("guide", skill="pptx", role="slide")["content"][0]["text"]
        self.assertIn("guide pptx <name> --role slide   (a name that is not one lists them)", slide)
        lead = server.call("guide", skill="pptx")["content"][0]["text"]
        self.assertIn("guide pptx <name>   (a name that is not one lists them)", lead)
        server.close()


class WorkedNamesInProcess(unittest.TestCase):
    def test_a_mistyped_worked_name_names_no_other_sheet(self):
        note = specimens.missing_note("worked-02-sumary-chain.jpg", LISTED)
        self.assertEqual(note, "no such sheet: worked-02-sumary-chain.jpg; the worked-notes text gives each worked image's exact name")
        self.assertEqual(specimens.missing_note("sumary-chain.jpg", LISTED), "no such sheet: sumary-chain.jpg; nearest: summary-chain.jpg",
                         "any other name is answered as before")

    def test_the_linked_form_is_the_file(self):
        for given, name in (("specimens/" + WORKED_SHEET, WORKED_SHEET), ("./specimens/cover-01.jpg", "cover-01.jpg"),
                            ("specimens/HC3", "HC3.jpg"), ("cover-01.jpg", "cover-01.jpg"), ("HC3", "HC3.jpg")):
            self.assertEqual(specimens.file_name(given), name, given)

    def test_the_schema_pattern_takes_a_worked_name_bare_or_linked(self):
        for name in (WORKED_SHEET, "specimens/" + WORKED_SHEET):
            self.assertTrue(re.search(mcp.WORKED_PATTERN, name), name)
        for name in ("cover-01.jpg", "worked-02.png", "worked-Upper.jpg", "specimens/../worked-1.jpg"):
            self.assertFalse(re.search(mcp.WORKED_PATTERN, name), name)


class WorkedNamesFromTheLauncher(ClientCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {name: JPEG + name.encode() for name in LISTED + [WORKED_SHEET]}
        self.api.guide_index = index()
        self.api.guides["deck-design"][WORKED_TEXT] = "# Worked 37\n\nthe worked example's note\n"
        self.work = self.box.tmp / "work"
        self.work.mkdir()

    def client(self, *args, **kwargs):
        kwargs.setdefault("cwd", getattr(self, "work", None))
        return super().client(*args, **kwargs)

    def test_a_worked_sheet_the_index_does_not_list_is_delivered_bare_or_linked(self):
        for given in (WORKED_SHEET, "specimens/" + WORKED_SHEET):
            result = self.client("specimen", given)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertTrue(lines[0].endswith("-" + WORKED_SHEET), lines[0])
        linked = self.client("specimen", "specimens/cover-01.jpg")
        self.assertEqual(linked.returncode, 0, linked.stdout + linked.stderr)
        self.assertTrue(linked.stdout.strip().endswith("-cover-01.jpg"))

    def test_a_mistyped_worked_sheet_and_an_unlisted_ordinary_one_are_refused(self):
        first, _nxt, _tell = self.assertStatus(self.client("specimen", "worked-02-sumary-chain.jpg"), "USAGE", 64)
        self.assertIn("no such sheet: worked-02-sumary-chain.jpg; the worked-notes text gives each worked image's exact name", first)
        self.assertNotIn("summary-chain.jpg", first, "no listed sheet is offered in its place")
        first, _nxt, _tell = self.assertStatus(self.client("specimen", "tables-the-range.jpg"), "USAGE", 64)
        self.assertIn("no such sheet: tables-the-range.jpg; nearest: range-tables.jpg", first, "as before")

    def test_a_worked_section_is_read_by_its_name_or_its_link(self):
        for given in (WORKED_TEXT, WORKED_TEXT + ".md", "references/" + WORKED_TEXT + ".md"):
            result = self.client("guide", "deck-design", given)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("the worked example's note", result.stdout, given)
        first, _nxt, _tell = self.assertStatus(self.client("guide", "deck-design", "worked-99-none"), "USAGE", 64)
        self.assertIn("has no section 'worked-99-none'", first)


class WorkedNamesInTheToolServer(McpCase):
    def test_a_worked_sheet_and_section_absent_from_the_index_come_from_the_service(self):
        self.sign_in()
        self.api.specimens = {name: JPEG + name.encode() for name in LISTED + [WORKED_SHEET]}
        self.api.guide_index = index()
        self.api.guides["deck-design"][WORKED_TEXT] = "# Worked 37\n\nthe worked example's note\n"
        server = self.server()
        tools = {t["name"]: t for t in server.send("tools/list")["result"]["tools"]}
        one, _many = tools["specimen"]["inputSchema"]["properties"]["file"]["anyOf"]
        self.assertNotIn(WORKED_SHEET, one["anyOf"][0]["enum"], "the index lists no worked name")
        self.assertTrue(any("pattern" in alt and re.search(alt["pattern"], WORKED_SHEET) for alt in one["anyOf"]),
                        "a host that checks the schema lets the worked name through")
        for given in (WORKED_SHEET, "specimens/" + WORKED_SHEET):
            answer = server.call("specimen", file=given)
            self.assertFalse(answer.get("isError"), answer)
            self.assertEqual(base64.b64decode(answer["content"][0]["data"]), JPEG + WORKED_SHEET.encode())
        for given in (WORKED_TEXT, WORKED_TEXT + ".md"):
            answer = server.call("guide", skill="deck-design", section=given)
            self.assertFalse(answer.get("isError"), answer)
            self.assertIn("the worked example's note", answer["content"][0]["text"])
        missing = server.call("specimen", file="worked-02-sumary-chain.jpg")
        self.assertTrue(missing.get("isError"))
        self.assertIn("the worked-notes text gives each worked image's exact name", missing["content"][0]["text"])
        ordinary = server.call("specimen", file="tables-the-range.jpg")
        self.assertTrue(ordinary.get("isError"))
        self.assertIn("no such sheet: tables-the-range.jpg; nearest: range-tables.jpg", ordinary["content"][0]["text"])
        server.close()


if __name__ == "__main__":
    unittest.main()
