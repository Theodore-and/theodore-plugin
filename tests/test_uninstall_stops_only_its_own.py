"""0.4.32: an uninstall stops a process only when it is provably Theodore's own.

Proven on the Windows test machine on 2026-11-23 with 0.4.31: a harmless stand-in (PING.EXE, not started by Theodore)
was given the number in a finished job's record, and `theodore uninstall` stopped it, with taskkill /T /F, which also
stops everything it started. Job records keep their number after the job ends, and Windows hands numbers out again.

Now a recorded number is acted on only when the record is not of a finished job, the process holding it runs from
Theodore's folder, and it started no later than the record's last write; never this process or anything above it.
These run on this computer with real processes (a stranger, a stand-in for a job); nothing above this test is ever
signalled: where an ancestor is involved only the choice is checked, with nothing stopped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (puts the client on sys.path)

from theodore_client import commands, core  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


@unittest.skipIf(core.IS_WINDOWS, "real processes are signalled the POSIX way here; the Windows test machine runs the same cases")
class StopsOnlyItsOwn(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "home" / ".theodore"
        (self.root / "state" / "jobs").mkdir(parents=True)
        (self.root / "logs").mkdir()
        patches = [mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.root)}), mock.patch.object(core, "_HOME", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(core.home().root, self.root)

    def start(self, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen(list(args), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=proc.wait, daemon=True).start()        # reaped as soon as it ends: no zombie stays "alive"
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(0.3)
        return proc

    def stranger(self) -> subprocess.Popen:
        return self.start("sleep", "60")

    def theodore_like(self) -> subprocess.Popen:
        """A process whose command line names Theodore's folder, as a job's does (its Python and client are in it)."""
        return self.start(sys.executable, "-c", "import time; time.sleep(60)", str(self.root / "client" / "0.4.32"))

    def record(self, name: str, **state) -> Path:
        path = self.root / "state" / "jobs" / f"{name}.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def alive_after_uninstall_stop(self, proc: subprocess.Popen) -> bool:
        commands._stop_theodore_processes(wait=3.0)
        time.sleep(0.3)
        return proc.poll() is None

    def test_a_finished_jobs_record_with_a_strangers_number_stops_nothing(self):
        proc = self.stranger()
        self.record("packs", pid=proc.pid, done=True, started=int(time.time()) - 7200)
        self.record("setup", pid=proc.pid, failed={"cause": "NETWORK", "detail": "x"}, started=int(time.time()) - 7200)
        self.assertEqual(commands._theodore_pids(), {})
        self.assertTrue(self.alive_after_uninstall_stop(proc), "the stranger was stopped")

    def test_an_unfinished_record_whose_number_a_stranger_holds_stops_nothing(self):
        proc = self.stranger()                  # runs nothing of Theodore's
        self.record("libreoffice", pid=proc.pid, phase="download", started=int(time.time()))
        self.assertTrue(self.alive_after_uninstall_stop(proc), "a process that does not run from Theodore's folder was stopped")

    def test_a_number_taken_over_after_the_record_was_written_is_not_stopped(self):
        path = self.record("libreoffice", pid=0, phase="download", started=int(time.time()) - 60)
        os.utime(path, (time.time() - 60, time.time() - 60))          # the job last wrote its record a minute ago
        proc = self.theodore_like()                                     # started after that, holding the number now
        state = json.loads(path.read_text())
        state["pid"] = proc.pid
        path.write_text(json.dumps(state))
        os.utime(path, (time.time() - 60, time.time() - 60))
        self.assertEqual(commands._theodore_pids(), {})
        self.assertTrue(self.alive_after_uninstall_stop(proc), "a process started after its record was stopped")

    def test_a_running_job_of_theodores_is_stopped(self):
        proc = self.theodore_like()
        self.record("libreoffice", pid=proc.pid, phase="download", percent=40, started=int(time.time()))
        self.assertEqual(list(commands._theodore_pids()), [proc.pid])
        self.assertFalse(self.alive_after_uninstall_stop(proc), "Theodore's own job was left running")

    def test_a_tool_servers_and_the_launchers_records_are_proven_the_same_way(self):
        servers = self.root / "state" / "servers"
        servers.mkdir()
        bootstrap = self.root / "state" / "bootstrap"
        bootstrap.mkdir()
        stranger = self.stranger()
        (servers / str(stranger.pid)).write_text(str(stranger.pid))
        (bootstrap / "pid").write_text(f"{stranger.pid}\n")
        self.assertEqual(commands._theodore_pids(), {})
        server = self.theodore_like()
        (servers / str(server.pid)).write_text(str(server.pid))
        self.assertEqual(list(commands._theodore_pids()), [server.pid])

    def test_this_process_and_every_process_above_it_are_never_chosen(self):
        mine = core.ancestors()
        self.assertIn(os.getpid(), mine)
        self.assertIn(os.getppid(), mine)
        parents = core._parents()
        if parents.get(os.getppid(), 0) > 1:
            self.assertIn(parents[os.getppid()], mine, "the parent's parent too")
        above = sorted(p for p in mine if p > 1)
        for i, pid in enumerate(above):
            self.record(f"job{i}", pid=pid, phase="x", started=int(time.time()))
        # Every one of them would pass the proof (as if each ran from Theodore's folder): still none is chosen.
        with mock.patch.object(core, "process_facts", lambda pid: (str(self.root / "runtime" / "python"), 0.0)):
            self.assertEqual(commands._theodore_pids(), {})
            # As Windows would list them (its own walk of the parents cannot run here: the walk above is kept).
            with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(core, "ancestors", lambda: mine), \
                    mock.patch.object(commands, "_run", lambda cmd, timeout=0: (0, "\n".join(str(p) for p in above + [999999]))):
                self.assertEqual(commands._processes_in_folder(self.root), [999999], "the folder scan leaves them out too")


class RecordsAndLauncher(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.tmp / ".theodore")}), mock.patch.object(core, "_HOME", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_finished_or_failed_job_no_longer_names_a_process(self):
        job = core.Job("packs")
        job.path.parent.mkdir(parents=True, exist_ok=True)
        job.begin("packs")
        self.assertEqual(job.read()["pid"], os.getpid())
        job.finish()
        self.assertEqual((job.read()["pid"], job.read()["done"]), (0, True))
        job.begin("again")
        job.fail("NETWORK", "offline")
        self.assertEqual(job.read()["pid"], 0)

    def test_ps_elapsed_time_is_read(self):
        self.assertEqual(core._elapsed_seconds("05:07"), 307)
        self.assertEqual(core._elapsed_seconds("2-03:04:05"), 2 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertIsNone(core._elapsed_seconds("bad"))

    def test_the_launchers_own_uninstall_stops_only_its_job(self):
        text = (REPO / "plugins" / "theodore" / "skills" / "pptx" / "scripts" / "theodore").read_text(encoding="utf-8")
        self.assertNotIn('kill "$(cat "$B/pid"', text)
        self.assertIn("""ps -ww -o command= -p "$bp" 2>/dev/null | grep -q -e ' __bootstrap' && kill "$bp\"""", text)


