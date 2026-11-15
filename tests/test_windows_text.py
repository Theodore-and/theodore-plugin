"""Windows text: paths with letters outside the console code page (C:\\Users\\Théo Tester), and what the client
generates for cmd.exe and PowerShell. Unit-level, on every platform: what is written, how it is invoked, and what cmd
would read from it (a batch file is read in the OEM code page)."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (puts the client on sys.path)

from theodore_client import commands, core, specimens  # noqa: E402

# A home with é, a space, an ampersand, a CJK letter and every character cmd treats specially.
STRANGE = "Théo Tester & 测试 (x) 50%off! ^a"


def cmd_reads(text: bytes, env: dict) -> list[str]:
    """The script as cmd.exe would see it after percent expansion: decoded in the OEM code page, each %NAME% replaced
    once by its value (cmd never expands the result again)."""
    lines = text.decode("cp437").split("\r\n")
    return [re.sub(r"%(THEODORE_CLEANUP_[A-Z]+)%", lambda m: env[m.group(1)], line) for line in lines]


class WindowsCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / STRANGE / "AppData" / "Local" / "Theodore"
        (self.root / "bin").mkdir(parents=True)
        env = {"THEODORE_HOME": str(self.root), "TMPDIR": str(self.tmp / STRANGE / "Temp"), "TEMP": str(self.tmp / STRANGE / "Temp")}
        (self.tmp / STRANGE / "Temp").mkdir()
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(core, "_HOME", None),
                   mock.patch.object(tempfile, "tempdir", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cleanup(self, probe_code: int = 0, popen_error: bool = False):
        calls = []

        def run(command, **kwargs):
            calls.append(("run", command, kwargs))
            return subprocess.CompletedProcess(command, probe_code)

        def popen(command, **kwargs):
            calls.append(("popen", command, kwargs))
            if popen_error and len([c for c in calls if c[0] == "popen"]) == 1:
                raise OSError("job forbids breakaway")
            return mock.Mock()

        ran = []
        with mock.patch.object(commands.subprocess, "run", run), mock.patch.object(commands.subprocess, "Popen", popen), \
                mock.patch.object(commands, "_run", lambda command, timeout=0: ran.append(command) or (0, "")):
            result = commands._windows_cleanup(self.root, list(core.OWNED), keep_specimen_record=True)
        return result, calls, ran

    def test_no_path_is_written_into_the_script_and_cmd_finds_every_path_through_variables(self):
        result, calls, _ran = self.run_cleanup()
        self.assertIsNotNone(result)
        script = result.parent / "cleanup.cmd"
        raw = script.read_bytes()
        self.assertTrue(raw.isascii(), "the batch file holds only ASCII")
        for piece in ("Théo", "测试", "&", "50%off", str(self.tmp)):
            self.assertNotIn(piece.encode("utf-8"), raw.replace(b"& rmdir", b"").replace(b"2>&1", b""))
        probe, started = calls[0], calls[1]
        self.assertEqual(("run", "popen"), (probe[0], started[0]), "the probe runs first, synchronously, then the cleanup")
        env = started[2]["env"]
        self.assertEqual(str(self.root), env["THEODORE_CLEANUP_ROOT"])
        self.assertEqual(str(script), env["THEODORE_CLEANUP_SCRIPT"])
        self.assertEqual(specimens.registry().name, env["THEODORE_CLEANUP_KEEP"])
        self.assertIs(probe[2]["env"], env)
        seen = cmd_reads(raw, env)
        self.assertIn(f'if exist "{self.root}\\{commands.UNINSTALL_MARKER}" goto pass', seen)
        self.assertIn(f'if exist "{self.root}\\bin" rmdir /s /q "{self.root}\\bin" >nul 2>&1', seen)
        self.assertIn(f'rmdir "{self.root}" >nul 2>&1', seen)
        # Every expanded path sits inside double quotes, so &, ( ) and ^ in it are never cmd syntax.
        for line in raw.decode("ascii").split("\r\n"):
            for m in re.finditer(r"%THEODORE_CLEANUP_[A-Z]+%", line):
                self.assertEqual(1, line[:m.start()].count('"') % 2, line)
            self.assertNotIn("enabledelayedexpansion", line.replace("disabledelayedexpansion", ""))
        # The command lines name no path but cmd.exe's; the script comes from a variable, in a pair cmd keeps.
        for _kind, command, _kw in calls:
            self.assertTrue(command.isascii())
            self.assertIn('/d /v:off /c ""%THEODORE_CLEANUP_SCRIPT%"', command)
        self.assertTrue(probe[1].endswith('" probe"'))
        self.assertTrue((self.root / commands.UNINSTALL_MARKER).exists())

    def test_a_probe_that_does_not_see_the_folder_starts_nothing_and_says_so(self):
        result, calls, _ran = self.run_cleanup(probe_code=3)
        self.assertIsNone(result)
        self.assertEqual(["run"], [c[0] for c in calls], "no cleanup is started")
        self.assertFalse((self.root / commands.UNINSTALL_MARKER).exists())
        self.assertFalse(any(p.name.startswith("theodore-uninstall-") for p in (self.tmp / STRANGE / "Temp").iterdir()))

    def test_the_wmi_way_carries_the_variables_in_an_encoded_command(self):
        system32 = self.tmp / "Windows" / "System32"
        ps = system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        ps.parent.mkdir(parents=True)
        ps.write_text("")
        with mock.patch.dict(os.environ, {"SystemRoot": str(self.tmp / "Windows")}):
            result, calls, ran = self.run_cleanup(popen_error=True)
            launch = os.environ["THEODORE_CLEANUP"]
        self.assertIsNotNone(result)
        self.assertEqual(1, len(ran))
        self.assertTrue(launch.isascii())
        body = base64.b64decode(launch.split("-EncodedCommand ")[1]).decode("utf-16-le")
        values = {k: base64.b64decode(v).decode("utf-8") for k, v in
                  re.findall(r"\$env:(THEODORE_CLEANUP_[A-Z]+) = \[Text\.Encoding\]::UTF8\.GetString\(\[Convert\]::FromBase64String\('([^']*)'\)\)", body)}
        self.assertEqual(str(self.root), values["THEODORE_CLEANUP_ROOT"])
        self.assertEqual(str(result.parent / "cleanup.cmd"), values["THEODORE_CLEANUP_SCRIPT"])
        self.assertTrue(body.isascii(), "no value is written as a literal")

    def test_script_names_must_be_plain(self):
        with self.assertRaises(ValueError):
            commands._cleanup_script(["bin&x"], False)


class Junction(unittest.TestCase):
    def test_a_junction_takes_its_paths_through_variables(self):
        tmp = Path(tempfile.mkdtemp()) / STRANGE
        seen = []
        with mock.patch.object(core, "IS_WINDOWS", True), \
                mock.patch.object(core.subprocess, "run", lambda command, **kw: seen.append((command, kw["env"]))):
            core.link_dir(tmp / "link", tmp / "target")
        command, env = seen[0]
        self.assertTrue(command.isascii(), command)
        self.assertTrue(command.endswith('/d /v:off /c "mklink /J "%THEODORE_LINK%" "%THEODORE_LINK_TARGET%""'))
        self.assertEqual(str(tmp / "link"), env["THEODORE_LINK"])
        self.assertEqual(str(tmp / "target"), env["THEODORE_LINK_TARGET"])


class BatchFiles(unittest.TestCase):
    """CALL doubles every caret, quoted or not, and expands % a second time: a path in a variable goes to CALL as
    %%NAME%% (expanded once, after that), so C:\\Users\\a^b or 50%off stay themselves."""
    FILES = [support.ROOT / "install" / "install.cmd", support.LAUNCHER_DIR / "theodore.cmd"]

    def test_no_call_of_a_path_variable_expanded_before_call(self):
        for path in self.FILES:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().lower().startswith("rem "):
                    continue
                self.assertIsNone(re.search(r'\bcall "%[A-Za-z_]+%', line, re.I), f"{path.name}:{n}: {line}")

    def test_install_cmd_waits_in_codex_as_install_sh(self):
        """Lane CHATGPT's Codex install wait, the same in install.cmd: 540 s by default under CODEX_THREAD_ID, a line a
        minute while it waits, and, still downloading, OK with nothing to poll (only when every assistant was registered)."""
        cmd = (support.ROOT / "install" / "install.cmd").read_text(encoding="utf-8")
        self.assertIn('set "SETUP_WAIT_DEFAULT=180"\nif defined CODEX_THREAD_ID set "SETUP_WAIT_DEFAULT=540"', cmd.replace("\r\n", "\n"))
        self.assertIn("set /a SETUP_STEP=60", cmd)
        self.assertIn("theodore-install: still downloading (!SETUP_NOW!); this installer waits at most !SETUP_LEFT! s more, "
                      "then the download goes on by itself", cmd)
        ok = cmd.index("echo THEODORE OK: Theodore is installed and signed in; the rest of its runtime and its engine finish "
                       "downloading by themselves in the background")
        self.assertLess(cmd.index('if not "%HOSTS_RC%"=="0" goto :doctor_in_progress'), ok)
        self.assertIn("echo NEXT: run nothing more now, neither doctor nor this installer: the download needs no command. "
                      "Tell the user to start a new session and ask for a deck.", cmd)
        self.assertLess(ok, cmd.index("echo THEODORE SETUP_IN_PROGRESS: Theodore is installed;"))
        # Ready in Codex: the deck goes to a new session (Codex loads the tool and the rules when a session starts).
        sentence = (" If they ask for a deck in this session anyway, ask them to start a new session first: Codex loads Theodore's "
                    "tool and permissions only when a session starts, so here each Theodore command would ask for approval.")
        self.assertEqual(2, cmd.count(sentence))
        ready = cmd.index("echo THEODORE OK: Theodore is ready (the check above).")
        self.assertLess(cmd.index(":doctor_shown"), ready)
        self.assertLess(cmd.index('if not "%HOSTS_RC%"=="0" goto :hosts_left'), ready)
        self.assertIn("echo TELL THE USER: Theodore is ready. Start a new session and ask for your deck there.", cmd)

    def test_the_batch_files_are_ascii(self):
        for path in self.FILES:
            self.assertTrue(path.read_bytes().isascii(), path)


class DesktopEntry(unittest.TestCase):
    def entry(self, home: str) -> dict:
        from theodore_client import desktop
        with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.dict(os.environ, {"COMSPEC": "C:\\Windows\\system32\\cmd.exe"}), \
                mock.patch.object(core, "home", lambda: mock.Mock(launcher=lambda: home + "\\bin\\theodore.cmd")):
            return desktop.entry()

    def test_a_plain_path_keeps_the_form_that_works(self):
        value = self.entry("C:\\Users\\Théo Tester\\AppData\\Local\\Theodore")
        self.assertEqual(["/d", "/c", "C:\\Users\\Théo Tester\\AppData\\Local\\Theodore\\bin\\theodore.cmd", "mcp", "--for", "claude-desktop"], value["args"])
        self.assertNotIn("env", value)

    def test_a_path_cmd_would_parse_goes_through_the_env_and_survives_json(self):
        import json
        home = "C:\\Users\\Tom & Jerry (x) 50%off ^a!\\AppData\\Local\\Theodore"
        value = self.entry(home)
        self.assertEqual(["/d", "/v:on", "/c", "!THEODORE_DESKTOP_LAUNCHER!", "mcp", "--for", "claude-desktop"], value["args"])
        for arg in value["args"]:
            self.assertFalse(set(arg) & set(' "&()^%'), arg)       # nothing the app's quoting or cmd's parsing touches
        self.assertEqual(f'"{home}\\bin\\theodore.cmd"', json.loads(json.dumps(value, ensure_ascii=False))["env"]["THEODORE_DESKTOP_LAUNCHER"])


class LauncherPartialUninstall(unittest.TestCase):
    def test_its_last_line_reads_paths_after_parsing(self):
        text = (support.LAUNCHER_DIR / "theodore.cmd").read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if l.startswith("(for %%D in (%OWNED_DIRS%) do rmdir"))
        before = text.splitlines()[text.splitlines().index(line) - 2]
        self.assertEqual("setlocal EnableDelayedExpansion", before)
        self.assertNotIn("call echo", line)
        self.assertNotIn("%HOME_DIR%", line)
        self.assertNotIn("%LEFT", line)


class PowerShellInstallLine(unittest.TestCase):
    def test_the_powershell_line_reads_the_installers_utf8(self):
        """W-4: PowerShell 5.1 decodes a program's output in the console's code page (cp437: "Th├⌐o"), in a console
        and in anything PowerShell does with it. The documented line sets UTF-8 first; where no console is attached the
        setting throws and is skipped."""
        page = (support.ROOT / "install" / "install.md").read_text(encoding="utf-8")
        line = next(l for l in page.splitlines() if l.strip().startswith("Windows PowerShell: "))
        self.assertTrue(line.strip().startswith("Windows PowerShell: try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}; "), line)


class ChildText(unittest.TestCase):
    def test_utf8_from_a_child_is_read_as_utf8(self):
        path = "C:\\Users\\Théo Tester\\测试"
        self.assertEqual(path, core.child_text(path.encode("utf-8")))
        self.assertNotIn("Ã", core.child_text("Théo".encode("utf-8")))

    def test_a_code_page_byte_is_still_text(self):
        self.assertTrue(core.child_text("Théo".encode("cp1252")).startswith("Th"))
        self.assertEqual("", core.child_text(None))

    def test_the_engine_writes_utf8(self):
        from theodore_client import install
        with mock.patch.object(install, "system_libreoffice", lambda: True):
            self.assertEqual("utf-8", install.engine_env(Path(tempfile.mkdtemp()))["PYTHONIOENCODING"])

    def test_the_self_test_error_names_the_path_as_written(self):
        from theodore_client import install
        directory = Path(tempfile.mkdtemp())
        (directory / "skills" / "pptx" / "scripts").mkdir(parents=True)
        (directory / "skills" / "pptx" / "scripts" / "pptx.py").write_text("")
        message = "[WinError 448] untrusted mount point: 'C:\\\\Users\\\\Théo Tester\\\\x'"
        failed = subprocess.CompletedProcess([], 1, b"", message.encode("utf-8"))
        with mock.patch.object(install.subprocess, "run", lambda *a, **k: failed), \
                mock.patch.object(install, "engine_env", lambda d: {}), mock.patch.object(install, "engine_python", lambda d: "py"):
            with self.assertRaises(install.EngineError) as raised:
                install._self_test(directory, "rt")
        self.assertIn("Théo Tester", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
