"""Windows text: paths with letters outside the console code page (C:\\Users\\Théo Tester), and what the client
generates for cmd.exe and PowerShell. Unit-level, on every platform: what is written, how it is invoked, and what cmd
would read from it (a batch file is read in the OEM code page)."""

from __future__ import annotations

import base64
import json
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
            self.assertNotIn(piece.encode("utf-8"), raw.replace(b"& rmdir", b"").replace(b"& cd /d", b"").replace(b"2>&1", b""))
        probe, started = calls[0], calls[1]
        self.assertEqual(("run", "popen"), (probe[0], started[0]), "the probe runs first, synchronously, then the cleanup")
        env = started[2]["env"]
        self.assertEqual(str(self.root), env["THEODORE_CLEANUP_ROOT"])
        self.assertEqual(str(script), env["THEODORE_CLEANUP_SCRIPT"])
        self.assertEqual(specimens.registry().name, env["THEODORE_CLEANUP_KEEP"])
        self.assertIs(probe[2]["env"], env)
        seen = cmd_reads(raw, env)
        self.assertIn(f'if not exist "{self.root}\\{commands.UNINSTALL_MARKER}" goto stopped', seen)
        self.assertIn(f'ren "{self.root}\\%~1" "%T%" >nul 2>&1', seen)       # each folder renamed first (0.4.32)
        self.assertIn(f'rmdir /s /q "{self.root}\\%~1" >nul 2>&1', seen)
        self.assertIn(f'if not defined STOP rmdir "{self.root}" >nul 2>&1', seen)
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


def fake_mklink(seen: list):
    """subprocess.run as mklink /J would act, with a symlink in place of the junction (POSIX can run it too)."""
    def run(command, **kw):
        if "mklink" not in str(command):        # another program (the engine's venv): it succeeds
            return subprocess.CompletedProcess(command, 0, b"", b"")
        seen.append((command, kw["env"]))
        if os.path.isdir(kw["env"]["THEODORE_LINK_TARGET"]):
            os.symlink(kw["env"]["THEODORE_LINK_TARGET"], kw["env"]["THEODORE_LINK"], target_is_directory=True)
    return run


class Junction(unittest.TestCase):
    def test_a_junction_takes_its_paths_through_variables(self):
        tmp = Path(tempfile.mkdtemp()) / STRANGE
        (tmp / "target").mkdir(parents=True)
        seen = []
        with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(core.subprocess, "run", fake_mklink(seen)):
            core.link_dir(tmp / "link", tmp / "target")
        command, env = seen[0]
        self.assertTrue(command.isascii(), command)
        self.assertTrue(command.endswith('/d /v:off /c "mklink /J "%THEODORE_LINK%" "%THEODORE_LINK_TARGET%""'))
        self.assertEqual(str(tmp / "link"), env["THEODORE_LINK"])
        self.assertEqual(os.path.realpath(tmp / "target"), env["THEODORE_LINK_TARGET"])

    def test_the_junction_stores_the_real_path_of_a_target_reached_through_a_link(self):
        """The first Windows user (the Claude desktop app, an MSIX package): the runtime's folder was reached through a
        redirect, a junction to the plain path led nowhere. The junction is given the target's final real path."""
        tmp = Path(tempfile.mkdtemp()) / STRANGE
        (tmp / "real" / "runtime" / "node_modules").mkdir(parents=True)
        os.symlink(tmp / "real", tmp / "seen", target_is_directory=True)
        seen = []
        with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(core.subprocess, "run", fake_mklink(seen)):
            core.link_dir(tmp / "engine" / "node_modules", tmp / "seen" / "runtime" / "node_modules")
        stored = seen[0][1]["THEODORE_LINK_TARGET"]
        self.assertEqual(os.path.realpath(tmp / "real" / "runtime" / "node_modules"), stored)
        self.assertNotIn(os.sep + "seen" + os.sep, stored)

    def test_a_link_whose_target_is_not_reachable_says_so_with_both_paths(self):
        tmp = Path(tempfile.mkdtemp()) / STRANGE
        tmp.mkdir(parents=True)
        with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(core.subprocess, "run", fake_mklink([])):
            with self.assertRaises(core.LinkError) as raised:
                core.link_dir(tmp / "link", tmp / "nowhere")
        message = str(raised.exception)
        self.assertIn(str(tmp / "link"), message)
        self.assertIn(os.path.realpath(tmp / "nowhere"), message)
        self.assertIn("not reachable from here", message)

    def test_an_engine_link_that_leads_nowhere_is_an_engine_error(self):
        """stage_engine records and cleans up an EngineError; the link's error must arrive as one, not as a crash."""
        from theodore_client import install
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(tmp / "home")}), mock.patch.object(core, "_HOME", None), \
                mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(core.subprocess, "run", fake_mklink([])):
            with self.assertRaises(install.EngineError) as raised:
                install._prepare_engine(tmp / "engine.partial", "rt")
        self.assertIn("not reachable from here", str(raised.exception))

    @unittest.skipUnless(os.name == "nt", "a real junction")
    def test_a_real_junction_through_a_junction_stores_the_real_path(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "real" / "node_modules" / "acorn").mkdir(parents=True)
        (tmp / "real" / "node_modules" / "acorn" / "package.json").write_text("{}")
        core.link_dir(tmp / "seen", tmp / "real")
        core.link_dir(tmp / "engine" / "node_modules", tmp / "seen" / "node_modules")
        stored = os.readlink(tmp / "engine" / "node_modules")
        self.assertEqual(os.path.normcase(os.path.realpath(tmp / "real" / "node_modules")),
                         os.path.normcase(stored.removeprefix("\\\\?\\")))
        self.assertTrue((tmp / "engine" / "node_modules" / "acorn" / "package.json").is_file())
        with self.assertRaises(core.LinkError):
            core.link_dir(tmp / "broken", tmp / "missing")