class BatchLauncherStopsNothingAboveIt(unittest.TestCase):
    """theodore.cmd's own uninstall (no Python yet) stops the programs running from Theodore's folder by their program
    file; the assistant's shell can be the Git Bash Theodore put there, so nothing above the command is stopped. Run
    with PowerShell's own engine where one is installed (pwsh), the system's process list replaced by a fixed one."""

    def command(self) -> str:
        text = (REPO / "plugins" / "theodore" / "skills" / "pptx" / "scripts" / "theodore.cmd").read_bytes().decode("ascii")
        line = next(x for x in text.split("\r\n") if "HOME_SCAN, [StringComparison]" in x)
        return line.split('-Command "', 1)[1].rsplit('" <nul', 1)[0]

    def test_the_line_leaves_out_every_process_above_it(self):
        self.assertIn("-and -not $keep.ContainsKey([int]$_.ProcessId)", self.command())

    @unittest.skipUnless(__import__("shutil").which("pwsh"), "PowerShell (pwsh) is not installed here")
    def test_powershell_stops_only_the_folders_programs_below_it(self):
        fake = ("function Get-CimInstance { param($c) @("
                "[pscustomobject]@{ProcessId=$PID; ParentProcessId=50; ExecutablePath='C:\\T\\x\\ps.exe'},"
                "[pscustomobject]@{ProcessId=50; ParentProcessId=40; ExecutablePath='C:\\T\\tools\\git\\bin\\bash.exe'},"
                "[pscustomobject]@{ProcessId=40; ParentProcessId=1; ExecutablePath='C:\\other\\claude.exe'},"
                "[pscustomobject]@{ProcessId=77; ParentProcessId=40; ExecutablePath='C:\\T\\runtime\\python.exe'},"
                "[pscustomobject]@{ProcessId=88; ParentProcessId=40; ExecutablePath='C:\\other\\x.exe'}) }; "
                "function Stop-Process { param($Id, [switch]$Force, $ErrorAction) Write-Output \"stop $Id\" }; ")
        done = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", fake + self.command()],
                              env={**os.environ, "HOME_SCAN": "C:\\T\\"}, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.stdout.split(), ["stop", "77"], done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
