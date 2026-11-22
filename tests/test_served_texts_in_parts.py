"""0.4.25: a served text reaches an assistant in Claude Code in parts that fit one answer, and what tells how to read
the text's links comes first.

Found on 2026-11-22 in the conversations of first-deck runs. Claude Code shows the assistant one command's output
whole only below about 30,000 characters; a longer one arrives as a 2 KB preview and the path of a saved file. The
client printed a deck-design text whole there whatever its length (2026-11-15), and the assistants read the first
thousands of characters of the saved file. The deck-design skill body of an account with Studio was cut at the
client's limit, and with its end went the one line saying how its links are read. And a designer that loaded a skill
with the Skill tool got the text written for the whole deck: the Skill tool carries no role.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from support import ClientCase, IS_WINDOWS, ROOT  # noqa: F401  (support puts the client on sys.path)

from theodore_client import commands  # noqa: E402

SHOWN_WHOLE = 30000          # what Claude Code shows of one answer
LINKS = ("Links in this guidance are read through the same launcher: a link written references/<name>.md or <name>.md is the "
         "section printed by: guide deck-design <name>, and a link written specimens/<file>.jpg is a sheet.")


def body_of(out: str, name: str) -> str:
    return out.split(f"--- {name} ---\n", 1)[1].rsplit("=== END", 1)[0]


class InClaudeCodeALongTextComesInParts(ClientCase):
    CC = {"CLAUDECODE": "1"}
    # six sections of about 9,000 characters: three parts of two sections
    CRAFT = "".join(f"## Section {i}\n" + "".join(f"craft {i} line {j:03d} " + "y" * 56 + "\n" for j in range(120)) for i in range(1, 7))

    def test_a_section_is_cut_at_its_headings_and_every_answer_fits(self):
        self.sign_in()
        self.api.guides["deck-design"]["craft"] = self.CRAFT
        launcher = f'"{self.box.home / "bin" / "theodore"}"'
        first = self.client("guide", "deck-design", "craft", env=self.CC)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        lines = first.stdout.splitlines()
        self.assertEqual(lines[0], "=== THEODORE deck-design g1 (beta), part 1/3 ===")
        self.assertTrue(lines[1].startswith("THIS IS PART 1 OF 3 OF ONE TEXT ("), lines[1])
        self.assertTrue(lines[-1].endswith(f"NEXT PART: {launcher} guide deck-design craft --part 2 ==="), lines[-1])
        joined = ""
        for number in (1, 2, 3):
            out = self.client("guide", "deck-design", "craft", "--part", str(number), env=self.CC).stdout
            self.assertLess(len(out), SHOWN_WHOLE, "the answer is shown whole")
            part = body_of(out, "craft")
            self.assertTrue(part.startswith(f"## Section {2 * number - 1}\n"), part[:40])
            joined += part
        self.assertEqual(joined, self.CRAFT, "the parts are the text, nothing lost and nothing twice")
        self.assertEqual([q.get("form") for q in self.api.guide_queries], ["whole"] * 4, "each command is one request for the whole text")
        self.assertStatus(self.client("guide", "deck-design", "craft", "--part", "4", env=self.CC), "USAGE", 64)

    def test_a_text_that_fits_is_one_document_as_before(self):
        self.sign_in()
        self.api.guides["deck-design"]["craft"] = self.CRAFT[:9000]
        out = self.client("guide", "deck-design", "craft", env=self.CC).stdout
        self.assertEqual(out.splitlines()[0], "=== THEODORE deck-design g1 (beta), part 1/1 ===")
        self.assertTrue(out.splitlines()[1].startswith("THIS IS THE WHOLE TEXT: 9000 characters"), out[:200])
        self.assertTrue(out.splitlines()[-1].endswith(". THE TEXT IS COMPLETE. ==="))
        licence = self.client("guide", "deck-design", "--license", env=self.CC).stdout
        self.assertNotIn("NEXT PART", licence)

    def test_a_service_that_pages_is_asked_for_every_part_and_the_parts_are_still_cut_here(self):
        self.sign_in()
        self.api.guide_whole = False
        self.api.guides["deck-design"]["craft"] = self.CRAFT
        result = self.client("guide", "deck-design", "craft", "--part", "2", env=self.CC)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([(q.get("form"), q.get("part")) for q in self.api.guide_queries], [("whole", None), (None, "2"), (None, "3")])
        self.assertTrue(body_of(result.stdout, "craft").startswith("## Section 3\n"), "its part 2 starts at a heading, not at character 24,000")

    def test_elsewhere_the_parts_stay_the_services(self):
        self.sign_in()
        self.api.guides["deck-design"]["craft"] = self.CRAFT
        out = self.client("guide", "deck-design", "craft").stdout
        self.assertEqual(out.splitlines()[0], "=== THEODORE deck-design g1 (beta), part 1/3 ===")
        self.assertEqual(body_of(out, "craft").rstrip("\n"), self.CRAFT[:24000].rstrip("\n"))
        self.assertEqual([(q.get("form"), q.get("part")) for q in self.api.guide_queries], [(None, None)])

    def test_the_links_written_as_commands_are_counted_in_a_parts_size(self):
        # a link becomes the command that prints its section, which is longer: a part is measured as it is printed
        self.sign_in()
        linked = "".join(f"## Section {i}\n" + "".join(f"see [the forms](references/forms.md) and line {j:03d}\n" for j in range(330)) for i in range(1, 5))
        self.api.guides["deck-design"]["craft"] = linked
        total = int(self.client("guide", "deck-design", "craft", env=self.CC).stdout.splitlines()[0].rsplit("/", 1)[1].split(" ")[0])
        self.assertGreater(total, 1)
        for number in range(1, total + 1):
            out = self.client("guide", "deck-design", "craft", "--part", str(number), env=self.CC).stdout
            self.assertLessEqual(len(body_of(out, "craft")), commands.GUIDE_PART_CHARS + 1)
            self.assertLess(len(out), SHOWN_WHOLE)
            self.assertNotIn("](references/forms.md)", out)


class TheLineOnLinksComesFirst(ClientCase):
    CC = {"CLAUDECODE": "1"}
    MAIN = "---\nname: deck-design\n---\n\n# Deck design\n\nStart with [craft](references/craft.md).\n\n## Later\n\nLast words.\n"

    def test_in_the_main_text_it_follows_the_front_matter(self):
        self.sign_in()
        self.api.guides["deck-design"][""] = self.MAIN
        self.api.guide_whole_tail = {"": "\n" + LINKS + "\n"}
        out = self.client("guide", "deck-design", env=self.CC).stdout
        text = body_of(out, "SKILL.md")
        self.assertTrue(text.startswith("---\nname: deck-design\n---\n\n" + LINKS + "\n\n# Deck design\n"), text[:400])
        self.assertEqual(text.count(LINKS), 1)
        self.assertTrue(text.rstrip("\n").endswith("Last words."), "and the text ends where it ended")

    def test_in_the_skill_body_it_is_the_first_line(self):
        self.sign_in()
        self.api.guides["deck-design"][""] = self.MAIN
        self.api.guide_whole_tail = {"": "\n" + LINKS + "\n"}
        out = self.client("skill", "deck-design").stdout
        self.assertTrue(out.startswith(LINKS + "\n\n# Deck design\n"), out[:300])
        self.assertEqual(out.count(LINKS), 1)

    def test_a_text_without_the_line_is_unchanged(self):
        self.assertEqual(commands._links_first(self.MAIN), self.MAIN)
        self.assertEqual(commands._links_first(""), "")

    def test_a_skill_body_cut_for_length_says_so_at_its_top_and_keeps_the_line(self):
        self.sign_in()
        long_main = "# Deck design\n\n" + "".join(f"a line of the method {i:04d} " + "z" * 50 + "\n" for i in range(420))      # about 31,000
        self.api.guides["deck-design"][""] = long_main
        self.api.guide_whole_tail = {"": "\n" + LINKS + "\n"}
        out = self.client("skill", "deck-design").stdout
        lines = out.splitlines()
        self.assertLessEqual(len(out), commands.SKILL_BODY_MAX_CHARS + 1)
        self.assertTrue(lines[0].startswith("[This document is longer than one answer holds and is cut near its end."), lines[0])
        self.assertIn("guide deck-design]", lines[0])
        self.assertEqual(lines[2], LINKS, "the line on links is not what the cut takes")
        self.assertTrue(lines[-1].startswith("[The rest of this document: "), lines[-1])


class TheStudioAgentReadsWithItsMessagesCommands(unittest.TestCase):
    def test_the_agent_text_sends_a_designer_to_the_commands_of_its_message_not_to_the_skill_tool(self):
        text = (ROOT / "plugins" / "theodore" / "agents" / "studio.md").read_text(encoding="utf-8")
        body = text.split("---\n", 2)[2]
        self.assertIn("Read Theodore's texts with the commands your message gives, exactly as written", body)
        self.assertIn("do not load the deck-design or pptx skill with the Skill tool", body)
        self.assertNotIn("Load the deck-design skill", body)
        for word in ("lead", "role", "designer"):
            self.assertNotIn(word, body.lower(), f"the file on disk does not name the method's {word}")


if __name__ == "__main__":
    unittest.main()
