"""The routing block in the assistants' global instructions (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ClientCase, IS_WINDOWS  # noqa: F401  (support puts the client on sys.path)

from theodore_client import core, routing


def _blocks(text: str) -> int:
    return text.count(routing.BEGIN)


class BlockFile(unittest.TestCase):
    """The writer and remover on one file, no client process."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="theodore-routing-"))
        self.path = self.dir / "CLAUDE.md"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_creates_the_file_with_only_the_block_and_removes_it_again(self):
        what, created = routing.write(self.path)
        self.assertEqual((what, created), ("written", True))
        self.assertEqual(self.path.read_text(), routing.block())
        self.assertIsNone(routing.remove(self.path, created=True))
        self.assertFalse(self.path.exists())

    def test_appends_after_the_users_text_and_takes_back_exactly_that(self):
        original = "# My rules\n\nAlways answer in French.\n"
        self.path.write_text(original)
        self.assertEqual(routing.write(self.path), ("written", False))
        text = self.path.read_text()
        self.assertTrue(text.startswith(original))
        self.assertIn("\n\n" + routing.BEGIN, text)
        self.assertIsNone(routing.remove(self.path, created=False))
        self.assertEqual(self.path.read_text(), original)

    def test_no_trailing_newline_is_kept_byte_for_byte(self):
        self.path.write_text("one line, no newline")
        routing.write(self.path)
        routing.remove(self.path, created=False)
        self.assertEqual(self.path.read_text(), "one line, no newline\n")      # only the line end Theodore needed stays

    def test_idempotent(self):
        self.path.write_text("mine\n")
        routing.write(self.path)
        once = self.path.read_bytes()
        self.assertEqual(routing.write(self.path), ("already there", False))
        self.assertEqual(self.path.read_bytes(), once)
        self.assertEqual(_blocks(self.path.read_text()), 1)

    def test_old_wording_is_replaced_in_place_and_text_around_it_is_untouched(self):
        self.path.write_text(f"before\n\n{routing.BEGIN}\nold wording\n{routing.END}\nafter\n")
        self.assertEqual(routing.write(self.path), ("updated", False))
        text = self.path.read_text()
        self.assertEqual(text, f"before\n\n{routing.block()}after\n")
        self.assertNotIn("old wording", text)

    def test_crlf_files_stay_crlf(self):
        self.path.write_bytes(b"mine\r\n")
        routing.write(self.path)
        raw = self.path.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        routing.remove(self.path, created=False)
        self.assertEqual(self.path.read_bytes(), b"mine\r\n")

    def test_a_marker_without_its_pair_is_never_touched(self):
        broken = f"mine\n{routing.BEGIN}\nhalf a block\n"
        self.path.write_text(broken)
        what, _created = routing.write(self.path)
        self.assertTrue(what.startswith("not changed"))
        self.assertEqual(self.path.read_text(), broken)
        self.assertIn("without its pair", routing.remove(self.path, created=False))
        self.assertEqual(self.path.read_text(), broken)

    def test_a_created_file_the_user_added_to_is_kept(self):
        _w, created = routing.write(self.path)
        with open(self.path, "a") as handle:
            handle.write("\nMy own line.\n")
        routing.remove(self.path, created=created)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_text().strip(), "My own line.")

    @unittest.skipIf(IS_WINDOWS, "symlinks need a privilege on Windows")
    def test_a_symlinked_file_is_edited_where_it_points(self):
        real = self.dir / "dotfiles-CLAUDE.md"
        real.write_text("mine\n")
        self.path.symlink_to(real)
        routing.write(self.path)
        self.assertTrue(self.path.is_symlink(), "the link stays a link")
        self.assertIn(routing.TEXT, real.read_text())
        routing.remove(self.path, created=False)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(real.read_text(), "mine\n")

    def test_not_utf8_is_left_alone(self):
        self.path.write_bytes(b"\xff\xfe binary")
        self.assertTrue(routing.write(self.path)[0].startswith("not changed"))
        self.assertEqual(self.path.read_bytes(), b"\xff\xfe binary")

    def test_ensure_skips_a_block_the_user_deleted_but_register_writes_it_again(self):
        claude_dir = self.dir / ".claude"
        claude_dir.mkdir()
        # A home of its own: codex is also looked for where its installers put it (~/.nvm, ~/.local/bin, ...).
        env = {"CLAUDE_CONFIG_DIR": str(claude_dir), "CODEX_HOME": str(self.dir / "no-codex"), "PATH": "",
               "HOME": str(self.dir), "USERPROFILE": str(self.dir), "THEODORE_HOME": str(self.dir / "theodore")}
        with mock.patch.dict(os.environ, env), mock.patch.object(core, "_HOME", None), \
                mock.patch.object(core, "is_loopback_api", lambda: True):
            record, lines = routing.ensure({})
            self.assertEqual(len(lines), 1)
            target = claude_dir / "CLAUDE.md"
            self.assertTrue(record["instructions"]["claude"]["file_created"])
            self.assertNotIn("codex", record["instructions"], "no Codex folder and no codex command: nothing written there")
            target.write_text("I removed Theodore's lines.\n")
            again, lines = routing.ensure(record)
            self.assertEqual(lines, [])
            self.assertNotIn(routing.BEGIN, target.read_text())
            forced, lines = routing.ensure(record, register=True)
            self.assertIn(routing.BEGIN, target.read_text())
            self.assertTrue(forced["instructions"]["claude"]["file_created"], "still the file Theodore created")


