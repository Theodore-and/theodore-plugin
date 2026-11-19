"""The check runner's plan (ci/run.sh plan, ci/textcheck.py): which class a commit lands in, from what changed since its
nearest stamped ancestor. A release that changes only text and bumps the version is class text; anything more is not."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("textcheck", ROOT / "ci" / "textcheck.py")
textcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(textcheck)

SHA_A, SHA_B = "a" * 64, "b" * 64


def launcher(version: str, sha: str, size: int, base: str = "https://dl.theodoreand.ai") -> str:
    return ('{\n  "launcher_generation": 12,\n  "shim_version": "%s",\n  "dl_base": "%s",\n  "stage1_client_version": "%s",\n'
            '  "stage1_client_url": "%s/client/client-%s.tar",\n  "stage1_client_sha256": "%s",\n  "stage1_client_size": %d,\n'
            '  "stage1_python_linux_x64_size": 1\n}\n') % (version, base, version, base, version, sha, size)


def installer_cmd(version: str, sha: str) -> str:
    return 'echo off\r\nset "PIN_CLIENT_VERSION=%s"\r\nset "PIN_CLIENT=client/client-%s.tar %s"\r\nexit /b 0\r\n' % (version, version, sha)


class VersionOnly(unittest.TestCase):
    def test_a_clean_bump_of_each_version_carrying_file(self):
        v = textcheck.version_only
        self.assertTrue(v(textcheck.CLIENT_INIT, '"""x"""\n__version__ = "0.4.13"\n', '"""x"""\n__version__ = "0.4.14"\n', "0.4.13", "0.4.14"))
        self.assertTrue(v("plugins/theodore/.claude-plugin/plugin.json", '{\n  "name": "t",\n  "version": "0.4.13",\n}\n',
                          '{\n  "name": "t",\n  "version": "0.4.14",\n}\n', "0.4.13", "0.4.14"))
        self.assertTrue(v("plugins/theodore/skills/pptx/scripts/launcher.json", launcher("0.4.13", SHA_A, 798720),
                          launcher("0.4.14", SHA_B, 799000), "0.4.13", "0.4.14"))
        self.assertTrue(v("install/install.cmd", installer_cmd("0.4.13", SHA_A), installer_cmd("0.4.14", SHA_B), "0.4.13", "0.4.14"))

    def test_anything_more_than_the_bump_is_not_version_only(self):
        v = textcheck.version_only
        old = launcher("0.4.13", SHA_A, 798720)
        self.assertFalse(v("plugins/theodore/skills/pptx/scripts/launcher.json", old,
                           launcher("0.4.14", SHA_B, 1).replace('"launcher_generation": 12', '"launcher_generation": 13'), "0.4.13", "0.4.14"))
        self.assertFalse(v("plugins/theodore/skills/pptx/scripts/launcher.json", old,
                           launcher("0.4.14", SHA_B, 1, base="https://elsewhere.example"), "0.4.13", "0.4.14"), "the download base is not the version")
        self.assertFalse(v("plugins/theodore/skills/pptx/scripts/launcher.json", old, launcher("0.4.15", SHA_B, 1), "0.4.13", "0.4.14"),
                         "the version must become the client's")
        self.assertFalse(v(textcheck.CLIENT_INIT, '__version__ = "0.4.13"\n', '__version__ = "0.4.14"\nX = 1\n', "0.4.13", "0.4.14"))
        self.assertFalse(v("install/install.cmd", installer_cmd("0.4.13", SHA_A), installer_cmd("0.4.14", SHA_B).replace("\r\n", "\n"),
                           "0.4.13", "0.4.14"), "line endings are part of the file")
        self.assertFalse(v("client/theodore_client/cli.py", 'V = "0.4.13"\n', 'V = "0.4.14"\n', "0.4.13", "0.4.14"), "not a version-carrying file")
        self.assertFalse(v(textcheck.CLIENT_INIT, '__version__ = "0.4.13"\n', '__version__ = "0.4.13"\n', "0.4.13", "0.4.13"))

    def test_shipped_differences_are_member_by_member(self):
        base = {("plugin tar", "plugins/theodore/agents/studio.md"): "1", ("client tar", "client/theodore_client/cli.py"): "2"}
        head = {("plugin tar", "plugins/theodore/agents/studio.md"): "3", ("client tar", "client/theodore_client/cli.py"): "2",
                ("plugin tar", "plugins/theodore/extra"): "4"}
        self.assertEqual(textcheck.shipped_differences(base, head),
                         [("plugin tar", "plugins/theodore/agents/studio.md"), ("plugin tar", "plugins/theodore/extra")])


@unittest.skipIf(os.name == "nt" or not shutil.which("sh") or not shutil.which("git"), "ci/run.sh runs on the Mac (sh and git)")
class Plan(unittest.TestCase):
    """`ci/run.sh plan` in a throwaway repository whose first commit is stamped."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo, self.logs = self.tmp / "repo", self.tmp / "logs"
        for name in ("run.sh", "lock.sh", "textcheck.py"):
            self.write("ci/" + name, (ROOT / "ci" / name).read_text(encoding="utf-8"))
        self.release("0.4.13", SHA_A)
        self.write("plugins/theodore/agents/studio.md", "The Studio agent.\n")
        self.write("plugins/theodore/skills/pptx/SKILL.md", "The stub.\n")
        self.write("client/theodore_client/cli.py", "print('hi')\n")
        self.write("runtime/locks/versions.json", "{}\n")
        self.write("CHANGELOG.md", "## 0.4.13\n")
        self.git("init", "-q")
        self.commit("stamped")
        (self.logs / "green" / "plugin").mkdir(parents=True)
        (self.logs / "green" / "plugin" / self.git("rev-parse", "HEAD")).write_text("checks green\n")

    def write(self, rel: str, text: str):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def release(self, version: str, sha: str):
        self.write(textcheck.CLIENT_INIT, '"""The client."""\n\n__version__ = "%s"\n' % version)
        self.write("plugins/theodore/.claude-plugin/plugin.json", '{\n  "name": "theodore",\n  "version": "%s",\n  "x": 1\n}\n' % version)
        self.write("plugins/theodore/skills/pptx/scripts/launcher.json", launcher(version, sha, 798720 + len(sha)))
        self.write("install/install.sh", '#!/bin/sh\nPIN_CLIENT_VERSION="%s"\nPIN_CLIENT="client/client-%s.tar %s"\n' % (version, version, sha))
        self.write("install/install.cmd", installer_cmd(version, sha))

    def git(self, *args: str) -> str:
        done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
                              cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def commit(self, message: str):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def plan(self) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "THEODORE_"))}
        env["THEODORE_CI_LOGS"] = str(self.logs)
        done = subprocess.run(["sh", "ci/run.sh", "plan"], cwd=self.repo, capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout.split(":", 1)[0]

    def test_text_only(self):
        self.write("plugins/theodore/skills/pptx/SKILL.md", "The stub, reworded.\n")
        self.commit("text")
        self.assertEqual(self.plan(), "text")

    def test_the_agent_file_only(self):
        self.write("plugins/theodore/agents/studio.md", "The Studio agent, reworded.\n")
        self.commit("agent")
        self.assertEqual(self.plan(), "text")

    def test_text_and_a_clean_version_bump(self):
        self.release("0.4.14", SHA_B)
        self.write("plugins/theodore/agents/studio.md", "The Studio agent, reworded.\n")
        self.write("CHANGELOG.md", "## 0.4.14\n\n## 0.4.13\n")
        self.commit("0.4.14")
        self.assertEqual(self.plan(), "text")

    def test_a_version_bump_with_one_more_changed_line_is_client(self):
        self.release("0.4.14", SHA_B)
        self.write("plugins/theodore/.claude-plugin/plugin.json", '{\n  "name": "theodore",\n  "version": "0.4.14",\n  "x": 2\n}\n')
        self.commit("0.4.14 and more")
        self.assertEqual(self.plan(), "client")

    def test_a_new_client_file_is_client(self):
        self.release("0.4.14", SHA_B)
        self.write("client/theodore_client/new.py", "X = 1\n")
        self.commit("new file")
        self.assertEqual(self.plan(), "client")

    def test_a_runtime_lock_is_runtime(self):
        self.write("plugins/theodore/agents/studio.md", "The Studio agent, reworded.\n")
        self.write("runtime/locks/versions.json", '{"x": 1}\n')
        self.commit("lock")
        self.assertEqual(self.plan(), "runtime")


if __name__ == "__main__":
    unittest.main()
