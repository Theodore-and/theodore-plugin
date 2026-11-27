"""The update hand-over's successor waits for the engine update before it starts a worker, `theodore --version` prints the
versions, and a Windows shell gets symbols as ASCII (26 Nov: a colleague's machine, mojibake for the warning sign)."""

from __future__ import annotations

import io
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402,F401  (puts the client on sys.path)

from theodore_client import cli, hot, install  # noqa: E402
from theodore_client.service import workers  # noqa: E402


class _Service:
    def __init__(self, handed_over: bool):
        self.handed_over = handed_over

    def log(self, _message):
        pass


class Settle(unittest.TestCase):
    def pool(self, handed_over: bool):
        with mock.patch.object(workers, "pool_sizes", return_value=(1, 2)):
            return workers.Pool(_Service(handed_over))

    def test_a_service_that_was_not_handed_over_does_not_wait(self):
        began = time.monotonic()
        with mock.patch.object(install, "engine_update_pending", return_value=True):
            self.pool(False).settle()
        self.assertLess(time.monotonic() - began, 0.5)

    def test_a_successor_waits_for_the_update_and_for_a_steady_engine(self):
        state = {"engine": "rc.37", "pending": True}
        pool = self.pool(True)
        seen = []

        def flip():
            time.sleep(0.6)
            state["engine"], state["pending"] = "rc.38", False
        import threading
        threading.Thread(target=flip, daemon=True).start()
        with mock.patch.object(workers, "SETTLE_QUIET_S", 0.5), mock.patch.object(workers, "SETTLE_MAX_S", 20.0), \
                mock.patch.object(install, "selected_engine", side_effect=lambda: state["engine"]), \
                mock.patch.object(install, "engine_update_pending", side_effect=lambda: state["pending"]):
            began = time.monotonic()
            pool.settle()
            seen.append(state["engine"])
            took = time.monotonic() - began
        self.assertEqual(seen, ["rc.38"], "it returned only after the engine moved")
        self.assertGreaterEqual(took, 1.0, "the update (0.6 s) and the quiet time (0.5 s)")
        self.assertTrue(pool.settled)

    def test_it_never_waits_longer_than_the_bound(self):
        pool = self.pool(True)
        with mock.patch.object(workers, "SETTLE_MAX_S", 0.6), mock.patch.object(install, "selected_engine", return_value="rc.37"), \
                mock.patch.object(install, "engine_update_pending", return_value=True):
            began = time.monotonic()
            pool.settle()
        self.assertLess(time.monotonic() - began, 3.0)


class Version(unittest.TestCase):
    def test_version_flags_print_the_client_plugin_and_engine(self):
        for flag in ("--version", "-V", "version"):
            out = io.StringIO()
            with mock.patch("theodore_client.commands._snapshot", return_value={
                    "plugin_version": "0.5.2", "launcher_generation": 3, "engine": "1.0.0-rc.38", "runtime_id": "rt"}), \
                    mock.patch.object(sys, "stdout", out), mock.patch("theodore_client.core.check_environment"), \
                    mock.patch.object(cli, "_newest_launcher"):
                code = cli.main([flag])
            self.assertEqual(code, 0, flag)
            text = out.getvalue()
            self.assertIn("theodore client ", text)
            self.assertIn("plugin 0.5.2", text)
            self.assertIn("engine 1.0.0-rc.38", text)


class Console(unittest.TestCase):
    def test_symbols_become_ascii_and_letters_stay(self):
        self.assertEqual(hot.fold("⚠️ Slide 1: a — b → c ✓"), "WARNING Slide 1: a - b -> c OK")
        self.assertEqual(hot.fold("café الع"), "café الع")
        self.assertEqual(hot.fold("rocket \U0001F680 end"), "rocket  end")
        self.assertEqual(hot.fold("plain"), "plain")

    def test_what_a_cp1252_reader_sees_is_ascii_for_the_markers(self):
        sent = hot.fold("⚠️ overlap").encode("utf-8")
        self.assertEqual(sent.decode("cp1252"), "WARNING overlap")

    def test_the_fronts_fold_only_on_windows(self):
        with mock.patch.object(hot.os, "name", "nt"):
            self.assertEqual(hot.console_text("⚠ x"), b"WARNING x")
        with mock.patch.object(hot.os, "name", "posix"):
            self.assertEqual(hot.console_text("⚠ x"), "⚠ x".encode("utf-8"))

    def test_the_clients_own_streams_fold_on_windows(self):
        raw = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        wrapped = cli._Folding(raw)             # noqa: SLF001
        wrapped.write("⚠ warn")
        wrapped.flush()
        self.assertEqual(raw.buffer.getvalue(), b"WARNING warn")


if __name__ == "__main__":
    unittest.main()
