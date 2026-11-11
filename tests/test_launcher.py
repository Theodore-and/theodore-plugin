"""The launcher's decision tree, branch by branch, with exact exit codes. Runs `theodore` (sh) on macOS and
Linux and `theodore.cmd` on Windows against the fake API; the home and plugin paths contain spaces on purpose."""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import cmd_command, RUNTIME_ID, IS_WINDOWS, FakeApi, Sandbox, engine_zip, fake_runtime_parts, pack_zip, status_lines


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi().start()
        self.addCleanup(self.api.stop)
        self.api.publish_runtime(RUNTIME_ID, fake_runtime_parts())
        self.api.publish_engine("1.0.0-rc.3", engine_zip("1.0.0-rc.3"))
        for pack in ("icons", "flags", "backgrounds", "maps"):
            self.api.publish_pack(pack, "1.0.0", pack_zip(pack, "1.0.0"))
        self.box = Sandbox(self.api)
        self.addCleanup(self.box.close)

    def run_launcher(self, *args, **kwargs):
        return self.box.run_launcher(*args, **kwargs)

    def assertStatus(self, result, name, code):
        first, nxt, tell = status_lines(result.stdout)
        self.assertEqual(result.returncode, code, f"{result.stdout}\n{result.stderr}")
        self.assertTrue(first.startswith(f"THEODORE {name}: "), first)
        self.assertEqual(len([l for l in result.stdout.splitlines() if l.strip()]), 3, f"exactly three lines:\n{result.stdout}")
        return first, nxt, tell

    def python_key(self) -> str:
        machine = os.uname().machine if hasattr(os, "uname") else "AMD64"
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        system = {"darwin": "darwin", "win32": "win32"}.get(sys.platform, "linux")
        return f"stage1_python_{system}_{arch}"


class FirstRun(LauncherCase):
    def test_first_run_bootstraps_copies_itself_and_runs_the_client(self):
        result = self.run_launcher("version")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("theodore client", result.stdout)
        stable = self.box.home / "bin"
        source = self.box.launcher.parent
        for name in ("theodore", "theodore.cmd", "launcher.json"):
            self.assertEqual((stable / name).read_bytes(), (source / name).read_bytes(), name)
        if not IS_WINDOWS:
            self.assertTrue(os.access(stable / "theodore", os.X_OK))
        self.assertEqual((self.box.home / "runtime" / "current.txt").read_text().strip(), RUNTIME_ID)
        import build_client
        self.assertEqual((self.box.home / "client" / "current.txt").read_text().strip(), build_client.client_version())
        self.box.wait_jobs()
        self.assertTrue((self.box.home / "runtime" / RUNTIME_ID / ".ready").exists(), "stage 2 ran in the background")
        self.assertEqual(sorted(p.name for p in (self.box.home / "staging").iterdir()), [])
        self.assertFalse((self.box.home / "state" / "bootstrap" / "pid").exists())

    def test_whole_first_use_through_the_launcher(self):
        self.api.issue_activation_code("ABCD-1234")
        self.assertStatus(self.run_launcher("login", "--code", "ABCD-1234"), "OK", 0)
        self.box.wait_jobs()
        guide = self.run_launcher("guide", "pptx")
        self.assertEqual(guide.returncode, 0, guide.stdout + guide.stderr)
        self.assertTrue(guide.stdout.startswith("=== THEODORE pptx 1.0.0-rc.3 (beta), part 1/1 ==="))
        self.assertIn(f'"{self.box.home / "bin" / ("theodore.cmd" if IS_WINDOWS else "theodore")}" pptx <verb>', guide.stdout)
        self.assertEqual(self.run_launcher("pptx", "exit", "7").returncode, 7, "engine exit codes pass through")
        self.assertEqual(self.run_launcher("pptx", "exit", "0").returncode, 0)
        other = self.box.plugin / "skills" / "deck-design" / "scripts" / self.box.launcher.name
        design = self.run_launcher("guide", "deck-design", launcher=other)
        self.assertEqual(design.returncode, 0, design.stdout + design.stderr)
        self.assertIn("=== END theodore deck-design SKILL.md", design.stdout)
        stable = self.box.home / "bin" / self.box.launcher.name
        self.assertEqual(self.run_launcher("pptx", "exit", "5", launcher=stable).returncode, 5, "the stable copy works on its own")
        self.assertStatus(self.run_launcher("nonsense"), "USAGE", 64)

    def test_slow_bootstrap_reports_progress_and_next_step(self):
        self.api.dl_delay = 4.0                                  # a slow connection: the download outlasts the wait
        result = self.run_launcher("guide", "pptx", env={"THEODORE_BOOTSTRAP_WAIT": "1"})
        first, nxt, tell = self.assertStatus(result, "SETUP_IN_PROGRESS", 67)
        self.assertRegex(first, r"the first part is at \d+% \(")
        self.assertEqual(nxt, "NEXT: run the same command again in 20 seconds. Do not use another slide tool meanwhile.")
        self.assertIn("first time", tell)
        again = self.run_launcher("guide", "pptx", env={"THEODORE_BOOTSTRAP_WAIT": "1"})      # the job is still running: no second one
        self.assertStatus(again, "SETUP_IN_PROGRESS", 67)
        self.assertEqual((self.box.home / "logs" / "bootstrap.log").read_text().count("\n"), 0, "one quiet job")
        import time
        downloads = lambda: len([r for r in self.api.requests if r["path"].startswith("/dl/runtime/")])
        deadline = time.monotonic() + 90                  # a loaded machine may start the job's download late
        while downloads() == 0 and time.monotonic() < deadline:
            time.sleep(0.5)
        time.sleep(2)
        self.assertEqual(downloads(), 1, "one download, not two")
        self.api.dl_delay = 0
        self.box.wait_jobs()
        self.assertEqual(self.run_launcher("version").returncode, 0)
        self.box.wait_jobs()

    @unittest.skipIf(IS_WINDOWS, "on Windows a running job is known by its open lock file; the slow-bootstrap test covers it")
    def test_a_running_job_is_not_started_twice_and_shows_percent(self):
        boot = self.box.home / "state" / "bootstrap"
        boot.mkdir(parents=True)
        (boot / "pid").write_text(str(os.getpid()))          # a live process stands in for the job
        (boot / "phase").write_text("download")
        staging = self.box.home / "staging"
        staging.mkdir()
        config = json.loads((self.box.launcher.parent / "launcher.json").read_text())
        size = config[self.python_key() + "_size"]
        (staging / "stage1-python.tar.gz").write_bytes(b"x" * (size // 4))
        result = self.run_launcher("guide", "pptx")
        first, _n, _t = self.assertStatus(result, "SETUP_IN_PROGRESS", 67)
        self.assertIn("the first part is at 2", first)        # a quarter: 24% or 25%
        self.assertFalse((self.box.home / "logs" / "bootstrap.log").exists(), "no second job was started")
        (boot / "pid").unlink()

    def test_engine_arguments_with_spaces_and_quotes_arrive_intact(self):
        self.api.issue_activation_code("ABCD-1234")
        self.run_launcher("login", "--code", "ABCD-1234")
        self.box.wait_jobs()
        self.run_launcher("guide", "pptx")
        out = self.run_launcher("pptx", "echo", "two words", "it's", 'say "hi"' if not IS_WINDOWS else "plain", "a&b")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout)["args"], ["two words", "it's", 'say "hi"' if not IS_WINDOWS else "plain", "a&b"])


