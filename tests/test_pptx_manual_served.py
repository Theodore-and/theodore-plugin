"""The pptx manual served by `guide pptx <name>` (0.4.22, for engines from 1.0.0-rc.29), and a set-up that another
assistant on the computer cannot take.

The owner's own first deck (2026-11-21): the assistant asked for the build API by its file name, got "no such
section", read the file from the engine's folder in one long command and was shown its first 2 KB. And his set-up
ended RETRY, without its welcome, because an old Codex on the computer could not take the plugin.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from support import ClientCase, IS_WINDOWS, status_lines  # noqa: F401  (support puts the client on sys.path)

from theodore_client import commands  # noqa: E402


class Paging(unittest.TestCase):
    def test_a_part_ends_where_a_heading_begins(self):
        blocks = ["intro line\n" * 30] + [f"## Section {i}\n" + (f"line of section {i}\n" * 60) for i in range(1, 9)]
        text = "".join(blocks)
        parts = commands.split_parts(text, limit=3000)
        self.assertEqual("".join(parts), text, "the parts joined give the text back")
        self.assertGreater(len(parts), 2)
        for part in parts[1:]:
            self.assertTrue(part.startswith("## Section "), "every later part starts at a heading")
        self.assertTrue(all(len(part) <= 3000 for part in parts))

    def test_one_long_block_and_a_text_without_headings_are_cut_on_line_ends(self):
        long_block = "## Long\n" + "a line of the long block\n" * 400
        text = "## Short\nshort\n" + long_block + "## After\nafter\n"
        parts = commands.split_parts(text, limit=2000)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part) <= 2000 and part.endswith("\n") for part in parts))
        plain = "no heading here\n" * 500
        self.assertEqual("".join(commands.split_parts(plain, limit=1000)), plain)
        self.assertEqual(commands.split_parts("short", limit=1000), ["short"])


class ServedManual(ClientCase):
    MANUAL = ("---\nname: pptx\n---\n\n# PPTX Skill\n\nServed manual text. The build API is in [pptxgenjs.md](pptxgenjs.md); charts in "
              "`references/pptxgenjs-charts.md`; parts in [slide parts](references/slide-parts.md).\n")

    def _engine_without_manual_files(self) -> Path:
        self.ready()
        docs = self.box.home / "engines" / self.ENGINE_VERSION / "skills" / "pptx"
        for name in ("SKILL.md", "pptxgenjs.md"):
            (docs / name).unlink()
        sections = self.api.guides.setdefault("pptx", {})
        sections["manual"] = self.MANUAL
        return docs

    def test_an_engine_without_manual_files_gets_the_manual_from_the_service(self):
        docs = self._engine_without_manual_files()
        result = self.client("guide", "pptx")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Served manual text.", result.stdout)
        self.assertIn("guide pptx <name>", result.stdout, "the binding names the command, not a folder")
        self.assertNotIn("<pptx-skill-dir>", result.stdout)
        self.assertNotIn(str(docs), result.stdout, "no folder of the engine is given to read from")
        for name in ("pptxgenjs", "pptxgenjs-charts", "slide-parts"):
            self.assertIn(f"guide pptx {name}", result.stdout, "each pointer became the command that prints the section")
        self.assertNotIn("(pptxgenjs.md)", result.stdout, "no link leads to a file any more")
        self.assertNotIn("references/", result.stdout)
        loaded = self.client("skill", "pptx")
        self.assertIn("Served manual text.", loaded.stdout, "the skill's load prints the same manual")
        self.assertNotIn("<pptx-skill-dir>", loaded.stdout)

    def test_an_engine_that_brought_its_manual_reads_it_as_before(self):
        self.ready()
        result = self.client("guide", "pptx")
        self.assertIn("Fake manual for engine", result.stdout)
        self.assertIn("<pptx-skill-dir>", result.stdout)
        self.assertNotIn("guide pptx <name>", result.stdout)

    def test_a_section_comes_in_parts_cut_at_its_headings_with_its_pointers_as_commands(self):
        self._engine_without_manual_files()
        text = "# Build API\n\nSee `references/pptxgenjs-tables.md` and [images](references/pptxgenjs-images.md), and [back](../pptxgenjs.md).\n" + \
               "".join(f"## Helper {i}\n" + (f"what helper {i} does, line by line\n" * 220) for i in range(1, 7))
        self.api.guides["pptx"]["pptxgenjs"] = text
        first = self.client("guide", "pptx", "pptxgenjs", "--part", "1")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("part 1/", first.stdout)
        for name in ("pptxgenjs-tables", "pptxgenjs-images", "pptxgenjs"):
            self.assertIn(f"guide pptx {name}", first.stdout)
        self.assertNotIn(".md", first.stdout.split("# Build API", 1)[1].split("## Helper 1", 1)[0])
        second = self.client("guide", "pptx", "pptxgenjs", "--part", "2")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        body = [l for l in second.stdout.splitlines() if l.startswith("## Helper ") or l.startswith("what helper ")]
        self.assertTrue(body and body[0].startswith("## Helper "), "part 2 starts at a heading, not in the middle of a block")
        by_name = self.client("guide", "pptx", "pptxgenjs.md", "--part", "1")
        self.assertIn("# Build API", by_name.stdout, "asked for by its file name, the section is the same")

    def test_an_unknown_name_lists_the_sections(self):
        self._engine_without_manual_files()
        self.api.guide_index = {"version": "1.0.0", "sections": [{"name": "manual"}, {"name": "pptxgenjs"}, {"name": "slide-parts"}], "specimens": []}
        result = self.client("guide", "pptx", "helpers")
        self.assertStatus(result, "USAGE", 64)
        first, nxt, _tell = status_lines(result.stdout)
        self.assertIn("Sections: pptxgenjs, slide-parts.", first)
        self.assertNotIn("manual,", first, "the main text is `guide pptx` alone, not a name to ask for")
        self.assertIn("guide pptx <section>", nxt)


class AnotherAssistantCannotTakeThePlugin(ClientCase):
    def _tools(self, codex_fails: bool = True) -> None:
        bin_dir = self.box.tmp / "fakebin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text('#!/bin/sh\ncase "$*" in "plugin list --json") echo "[]";; esac\nexit 0\n')
        codex = bin_dir / "codex"
        codex.write_text("#!/bin/sh\necho \"error: unexpected argument 'marketplace' found\" >&2\necho \"For more information, try '--help'.\" >&2\nexit 2\n"
                         if codex_fails else '#!/bin/sh\nexit 0\n')
        for script in (claude, codex):
            script.chmod(0o755)
        self.box.env["PATH"] = f"{bin_dir}{os.pathsep}{self.box.env['PATH']}"
        plugin = self.box.home / "plugin"
        (plugin / ".agents" / "plugins").mkdir(parents=True, exist_ok=True)
        (plugin / ".agents" / "plugins" / "marketplace.json").write_text("{}")

    @unittest.skipIf(IS_WINDOWS, "the fake assistants are shell scripts; the rule itself is not per platform")
    def test_from_claude_code_an_old_codex_does_not_fail_the_set_up(self):
        self._tools()
        result = self.client("hosts", "register", env={"CLAUDECODE": "1"})
        self.assertStatus(result, "OK", 0)
        self.assertIn("claude-code: registered", result.stdout)
        self.assertIn("codex: not added (", result.stdout)
        self.assertNotIn("FAILED", result.stdout)
        _first, nxt, tell = status_lines(result.stdout)
        self.assertEqual(nxt, "NEXT: continue.")
        self.assertIn("could not be added to Codex", tell)
        self.assertNotIn("by hand", result.stdout, "nothing sends the assistant off to repair the other one")

    @unittest.skipIf(IS_WINDOWS, "the fake assistants are shell scripts")
    def test_run_by_a_person_one_assistant_taking_it_is_enough(self):
        self._tools()
        result = self.client("hosts", "register")
        self.assertStatus(result, "OK", 0)
        self.assertIn("codex: not added (", result.stdout)

    @unittest.skipIf(IS_WINDOWS, "the fake assistants are shell scripts")
    def test_the_calling_assistants_own_failure_is_still_a_failure(self):
        self._tools()
        result = self.client("hosts", "register", env={"CODEX_THREAD_ID": "01a1df00-test"})
        self.assertStatus(result, "RETRY", 75)
        self.assertIn("codex: FAILED (", result.stdout)


if __name__ == "__main__":
    unittest.main()