class SelfTestModules(unittest.TestCase):
    def run_self_test(self, in_runtime: bool) -> str:
        from theodore_client import install
        tmp = Path(tempfile.mkdtemp())
        directory, runtime = tmp / "engine", tmp / "home" / "runtime" / "rt"
        (directory / "skills" / "pptx" / "scripts").mkdir(parents=True)
        (directory / "skills" / "pptx" / "scripts" / "pptx.py").write_text("")
        (directory / "package.json").write_text(json.dumps({"dependencies": {"acorn": "1"}}))
        (directory / "node_modules").mkdir()
        if in_runtime:
            (runtime / "node_modules" / "acorn").mkdir(parents=True)
            (runtime / "node_modules" / "acorn" / "package.json").write_text("{}")
        ok = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(tmp / "home")}), mock.patch.object(core, "_HOME", None), \
                mock.patch.object(install.subprocess, "run", lambda *a, **k: ok), \
                mock.patch.object(install, "engine_env", lambda d: {}), mock.patch.object(install, "engine_python", lambda d: "py"):
            with self.assertRaises(install.EngineError) as raised:
                install._self_test(directory, "rt")
        return str(raised.exception)

    def test_a_module_the_runtime_lacks(self):
        self.assertEqual("the runtime rt lacks the Node module acorn", self.run_self_test(in_runtime=False))

    def test_a_module_the_runtime_has_but_the_link_does_not_reach(self):
        message = self.run_self_test(in_runtime=True)
        self.assertIn("the engine's link to the runtime rt does not resolve", message)
        self.assertNotIn("lacks", message)


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
        # Lane ONBOARD: first the client's welcome (only when every assistant was registered); the lines below are what
        # is left when it cannot be given.
        welcome = cmd.index('set "WELCOME_ARG=--downloading"\ncall :welcome\nif not errorlevel 1 goto :finish')
        self.assertLess(welcome, ok)
        self.assertLess(cmd.index('set "WELCOME_ARG="\ncall :welcome\nif not errorlevel 1 goto :finish'),
                        cmd.index("echo THEODORE OK: Theodore is ready (the check above)."))
        self.assertIn('\n:welcome\n', cmd)
        self.assertIn('if not "%HOSTS_RC%"=="0" exit /b 1\ncall "%%T%%" welcome %WELCOME_ARG% >"%WORK%\\welcome.txt"', cmd)
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


