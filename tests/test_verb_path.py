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

class RefusalFromOutput(HomeCase):
    """No second engine start with --help after a failing verb: the refusal is the engine's own last JSON object."""

    def test_one_line_a_whole_payload_and_none(self):
        line = b'{"ok": false, "error": "x", "code": "entitlement_clock_rollback"}\n'
        cases = ((b"progress\n" + line, 1, "entitlement_clock_rollback"), (line, install.LICENCE_EXIT, "entitlement_clock_rollback"),
                 (b'{\n  "ok": false,\n  "code": "entitlement_untrusted"\n}\n', 1, "entitlement_untrusted"))
        for output, exit_code, cause in cases:
            status = install.refusal_in_output(output, exit_code)
            self.assertEqual((status.name, status.cause), ("NO_ENTITLEMENT", cause), output)
        self.assertIsNone(install.refusal_in_output(b'{"ok": false, "code": "slide_not_found"}\n', 1), "a verb's own refusal")
        self.assertIsNone(install.refusal_in_output(b"Traceback ...\n", 1))
        self.assertIsNone(install.refusal_in_output(b"", 1))
        unread = install.refusal_in_output(b"garbled", install.LICENCE_EXIT)
        self.assertEqual((unread.name, unread.cause), ("NO_ENTITLEMENT", "entitlement_refused"), "77 is a refusal all the same")

    def test_the_output_passes_through_whole_and_the_tail_is_kept(self):
        import io
        sink = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        # The child writes bytes, as they reach the client whatever its platform's text mode (Windows writes a line
        # break as CR LF): the test is about the client passing them through unchanged, not about the child's text.
        script = "import sys; sys.stdout.buffer.write(('é' * 50000 + '\\nlast line').encode('utf-8')); sys.stdout.flush(); sys.exit(3)"
        with mock.patch.object(sys, "stdout", sink):
            code, tail, _whole = install._call_passing_output([sys.executable, "-c", script], dict(os.environ))
        self.assertEqual(code, 3)
        self.assertEqual(sink.buffer.getvalue(), ("é" * 50000 + "\nlast line").encode("utf-8"), "every byte, unchanged")
        self.assertTrue(tail.endswith(b"\nlast line"))
        self.assertLessEqual(len(tail), install.OUTPUT_TAIL_BYTES)

    def test_a_reader_that_went_away_never_blocks_the_engine(self):
        class Closed:
            def write(self, data):
                raise BrokenPipeError()

            def flush(self):
                pass
        holder = mock.Mock()
        holder.buffer = Closed()
        script = "import sys; [sys.stdout.write('x' * 65536) for _ in range(40)]; sys.stdout.flush()"
        with mock.patch.object(sys, "stdout", holder):
            code, tail, _whole = install._call_passing_output([sys.executable, "-c", script], dict(os.environ))
        self.assertEqual(code, 0, "2.5 MB written into a closed reader: drained, finished")
        self.assertEqual(len(tail), install.OUTPUT_TAIL_BYTES)

