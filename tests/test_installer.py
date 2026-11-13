"""install.sh end to end against the fake API, in a clean home, with stand-ins for `claude` and `codex`."""

from __future__ import annotations

import json
import os
import sys
import socket
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import RUNTIME_ID, IS_WINDOWS, ROOT, FakeApi, Sandbox, cmd_command, engine_zip, fake_runtime_parts, pack_zip, status_lines

STABLE = "theodore.cmd" if IS_WINDOWS else "theodore"


class Installer(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi().start()
        self.addCleanup(self.api.stop)
        self.api.publish_runtime(RUNTIME_ID, fake_runtime_parts())
        self.api.publish_engine("1.0.0-rc.3", engine_zip("1.0.0-rc.3"))
        for pack in ("icons", "flags", "backgrounds", "maps"):
            self.api.publish_pack(pack, "1.0.0", pack_zip(pack, "1.0.0"))
        self.box = Sandbox(self.api)
        self.addCleanup(self.box.close)
        self.box.serve_plugin_archive()
        fake = self.box.tmp / "fakebin"
        fake.mkdir()
        self.calls = self.box.tmp / "host-calls.log"
        for name in ("claude", "codex"):
            if IS_WINDOWS:
                (fake / f"{name}.cmd").write_text(f'@echo off\r\n>>"{self.calls}" echo {name} %*\r\nif "%*"=="plugin list --json" echo []\r\nexit /b 0\r\n')
            else:
                (fake / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\ncase "$*" in "plugin list --json") echo "[]";; esac\nexit 0\n')
                (fake / name).chmod(0o755)
        self.box.env["PATH"] = f"{fake}{os.pathsep}{self.box.env['PATH']}"
        self.box.env["THEODORE_DL_BASE"] = f"{self.api.base}/dl"

    def host_calls(self) -> list[str]:
        # The fake .cmd logs %*, which keeps the quotes cmd needed around a path with a space; compare without them.
        lines = self.calls.read_text().splitlines()
        return [line.replace('"', "") for line in lines] if IS_WINDOWS else lines

    def wait_gone(self, path: Path, seconds: float = 150.0) -> None:
        import time
        deadline = time.monotonic() + seconds
        while path.exists() and time.monotonic() < deadline:
            time.sleep(1)
        self.assertFalse(path.exists(), f"{path} is still there after {seconds:.0f} s: {sorted(p.name for p in path.iterdir())}")

    @unittest.skipUnless(IS_WINDOWS, "a running Theodore process that holds a file in the folder: Windows cannot delete it")
    def test_uninstall_stops_theodores_own_background_process_and_removes_the_folder_in_one_run(self):
        # ACCEPT-LOCAL2 D-C: the background LibreOffice download's python.exe held the runtime; the folder stayed, and the
        # status asked to run the deleted launcher again.
        import subprocess
        import time
        self.api.issue_activation_code("ABCD-1234")
        self.assertEqual(self.install("--code", "ABCD-1234").returncode, 0)
        self.box.wait_jobs()
        home = self.box.home
        held = home / "runtime" / "held-by-a-job.bin"
        holder = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1], 'wb'); "
                                   "child = __import__('subprocess').Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
                                   "time.sleep(600)", str(held)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        (home / "state" / "jobs").mkdir(parents=True, exist_ok=True)
        (home / "state" / "jobs" / "libreoffice.json").write_text(json.dumps({"pid": holder.pid, "phase": "downloading LibreOffice",
                                                                                "percent": 30, "started": int(time.time())}))
        time.sleep(2)
        removed = self.install("--uninstall")
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertTrue(status_lines(removed.stdout)[0].startswith("THEODORE OK: Theodore was removed"), removed.stdout)
        self.assertNotIn("run the same command again", removed.stdout)
        holder.wait(timeout=30)                 # stopped by its job file's pid, with its child
        self.wait_gone(home)

    def install(self, *args, env=None, installer=None):
        import subprocess
        installer = installer or self.box.installer()
        command = cmd_command(str(installer), *args) if IS_WINDOWS else ["/bin/sh", str(installer), *args]
        return subprocess.run(command, env={**self.box.env, **(env or {})}, capture_output=True, text=True,
                              timeout=1800, stdin=subprocess.DEVNULL, cwd=str(self.box.tmp))

    def test_install_with_a_code_then_again_then_uninstall(self):
        self.api.issue_activation_code("ABCD-1234")
        result = self.install("--code", "ABCD-1234")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        self.assertRegex(out.splitlines()[0], r"^theodore-install: installer sha256 [0-9a-f]{64} ")
        self.assertNotIn("ABCD-1234", out + result.stderr)
        first, _next, tell = status_lines(out)
        self.assertTrue(first.startswith("THEODORE OK: Theodore is ready: engine 1.0.0-rc.3"), first)
        self.assertEqual(out.strip().splitlines()[-1], tell)
        home = self.box.home
        self.assertIn(f"the stable launcher is {home / 'bin' / STABLE}", out)
        # the code is redeemed before any large download
        paths = [r["path"] for r in self.api.requests]
        self.assertLess(paths.index("/v1/device/activate"), min(i for i, p in enumerate(paths) if p.startswith("/dl/runtime/")))
        # the assistant running the installer, as the client tells it (the test home sets CODEX_HOME); install.cmd says "installer"
        self.assertEqual(self.api.activations[0]["host"], "installer" if IS_WINDOWS else "codex")
        self.assertFalse((home / "state" / "activation.pending").exists(), "the pending answer was moved into the credential store")
        self.assertTrue((home / "state" / "credentials").exists())
        self.assertEqual(json.loads((home / "engines" / "current.json").read_text())["version"], "1.0.0-rc.3")
        self.assertTrue((home / "plugin" / ".agents" / "plugins" / "marketplace.json").exists())
        self.assertTrue((home / "plugin" / "plugins" / "theodore" / "skills" / "deck-design" / "SKILL.md").exists())
        calls = self.host_calls()
        self.assertIn(f"claude plugin install theodore --marketplace {self.api.base}/marketplace.json", calls)
        self.assertIn(f"codex plugin marketplace add {home / 'plugin'}", calls)
        self.assertIn("codex plugin add theodore@theodore", calls)
        self.assertIn("hosts settings --apply", out, "the settings change is offered, not made")
        self.assertFalse((self.box.user_home / ".claude" / "settings.json").exists())
        self.box.wait_jobs()
        self.assertEqual({v["status"] for v in json.loads((home / "packs" / "state.json").read_text()).values()}, {"ready"})
        self.assertEqual([p.name for p in (home / "staging").iterdir() if p.name.startswith("install")], [], "the installer removed its own files")
        if IS_WINDOWS:          # state\ is the current user's alone, whatever folder THEODORE_HOME points at
            import subprocess
            acl = subprocess.run(["icacls", str(home / "state")], capture_output=True, text=True).stdout
            self.assertNotIn("(I)", acl, acl)
            self.assertNotIn("BUILTIN\\Users", acl, acl)

        again = self.install("--code", "ABCD-1234", "--accept-settings")          # idempotent; the spent code is harmless
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("the one-time code was not accepted", again.stdout)
        self.assertTrue(status_lines(again.stdout)[0].startswith("THEODORE OK: Theodore is ready"))
        settings = json.loads((self.box.user_home / ".claude" / "settings.json").read_text())
        self.assertTrue(settings["extraKnownMarketplaces"]["theodore"]["autoUpdate"])
        self.box.wait_jobs()

        removed = self.install("--uninstall")
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertNotIn("run the same command again", removed.stdout, "never a command that no longer exists")
        if IS_WINDOWS:          # the folder goes after the client has exited, by a cleanup that waits for the last files
            self.wait_gone(home)
        self.assertFalse(home.exists())
        calls = self.host_calls()
        for expected in ("claude plugin uninstall theodore@theodore", "claude plugin marketplace remove theodore",
                         "codex plugin remove theodore@theodore", "codex plugin marketplace remove theodore"):
            self.assertIn(expected, calls)
        self.assertFalse((self.box.user_home / ".claude" / "settings.json").exists(), "the settings file Theodore created is gone again")
        if not IS_WINDOWS:          # on Windows the folder goes a moment after the client has exited
            self.assertEqual(status_lines(self.install("--uninstall").stdout)[0].split(":")[0], "THEODORE OK")

    @unittest.skipUnless(IS_WINDOWS, "install.cmd: the confirmation run after a deferred removal")
    def test_uninstall_confirms_only_when_the_render_cache_is_gone_too(self):
        # The run that confirms a scheduled removal (no launcher left) checks every path the uninstall removes,
        # the render cache under %LOCALAPPDATA% included, and keeps answering 75 while any of it remains.
        local = self.box.tmp / "localappdata"
        cache = local / "pptx-runtime"
        cache.mkdir(parents=True)
        held = open(cache / "f", "w")           # an open file cannot be deleted on Windows
        try:
            left = self.install("--uninstall", env={"LOCALAPPDATA": str(local)})
            self.assertEqual(left.returncode, 75, left.stdout + left.stderr)
            self.assertIn("pptx-runtime", status_lines(left.stdout)[0])
        finally:
            held.close()
        self.assertTrue(cache.exists())
        done = self.install("--uninstall", env={"LOCALAPPDATA": str(local)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(cache.exists())

    def test_the_installer_removes_its_own_download_folder_when_the_cleanup_was_dropped(self):
        # Codex ran the install line split in two and dropped "rm -rf $d" (lane PORTAL, 2026-11-12): the installer
        # removes its own download folder when it ends, only a folder of the line's own name holding nothing else.
        import shutil
        name = "install.cmd" if IS_WINDOWS else "install.sh"
        for folder_name in ("theodore-1a2b3c", "tmp.Ab12Cd34") if not IS_WINDOWS else ("theodore-1a2b3c",):
            folder = self.box.tmp / folder_name
            folder.mkdir()
            shutil.copy2(self.box.installer(), folder / name)
            result = self.install("--uninstall", installer=folder / name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(status_lines(result.stdout)[0].startswith("THEODORE OK"), result.stdout)
            self.assertFalse(folder.exists(), f"{folder_name}: the download folder is gone")
        kept = self.box.tmp / "theodore-keep"
        kept.mkdir()
        shutil.copy2(self.box.installer(), kept / name)
        (kept / "notes.txt").write_text("someone else's")
        self.assertEqual(self.install("--uninstall", installer=kept / name).returncode, 0)
        self.assertEqual(sorted(p.name for p in kept.iterdir()), sorted([name, "notes.txt"]), "a folder with anything else stays whole")
        other = self.box.tmp / "downloads"
        other.mkdir()
        shutil.copy2(self.box.installer(), other / name)
        self.assertEqual(self.install("--uninstall", installer=other / name).returncode, 0)
        self.assertTrue((other / name).exists(), "a folder of another name is never touched")

    def test_the_installer_removes_install_md_copies_holding_the_code(self):
        # The agent saved install.md, whose first line carries the one-time code, to /tmp (lane accept-local): the
        # installer removes a copy it can identify by that first line, and nothing else.
        import shutil
        page = (Path(__file__).resolve().parents[1] / "install" / "install.md").read_text().replace("{{ACTIVATION_CODE}}", "ABCD-2345")
        temp = Path(self.box.env["TMPDIR"]) if not IS_WINDOWS else self.box.tmp / "temp"
        temp.mkdir(exist_ok=True)
        env = {"TMPDIR": str(temp), "TEMP": str(temp), "TMP": str(temp)}
        loose = temp / "install.md"
        loose.write_text(page)
        name = "install.cmd" if IS_WINDOWS else "install.sh"
        own = self.box.tmp / "theodore-own"
        own.mkdir()
        shutil.copy2(self.box.installer(), own / name)
        (own / "install.md").write_text(page)
        private = temp / "tmp.Xy12Ab34"
        private.mkdir()
        (private / "install.md").write_text(page)
        other = temp / "notes"
        other.mkdir()
        (other / "install.md").write_text("# my own install notes\n")
        result = self.install("--uninstall", env=env, installer=own / name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(loose.exists(), "the copy straight in the temporary folder")
        self.assertFalse(own.exists(), "the installer's own folder, with its copy")
        if not IS_WINDOWS:
            self.assertFalse(private.exists(), "a private folder holding only the copy")
        self.assertEqual((other / "install.md").read_text(), "# my own install notes\n", "a file of another content stays")

    def test_uninstall_with_no_installation_still_removes_the_engines_former_caches(self):
        temp = self.box.tmp / "tmp"
        env = {"TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp)}
        cache = self.box.user_home / ".cache"
        raster = cache / "pptx-slide-raster" / ("c" * 64)
        raster.mkdir(parents=True)
        (raster / "slide-0001-150.jpg").write_bytes(b"jpg")
        (cache / "pptx-icons").mkdir()
        (cache / "pptx-icons" / f"{'1' * 40}.png").write_bytes(b"png")
        (cache / "pptx-icons" / "mine.txt").write_text("not the engine's")
        result = self.install("--uninstall", env=env)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertIn(str(cache / "pptx-icons"), status_lines(result.stdout)[0])
        self.assertFalse((cache / "pptx-slide-raster").exists())
        self.assertEqual([p.name for p in (cache / "pptx-icons").iterdir()], ["mine.txt"])
        (cache / "pptx-icons" / "mine.txt").unlink()
        done = self.install("--uninstall", env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse((cache / "pptx-icons").exists())

    def test_without_a_code_it_prints_the_sign_in_link_opens_no_browser_and_still_sets_up(self):
        opened = self.box.tmp / "opened.log"
        if IS_WINDOWS:
            browser = self.box.tmp / "fake-browser.cmd"
            browser.write_text(f'@echo off\r\n>>"{opened}" echo %*\r\n')
        else:
            browser = self.box.tmp / "fake-browser"
            browser.write_text(f'#!/bin/sh\necho "$1" >> "{opened}"\n')
            browser.chmod(0o755)
        result = self.install(env={"THEODORE_NO_BROWSER": "", "THEODORE_TEST_BROWSER": str(browser)})
        self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
        first, nxt, tell = status_lines(result.stdout)
        self.assertTrue(first.startswith("THEODORE SIGN_IN_REQUIRED"), first)
        self.assertIn("https://theodoreand.ai/device?code=", nxt, "the last block carries the sign-in link for the assistant to relay")
        self.assertIn("https://theodoreand.ai/device?code=", tell)
        self.assertEqual(result.stdout.strip().splitlines()[-1], tell)
        import time
        time.sleep(1)
        self.assertFalse(opened.exists(), "the installer opens no browser")
        self.assertTrue((self.box.home / "runtime" / RUNTIME_ID / ".ready").exists(), "the runtime is installed while the user approves")
        self.assertNotIn("/v1/device/activate", [r["path"] for r in self.api.requests])

    def test_a_folder_of_another_service_is_refused_before_anything_is_downloaded(self):
        (self.box.home / "state").mkdir(parents=True)
        (self.box.home / "state" / "environment").write_text("https://api.other.example.test\n")
        result = self.install()
        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
        first, nxt, _tell = status_lines(result.stdout)
        self.assertIn("https://api.other.example.test", first)
        self.assertIn(self.api.base, first)
        self.assertIn("THEODORE_HOME", nxt)
        self.assertIn("uninstall", nxt)
        self.assertEqual([r for r in self.api.requests if r["path"].startswith("/dl/")], [], "nothing was downloaded")
        self.assertEqual(sorted(p.name for p in self.box.home.iterdir()), ["state"])

    def test_uninstall_without_a_launcher_still_removes_recorded_specimen_folders(self):
        state = self.box.home / "state"
        state.mkdir(parents=True)
        recorded = self.box.tmp / "an old TMPDIR" / "theodore-specimens-0123456789abcdef"
        recorded.mkdir(parents=True)
        (recorded / "cover-01.jpg").write_bytes(b"jpeg")
        precious = self.box.tmp / "Documents"
        precious.mkdir()
        (state / "specimens.txt").write_text(f"1700000000 {recorded}\n1700000000 {precious}\n")
        result = self.install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(recorded.exists())
        self.assertTrue(precious.exists())

    def test_bad_arguments(self):
        for args in (("--frobnicate",), ("--code", "nope")):
            result = self.install(*args)
            self.assertEqual(result.returncode, 64, result.stdout)
            self.assertTrue(status_lines(result.stdout)[0].startswith("THEODORE USAGE: "))
        self.assertFalse((self.box.home / "plugin").exists())

    def test_blocked_download_host_points_to_the_direct_route(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        result = self.install("--code", "ABCD-1234", env={"THEODORE_DL_BASE": f"http://127.0.0.1:{port}"})
        self.assertEqual(result.returncode, 68, result.stdout)
        first, nxt, _tell = status_lines(result.stdout)
        self.assertIn("cause NETWORK", first)
        self.assertIn("register the plugin directly", nxt)

    def test_tampered_plugin_archive_is_refused(self):
        """Whoever controls the download host (or an accepted inspecting proxy) cannot substitute the plugin: the
        release document must carry a signature from a pinned key, and the archive must match it."""
        import base64
        from fake_api import b64u, public_key, sign
        signed = json.loads(self.api.files["plugin/latest.json"])
        document = json.loads(signed["manifest"])
        other_seed = __import__("hashlib").sha256(b"an attacker's key").digest()
        tampered_archive = self.api.files[f"plugin/{document['archive']}"][:-10] + b"0123456789"
        forged = dict(document, sha256=__import__("hashlib").sha256(tampered_archive).hexdigest(), size=len(tampered_archive))
        from fake_api import canonical_json
        cases = {
            "unsigned latest.txt only": None,
            "a release document signed with another key": {"manifest": canonical_json(forged), "kid": signed["kid"],
                                                           "signature": b64u(sign(other_seed, canonical_json(forged).encode()))},
            "a signed document with a substituted archive": signed,
            "a signed document of another kind": self.api.signed(dict(document, schema="theodore-channel-manifest@1")),
        }
        for name, envelope in cases.items():
            with self.subTest(name):
                self.api.files.pop("plugin/latest.json", None)
                self.api.files["plugin/latest.txt"] = f"{document['version']} {forged['sha256']}\n".encode()
                if envelope is not None:
                    self.api.files["plugin/latest.json"] = json.dumps(envelope).encode()
                self.api.files[f"plugin/{document['archive']}"] = tampered_archive
                result = self.install()
                self.assertEqual(result.returncode, 68, result.stdout + result.stderr)
                self.assertIn("cause HASH_MISMATCH" if envelope is not None else "cause NETWORK", status_lines(result.stdout)[0])
                self.assertFalse((self.box.home / "plugin").exists())
                self.assertFalse((self.box.home / "bin").exists(), "no launcher from the download ever ran")
                self.assertEqual(sorted(p.name for p in (self.box.home / "staging").iterdir()), [], "the installer removed its own files")

    @unittest.skipUnless(IS_WINDOWS, "Windows ACLs")
    def test_the_activation_answer_is_private_even_in_a_folder_others_may_read(self):
        import subprocess
        state = self.box.home / "state"
        state.mkdir(parents=True)
        subprocess.run(["icacls", str(state), "/grant", "*S-1-1-0:(OI)(CI)R"], check=True, capture_output=True)   # Everyone may read
        self.api.issue_activation_code("ABCD-1234")
        # Stop right after the code is redeemed (Python's pin does not match), so the answer is still on disk.
        broken = {k: "0" * 64 for k in json.loads((self.box.plugin / "skills" / "pptx" / "scripts" / "launcher.json").read_text())
                  if k.startswith("stage1_python_") and k.endswith("_sha256")}
        result = self.install("--code", "ABCD-1234", installer=self.box.installer(**broken))
        self.assertEqual(result.returncode, 68, result.stdout + result.stderr)
        pending = state / "activation.pending"
        self.assertTrue(pending.exists(), result.stdout)
        acl = subprocess.run(["icacls", str(pending)], capture_output=True, text=True).stdout
        self.assertNotIn("Everyone", acl, acl)
        self.assertNotIn("S-1-1-0", acl, acl)
        self.assertEqual([p.name for p in state.iterdir() if p.name.startswith("incoming-")], [], "the private folder is gone")

    def test_the_plugin_document_and_archive_follow_no_redirect(self):
        signed = self.api.files["plugin/latest.json"]
        self.api.files["old/latest.json"] = signed
        self.api.dl_redirect["plugin/latest.json"] = f"{self.api.base}/dl/old/latest.json"      # same origin, still refused
        result = self.install()
        self.assertEqual(result.returncode, 68, result.stdout + result.stderr)
        self.assertIn("redirect", status_lines(result.stdout)[0])
        self.assertNotIn("/dl/old/latest.json", [r["path"] for r in self.api.requests])
        self.assertFalse((self.box.home / "plugin").exists())

    def test_an_older_plugin_than_the_installed_one_is_refused(self):
        self.api.issue_activation_code("ABCD-1234")
        self.assertEqual(self.install("--code", "ABCD-1234").returncode, 0)
        self.box.wait_jobs()
        installed = self.box.home / "plugin" / "plugins" / "theodore" / ".claude-plugin" / "plugin.json"
        data = json.loads(installed.read_text())
        offered = json.loads((self.box.plugin / ".claude-plugin" / "plugin.json").read_text())["version"]
        major, minor, patch = (int(x) for x in offered.split("-")[0].split("."))
        for newer in ("9.0.0", f"{major}.{minor}.{patch + 1}+build"):     # build metadata does not hide a newer version
            data["version"] = newer                                   # the computer already has a newer plugin
            installed.write_text(json.dumps(data))
            result = self.install()
            self.assertEqual(result.returncode, 68, result.stdout + result.stderr)
            self.assertIn(f"older than the installed plugin {newer}", status_lines(result.stdout)[0])
            self.assertEqual(json.loads(installed.read_text())["version"], newer, "nothing was replaced")
        self.box.wait_jobs()

    def test_python_and_client_are_checked_against_the_installers_pins_before_anything_runs(self):
        python_key = next(k for k in json.loads((self.box.plugin / "skills" / "pptx" / "scripts" / "launcher.json").read_text())
                          if k.startswith("stage1_python_") and k.endswith("_sha256") and (("win32" in k) == IS_WINDOWS))
        for overrides in ({k: "0" * 64 for k in json.loads((self.box.plugin / "skills" / "pptx" / "scripts" / "launcher.json").read_text())
                           if k.startswith("stage1_python_") and k.endswith("_sha256")}, {"stage1_client_sha256": "0" * 64}):
            with self.subTest(sorted(overrides)[0]):
                result = self.install(installer=self.box.installer(**overrides))
                self.assertEqual(result.returncode, 68, result.stdout + result.stderr)
                self.assertIn("cause HASH_MISMATCH", status_lines(result.stdout)[0])
                self.assertIn("SHA-256 this installer carries", status_lines(result.stdout)[0])
                self.assertNotIn("/dl/plugin/latest.json", [r["path"] for r in self.api.requests])
                self.assertEqual(sorted(p.name for p in (self.box.home / "staging").iterdir()), [])
        self.assertTrue(python_key)

    def test_an_installer_without_release_keys_refuses_to_run(self):
        result = self.install(installer=self.box.installer(manifest_key_1_kid="manifest-unset"))
        self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
        self.assertIn("carries no release signing key", status_lines(result.stdout)[0])
        self.assertEqual([r for r in self.api.requests], [], "nothing is downloaded")

    def test_the_repository_installers_carry_the_launcher_pins(self):
        import sync_launchers
        config = json.loads((ROOT / "plugins" / "theodore" / "skills" / "pptx" / "scripts" / "launcher.json").read_text())
        for kind, path in sync_launchers.INSTALLERS.items():
            text = path.read_bytes().decode("utf-8")
            for key, value in sync_launchers.installer_pins(config).items():
                if kind == "cmd" and key.startswith("PIN_PYTHON_") and key != "PIN_PYTHON_win32_x64":
                    continue
                if kind == "sh" and key == "PIN_PYTHON_win32_x64":
                    continue
                self.assertIn(value, text, f"{path.name}: {key}")
            self.assertNotIn("latest.txt", text)


if __name__ == "__main__":
    unittest.main()