class DesktopAppProcess(unittest.TestCase):
    """W-7: Claude Code's CLI is claude.exe too; only the Claude app's own program files count as the app running."""

    def test_only_the_apps_program_files_count(self):
        from theodore_client import desktop
        for path in (r"C:\Users\Théo Tester\AppData\Local\AnthropicClaude\app-3.6556.2\claude.exe",
                     r"C:\Program Files\WindowsApps\Claude_5.11492.5.0_x64__pzs8sxrjxfjjc\app\claude.exe"):
            self.assertTrue(desktop.is_desktop_app(path), path)
        for path in (r"C:\Users\Théo Tester\.local\bin\claude.exe", r"C:\Users\T\AppData\Roaming\npm\claude.exe",
                     r"C:\Users\T\AppData\Local\AnthropicClaude\Update.exe", r"C:\Users\T\AppData\Local\AnthropicClaude\claude.exe",
                     r"C:\Program Files\WindowsApps\Other_1.0\claude.exe", ""):
            self.assertFalse(desktop.is_desktop_app(path), path)

    def test_the_cli_alone_is_not_the_app_running(self):
        from theodore_client import desktop
        out = ("2026-11-15T13:16:39.0000000Z|C:\\Users\\Théo Tester\\.local\\bin\\claude.exe\r\n"
               "2026-11-15T13:20:00.0000000Z|C:\\Users\\Théo Tester\\AppData\\Local\\AnthropicClaude\\app-3.6556.2\\claude.exe\r\n").encode("utf-8")
        with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(desktop.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, out, b"")):
            os.environ.pop("THEODORE_TEST_CLAUDE_APP_STARTED", None)
            from datetime import datetime
            self.assertEqual(datetime.fromisoformat("2026-11-15T13:20:00+00:00").timestamp(), desktop.app_started_at())
        cli_only = out.split(b"\r\n")[0]
        with mock.patch.object(core, "IS_WINDOWS", True), \
                mock.patch.object(desktop.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, cli_only, b"")):
            self.assertIsNone(desktop.app_started_at())


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
        lines = page.splitlines()
        run = lines[next(n for n, l in enumerate(lines) if l.strip().startswith("Windows PowerShell: ")) + 1]
        self.assertTrue(run.strip().startswith("then: try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}; & "), run)


class UninstallRecord(unittest.TestCase):
    def test_a_new_log_on_windows_starts_with_a_utf8_mark_once(self):
        target = Path(tempfile.mkdtemp()) / STRANGE / "Theodore-uninstall.log"
        target.parent.mkdir(parents=True)
        with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(core, "_LOG_FILE", target):
            core.log("uninstall: C:\\Users\\Théo Tester\\AppData\\Local\\Theodore")
            core.log("uninstall: ended")
        raw = target.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(1, raw.count(b"\xef\xbb\xbf"))
        self.assertIn("Théo Tester", raw.decode("utf-8-sig"))

    def test_elsewhere_the_log_has_no_mark(self):
        target = Path(tempfile.mkdtemp()) / "x.log"
        with mock.patch.object(core, "IS_WINDOWS", False), mock.patch.object(core, "_LOG_FILE", target):
            core.log("é")
        self.assertFalse(target.read_bytes().startswith(b"\xef\xbb\xbf"))


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


class CleanupLeavesNoFolder(unittest.TestCase):
    def test_the_script_leaves_its_own_folder_before_removing_it(self):
        """W-12: cmd cannot remove its own current folder, so every uninstall left an empty theodore-uninstall-<random>."""
        lines = commands._cleanup_script(list(core.OWNED), keep_specimen_record=True).split("\r\n")
        last = next(i for i, line in enumerate(lines) if 'rmdir /s /q "%THEODORE_CLEANUP_WORK%"' in line)
        self.assertEqual(lines[last], '(goto) 2>nul & cd /d "%THEODORE_CLEANUP_WORK%\\.." & rmdir /s /q "%THEODORE_CLEANUP_WORK%"',
                         "after (goto), on the same line: the script's setlocal would otherwise put this folder back as current")
        self.assertIn("setlocal", lines[1])
        self.assertLess(lines.index(":end"), last, "on every way out, a stopped cleanup included")


