"""What every command reads on its way through the service (the admission): the small state files answered from memory
while their stat holds, never stale, and the session's design-version line looked at without the lock. A Studio burst
puts 15 designers' commands through one process at once (rig 1: admission 18-52 ms each); these keep it to a stat."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import CLIENT_DIR  # noqa: E402

sys.path.insert(0, str(CLIENT_DIR))
from theodore_client import core, install, sessions  # noqa: E402


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theodore-test-admission-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.tmp / "home"), "CLAUDE_CODE_SESSION_ID": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = core._HOME                         # noqa: SLF001
        core._HOME = None                          # noqa: SLF001
        self.addCleanup(setattr, core, "_HOME", saved)
        install._BY_STAT.clear()                   # noqa: SLF001
        self.addCleanup(install._BY_STAT.clear)    # noqa: SLF001
        core.home().state.mkdir(parents=True, exist_ok=True)

    def write(self, path: Path, value, age_s: float = 10.0) -> None:
        """A state file as another process leaves it, its time `age_s` in the past (outside the racy window)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        then = time.time() - age_s
        os.utime(path, (then, then))


class StateByStat(HomeCase):
    def test_a_kept_file_is_read_once_and_a_change_by_another_process_is_seen_at_the_next_call(self):
        path = core.home().state / "update.json"
        self.write(path, {"restrictions": {}})
        reads = []
        real = core.read_json
        with mock.patch.object(core, "read_json", side_effect=lambda p, *a: reads.append(p) or real(p, *a)):
            for _ in range(5):
                self.assertEqual(install.update_state(), {"restrictions": {}})
            self.assertEqual(len(reads), 1, "read once while its stat holds")
            # another process writes a restriction: same size or not, a new stat
            self.write(path, {"restrictions": {"kill": True, "kill_message": "x"}}, age_s=5.0)
            self.assertTrue(install.update_state()["restrictions"]["kill"])
            self.assertEqual(len(reads), 2)
        with self.assertRaises(core.Status) as refused:
            install.enforce_restrictions()
        self.assertEqual(refused.exception.name, "BLOCKED_BY_POLICY")

    def test_a_file_written_within_the_racy_window_is_read_every_time(self):
        path = core.home().state / "pins.json"
        reads = []
        real = core.read_json
        with mock.patch.object(core, "read_json", side_effect=lambda p, *a: reads.append(p) or real(p, *a)):
            self.write(path, {"engine": "1.0.0-rc.1"}, age_s=0.0)
            self.assertEqual(install.pins(), {"engine": "1.0.0-rc.1"})
            # the same size and, on a coarse clock, the same time: never trusted inside the window
            path.write_text(json.dumps({"engine": "1.0.0-rc.2"}), encoding="utf-8")
            self.assertEqual(install.pins(), {"engine": "1.0.0-rc.2"})
            self.assertEqual(len(reads), 2)

    def test_what_a_caller_changes_in_its_copy_is_not_kept(self):
        self.write(core.home().state / "pins.json", {"workspaces": {}})
        first = install.pins()
        first["workspaces"]["k"] = {"version": "x"}
        self.assertEqual(install.pins(), {"workspaces": {}})

    def test_a_missing_file_reads_empty_and_is_seen_once_it_exists(self):
        path = core.home().engines / "current.json"
        self.assertEqual(install.engines_state(), {})
        self.write(path, {"version": "1.0.0-rc.9"})
        self.assertEqual(install.engines_state(), {"version": "1.0.0-rc.9"})
        self.assertEqual(install.read_text_by_stat(core.home().runtime / "current.txt"), "")


class NoticeWithoutTheLock(HomeCase):
    def note(self, design: str, told: list[str] | None = None) -> None:
        now = int(time.time())
        self.write(core.home().state / "sessions.json", {"sessions": {"s-burst": {
            "at": now, "loaded": {"deck-design": {"version": design, "at": now}}, **({"told": told} if told else {})}}})
        self.write(core.home().state / "update.json", {"guides": {"deck-design": "g2"}})

    def burst(self, n: int = 15) -> tuple[int, list[str]]:
        """n designers' applies at once in one process, one session: the lock's entries and the lines written."""
        entries, lines = [], []
        real_lock = core.lock

        def counting(name, *a, **k):
            entries.append(name)
            return real_lock(name, *a, **k)
        out = mock.MagicMock()
        out.write.side_effect = lambda text: lines.append(text)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s-burst"}), \
                mock.patch.object(core, "lock", side_effect=counting), mock.patch.object(sys, "stderr", out):
            threads = [threading.Thread(target=sessions.engine_notice, args=("apply",)) for _ in range(n)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        return len(entries), [line for line in lines if line.strip()]

    def test_nothing_to_say_takes_no_lock(self):
        self.note("g2")                                         # the session loaded the current version
        taken, lines = self.burst()
        self.assertEqual((taken, lines), (0, []))

    def test_a_line_said_before_takes_no_lock(self):
        self.note("g1", told=["deck-design g2"])
        taken, lines = self.burst()
        self.assertEqual((taken, lines), (0, []))

    def test_a_line_owed_is_said_once_among_designers_at_once(self):
        self.note("g1")
        _taken, lines = self.burst()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("as g1; the current version is g2", lines[0])
        told = core.read_json(core.home().state / "sessions.json")["sessions"]["s-burst"]["told"]
        self.assertEqual(told, ["deck-design g2"])
        self.assertEqual(self.burst(), (0, []), "said: the next burst takes no lock")


if __name__ == "__main__":
    unittest.main()