@unittest.skipUnless(IS_WINDOWS, "Windows shells")
class WindowsShells(LauncherCase):
    def _run(self, command):
        import subprocess
        return subprocess.run(command, env=self.box.env, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL, cwd=str(self.box.tmp))

    def setUp(self):
        super().setUp()
        self.assertEqual(self.run_launcher("version").returncode, 0)            # bootstrap once through cmd
        self.box.wait_jobs(600)
        self.cmd_path = str(self.box.launcher)

    def test_powershell_call_operator_and_exit_code(self):
        for policy in ("Bypass", "AllSigned"):                                  # a .cmd is not subject to the execution policy
            done = self._run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", policy, "-Command",
                              f"& '{self.cmd_path}' frobnicate; exit $LASTEXITCODE"])
            self.assertEqual(done.returncode, 64, f"{policy}: {done.stdout}{done.stderr}")
            self.assertIn("THEODORE USAGE: ", done.stdout)

    def test_git_bash_through_the_sh_launcher(self):
        import shutil
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("Git Bash is not installed")
        sh_path = str(self.box.launcher.with_name("theodore")).replace("\\", "/")
        done = self._run([bash, "-c", f'sh "{sh_path}" frobnicate "two words"'])
        self.assertEqual(done.returncode, 64, done.stdout + done.stderr)
        self.assertIn("THEODORE USAGE: ", done.stdout)
        # Embedded quotes and shell metacharacters arrive intact: Git Bash starts the client itself once set up.
        tricky = 'say "hi" & echo pwned | x > y'
        done = self._run([bash, "-c", f"sh \"{sh_path}\" guide '{tricky}'"])
        self.assertEqual(done.returncode, 64, done.stdout + done.stderr)
        self.assertIn(f"unknown skill '{tricky}'", done.stdout)
        self.assertNotIn("pwned", done.stdout.replace(tricky, ""))
        self.assertFalse((self.box.tmp / "y").exists(), "no redirection happened")

    def test_without_powershell_the_job_still_inherits_no_output_handle(self):
        """The fallback starter (WMI) must not hand the job our output pipe: the call returns at once with a status
        while the download goes on, instead of waiting for the download to end."""
        import shutil
        import subprocess
        import time
        if not (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wbem" / "WMIC.exe").exists():
            self.skipTest("WMIC is not installed on this Windows")
        fresh = Sandbox(self.api, home_name="fresh home")
        self.addCleanup(fresh.close)
        import ctypes
        fresh.home.mkdir()
        def short(path):
            buffer = ctypes.create_unicode_buffer(1024)
            ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
            return buffer.value
        if " " in short(fresh.home) or " " in short(fresh.launcher):
            self.skipTest("this volume makes no 8.3 short names; the WMI starter then falls back to start /b")
        self.api.dl_delay = 30.0
        self.addCleanup(setattr, self.api, "dl_delay", 0.0)
        started = time.monotonic()
        result = fresh.run_launcher("version", env={"THEODORE_TEST_STARTER": "wmic", "THEODORE_BOOTSTRAP_WAIT": "2"}, timeout=120)
        self.assertLess(time.monotonic() - started, 25, "the caller did not wait for the download")
        self.assertEqual(result.returncode, 67, result.stdout + result.stderr)
        self.api.dl_delay = 0.0
        fresh.wait_jobs(120)

    def test_with_no_way_to_detach_setup_runs_in_the_foreground_and_says_so(self):
        fresh = Sandbox(self.api, home_name="foreground home")
        self.addCleanup(fresh.close)
        result = fresh.run_launcher("version", env={"THEODORE_TEST_STARTER": "none"}, timeout=900)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith("theodore: no background job can be started"), result.stdout)
        self.assertIn("theodore client", result.stdout)
        fresh.wait_jobs(300)

    def test_inbox_tools_are_present(self):
        import shutil
        for tool in ("curl.exe", "tar.exe", "certutil.exe", "findstr.exe"):
            self.assertIsNotNone(shutil.which(tool), tool)