class CredentialAndLookups(HomeCase):
    """An engine command reads no credential while the licence holds, and finds LibreOffice from the last lookup."""

    def token(self, exp_in: float) -> None:
        import base64
        b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
        now = int(time.time())
        claims = {"aud": "theodore-engine", "feat": ["pptx"], "plan": "beta", "iat": now - 60, "rfa": now + exp_in / 2, "exp": now + exp_in}
        core.write_json(self.home / "state" / "entitlement.json", {"entitlement_token": b64({"alg": "EdDSA"}) + "." + b64(claims) + ".c2ln"})

    def test_a_valid_licence_needs_no_credential_on_the_verb_path(self):
        from theodore_client import account, credentials
        self.token(exp_in=86400)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("THEODORE_ENTITLEMENT", None)
            with mock.patch.object(credentials, "load", side_effect=AssertionError("the keychain was read")):
                self.assertEqual(account.require("pptx", allow_refresh=False)["plan"], "beta")
            self.token(exp_in=-10)                      # expired: the credential is needed to renew it or to sign in
            sign_in = core.Status("SIGN_IN_REQUIRED", "a", "b", "c")
            with mock.patch.object(credentials, "load", return_value=None) as load, \
                    mock.patch.object(account, "login_start", return_value=sign_in):
                with self.assertRaises(core.Status) as refused:
                    account.require("pptx", allow_refresh=False)
            self.assertTrue(load.called)
            self.assertEqual(refused.exception.name, "SIGN_IN_REQUIRED")
            self.token(exp_in=86400)                    # a guide (allow_refresh True) still checks the sign-in
            with mock.patch.object(credentials, "load", return_value=None), mock.patch.object(account, "login_start",
                                                                                               return_value=core.Status("SIGN_IN_REQUIRED", "a", "b", "c")):
                with self.assertRaises(core.Status):
                    account.require("pptx")

    def test_libreoffice_is_looked_up_once_an_hour_per_path(self):
        office = self.tmp / "bin" / "soffice"
        office.parent.mkdir()
        office.write_text("", encoding="utf-8")
        lookups = []
        which = lambda name: lookups.append(name) or str(office)
        with mock.patch.object(install.shutil, "which", side_effect=which), mock.patch.dict(os.environ, {"PATH": "/one"}):
            os.environ.pop("THEODORE_TEST_NO_SYSTEM_LIBREOFFICE", None)
            os.environ.pop("SOFFICE_BIN", None)
            self.assertEqual(install.known_libreoffice(), str(office))
            self.assertEqual(install.known_libreoffice(), str(office))
            self.assertEqual(len(lookups), 1, "the second command reuses the first lookup")
            env = install.engine_env(self.home)
            self.assertNotIn("SOFFICE_BIN", env)
            self.assertEqual(len(lookups), 1, "the engine's environment too")
            with mock.patch.dict(os.environ, {"PATH": "/two"}):
                install.known_libreoffice()
            self.assertEqual(len(lookups), 2, "another PATH: looked up again")
            install.known_libreoffice()
            with mock.patch.dict(os.environ, {"THEODORE_TEST_CLOCK_OFFSET": str(install.RENDERER_RECHECK_S + 5)}), \
                    mock.patch.object(core, "is_loopback_api", return_value=True):
                install.known_libreoffice()
            self.assertEqual(len(lookups), 4, "an hour on: looked up again")
            office.unlink()
            install.known_libreoffice()
            self.assertEqual(len(lookups), 5, "the program found is gone: looked up again")
            install.system_libreoffice()
            self.assertEqual(len(lookups), 6, "doctor and tools install always look")

class RunLock(HomeCase):
    """Each engine run held a lock file of its own (engine-run-<pid>-<thread>.lock), created and deleted per run: one more
    new file per command for the antivirus to scan. Now a run takes the first free of a few fixed slot files."""

    def test_a_run_creates_no_file_after_the_first(self):
        from theodore_client import withdraw
        with withdraw.engine_run():
            pass
        before = sorted(p.name for p in core.home().locks.iterdir())
        import threading
        for _ in range(3):
            with withdraw.engine_run():
                seen = []
                other = threading.Thread(target=lambda: seen.append(withdraw._busy()))     # what another process sees
                other.start()
                other.join(10)
                self.assertEqual(seen, ["an engine run is in progress"])
        self.assertEqual(sorted(p.name for p in core.home().locks.iterdir()), before)
        self.assertIn(f"{withdraw.RUN_SLOT}00.lock", before)
        self.assertIsNone(withdraw._busy())
        self.assertIn(f"{withdraw.RUN_SLOT}00.lock", [p.name for p in core.home().locks.iterdir()], "a free slot stays")

    def test_runs_side_by_side_take_different_slots_and_a_leftover_is_cleared(self):
        import threading
        from theodore_client import withdraw
        (core.home().locks).mkdir(parents=True, exist_ok=True)
        leftover = core.home().locks / f"{withdraw.RUN_LOCK_PREFIX}99999-1.lock"      # left by a run before 0.4.34
        leftover.write_text("", encoding="utf-8")
        entered, release, slots = threading.Barrier(4), threading.Event(), []

        def run():
            with withdraw.engine_run():
                entered.wait(10)
                release.wait(10)
        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        entered.wait(10)
        self.assertEqual(withdraw._busy(), "an engine run is in progress")
        release.set()
        for thread in threads:
            thread.join(10)
        names = sorted(p.name for p in core.home().locks.iterdir() if p.name.startswith(withdraw.RUN_SLOT))
        self.assertEqual(names, [f"{withdraw.RUN_SLOT}{i:02d}.lock" for i in range(3)], "three runs at once, three slots")
        self.assertIsNone(withdraw._busy())
        self.assertFalse(leftover.exists(), "an earlier run's own lock file is removed once free")