class BlockThroughTheClient(ClientCase):
    """The client's commands: register writes it, guide/doctor write it for a marketplace-only install, uninstall
    takes it back."""

    def _fake_tools(self) -> None:
        bin_dir = self.box.tmp / "fakebin"
        bin_dir.mkdir()
        for name in ("claude", "codex"):
            script = bin_dir / name
            script.write_text('#!/bin/sh\ncase "$*" in "plugin list --json") echo "[]";; esac\nexit 0\n')
            script.chmod(0o755)
        self.box.env["PATH"] = f"{bin_dir}{os.pathsep}{self.box.env['PATH']}"
        plugin = self.box.home / "plugin"
        (plugin / ".agents" / "plugins").mkdir(parents=True, exist_ok=True)
        (plugin / ".agents" / "plugins" / "marketplace.json").write_text("{}")

    def _files(self) -> tuple[Path, Path]:
        return Path(self.box.env["CLAUDE_CONFIG_DIR"]) / "CLAUDE.md", Path(self.box.env["CODEX_HOME"]) / "AGENTS.md"

    def test_register_writes_both_and_uninstall_takes_back_exactly_the_block(self):
        self._fake_tools()
        claude_md, agents_md = self._files()
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text("# The user's own instructions\n")
        result = self.client("hosts", "register")
        self.assertStatus(result, "OK", 0)
        self.assertIn(f"claude instructions: Theodore added this line to {claude_md}", result.stdout)
        self.assertIn(routing.TEXT, result.stdout, "the installer shows the sentence it writes")
        self.assertIn(routing.TEXT, claude_md.read_text())
        self.assertEqual(agents_md.read_text(), routing.block(), "AGENTS.md created with only the block")
        record = self.state("state/host-settings.json")
        self.assertEqual(record["instructions"]["claude"], {"path": str(claude_md), "version": routing.VERSION, "file_created": False})
        self.assertTrue(record["instructions"]["codex"]["file_created"])
        again = self.client("hosts", "register")
        self.assertNotIn("instructions:", again.stdout, "a second register changes nothing")
        self.assertEqual(_blocks(claude_md.read_text()), 1)
        self.ready()
        self.client("uninstall")
        self.assertEqual(claude_md.read_text(), "# The user's own instructions\n")
        self.assertFalse(agents_md.exists(), "the AGENTS.md Theodore created and nobody added to is deleted")

    def test_hosts_unregister_takes_the_block_back_too(self):
        self._fake_tools()
        claude_md, agents_md = self._files()
        self.client("hosts", "register")
        self.assertTrue(claude_md.exists())
        self.assertStatus(self.client("hosts", "unregister"), "OK", 0)
        self.assertFalse(claude_md.exists())
        self.assertFalse(agents_md.exists())
        self.assertNotIn("instructions", self.state("state/host-settings.json"))

    def test_a_marketplace_only_install_gets_it_at_the_first_doctor_or_guide(self):
        claude_md, agents_md = self._files()
        claude_md.parent.mkdir(parents=True)            # Claude Code's folder, no command on PATH (the desktop app)
        self.ready()
        self.client("doctor")
        self.assertIn(routing.TEXT, claude_md.read_text())
        self.assertFalse(agents_md.exists(), "no Codex on this computer")
        claude_md.write_text("")                        # the user removes it: not written again by later runs
        self.client("guide", "pptx")
        self.assertEqual(claude_md.read_text(), "")
        self.client("uninstall")
        self.assertTrue(claude_md.exists(), "an emptied file the user kept is not Theodore's to delete")

    def test_uninstall_keep_hosts_still_takes_the_block_back(self):
        self._fake_tools()
        claude_md, _agents = self._files()
        self.client("hosts", "register")
        self.ready()
        self.client("uninstall", "--keep-hosts")
        self.assertFalse(claude_md.exists())


if __name__ == "__main__":
    unittest.main()