class WindowsShortCommand(unittest.TestCase):
    """Lane PROMPTS: `theodore` in a per-user folder on PATH, for the PowerShell tool and Git Bash alike."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.local = self.tmp / STRANGE / "AppData" / "Local"
        (self.local / "Theodore" / "bin").mkdir(parents=True)
        env = {"LOCALAPPDATA": str(self.local), "USERPROFILE": str(self.tmp / STRANGE), "HOME": str(self.tmp / STRANGE)}
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(core, "_HOME", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("THEODORE_HOME", None)

    def test_the_batch_file_names_no_path_and_the_bash_script_runs_the_sh_launcher(self):
        texts = core.shim_texts()
        self.assertEqual(set(texts), {"theodore.cmd", "theodore"}, "PowerShell and cmd find the .cmd (PATHEXT), Git Bash the script")
        batch = texts["theodore.cmd"]
        self.assertTrue(batch.isascii(), "cmd reads a batch file in the OEM code page: no é in it")
        # The home by the one rule, from the variables (WINHOME): %USERPROFILE%\.theodore, else an earlier installation in
        # %LOCALAPPDATA%\Theodore, THEODORE_HOME first.
        self.assertIn('set "THEODORE_SHIM_HOME=%USERPROFILE%\\.theodore"\r\n', batch)
        self.assertIn('if not exist "%USERPROFILE%\\.theodore\\bin\\launcher.json" if exist "%LOCALAPPDATA%\\Theodore\\bin\\launcher.json" '
                      'set "THEODORE_SHIM_HOME=%LOCALAPPDATA%\\Theodore"\r\n', batch)
        self.assertIn('if defined THEODORE_HOME set "THEODORE_SHIM_HOME=%THEODORE_HOME%"\r\n', batch)
        self.assertTrue(batch.endswith('"%THEODORE_SHIM_HOME%\\bin\\theodore.cmd" %*\r\n'), batch)
        self.assertNotIn(STRANGE, batch, "no path baked in")
        self.assertIn('h=$(cygpath -u "$USERPROFILE" 2>/dev/null)/.theodore\n', texts["theodore"])
        self.assertIn('[ ! -f "$h/bin/launcher.json" ] && [ -f "$o/bin/launcher.json" ] && h=$o\n', texts["theodore"])
        self.assertNotIn("call ", batch.lower(), "no CALL: it doubles carets and expands % again")
        self.assertIn('exec sh "$b/theodore" "$@"', texts["theodore"], "Git Bash: the sh launcher, which carries every argument exactly")
        self.assertTrue(texts["theodore"].startswith("#!/bin/sh\n"))

    def test_the_written_batch_file_is_recognised_as_theodores(self):
        """WINDOWS's recount: read as text, the batch file's CRLF became LF, so it never matched what Theodore writes:
        no short-name rules, and every text kept the long path."""
        texts = core.shim_texts()
        folder = self.tmp / STRANGE / ".local" / "bin"
        for name, text in texts.items():
            core.write_atomic(folder / name, text, 0o755)
        self.assertTrue(all(core.shim_matches(folder / n, t) for n, t in texts.items()))
        with mock.patch.object(commands, "_host_record", lambda: {"shim": {"files": [str(folder / n) for n in texts]}}):
            self.assertTrue(commands._shim_written(), "its rules are written")
        for found in ("theodore.cmd", "theodore.CMD"):       # PATHEXT's spelling, as Windows' which() gives it
            if not (folder / found).exists():                # a case-sensitive disk (Linux): Windows opens either spelling
                core.write_atomic(folder / found, texts[found.lower()], 0o755)
            with mock.patch.dict(os.environ, {"PATH": str(folder)}), mock.patch.object(core.shutil, "which", lambda n, f=found: str(folder / f)):
                core.shim_reset()
                self.assertEqual(core.quoted_launcher(), "theodore", f"and the texts name it ({found})")
                core.shim_reset()

    def test_a_home_elsewhere_gets_no_short_command_on_windows(self):
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.tmp / "elsewhere")}), mock.patch.object(core, "_HOME", None):
            self.assertIsNone(core.shim_texts(), "no path is written into a batch file")
        new = self.tmp / STRANGE / ".theodore"
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(new)}), mock.patch.object(core, "_HOME", None):
            self.assertIsNotNone(core.shim_texts(), "the launchers always set THEODORE_HOME: the default folder named so still gets it")
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.local / "Theodore")}), mock.patch.object(core, "_HOME", None):
            self.assertIsNone(core.shim_texts(), "the earlier folder without an installation: the short command would find another")
        (self.local / "Theodore" / "bin" / "launcher.json").write_text("{}")
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.local / "Theodore")}), mock.patch.object(core, "_HOME", None):
            self.assertIsNotNone(core.shim_texts(), "an installation by 0.4.14 or earlier, which the rule finds: it gets it too")
        (new / "bin").mkdir(parents=True)
        (new / "bin" / "launcher.json").write_text("{}")
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.local / "Theodore")}), mock.patch.object(core, "_HOME", None):
            self.assertIsNone(core.shim_texts(), "both hold one: the short command finds the new one, so the old one gets none")

    def test_the_folders_it_may_use_and_only_on_path(self):
        candidates = core.shim_dir_candidates()
        self.assertEqual(candidates, [self.tmp / STRANGE / ".local" / "bin", self.local / "Microsoft" / "WindowsApps"])
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([str(self.local / "Microsoft" / "WindowsApps"), "/usr/bin"])}):
            self.assertEqual(core.shim_dir(), self.local / "Microsoft" / "WindowsApps", "a stock Windows: WindowsApps is on PATH")
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            self.assertIsNone(core.shim_dir(), "never a folder that is not on PATH")

    def test_powershell_rules_for_the_launcher_the_skill_launchers_and_the_short_command(self):
        launcher = str(core.home().launcher())
        script = self.tmp / "cache" / "skills" / "pptx" / "scripts" / "theodore.cmd"
        with mock.patch.object(commands, "_claude_skill_launchers", lambda: [script]), \
                mock.patch.object(commands, "_render_cache_dirs", lambda: []), \
                mock.patch.object(commands.specimens, "cache_root", lambda: self.local / "Theodore" / "Cache" / "specimens"), \
                mock.patch.object(commands, "_shim_written", lambda: True):
            rules = commands._permission_rules()
        for rule in (f"PowerShell({launcher} *)", f'PowerShell(& "{launcher}" *)', f'PowerShell(& "{script}" *)',
                     "PowerShell(theodore *)", "PowerShell(& theodore *)", "Bash(theodore *)", "Edit(//**/.theodore/workspaces/**)"):
            self.assertIn(rule, rules)
        self.assertFalse([r for r in rules if r in ("PowerShell", "PowerShell(*)", "PowerShell(& *)", "Bash(*)")], "nothing broader")
        with mock.patch.object(commands, "_claude_skill_launchers", lambda: []), mock.patch.object(commands, "_render_cache_dirs", lambda: []), \
                mock.patch.object(commands.specimens, "cache_root", lambda: self.local / "Theodore" / "Cache" / "specimens"), \
                mock.patch.object(commands, "_shim_written", lambda: False):
            rules = commands._permission_rules()
        self.assertFalse([r for r in rules if "(theodore " in r or "(& theodore " in r], "no rule for the name without the command")


class WindowsHomeRule(unittest.TestCase):
    """WINHOME: Theodore's folder on Windows is %USERPROFILE%\\.theodore, which an MSIX app (the Claude desktop app) does not
    redirect; an installation by 0.4.14 or earlier in %LOCALAPPDATA%\\Theodore keeps working where it is."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.profile = self.tmp / STRANGE
        self.local = self.profile / "AppData" / "Local"
        self.new, self.old = self.profile / ".theodore", self.local / "Theodore"
        env = {"LOCALAPPDATA": str(self.local), "USERPROFILE": str(self.profile), "HOME": str(self.profile)}
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(core, "_HOME", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("THEODORE_HOME", None)

    def installed(self, home: Path) -> None:
        (home / "bin").mkdir(parents=True, exist_ok=True)
        (home / "bin" / "launcher.json").write_text('{\n  "launcher_generation": 13\n}\n')

    def home(self) -> Path:
        core._HOME = None
        return core.home().root

    def test_the_rule_in_its_four_cases(self):
        self.assertEqual(self.home(), self.new, "neither holds an installation: a new one goes to %USERPROFILE%\\.theodore")
        self.old.mkdir(parents=True)                # the engine's caches live under it (Theodore\\Cache): not an installation
        (self.old / "Cache").mkdir()
        self.assertEqual(self.home(), self.new)
        self.installed(self.old)
        self.assertEqual(self.home(), self.old, "only the earlier one holds an installation: it keeps working where it is")
        self.installed(self.new)
        self.assertEqual(self.home(), self.new, "the new one holds an installation: it wins")
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.tmp / "chosen")}):
            self.assertEqual(self.home(), self.tmp / "chosen", "THEODORE_HOME first, always")

    def test_what_is_written_for_others_names_the_resolved_home(self):
        self.installed(self.new)
        launcher = self.new / "bin" / "theodore.cmd"
        self.assertEqual(core.home().launcher(), launcher)
        self.assertEqual(core.full_launcher(), f'"{launcher}"')
        with mock.patch.object(core, "shim_active", lambda: False):
            self.assertEqual(core.quoted_launcher(), f'"{launcher}"')
        with mock.patch.object(commands, "_claude_skill_launchers", lambda: []), mock.patch.object(commands, "_render_cache_dirs", lambda: []), \
                mock.patch.object(commands.specimens, "cache_root", lambda: self.local / "Theodore" / "Cache" / "specimens"), \
                mock.patch.object(commands, "_shim_written", lambda: True), \
                mock.patch.object(commands, "_claude_read_rule", lambda folder: f"Read({folder})"):
            rules = commands._permission_rules()
        for rule in (f"Bash({launcher} *)", f'Bash("{launcher}" *)', f"PowerShell({launcher} *)", f'PowerShell(& "{launcher}" *)',
                     f"Read({self.new})"):
            self.assertIn(rule, rules)
        self.assertFalse([r for r in rules if str(self.old / "bin") in r], "nothing names the earlier folder's launcher")
        from theodore_client import routing
        with mock.patch.object(core, "shim_active", lambda: False):
            self.assertIn(f'run "{launcher}" uninstall', routing.sentence())
        from theodore_client import desktop
        self.assertEqual(desktop.entry()["env"]["THEODORE_DESKTOP_LAUNCHER"], f'"{launcher}"', "the Claude app's entry runs it")

    def test_an_earlier_installation_keeps_its_folder_for_everything_it_writes(self):
        self.installed(self.old)
        launcher = self.old / "bin" / "theodore.cmd"
        self.assertEqual(core.home().launcher(), launcher)
        self.assertIsNotNone(core.shim_texts(), "its short command, which finds it by the same rule")
        with mock.patch.object(core, "shim_active", lambda: False):
            self.assertEqual(core.quoted_launcher(), f'"{launcher}"')
        self.assertEqual(core.uninstall_log(), self.local / "Theodore-uninstall.log")

    def test_the_rule_is_the_same_text_in_every_launcher_and_the_installer(self):
        """The batch rule is three lines, literally the same in both skill launchers and install.cmd; the sh launcher's
        Git Bash branch and the short command read the same two places in the same order."""
        rule = ['set "HOME_DIR=%USERPROFILE%\\.theodore"',
                'if not exist "%USERPROFILE%\\.theodore\\bin\\launcher.json" if exist "%LOCALAPPDATA%\\Theodore\\bin\\launcher.json" set "HOME_DIR=%LOCALAPPDATA%\\Theodore"',
                'if defined THEODORE_HOME set "HOME_DIR=%THEODORE_HOME%"']
        root = Path(support.PLUGIN).parents[1]
        for path in (Path(support.PLUGIN) / "skills" / "pptx" / "scripts" / "theodore.cmd",
                     Path(support.PLUGIN) / "skills" / "deck-design" / "scripts" / "theodore.cmd", root / "install" / "install.cmd"):
            lines = path.read_bytes().decode("utf-8").split("\r\n")
            start = lines.index(rule[0])
            self.assertEqual(lines[start:start + 3], rule, path)
            self.assertFalse([l for l in lines if l.startswith('set "HOME_DIR=%LOCALAPPDATA%')], path)
        sh = (Path(support.PLUGIN) / "skills" / "pptx" / "scripts" / "theodore").read_text(encoding="utf-8")
        self.assertIn('wh_new="${USERPROFILE:-}\\\\.theodore" wh_old="${LOCALAPPDATA:-}\\\\Theodore"', sh)
        self.assertNotIn("${LOCALAPPDATA:-}\\\\Theodore}", sh, "no copy of the earlier default is left")
        self.assertEqual(sh.count("$(win_home)"), 2, "the session-start hook and the Git Bash branch")