class QuickSweep(HomeCase):
    """Specimen folders stay exactly as transient as before (a recorded one goes at the first command after its hour,
    verbs included), but a verb no longer lists the temporary folder or rewrites the record every time."""

    def setUp(self):
        super().setUp()
        import tempfile as tempmod
        from theodore_client import specimens
        self.specimens = specimens
        self.temp = self.tmp / "temp"
        self.temp.mkdir()
        patch = mock.patch.object(tempmod, "tempdir", str(self.temp))
        patch.start()
        self.addCleanup(patch.stop)

    def folder(self, name: str, age: float, recorded: bool) -> Path:
        folder = self.temp / f"theodore-specimens-{name}"
        folder.mkdir()
        made = time.time() - age
        os.utime(folder, (made, made))
        if recorded:
            entries = self.specimens._read_registry() + [(made, folder)]
            self.specimens._write_registry(entries)
        return folder

    def test_a_recorded_folder_goes_at_the_first_verb_after_its_hour(self):
        fresh = self.folder("fresh", 60, recorded=True)
        self.specimens.sweep()                                   # a guide: lists the roots, writes the scan time
        listed = []
        real = self.specimens._folders
        with mock.patch.object(self.specimens, "_folders", side_effect=lambda: listed.append(1) or real()):
            self.assertEqual(self.specimens.sweep(quick=True), [])
            self.assertEqual(listed, [], "nothing expired and the roots were listed minutes ago: the record alone")
            old = self.folder("old", 3700, recorded=True)
            self.specimens.sweep(quick=True)
            self.assertFalse(old.exists(), "past its hour: removed by the verb, as before")
        self.assertTrue(fresh.exists())

    def test_an_unrecorded_folder_goes_at_the_next_listing(self):
        stray = self.folder("stray", 3700, recorded=False)
        self.specimens.sweep()                                   # a non-verb command lists the roots, as before
        self.assertFalse(stray.exists())
        stray = self.folder("stray2", 3700, recorded=False)
        self.specimens.sweep(quick=True)
        self.assertTrue(stray.exists(), "a verb within ten minutes of the last listing does not list again")
        with mock.patch.dict(os.environ, {"THEODORE_TEST_CLOCK_OFFSET": str(self.specimens.SCAN_EVERY_S + 5)}), \
                mock.patch.object(core, "is_loopback_api", return_value=True):
            self.specimens.sweep(quick=True)
        self.assertFalse(stray.exists(), "ten minutes on, a verb lists them")

class CredentialKept(HomeCase):
    """The tool server keeps the credential in memory: one keychain or DPAPI read per change, not two per guide call."""

    def setUp(self):
        super().setUp()
        from theodore_client import credentials
        self.credentials = credentials
        patches = [mock.patch.object(credentials, "_KEEP", True), mock.patch.object(credentials, "_KEPT", None),
                   mock.patch.dict(os.environ, {"THEODORE_CREDENTIAL_STORE": "file"}),
                   mock.patch.object(core, "_CONFIG", {"api_base": "https://api.example.test"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_read_once_and_again_when_it_changes(self):
        reads = []
        real = self.credentials._load
        with mock.patch.object(self.credentials, "_load", side_effect=lambda: reads.append(1) or real()):
            self.assertIsNone(self.credentials.load())
            self.credentials.store("iid-1", "sec-0123456789abcdef0123")
            self.assertEqual(self.credentials.load()["installation_id"], "iid-1")
            self.assertEqual(self.credentials.load()["installation_id"], "iid-1")
            self.assertEqual(len(reads), 2, "after a store, read once, then from memory")
            other = json.loads((self.home / "state" / "credentials").read_text())     # another process signs in again
            other["installation_id"] = "iid-2"
            time.sleep(0.01)
            (self.home / "state" / "credentials").write_text(json.dumps(other))
            self.assertEqual(self.credentials.load()["installation_id"], "iid-2", "a change on disk is seen")
            self.credentials.delete()
            self.assertIsNone(self.credentials.load(), "deleted: gone from memory too")

if __name__ == "__main__":
    unittest.main()
