"""The verb path through the client's command line (0.4.34), with the stand-in engine: the engine on its runtime's own
Python, a failing verb's licence refusal read from its output (one engine start), and the engine's help from a file.
Moved out of test_client.py, which stays under the repository's file size limit."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase  # noqa: E402


class EngineVerbPath(ClientCase):
    def test_exit_codes_and_environment_pass_through(self):
        self.ready()
        self.assertEqual(self.client("pptx", "exit", "7").returncode, 7)
        self.assertEqual(self.client("pptx", "exit", "0").returncode, 0)
        env = json.loads(self.client("pptx", "env").stdout)
        self.assertEqual(env["path0"], str(self.box.home / "engines" / self.ENGINE_VERSION / ".runtime" / "bin"))
        self.assertTrue(env["node_modules"])
        # 0.4.34: the engine runs on its runtime's own Python, not through the venv beside it (on Windows a redirector
        # that starts a second process); the venv is still made, for a client that rolls back.
        self.assertTrue(env["runtime_python"], "started by the runtime's python")
        self.assertEqual(env["home"], str(self.box.home))
        py = self.client("python", "-c", "import os, sys; print('venv' if sys.prefix != sys.base_prefix else os.environ.get('THEODORE_TEST_STARTED_BY'))")
        self.assertEqual(py.stdout.strip(), "runtime-python")

    def test_a_failing_verb_starts_the_engine_once(self):
        """0.4.34: a licence refusal is read from the engine's own output (its last JSON object, exit code 1, or 77 from
        engine rc.34), never by starting the engine again with --help (0.25 s on the Windows test machine, 1 to 1.5 s on a
        laptop, per failing verb)."""
        self.ready()
        starts = self.box.tmp / "engine-starts.jsonl"
        env = {"THEODORE_TEST_ENGINE_STARTS": str(starts)}
        engine = self.box.home / "engines" / self.ENGINE_VERSION
        (engine / "REFUSE").write_text("entitlement_clock_rollback")
        for exit_code in ("1", "77"):
            (engine / "REFUSE_EXIT").write_text(exit_code)
            first, nxt, _t = self.assertStatus(self.client("pptx", "slide", "list", "x", env=env), "NO_ENTITLEMENT", 70)
            self.assertIn("date and time", nxt, exit_code)
        (engine / "REFUSE").write_text("not_a_licence_code")
        (engine / "REFUSE_EXIT").write_text("1")
        failed = self.client("pptx", "slide", "list", "x", env=env)
        self.assertEqual(failed.returncode, 1, "a verb's own refusal passes through")
        self.assertEqual(json.loads(failed.stdout)["code"], "not_a_licence_code", "its output whole, as the engine wrote it")
        (engine / "REFUSE").unlink()
        self.assertEqual(self.client("pptx", "exit", "1", env=env).returncode, 1)
        lines = [json.loads(line) for line in starts.read_text().splitlines()]
        self.assertEqual(len(lines), 4, f"one engine start per command: {lines}")
        self.assertNotIn(["--help"], lines)
        big = self.client("pptx", "env", env=env)
        self.assertEqual(json.loads(big.stdout)["home"], str(self.box.home), "the output arrives whole through the client")

    def test_help_is_printed_from_a_file_after_the_first_time(self):
        """0.4.34: `pptx --help` (and `pptx <verb> --help`) start the engine once per engine build; then the client prints
        the kept text (1.7 s on the Windows test machine, the engine's start 0.3 s of it)."""
        self.ready()
        starts = self.box.tmp / "engine-starts.jsonl"
        env = {"THEODORE_TEST_ENGINE_STARTS": str(starts)}
        first = self.client("pptx", "--help", env=env)
        again = self.client("pptx", "--help", env=env)
        self.assertEqual((first.returncode, again.returncode), (0, 0))
        self.assertEqual(again.stdout, first.stdout)
        self.assertEqual(first.stdout, "usage: pptx <verb>\n")
        self.client("pptx", "slide", "--help", env=env)
        self.client("pptx", "slide", "--help", env=env)
        self.assertEqual([json.loads(l) for l in starts.read_text().splitlines()], [["--help"], ["slide", "--help"]], "one start each")
        workspace = self.box.tmp / "ws"
        self.client("pptx", str(workspace), "--help", env=env)
        self.client("pptx", str(workspace), "--help", env=env)
        self.assertEqual(len(starts.read_text().splitlines()), 4, "a path before the flag is never answered from a file")
        engine = self.box.home / "engines" / self.ENGINE_VERSION
        record = json.loads((engine / ".theodore-engine.json").read_text())
        (engine / ".theodore-engine.json").write_text(json.dumps({**record, "sha256": "f" * 64}))     # another build
        self.client("pptx", "--help", env=env)
        self.assertEqual(len(starts.read_text().splitlines()), 5, "another build of the engine prints its own help again")



if __name__ == "__main__":
    unittest.main()
