"""The verb path (0.4.34): what a `pptx` command does in the client before and after the engine, in-process. Each case
names the cost it keeps off the path: the asset-pack scan of the deck's scripts, the venv redirector, the second
engine start after a failing verb, the help text, the specimen sweep, the usage send, the credential read, the
LibreOffice lookup and the per-run lock file."""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (puts the client on sys.path)

from theodore_client import core, install  # noqa: E402
from theodore_client.contract import PACK_ORDER  # noqa: E402


class HomeCase(unittest.TestCase):
    """A scratch Theodore home, set for this process (THEODORE_HOME, a fresh core.home())."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theodore-verb-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / "state").mkdir(parents=True)
        patches = [mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.home), "HOME": str(self.tmp / "user")}),
                   mock.patch.object(core, "_HOME", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def workspace(self, name: str = "ws", script: str = "deck.slide(s => { s.text('plain'); });\n") -> Path:
        ws = self.tmp / name
        (ws / ".pptx-state").mkdir(parents=True)
        (ws / "deck.js").write_text(script, encoding="utf-8")
        return ws

    def packs(self, ready: tuple[str, ...]) -> None:
        state = {}
        for pack in PACK_ORDER:
            (self.home / "packs" / pack).mkdir(parents=True, exist_ok=True)
            state[pack] = {"status": "ready" if pack in ready else "downloading", "version": "1.0.0", "percent": 100 if pack in ready else 10}
        core.write_json(self.home / "packs" / "state.json", state)


class PackScan(HomeCase):
    """ISSUES 23: the client read and regex-scanned every deck.js before every verb to learn which asset packs the build
    needs (1.36 s of the Windows test machine's 1.66 s `slide list` on the 2.3 MB synthetic deck), although after the
    installation every pack is ready."""

    def test_with_every_pack_ready_nothing_of_the_workspace_is_read(self):
        self.packs(ready=PACK_ORDER)
        ws = self.workspace(script="deck.slide(s => { s.safeIcon('rocket'); });\n")
        with mock.patch.object(install, "packs_needed", side_effect=AssertionError("the scripts were scanned")), \
                mock.patch.object(install, "packs_in_script", side_effect=AssertionError("a script was read")):
            install.wait_for_packs(["slide", "list", str(ws)])
            install.wait_for_packs(["apply", str(ws)])
        self.assertFalse((self.home / "state" / install.PACK_SCAN_FILE).exists(), "nothing written either")

    def test_a_pack_still_arriving_scans_each_script_once_per_size_and_time(self):
        self.packs(ready=("flags", "maps", "backgrounds"))       # icons still downloading
        ws = self.workspace(script="deck.slide(s => { s.text('plain'); });\n")
        scans = []
        real = install.packs_in_script
        with mock.patch.object(install, "packs_in_script", side_effect=lambda text: scans.append(len(text)) or real(text)):
            self.assertEqual(install.packs_needed(["apply", str(ws)]), set())
            self.assertEqual(install.packs_needed(["apply", str(ws)]), set())
            self.assertEqual(len(scans), 1, "an unchanged deck.js is not read again")
            (ws / "deck.js").write_text("deck.slide(s => { s.safeIcon('rocket'); });\n", encoding="utf-8")
            self.assertEqual(install.packs_needed(["apply", str(ws)]), {"icons"})
            self.assertEqual(len(scans), 2, "a changed deck.js is read again")
            later = time.time() + 5
            os.utime(ws / "deck.js", (later, later))         # same size, another time: read again
            self.assertEqual(install.packs_needed(["apply", str(ws)]), {"icons"})
            self.assertEqual(len(scans), 3)
        memo = json.loads((self.home / "state" / install.PACK_SCAN_FILE).read_text())
        self.assertNotIn(str(ws), json.dumps(memo), "no folder name is stored, only a hash of the path")
        with mock.patch.object(install, "pack_ready", return_value=False), mock.patch.object(install, "start_packs_job"), \
                mock.patch.object(install.Job, "alive", return_value=False), \
                mock.patch.dict(os.environ, {"THEODORE_TEST_PACK_WAIT": "0"}), mock.patch.object(core, "is_loopback_api", return_value=True):
            with self.assertRaises(core.Status) as waited:
                install.wait_for_packs(["apply", str(ws)])
        self.assertEqual(waited.exception.name, "SETUP_IN_PROGRESS")
        self.assertIn("icon pack is still downloading", waited.exception.summary)

    def test_the_memo_is_bounded(self):
        self.packs(ready=())
        ws = self.workspace()
        memo = {f"{i:016x}": {"stamp": [1, 1], "packs": []} for i in range(install.PACK_SCAN_MAX + 20)}
        core.write_json(self.home / "state" / install.PACK_SCAN_FILE, memo)
        install.packs_needed(["apply", str(ws)])
        kept = json.loads((self.home / "state" / install.PACK_SCAN_FILE).read_text())
        self.assertEqual(len(kept), install.PACK_SCAN_MAX)
        self.assertNotIn(f"{0:016x}", kept, "the oldest went first")

    def test_the_loose_patterns_with_a_word_boundary_find_the_same_calls(self):
        """`\\b` in front of the loose patterns: a search starts once per word instead of at every position (2.5 times
        faster on the synthetic deck), and finds a match exactly when the unbounded pattern does."""
        old = {"icons": r"\w*[iI]con\w*\(", "flags": r"\w*[fF]lag\w*\(", "backgrounds": r"\w*[bB]ackground\w*\("}
        words = ["icon", "Icon", "safeIcon", "xIconY", "flag", "Flag", "getFlagMeta", "background", "Background", "bg",
                 "seen", "map", "Map", "addMap", "_icon_", "é", "9flag", "const", "x"]
        rng = random.Random(23)
        samples = ["a.b.safeIcon(", "flags.draft", "renderIconGrid (slide)", "x = icon(1)", "_Background_x(", "é9flag("]
        for _ in range(3000):
            samples.append("".join(rng.choice(words + [" ", ".", "(", "(", "_", "-", "\n"]) for _ in range(rng.randint(1, 12))))
        for pack, pattern in old.items():
            new = install._PACK_LOOSE[pack]
            self.assertTrue(new.startswith(r"\b"), pack)
            for text in samples:
                self.assertEqual(bool(re.search(pattern, text)), bool(re.search(new, text)), (pack, text))


class EnginePython(HomeCase):
    """The engine runs on its runtime's own python.exe, not the venv's redirector (a second process on Windows)."""

    def engine(self, runtime_id: str | None, venv_home: Path | None) -> Path:
        directory = self.home / "engines" / "1.0.0-rc.33"
        (directory / ".venv").mkdir(parents=True)
        record = {"version": "1.0.0-rc.33", **({"runtime_id": runtime_id} if runtime_id is not None else {})}
        core.write_json(directory / ".theodore-engine.json", record)
        if venv_home is not None:
            (directory / ".venv" / "pyvenv.cfg").write_text(f"home = {venv_home}\ninclude-system-site-packages = true\n", encoding="utf-8")
        return directory

    def runtime(self, runtime_id: str) -> Path:
        python = core.home().runtime_python(runtime_id)
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        return python

    def test_the_runtime_the_engine_was_installed_for(self):
        python = self.runtime("rt1")
        self.assertEqual(install.engine_python(self.engine("rt1", None)), python)

    def test_else_the_runtime_its_venv_names(self):
        python = self.runtime("rt2")
        directory = self.engine(None, python.parent)
        self.assertEqual(install.engine_python(directory), python)
        core.write_json(directory / ".theodore-engine.json", {"runtime_id": ".."})        # never a path out of runtime/
        self.assertEqual(install.engine_python(directory), python)

    def test_else_the_venv_as_before(self):
        directory = self.engine("gone", self.tmp / "nowhere")
        self.assertEqual(install.engine_python(directory), directory / ".venv" / ("Scripts/python.exe" if core.IS_WINDOWS else "bin/python"))

if __name__ == "__main__":
    unittest.main()