class StableLauncherRefresh(unittest.TestCase):
    """WINHOME: an update brings <home>\\bin's stable launcher up to a newer launcher of the plugin (Windows files)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / STRANGE / ".theodore"
        patches = [mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.home)}), mock.patch.object(core, "IS_WINDOWS", True),
                   mock.patch.object(core, "_HOME", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def launcher(self, folder: Path, generation: int, api: str = "https://api.example.test") -> dict:
        folder.mkdir(parents=True, exist_ok=True)
        files = {"theodore.cmd": f"@echo off\r\nrem generation {generation}\r\n".encode(), "theodore": f"#!/bin/sh\n# {generation}\n".encode(),
                 "launcher.json": f'{{\r\n  "launcher_generation": {generation},\r\n  "api_base": "{api}"\r\n}}\r\n'.encode()}
        for name, data in files.items():
            (folder / name).write_bytes(data)
        return files

    def test_a_newer_generation_replaces_all_three_files_byte_for_byte(self):
        self.launcher(self.home / "bin", 12)
        plugin = self.tmp / "plugin" / "skills" / "pptx" / "scripts"
        files = self.launcher(plugin, 13)
        self.assertEqual(core.refresh_stable_launcher([plugin]), "stable launcher: generation 12 -> 13")
        for name, data in files.items():
            self.assertEqual((self.home / "bin" / name).read_bytes(), data, f"{name}: CRLF kept")
        self.assertEqual([p.name for p in (self.home / "bin").iterdir() if p.name.endswith(".tmp")], [], "no half-written file left")

    def test_an_equal_or_older_one_or_another_services_is_left_alone(self):
        stable = self.launcher(self.home / "bin", 13)
        for generation, api in ((13, "https://api.example.test"), (12, "https://api.example.test"), (14, "https://api.elsewhere.test")):
            plugin = self.tmp / f"plugin-{generation}"
            self.launcher(plugin, generation, api)
            self.assertIsNone(core.refresh_stable_launcher([plugin]), generation)
        for name, data in stable.items():
            self.assertEqual((self.home / "bin" / name).read_bytes(), data)

    def test_no_stable_launcher_yet_is_not_made_here(self):
        plugin = self.tmp / "plugin"
        self.launcher(plugin, 13)
        self.assertIsNone(core.refresh_stable_launcher([plugin]))
        self.assertFalse((self.home / "bin").exists(), "the launcher makes the first stable copy itself")


class CodexServerOnWindows(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "mcp.json"

    def test_the_sh_entry_becomes_the_batch_command(self):
        published = json.loads((Path(support.PLUGIN) / "mcp.json").read_text())
        self.path.write_text(json.dumps(published))
        commands._codex_mcp_for_windows(self.path)
        entry = json.loads(self.path.read_text())["mcpServers"]["theodore"]
        self.assertEqual(entry["command"], "./scripts/theodore.cmd", "the plugin's command: the front, else the launcher")
        self.assertEqual(entry["args"], ["mcp", "--for", "codex"])
        self.assertEqual(entry["type"], "stdio")
        self.assertTrue((Path(support.PLUGIN) / "scripts" / "theodore.cmd").is_file())
        # A plugin from before the front: its launcher, the form lane WINDOWS saw Codex start.
        published["mcpServers"]["theodore"]["args"] = ["./skills/pptx/scripts/theodore", "mcp", "--for", "codex"]
        self.path.write_text(json.dumps(published))
        commands._codex_mcp_for_windows(self.path)
        entry = json.loads(self.path.read_text())["mcpServers"]["theodore"]
        self.assertEqual(entry["command"], "./skills/pptx/scripts/theodore.cmd")
        self.assertTrue((Path(support.PLUGIN) / "skills" / "pptx" / "scripts" / "theodore.cmd").is_file())

    def test_any_other_entry_is_left_as_it_is(self):
        other = {"mcpServers": {"theodore": {"type": "stdio", "command": "node", "args": ["x"]}}}
        self.path.write_text(json.dumps(other))
        commands._codex_mcp_for_windows(self.path)
        self.assertEqual(json.loads(self.path.read_text()), other)
        self.path.write_text("not json")
        commands._codex_mcp_for_windows(self.path)
        self.assertEqual(self.path.read_text(), "not json")


class GitBashBeingFetched(unittest.TestCase):
    def test_a_running_portablegit_download_is_said_with_how_far(self):
        """W-9: doctor told the person to install Git for Windows while Theodore's own PortableGit was downloading."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"THEODORE_HOME": tmp}), mock.patch.object(core, "_HOME", None):
            self.assertIsNone(commands._git_fetching())
            job = commands.Job("git")
            job.begin("git")
            job.progress("downloading", 40)
            self.assertEqual(commands._git_fetching(), "downloading, 40%")
            job.finish()
            self.assertIsNone(commands._git_fetching())


