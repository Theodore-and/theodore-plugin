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
        if IS_WINDOWS:
            import time
            for _ in range(60):
                if not home.exists():
                    break
                time.sleep(0.5)
        self.assertFalse(home.exists())
        calls = self.host_calls()
        for expected in ("claude plugin uninstall theodore@theodore", "claude plugin marketplace remove theodore",
                         "codex plugin remove theodore@theodore", "codex plugin marketplace remove theodore"):
            self.assertIn(expected, calls)
        self.assertEqual(json.loads((self.box.user_home / ".claude" / "settings.json").read_text()), {"permissions": {"allow": []}, "extraKnownMarketplaces": {}})
        if not IS_WINDOWS:          # on Windows the folder goes a moment after the client has exited
            self.assertEqual(status_lines(self.install("--uninstall").stdout)[0].split(":")[0], "THEODORE OK")

    def test_without_a_code_it_starts_the_browser_sign_in_and_still_sets_up(self):
        result = self.install()
        self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
        self.assertIn("THEODORE SIGN_IN_REQUIRED", result.stdout)
        self.assertIn("https://theodoreand.ai/device?code=", result.stdout)
        self.assertTrue((self.box.home / "runtime" / RUNTIME_ID / ".ready").exists(), "the runtime is installed while the user approves")
        self.assertNotIn("/v1/device/activate", [r["path"] for r in self.api.requests])

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
