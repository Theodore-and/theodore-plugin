"""0.4.24: specimen sheets for an assistant that was started in the user's own folder.

A terminal opens in the user's folder, and on Windows that folder contains every place a specimen folder may go: the
temporary folder is %LOCALAPPDATA%\\Temp and Theodore's cache is under %LOCALAPPDATA% too. The rule "never inside the
working folder" then refused every sheet (BLOCKED_BY_POLICY) and the designer was told to go on without them. Seen on
2026-11-22 on the Windows test machine, from C:\\Users\\<name>. From that folder itself, and only when no other place is
possible, Theodore's own cache folder is used. And a text of the guidance asked for as a sheet
(`specimen worked-04.md`) is answered with the command that reads it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from support import ClientCase, IS_WINDOWS  # noqa: F401  (support puts the client on sys.path)

JPEG = b"\xff\xd8\xff\xe0" + b"sheet" * 40 + b"\xff\xd9"


class SheetsFromTheUsersOwnFolder(ClientCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {"cover-01.jpg": JPEG}
        home, env = self.box.user_home, self.box.env
        self.cache = (home / "Library" / "Caches" / "Theodore" / "specimens" if sys.platform == "darwin" else
                      Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "theodore" / "specimens")
        # as on Windows: the temporary folder is inside the user's folder as well
        self.temp = home / "AppData" / "Local" / "Temp"
        self.temp.mkdir(parents=True)

    def sheet(self, cwd: Path):
        return self.client("specimen", "cover-01.jpg", cwd=cwd, env={"TMPDIR": str(self.temp)})

    def test_started_in_the_users_folder_the_sheet_goes_to_theodores_own_cache(self):
        result = self.sheet(self.box.user_home)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = Path(result.stdout.strip().splitlines()[0])
        self.assertEqual(path.read_bytes(), JPEG)
        self.assertIn(self.cache.resolve(), path.resolve().parents, "in the cache folder made for sheets")
        self.assertEqual([p for p in self.temp.iterdir() if p.name.startswith("theodore-specimens-")], [], "not in the temporary folder")
        if not IS_WINDOWS:
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700, "private to the user, as everywhere")

    def test_with_a_temporary_folder_outside_the_users_folder_that_one_is_used_as_before(self):
        # macOS and Linux: the cache is inside the home, the temporary folder is not
        result = self.client("specimen", "cover-01.jpg", cwd=self.box.user_home)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = Path(result.stdout.strip().splitlines()[0])
        self.assertIn(Path(self.box.env["TMPDIR"]).resolve(), path.resolve().parents)
        self.assertNotIn(self.cache.resolve(), path.resolve().parents)

    def test_a_folder_above_the_users_that_holds_every_place_is_still_refused(self):
        inside = self.box.user_home.parent / "tmp-above"
        inside.mkdir()
        result = self.client("specimen", "cover-01.jpg", cwd=self.box.user_home.parent, env={"TMPDIR": str(inside)})
        if result.returncode == 0:              # another temporary folder of the system was used: never under that folder
            path = Path(result.stdout.strip().splitlines()[0])
            self.addCleanup(lambda: __import__("shutil").rmtree(path.parent, True))
            self.assertNotIn(self.box.user_home.parent.resolve(), path.resolve().parents)
        else:
            self.assertStatus(result, "BLOCKED_BY_POLICY", 73)
        self.assertEqual(list(self.box.user_home.parent.rglob("*.jpg")), [])

    def test_a_project_folder_that_holds_the_cache_and_the_temporary_folder_is_still_refused(self):
        # not the user's own folder: a folder inside it that happens to contain both places
        project = self.cache.parents[2] if sys.platform == "darwin" else self.cache.parents[1]
        inside = project / "tmp-of-the-project"
        inside.mkdir(parents=True)
        result = self.client("specimen", "cover-01.jpg", cwd=project, env={"TMPDIR": str(inside)})
        if result.returncode == 0:              # another temporary folder of the system was used: never the project
            path = Path(result.stdout.strip().splitlines()[0])
            self.addCleanup(lambda: __import__("shutil").rmtree(path.parent, True))
            self.assertNotIn(project.resolve(), path.resolve().parents)
        else:
            self.assertStatus(result, "BLOCKED_BY_POLICY", 73)
        self.assertEqual(list(project.rglob("*.jpg")), [], "no sheet inside a project folder")


class ATextAskedForAsASheet(ClientCase):
    def test_the_answer_names_the_command_that_reads_the_text(self):
        self.sign_in()
        launcher = f'"{self.box.home / "bin" / "theodore"}"'
        for asked in ("worked-04-bridge.md", "references/worked-04-bridge.md"):
            first, nxt, _tell = self.assertStatus(self.client("specimen", asked), "USAGE", 64)
            self.assertIn("is a text of the guidance, not a specimen sheet", first)
            self.assertIn(f"{launcher} guide deck-design worked-04-bridge", nxt)
            self.assertIn(".jpg", nxt)
        self.assertEqual([r for r in self.api.requests if "/guide/specimens/" in r["path"]], [], "nothing was asked of the service")
        first, _nxt, _tell = self.assertStatus(self.client("specimen", "no such thing!"), "USAGE", 64)
        self.assertNotIn("text of the guidance", first, "any other wrong name is answered as before")


if __name__ == "__main__":
    unittest.main()