class InstallCmdRateLimit(unittest.TestCase):
    """W-16 in install.cmd (its run is on Windows CI): the 429 rate_limited answer is told apart from a wrong code, and
    the wait is the 7th space-separated word of the service's body, as for /f "tokens=7" reads it."""

    def test_the_branch_and_the_word_it_reads(self):
        text = (Path(support.ROOT) / "install" / "install.cmd").read_text(encoding="ascii")
        self.assertIn('if "%HTTP%"=="429" "%SYS32%\\findstr.exe" /c:"rate_limited"', text)
        self.assertIn('for /f "usebackq tokens=7 delims= " %%S in ("%SAFE%\\activation.pending")', text)
        self.assertIn("echo THEODORE RETRY: Theodore is installed, but this computer is not signed in: there have been too many log-ins", text)
        body = json.dumps({"error": {"code": "rate_limited", "message": "Too many requests. Try again in 1500 seconds.",
                                     "request_id": "r"}}, separators=(",", ":"))
        self.assertEqual(body.split(" ")[6], "1500", "the service's JSON body, as the batch file splits it")


class InstallCmdComputerLimit(unittest.TestCase):
    """W-17 in install.cmd (its run is on Windows CI): the 409 computer_limit answer is kept for the client's last status
    (no sign-in link), and no answer or a 5xx is never called a code that was not accepted. As install.sh."""

    def test_the_branches(self):
        text = (Path(support.ROOT) / "install" / "install.cmd").read_text(encoding="ascii")
        self.assertIn('if "%HTTP%"=="409" "%SYS32%\\findstr.exe" /c:"computer_limit" "%SAFE%\\activation.pending" >nul 2>&1 && '
                      'move /y "%SAFE%\\activation.pending" "%WORK%\\activation.refused"', text)
        self.assertIn(":sign_in_last\nif defined LIMIT_REFUSED goto :limit_last\n", text)
        self.assertIn('call "%%T%%" login --refused "%%LIMIT_REFUSED%%"', text)
        self.assertIn('set "LIMIT_REFUSED="', text)
        for http in ('"%HTTP%"=="000"', '"%HTTP:~0,1%"=="5"'):
            self.assertIn(f"if {http} goto :stage1", text)
        sh = (Path(support.ROOT) / "install" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("theodore login --refused \"$LIMIT_REFUSED\"", sh)


class OneLibreOfficeInstall(unittest.TestCase):
    """W-15: a second LibreOffice install while one runs (the installer's background job plus `tools install
    libreoffice`) raced over the same .part file and ended INTERNAL; it now reports the first one's progress."""

    def test_a_second_install_reports_the_running_one_and_downloads_nothing(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"THEODORE_HOME": tmp}), \
                mock.patch.object(core, "_HOME", None):
            first = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            self.addCleanup(first.kill)
            core.write_json(core.home().jobs / "libreoffice.json", {"pid": first.pid, "phase": "downloading", "percent": 40, "started": 1})
            with mock.patch.object(commands.install, "system_libreoffice", lambda: None), \
                    mock.patch.object(commands.install, "libreoffice_binary", lambda: None), \
                    mock.patch.object(commands.install, "libreoffice_pin", lambda: {"version": "26.2.7", "size": 350_000_000}), \
                    mock.patch.object(commands.install, "install_libreoffice", side_effect=AssertionError("a second download")):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = commands.tools("libreoffice", background=False, as_job=False)
                self.assertEqual(code, 0)
                self.assertIn("THEODORE OK: LibreOffice is already being installed in the background (downloading, 40%)", out.getvalue())
                self.assertEqual(commands.tools("libreoffice", background=False, as_job=True), 0, "a second job just leaves")


