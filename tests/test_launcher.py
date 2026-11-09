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

from support import IS_WINDOWS, FakeApi, Sandbox, engine_zip, fake_runtime_parts, pack_zip, status_lines


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi().start()
        self.addCleanup(self.api.stop)
        self.api.publish_runtime("rt-test-1", fake_runtime_parts())
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
        self.assertEqual((self.box.home / "runtime" / "current.txt").read_text().strip(), "rt-test-1")
        self.assertEqual((self.box.home / "client" / "current.txt").read_text().strip(), "0.1.0")
        self.box.wait_jobs()
        self.assertTrue((self.box.home / "runtime" / "rt-test-1" / ".ready").exists(), "stage 2 ran in the background")
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
        result = self.run_launcher("guide", "pptx", env={"THEODORE_BOOTSTRAP_WAIT": "0"})
        first, nxt, tell = self.assertStatus(result, "SETUP_IN_PROGRESS", 67)
        self.assertRegex(first, r"the first part is at \d+% \(")
        self.assertEqual(nxt, "NEXT: run the same command again in 20 seconds. Do not use another slide tool meanwhile.")
        self.assertIn("first time", tell)
        self.box.wait_jobs()

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


class Failures(LauncherCase):
    def test_wrong_checksum_is_hash_mismatch_and_clears_for_a_retry(self):
        self.box.write_launcher_json(**{self.python_key() + "_sha256": "0" * 64})
        first, nxt, _t = self.assertStatus(self.run_launcher("guide", "pptx"), "SETUP_FAILED", 68)
        self.assertIn("cause HASH_MISMATCH", first)
        self.assertIn("support@theodoreand.ai", nxt)
        self.assertFalse((self.box.home / "runtime" / "rt-test-1" / "python").exists())
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

    def test_proxy_authentication_is_proxy_auth(self):
        key = next(k for k in self.api.files if k.endswith("python.tar.gz"))
        self.api.dl_status[key] = 407
        first, nxt, _t = self.assertStatus(self.run_launcher("guide", "pptx"), "SETUP_FAILED", 68)
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
        self.assertFalse((self.box.home / "state").exists(), "nothing is downloaded or written")

    def test_sandbox_without_a_route_asks_for_the_connector(self):
        dead = f"http://127.0.0.1:{closed_port()}"
        self.box.write_launcher_json(stage1_client_url=f"{dead}/client.tar")
        result = self.run_launcher("guide", "deck-design", env={"THEODORE_EPHEMERAL": "1"})
        first, nxt, tell = self.assertStatus(result, "CONNECTOR_REQUIRED", 74)
        self.assertIn("connector", nxt)
        self.assertIn("Customize > Plugins > Theodore > Connectors", tell)
        self.assertFalse((self.box.home / "logs" / "bootstrap.log").exists(), "no bootstrap is attempted")

    def test_sandbox_with_a_route_bootstraps_normally(self):
        result = self.run_launcher("version", env={"THEODORE_EPHEMERAL": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.box.wait_jobs()

    def test_missing_launcher_json_is_internal(self):
        (self.box.launcher.parent / "launcher.json").unlink()
        self.assertStatus(self.run_launcher("guide", "pptx"), "INTERNAL", 78)

    def test_uninstall_before_anything_is_installed(self):
        (self.box.home / "state").mkdir(parents=True)
        result = self.run_launcher("uninstall")
        self.assertStatus(result, "OK", 0)
        self.assertFalse(self.box.home.exists())


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


if __name__ == "__main__":
    unittest.main()
