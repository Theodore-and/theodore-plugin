"""0.4.32: an uninstall's cleanup on Windows never removes what an installation started meanwhile writes.

Seen on the Windows test machine on 2026-11-23: `theodore uninstall` answered OK and started its cleanup; an install
started a few seconds later removed the cleanup's marker, waited five seconds and wrote staging\\install-<n>\\python.tar.gz,
which tar then could not open: the cleanup checked the marker once a pass, and the pass that had begun before the
marker went was still removing folders one after another. The installer ended SETUP_FAILED DISK ("the disk may be
full"), which was not true.

Now the cleanup renames each folder before removing it, removes the safe file and looks for the marker before every
step that could reach what an installation writes, and writes the safe file only where no such step is under way; the
installer and the launchers wait for that file (at most a minute) instead of five seconds.

cmd.exe cannot run here, so the script is run by a small interpreter of the few commands it uses, on a real folder,
one command at a time, with an installer acting between the commands as install.cmd does. A folder removal takes one
step per file, so a large folder takes as long, relative to the installer, as it does on Windows.
"""

from __future__ import annotations

import fnmatch
import os
import random
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (puts the client on sys.path)

from theodore_client import commands, core  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
# By value too, so that this file also runs against a client before 0.4.32 (and fails there, as it should).
SAFE = getattr(commands, "CLEANUP_SAFE", ".theodore-cleanup-safe")
REMOVING = getattr(commands, "REMOVING", ".removing-")
SECOND = 100            # interpreter steps in one second: `ping -n 2` waits one second