class CutCommandLine(unittest.TestCase):
    """cmd ends a command line at its first line feed: an argument of several lines leaves a quote open, and the rest of
    theodore.cmd's run line arrives inside the client's last argument. The client knows that text and runs nothing."""

    def test_the_marker_is_the_text_after_the_arguments_on_the_launchers_run_line(self):
        from theodore_client import cli
        for folder in ("pptx", "deck-design"):
            text = (support.PLUGIN / "skills" / folder / "scripts" / "theodore.cmd").read_text(encoding="ascii")
            runs = [line for line in text.splitlines() if line.startswith('"%PY%" -I "%HOME_DIR%\\client\\%CV%" %* ')]
            self.assertEqual(1, len(runs), folder)
            self.assertTrue(runs[0].split(" %* ", 1)[1].startswith(cli.LAUNCHER_TAIL), runs[0][:160])

    def test_the_run_line_is_followed_by_the_end_of_the_batch(self):
        lines = (support.LAUNCHER_DIR / "theodore.cmd").read_text(encoding="ascii").splitlines()
        run = next(i for i, line in enumerate(lines) if line.startswith('"%PY%" -I "%HOME_DIR%\\client\\%CV%" %* '))
        self.assertEqual("call exit /b %%ERRORLEVEL%%", lines[run + 1], "a cut run line falls through only to the end of the batch")
        self.assertLess(lines.index(":mcp_not_ready"), run, "no label follows the run line where a fall-through would reach it")

    def test_an_argument_holding_the_marker_is_refused_on_every_platform(self):
        from theodore_client import cli
        argv = ["report", "--kind", "bug", "--title", "t t t", "--details", "first line " + cli.LAUNCHER_TAIL + ": Theodore's Python"]
        status = cli.cut_command_line(argv)
        self.assertEqual("USAGE", status.name)
        self.assertIn("--details-file", status.next_step)
        self.assertIsNone(cli.cut_command_line(["report", "--details", "an ordinary & line (with brackets)"]))
