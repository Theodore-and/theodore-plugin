"""The session note and what reads it: `session-start` (the plugin's hook after a compaction or a resume) and the one
line before `pptx start` and `pptx apply`. The client against the fake API, as in test_client."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import CLIENT_DIR, ClientCase

COMPACT_BOTH = ("Theodore: this conversation loaded Theodore's instructions before it was compacted, and they are no longer "
                "here in full. Before you design or build slides again, load them again (run `{l} guide deck-design` and "
                "`{l} guide pptx`, and read what they name), then read the deck's design note again before you continue.")
COMPACT_PPTX = ("Theodore: this conversation loaded Theodore's instructions before it was compacted, and they are no longer "
                "here in full. Before you design or build slides again, load them again (run `{l} guide pptx` and read what "
                "it names), then read the deck's design note again before you continue.")
RESUME_DESIGN = ("Theodore: this conversation loaded Theodore's instructions as g1; the current version is g2. Before you "
                 "design or build slides again, load them again (run `{l} guide deck-design` and read what it names), then "
                 "read the deck's design note again before you continue.")


class SessionCase(ClientCase):
    def setUp(self):
        super().setUp()
        self.launcher = f'"{self.box.home / "bin" / "theodore"}"'

    def note(self, sessions: dict) -> None:
        path = self.box.home / "state" / "sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        path.write_text(json.dumps({"sessions": {sid: {"at": now, "loaded": {skill: {"version": v, "at": now} for skill, v in loaded.items()}}
                                                 for sid, loaded in sessions.items()}}))

    def noted(self) -> dict:
        return self.state("state/sessions.json").get("sessions", {})

    def hook(self, payload, env: dict | None = None):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, "-I", str(CLIENT_DIR), "session-start"], input=text, env={**self.box.env, **(env or {})},
                              capture_output=True, text=True, timeout=60, cwd=str(self.box.tmp))

    def event(self, sid: str, source: str) -> dict:
        return {"session_id": sid, "transcript_path": "/nonexistent/never-read.jsonl", "cwd": str(self.box.tmp),
                "hook_event_name": "SessionStart", "source": source}


class Note(SessionCase):
    def test_guide_and_skill_note_what_they_served_only_with_a_session(self):
        self.sign_in()
        self.assertEqual(self.client("guide", "deck-design").returncode, 0)
        self.assertFalse((self.box.home / "state" / "sessions.json").exists(), "no session id: nothing is noted")
        guide = self.client("guide", "deck-design", env={"CLAUDE_CODE_SESSION_ID": "s-guide"})
        self.assertEqual(guide.returncode, 0, guide.stdout + guide.stderr)
        skill = self.client("skill", "deck-design", env={"CLAUDE_CODE_SESSION_ID": "s-skill"})
        self.assertEqual(skill.returncode, 0, skill.stdout + skill.stderr)
        self.assertNotIn("THEODORE ", skill.stdout.splitlines()[0])
        noted = self.noted()
        self.assertEqual(sorted(noted), ["s-guide", "s-skill"])
        for sid in noted:
            self.assertEqual(noted[sid]["loaded"]["deck-design"]["version"], "g1")
            self.assertEqual(list(noted[sid]["loaded"]), ["deck-design"])
        refused = self.client("guide", "deck-design", "no-such-section", env={"CLAUDE_CODE_SESSION_ID": "s-refused"})
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn("s-refused", self.noted(), "a status is not instructions")
        self.assertEqual(oct((self.box.home / "state" / "sessions.json").stat().st_mode & 0o777), "0o600")

    def test_the_pptx_manual_is_noted_with_its_engine(self):
        self.ready()
        result = self.client("guide", "pptx", env={"CLAUDE_CODE_SESSION_ID": "s-pptx"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.noted()["s-pptx"]["loaded"], {"pptx": {"version": self.ENGINE_VERSION, "at": self.noted()["s-pptx"]["loaded"]["pptx"]["at"]}})

    def test_old_and_surplus_sessions_are_dropped(self):
        path = self.box.home / "state" / "sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        sessions = {f"s{i}": {"at": now - 1000 + i, "loaded": {"pptx": {"version": "1", "at": now}}} for i in range(250)}
        sessions["old"] = {"at": now - 31 * 86400, "loaded": {"pptx": {"version": "1", "at": now}}}
        path.write_text(json.dumps({"sessions": sessions}))
        self.sign_in()
        self.client("guide", "deck-design", env={"CLAUDE_CODE_SESSION_ID": "s-new"})
        noted = self.noted()
        self.assertEqual(len(noted), 200)
        self.assertIn("s-new", noted)
        self.assertNotIn("old", noted)
        self.assertNotIn("s0", noted, "the oldest go first")


class SessionStart(SessionCase):
    def test_compaction_names_the_instructions_the_session_loaded(self):
        self.note({"both": {"deck-design": "g1", "pptx": "1.0.0-rc.3"}, "build": {"pptx": "1.0.0-rc.3"}})
        self.api.requests.clear()
        both = self.hook(self.event("both", "compact"))
        self.assertEqual((both.returncode, both.stdout, both.stderr), (0, COMPACT_BOTH.format(l=self.launcher) + "\n", ""))
        build = self.hook(self.event("build", "compact"))
        self.assertEqual(build.stdout, COMPACT_PPTX.format(l=self.launcher) + "\n")
        self.assertEqual(self.api.requests, [], "the service is never asked")

    def test_nothing_for_a_session_without_a_note_another_source_or_bad_input(self):
        self.note({"s1": {"deck-design": "g1"}})
        self.api.requests.clear()
        for payload in (self.event("unknown", "compact"), self.event("s1", "startup"), self.event("s1", "clear"),
                        self.event("s1", "fork"), {"session_id": "s1"}, "not json", "", "[1, 2]", '{"session_id": 3, "source": "compact"}'):
            result = self.hook(payload)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""), payload)
        (self.box.home / "state" / "sessions.json").write_text("{broken")
        result = self.hook(self.event("s1", "compact"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.api.requests, [])

    def test_nothing_without_a_home(self):
        missing = self.box.tmp / "no home here"
        result = self.hook(self.event("s1", "compact"), env={"THEODORE_HOME": str(missing)})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(missing.exists(), "nothing is created")

    def test_resume_speaks_only_when_a_newer_version_is_current(self):
        self.sign_in()
        self.note({"s1": {"deck-design": "g1"}})
        self.api.guide_version = "g1"
        self.api.requests.clear()
        same = self.hook(self.event("s1", "resume"))
        self.assertEqual((same.returncode, same.stdout), (0, ""))
        self.assertEqual([r["path"] for r in self.api.requests], ["/v1/releases/manifest"], "one request for the current versions")
        self.api.guide_version = "g2"
        newer = self.hook(self.event("s1", "resume"))
        self.assertEqual((newer.returncode, newer.stdout, newer.stderr), (0, RESUME_DESIGN.format(l=self.launcher) + "\n", ""))
        self.api.requests.clear()
        self.assertEqual(self.hook(self.event("other", "resume")).stdout, "")
        self.assertEqual(self.api.requests, [], "no request for a session without a note")

    def test_resume_names_one_label_when_both_skills_moved_together(self):
        """An engine release labels the engine and the design text alike: the line says each label once."""
        self.note({"s1": {"deck-design": "1.0.0-rc.18", "pptx": "1.0.0-rc.18"}})
        (self.box.home / "state" / "update.json").write_text(json.dumps({"guides": {"deck-design": "1.0.0-rc.19"}, "latest": "1.0.0-rc.19"}))
        out = self.hook(self.event("s1", "resume")).stdout
        self.assertIn("loaded Theodore's instructions as 1.0.0-rc.18; the current version is 1.0.0-rc.19.", out)
        self.assertNotIn(" and 1.0.0-rc", out)
        # Different labels are still each named, in order.
        self.note({"s2": {"deck-design": "1.0.0-rc.18+text.1", "pptx": "1.0.0-rc.18"}})
        out = self.hook(self.event("s2", "resume")).stdout
        self.assertIn("as 1.0.0-rc.18+text.1 and 1.0.0-rc.18; the current versions are 1.0.0-rc.19 and 1.0.0-rc.19.", out)

    def test_resume_falls_back_to_what_the_last_check_learned(self):
        self.note({"s1": {"deck-design": "g1"}})          # not signed in: the request cannot be made
        (self.box.home / "state" / "update.json").write_text(json.dumps({"guides": {"deck-design": "g2"}}))
        result = self.hook(self.event("s1", "resume"))
        self.assertEqual((result.returncode, result.stdout), (0, RESUME_DESIGN.format(l=self.launcher) + "\n"))
        (self.box.home / "state" / "update.json").write_text(json.dumps({"guides": {"deck-design": "g1"}}))
        self.assertEqual(self.hook(self.event("s1", "resume")).stdout, "")

    def test_a_slow_service_costs_two_seconds_at_most(self):
        self.sign_in()
        self.note({"s1": {"deck-design": "g1"}})
        (self.box.home / "state" / "update.json").write_text(json.dumps({"guides": {"deck-design": "g2"}}))
        self.api.manifest_delay = 8.0
        started = time.monotonic()
        result = self.hook(self.event("s1", "resume"))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result.returncode, result.stdout), (0, RESUME_DESIGN.format(l=self.launcher) + "\n"))


class EngineLine(SessionCase):
    def setUp(self):
        super().setUp()
        self.ready()
        self.workspace = self.box.tmp / "deck"
        self.workspace.mkdir()

    def pptx(self, verb: str, sid: str | None):
        return self.client("pptx", verb, str(self.workspace), env={"CLAUDE_CODE_SESSION_ID": sid} if sid else None)

    def test_start_in_a_session_that_never_loaded_the_design_instructions(self):
        plain = self.pptx("start", None)
        first = self.pptx("start", "s1")
        self.assertEqual(first.stdout, plain.stdout, "the engine's own output is unchanged")
        self.assertEqual(first.stderr, f"Theodore: if you are designing or redesigning slides, load Theodore's instructions first: run "
                                       f"`{self.launcher} guide deck-design` and read what it names.\n")
        self.assertEqual(json.loads(first.stdout.splitlines()[-1])["ok"], True)
        self.assertEqual(self.pptx("start", "s1").stderr, "", "once per session")
        self.assertEqual(self.pptx("apply", "s2").stderr, "", "never on apply")
        self.assertEqual(plain.stderr, "", "no session id: nothing")

    def test_a_newer_design_version_is_said_once_per_version(self):
        self.pptx("start", None)
        self.note({"s1": {"deck-design": "g1"}})
        update = self.state("state/update.json")
        current = update["guides"]["deck-design"]
        self.assertEqual(current, "g1")
        self.assertEqual(self.pptx("apply", "s1").stderr, "", "the same version: nothing")
        self.assertEqual(self.pptx("start", "s1").stderr, "", "a session that loaded them gets no reminder to load them")
        (self.box.home / "state" / "update.json").write_text(json.dumps({**update, "guides": {"deck-design": "g2"}}))
        plain = self.pptx("apply", None)
        line = self.pptx("apply", "s1")
        self.assertEqual(line.stdout, plain.stdout)
        self.assertEqual(line.stderr, "Theodore: this conversation loaded Theodore's design instructions as g1; the current version is g2. "
                                      f"Before you design further, load them again: run `{self.launcher} guide deck-design` and read what it names.\n")
        self.assertEqual(self.pptx("start", "s1").stderr, "")
        self.assertEqual(self.pptx("apply", "s1").stderr, "")
        (self.box.home / "state" / "update.json").write_text(json.dumps({**update, "guides": {"deck-design": "g3"}}))
        self.assertIn("the current version is g3", self.pptx("apply", "s1").stderr)
        self.assertEqual(self.client("pptx", "version", env={"CLAUDE_CODE_SESSION_ID": "s3"}).stderr, "", "other verbs: nothing")


if __name__ == "__main__":
    unittest.main()
