"""Faults Theodore files by itself (faults.py): an answer with engine_fault, a worker stopped at a ceiling or lost during
a call, and an exception the client only logged in a background job. Against the fake API, through the same path as
`theodore report` (origin automatic, scrubbed, one per fault per day, the account's switch respected, never on the
answer's path)."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase  # noqa: E402

FAULT_OUTPUT = json.dumps({"ok": False, "error_code": "engine_fault", "fault_at": "layout.py:88 place",
                           "error": "The command stopped on a fault in the engine: JSONDecodeError: Expecting value in /Users/x/secret deck.json."}) + "\n"


class FaultReports(ClientCase):
    def setUp(self):
        super().setUp()
        self.ready()
        patch = mock.patch.dict(os.environ, self.box.env)
        patch.start()
        self.addCleanup(patch.stop)
        from theodore_client import core
        for name in ("_HOME", "_CONFIG"):
            other = mock.patch.object(core, name, None)
            other.start()
            self.addCleanup(other.stop)
        from theodore_client import faults
        self.faults = faults
        # Registered last, so it runs first (cleanups run last-in first-out), before the environment and the homes are
        # put back: a report still on its background thread would otherwise be sent with the real settings, to the
        # real service (26 Nov: one such report reached production as TR-BBR7DB).
        self.addCleanup(self._join_fault_threads)

    def _join_fault_threads(self) -> None:
        self.api.report_delay = 0.0
        for thread in [t for t in threading.enumerate() if t.name == "fault-report"]:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "a fault report still running after its test")

    def posts(self) -> list[dict]:
        self.faults.wait_idle()
        return [r for r in self.api.reports if r["origin"] == "automatic"]

    def test_an_engine_fault_is_filed_once_with_verb_class_and_place_only(self):
        self.faults.engine_fault("apply", FAULT_OUTPUT)
        sent = self.posts()
        self.assertEqual(1, len(sent))
        self.assertEqual(("bug", "engine fault in apply"), (sent[0]["kind"], sent[0]["title"]))
        self.assertEqual({"verb": "apply", "error_code": "engine_fault"}, {k: sent[0]["details"][k] for k in ("verb", "error_code")})
        text = json.dumps(sent[0])
        self.assertIn("JSONDecodeError", text)
        self.assertIn("layout:88 place", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("/Users/x", text)
        self.faults.engine_fault("apply", FAULT_OUTPUT)         # the same fault again the same day
        self.assertEqual(1, len(self.posts()))
        self.faults.engine_fault("render", FAULT_OUTPUT)        # another verb is another fault
        self.assertEqual(2, len(self.posts()))

    def test_a_call_lost_at_the_ceiling_or_with_its_worker_is_filed_once(self):
        self.faults.call_lost("render", "did not finish within 900 s", 900.4, "workers.py:526 run <- tools.py:210 _pptx")
        self.faults.call_lost("render", "did not finish within 900 s", 901.0, "workers.py:526 run")
        sent = self.posts()
        self.assertEqual(["call stopped at its ceiling in render"], [r["title"] for r in sent])
        self.assertIn("900", sent[0]["details"]["message"])
        self.assertIn("workers:526", sent[0]["details"]["message"])
        self.faults.call_lost("render", "ended (exit -9)", 12, None)
        self.assertEqual(["call stopped at its ceiling in render", "call lost its worker in render"], [r["title"] for r in self.posts()])

    def test_the_routers_ceiling_files_with_the_last_stack_it_logged(self):
        from theodore_client.service import router
        log: list[str] = []
        service = mock.Mock()
        service.log = log.append
        call = router.Call.__new__(router.Call)
        call.args, call.tool, call.id, call.last_stack = ["finish"], "pptx", "c1", "workers.py:1 a <- tools.py:2 b"
        call.out = lambda *a, **k: None
        call.finish = lambda *a, **k: None
        call.thread = None
        mine = router.Router.__new__(router.Router)
        mine.service = service
        mine._past_ceiling(call, 1800.0)
        sent = self.posts()
        self.assertEqual(["call stopped at its ceiling in finish"], [r["title"] for r in sent])
        self.assertIn("workers:1", sent[0]["details"]["message"])

    def test_a_job_exception_is_filed_once_a_day_scrubbed(self):
        error = ValueError("bad value in C:\\Users\\someone\\folder\\file.json")
        self.faults.job_failed("resume", error)
        self.faults.job_failed("resume", error)
        sent = self.posts()
        self.assertEqual(["background job failed: resume"], [r["title"] for r in sent])
        self.assertIn("ValueError", sent[0]["details"]["message"])
        self.assertNotIn("someone", json.dumps(sent[0]))
        self.faults.job_failed("hosts", "codex: FAILED (could not run codex)")
        self.assertEqual(2, len(self.posts()))

    def test_the_client_hooks_file_a_failed_host_and_a_resume_that_could_not_start(self):
        from theodore_client import commands, core
        marker = commands._hosts_pending_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        core.write_atomic(marker, "1", 0o600)
        with mock.patch.object(commands.front, "refresh"), mock.patch.object(commands, "ensure_settings"), \
                mock.patch.object(commands, "_follow_claude_app"), \
                mock.patch.object(commands, "_register_hosts", return_value=["codex: FAILED (could not run codex)"]):
            commands.ensure_hosts()
        self.assertEqual(["background job failed: hosts"], [r["title"] for r in self.posts()])
        with mock.patch.object(commands.install, "theodore_git_bash", side_effect=ValueError("boom")), \
                mock.patch.object(commands.core.Job, "read", return_value={"started": 1, "failed": True}), \
                mock.patch.object(commands.core.Job, "alive", return_value=False):
            commands.resume_background()
        self.assertEqual(["background job failed: hosts", "background job failed: resume"], [r["title"] for r in self.posts()])

    def test_switched_off_nothing_is_filed(self):
        self.api.auto_reports = False
        from theodore_client import account
        with contextlib.suppress(Exception):
            account.refresh(timeout=3.0, nudge_updates=False)
        self.faults.engine_fault("apply", FAULT_OUTPUT)
        self.faults.call_lost("render", "did not finish within 900 s", 900, None)
        self.faults.job_failed("resume", ValueError("x"))
        self.assertEqual([], self.posts())

    def test_filing_is_off_the_answers_path(self):
        from theodore_client.service import router
        self.api.report_delay = 20.0
        self.addCleanup(setattr, self.api, "report_delay", 0.0)
        call = router.Call.__new__(router.Call)
        call.args, call.tool, call.id, call.engine_timings, call.stdout_kept, call.stdout_kept_size = ["apply"], "pptx", "c1", [], [], 0
        call._emit = lambda *a, **k: None
        started = time.monotonic()
        call.out(FAULT_OUTPUT)
        self.assertLess(time.monotonic() - started, 1.0, "the answer is not held for the report")
        self.faults.queue("x", "apply", "k|slow", "slow", "engine_fault")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(any(t.name == "fault-report" and t.is_alive() for t in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
