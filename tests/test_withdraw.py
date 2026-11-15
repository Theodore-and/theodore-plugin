"""Lane PROTECT2: Theodore's licensed files (engine, manual, packs) leave the computer when the service ends its access,
and only then; they come back as on a first install when access is active again."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import CLIENT_DIR, IS_WINDOWS, ClientCase, Sandbox, status_lines

sys.path.insert(0, str(CLIENT_DIR))


class Withdrawal(ClientCase):
    ENGINE = None

    def fresh(self):
        self.box.close()
        self.box = Sandbox(self.api)
        self.box.install_client_directly()
        self.api.installations.clear()
        self.api.activation_codes.clear()
        self.api.account_state = "active"
        self.api.fail_with = self.api.fail_raw = None
        self.api.down = False
        self.ready()
        self.assertTrue(self.installed(), "the engine and the packs are installed before the test")
        # What is not Theodore's own, or not the licensed files: these must all survive.
        self.workspace = self.box.tmp / "my decks" / "q3"
        (self.workspace / ".pptx-state").mkdir(parents=True)
        (self.workspace / "deck.pptx").write_bytes(b"the person's deck")
        (self.box.home / "notes.txt").write_text("not Theodore's", encoding="utf-8")
        self.runtime_files = sorted(p.relative_to(self.box.home) for p in (self.box.home / "runtime").rglob("*"))
        self.assertTrue(self.runtime_files)
        self.work = self.box.tmp / "work"
        self.work.mkdir(exist_ok=True)

    def installed(self) -> bool:
        engines = self.box.home / "engines"
        return any(engines.glob("*/.theodore-engine.json")) and (self.box.home / "packs" / "icons").is_dir()

    def assertRemoved(self):
        home = self.box.home
        self.assertFalse((home / "engines").exists(), "the engine folder (with the manual) is removed")
        self.assertFalse((home / "packs").exists(), "the asset packs are removed")
        self.assertFalse(list((home / "staging").glob("engine-*")) if (home / "staging").exists() else [])
        self.assertTrue((home / "state").is_dir(), "the state stays")
        self.assertTrue((home / "logs").is_dir(), "the logs stay")
        self.assertEqual(sorted(p.relative_to(home) for p in (home / "runtime").rglob("*")), self.runtime_files, "the runtime is untouched")
        self.assertEqual((self.workspace / "deck.pptx").read_bytes(), b"the person's deck")
        self.assertEqual((home / "notes.txt").read_text(encoding="utf-8"), "not Theodore's")
        self.assertEqual(json.loads((home / "state" / "withdrawn.json").read_text())["state"], "removed")

    def assertKept(self):
        self.assertTrue(self.installed(), "the engine and the packs stay")
        record = self.box.home / "state" / "withdrawn.json"
        self.assertTrue(not record.exists() or json.loads(record.read_text()).get("state") != "removed")

    # --- the definitive refusals -------------------------------------------------------------------------------------

    def test_a_revoked_installation_loses_the_engine_the_manual_and_the_packs(self):
        for command in (["guide", "deck-design"], ["prefs", "list"], ["update", "--check"]):
            with self.subTest(command=" ".join(command)):
                self.fresh()
                self.api.revoke_all()
                result = self.client(*command, cwd=self.work)
                first, _nxt, _tell = self.assertStatus(result, "SIGN_IN_REQUIRED", 65)
                self.assertIn("removed from this computer as well", first)
                self.assertRemoved()

    def test_an_engine_only_user_loses_them_through_the_hourly_background_check(self):
        self.fresh()
        self.api.revoke_all()
        result = self.client("licence-check")          # what an engine command starts once the licence is an hour old
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertRemoved()

    def test_a_suspended_account_loses_them_and_gets_them_back_once_access_is_active_again(self):
        self.fresh()
        self.api.account_state = "suspended"
        first, _nxt, _tell = self.assertStatus(self.client("prefs", "list", cwd=self.work), "NO_ENTITLEMENT", 70)
        self.assertIn("removed from this computer as well", first)
        self.assertRemoved()
        self.assertTrue((self.box.home / "state" / "credentials").exists(), "the sign-in stays: a suspension can be lifted")
        self.api.account_state = "active"
        # The next command fetches a licence again; the setup job downloads what is missing, as on a first install.
        self.client("whoami")
        self.box.wait_jobs(120)
        self.assertTrue(self.installed(), "the engine and the packs are back")
        record = json.loads((self.box.home / "state" / "withdrawn.json").read_text())
        self.assertEqual(record["state"], "restored", "the notice is still to be said")

    def test_after_a_restore_the_download_status_says_why_once(self):
        self.fresh()
        self.api.account_state = "inactive"
        self.client("guide", "deck-design", cwd=self.work)
        self.client("whoami")                    # no licence: the refresh says the plan is not active
        self.assertRemoved()
        self.api.account_state = "active"
        self.api.pack_delay = 5.0       # the setup job is still at it when the next command runs
        ws = self.box.tmp / "ws"
        ws.mkdir()
        result = self.client("pptx", "start", str(ws))
        first, _nxt, tell = self.assertStatus(result, "SETUP_IN_PROGRESS", 67)
        self.assertIn("access on this computer is active again", first)
        self.assertIn("downloading it again", tell)
        again = self.client("pptx", "start", str(ws))
        if again.returncode == 67:
            self.assertNotIn("active again", again.stdout, "said once")
        self.box.wait_jobs(120)
        self.api.pack_delay = 0.0
        self.assertFalse((self.box.home / "state" / "withdrawn.json").exists(), "the record goes once the notice is said")

    def test_a_plan_refusal_on_a_guide_route_alone_removes_nothing(self):
        """403 entitlement_inactive on a guide or sheet route can mean the plan lacks that one skill: only the licence
        refresh's answer says the account has no plan."""
        self.fresh()
        self.client("guide", "deck-design", cwd=self.work)         # the first command after a log in refreshes once
        self.api.account_state = "inactive"
        before = len(self.api.requests)
        result = self.client("guide", "deck-design", cwd=self.work)
        self.assertStatus(result, "NO_ENTITLEMENT", 70)
        self.assertEqual([r["path"] for r in self.api.requests[before:]], ["/v1/guide/deck-design"], "learned on the guide route")
        self.assertKept()
        self.assertStatus(self.client("whoami"), "NO_ENTITLEMENT", 70)          # the licence refresh: definitive
        self.assertRemoved()

    # --- what never removes anything ----------------------------------------------------------------------------------

    def test_no_removal_on_a_network_error_a_5xx_a_malformed_answer_or_another_code(self):
        cases = [
            ("down", lambda: setattr(self.api, "down", True)),
            ("503", lambda: setattr(self.api, "fail_with", (503, "service_disabled"))),
            ("500", lambda: setattr(self.api, "fail_with", (500, "internal_error"))),
            ("429", lambda: setattr(self.api, "fail_with", (429, "rate_limited"))),
            ("401 page", lambda: setattr(self.api, "fail_raw", (401, b"<html>installation_revoked</html>"))),
            ("401 device shape", lambda: setattr(self.api, "fail_raw", (401, b'{"error": "installation_revoked"}'))),
            ("403 page", lambda: setattr(self.api, "fail_raw", (403, b"<html>Forbidden by proxy</html>"))),
            ("401 wrong status", lambda: setattr(self.api, "fail_with", (403, "installation_revoked"))),
            ("unauthenticated", lambda: setattr(self.api, "fail_with", (401, "unauthenticated"))),
            ("forbidden", lambda: setattr(self.api, "fail_with", (403, "forbidden"))),
        ]
        for name, prepare in cases:
            with self.subTest(case=name):
                self.fresh()
                prepare()
                for command in (["whoami"], ["prefs", "list"], ["guide", "deck-design"], ["update", "--check"]):
                    result = self.client(*command, cwd=self.work)
                    self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertKept()
                self.api.down, self.api.fail_with, self.api.fail_raw = False, None, None

    def test_an_expired_licence_offline_removes_nothing(self):
        self.fresh()
        self.api.down = True
        offset = {"THEODORE_TEST_CLOCK_OFFSET": str(30 * 86400)}
        result = self.client("pptx", "--help", env=offset)
        self.assertStatus(result, "OFFLINE", 71)
        self.assertKept()

    def test_logout_keeps_every_file(self):
        self.fresh()
        self.assertStatus(self.client("logout"), "OK", 0)
        self.assertKept()
        self.assertFalse((self.box.home / "state" / "withdrawn.json").exists())

    # --- not while a build runs ---------------------------------------------------------------------------------------

    def hold_engine_run(self) -> tuple[subprocess.Popen, Path]:
        """Another Theodore process in the middle of a build: it holds its engine-run lock until `release` exists."""
        release = self.box.tmp / "release"
        script = ("import sys, time, pathlib; sys.path.insert(0, sys.argv[1]);"
                  "from theodore_client import withdraw;"
                  "cm = withdraw.engine_run(); cm.__enter__(); print('held', flush=True);"
                  "p = pathlib.Path(sys.argv[2])\nwhile not p.exists(): time.sleep(0.05)\ncm.__exit__(None, None, None)")
        holder = subprocess.Popen([sys.executable, "-c", script, str(CLIENT_DIR), str(release)], env=self.box.env,
                                  stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder, release

    def test_a_running_build_postpones_the_removal_to_the_next_command(self):
        self.fresh()
        holder, release = self.hold_engine_run()
        try:
            self.api.revoke_all()
            self.assertStatus(self.client("prefs", "list", cwd=self.work), "SIGN_IN_REQUIRED", 65)
            self.assertTrue(self.installed(), "nothing is removed under a running build")
            self.assertEqual(json.loads((self.box.home / "state" / "withdrawn.json").read_text())["state"], "pending")
            self.client("version")
            self.assertTrue(self.installed(), "still running: still postponed")
        finally:
            release.write_text("")
            holder.wait(30)
            holder.stdout.close()
        self.client("version")              # the next command after the build
        self.assertRemoved()

    # --- links ---------------------------------------------------------------------------------------------------------

    def test_links_are_never_followed_out_of_theodores_folder(self):
        self.fresh()
        outside = self.box.tmp / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep", encoding="utf-8")
        engine = next((self.box.home / "engines").glob("*/.theodore-engine.json")).parent
        os.symlink(outside, engine / "a-link-out", target_is_directory=True)
        os.symlink(outside / "precious.txt", engine / "a-file-link")
        self.api.revoke_all()
        self.assertStatus(self.client("prefs", "list", cwd=self.work), "SIGN_IN_REQUIRED", 65)
        self.assertRemoved()
        self.assertEqual((outside / "precious.txt").read_text(encoding="utf-8"), "keep")

    def test_a_packs_folder_that_is_a_link_is_left_alone(self):
        self.fresh()
        packs = self.box.home / "packs"
        moved = self.box.tmp / "packs elsewhere"
        packs.rename(moved)
        os.symlink(moved, packs, target_is_directory=True)
        self.api.revoke_all()
        self.assertStatus(self.client("prefs", "list", cwd=self.work), "SIGN_IN_REQUIRED", 65)
        self.assertFalse((self.box.home / "engines").exists())
        self.assertTrue((moved / "icons").is_dir(), "nothing is removed through a link")
        self.assertTrue(packs.is_symlink())


class InUse(unittest.TestCase):
    """Windows answers "in use" for a file a process holds open: the removal stays marked and is retried, and the
    command never fails. Simulated on every platform by refusing one deletion."""

    def setUp(self):
        from theodore_client import core
        self.core = core
        self.tmp = Path(tempfile.mkdtemp(prefix="theodore-withdraw-"))
        self.addCleanup(lambda: core.remove_tree(self.tmp))
        self.home = self.tmp / "theodore"
        for rel in ("engines/1.0.0/skills/pptx/SKILL.md", "engines/1.0.0/.theodore-engine.json", "engines/current.json",
                    "packs/icons/a.svg", "packs/state.json", "state/x", "runtime/rt/python"):
            path = self.home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        core._HOME = None
        self.addCleanup(setattr, core, "_HOME", None)

    def test_a_file_in_use_is_retried_at_the_next_command(self):
        from theodore_client import withdraw
        withdraw.note_refusal(401, "installation_revoked", "https://api.example/v1/guide/deck-design", True)
        self.assertTrue(withdraw.pending())
        held = self.home / "packs" / "icons" / "a.svg"
        real = os.unlink

        def unlink(path, *args, **kwargs):
            if Path(path) == held:
                raise PermissionError(32, "The process cannot access the file because it is being used by another process")
            return real(path, *args, **kwargs)

        with mock.patch("os.unlink", unlink), mock.patch.object(withdraw.os, "chmod"):
            withdraw.resume()           # never raises
        self.assertTrue(withdraw.pending(), "still marked")
        self.assertTrue(held.exists())
        self.assertFalse((self.home / "engines").exists())
        self.assertFalse((self.home / "packs" / "state.json").exists(), "nothing half-removed still looks installed")
        withdraw.resume()
        self.assertFalse((self.home / "packs").exists())
        self.assertEqual(withdraw.record()["state"], "removed")
        self.assertTrue((self.home / "runtime" / "rt" / "python").exists())
        self.assertTrue((self.home / "state" / "x").exists())

    def test_only_definitive_well_formed_refusals_count(self):
        from theodore_client import withdraw
        refresh, guide = "https://api.example/v1/licence/refresh", "https://api.example/v1/guide/deck-design"
        self.assertTrue(withdraw.definitive(401, "installation_revoked", guide, True))
        self.assertTrue(withdraw.definitive(403, "account_suspended", guide, True))
        self.assertTrue(withdraw.definitive(403, "entitlement_inactive", refresh, True))
        self.assertFalse(withdraw.definitive(403, "entitlement_inactive", guide, True))
        self.assertFalse(withdraw.definitive(401, "installation_revoked", guide, False))
        self.assertFalse(withdraw.definitive(403, "installation_revoked", guide, True))
        for code in ("unauthenticated", "device_proof_invalid", "forbidden", "http_401", "rate_limited"):
            self.assertFalse(withdraw.definitive(401, code, refresh, True), code)

    def test_a_run_in_another_thread_of_this_process_postpones_it(self):
        import threading
        from theodore_client import withdraw
        withdraw.note_refusal(403, "account_suspended", "", True)
        entered, leave = threading.Event(), threading.Event()

        def build():
            with withdraw.engine_run():
                entered.set()
                leave.wait(10)
        thread = threading.Thread(target=build)
        thread.start()
        entered.wait(10)
        withdraw.resume()
        self.assertTrue((self.home / "engines").exists(), "a build in this process holds it off too")
        leave.set()
        thread.join(10)
        withdraw.resume()
        self.assertFalse((self.home / "engines").exists())


if __name__ == "__main__":
    unittest.main()
