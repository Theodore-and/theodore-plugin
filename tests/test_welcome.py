"""Lane ONBOARD: the welcome the installers end with, and `theodore studio [on|off]`."""

from __future__ import annotations

import json
import re
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
            self.assertFalse((self.box.home / "state" / "welcome.json").exists(), answer)

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

    def test_still_downloading(self):
        _f, nxt, tell = self.assertStatus(self.welcome("--downloading"), "SETUP_IN_PROGRESS", 67)
        self.assertIn("Welcome to Theodore.", tell)
        self.assertIn("A last download finishes by itself in the next few minutes.", tell)
        self.assertIn("status --wait 100", nxt)
        self.assertIn("tell the user in one short line that Theodore has finished downloading", nxt)
        _f, nxt, tell = self.assertStatus(self.welcome("--downloading", env={"CODEX_THREAD_ID": "t-1"}), "OK", 0)
        self.assertIn("A last download finishes by itself in the next few minutes. Start a new session, then ask for your first deck.", tell)
        self.assertNotIn("status --wait", nxt)


if __name__ == "__main__":
    unittest.main()
