"""The client's side of the native front (client/theodore_client/front.py, contract C1): its copy in <home>/bin,
launch.json, the short command as the front, and the plugin archives that carry it."""

from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "tools"))

from theodore_client import commands, core, front, install  # noqa: E402

LAUNCH_KEYS = {"v", "home", "runtime_python", "client_dir", "client_version", "engine_dir", "engine_version", "oneshot"}
LAUNCH_REQUIRED = LAUNCH_KEYS - {"oneshot"}


def pe(signed: bool, magic: int = 0x20B) -> bytes:
    """A minimal Windows program header: its certificate table (data directory 4) empty or not."""
    data = bytearray(1024)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    optional = 0x80 + 24
    struct.pack_into("<H", data, optional, magic)
    directories = optional + (112 if magic == 0x20B else 96)
    struct.pack_into("<II", data, directories + 4 * 8, 0x400 if signed else 0, 0x200 if signed else 0)
    return bytes(data) + core.SHIM_MARK.encode() + b" and the plugin's MCP relay"


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tfc")).resolve()
        self.home = self.tmp / "home"
        self.plugin = self.home / "plugin" / "plugins" / "theodore" / "scripts"
        self.plugin.mkdir(parents=True)
        patches = [mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.home)}), mock.patch.object(core, "_HOME", None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("THEODORE_SKILL_DIR", None)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        core.shim_reset()
        self.addCleanup(core.shim_reset)

    def install(self, engine: str = "1.0.0-rc.35", client: str = "0.5.0") -> None:
        """What launch.json describes: a runtime, a current client and a current engine."""
        python = self.home / "runtime" / "rt1" / "python" / ("python.exe" if core.IS_WINDOWS else "bin/python3")
        python.parent.mkdir(parents=True)
        python.write_text("")
        (self.home / "runtime" / "current.txt").write_text("rt1")
        (self.home / "client" / client).mkdir(parents=True)
        (self.home / "client" / client / "__main__.py").write_text("")
        (self.home / "client" / "current.txt").write_text(client)
        (self.home / "engines" / engine).mkdir(parents=True)
        (self.home / "engines" / engine / ".theodore-engine.json").write_text(json.dumps({"version": engine}))
        (self.home / "engines" / "current.json").write_text(json.dumps({"version": engine}))


class Placement(Home):
    def test_binary_names(self):
        for system, arch, windows, name in [("darwin", "arm64", False, "theodore-darwin"), ("darwin", "x64", False, "theodore-darwin"),
                                            ("linux", "x64", False, "theodore-linux-x64"), ("linux", "arm64", False, "theodore-linux-arm64"),
                                            ("linux", "riscv64", False, None), ("win32", "x64", True, "theodore.exe"),
                                            ("win32", "arm64", True, "theodore.exe")]:
            with mock.patch.object(core, "platform_arch", return_value=(system, arch)), mock.patch.object(front, "IS_WINDOWS", windows):
                self.assertEqual(front.binary_name(), name, (system, arch))

    def test_an_authenticode_signature_is_seen(self):
        cases = {"signed.exe": pe(True), "unsigned.exe": pe(False), "signed32.exe": pe(True, 0x10B), "text.exe": b"#!/bin/sh\n", "empty.exe": b""}
        for name, data in cases.items():
            (self.tmp / name).write_bytes(data)
        self.assertEqual({n: front.signed(self.tmp / n) for n in cases},
                         {"signed.exe": True, "unsigned.exe": False, "signed32.exe": True, "text.exe": False, "empty.exe": False})
        self.assertFalse(front.signed(self.tmp / "missing.exe"))

    def test_the_plugins_front_is_copied_to_home_bin_once(self):
        name = front.binary_name()
        if name is None or core.IS_WINDOWS:
            self.skipTest("a Unix system with a front")
        (self.plugin / name).write_bytes(b"\x7fELF front one")
        self.assertIn("front:", front.place())
        placed = self.home / "bin" / "theodore-front"
        self.assertEqual(placed.read_bytes(), b"\x7fELF front one")
        self.assertTrue(placed.stat().st_mode & stat.S_IXUSR)
        self.assertIsNone(front.place(), "the same bytes: nothing to do")
        (self.plugin / name).write_bytes(b"\x7fELF front two")
        front.place()
        self.assertEqual(placed.read_bytes(), b"\x7fELF front two")
        self.assertEqual(front.installed(), placed)

    def test_an_unsigned_windows_front_is_never_placed(self):
        (self.plugin / "theodore.exe").write_bytes(pe(False))
        with mock.patch.object(core, "platform_arch", return_value=("win32", "x64")), mock.patch.object(front, "IS_WINDOWS", True), \
                mock.patch.object(front, "FRONT_NAME", "theodore-front.exe"):
            self.assertIsNone(front.source())
            self.assertIsNone(front.place())
            (self.plugin / "theodore.exe").write_bytes(pe(True))
            self.assertEqual(front.source(), self.plugin / "theodore.exe")
            front.place()
            self.assertTrue((self.home / "bin" / "theodore-front.exe").exists())

    def test_nothing_to_place_without_a_plugin_copy(self):
        self.assertIsNone(front.source())
        self.assertIsNone(front.place())
        self.assertIsNone(front.installed())
        self.assertEqual(front.refresh(), [])


class LaunchJson(Home):
    def test_written_per_c1_once_everything_is_installed(self):
        self.assertIsNone(front.write_launch(), "nothing installed yet: no launch.json")
        self.install()
        self.assertIn("launch.json: client 0.5.0, engine 1.0.0-rc.35", front.write_launch())
        doc = json.loads((self.home / "bin" / "launch.json").read_text())
        self.assertTrue(LAUNCH_REQUIRED <= set(doc) <= LAUNCH_KEYS, doc)
        self.assertEqual(doc["v"], 1)
        self.assertEqual(Path(doc["home"]), self.home)
        self.assertEqual(Path(doc["client_dir"]), self.home / "client" / "0.5.0")
        self.assertEqual(Path(doc["engine_dir"]), install.engine_dir("1.0.0-rc.35"))
        self.assertTrue(Path(doc["runtime_python"]).is_file(), "the runtime's base interpreter, never the venv redirector")
        self.assertNotIn(".venv", doc["runtime_python"])
        self.assertTrue(all(isinstance(doc[k], str) and doc[k] for k in LAUNCH_REQUIRED - {"v"}))
        self.assertIsNone(front.write_launch(), "unchanged: not rewritten")

    def test_follows_the_client_and_the_engine_and_keeps_one_shot(self):
        self.install()
        front.write_launch(oneshot=True)
        (self.home / "client" / "0.5.1").mkdir()
        (self.home / "client" / "0.5.1" / "__main__.py").write_text("")
        (self.home / "client" / "current.txt").write_text("0.5.1")
        front.refresh()
        doc = json.loads((self.home / "bin" / "launch.json").read_text())
        self.assertEqual((doc["client_version"], doc["oneshot"]), ("0.5.1", True))
        front.write_launch(oneshot=False)
        self.assertNotIn("oneshot", json.loads((self.home / "bin" / "launch.json").read_text()))

    def test_engine_activation_rewrites_it(self):
        self.install()
        front.write_launch()
        (self.home / "engines" / "1.0.0-rc.36").mkdir()
        (self.home / "engines" / "1.0.0-rc.36" / ".theodore-engine.json").write_text(json.dumps({"version": "1.0.0-rc.36"}))
        with mock.patch.object(install, "collect_garbage", return_value=[]):
            install.activate_engine("1.0.0-rc.36")
        self.assertEqual(json.loads((self.home / "bin" / "launch.json").read_text())["engine_version"], "1.0.0-rc.36")

    def test_the_front_reads_what_the_client_writes(self):
        """The binary's own reader against this writer: `theodore __front version` sees the installation as ready."""
        sys.path.insert(0, str(ROOT / "tests"))
        from test_front import front_binary
        binary = front_binary()
        if binary is None:
            self.skipTest("no front binary")
        self.install()
        front.write_launch()
        import subprocess
        out = subprocess.run([str(binary), "__front", "version"], capture_output=True, text=True,
                             env={**os.environ, "THEODORE_HOME": str(self.home)}, timeout=30)
        doc = json.loads(out.stdout)
        self.assertEqual(doc["launch"], {"ready": True, "oneshot": False, "client": "0.5.0"})


@unittest.skipIf(core.IS_WINDOWS, "the Unix form of the short command")
class ShortCommand(Home):
    def setUp(self):
        super().setUp()
        self.install()
        self.bin = self.tmp / "localbin"
        self.bin.mkdir()
        patches = [mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"}),     # no other theodore
                   mock.patch.object(core, "shim_dir_candidates", return_value=[self.bin]),
                   mock.patch.object(commands, "_host_record_path", return_value=self.tmp / "host-settings.json"),
                   mock.patch.object(commands, "ensure_instructions", return_value=None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_short_command_runs_the_front_once_placed(self):
        self.assertIn("short command", commands._shim_apply())
        text = (self.bin / "theodore").read_text()
        self.assertNotIn("theodore-front", text, "no front yet: the launcher")
        (self.plugin / front.binary_name()).write_bytes(b"\x7fELF the front " + core.SHIM_MARK.encode())
        front.refresh()
        core.shim_reset()
        commands._shim_apply()
        text = (self.bin / "theodore").read_text()
        self.assertIn(str(self.home / "bin" / "theodore-front"), text)
        self.assertIn(core.SHIM_MARK, text)
        self.assertIn(f"exec '{self.home / 'bin' / 'theodore'}'", text, "the launcher when the front is gone")
        self.assertIn("THEODORE_HOME=", text, "a home that is not ~/.theodore is carried")
        self.assertTrue(commands._shim_written())
        core.shim_reset()
        self.assertTrue(core.shim_active())
        notes: list[str] = []
        commands._shim_take_back([], notes)
        self.assertFalse((self.bin / "theodore").exists())

    def test_a_program_is_ours_only_with_the_mark(self):
        (self.tmp / "ours").write_bytes(b"\x7fELF...." + core.SHIM_MARK.encode() + b"...")
        (self.tmp / "theirs").write_bytes(b"\x7fELF....something else")
        (self.tmp / "script").write_text(f"#!/bin/sh\n# {core.SHIM_MARK}\n")
        self.assertEqual([commands._shim_ours(self.tmp / n) for n in ("ours", "theirs", "script")], [True, False, True])


class Archives(unittest.TestCase):
    def test_the_fronts_builds_go_into_scripts_executable(self):
        import build_plugin_archives
        with tempfile.TemporaryDirectory() as tmp:
            builds = Path(tmp) / "builds"
            builds.mkdir()
            for name in ("theodore-darwin", "theodore-linux-x64", "theodore-linux-arm64"):
                (builds / name).write_bytes(b"\x7fELF " + name.encode())
            (builds / "theodore.exe").write_bytes(pe(False))
            record = build_plugin_archives.build(Path(tmp) / "out", front=builds)
            with zipfile.ZipFile(Path(tmp) / "out" / record["zip"]["file"]) as bundle:
                for name in ("theodore-darwin", "theodore-linux-x64", "theodore-linux-arm64"):
                    self.assertEqual((bundle.getinfo(f"scripts/{name}").external_attr >> 16) & 0o777, 0o755)
                self.assertIn("scripts/theodore.exe", bundle.namelist(), "a test build carries it unsigned")
            import tarfile
            with tarfile.open(Path(tmp) / "out" / record["tar"]["file"]) as tar:
                self.assertEqual(tar.getmember("plugins/theodore/scripts/theodore-darwin").mode, 0o755)
            with self.assertRaises(SystemExit) as refused:
                build_plugin_archives.front_files(builds, release=True)
            self.assertIn("Authenticode", str(refused.exception))
            (builds / "theodore.exe").write_bytes(pe(True))
            self.assertIn("scripts/theodore.exe", build_plugin_archives.front_files(builds, release=True))


if __name__ == "__main__":
    unittest.main()
