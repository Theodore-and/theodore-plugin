"""The outbox: a report the service cannot take now ("later": 429, 503; or no answer) is saved on this computer and sent
by Theodore itself later, never by the assistant and never with a question to the user. Against the fake API."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase, status_lines  # noqa: E402

LATER = {"Retry-After": "120"}


class Outbox(ClientCase):

    def outbox(self):
        folder = self.box.home / "state" / "outbox"
        return sorted(folder.glob("*.json")) if folder.is_dir() else []

    def report_posts(self) -> int:
        return len([r for r in self.api.requests if r["path"] == "/v1/reports"])

    def report(self, title: str = "render failed", origin: str = "user_requested", env: dict | None = None):
        return self.client("report", "--kind", "bug", "--title", title, "--details", "the render stopped", "--origin", origin, env=env)

    def assertNoAsking(self, result):
        for line in status_lines(result.stdout):
            self.assertNotIn("?", line)
            self.assertNotIn("wait", line.lower())
            self.assertNotIn("skills", line.lower())

    def later_env(self, seconds: int) -> dict:
        return {"THEODORE_TEST_CLOCK_OFFSET": str(seconds)}

    def in_process(self):
        from theodore_client import core
        return mock.patch.dict(os.environ, self.box.env), mock.patch.object(core, "_HOME", None), mock.patch.object(core, "_CONFIG", None)

    # --- saving ------------------------------------------------------------------------------------------------------------

    def test_a_429_saves_the_report_answers_ok_and_a_later_command_sends_it(self):
        self.ready()
        self.api.path_errors["/v1/reports"] = (429, "rate_limited", LATER)
        result = self.report("first report")
        first, nxt, tell = self.assertStatus(result, "OK", 0)
        self.assertNoAsking(result)
        self.assertIn("Theodore sends it by itself", first)
        self.assertIn("do not run the command again and do not ask the user", nxt)
        self.assertEqual("TELL THE USER: I saved the report; Theodore sends it by itself.", tell)
        self.assertEqual(1, self.report_posts(), "one try in that call, nothing more")
        [path] = self.outbox()
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        record = json.loads(path.read_text())
        self.assertEqual("first report", record["payload"]["title"])
        self.assertEqual(path.stem, record["payload"]["client_report_id"])
        self.assertNotIn(str(self.box.tmp), path.read_text(), "saved already scrubbed")
        self.assertAlmostEqual(record["queued_at"] + 120, record["not_before"], delta=2)

        # not before the time: a second report is tried (and saved), the first is not sent again
        result = self.report("second report", env=self.later_env(30))
        self.assertStatus(result, "OK", 0)
        self.assertEqual(2, self.report_posts())
        self.assertEqual(2, len(self.outbox()))

        # once the time has come, the next command sends both, oldest first, and removes the files
        self.api.path_errors.clear()
        result = self.report("third report", env=self.later_env(400))
        _f, _n, tell = self.assertStatus(result, "OK", 0)
        self.assertIn("I sent Theodore a technical bug report", tell)
        self.assertEqual(["first report", "second report", "third report"], [r["title"] for r in self.api.reports])
        self.assertEqual([], self.outbox())

    def test_a_503_and_a_network_failure_save_a_validation_refusal_does_not(self):
        self.ready()
        self.api.path_errors["/v1/reports"] = (503, "service_disabled")
        self.assertStatus(self.report("on a 503"), "OK", 0)
        self.assertEqual(1, len(self.outbox()))
        del self.api.path_errors["/v1/reports"]
        self.api.drop_paths = {"/v1/reports"}
        result = self.report("no connection", env=self.later_env(-3600))      # nothing due: the first one waits
        self.assertStatus(result, "OK", 0)
        self.assertNoAsking(result)
        self.assertEqual(2, len(self.outbox()))
        self.assertEqual([], self.api.reports)
        self.api.drop_paths = set()
        self.api.path_errors["/v1/reports"] = (422, "validation_failed")
        result = self.report("refused for good", env=self.later_env(-3600))
        self.assertStatus(result, "USAGE", 64)
        self.assertEqual(2, len(self.outbox()), "a refusal for good is not saved")

    def test_signed_out_nothing_is_saved_and_logout_removes_the_outbox(self):
        self.ready()
        self.api.path_errors["/v1/reports"] = (429, "rate_limited", LATER)
        self.assertStatus(self.report(), "OK", 0)
        self.assertEqual(1, len(self.outbox()))
        self.assertStatus(self.client("logout"), "OK", 0)
        self.assertFalse((self.box.home / "state" / "outbox").exists(), "logout removes the outbox")
        posts = self.report_posts()
        result = self.report(env=self.later_env(400))
        self.assertStatus(result, "SIGN_IN_REQUIRED", 65)
        self.assertEqual([], self.outbox())
        self.assertEqual(posts, self.report_posts(), "nothing sent while signed out")

    def test_automatic_reports_off_nothing_is_saved_and_nothing_waiting_is_sent(self):
        self.ready()
        self.api.path_errors["/v1/reports"] = (429, "rate_limited", LATER)
        self.assertStatus(self.report("automatic one", origin="automatic"), "OK", 0)
        self.assertEqual(1, len(self.outbox()))
        self.api.path_errors.clear()
        self.api.auto_reports = False
        off = self.later_env(13 * 3600)             # the switch is learned afresh, and found off
        self.assertStatus(self.report("automatic two", origin="automatic", env=off), "BLOCKED_BY_POLICY", 73)
        self.assertEqual(1, len(self.outbox()), "nothing more is saved when automatic reports are off")
        self.assertStatus(self.report("the user's own", env=off), "OK", 0)
        self.assertEqual(["the user's own"], [r["title"] for r in self.api.reports], "the waiting automatic report is not sent")
        self.assertEqual([], self.outbox(), "and is removed")

    # --- delivery ------------------------------------------------------------------------------------------------------------

    def test_a_report_whose_answer_was_lost_is_not_delivered_twice(self):
        self.ready()
        self.api.path_errors["/v1/reports"] = (429, "rate_limited", {"Retry-After": "1"})
        self.assertStatus(self.report("kept"), "OK", 0)
        [path] = self.outbox()
        report_id = path.stem
        self.api.path_errors.clear()
        self.api.report_delay = 6.0                 # the service stores it, but the flush stops waiting first
        self.addCleanup(setattr, self.api, "report_delay", 0.0)
        started = time.monotonic()
        self.assertStatus(self.report("next", env=self.later_env(60)), "OK", 0)
        self.assertLess(time.monotonic() - started - 6.0, 10, "the flush holds the command a few seconds at most")
        time.sleep(1.5)
        self.assertEqual(1, len([r for r in self.api.reports if r["client_report_id"] == report_id]))
        self.assertEqual([report_id], [p.stem for p in self.outbox()], "no answer: the file stays")
        self.api.report_delay = 0.0
        self.assertStatus(self.report("after", env=self.later_env(120)), "OK", 0)
        self.assertEqual([], self.outbox())
        self.assertEqual(1, len([r for r in self.api.reports if r["client_report_id"] == report_id]), "the same id: one report")
        self.assertEqual(["kept", "next", "after"], [r["title"] for r in self.api.reports])

    def test_the_hourly_heartbeat_sends_what_waits(self):
        self.ready()
        self.api.path_errors["/v1/reports"] = (429, "rate_limited", LATER)
        self.assertStatus(self.report("kept"), "OK", 0)
        self.api.path_errors.clear()
        self.client("guide", "pptx", env=self.later_env(2 * 3600))
        self.assertEqual(["kept"], [r["title"] for r in self.api.reports])
        self.assertEqual([], self.outbox())

    def test_an_internal_error_report_is_saved_sent_later_and_once_a_day(self):
        self.ready()
        self.api.path_errors["/v1/preferences"] = (418, "teapot_brewing")
        self.api.path_errors["/v1/reports"] = (429, "rate_limited", LATER)
        result = self.client("prefs", "list")
        _f, nxt, tell = self.assertStatus(result, "INTERNAL", 78)
        self.assertNoAsking(result)
        self.assertIn("Theodore sends it by itself", nxt)
        self.assertNotIn("happens again", nxt)
        self.assertIn("saved a report", tell)
        self.assertEqual(1, len(self.outbox()))
        result = self.client("prefs", "list")
        self.assertIn("Theodore sends it by itself", status_lines(result.stdout)[1])
        self.assertEqual(1, self.report_posts(), "a saved report counts as sent today")
        self.assertEqual(1, len(self.outbox()))
        del self.api.path_errors["/v1/reports"]
        self.assertStatus(self.report("the user's own", env=self.later_env(200)), "OK", 0)
        automatic = [r for r in self.api.reports if r["origin"] == "automatic"]
        self.assertEqual(["internal error in prefs"], [r["title"] for r in automatic])
        self.assertEqual([], self.outbox())
        result = self.client("prefs", "list", env=self.later_env(200))
        self.assertIn("already reported to Theodore automatically today", status_lines(result.stdout)[1])
        self.assertEqual(1, len([r for r in self.api.reports if r["origin"] == "automatic"]), "not sent a second time that day")

    def test_doctor_says_how_many_wait(self):
        self.ready()
        self.api.path_errors["/v1/reports"] = (429, "rate_limited", LATER)
        before = self.client("doctor", "--json")
        self.assertNotIn("reports waiting", before.stdout)
        self.assertStatus(self.report(), "OK", 0)
        after = json.loads(self.client("doctor", "--json").stdout)
        line = [c for c in after["checks"] if c["name"] == "reports waiting"]
        self.assertEqual(1, len(line))
        self.assertEqual("ok", line[0]["level"])
        self.assertTrue(line[0]["detail"].startswith("1 report(s)"))

    # --- bounds and failures (in process) ------------------------------------------------------------------------------------

    def test_at_most_twenty_and_none_older_than_fourteen_days(self):
        from theodore_client import outbox
        patches = self.in_process()
        with patches[0], patches[1], patches[2]:
            for n in range(21):
                self.assertTrue(outbox.save({"client_report_id": f"report{n:04d}", "origin": "user_requested"}))
                time.sleep(0.01)
            names = [p.stem for p in self.outbox()]
            self.assertEqual(20, len(names))
            self.assertNotIn("report0000", names, "the oldest went")
            old = self.outbox()[0]
            record = json.loads(old.read_text())
            record["queued_at"] -= 15 * 86400
            old.write_text(json.dumps(record))
            (old.parent / "garbled00.json").write_text("{not json")
            self.assertEqual(21, outbox.count())        # the old and the garbled file are counted until they are read
            outbox._entries()
            self.assertEqual(19, len(self.outbox()))
            self.assertFalse(old.exists())
            self.assertFalse((old.parent / "garbled00.json").exists())
            outbox.forget()
            self.assertEqual(0, outbox.count())

    def test_a_flush_never_raises_and_never_changes_the_exit_code(self):
        from theodore_client import api, outbox
        self.ready()
        patches = self.in_process()
        with patches[0], patches[1], patches[2]:
            self.assertTrue(outbox.save({"client_report_id": "brokenflush", "origin": "user_requested"}))
            record = json.loads(self.outbox()[0].read_text())
            record["not_before"] = 0
            self.outbox()[0].write_text(json.dumps(record))
            with mock.patch.object(api, "call", side_effect=RuntimeError("boom")):
                self.assertIsNone(outbox.flush())
            self.assertEqual(1, len(self.outbox()))
        # the fake refuses the stored payload for good (422): it is dropped, and the command still answers as usual
        self.assertStatus(self.report("still fine"), "OK", 0)
        self.assertEqual([], self.outbox())
        self.assertEqual(["still fine"], [r["title"] for r in self.api.reports])


if __name__ == "__main__":
    unittest.main()
