"""0.4.30: designers working side by side never remove each other's sheets.

Seen on 2026-11-22 in a first-deck run: one designer's `specimen` call printed two paths, the first opened, and less
than a second later the second was gone; in that moment another designer was refused with BLOCKED_BY_POLICY, a refusal
of that one call, and every status of that name removed every specimen folder on the computer. Since contract C6 (24
November 2026) a shell's sheets are files in the session's own folder (<home>/work/scratch/sheets/<session>/), named by
their ETag: a refusal of one call (automatic reports off, a damaged answer) leaves every file other calls printed; a
refusal of the device or the account still removes them all.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase  # noqa: E402  (support puts the client on sys.path)
from fake_api import FakeApi  # noqa: E402

from theodore_client import api, core, specimens  # noqa: E402
from theodore_client.service import texts as texts_module  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0 stand-in sheet \xff\xd9"
NO_PROXY = {k: "" for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")}


class InProcess(unittest.TestCase):
    """In process, so it also runs on Windows."""

    def setUp(self):
        self.fake = FakeApi().start()
        self.addCleanup(self.fake.stop)
        self.fake.installations["iid-1"] = {"secret": "secret-1", "revoked": False, "gen": 1, "meta": {}}
        self.fake.specimens.update({"a.jpg": JPEG, "b.jpg": JPEG + b"b"})
        home = Path(tempfile.mkdtemp(prefix="theodore-side-by-side-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "bin").mkdir()
        (home / "bin" / "launcher.json").write_text(json.dumps({"api_base": self.fake.base, "dl_base": self.fake.base}))
        saved = (core._HOME, core._CONFIG, core._REFUSAL, core._REFUSAL_EMITTED)
        core._HOME, core._CONFIG, core._REFUSAL, core._REFUSAL_EMITTED = core.Home(home), None, None, False
        self.addCleanup(lambda: setattr(core, "_HOME", saved[0]) or setattr(core, "_CONFIG", saved[1])
                        or setattr(core, "_REFUSAL", saved[2]) or setattr(core, "_REFUSAL_EMITTED", saved[3]))
        self.enterContext(mock.patch.dict(os.environ, {**NO_PROXY, "THEODORE_HOME": str(home), "THEODORE_CREDENTIAL_STORE": "file"}))
        self.enterContext(mock.patch.object(specimens, "roots", return_value=[]))
        core.home().ensure()
        self.pool = api.KeptAlive()
        self.addCleanup(self.pool.close)

    def texts(self) -> texts_module.Texts:
        return texts_module.Texts(credential=lambda: "secret-1", require=lambda skill: {}, pool=self.pool)

    def test_a_refusal_of_one_call_leaves_the_files_others_printed(self):
        mine = [path for _n, path, _note in self.texts().sheet_files(["a.jpg", "b.jpg"], session="s1")]
        self.assertTrue(all(path.exists() for path in mine))
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            core.emit(specimens.local_block(core.Status("BLOCKED_BY_POLICY", "automatic reports are off for this account; nothing was sent.",
                                                        "continue.", None)))
        self.assertIn("THEODORE BLOCKED_BY_POLICY: ", printed.getvalue())
        self.assertTrue(all(path.exists() for path in mine), "a refusal of one call is not a refusal of the device")
        with contextlib.redirect_stdout(io.StringIO()):         # a refusal of the device or the account removes them all
            core.emit(core.Status("BLOCKED_BY_POLICY", "the channel is paused.", "stop.", "Theodore is paused."))
        self.assertFalse(any(path.exists() for path in mine))
        self.assertFalse(specimens.sheets_root().exists())

    def test_a_call_that_fails_keeps_the_sheets_another_call_printed(self):
        mine = self.texts().sheet_files(["a.jpg"], session="s1")[0][1]
        self.fake.specimen_sha = "0" * 64                       # the next answer arrives damaged
        with self.assertRaises(core.Status) as raised:
            self.texts().sheet_files(["a.jpg", "b.jpg"], session="s1")
        self.assertEqual(raised.exception.name, "RETRY")
        self.assertTrue(mine.exists(), "the path the other call printed still opens")
        self.assertEqual([p.name.split("-", 1)[1] for p in mine.parent.iterdir()], ["a.jpg"], "nothing of the damaged answer was kept")

    def test_two_calls_of_one_session_share_the_file_of_a_sheet(self):
        first = self.texts().sheet_files(["a.jpg"], session="s1")[0][1]
        second = self.texts().sheet_files(["a.jpg", "b.jpg"], session="s1")
        self.assertEqual(second[0][1], first)
        self.assertEqual(self.fake.served["specimen", "a.jpg", 304], 1)


class TwoProcesses(ClientCase):
    """Two `theodore specimen` processes of one user, one after the other in the same session: the second prints the
    first one's file, and that file stays."""

    def test_a_second_process_beside_the_first(self):
        self.sign_in()
        self.api.specimens = {"a.jpg": JPEG, "b.jpg": JPEG + b"b"}
        work = self.box.tmp / "work"
        work.mkdir()
        env = {"CLAUDE_CODE_SESSION_ID": "designers"}
        first = self.client("specimen", "a.jpg", "b.jpg", cwd=work, env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        paths = [Path(line) for line in first.stdout.splitlines()]
        second = self.client("specimen", "a.jpg", cwd=self.box.user_home.parent, env=env)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(Path(second.stdout.strip()), paths[0])
        self.assertTrue(all(path.exists() for path in paths), "the first call's sheets are still there to open")


if __name__ == "__main__":
    unittest.main()
