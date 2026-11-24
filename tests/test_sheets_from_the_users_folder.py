"""0.4.24: specimen sheets for an assistant that was started in the user's own folder.

A terminal opens in the user's folder, and on Windows that folder contains every place a specimen folder could go
(the temporary folder is %LOCALAPPDATA%\\Temp, Theodore's cache is under %LOCALAPPDATA% too): until 0.4.24 every sheet
was refused there. Since contract C6 (24 November 2026) a shell's sheet is a transient file in Theodore's own folder
(<home>/work/scratch/sheets/<session>/) whatever the working folder, so from the user's folder, or from a folder above
it, the sheet comes, and nothing lands anywhere else. And a text of the guidance asked for as a sheet
(`specimen worked-04.md`) is answered with the command that reads it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from support import ClientCase  # (support puts the client on sys.path)

JPEG = b"\xff\xd8\xff\xe0" + b"sheet" * 40 + b"\xff\xd9"


class SheetsFromTheUsersOwnFolder(ClientCase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {"cover-01.jpg": JPEG}
        self.temp = self.box.user_home / "AppData" / "Local" / "Temp"      # as on Windows: inside the user's folder
        self.temp.mkdir(parents=True)

    def test_from_the_users_folder_or_above_it_the_sheet_goes_to_theodores_own_folder(self):
        for cwd in (self.box.user_home, self.box.user_home.parent):
            result = self.client("specimen", "cover-01.jpg", cwd=cwd, env={"TMPDIR": str(self.temp)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            path = Path(result.stdout.strip().splitlines()[0])
            self.assertEqual(path.read_bytes(), JPEG)
            self.assertEqual(path.parent.parent, (self.box.home / "work" / "scratch" / "sheets").resolve())
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700, "private to the user, as everywhere")
            self.assertEqual(list(self.temp.iterdir()), [], "not in the temporary folder")
            outside = [p for p in self.box.user_home.parent.rglob("*.jpg") if self.box.home.resolve() not in p.resolve().parents]
            self.assertEqual(outside, [], "nothing anywhere else")


class ATextAskedForAsASheet(ClientCase):
    def test_the_answer_names_the_command_that_reads_the_text(self):
        self.sign_in()
        launcher = f'"{self.box.home / "bin" / "theodore"}"'
        for asked in ("worked-04-bridge.md", "references/worked-04-bridge.md"):
            first, nxt, _tell = self.assertStatus(self.client("specimen", asked), "USAGE", 64)
            self.assertIn("is a text of the guidance, not a specimen sheet", first)
            self.assertIn(f"{launcher} guide deck-design worked-04-bridge", nxt)
            self.assertIn(".jpg", nxt)
        self.assertEqual([r for r in self.api.requests if "/guide/" in r["path"]], [], "nothing was asked of the service")
        first, _nxt, _tell = self.assertStatus(self.client("specimen", "no such thing!"), "USAGE", 64)
        self.assertNotIn("text of the guidance", first, "any other wrong name is answered as before")


if __name__ == "__main__":
    unittest.main()