class Cmd:
    """The cleanup script run command by command on a real folder (see the module's text). `held` are files a process
    still has open: they cannot be deleted, and a folder holding one cannot be renamed."""

    def __init__(self, text: str, env: dict, held=()):
        self.lines = text.split("\r\n")
        self.labels = {line[1:].split()[0].lower(): i for i, line in enumerate(self.lines) if line.startswith(":")}
        self.env = dict(env)
        self.vars: dict[str, str] = {}
        self.held = {Path(p) for p in held}
        self.clock = 0
        self.ran: list[str] = []

    # --- paths and expansion ---------------------------------------------------------------------------------------
    @staticmethod
    def path(text: str) -> Path:
        return Path(text.replace("\\", os.sep))

    def expand(self, text: str, args: list[str]) -> str:
        text = re.sub(r"%~(\d)", lambda m: args[int(m.group(1)) - 1] if len(args) >= int(m.group(1)) else "", text)
        def var(m):
            name = m.group(1)
            if name == "RANDOM":
                return str(random.randint(0, 32767))
            return self.env.get(name, self.vars.get(name, ""))
        return re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", var, text)

    def matches(self, pattern: str, dirs: bool) -> list[Path]:
        where = self.path(pattern)
        if not where.parent.is_dir():
            return []
        return sorted(p for p in where.parent.iterdir() if fnmatch.fnmatchcase(p.name, where.name) and p.is_dir() == dirs)

    def is_held(self, path: Path) -> bool:
        return any(h == path or path in h.parents for h in self.held)

    # --- running -----------------------------------------------------------------------------------------------------
    def run(self, args=()):
        """A generator: one yield per command (and per file removed); returns at the script's end."""
        yield from self.block(0, list(args))

    def block(self, start: int, args: list[str]):
        at = start
        while at < len(self.lines):
            line = self.lines[at].strip()
            at += 1
            if not line or line.startswith(":") or line.startswith("@") or line.lower().startswith("setlocal"):
                continue
            action = yield from self.stmt(line, args)
            if action is None:
                continue
            if action[0] == "goto":
                at = self.labels[action[1].lower()] + 1
            elif action[0] in ("exit", "end"):
                return action
        return ("exit", "0")

    def step(self, n: int = 1):
        self.clock += n
        yield

    def stmt(self, line: str, args: list[str]):
        yield from self.step()
        if line.startswith("(goto)"):
            return ("end",)
        if line.startswith("(") and line.endswith(")"):
            for part in line[1:-1].split("& "):
                action = yield from self.stmt(part.strip(), args)
                if action:
                    return action
            return None
        m = re.match(r'for (/d )?%%(\w) in \("([^"]*)"\) do (.*)$', line)
        if m:
            dirs, letter, pattern, body = bool(m.group(1)), m.group(2), self.expand(m.group(3), args), m.group(4)
            for found in self.matches(pattern, dirs):
                each = body.replace(f"%%~f{letter}", str(found).replace(os.sep, "\\")).replace(f"%%~nx{letter}", found.name) \
                    .replace(f"%%{letter}", str(found).replace(os.sep, "\\"))
                action = yield from self.stmt(each, args)
                if action:
                    return action
            return None
        line = self.expand(line, args)
        self.ran.append(line)
        m = re.match(r'if (/i )?(not )?(.*)$', line)
        if m and line.startswith("if "):
            negate, rest = bool(m.group(2)), m.group(3)
            if rest.startswith("exist "):
                target, body = re.match(r'exist "([^"]*)" (.*)$', rest).groups()
                if "*" in target:
                    found = bool(self.matches(target, True) or self.matches(target, False))
                else:
                    found = self.path(target).exists()
            elif rest.startswith("defined "):
                name, body = rest[len("defined "):].split(" ", 1)
                found = bool(self.vars.get(name))
            elif rest.startswith('"'):
                left, right, body = re.match(r'"([^"]*)"=="([^"]*)" (.*)$', rest).groups()
                found = left.lower() == right.lower() if m.group(1) else left == right
            else:
                left, right, body = re.match(r'(\S+) geq (\S+) (.*)$', rest).groups()
                found = int(left or 0) >= int(right)
            if found != negate:
                return (yield from self.stmt(body, args))
            return None
        if line.startswith("goto "):
            return ("goto", line.split()[1])
        if line.startswith("exit /b"):
            return ("exit", line.split()[2])
        if line.startswith("call :"):
            parts = line[len("call :"):].split()
            called = [p.strip('"') for p in parts[1:]]
            yield from self.block(self.labels[parts[0].lower()] + 1, called)
            return None
        if line.startswith("set /a "):
            name, amount = re.match(r"set /a (\w+)\+=(\d+)", line).groups()
            self.vars[name] = str(int(self.vars.get(name) or 0) + int(amount))
            return None
        if line.startswith("set "):
            assignment = line[4:].strip()
            if assignment.startswith('"'):
                assignment = assignment[1:-1]
            name, _eq, value = assignment.partition("=")
            self.vars[name] = value
            return None
        if line.startswith("ping -n "):
            yield from self.step((int(line.split()[2]) - 1) * SECOND)
            return None
        if line.startswith("type nul >"):
            target = self.path(re.match(r'type nul >"([^"]*)"', line).group(1))
            if target.parent.is_dir():
                target.touch()
            return None
        if line.startswith(">>") or line.startswith(">"):
            target, text = re.match(r'>>?"([^"]*)" echo (.*)$', line).groups()
            with open(self.path(target), "a" if line.startswith(">>") else "w", encoding="utf-8") as out:
                out.write(text + "\n")
            return None
        if line.startswith("ren "):
            source, name = re.match(r'ren "([^"]*)" "([^"]*)"', line).groups()
            source = self.path(source)
            if source.exists() and not self.is_held(source) and not (source.parent / name).exists():
                source.rename(source.parent / name)
            return None
        if line.startswith("rmdir /s /q "):
            target = self.path(re.match(r'rmdir /s /q "([^"]*)"', line).group(1))
            if target.is_dir():
                for top, dirs, files in os.walk(target, topdown=False):
                    for name in files:
                        file = Path(top) / name
                        if not self.is_held(file):
                            file.unlink()
                        yield from self.step()
                    for name in dirs:
                        if not any((Path(top) / name).iterdir()):
                            (Path(top) / name).rmdir()
                if not any(target.iterdir()):
                    target.rmdir()
            return None
        if line.startswith("rmdir "):
            target = self.path(re.match(r'rmdir "([^"]*)"', line).group(1))
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
            return None
        if line.startswith("del /f /q "):
            target = self.path(re.match(r'del /f /q "([^"]*)"', line).group(1))
            if target.is_file() and not self.is_held(target):
                target.unlink()
            elif target.is_dir():           # del on a folder deletes the files in it, as cmd's does
                for file in target.iterdir():
                    if file.is_file() and not self.is_held(file):
                        file.unlink()
            return None
        raise AssertionError(f"the interpreter does not know this command: {line}")


