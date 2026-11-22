"""0.4.24: two things found on 2026-11-22 while a first-user journey was replayed on a wiped Windows computer.

A computer that leaves the account says so. `theodore logout` and `theodore uninstall` deleted the sign-in on the
computer and told the service nothing, so the account kept listing the computer as logged in; the test account met
its limit of ten computers after ten installs, each of them uninstalled.

The Claude app for Windows moved its data. Since its update of November 2026 it keeps its configuration file in
%LOCALAPPDATA%\\Claude-Data\\<its package family> and leaves a file `.user-data-left.<family>` where the data was. A
set-up wrote Theodore's entry into the folder the app had left, where nothing reads it, and told the user the Claude
app was registered; an uninstall left the entry in the file the app had carried to its new folder.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ClientCase, IS_WINDOWS  # noqa: F401  (support puts the client on sys.path)

from theodore_client import core, desktop  # noqa: E402

RELEASE = "/v1/installation/release"


class AComputerThatLeavesSaysSo(ClientCase):
    def releases(self) -> list[dict]:
        return [request for request in self.api.requests if request["path"] == RELEASE]

    def test_logout_tells_the_service_before_the_sign_in_goes(self):
        self.sign_in()
        installation = next(iter(self.api.installations))
        self.assertStatus(self.client("logout"), "OK", 0)
        self.assertEqual(self.api.released, [installation], "the computer released itself")
        self.assertEqual([(r["method"], r["auth"]) for r in self.releases()], [("POST", True)], "once, with its own sign-in")
        self.assertStatus(self.client("whoami"), "SIGN_IN_REQUIRED", 65)

    def test_uninstall_tells_the_service(self):
        self.sign_in()
        installation = next(iter(self.api.installations))
        self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertEqual(self.api.released, [installation])
        self.assertFalse(self.box.home.exists(), "and Theodore is gone from the computer as before")

    def test_a_service_without_the_route_or_no_connection_stops_neither(self):
        self.sign_in()
        self.api.release_route = False                       # a service from before 0.4.24's route
        self.assertStatus(self.client("logout"), "OK", 0)
        self.assertEqual(self.api.released, [])
        self.assertEqual(len(self.releases()), 1, "it was asked, and its 'no such route' changed nothing")
        self.api.issue_activation_code("WXYZ-5678")
        self.assertStatus(self.client("login", "--code", "WXYZ-5678"), "OK", 0)
        self.api.down = True                                 # no connection at all
        started = time.monotonic()
        self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertLess(time.monotonic() - started, 40, "a few seconds for the call, never a wait for the network")
        self.assertFalse(self.box.home.exists())

    def test_a_computer_that_was_never_signed_in_sends_nothing(self):
        self.assertStatus(self.client("logout"), "OK", 0)
        self.assertEqual(self.releases(), [])


class TheClaudeAppMovedItsData(unittest.TestCase):
    """The Windows rules, with this computer's folders stood in by a temporary one (the client suite itself does not
    run on Windows; these are the module's own functions under core.IS_WINDOWS)."""

    FAMILY = "Claude_pzs8sxrjxfjjc"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theodore-moved-"))
        self.addCleanup(lambda: core.remove_tree(self.tmp))
        self.local = self.tmp / "AppData" / "Local"
        self.roaming = self.tmp / "AppData" / "Roaming"
        self.old = self.local / "Packages" / self.FAMILY / "LocalCache" / "Roaming" / "Claude"      # where the app kept its data
        self.new = self.local / "Claude-Data" / self.FAMILY.lower()                                  # where it keeps it now
        self.launcher = str(self.tmp / "home" / "bin" / "theodore.cmd")
        patches = [mock.patch.object(core, "IS_WINDOWS", True),
                   mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.local), "APPDATA": str(self.roaming), "COMSPEC": "cmd.exe"}),
                   mock.patch.object(core, "home", lambda: mock.Mock(launcher=lambda: self.launcher)),
                   mock.patch.object(core, "write_atomic", lambda path, text, mode=None: Path(path).write_text(text, encoding="utf-8"))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def config(self, folder: Path, value: dict | None = None) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "claude_desktop_config.json"
        if value is not None:
            path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def leave(self, folder: Path) -> None:
        """What the app does to the folder it moves out of: nothing stays but its marker."""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f".user-data-left.{self.FAMILY.lower()}").write_text('{"v":1}', encoding="utf-8")

    def servers(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}

    def test_before_the_move_the_package_folder_is_the_apps_folder(self):
        self.config(self.old, {"preferences": {}})
        self.assertEqual(desktop.config_dirs(), [self.old])
        record: dict = {}
        self.assertEqual(desktop.register(record), ["claude-desktop: registered (it loads when the Claude app is quit and opened again)"])
        self.assertIn("theodore", self.servers(self.old / "claude_desktop_config.json"))

    def test_after_the_move_the_entry_goes_where_the_app_reads_and_not_where_it_was(self):
        self.leave(self.old)
        new = self.config(self.new, {"preferences": {"sidebarMode": "chat"}, "mcpServers": {"files": {"command": "npx"}}})
        self.assertEqual(desktop.config_dirs(), [self.new], "the folder the app left is not one of its folders")
        record: dict = {}
        lines = desktop.register(record)
        self.assertEqual(lines, ["claude-desktop: registered (it loads when the Claude app is quit and opened again)"])
        self.assertEqual(sorted(self.servers(new)), ["files", "theodore"])
        self.assertEqual(json.loads(new.read_text(encoding="utf-8"))["preferences"], {"sidebarMode": "chat"}, "nothing else in the file changed")
        self.assertFalse((self.old / "claude_desktop_config.json").exists(), "nothing is written into the folder the app left")
        self.assertEqual(list(record[desktop.RECORD_KEY]), [str(new)])
        self.assertTrue(desktop.entry_present())

    def test_the_roaming_folder_the_app_left_is_skipped_too(self):
        self.leave(self.roaming / "Claude")
        self.config(self.new, {})
        self.assertEqual(desktop.config_dirs(), [self.new])

    def test_the_app_never_installed_is_still_said(self):
        self.assertEqual(desktop.register({}), ["claude-desktop: not found on this computer"])

    def test_uninstall_takes_the_entry_back_from_the_file_the_app_carried_along(self):
        old = self.config(self.old, {"preferences": {}})
        record: dict = {}
        desktop.register(record)                                     # installed before the app moved
        carried = json.loads(old.read_text(encoding="utf-8"))
        old.unlink()
        self.leave(self.old)
        new = self.config(self.new, carried)                         # the app moved its file, Theodore's entry in it
        left, notes = [], []
        changed = desktop.unregister(record, left, notes)
        self.assertEqual(changed, [str(new)])
        self.assertEqual(json.loads(new.read_text(encoding="utf-8")), {"preferences": {}}, "the entry and the object Theodore made for it are gone")
        self.assertEqual((left, notes, record), ([], [], {}))

    def test_uninstall_leaves_an_entry_in_the_new_file_that_theodore_did_not_write(self):
        old = self.config(self.old, {})
        record: dict = {}
        desktop.register(record)
        old.unlink()
        self.leave(self.old)
        theirs = {"command": "C:\\\\tools\\\\other.exe", "args": ["serve"]}
        new = self.config(self.new, {"mcpServers": {"theodore": theirs}})
        self.assertEqual(desktop.unregister(record, [], []), [])
        self.assertEqual(self.servers(new), {"theodore": theirs})

    def test_an_install_made_after_the_move_is_put_right_at_the_next_command(self):
        # 0.4.23 and before: the package folder still exists (it holds the app's marker), so the entry was written there
        self.leave(self.old)
        new = self.config(self.new, {"preferences": {}})
        stale = self.config(self.old, {"mcpServers": {"theodore": desktop.entry()}})
        record = {desktop.RECORD_KEY: {str(stale): {"ours": True, "created_servers": True, "entry": desktop.entry(), "written_at": 1.0}}}
        self.assertTrue(desktop.left_behind(record))
        lines = desktop.follow(record)
        self.assertEqual(lines, ["claude-desktop: registered (it loads when the Claude app is quit and opened again)"])
        self.assertEqual(self.servers(new), {"theodore": desktop.entry()})
        self.assertFalse(stale.exists(), "the file Theodore alone had made in the folder nothing reads is gone")
        self.assertEqual(list(record[desktop.RECORD_KEY]), [str(new)])
        self.assertFalse(desktop.left_behind(record), "done once: the next command has nothing to follow")

    def test_following_keeps_what_else_is_in_the_old_file_and_an_entry_the_app_already_carried(self):
        self.leave(self.old)
        carried = {"mcpServers": {"theodore": desktop.entry(), "files": {"command": "npx"}}}
        new = self.config(self.new, carried)
        stale = self.config(self.old, {"mcpServers": {"theodore": desktop.entry(), "notes": {"command": "notes.exe"}}})
        record = {desktop.RECORD_KEY: {str(stale): {"ours": True, "entry": desktop.entry()}}}
        self.assertEqual(desktop.follow(record), ["claude-desktop: already registered"])
        self.assertEqual(json.loads(new.read_text(encoding="utf-8")), carried)
        self.assertEqual(self.servers(stale), {"notes": {"command": "notes.exe"}}, "only Theodore's entry left the old file")
        self.assertEqual(list(record[desktop.RECORD_KEY]), [str(new)])

    def test_nothing_is_followed_while_the_app_has_not_moved(self):
        old = self.config(self.old, {})
        record: dict = {}
        desktop.register(record)
        self.assertFalse(desktop.left_behind(record))
        self.assertFalse(desktop.left_behind({}))
        self.assertIn("theodore", self.servers(old))


if __name__ == "__main__":
    unittest.main()
