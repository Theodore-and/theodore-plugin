"""0.4.30: designers working side by side never remove each other's fresh specimen folder.

Seen on 2026-11-22 in a first-deck run: one designer's `specimen` call printed two paths, the first opened, and less
than a second later the second was gone; in that moment another designer, working in a folder that holds every place
a sheet may go, was refused with BLOCKED_BY_POLICY. Every status of that name removed every specimen folder on the
computer (core.emit, meant for a kill switch or a refusal of the account), so that local refusal emptied the folders of
the calls running beside it. A second path did the same: a call that failed after it had written a sheet removed its
folder, although another call may have reused that sheet (held()) and printed its path a moment before.
"""

from __future__ import annotations

import contextlib
import email.message
import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase, IS_WINDOWS  # noqa: F401,E402  (support puts the client on sys.path)
from test_hardening import client_home  # noqa: E402

from theodore_client import account, commands, core, specimens  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0 stand-in sheet \xff\xd9"


def answer(body: bytes, sha: str | None = None):
    headers = email.message.Message()
    headers["Content-Type"] = "image/jpeg"
    headers["X-Theodore-Sha256"] = sha or hashlib.sha256(body).hexdigest()
    return 200, headers, body


class InProcess(unittest.TestCase):
    """In process, so it also runs on Windows."""

    @contextlib.contextmanager
    def root(self):
        root = Path(tempfile.mkdtemp(prefix="theodore-side-by-side-tmp-")).resolve()
        work = Path(tempfile.mkdtemp(prefix="theodore-side-by-side-cwd-")).resolve()
        before = os.getcwd()
        os.chdir(work)
        try:
            with client_home(), mock.patch.object(specimens, "roots", return_value=[root]):
                core.home().ensure()
                yield root
        finally:
            os.chdir(before)
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)

    def fetch(self, names, fake):
        out = []
        with mock.patch.object(account, "require", return_value={}), mock.patch.object(commands, "_api", side_effect=fake), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(a[0])):
            commands.specimen(names)
        return [Path(line) for line in out]

    def test_a_working_folder_refusal_leaves_the_other_calls_folders(self):
        with self.root() as root:
            mine = self.fetch(["a.jpg", "b.jpg"], lambda *a, **k: answer(JPEG))
            self.assertTrue(all(path.exists() for path in mine))
            # another call, refused because its working folder holds every place a sheet may go
            with mock.patch.object(specimens, "_usable", return_value=False), mock.patch.object(specimens, "_in_the_users_own_folder", return_value=False):
                with self.assertRaises(core.Status) as raised:
                    specimens.make_folder()
            self.assertEqual(raised.exception.name, "BLOCKED_BY_POLICY")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                core.emit(raised.exception)
            self.assertIn("THEODORE BLOCKED_BY_POLICY: ", printed.getvalue())
            self.assertTrue(all(path.exists() for path in mine), "a refusal of one call's folder is not a refusal of the device")
            # a refusal of the device or the account still removes them all
            with contextlib.redirect_stdout(io.StringIO()):
                core.emit(core.Status("BLOCKED_BY_POLICY", "the channel is paused.", "stop.", "Theodore is paused."))
            self.assertFalse(mine[0].parent.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_automatic_reports_off_leaves_the_folders(self):
        with self.root():
            mine = self.fetch(["a.jpg"], lambda *a, **k: answer(JPEG))
            with contextlib.redirect_stdout(io.StringIO()):
                core.emit(specimens.local_block(core.Status("BLOCKED_BY_POLICY", "automatic reports are off for this account; nothing was sent.",
                                                            "continue.", None)))
            self.assertTrue(mine[0].exists())

    def test_a_call_that_fails_after_a_sheet_keeps_the_sheet_another_call_printed(self):
        with self.root():
            reused = []

            def fake(route, what, **kwargs):
                if kwargs["file"] == "b.jpg":
                    # meanwhile another call reuses the sheet this one has just written, and prints its path
                    reused.append(specimens.held("a.jpg"))
                    return answer(JPEG, sha="0" * 64)        # this call's next sheet arrives damaged: RETRY
                return answer(JPEG)
            with self.assertRaises(core.Status) as raised:
                self.fetch(["a.jpg", "b.jpg"], fake)
            self.assertEqual(raised.exception.name, "RETRY")
            self.assertIsNotNone(reused[0])
            self.assertTrue(reused[0].exists(), "the path the other call printed still opens")
            self.assertEqual(sorted(p.name for p in reused[0].parent.iterdir()), ["a.jpg"], "nothing of the damaged answer was kept")
            self.assertIn(reused[0].parent, [folder for _made, folder in specimens._read_registry()], "recorded: swept after its hour")

    def test_a_call_that_fails_before_any_sheet_leaves_no_folder(self):
        with self.root() as root:
            with self.assertRaises(core.Status):
                self.fetch(["a.jpg"], lambda *a, **k: answer(JPEG, sha="0" * 64))
            self.assertEqual(list(root.iterdir()), [])


class TwoProcesses(ClientCase):
    """Two `theodore specimen` processes of one user: the second refused for its working folder, the first's folder stays."""

    def test_a_refused_process_beside_a_fetching_one(self):
        self.sign_in()
        self.api.specimens = {"a.jpg": JPEG, "b.jpg": JPEG + b"b"}
        work = self.box.tmp / "work"
        work.mkdir()
        first = self.client("specimen", "a.jpg", "b.jpg", cwd=work)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        paths = [Path(line) for line in first.stdout.splitlines()]
        inside = self.box.user_home.parent / "tmp-above"
        inside.mkdir()
        refused = self.client("specimen", "a.jpg", cwd=self.box.user_home.parent, env={"TMPDIR": str(inside)})
        if refused.returncode == 0:
            self.skipTest("this system has another temporary folder outside the working folder")
        self.assertStatus(refused, "BLOCKED_BY_POLICY", 73)
        self.assertTrue(all(path.exists() for path in paths), "the first call's sheets are still there to open")


if __name__ == "__main__":
    unittest.main()
