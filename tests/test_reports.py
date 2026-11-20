"""Reports: details of several lines (a file or standard input, a command line cut by cmd), and the report the client
files itself on an internal error. Against the fake API, on macOS and Linux (ClientCase); Windows runs test_launcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase, engine_zip, status_lines  # noqa: E402


class DetailsOfSeveralLines(ClientCase):
    """A report's details of more than one line come from a file or standard input: cmd cuts an argument at its first
    line feed (the first Windows user's report arrived ten times, its details ending in the launcher's own text)."""

    def test_details_from_a_utf8_file_are_read_whole(self):
        path = self.box.tmp / "details.txt"
        path.write_bytes("\ufeffguide pptx failed\nexit code 77\nNode v24 \u00e9\n".encode("utf-8"))
        out = self.client("report", "--kind", "bug", "--title", "render failed", "--details-file", str(path), "--dry-run")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual("guide pptx failed exit code 77 Node v24 \u00e9", payload["details"]["message"])

    def test_details_from_standard_input(self):
        done = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve().parents[1] / "client"), "report", "--kind", "bug",
                               "--title", "render failed", "--details", "-", "--dry-run"], input="first line\nsecond line\n".encode("utf-8"),
                              env=self.box.env, capture_output=True, timeout=120, cwd=str(self.box.tmp))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual("first line second line", json.loads(done.stdout)["details"]["message"])

    def test_the_same_length_rules_and_clear_refusals(self):
        long = self.box.tmp / "long.txt"
        long.write_text("x " * 5000, encoding="utf-8")
        payload = json.loads(self.client("report", "--kind", "bug", "--title", "render failed", "--details-file", str(long), "--dry-run").stdout)
        self.assertEqual(4000, len(payload["details"]["message"]))
        first, _n, _t = self.assertStatus(self.client("report", "--kind", "bug", "--title", "render failed", "--details-file",
                                                      str(self.box.tmp / "missing.txt")), "USAGE", 64)
        self.assertIn("--details-file", first)
        (self.box.tmp / "latin.txt").write_bytes(b"caf\xe9")
        first, _n, _t = self.assertStatus(self.client("report", "--kind", "bug", "--title", "render failed", "--details-file",
                                                      str(self.box.tmp / "latin.txt")), "USAGE", 64)
        self.assertIn("not UTF-8", first)
        self.assertStatus(self.client("report", "--kind", "bug", "--title", "render failed", "--details", "d", "--details-file", str(long)),
                          "USAGE", 64)

    def test_a_command_line_cut_by_cmd_is_not_run(self):
        self.ready()
        from theodore_client import cli
        cut = "line one " + cli.LAUNCHER_TAIL + ": Theodore's Python could not be started"
        result = self.client("report", "--kind", "bug", "--title", "render failed", "--dry-run", "--details", cut)
        first, nxt, _t = self.assertStatus(result, "USAGE", 64)
        self.assertIn("first line break", first)
        self.assertIn("--details-file", nxt)
        self.assertEqual([r for r in self.api.requests if r["path"] == "/v1/reports"], [], "nothing was sent")


class AutomaticInternalReports(ClientCase):
    """An internal error is reported by the client itself, once, before its status is printed; nobody is asked."""

    def posts(self) -> list[dict]:
        return [r for r in self.api.reports if r["origin"] == "automatic"]

    def assertNoAsking(self, result):
        lines = status_lines(result.stdout)
        for line in lines:
            self.assertNotIn("?", line)
            self.assertNotIn("report --kind", line)
            self.assertNotIn("skills", line.lower())
        return lines

    def test_an_internal_error_is_reported_once_with_its_fields(self):
        self.ready()
        self.api.path_errors["/v1/preferences"] = (418, "teapot_brewing")
        result = self.client("prefs", "list")
        first, nxt, tell = self.assertStatus(result, "INTERNAL", 78)
        self.assertNoAsking(result)
        self.assertEqual(1, len(self.posts()), result.stdout)
        sent = self.posts()[0]
        self.assertEqual(("bug", "automatic", "internal error in prefs"), (sent["kind"], sent["origin"], sent["title"]))
        self.assertEqual({"verb": "prefs", "error_code": "INTERNAL"}, {k: sent["details"][k] for k in ("verb", "error_code")})
        self.assertIn("teapot_brewing", sent["details"]["message"])
        self.assertNotIn(str(self.box.tmp), json.dumps(sent))
        self.assertEqual(sorted(sent["environment"]), ["arch", "client_version", "engine_version", "host", "os", "plugin_version"])
        self.assertIn(f"reported to Theodore automatically ({sent['ref']})", nxt)
        self.assertIn("doctor", nxt)
        self.assertEqual("TELL THE USER: Theodore hit an internal error and has reported it to Theodore automatically.", tell)
        again = self.client("prefs", "list")
        _f, nxt, tell = self.assertStatus(again, "INTERNAL", 78)
        self.assertEqual(1, len(self.posts()), "the same error is reported once a day")
        self.assertIn(f"already reported to Theodore automatically today ({sent['ref']})", nxt)
        self.assertIn("has reported it", tell)

    def test_a_report_that_cannot_be_sent_now_is_saved_and_said_so(self):
        self.ready()
        self.api.path_errors["/v1/preferences"] = (418, "teapot_brewing")
        self.api.drop_paths = {"/v1/reports"}
        result = self.client("prefs", "list")
        _f, nxt, tell = self.assertStatus(result, "INTERNAL", 78)
        self.assertNoAsking(result)
        self.assertIn("saved on this computer and Theodore sends it by itself", nxt)
        self.assertNotIn("happens again", nxt)
        self.assertEqual("TELL THE USER: Theodore hit an internal error and has saved a report that it sends to Theodore by itself.", tell)
        self.api.drop_paths = set()
        self.api.path_errors["/v1/preferences"] = (418, "kettle_boiling")      # another error: another report
        self.api.report_delay = 30.0
        self.addCleanup(setattr, self.api, "report_delay", 0.0)
        started = time.monotonic()
        result = self.client("prefs", "list")
        took = time.monotonic() - started
        self.assertStatus(result, "INTERNAL", 78)
        self.assertLess(took, 15, "the answer waits for the report at most a few seconds")
        self.assertIn("Theodore sends it by itself", status_lines(result.stdout)[1])

    def test_signed_out_or_switched_off_nothing_is_sent(self):
        from unittest import mock
        from theodore_client import commands, core
        with mock.patch.dict(os.environ, self.box.env), mock.patch.object(core, "_HOME", None), mock.patch.object(core, "_CONFIG", None):
            self.assertEqual(("none", None), commands.file_internal_report("RuntimeError: boom", "prefs"))
        self.assertEqual([r for r in self.api.requests if r["path"] == "/v1/reports"], [])
        self.ready()
        self.api.auto_reports = False
        self.api.path_errors["/v1/preferences"] = (418, "teapot_brewing")
        result = self.client("prefs", "list", env={"THEODORE_TEST_CLOCK_OFFSET": str(13 * 3600)})       # the switch is learned afresh
        _f, nxt, tell = self.assertStatus(result, "INTERNAL", 78)
        self.assertEqual([], self.posts())
        self.assertNotIn("reported", nxt)
        self.assertEqual("TELL THE USER: Theodore hit an internal error.", tell)

    def test_an_engine_that_fails_in_the_background_set_up_is_reported(self):
        self.api.publish_engine(self.ENGINE_VERSION, engine_zip(self.ENGINE_VERSION, broken=True))
        self.sign_in()
        self.assertEqual(self.client("setup", "--job").returncode, 0)
        self.box.wait_jobs()
        self.assertEqual(["internal error in setup"], [r["title"] for r in self.posts()])
        self.assertIn("the engine could not be installed", self.posts()[0]["details"]["message"])


if __name__ == "__main__":
    unittest.main()
