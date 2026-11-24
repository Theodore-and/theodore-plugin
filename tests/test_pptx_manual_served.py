"""Every text of the pptx manual read through `guide pptx <name>`, in parts that fit one answer (0.4.22): served by
the service for engines from 1.0.0-rc.29, read from the engine's own files before that. And a set-up that another
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

    def test_a_long_first_section_gives_two_even_parts_not_a_short_opening_and_two_more(self):
        """On a Mac the build API came as three parts, the first one its opening paragraph alone (1,463 characters with
        the header): the first section was just over a part, so the opening was sent by itself."""
        text = "intro\n" * 60 + "## Big\n" + ("a line of the big section\n" * 930) + "## Next\n" + ("next line\n" * 1800)
        parts = commands.split_parts(text, limit=24000)
        self.assertEqual("".join(parts), text)
        self.assertEqual(len(parts), 2, "the fewest parts that hold the text")
        self.assertTrue(all(8000 < len(part) <= 24000 for part in parts), [len(part) for part in parts])
        self.assertEqual(commands.split_parts("x" * 5000, limit=2000), ["x" * 2000, "x" * 2000, "x" * 1000])

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

    # An engine up to 1.0.0-rc.28 brought its manual as files. The assistant reaches them the same way, through the
    # launcher: the owner's assistant read the build API with cat, 34 KB in one answer, and was shown its first 2 KB.
    BUILD_API = "# Build API\n\nTables are in `references/pptxgenjs-tables.md`.\n" + \
        "".join(f"## Helper {i}\n" + (f"what helper {i} does, line by line\n" * 220) for i in range(1, 7))

    def _engine_with_manual_files(self) -> Path:
        self.ready()
        docs = self.box.home / "engines" / self.ENGINE_VERSION / "skills" / "pptx"
        (docs / "SKILL.md").write_text(self.MANUAL, encoding="utf-8")
        (docs / "pptxgenjs.md").write_text(self.BUILD_API, encoding="utf-8")
        (docs / "references").mkdir(exist_ok=True)
        (docs / "references" / "pptxgenjs-tables.md").write_text("# Tables\n\ntable text\n", encoding="utf-8")
        (docs / "served-entry.md").write_text("not a text to read\n", encoding="utf-8")
        return docs

    def test_an_engine_that_brought_its_manual_names_the_same_commands(self):
        docs = self._engine_with_manual_files()
        result = self.client("guide", "pptx")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Served manual text.", result.stdout)
        self.assertIn(f"<pptx-skill-dir> = {docs}", result.stdout)
        self.assertIn("guide pptx <name>", result.stdout, "the texts are read through the launcher, whatever the engine")
        self.assertNotIn("read pptxgenjs.md", result.stdout, "nothing sends the assistant to read a long file in one answer")
        for name in ("pptxgenjs", "pptxgenjs-charts", "slide-parts"):
            self.assertIn(f"guide pptx {name}", result.stdout)
        self.assertNotIn("(pptxgenjs.md)", result.stdout)

    def test_the_engines_own_file_comes_in_parts_that_fit_one_answer_also_in_claude_code(self):
        self._engine_with_manual_files()
        self.assertGreater(len(self.BUILD_API), 30000, "longer than Claude Code shows of one answer")
        first = self.client("guide", "pptx", "pptxgenjs", env={"CLAUDECODE": "1"})     # as the owner's assistant asked
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("part 1/2", first.stdout)
        self.assertLess(len(first.stdout), 30000, "one answer of the host holds the whole part")
        self.assertIn("guide pptx pptxgenjs-tables", first.stdout, "a pointer inside it is the command too")
        self.assertIn("--part 2", first.stdout, "and it says how to read on")
        second = self.client("guide", "pptx", "pptxgenjs", "--part", "2", env={"CLAUDECODE": "1"})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertLess(len(second.stdout), 30000)
        kept = lambda out: [l for l in out.splitlines() if l.startswith("## Helper ") or l.startswith("what helper ")]  # noqa: E731
        self.assertTrue(kept(second.stdout)[0].startswith("## Helper "), "part 2 starts at a heading")
        self.assertEqual(kept(first.stdout) + kept(second.stdout), kept(self.BUILD_API), "the two parts are the whole text, in order")
        reference = self.client("guide", "pptx", "references/pptxgenjs-tables.md", env={"CLAUDECODE": "1"})
        self.assertIn("table text", reference.stdout, "a reference by the name the manual used to give it")
        self.assertIn("part 1/1", reference.stdout)

    def test_the_engines_own_file_is_still_read_with_no_connection(self):
        self._engine_with_manual_files()
        self.api.down = True
        result = self.client("guide", "pptx", "pptxgenjs")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("# Build API", result.stdout)

    def test_a_name_that_is_no_text_lists_the_engines_own_texts(self):
        self._engine_with_manual_files()
        result = self.client("guide", "pptx", "helpers")
        self.assertStatus(result, "USAGE", 64)
        first, _nxt, _tell = status_lines(result.stdout)
        self.assertIn("pptxgenjs", first)
        self.assertIn("pptxgenjs-tables", first)
        self.assertNotIn("served-entry", first)
        self.assertNotIn("SKILL", first)
        (self.box.home / "engines" / self.ENGINE_VERSION / "skills" / "pptx" / "VENDOR_MANIFEST.md").write_text("notices\n", encoding="utf-8")
        for name in ("served-entry", "VENDOR_MANIFEST", "vendor_manifest"):
            self.assertStatus(self.client("guide", "pptx", name), "USAGE", 64)

    def test_the_main_text_by_its_name_is_the_manual(self):
        """An engine message of 1.0.0-rc.29 names it `theodore guide pptx manual`; a pointer in a text names SKILL.md."""
        self._engine_with_manual_files()
        for name in ("manual", "SKILL.md", "../SKILL.md"[3:]):
            result = self.client("guide", "pptx", name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Served manual text.", result.stdout, name)
            self.assertIn("=== END theodore pptx SKILL.md", result.stdout, name)
        self._engine_without_manual_files()
        served = self.client("guide", "pptx", "manual")
        self.assertIn("Served manual text.", served.stdout)
        self.assertIn("=== END theodore pptx SKILL.md", served.stdout, "the same document whether a file or served")

    def test_only_pointers_to_the_manuals_texts_become_commands(self):
        from theodore_client import commands as c
        text = ("Read [the build API](../pptxgenjs.md) and [the manual](../SKILL.md); see `pptxgenjs-advanced.md` and "
                "`references/translate.md`. Write `design.md`, keep `template.md`, and [the note](design.md) with `<part>/design.md`.")
        out = c._pptx_pointers(text)
        launcher = c.quoted_launcher()
        self.assertIn(f"[the build API](run: {launcher} guide pptx pptxgenjs)", out)
        self.assertIn(f"[the manual](run: {launcher} guide pptx manual)", out)
        self.assertIn(f"`{launcher} guide pptx pptxgenjs-advanced`", out)
        self.assertIn(f"`{launcher} guide pptx translate`", out)
        for kept in ("`design.md`", "`template.md`", "[the note](design.md)", "`<part>/design.md`"):
            self.assertIn(kept, out, "a deck's own file is not a text of the manual")

    def test_the_plugins_guide_tool_reads_the_engines_own_file_whole(self):
        from test_mcp import Server
        self._engine_with_manual_files()
        server = Server(self)
        self.addCleanup(lambda: server.proc.poll() is None and server.proc.kill())
        server.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        server.send("notifications/initialized", notify=True)
        answer = server.call("guide", skill="pptx", section="pptxgenjs")
        self.assertFalse(answer.get("isError"), answer)
        self.assertEqual(answer["content"][0]["text"], self.BUILD_API, "a tool result is not cut by the host: the text comes whole")
        manual = server.call("guide", skill="pptx")["content"][0]["text"]
        self.assertIn('[pptxgenjs.md](the Theodore tool guide with skill "pptx" and section "pptxgenjs")', manual,
                      "a reader with the plugin's tools: the manual's pointers name the guide tool")
        self.assertNotIn("guide pptx pptxgenjs", manual, "never a shell command where the tool is there")
        missing = server.call("guide", skill="pptx", section="helpers")
        self.assertTrue(missing["isError"])
        self.assertIn("pptxgenjs", missing["content"][0]["text"].splitlines()[0])
        server.close()

    def test_a_chat_keeps_reading_the_engines_own_files_with_its_own_tool(self):
        from test_mcp import Server
        self._engine_with_manual_files()
        server = Server(self, args=("--for", "claude-desktop"))
        self.addCleanup(lambda: server.proc.poll() is None and server.proc.kill())
        server.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        server.send("notifications/initialized", notify=True)
        manual = server.call("theodore", action="manual")["content"][0]["text"]
        self.assertIn("[pptxgenjs.md](pptxgenjs.md)", manual, "a chat has no launcher: the manual's links stay links to files")
        self.assertIn('with action "read"', manual)
        self.assertNotIn("guide pptx pptxgenjs", manual)
        server.close()

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
