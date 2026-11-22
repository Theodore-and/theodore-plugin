"""0.4.30: the line that says how a main text's links are read comes first, and a skill body cut for length says so.

Found on 2026-11-22 in the conversations of first-deck runs: the deck-design skill body of an account with Studio is
longer than the client's limit for one injected answer, and the cut took its end, where the service puts the one line
saying how the text's links are read. That line is now moved to the start of the text (after its front matter), and a
body cut for length says so on its first line.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase, IS_WINDOWS  # noqa: F401,E402  (support puts the client on sys.path)

from theodore_client import commands  # noqa: E402

LINKS = ("Links in this guidance are read through the same launcher: a link written references/<name>.md or <name>.md is the "
         "section printed by: guide deck-design <name>, and a link written specimens/<file>.jpg is a sheet.")
MAIN = "---\nname: deck-design\n---\n\n# Deck design\n\nStart with [craft](references/craft.md).\n\n## Later\n\nLast words.\n"


def body_of(out: str, name: str) -> str:
    return out.split(f"--- {name} ---\n", 1)[1].rsplit("=== END", 1)[0]


class LinksFirstInProcess(unittest.TestCase):
    """In process, so it also runs on Windows."""

    def test_a_text_without_the_line_is_unchanged(self):
        self.assertEqual(commands._links_first(MAIN), MAIN)
        self.assertEqual(commands._links_first(""), "")

    def test_the_line_follows_the_front_matter_once(self):
        text = commands._links_first(MAIN + "\n" + LINKS + "\n")
        self.assertTrue(text.startswith("---\nname: deck-design\n---\n\n" + LINKS + "\n\n# Deck design\n"), text[:300])
        self.assertEqual(text.count(LINKS), 1)
        self.assertTrue(text.endswith("Last words.\n"), "the blank lines that set it apart go with it")

    def test_without_front_matter_it_is_the_first_line(self):
        text = commands._links_first("# Deck design\n\nWords.\n\n" + LINKS + "\n")
        self.assertEqual(text, LINKS + "\n\n# Deck design\n\nWords.\n")

    def test_a_body_cut_for_length_says_so_and_keeps_the_line(self):
        long_main = LINKS + "\n\n# Deck design\n\n" + "".join(f"a line of the method {i:04d} " + "z" * 50 + "\n" for i in range(420))
        out = commands._bounded(long_main, "run theodore guide deck-design")
        lines = out.splitlines()
        self.assertLessEqual(len(out), commands.SKILL_BODY_MAX_CHARS)
        self.assertTrue(lines[0].startswith("[This document is longer than one answer holds and is cut near its end."), lines[0])
        self.assertEqual(lines[2], LINKS)
        self.assertTrue(lines[-1].startswith("[The rest of this document: run theodore guide deck-design]"), lines[-1])
        self.assertEqual(commands._bounded("short\n", "anything"), "short\n", "a text within the limit is unchanged")


class TheLineOnLinksComesFirst(ClientCase):
    CC = {"CLAUDECODE": "1"}

    def test_in_the_main_text_it_follows_the_front_matter(self):
        self.sign_in()
        self.api.guides["deck-design"][""] = MAIN
        self.api.guide_whole_tail = {"": "\n" + LINKS + "\n"}
        out = self.client("guide", "deck-design", env=self.CC).stdout
        text = body_of(out, "SKILL.md")
        self.assertTrue(text.startswith("---\nname: deck-design\n---\n\n" + LINKS + "\n\n# Deck design\n"), text[:400])
        self.assertEqual(text.count(LINKS), 1)
        self.assertTrue(text.rstrip("\n").endswith("Last words."), "and the text ends where it ended")

    def test_in_the_skill_body_it_is_the_first_line(self):
        self.sign_in()
        self.api.guides["deck-design"][""] = MAIN
        self.api.guide_whole_tail = {"": "\n" + LINKS + "\n"}
        out = self.client("skill", "deck-design").stdout
        self.assertTrue(out.startswith(LINKS + "\n\n# Deck design\n"), out[:300])
        self.assertEqual(out.count(LINKS), 1)

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


if __name__ == "__main__":
    unittest.main()