class Installer:
    """What install.cmd does from its marker line on: the marker goes, then it waits for the safe file (polled every
    second, at most 60), then it writes its working file in staging\\ and the runtime, as on the test machine."""

    def __init__(self, root: Path, start: int):
        self.root, self.start, self.state, self.waited, self.next = root, start, "idle", 0, start
        self.written: list[Path] = []
        self.busy = False

    def act(self, clock: int):
        if clock < self.next or self.state == "wrote":
            return
        if self.state == "idle":
            marker = self.root / commands.UNINSTALL_MARKER
            if not marker.exists():
                self.state = "write"
            else:
                marker.unlink()
                self.state = "wait"
        if self.state == "wait":
            if (self.root / SAFE).exists():
                self.state = "write"
            elif self.waited >= 60:
                self.busy, self.state = True, "write"
            else:
                self.waited += 1
                self.next = clock + SECOND
                return
        if self.state == "write":
            (self.root / SAFE).unlink(missing_ok=True)
            for rel in ("staging/install-1/python.tar.gz", "runtime/python/python.exe", "bin/theodore.cmd"):
                path = self.root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("new installation")
                self.written.append(path)
            self.state = "wrote"


class CleanupRaces(unittest.TestCase):
    NAMES = list(core.OWNED)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "home" / ".theodore"
        self.work = self.tmp / "temp" / "theodore-uninstall-1"
        self.work.mkdir(parents=True)
        # What the client leaves for the cleanup on Windows: its own Python (large), its code and the launcher.
        for i in range(3000):
            path = self.root / "runtime" / f"lib{i // 100}" / f"m{i}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        for rel in ("bin/theodore.cmd", "bin/launcher.json", "client/0.4.31/__main__.py", "staging/old.tar"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("old")
        (self.root / commands.UNINSTALL_MARKER).write_text("1\n")
        self.result = self.work / "result.txt"
        self.env = {"THEODORE_CLEANUP_ROOT": str(self.root), "THEODORE_CLEANUP_RESULT": str(self.result),
                    "THEODORE_CLEANUP_WORK": str(self.work), "THEODORE_CLEANUP_SCRIPT": str(self.work / "cleanup.cmd"),
                    "THEODORE_CLEANUP_KEEP": "specimens.txt"}

    def run_cleanup(self, installer: Installer | None = None, held=(), kill_when=None, keep=False):
        cmd = Cmd(commands._cleanup_script(self.NAMES, keep), self.env, held)
        for _ in cmd.run():
            if installer:
                installer.act(cmd.clock)
            if kill_when and kill_when():
                break
        if installer:                   # the cleanup has ended (or was killed): the installer goes on as it would
            for tick in range(70):
                installer.act(cmd.clock + tick * SECOND)
        return cmd

    def result_lines(self) -> list[str]:
        return self.result.read_text(encoding="utf-8").splitlines() if self.result.exists() else []

    def test_a_no_install_follows_everything_goes(self):
        cmd = self.run_cleanup()
        self.assertFalse(self.root.exists(), "the folder is removed")
        self.assertEqual(self.result_lines(), ["passes: 0", "removed: the folder"])
        self.assertTrue(any(line.startswith("ren ") for line in cmd.ran), "each folder is renamed before it is removed")

    def test_b_an_install_that_starts_mid_pass_keeps_every_file_it_writes(self):
        # One second into the removal of the large folder: the pass that began before the marker went is under way.
        installer = Installer(self.root, start=2 * SECOND + 1000)
        self.run_cleanup(installer)
        self.assertEqual(installer.state, "wrote")
        self.assertFalse(installer.busy, "the cleanup answered")
        self.assertLessEqual(installer.waited, 1, "the answer is there at once while only renamed folders are removed")
        for path in installer.written:
            self.assertTrue(path.exists(), f"{path} was removed by the cleanup")
        self.assertIn("stopped: a new installation started in the folder", self.result_lines())
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if REMOVING in p.name), [], "nothing renamed is left")

    def test_c_an_install_before_the_first_pass_stops_it_before_it_touches_anything(self):
        installer = Installer(self.root, start=10)          # during the cleanup's first pause
        self.run_cleanup(installer)
        self.assertEqual(installer.state, "wrote")
        self.assertFalse(installer.busy)
        self.assertTrue((self.root / "client" / "0.4.31" / "__main__.py").exists(), "nothing was removed")
        self.assertEqual(len(list((self.root / "runtime").rglob("m*.py"))), 3000)
        for path in installer.written:
            self.assertTrue(path.exists())
        self.assertEqual(self.result_lines(), ["stopped: a new installation started in the folder"])

    def test_d_a_folder_with_a_held_file_is_removed_in_place_and_an_install_waits_for_that_step(self):
        held = [self.root / "runtime" / "lib0" / "m0.py"]
        installer = Installer(self.root, start=2 * SECOND + 1000)       # mid-way through the removal in place
        cmd = self.run_cleanup(installer, held=held)
        self.assertTrue(any(line.startswith("rmdir /s /q ") and line.endswith('runtime" >nul 2>&1') for line in cmd.ran),
                        "the folder that could not be renamed was removed in place")
        self.assertEqual(installer.state, "wrote")
        self.assertFalse(installer.busy, "the step in place ended within the minute and the cleanup answered")
        self.assertGreater(installer.waited, 0, "the installer waited while the folder was removed in place")
        for path in installer.written:
            self.assertTrue(path.exists(), f"{path} was removed by the cleanup")
        self.assertTrue(held[0].exists())
        self.assertIn("stopped: a new installation started in the folder", self.result_lines())

    def test_e_a_cleanup_killed_between_rename_and_removal_harms_no_install_and_goes_at_the_next_uninstall(self):
        renamed = lambda: any(REMOVING in p.name and p.name.startswith("runtime") for p in self.root.iterdir())  # noqa: E731
        self.run_cleanup(kill_when=renamed)
        left = [p for p in self.root.iterdir() if REMOVING in p.name]
        self.assertTrue(left, "killed after the rename")
        self.assertTrue((self.root / commands.UNINSTALL_MARKER).exists(), "a killed cleanup leaves its marker")
        installer = Installer(self.root, start=0)       # no cleanup runs: no answer comes, the wait ends after a minute
        for tick in range(70):
            installer.act(tick * SECOND)
        self.assertEqual(installer.state, "wrote")
        self.assertTrue(installer.busy)
        self.assertEqual(installer.waited, 60)
        self.assertTrue(all(p.exists() for p in installer.written))
        self.assertTrue(all(p.exists() for p in left), "the leftover is not the installation's and it does not touch it")
        # The next uninstall: its cleanup removes the leftover with the rest.
        (self.root / commands.UNINSTALL_MARKER).write_text("1\n")
        self.result.unlink(missing_ok=True)
        self.run_cleanup()
        self.assertFalse(self.root.exists())

    def test_the_record_of_specimen_folders_stays_and_each_entry_of_state_is_checked_first(self):
        for rel in ("state/specimens.txt", "state/environment", "state/jobs/a.json"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("1")
        self.run_cleanup(keep=True)
        self.assertEqual(sorted(p.name for p in (self.root / "state").iterdir()), ["specimens.txt"])
        self.assertFalse((self.root / "runtime").exists())


class ScriptText(unittest.TestCase):
    def setUp(self):
        self.lines = commands._cleanup_script(list(core.OWNED), keep_specimen_record=True).split("\r\n")

    def test_every_step_that_can_reach_an_installation_first_drops_the_answer_and_looks_for_the_marker(self):
        safe, marker = f'"%THEODORE_CLEANUP_ROOT%\\{SAFE}"', f'"%THEODORE_CLEANUP_ROOT%\\{commands.UNINSTALL_MARKER}"'
        risky = [i for i, line in enumerate(self.lines)
                 if (line.startswith(("ren ", "rmdir ", 'if "%~1"==')) or (line.startswith("if not defined STOP")))
                 and REMOVING not in line]
        self.assertTrue(risky)
        for i in risky:
            before = self.lines[max(0, i - 6):i]
            drop = max((j for j, line in enumerate(before) if line == f"del /f /q {safe} >nul 2>&1"), default=None)
            look = max((j for j, line in enumerate(before) if marker in line and "not exist" in line), default=None)
            self.assertIsNotNone(drop, self.lines[i])
            self.assertIsNotNone(look, self.lines[i])
            self.assertLess(drop, look, self.lines[i])
            self.assertFalse(any(line.startswith("type nul >") for line in before[drop:]), self.lines[i])

    def test_the_answer_is_written_only_after_the_pass_and_when_stopped(self):
        written = [i for i, line in enumerate(self.lines) if line.startswith("type nul >")]
        self.assertEqual(len(written), 2)
        self.assertEqual(self.lines[written[0] - 1], "if defined STOP goto stopped")
        self.assertEqual(self.lines[written[1] - 2], ":stopped")
        self.assertTrue("".join(self.lines).isascii())


class InstallerAndLaunchers(unittest.TestCase):
    FILES = ("install/install.cmd", "plugins/theodore/skills/pptx/scripts/theodore.cmd",
             "plugins/theodore/skills/deck-design/scripts/theodore.cmd")

    def test_each_waits_for_the_cleanups_answer_not_five_seconds(self):
        for rel in self.FILES:
            text = (REPO / rel).read_bytes().decode("ascii")
            self.assertIn('if exist "%HOME_DIR%\\.theodore-uninstalling" call :stop_cleanup\r\n', text, rel)
            self.assertNotIn("ping -n 6", text, rel)
            body = text.split("\r\n:stop_cleanup\r\n", 1)[1].split("exit /b 0", 1)[0]
            self.assertIn(f'if exist "%HOME_DIR%\\{SAFE}" goto :stop_cleanup_done', body, rel)
            self.assertIn("GEQ 60", body, rel)
            self.assertLess(body.index("del /f /q \"%HOME_DIR%\\.theodore-uninstalling\""), body.index(":stop_cleanup_wait"), rel)
            self.assertIn("if defined CLEANUP_BUSY goto :cleanup_busy", text.split("\r\n:not_writable\r\n", 1)[1].split("\r\n")[0], rel)
            self.assertIn("THEODORE RETRY: an uninstall that ran just before is still removing", text, rel)

    def test_a_launchers_job_says_uninstalling_and_the_next_call_reads_it(self):
        text = (REPO / self.FILES[1]).read_bytes().decode("ascii")
        self.assertIn(":job_disk\r\nif defined CLEANUP_BUSY call :fail UNINSTALLING\r\nif not defined CLEANUP_BUSY call :fail DISK\r\n", text)
        self.assertIn('if /i "%F_CAUSE%"=="UNINSTALLING" goto :cleanup_busy', text)

    def test_the_posix_side_has_no_cleanup_to_wait_for(self):
        for rel in ("install/install.sh", "plugins/theodore/skills/pptx/scripts/theodore"):
            self.assertNotIn("theodore-uninstalling", (REPO / rel).read_text(encoding="utf-8"), rel)


if __name__ == "__main__":
    unittest.main()
