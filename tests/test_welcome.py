"""Lane ONBOARD: the welcome the installers end with, and `theodore studio [on|off]`."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest

from support import ClientCase, status_lines

# Nothing a user is told describes how Theodore works, says "skills" or shouts.
MECHANISM = re.compile(r"designer|agent|\blead\b|parallel|sub-?agent|skills|!", re.IGNORECASE)
# The design session's check (2026-11-18): Studio is not reliably slower for a whole deck, and "lighter" reads as fewer
# slides: no claim about time, speed or size in any Studio text.
NO_TIME_CLAIM = re.compile(r"longer|faster|lighter|quick|slow", re.IGNORECASE)


class Studio(ClientCase):
    def setUp(self):
        super().setUp()
        self.sign_in()

    def test_reads_turns_off_and_on_for_the_account(self):
        _f, nxt, tell = self.assertStatus(self.client("studio"), "OK", 0)
        self.assertIn("Studio is on: each slide gets a deeper design pass, which uses more of your AI plan.", tell)
        self.assertIn("studio off changes it only if the user asks", nxt)
        self.assertEqual(self.api.studio_puts, [])

        _f, _n, tell = self.assertStatus(self.client("studio", "off"), "OK", 0)
        self.assertEqual(self.api.studio_puts, [{"studio_enabled": False}])
        self.assertEqual(tell, "TELL THE USER: Studio is off for your account: from your next deck, slides get a simpler design pass that uses less of your AI plan. "
                               "Say if you want it on again.")
        _f, _n, tell = self.assertStatus(self.client("studio"), "OK", 0)
        self.assertIn("Studio is off: slides get a simpler design pass that uses less of your AI plan.", tell)

        self.assertStatus(self.client("studio", "off"), "OK", 0)          # already off: nothing sent
        self.assertEqual(len(self.api.studio_puts), 1)
        _f, _n, tell = self.assertStatus(self.client("studio", "on"), "OK", 0)
        self.assertEqual(self.api.studio_puts[-1], {"studio_enabled": True})
        self.assertEqual(tell, "TELL THE USER: Studio is on again for your account: from your next deck, each slide gets a deeper "
                               "design pass, which uses more of your AI plan.")
        for out in (self.client("studio").stdout, self.client("studio", "off").stdout):
            self.assertNotRegex(status_lines(out)[2], MECHANISM)
            self.assertNotRegex(status_lines(out)[2], NO_TIME_CLAIM)

    def test_a_plan_without_studio_changes_nothing(self):
        self.api.studio = {"studio_enabled": True, "included": False, "available": True}
        _f, _n, tell = self.assertStatus(self.client("studio", "off"), "OK", 0)
        self.assertIn("Studio is not part of your Theodore plan, so it is off.", tell)
        self.assertEqual(self.api.studio_puts, [])

    def test_a_wrong_word_and_a_server_before_the_route(self):
        self.assertStatus(self.client("studio", "maybe"), "USAGE", 64)
        self.api.studio = None
        result = self.client("studio", "off")
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(self.api.studio_puts, [])


class Welcome(ClientCase):
    def setUp(self):
        super().setUp()
        self.ready()

    def welcome(self, *args, env=None):
        return self.client("welcome", *args, env=env)

    def test_asks_about_studio_once_in_five_short_lines_at_most(self):
        _f, nxt, tell = self.assertStatus(self.welcome(), "OK", 0)
        text = tell.removeprefix("TELL THE USER: ")
        self.assertTrue(text.startswith("Welcome to Theodore. It is set up and signed in on this computer."), text)
        self.assertIn("From now on, when you ask me for a presentation, I build it as a PowerPoint file with Theodore.", text)
        self.assertIn("Studio is on, so each slide gets a deeper design pass, which uses more of your AI plan. "
                      "Would you like it off, for simpler slides that use less of it?", text)
        self.assertNotIn("new session", text)                  # not run by an assistant that needs one
        self.assertNotRegex(text, MECHANISM)
        self.assertNotRegex(text, NO_TIME_CLAIM)
        self.assertLessEqual(len(text), 520)
        self.assertIn("at most five short lines", nxt)
        self.assertIn("if they answer that they want Studio off, run", nxt)
        self.assertIn("studio off", nxt)
        self.assertIn("do not ask again", nxt)
        self.assertTrue(nxt.endswith(". This welcome is your whole closing message: say nothing about permissions, settings or "
                                     "uninstalling unless the user asks."), nxt)
        self.assertTrue(json.loads((self.box.home / "state" / "welcome.json").read_text())["studio_asked"])

        _f, nxt, tell = self.assertStatus(self.welcome(), "OK", 0)       # set up again: the question is not repeated
        self.assertNotIn("Studio", tell + nxt)
        self.assertIn("Welcome to Theodore.", tell)

    def test_no_studio_question_when_it_is_off_not_in_the_plan_not_honoured_or_not_readable(self):
        for answer in ({"studio_enabled": False, "included": True, "available": True},
                       {"studio_enabled": True, "included": False, "available": True},
                       {"studio_enabled": True, "included": True, "available": False},
                       None):
            self.api.studio = answer
            _f, nxt, tell = self.assertStatus(self.welcome(), "OK", 0)
            self.assertNotIn("Studio", tell + nxt, answer)
            self.assertNotIn("studio_asked", self.state("state/welcome.json"), answer)

    def test_a_studio_answer_made_elsewhere_counts_as_asked(self):
        self.assertStatus(self.client("studio", "off"), "OK", 0)
        self.api.studio = {"studio_enabled": True, "included": True, "available": True}
        _f, nxt, tell = self.assertStatus(self.welcome(), "OK", 0)
        self.assertNotIn("Studio", tell + nxt)

    def test_codex_needs_a_new_session_and_runs_nothing_else(self):
        _f, nxt, tell = self.assertStatus(self.welcome(env={"CODEX_THREAD_ID": "t-1"}), "OK", 0)
        self.assertIn("Start a new session, then ask for your first deck.", tell)
        self.assertIn("Apart from that, run no Theodore command in this session", nxt)
        self.assertIn("ask them to start a new session first", nxt)

    def test_claude_code_needs_a_new_session_unless_this_session_already_loaded_theodore(self):
        env = {"CLAUDE_CODE_SESSION_ID": "s-new"}
        _f, _n, tell = self.assertStatus(self.welcome(env=env), "OK", 0)
        self.assertIn("Start a new session, then ask for your first deck.", tell)
        (self.box.home / "state").mkdir(parents=True, exist_ok=True)
        (self.box.home / "state" / "sessions.json").write_text(json.dumps(
            {"sessions": {"s-loaded": {"at": 4102444800, "loaded": {"pptx": {"version": "1.0.0", "at": 4102444800}}}}}))
        _f, _n, tell = self.assertStatus(self.welcome(env={"CLAUDE_CODE_SESSION_ID": "s-loaded"}), "OK", 0)
        self.assertNotIn("new session", tell)

    def test_still_downloading_the_welcome_waits_for_the_first_status_that_says_ok(self):
        """Lead's staging run (2026-11-19): an assistant finishes its tool work before it speaks, so a welcome given with
        a NEXT that sends it to `status --wait` was lost. The installer now only sends it there and marks the welcome
        owed; the first `status` that says OK gives it, once."""
        _f, nxt, tell = self.assertStatus(self.welcome("--downloading"), "SETUP_IN_PROGRESS", 67)
        self.assertEqual(tell, "TELL THE USER: Theodore is installed and is finishing a download in the background; this "
                               "takes a few minutes.")
        self.assertIn("status --wait 100 (it returns as soon as the download is done), again while it says "
                      "SETUP_IN_PROGRESS; when it says OK, its answer holds a short welcome for the user.", nxt)
        self.assertNotIn("Welcome", tell + nxt)
        self.assertTrue(self.state("state/welcome.json")["owed"])
        self.assertNotIn("studio_asked", self.state("state/welcome.json"), "not asked yet")

        first, nxt, tell = self.assertStatus(self.client("status", "--wait", "5"), "OK", 0)
        self.assertIn("Set-up is finished.", first)
        self.assertTrue(tell.startswith("TELL THE USER: Welcome to Theodore. It is set up and signed in on this computer."), tell)
        self.assertIn("Studio is on, so each slide gets a deeper design pass", tell)
        self.assertNotIn("A last download", tell)
        self.assertTrue(nxt.startswith("NEXT: give the user this welcome now, in this conversation, in at most five short lines"), nxt)
        self.assertIn("studio off", nxt)
        self.assertTrue(nxt.endswith("This welcome is your whole closing message: say nothing about permissions, settings or "
                                     "uninstalling unless the user asks."), nxt)
        state = self.state("state/welcome.json")
        self.assertFalse(state["owed"])
        self.assertTrue(state["given"] and state["studio_asked"])

        _f, nxt, tell = self.assertStatus(self.client("status"), "OK", 0)             # given: later statuses as before
        self.assertEqual(tell, "TELL THE USER: Theodore is ready.")
        self.assertNotIn("welcome", nxt)

    def test_a_status_with_no_welcome_owed_answers_as_before(self):
        _f, _n, tell = self.assertStatus(self.client("status"), "OK", 0)
        self.assertEqual(tell, "TELL THE USER: Theodore is ready.")
        self.assertStatus(self.welcome(), "OK", 0)                                   # the ready form leaves nothing owed
        self.assertFalse(self.state("state/welcome.json")["owed"])
        _f, _n, tell = self.assertStatus(self.client("status"), "OK", 0)
        self.assertEqual(tell, "TELL THE USER: Theodore is ready.")

    def test_in_codex_still_downloading_the_installer_gives_it_at_once(self):
        _f, nxt, tell = self.assertStatus(self.welcome("--downloading", env={"CODEX_THREAD_ID": "t-1"}), "OK", 0)
        self.assertIn("A last download finishes by itself in the next few minutes. Start a new session, then ask for your first deck.", tell)
        self.assertNotIn("status --wait", nxt)
        self.assertFalse(self.state("state/welcome.json")["owed"])


class WelcomeAfterSignInByLink(ClientCase):
    """A set-up whose one-time code was old or missing ends with a sign-in link instead of the welcome; the sign-in that
    link completes gives it, once per computer, by the installer's rules."""

    def setUp(self):
        super().setUp()
        self.ready()
        self.assertStatus(self.client("logout"), "OK", 0)        # signed out, as a set-up that ended with the link
        (self.box.home / "state" / "welcome.json").unlink(missing_ok=True)

    def sign_in_by_link(self, env=None):
        self.assertStatus(self.client("login", "--start"), "SIGN_IN_REQUIRED", 65)
        self.api.approve_device()
        return self.client("login", "--finish", "--wait", "5", env=env)

    def setup_job_running(self):
        """The setup job still fetching the engine: its record names a live process."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        jobs = self.box.home / "state" / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        (jobs / "setup.json").write_text(json.dumps({"pid": proc.pid, "phase": "engine", "percent": 40}))
        return jobs / "setup.json"

    def test_the_first_sign_in_by_link_gives_the_welcome(self):
        first, nxt, tell = self.assertStatus(self.sign_in_by_link(), "OK", 0)
        self.assertIn("this computer is signed in to Theodore", first)
        self.assertTrue(tell.startswith("TELL THE USER: Welcome to Theodore. It is set up and signed in on this computer."), tell)
        self.assertIn("Studio is on, so each slide gets a deeper design pass", tell)
        self.assertNotIn("A last download", tell)
        self.assertTrue(nxt.startswith("NEXT: give the user this welcome now, in this conversation, in at most five short lines"), nxt)
        self.assertTrue(nxt.endswith("This welcome is your whole closing message: say nothing about permissions, settings or "
                                     "uninstalling unless the user asks."), nxt)
        state = self.state("state/welcome.json")
        self.assertTrue(state["given"] and state["studio_asked"])
        self.assertFalse(state["owed"])

    def test_with_a_download_running_it_is_owed_and_the_first_status_that_says_ok_gives_it(self):
        job = self.setup_job_running()
        _f, nxt, tell = self.assertStatus(self.sign_in_by_link(), "SETUP_IN_PROGRESS", 67)
        self.assertEqual(tell, "TELL THE USER: Theodore is installed and is finishing a download in the background; this "
                               "takes a few minutes.")
        self.assertIn("status --wait 100", nxt)
        self.assertNotIn("Welcome", tell + nxt)
        self.assertTrue(self.state("state/welcome.json")["owed"])

        job.write_text(json.dumps({"pid": 0, "phase": "done", "percent": 100, "done": True}))
        first, nxt, tell = self.assertStatus(self.client("status", "--wait", "5"), "OK", 0)
        self.assertIn("Set-up is finished.", first)
        self.assertTrue(tell.startswith("TELL THE USER: Welcome to Theodore. It is set up and signed in on this computer."), tell)
        self.assertNotIn("A last download", tell)
        self.assertTrue(nxt.startswith("NEXT: give the user this welcome now"), nxt)
        state = self.state("state/welcome.json")
        self.assertTrue(state["given"])
        self.assertFalse(state["owed"])
        _f, _n, tell = self.assertStatus(self.client("status"), "OK", 0)
        self.assertEqual(tell, "TELL THE USER: Theodore is ready.")

    def test_a_second_sign_in_gives_no_welcome(self):
        self.assertStatus(self.sign_in_by_link(), "OK", 0)
        self.assertStatus(self.client("logout"), "OK", 0)
        _f, nxt, tell = self.assertStatus(self.sign_in_by_link(), "OK", 0)
        self.assertEqual(tell, "TELL THE USER: Theodore is signed in on this computer.")
        self.assertEqual(nxt, "NEXT: run the command you were running before the sign-in.")

    def test_in_codex_with_a_download_running_it_is_given_at_once(self):
        self.setup_job_running()
        _f, nxt, tell = self.assertStatus(self.sign_in_by_link(env={"CODEX_THREAD_ID": "t-1"}), "OK", 0)
        self.assertIn("Welcome to Theodore.", tell)
        self.assertIn("A last download finishes by itself in the next few minutes. Start a new session, then ask for your first deck.", tell)
        self.assertIn("Apart from that, run no Theodore command in this session", nxt)
        self.assertNotIn("status --wait", nxt)
        state = self.state("state/welcome.json")
        self.assertTrue(state["given"])
        self.assertFalse(state["owed"])


if __name__ == "__main__":
    unittest.main()