class Failures(LauncherCase):
    def test_wrong_checksum_is_hash_mismatch_and_clears_for_a_retry(self):
        self.box.write_launcher_json(**{self.python_key() + "_sha256": "0" * 64})
        first, nxt, _t = self.assertStatus(self.run_launcher("guide", "pptx"), "SETUP_FAILED", 68)
        self.assertIn("cause HASH_MISMATCH", first)
        self.assertIn("support@theodoreand.ai", nxt)
        self.assertFalse((self.box.home / "runtime" / RUNTIME_ID / "python").exists())
        self.assertEqual(sorted(p.name for p in (self.box.home / "staging").iterdir()), [], "the bad download is deleted")
        self.box.write_launcher_json()
        for name in ("launcher.json",):                       # the stable copy holds the bad pin: same generation, so refresh it
            (self.box.home / "bin" / name).write_bytes((self.box.launcher.parent / name).read_bytes())
        self.assertEqual(self.run_launcher("version").returncode, 0, "the next run retries")
        self.box.wait_jobs()

    def test_client_checksum_is_checked_too(self):
        self.box.write_launcher_json(stage1_client_sha256="0" * 64)
        first, _n, _t = self.assertStatus(self.run_launcher("guide", "pptx"), "SETUP_FAILED", 68)
        self.assertIn("cause HASH_MISMATCH", first)
        self.assertFalse((self.box.home / "client" / "current.txt").exists())

    def test_unreachable_download_host_is_network(self):
        dead = f"http://127.0.0.1:{closed_port()}/python.tar.gz"
        self.box.write_launcher_json(**{self.python_key() + "_url": dead})
        first, nxt, tell = self.assertStatus(self.run_launcher("guide", "pptx"), "SETUP_FAILED", 68)
        self.assertIn("cause NETWORK", first)
        self.assertIn("internet connection", nxt)
        self.assertIn("internet connection", tell)

    def test_tls_failure_is_tls_intercept(self):
        url = self.api.base.replace("http://", "https://") + "/dl/python.tar.gz"     # TLS to a plain-HTTP port
        self.box.write_launcher_json(**{self.python_key() + "_url": url})
        first, nxt, _t = self.assertStatus(self.run_launcher("guide", "pptx"), "SETUP_FAILED", 68)
        self.assertIn("cause TLS_INTERCEPT", first)
        self.assertIn("root certificate", nxt)

    def test_a_redirect_is_not_followed(self):
        python = next(p for p in self.api.runtime["parts"] if p["part"] == "python")
        key = python["url"].split("/dl/", 1)[1]
        self.api.files["elsewhere/python.tar.gz"] = self.api.files[key]
        self.api.dl_redirect[key] = self.api.base.replace("127.0.0.1", "localhost") + "/dl/elsewhere/python.tar.gz"
        first, _n, _t = self.assertStatus(self.run_launcher("guide", "pptx"), "SETUP_FAILED", 68)
        self.assertIn("redirect", first)
        self.assertNotIn("/dl/elsewhere/python.tar.gz", [r["path"] for r in self.api.requests])
        self.assertFalse((self.box.home / "runtime" / RUNTIME_ID / "python").exists())

    def test_proxy_authentication_is_proxy_auth(self):
        python = next(p for p in self.api.runtime["parts"] if p["part"] == "python")
        key = python["url"].split("/dl/", 1)[1]
        self.api.dl_status[key] = 407
        result = self.run_launcher("guide", "pptx")
        import time
        deadline = time.monotonic() + 300
        while IS_WINDOWS and result.returncode == 67 and time.monotonic() < deadline:
            # Windows tries its own proxy credentials (PowerShell, then BITS, two minutes at most) before it gives up
            time.sleep(15)
            result = self.run_launcher("guide", "pptx")
        first, nxt, _t = self.assertStatus(result, "SETUP_FAILED", 68)
        self.assertIn("cause PROXY_AUTH", first)
        self.assertIn("proxy", nxt)

    @unittest.skipIf(IS_WINDOWS or os.geteuid() == 0, "needs POSIX permissions and a non-root user")
    def test_home_not_writable_is_disk(self):
        locked = self.box.tmp / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(lambda: locked.chmod(0o755))
        result = self.run_launcher("guide", "pptx", env={"THEODORE_HOME": str(locked / "theodore")})
        first, nxt, _t = self.assertStatus(result, "SETUP_FAILED", 68)
        self.assertIn("cause DISK", first)
        self.assertIn("THEODORE_HOME", nxt)

    @unittest.skipIf(IS_WINDOWS, "uname is replaced on PATH")
    def test_unsupported_platform(self):
        fake = self.box.tmp / "fakeuname"
        fake.mkdir()
        (fake / "uname").write_text('#!/bin/sh\ncase "$1" in -s) echo SunOS;; -m) echo sparc64;; *) echo SunOS;; esac\n')
        (fake / "uname").chmod(0o755)
        result = self.run_launcher("guide", "pptx", env={"PATH": f"{fake}{os.pathsep}{self.box.env['PATH']}"})
        first, nxt, _t = self.assertStatus(result, "UNSUPPORTED_PLATFORM", 69)
        self.assertIn("SunOS sparc64", first)
        self.assertIn("do not retry", nxt)
        self.assertFalse(any((self.box.home / d).exists() for d in ("runtime", "client", "staging", "logs")), "nothing is downloaded")
        state = self.box.home / "state"
        self.assertEqual(sorted(p.name for p in state.iterdir()) if state.exists() else [], ["environment"],
                         "only the folder's claim for this service is written")

    def test_sandbox_without_a_route_asks_for_the_connector(self):
        dead = f"http://127.0.0.1:{closed_port()}"
        self.box.write_launcher_json(stage1_client_url=f"{dead}/client.tar")
        result = self.run_launcher("guide", "deck-design", env={"THEODORE_EPHEMERAL": "1"})
        first, nxt, tell = self.assertStatus(result, "CONNECTOR_REQUIRED", 74)
        self.assertIn("connector", nxt)
        if IS_WINDOWS:      # theodore.cmd: no cloud sandbox runs Windows; its wording is unchanged
            self.assertIn("Customize > Plugins > Theodore > Connectors", tell)
        else:               # the setting the user changes in Claude, and where ChatGPT has a network
            self.assertIn("run ", nxt)
            self.assertIn('Settings > Capabilities, and under "Use the internet while making files" > Allowed websites add '
                          'theodoreand.ai and *.theodoreand.ai', tell)
            self.assertIn("ChatGPT Work mode", tell)
        self.assertFalse((self.box.home / "logs" / "bootstrap.log").exists(), "no bootstrap is attempted")

    def test_sandbox_with_a_route_bootstraps_normally(self):
        result = self.run_launcher("version", env={"THEODORE_EPHEMERAL": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.box.wait_jobs()

    def test_a_plugin_with_the_placeholder_key_refuses_to_start(self):
        for overrides in ({"manifest_key_1_kid": "manifest-unset"}, {"manifest_key_1_x": "A" * 43}):
            self.box.write_launcher_json(**overrides)
            first, nxt, _t = self.assertStatus(self.run_launcher("guide", "pptx"), "INTERNAL", 78)
            self.assertIn("no release signing key", first)
            self.assertIn("theodoreand.ai/install", nxt)
            self.assertFalse((self.box.home / "bin").exists(), "nothing is copied, downloaded or run")
            self.assertEqual([r for r in self.api.requests if r["path"].startswith("/dl/")], [])
        self.assertStatus(self.run_launcher("uninstall"), "OK", 0)

    def test_an_interpreter_the_system_refuses_to_start_is_blocked_by_policy(self):
        self.assertEqual(self.run_launcher("version").returncode, 0)
        self.box.wait_jobs()
        # A runtime whose interpreter the system will not start: current.txt points at it (the real one may still be
        # running a job, and a running program cannot be deleted on Windows).
        blocked = self.box.home / "runtime" / "blocked-rt" / "python"
        if IS_WINDOWS:
            blocked.mkdir(parents=True)
            (blocked / "python.exe").write_bytes(b"MZ not a program")    # Windows refuses to start it, as it does a blocked one
        else:
            (blocked / "bin" / "python3").mkdir(parents=True)              # exec fails with 126, as on a noexec mount
        (self.box.home / "runtime" / "current.txt").write_text("blocked-rt")
        first, nxt, tell = self.assertStatus(self.run_launcher("version"), "BLOCKED_BY_POLICY", 73)
        self.assertIn("could not be started", first)
        self.assertIn("connector", tell)

    def test_missing_launcher_json_is_internal(self):
        (self.box.launcher.parent / "launcher.json").unlink()
        self.assertStatus(self.run_launcher("guide", "pptx"), "INTERNAL", 78)

    def test_uninstall_before_anything_is_installed(self):
        (self.box.home / "state").mkdir(parents=True)
        result = self.run_launcher("uninstall")
        self.assertStatus(result, "OK", 0)
        self.assertFalse(self.box.home.exists())

    @unittest.skipIf(IS_WINDOWS, "the sh launcher's own uninstall")
    def test_uninstall_without_python_removes_registrations_keychain_and_cache_and_names_what_is_left(self):
        import hashlib
        import subprocess
        state = self.box.home / "state"
        state.mkdir(parents=True)
        settings = self.box.user_home / ".claude" / "settings.json"
        (state / "host-settings.json").write_text(json.dumps({"rules": ["Bash(x *)"], "path": str(settings)}, indent=2))
        cache = (self.box.user_home / "Library" / "Caches" / "pptx-runtime") if sys.platform == "darwin" else (self.box.user_home / ".cache" / "pptx-runtime")
        cache.mkdir(parents=True)
        current = (self.box.user_home / "Library" / "Caches" / "Theodore") if sys.platform == "darwin" else (self.box.user_home / ".cache" / "theodore")
        (current / "workspaces").mkdir(parents=True)          # the engine's render cache since 1.0.0-rc.4
        fake = self.box.tmp / "fakebin"
        fake.mkdir()
        log = self.box.tmp / "calls.log"
        for name in ("claude", "codex"):
            (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
            (fake / name).chmod(0o755)
        env = {"PATH": f"{fake}{os.pathsep}{self.box.env['PATH']}"}
        keychain = None
        if sys.platform == "darwin":
            keychain = self.box.tmp / "test.keychain-db"
            subprocess.run(["/usr/bin/security", "create-keychain", "-p", "", str(keychain)], check=True, capture_output=True)
            self.addCleanup(lambda: subprocess.run(["/usr/bin/security", "delete-keychain", str(keychain)], capture_output=True))
            account = "installation-" + hashlib.sha256(str(self.box.home).encode()).hexdigest()[:12]
            subprocess.run(["/usr/bin/security", "add-generic-password", "-a", account, "-s", "ai.theodoreand.theodore", "-w", "x", str(keychain)],
                           check=True, capture_output=True)
            env["THEODORE_TEST_KEYCHAIN"] = str(keychain)
        first, nxt, _t = self.assertStatus(self.run_launcher("uninstall", env=env), "RETRY", 75)
        self.assertIn(str(settings), first, "the settings entries it cannot take back without Python are named")
        self.assertNotIn("keychain", first)
        calls = log.read_text().splitlines()
        for expected in ("claude plugin uninstall theodore@theodore", "claude plugin marketplace remove theodore",
                         "codex plugin remove theodore@theodore", "codex plugin marketplace remove theodore"):
            self.assertIn(expected, calls)
        self.assertFalse(cache.exists())
        self.assertFalse(current.exists())
        self.assertEqual([p.relative_to(self.box.home).as_posix() for p in self.box.home.rglob("*")], ["state", "state/host-settings.json"],
                         "only the record of the settings it could not take back stays")
        if keychain:
            found = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "ai.theodoreand.theodore", str(keychain)], capture_output=True)
            self.assertNotEqual(found.returncode, 0, "the keychain item is gone")

    @unittest.skipIf(IS_WINDOWS, "the sh launcher's own uninstall; Windows keeps the credential in a DPAPI file in state")
    def test_uninstall_without_python_gives_the_keychain_ten_seconds(self):
        (self.box.home / "state").mkdir(parents=True)
        hang = self.box.tmp / "hanging-security"
        started = self.box.tmp / "security-started"
        hang.write_text(f'#!/bin/sh\necho "$*" > "{started}"\nexec sleep 120\n')
        hang.chmod(0o755)
        import time
        began = time.monotonic()
        result = self.run_launcher("uninstall", env={"THEODORE_TEST_SECURITY": str(hang)}, timeout=60)
        took = time.monotonic() - began
        self.assertTrue(started.exists(), "the keychain was asked")
        self.assertIn("delete-generic-password", started.read_text())
        first, _n, _t = self.assertStatus(result, "RETRY", 75)
        self.assertIn("keychain", first, "a keychain that did not answer is named as left")
        self.assertLess(took, 30, f"the uninstall waited {took:.0f} s for the keychain")
        self.assertFalse(self.box.home.exists(), "everything else is still removed")

    def test_uninstall_without_python_removes_recorded_specimen_folders(self):
        state = self.box.home / "state"
        state.mkdir(parents=True)
        elsewhere = self.box.tmp / "an old TMPDIR"             # recorded while TMPDIR pointed here
        recorded = elsewhere / "theodore-specimens-0123456789abcdef"
        current = Path(self.box.env["TMPDIR"]) / "theodore-specimens-fedcba9876543210"      # not recorded, in today's root
        for folder in (recorded, current):
            folder.mkdir(parents=True)
            (folder / "cover-01.jpg").write_bytes(b"jpeg")
        precious = self.box.tmp / "Documents"
        precious.mkdir()
        (precious / "keep.txt").write_text("keep")
        lines = [f"1700000000 {recorded}", f"1700000000 {precious}"]
        link = elsewhere / "theodore-specimens-link"
        if not IS_WINDOWS:
            os.symlink(precious, link)
            lines.append(f"1700000000 {link}")
        (state / "specimens.txt").write_text("\n".join(lines) + "\n")
        temp = self.box.env["TMPDIR"]            # batch reads %TEMP%, sh reads $TMPDIR: both name today's root
        self.assertStatus(self.run_launcher("uninstall", env={"TEMP": temp, "TMP": temp}), "OK", 0)
        self.assertFalse(recorded.exists(), "a recorded folder goes, wherever it is")
        self.assertFalse(current.exists(), "a folder in the current temporary folder goes")
        self.assertTrue((precious / "keep.txt").exists(), "a recorded path that is not a specimen folder is never deleted")
        if not IS_WINDOWS:
            self.assertTrue(os.path.lexists(link) and (precious / "keep.txt").exists(), "a link is not followed")
        self.assertFalse(self.box.home.exists())

    def test_uninstall_without_python_keeps_marketplaces_that_were_there_before(self):
        state = self.box.home / "state"
        state.mkdir(parents=True)
        (state / "host-settings.json").write_text(json.dumps({"claude_added_marketplace": False, "claude_registered": True,
                                                              "codex_added_marketplace": False, "codex_registered": True}, indent=2) + "\n")
        fake = self.box.tmp / "fakebin"
        fake.mkdir()
        log = self.box.tmp / "calls.log"
        for name in ("claude", "codex"):
            if IS_WINDOWS:
                (fake / f"{name}.cmd").write_text(f'@echo off\r\n>>"{log}" echo {name} %*\r\nexit /b 0\r\n')
            else:
                (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
                (fake / name).chmod(0o755)
        self.assertStatus(self.run_launcher("uninstall", env={"PATH": f"{fake}{os.pathsep}{self.box.env['PATH']}"}), "OK", 0)
        calls = [line.strip() for line in log.read_text().splitlines()]
        self.assertIn("claude plugin uninstall theodore@theodore", calls)
        self.assertIn("codex plugin remove theodore@theodore", calls)
        self.assertFalse([c for c in calls if "marketplace remove" in c], calls)

    @unittest.skipIf(IS_WINDOWS, "the sh launcher's own uninstall")
    def test_a_keychain_call_that_ignores_sigterm_is_killed(self):
        # Started with SIGTERM ignored, the stand-in for security inherits that; it is killed after
        # the bounded wait instead of holding the uninstall.
        (self.box.home / "state").mkdir(parents=True)
        stubborn = self.box.tmp / "stubborn-security"
        stubborn.write_text("#!/bin/sh\ntrap '' TERM\nsleep 120\n")
        stubborn.chmod(0o755)
        import subprocess
        import time
        began = time.monotonic()
        command = ["/bin/sh", "-c", 'trap "" TERM; exec /bin/sh "$0" uninstall', str(self.box.launcher)]
        result = subprocess.run(command, env={**self.box.env, "THEODORE_TEST_SECURITY": str(stubborn)}, capture_output=True, text=True,
                                timeout=90, stdin=subprocess.DEVNULL, cwd=str(self.box.tmp))
        took = time.monotonic() - began
        self.assertLess(took, 45, f"the uninstall waited {took:.0f} s")
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("keychain", status_lines(result.stdout)[0])

    def test_uninstall_without_python_keeps_the_settings_record_and_says_how_to_restore(self):
        # Without Theodore's Python the settings cannot be taken back exactly; the record of what
        # was there stays, and the status says where it is and what to do. A registration that was there before stays.
        state = self.box.home / "state"
        state.mkdir(parents=True)
        record = state / "host-settings.json"
        record.write_text(json.dumps({"claude_registered": False, "claude_added_marketplace": False, "rules": ["Bash(x *)"],
                                      "marketplace": {"created": False, "set_auto_update": True, "auto_update_prior": {"present": True, "value": False}},
                                      "path": str(self.box.user_home / ".claude" / "settings.json")}, indent=2) + "\n")
        (state / "credentials").write_text("{}")
        fake = self.box.tmp / "fakebin"
        fake.mkdir()
        log = self.box.tmp / "calls.log"
        for name in ("claude", "codex"):
            if IS_WINDOWS:
                (fake / f"{name}.cmd").write_text(f'@echo off\r\n>>"{log}" echo {name} %*\r\nexit /b 0\r\n')
            else:
                (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
                (fake / name).chmod(0o755)
        first, nxt, _t = self.assertStatus(self.run_launcher("uninstall", env={"PATH": f"{fake}{os.pathsep}{self.box.env['PATH']}"}), "RETRY", 75)
        self.assertIn(str(record), first)
        self.assertIn("install Theodore again and run its uninstall", first)
        self.assertTrue(record.exists(), "the record of what was there before is kept")
        self.assertEqual(sorted(p.name for p in state.iterdir()), ["host-settings.json"], "everything else in state goes")
        calls = [line.strip() for line in log.read_text().splitlines()] if log.exists() else []
        self.assertFalse([c for c in calls if c.startswith("claude plugin")], calls)

    def test_an_ordinary_run_sweeps_expired_specimen_folders_even_when_the_bootstrap_fails(self):
        # Before the bootstrap, so also when it cannot start (here: the download host is dead).
        import time
        state = self.box.home / "state"
        state.mkdir(parents=True)
        old_root = self.box.tmp / "an old TMPDIR"
        temp = Path(self.box.env["TMPDIR"])
        expired, fresh = old_root / "theodore-specimens-0000000000000001", old_root / "theodore-specimens-0000000000000002"
        untouched, recent = temp / "theodore-specimens-0000000000000003", temp / "theodore-specimens-0000000000000004"
        for folder in (expired, fresh, untouched, recent):
            folder.mkdir(parents=True)
            (folder / "cover-01.jpg").write_bytes(b"jpeg")
        hour_ago = time.time() - 3700
        os.utime(untouched, (hour_ago, hour_ago))
        now = int(time.time())
        (state / "specimens.txt").write_text(f"#v2\n{now - 3700} {expired}\n{now - 60} {fresh}\n")
        dead = f"http://127.0.0.1:{closed_port()}/python.tar.gz"
        self.box.write_launcher_json(**{self.python_key() + "_url": dead})
        result = self.run_launcher("guide", "pptx", env={"TEMP": str(temp), "TMP": str(temp)})
        self.assertIn(result.returncode, (67, 68), result.stdout)
        self.assertFalse(expired.exists(), "recorded more than an hour ago: removed")
        self.assertFalse(untouched.exists(), "untouched for an hour in today's temporary folder: removed")
        self.assertTrue(fresh.exists() and recent.exists(), "recent ones stay")

    @unittest.skipIf(IS_WINDOWS, "the sh launcher: a file system without hard links")
    def test_without_hard_links_two_services_racing_still_end_with_one(self):
        fake = self.box.tmp / "no-links"
        fake.mkdir()
        (fake / "ln").write_text("#!/bin/sh\nexit 1\n")
        (fake / "ln").chmod(0o755)
        self.box.env["PATH"] = f"{fake}{os.pathsep}{self.box.env['PATH']}"
        self.test_of_launchers_of_two_services_racing_on_an_empty_folder_one_wins()

    def test_of_launchers_of_two_services_racing_on_an_empty_folder_one_wins(self):
        # The claim is one exclusive step; the losers read the winner's record and refuse.
        import shutil
        import subprocess
        dead = f"http://127.0.0.1:{closed_port()}"
        self.box.write_launcher_json(stage1_client_url=f"{dead}/client.tar")
        other = self.box.tmp / "staging build" / "skills" / "pptx" / "scripts"
        shutil.copytree(self.box.launcher.parent, other)
        config = (other / "launcher.json").read_text().replace(self.api.base, "https://api.staging.example.test")
        (other / "launcher.json").write_text(config.replace("https://api.staging.example.test/dl/client", f"{dead}/client"))
        launchers = [self.box.launcher if i % 2 == 0 else other / self.box.launcher.name for i in range(8)]
        env = {**self.box.env, "THEODORE_EPHEMERAL": "1"}
        procs = []
        for launcher in launchers:
            command = cmd_command(str(launcher), "version") if IS_WINDOWS else ["/bin/sh", str(launcher), "version"]
            procs.append((launcher, subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                                     stdin=subprocess.DEVNULL, cwd=str(self.box.tmp))))
        results = [(launcher, proc.communicate(timeout=120)[0], proc.returncode) for launcher, proc in procs]
        winner = (self.box.home / "state" / "environment").read_text().strip()
        self.assertIn(winner, (self.api.base, "https://api.staging.example.test"))
        for launcher, out, code in results:
            ours = (launcher == self.box.launcher) == (winner == self.api.base)
            self.assertEqual(code, 74 if ours else 64, f"{launcher}: {out}")

    def odd_roots(self) -> list[Path]:
        roots = [self.box.tmp / "100%proof"]
        if not IS_WINDOWS:
            roots.append(self.box.tmp / "tmp\nwith a line feed %0A")
        return roots

    def record_odd_folders(self, age: int) -> list[Path]:
        import time
        from theodore_client import specimens as client_specimens
        folders = []
        for index, root in enumerate(self.odd_roots()):
            folder = root / f"theodore-specimens-00000000000000a{index}"
            folder.mkdir(parents=True)
            (folder / "cover-01.jpg").write_bytes(b"jpeg")
            folders.append(folder)
        state = self.box.home / "state"
        state.mkdir(parents=True, exist_ok=True)
        made = int(time.time()) - age
        (state / "specimens.txt").write_text("#v2\n" + "".join(f"{made} {client_specimens._encode(f)}\n" for f in folders), newline="\n")
        return folders

    def test_bare_runs_read_the_encoded_record(self):
        # A root with "%" and one with a line feed, TMPDIR changed since; the launcher's
        # own sweep (bootstrap failing) and its own uninstall find them through the record.
        temp = Path(self.box.env["TMPDIR"])
        env = {"TEMP": str(temp), "TMP": str(temp)}
        expired = self.record_odd_folders(age=3700)
        dead = f"http://127.0.0.1:{closed_port()}/python.tar.gz"
        self.box.write_launcher_json(**{self.python_key() + "_url": dead})
        result = self.run_launcher("guide", "pptx", env=env)
        self.assertIn(result.returncode, (67, 68), result.stdout + result.stderr)
        for folder in expired:
            self.assertFalse(folder.exists(), f"expired and recorded under an encoded path: removed ({folder!r})")
        fresh = self.record_odd_folders(age=60)
        self.assertStatus(self.run_launcher("uninstall", env=env), "OK", 0)
        for folder in fresh:
            self.assertFalse(folder.exists(), f"uninstall removes it through the record ({folder!r})")

    @unittest.skipIf(IS_WINDOWS or (hasattr(os, "geteuid") and os.geteuid() == 0), "a folder that cannot be removed (POSIX permissions)")
    def test_an_ordinary_bare_run_survives_a_removal_that_fails(self):
        # Under set -u a failed removal in the sweep of an ordinary run must not end the launcher.
        import time
        state = self.box.home / "state"
        state.mkdir(parents=True)
        locked = self.box.tmp / "locked root"
        folder = locked / "theodore-specimens-0000000000000001"
        folder.mkdir(parents=True)
        (folder / "f").write_text("x")
        (state / "specimens.txt").write_text(f"#v2\n{int(time.time()) - 3700} {folder}\n")
        locked.chmod(0o555)
        self.addCleanup(lambda: locked.chmod(0o755))
        dead = f"http://127.0.0.1:{closed_port()}/python.tar.gz"
        self.box.write_launcher_json(**{self.python_key() + "_url": dead})
        result = self.run_launcher("guide", "pptx")
        self.assertNotIn("unbound", result.stderr)
        self.assertIn(result.returncode, (67, 68), result.stdout + result.stderr)
        status_lines(result.stdout)
        self.assertTrue(folder.exists())

    @unittest.skipIf(IS_WINDOWS, "Windows keeps the credential in a DPAPI file in state, removed with the folder")
    def test_uninstall_of_another_services_folder_deletes_that_services_keychain_item(self):
        # Service A's folder, its runtime gone, uninstalled by service B's launcher.
        import hashlib
        state = self.box.home / "state"
        state.mkdir(parents=True)
        other = "https://api.other.example.test"
        (state / "environment").write_text(other + "\n")
        log = self.box.tmp / "security.log"
        fake = self.box.tmp / "fake-security"
        fake.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
        fake.chmod(0o755)
        self.assertStatus(self.run_launcher("uninstall", env={"THEODORE_TEST_SECURITY": str(fake)}), "OK", 0)
        expected = "installation-" + hashlib.sha256(f"{self.box.home}\n{other}".encode()).hexdigest()[:12]
        self.assertIn(f"-a {expected} ", log.read_text(), "the item of the service the folder belongs to is deleted")

    def test_uninstall_without_python_reports_what_it_could_not_remove(self):
        fake = self.box.tmp / "failing tools"
        fake.mkdir()
        if IS_WINDOWS:
            (fake / "claude.cmd").write_text("@echo off\r\necho plugin database is locked\r\nexit /b 1\r\n")
        else:
            (fake / "claude").write_text('#!/bin/sh\necho "plugin database is locked"\nexit 1\n')
            (fake / "claude").chmod(0o755)
        (self.box.home / "plugin.partial" / "plugins").mkdir(parents=True)           # an interrupted plugin unpack
        env = {"PATH": f"{fake}{os.pathsep}{self.box.env['PATH']}"}
        first, _n, tell = self.assertStatus(self.run_launcher("uninstall", env=env), "RETRY", 75)
        self.assertIn("claude-code registration", first)
        self.assertNotIn("Theodore is uninstalled", tell)
        self.assertFalse((self.box.home / "plugin.partial").exists(), "an interrupted unpack is Theodore's and goes too")
        if not IS_WINDOWS and os.geteuid() != 0:
            cache = (self.box.user_home / "Library" / "Caches" / "pptx-runtime") if sys.platform == "darwin" else (self.box.user_home / ".cache" / "pptx-runtime")
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "f").write_text("x")
            cache.parent.chmod(0o555)                          # the cache's own folder cannot be removed from its parent
            self.addCleanup(lambda: cache.parent.chmod(0o755))
            first, _n, _t = self.assertStatus(self.run_launcher("uninstall"), "RETRY", 75)
            self.assertIn("render cache", first, "a render cache it could not delete is named")
            cache.parent.chmod(0o755)
            stuck = self.box.home / "state" / "stuck"
            stuck.mkdir(parents=True)
            (stuck / "file").write_text("x")
            stuck.chmod(0o555)
            self.addCleanup(lambda: stuck.chmod(0o755))
            first, _n, _t = self.assertStatus(self.run_launcher("uninstall"), "RETRY", 75)
            self.assertIn(str(self.box.home / "state"), first, "a folder it could not delete is named")
            stuck.chmod(0o755)

    def test_a_folder_that_already_holds_other_things_is_shared_politely(self):
        foreign = self.box.home / "memory" / "notes.txt"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("not Theodore's")
        self.assertEqual(self.run_launcher("version").returncode, 0)            # bootstraps beside the foreign folder
        self.box.wait_jobs()
        owned = {"bin", "client", "runtime", "engines", "packs", "tools", "state", "logs", "staging", "plugin"}
        self.assertLessEqual({p.name for p in self.box.home.iterdir()} - {"memory"}, owned, "only the named subfolders are created")
        (self.box.home / "client" / "current.txt").unlink()                    # make it "not installed" again: the launcher's own uninstall
        self.assertStatus(self.run_launcher("uninstall"), "OK", 0)
        self.assertEqual([p.name for p in self.box.home.iterdir()], ["memory"])
        self.assertEqual(foreign.read_text(), "not Theodore's")


class CorporateNetwork(unittest.TestCase):
    """A network that inspects TLS, and a proxy that wants a sign-in. The trusted-certificate and proxy cases need a
    certificate in the system store and a running proxy, so they run in CI (shim.yml, job network)."""

    def _world(self, tls):
        api = FakeApi(tls=tls).start()
        self.addCleanup(api.stop)
        api.publish_runtime(RUNTIME_ID, fake_runtime_parts())
        api.publish_engine("1.0.0-rc.3", engine_zip("1.0.0-rc.3"))
        box = Sandbox(api)
        self.addCleanup(box.close)
        return api, box

    @unittest.skipIf(IS_WINDOWS, "POSIX launcher")
    def test_untrusted_certificate_is_tls_intercept_in_the_launcher_and_in_the_client(self):
        import tempfile
        from support import make_certificate
        folder = Path(tempfile.mkdtemp(prefix="theodore-cert-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        api, box = self._world(make_certificate(folder))
        result = box.run_launcher("guide", "pptx")
        first, nxt, _tell = status_lines(result.stdout)
        self.assertEqual(result.returncode, 68, result.stdout + result.stderr)
        self.assertIn("cause TLS_INTERCEPT", first)
        self.assertIn("root certificate", nxt)
        box.install_client_directly()
        api.issue_activation_code("ABCD-1234")
        client = box.run_client("login", "--code", "ABCD-1234")
        first, nxt, _tell = status_lines(client.stdout)
        self.assertEqual(client.returncode, 71, client.stdout + client.stderr)
        self.assertIn("cause TLS_INTERCEPT", first)
        self.assertIn("root certificate", nxt)
        self.assertEqual([r for r in api.requests if r["path"].startswith("/v1/")], [], "nothing was sent over the untrusted connection")

    @unittest.skipUnless(os.environ.get("THEODORE_TEST_TRUSTED_CERT"), "needs a test certificate installed in the system store (CI)")
    def test_certificate_in_the_system_store_is_trusted_end_to_end(self):
        cert, key = os.environ["THEODORE_TEST_TRUSTED_CERT"].split(os.pathsep)
        api, box = self._world((cert, key))
        api.issue_activation_code("ABCD-1234")
        login = box.run_launcher("login", "--code", "ABCD-1234")
        self.assertEqual(login.returncode, 0, login.stdout + login.stderr)        # curl (stage 1) and the client both trust it
        box.wait_jobs(600)
        guide = box.run_launcher("guide", "deck-design")
        self.assertEqual(guide.returncode, 0, guide.stdout + guide.stderr)

    @unittest.skipUnless(os.environ.get("THEODORE_TEST_AUTH_PROXY"), "needs an authenticating proxy (CI)")
    def test_proxy_that_wants_a_sign_in_then_with_credentials(self):
        proxy = os.environ["THEODORE_TEST_AUTH_PROXY"]                           # for example http://127.0.0.1:8888
        creds = os.environ["THEODORE_TEST_AUTH_PROXY_CREDENTIALS"]               # user:password
        api, box = self._world(None)
        base = {"no_proxy": "", "NO_PROXY": ""}
        refused = box.run_launcher("version", env={**base, "http_proxy": proxy, "HTTP_PROXY": proxy})
        self.assertEqual(refused.returncode, 68, refused.stdout + refused.stderr)
        self.assertIn("cause PROXY_AUTH", status_lines(refused.stdout)[0])
        with_creds = proxy.replace("://", f"://{creds}@")
        allowed = box.run_launcher("version", env={**base, "http_proxy": with_creds, "HTTP_PROXY": with_creds})
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        box.wait_jobs(600)
        self.assertTrue((box.home / "runtime" / RUNTIME_ID / ".ready").exists(), "the client downloaded the rest through the proxy")


class Generations(LauncherCase):
    def _stable(self, generation: int, marker: str) -> None:
        stable = self.box.home / "bin"
        stable.mkdir(parents=True)
        if IS_WINDOWS:
            (stable / "theodore.cmd").write_text(f"@echo off\r\necho {marker} %*\r\nexit /b 0\r\n")
        else:
            (stable / "theodore").write_text(f'#!/bin/sh\necho "{marker} $*"\n')
            (stable / "theodore").chmod(0o755)
        (stable / "launcher.json").write_text(json.dumps({"launcher_generation": generation}, indent=2) + "\n")

    def test_a_newer_stable_copy_takes_over(self):
        self._stable(5, "STABLE-COPY-RAN")
        result = self.run_launcher("guide", "pptx")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "STABLE-COPY-RAN guide pptx")

    def test_an_older_stable_copy_is_replaced(self):
        self._stable(0, "OLD-COPY-RAN")
        result = self.run_launcher("version")
        self.assertNotIn("OLD-COPY-RAN", result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.box.home / "bin" / self.box.launcher.name).read_bytes(), self.box.launcher.read_bytes())
        self.assertEqual(json.loads((self.box.home / "bin" / "launcher.json").read_text())["launcher_generation"], 1)
        self.box.wait_jobs()

    def test_same_generation_leaves_the_stable_copy_alone(self):
        self.assertEqual(self.run_launcher("version").returncode, 0)
        self.box.wait_jobs()
        stamp = self.box.home / "bin" / "launcher.json"
        before = stamp.stat().st_mtime_ns
        self.assertEqual(self.run_launcher("version").returncode, 0)
        self.assertEqual(stamp.stat().st_mtime_ns, before)


class Environments(LauncherCase):
    """A Theodore folder records the service it belongs to; a launcher stamped for another refuses it."""

    STAGING = "https://api.staging.example.test"

    def test_a_folder_of_another_service_is_not_reused_and_the_way_out_is_named(self):
        self.assertEqual(self.run_launcher("version").returncode, 0)
        self.box.wait_jobs()
        record = self.box.home / "state" / "environment"
        self.assertEqual(record.read_text().strip(), self.api.base, "the folder records its service")
        stable = self.box.home / "bin" / "launcher.json"
        before = stable.read_bytes()
        for generation in (1, 2):           # the same generation, and a newer one that would otherwise take the stable copy's place
            with self.subTest(generation=generation):
                self.box.write_launcher_json(generation=generation, api_base=self.STAGING, site_base="https://staging.example.test")
                first, nxt, tell = self.assertStatus(self.run_launcher("guide", "pptx"), "USAGE", 64)
                for text in (self.api.base, self.STAGING, str(self.box.home)):
                    self.assertIn(text, first)
                self.assertIn("THEODORE_HOME", nxt)
                self.assertIn("uninstall", nxt)
                self.assertIn(str(self.box.home / "bin" / ("theodore.cmd" if IS_WINDOWS else "theodore")), nxt)
                self.assertEqual(stable.read_bytes(), before, "the other service's stable copy is left alone")
                self.assertEqual(record.read_text().strip(), self.api.base)
        legacy = self.box.home / "state" / "environment"
        legacy.unlink()                     # a folder from before the record: its stable launcher.json says
        self.assertStatus(self.run_launcher("version"), "USAGE", 64)
        self.box.write_launcher_json()      # this service's build again: it works and records the folder's service
        self.assertEqual(self.run_launcher("version").returncode, 0)
        self.assertEqual(legacy.read_text().strip(), self.api.base)

    def test_uninstall_is_the_way_out_and_a_separate_folder_works(self):
        self.assertEqual(self.run_launcher("version").returncode, 0)
        self.box.wait_jobs()
        self.box.write_launcher_json(api_base=self.STAGING)
        other = self.box.tmp / "staging home"
        result = self.run_launcher("version", env={"THEODORE_HOME": str(other)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.box.wait_jobs()
        self.assertEqual((other / "state" / "environment").read_text().strip(), self.STAGING)
        removed = self.run_launcher("uninstall")
        self.assertIn(removed.returncode, (0, 75), removed.stdout + removed.stderr)     # Windows schedules the removal
        self.assertNotIn("THEODORE USAGE", removed.stdout)


if __name__ == "__main__":
    unittest.main()
