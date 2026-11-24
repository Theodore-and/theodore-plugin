"""Lane ONBOARD: the welcome the installers end with, and `theodore studio [on|off]`."""

from __future__ import annotations

import json
import re
import shutil
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


# The words every ready says (lane L3): the welcome's opening, Studio's state, the asset packs' state.
WELCOME = ("Welcome to Theodore: it is set up and signed in on this computer, and from now on I build your presentations "
           "with it, as PowerPoint files.")
STUDIO_ON = "Studio is on: each slide gets a deeper design pass, which uses more of your AI plan."
STUDIO_OFF = "Studio is off: slides get a simpler design pass that uses less of your AI plan."
PACKS_READY = "Its icon, flag, map and background libraries are ready."
# An assistant runs the installer (Claude Code sets these for the commands it runs); a person's terminal has neither.
ASSISTANT = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s-loaded"}


class Welcome(ClientCase):
    def setUp(self):
        super().setUp()
        self.ready()
        # the assistant's session already loaded Theodore (Claude Code 2.1.330 picks a new plugin up mid-session)
        (self.box.home / "state").mkdir(parents=True, exist_ok=True)
        (self.box.home / "state" / "sessions.json").write_text(json.dumps(
            {"sessions": {"s-loaded": {"at": 4102444800, "loaded": {"pptx": {"version": "1.0.0", "at": 4102444800}}}}}))

    def welcome(self, *args, env=ASSISTANT):
        return self.client("welcome", *args, env=env)

    def test_states_studio_and_the_packs_in_five_short_lines_and_asks_nothing(self):
        _f, nxt, tell = self.assertStatus(self.welcome(), "OK", 0)
        text = tell.removeprefix("TELL THE USER: ")
        self.assertTrue(text.startswith(WELCOME), text)
        self.assertIn(STUDIO_ON, text)
        self.assertIn(PACKS_READY, text)
        self.assertNotIn("?", text, "a state, never a question (24 Nov: he asked 'can you turn studio on?' 10 s later)")
        self.assertNotIn("new session", text)
        self.assertNotIn("quit the app", text, "the app's chats only when the user asks")
        self.assertNotRegex(text, MECHANISM)
        self.assertNotRegex(text, NO_TIME_CLAIM)
        self.assertLessEqual(len(text), 600)
        self.assertIn("at most five short lines", nxt)
        self.assertIn("if they say they want Studio off, run", nxt)
        self.assertIn("studio off", nxt)
        self.assertTrue(nxt.endswith(". This welcome is your whole closing message: say nothing about permissions, settings or "
                                     "uninstalling unless the user asks."), nxt)
        _f, _n, again = self.assertStatus(self.welcome(), "OK", 0)       # set up again: the state is said again
        self.assertIn(STUDIO_ON, again)

    def test_studio_off_not_in_the_plan_or_not_readable(self):
        for answer, said in (({"studio_enabled": False, "included": True, "available": True}, STUDIO_OFF),
                             ({"studio_enabled": True, "included": False, "available": True},
                              "Studio, a deeper design pass for each slide, is not part of your Theodore plan."),
                             ({"studio_enabled": True, "included": True, "available": False}, None),
                             (None, None)):
            self.api.studio = answer
            _f, nxt, tell = self.assertStatus(self.welcome(), "OK", 0)
            if said:
                self.assertIn(said, tell, answer)
            else:
                self.assertNotIn("Studio", tell, answer)
            self.assertEqual("studio off" in nxt, bool(answer and answer["included"] and answer["available"]), (answer, nxt))
            self.assertIn(PACKS_READY, tell)

    def test_packs_still_downloading_say_about_how_much_is_left(self):
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        (self.box.home / "state" / "jobs" / "packs.json").write_text(json.dumps({"pid": proc.pid, "phase": "packs"}))
        packs = json.loads((self.box.home / "packs" / "state.json").read_text())
        packs["maps"].update(status="downloading", percent=50)
        (self.box.home / "packs" / "state.json").write_text(json.dumps(packs))
        update = self.state("state/update.json")
        update["published_pack_sizes"] = {"icons": 30_000_000, "flags": 10_000_000, "maps": 40_000_000, "backgrounds": 15_000_000}
        (self.box.home / "state" / "update.json").write_text(json.dumps(update))
        _f, _n, tell = self.assertStatus(self.welcome(), "OK", 0)
        self.assertIn("Its icon, flag, map and background libraries are finishing their download by themselves (about 20 MB "
                      "left); a deck can start meanwhile.", tell)

    def test_run_by_a_person_it_ends_in_plain_words_and_the_assistant_gives_the_welcome_later(self):
        result = self.welcome(env={})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("THEODORE ", result.stdout, "no block written for an assistant")
        self.assertNotIn("NEXT:", result.stdout)
        self.assertEqual(result.stdout.splitlines(), ["Theodore is set up and signed in on this computer.", STUDIO_ON, PACKS_READY,
                                                      "Go back to your assistant (Claude or Codex) and ask for your presentation there."])
        self.assertTrue(self.state("state/welcome.json")["owed"])
        # the person goes back to Claude and asks "now is it installed?": Theodore's check gives the welcome, once
        first, nxt, tell = self.assertStatus(self.client("doctor", env=ASSISTANT), "OK", 0)
        self.assertIn("Set-up is finished.", first)
        self.assertTrue(tell.startswith("TELL THE USER: " + WELCOME), tell)
        self.assertIn(STUDIO_ON, tell)
        self.assertFalse(self.state("state/welcome.json")["owed"])
        _f, _n, tell = self.assertStatus(self.client("status", env=ASSISTANT), "OK", 0)
        self.assertEqual(tell, f"TELL THE USER: Theodore is ready. {STUDIO_ON} {PACKS_READY}")

    def test_owed_to_an_assistant_that_never_checked_it_heads_its_first_instructions(self):
        self.assertEqual(self.welcome(env={}).returncode, 0)
        body = self.client("skill", "pptx", env=ASSISTANT).stdout
        self.assertTrue(body.startswith(f"THEODORE NOTICE: Theodore is set up and signed in on this computer. {STUDIO_ON} "
                                        f"{PACKS_READY} Tell the user once, in one or two lines; it does not stop this task.\n"),
                        body[:400])
        self.assertFalse(self.state("state/welcome.json")["owed"])
        self.assertNotIn("THEODORE NOTICE: Theodore is set up", self.client("skill", "pptx", env=ASSISTANT).stdout, "once")

    def test_codex_needs_a_new_session_and_runs_nothing_else(self):
        _f, nxt, tell = self.assertStatus(self.welcome(env={"CODEX_THREAD_ID": "t-1"}), "OK", 0)
        self.assertIn("Start a new session, then ask for your first deck.", tell)
        self.assertIn("Apart from that, run no Theodore command in this session", nxt)
        self.assertIn("ask them to start a new session first", nxt)

    def test_claude_code_needs_a_new_session_unless_this_session_already_loaded_theodore(self):
        _f, _n, tell = self.assertStatus(self.welcome(env={"CLAUDE_CODE_SESSION_ID": "s-new"}), "OK", 0)
        self.assertIn("Start a new session, then ask for your first deck.", tell)
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

        first, nxt, tell = self.assertStatus(self.client("status", "--wait", "5", env=ASSISTANT), "OK", 0)
        self.assertIn("Set-up is finished.", first)
        self.assertTrue(tell.startswith("TELL THE USER: " + WELCOME), tell)
        self.assertIn(STUDIO_ON, tell)
        self.assertNotIn("A last download", tell)
        self.assertTrue(nxt.startswith("NEXT: give the user this welcome now, in this conversation, in at most five short lines"), nxt)
        self.assertIn("studio off", nxt)
        state = self.state("state/welcome.json")
        self.assertFalse(state["owed"])
        self.assertTrue(state["given"])

        _f, nxt, tell = self.assertStatus(self.client("status", env=ASSISTANT), "OK", 0)    # given: later statuses as before
        self.assertEqual(tell, f"TELL THE USER: Theodore is ready. {STUDIO_ON} {PACKS_READY}")
        self.assertNotIn("welcome", nxt)

    def test_run_by_a_person_while_it_downloads(self):
        result = self.welcome("--downloading", env={})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[0],
                         "Theodore is installed on this computer and finishes a download by itself in the next few minutes.")
        self.assertNotIn("THEODORE ", result.stdout)
        self.assertTrue(self.state("state/welcome.json")["owed"])

    def test_a_status_with_no_welcome_owed_answers_as_before(self):
        _f, _n, tell = self.assertStatus(self.client("status"), "OK", 0)
        self.assertEqual(tell, f"TELL THE USER: Theodore is ready. {STUDIO_ON} {PACKS_READY}")
        self.assertStatus(self.welcome(), "OK", 0)                                   # the ready form leaves nothing owed
        self.assertFalse(self.state("state/welcome.json")["owed"])

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
        setup_record = (self.box.home / "state" / "jobs" / "setup.json").read_text()
        first, nxt, tell = self.assertStatus(self.sign_in_by_link(), "OK", 0)
        self.assertEqual((self.box.home / "state" / "jobs" / "setup.json").read_text(), setup_record,
                         "the engine is installed: no set-up job is started")
        self.assertIn("this computer is signed in to Theodore", first)
        self.assertTrue(tell.startswith("TELL THE USER: " + WELCOME), tell)
        self.assertIn(STUDIO_ON, tell)
        self.assertNotIn("A last download", tell)
        self.assertTrue(nxt.startswith("NEXT: give the user this welcome now, in this conversation, in at most five short lines"), nxt)
        self.assertTrue(nxt.endswith("This welcome is your whole closing message: say nothing about permissions, settings or "
                                     "uninstalling unless the user asks."), nxt)
        state = self.state("state/welcome.json")
        self.assertTrue(state["given"])
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
        self.assertTrue(tell.startswith("TELL THE USER: " + WELCOME), tell)
        self.assertNotIn("A last download", tell)
        self.assertTrue(nxt.startswith("NEXT: give the user this welcome now"), nxt)
        state = self.state("state/welcome.json")
        self.assertTrue(state["given"])
        self.assertFalse(state["owed"])
        _f, _n, tell = self.assertStatus(self.client("status"), "OK", 0)
        self.assertEqual(tell, f"TELL THE USER: Theodore is ready. {STUDIO_ON} {PACKS_READY}")

    def test_with_no_engine_the_sign_in_starts_the_set_up_job_and_leaves_the_welcome_owed(self):
        """Staging run of 0.4.8: after a sign-in by link the engine was not installed and nothing was fetching it."""
        shutil.rmtree(self.box.home / "engines")
        _f, nxt, tell = self.assertStatus(self.sign_in_by_link(), "SETUP_IN_PROGRESS", 67)
        self.assertIn("status --wait 100", nxt)
        self.assertNotIn("Welcome", tell + nxt)
        self.assertTrue(self.state("state/welcome.json")["owed"])
        self.assertFalse(self.state("state/jobs/setup.json").get("done"), "a new set-up job was started")
        self.box.wait_jobs()
        first, nxt, tell = self.assertStatus(self.client("status", "--wait", "5"), "OK", 0)
        self.assertIn(f"engine {self.ENGINE_VERSION}", first)
        self.assertTrue(tell.startswith("TELL THE USER: " + WELCOME), tell)
        self.assertTrue(self.state("state/welcome.json")["given"])

    def test_with_no_engine_in_codex_the_job_starts_and_the_welcome_comes_at_once(self):
        shutil.rmtree(self.box.home / "engines")
        _f, nxt, tell = self.assertStatus(self.sign_in_by_link(env={"CODEX_THREAD_ID": "t-1"}), "OK", 0)
        self.assertIn("A last download finishes by itself in the next few minutes.", tell)
        self.assertNotIn("status --wait", nxt)
        self.assertFalse(self.state("state/jobs/setup.json").get("done"), "a new set-up job was started")
        self.box.wait_jobs()

    def test_when_the_set_up_job_cannot_start_the_sign_in_still_gives_the_welcome(self):
        shutil.rmtree(self.box.home / "engines")
        jobs = self.box.home / "state" / "jobs"
        jobs.chmod(0o500)                       # no job record can be written
        self.addCleanup(jobs.chmod, 0o700)
        first, nxt, tell = self.assertStatus(self.sign_in_by_link(), "OK", 0)
        self.assertIn("this computer is signed in to Theodore", first)
        self.assertTrue(tell.startswith("TELL THE USER: " + WELCOME), tell)
        self.assertNotIn("A last download", tell)
        self.assertTrue(self.state("state/welcome.json")["given"])
        self.assertIn("welcome: the set-up job did not start", (self.box.home / "logs" / "client.log").read_text())

    def test_a_second_sign_in_gives_no_welcome(self):
        self.assertStatus(self.sign_in_by_link(), "OK", 0)
        self.assertStatus(self.client("logout"), "OK", 0)
        shutil.rmtree(self.box.home / "engines")
        setup_record = (self.box.home / "state" / "jobs" / "setup.json").read_text()
        _f, nxt, tell = self.assertStatus(self.sign_in_by_link(), "OK", 0)
        self.assertEqual((self.box.home / "state" / "jobs" / "setup.json").read_text(), setup_record, "nothing is started")
        self.assertEqual(tell, "TELL THE USER: Theodore is signed in on this computer.")
        self.assertEqual(nxt, "NEXT: run the command you were running before the sign-in.")

    def test_in_codex_with_a_download_running_it_is_given_at_once(self):
        self.setup_job_running()
        _f, nxt, tell = self.assertStatus(self.sign_in_by_link(env={"CODEX_THREAD_ID": "t-1"}), "OK", 0)
        self.assertIn(WELCOME, tell)
        self.assertIn("A last download finishes by itself in the next few minutes. Start a new session, then ask for your first deck.", tell)
        self.assertIn("Apart from that, run no Theodore command in this session", nxt)
        self.assertNotIn("status --wait", nxt)
        state = self.state("state/welcome.json")
        self.assertTrue(state["given"])
        self.assertFalse(state["owed"])


if __name__ == "__main__":
    unittest.main()
