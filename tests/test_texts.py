"""Texts and sheets served online on every call (contract C6, service/texts.py): one request per call, conditional when
the body is held, the body held in this process's memory only; a call's items in one batch; the single routes from a
service without the batch route; the shell route's sheet files per session; nothing served without its request."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
import support  # noqa: E402,F401  (puts the client on the path)
from fake_api import FakeApi, mark_like_the_service, sha256_hex  # noqa: E402
from theodore_client import api, core, specimens  # noqa: E402
from theodore_client.service import texts as texts_module  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"sheet bytes " * 50 + b"\xff\xd9"
NO_PROXY = {k: "" for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")}
ENTRY = "# Deck design\n\nENTRY é—“quoted”\n" + "".join(f"line {i:04d}\n" for i in range(50))


class TextsCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeApi().start()
        self.addCleanup(self.fake.stop)
        self.fake.installations["iid-1"] = {"secret": "secret-1", "revoked": False, "gen": 1, "meta": {}}
        self.fake.guides["deck-design"][""] = ENTRY
        self.fake.guides["deck-design"]["critique"] = "# Critique\n\nCRITIQUE TEXT\n<!-- theodore:if lead -->\nLEAD ONLY\n<!-- theodore:end -->\n"
        self.fake.specimens.update({"a.jpg": JPEG, "b.jpg": JPEG + b"b", "HC3.jpg": JPEG + b"hc3"})
        self.fake.guide_index = {"version": "g1", "sections": [{"name": "critique"}], "specimens": [{"file": "a.jpg"}, {"file": "b.jpg"}, {"file": "HC3.jpg"}]}
        home = Path(tempfile.mkdtemp(prefix="theodore-texts-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "bin").mkdir()
        (home / "bin" / "launcher.json").write_text(json.dumps({"api_base": self.fake.base, "dl_base": self.fake.base}))
        saved = (core._HOME, core._CONFIG, core._REFUSAL, core._REFUSAL_EMITTED)
        core._HOME, core._CONFIG, core._REFUSAL, core._REFUSAL_EMITTED = core.Home(home), None, None, False
        self.addCleanup(lambda: setattr(core, "_HOME", saved[0]) or setattr(core, "_CONFIG", saved[1])
                        or setattr(core, "_REFUSAL", saved[2]) or setattr(core, "_REFUSAL_EMITTED", saved[3]))
        self.enterContext(mock.patch.dict(os.environ, {**NO_PROXY, "THEODORE_HOME": str(home), "THEODORE_CREDENTIAL_STORE": "file",
                                                       "THEODORE_WORK_DIR": str(home / "work")}))
        self.home = home
        self.pool = api.KeptAlive()
        self.addCleanup(self.pool.close)
        self.texts = texts_module.Texts(credential=lambda: "secret-1", require=lambda skill: {"plan": "beta"}, pool=self.pool)
        self.addCleanup(self.texts.close)

    def posts(self) -> list[dict]:
        return [r for r in self.fake.requests if r["path"] == "/v1/guide/batch"]


class OneRequestPerCall(TextsCase):
    def test_a_text_is_fetched_then_held_and_every_call_still_asks(self):
        first = self.texts.guide("deck-design", ["critique"], None)
        second = self.texts.guide("deck-design", ["critique"], None)
        self.assertEqual(first, second)
        self.assertIn("CRITIQUE TEXT", first[0])
        self.assertEqual(len(self.posts()), 2, "one request per call, the second conditional")
        self.assertEqual(self.fake.served["guide", "critique", 200], 1)
        self.assertEqual(self.fake.served["guide", "critique", 304], 1)
        self.assertEqual(self.pool.opened, 1, "both on one kept-alive connection")
        self.assertEqual(self.fake.batches[1][0]["etag"][:4], '"b1-')

    def test_several_sections_and_the_role_go_in_one_request(self):
        got = self.texts.guide("deck-design", [None, "critique"], "slide")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual([item["role"] for item in self.fake.batches[0]], ["slide", "slide"])
        self.assertIn("ENTRY", got[0])
        self.assertNotIn("LEAD ONLY", got[1])
        self.assertIn("LEAD ONLY", self.texts.guide("deck-design", ["critique"], None)[0], "the lead's text is held apart")

    def test_the_tool_and_the_launcher_wordings_are_held_apart(self):
        self.texts.guide("deck-design", ["critique"], None, bindings="tool")
        self.texts.guide("deck-design", ["critique"], None, bindings="launcher")
        self.assertEqual([batch[0]["bindings"] for batch in self.fake.batches], ["tool", "launcher"])
        self.assertNotIn("etag", self.fake.batches[1][0], "nothing held for the launcher's wording yet")
        self.texts.guide("deck-design", ["critique"], None, bindings="chat")      # the chat's tool wording is the tool's
        self.assertEqual(self.fake.batches[2][0]["bindings"], "tool")
        self.assertIn("etag", self.fake.batches[2][0])

    def test_a_changed_text_is_sent_again(self):
        self.texts.guide("deck-design", ["critique"], None)
        self.fake.guides["deck-design"]["critique"] = "# Critique\n\nNEW TEXT\n"
        self.assertIn("NEW TEXT", self.texts.guide("deck-design", ["critique"], None)[0])
        self.assertEqual(self.fake.served["guide", "critique", 200], 2)

    def test_an_unknown_section_names_the_sections_there_are(self):
        with self.assertRaises(core.Status) as caught:
            self.texts.guide("deck-design", ["nothing-here"], None)
        self.assertEqual(caught.exception.name, "USAGE")

    def test_concurrent_calls_all_answer(self):
        results, errors = [], []
        start = threading.Barrier(5)

        def one(section):
            start.wait()
            try:
                results.append(self.texts.guide("deck-design", [section], None)[0])
            except Exception as exc:        # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=one, args=(section,)) for section in (None, "critique", None, "critique", None)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 5)
        self.assertEqual(len(self.posts()), 5)


class Sheets(TextsCase):
    def test_sheets_come_in_one_request_and_a_held_one_is_confirmed(self):
        got = self.texts.specimens(["a.jpg", "b.jpg"])
        self.assertEqual([(name, raw, note) for name, raw, note in got], [("a.jpg", JPEG, None), ("b.jpg", JPEG + b"b", None)])
        self.assertEqual(len(self.posts()), 1)
        again = self.texts.specimens(["b.jpg", "a.jpg"])
        self.assertEqual([raw for _n, raw, _note in again], [JPEG + b"b", JPEG])
        self.assertEqual(len(self.posts()), 2, "every call sends its request")
        self.assertEqual(self.fake.served["specimen", "a.jpg", 304], 1)
        self.assertEqual(self.fake.served["specimen", "a.jpg", 200], 1)

    def test_a_missing_sheet_gets_a_note_and_the_others_come(self):
        got = self.texts.specimens(["a.jpg", "HC4.jpg"])
        self.assertEqual(got[0][1], JPEG)
        self.assertIsNone(got[1][1])
        self.assertIn("no such sheet: HC4.jpg", got[1][2])
        self.assertIn("HC3", got[1][2], "the nearest real names, from the index")

    def test_a_marked_sheet_is_kept_as_sent(self):
        self.fake.specimen_mark = b"token-1"
        raw = self.texts.specimens(["a.jpg"])[0][1]
        self.assertEqual(raw, mark_like_the_service(JPEG, b"token-1"))
        self.assertEqual(specimens.content_sha256(raw), sha256_hex(JPEG))

    def test_a_sheet_that_did_not_arrive_as_sent_is_refused(self):
        self.fake.specimen_sha = "0" * 64
        with self.assertRaises(core.Status) as caught:
            self.texts.specimens(["a.jpg"])
        self.assertEqual(caught.exception.name, "RETRY")


class SheetFiles(TextsCase):
    def test_files_go_in_the_sessions_private_folder_and_go_with_it(self):
        got = self.texts.sheet_files(["a.jpg", "b.jpg"], session="session-1")
        folder = self.home / "work" / "scratch" / "sheets" / "session-1"
        self.assertEqual([path.parent for _n, path, _note in got], [folder, folder])
        self.assertEqual(got[0][1].read_bytes(), JPEG)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(got[0][1]).st_mode), 0o600)
        self.assertTrue(got[0][1].name.endswith("-a.jpg"))
        again = self.texts.sheet_files(["a.jpg"], session="session-1")
        self.assertEqual(again[0][1], got[0][1], "the same sheet: the same file, after its request was answered 304")
        self.assertEqual(self.fake.served["specimen", "a.jpg", 304], 1)
        fresh = texts_module.Texts(credential=lambda: "secret-1", require=lambda skill: {}, pool=self.pool)
        self.assertEqual(fresh.sheet_files(["a.jpg"], session="session-1")[0][1], got[0][1],
                         "a process holding nothing in memory: the session's file is its held copy, confirmed by a 304")
        self.assertEqual(self.fake.served["specimen", "a.jpg", 304], 2)
        self.assertEqual(self.fake.served["specimen", "a.jpg", 200], 1)
        self.fake.specimens["a.jpg"] = JPEG + b"v2"
        changed = self.texts.sheet_files(["a.jpg"], session="session-1")[0][1]
        self.assertNotEqual(changed, got[0][1])
        self.assertEqual(changed.read_bytes(), JPEG + b"v2")
        self.texts.session_closed("session-1")
        self.assertFalse(folder.exists())

    def test_files_older_than_15_minutes_go(self):
        old = self.texts.sheet_files(["a.jpg"], session="s")[0][1]
        fresh = self.texts.sheet_files(["b.jpg"], session="s")[0][1]
        past = time.time() - texts_module.SHEET_FILES_KEEP_S - 5
        os.utime(old, (past, past))
        self.texts.sweep()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_never_in_the_working_folder(self):
        work = Path(tempfile.mkdtemp(prefix="theodore-cwd-"))
        self.addCleanup(shutil.rmtree, work, True)
        before = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, before)
        path = self.texts.sheet_files(["a.jpg"], session=None)[0][1]
        self.assertNotIn(work.resolve(), path.resolve().parents)
        self.assertEqual(path.parent.name, "shell")


class Refusals(TextsCase):
    def test_a_revoked_device_gets_nothing_held(self):
        self.texts.guide("deck-design", ["critique"], None)
        self.fake.installations["iid-1"]["revoked"] = True
        with mock.patch.object(texts_module.credentials, "delete"), mock.patch.object(texts_module.account, "delete_token"):
            with self.assertRaises(core.Status) as caught:
                self.texts.guide("deck-design", ["critique"], None)
        self.assertIn(caught.exception.name, ("SIGN_IN_REQUIRED", "SIGN_IN_PENDING"))
        self.assertEqual(len(self.texts._held), 0, "nothing held after a refusal")

    def test_offline_serves_nothing_from_memory(self):
        self.texts.guide("deck-design", ["critique"], None)
        self.fake.down = True
        with self.assertRaises(core.Status) as caught:
            self.texts.guide("deck-design", ["critique"], None)
        self.assertEqual(caught.exception.name, "OFFLINE")

    def test_a_skill_the_plan_lacks_is_refused_as_on_the_single_routes(self):
        self.fake.features = ["pptx"]
        with mock.patch.object(texts_module.account, "delete_token"), mock.patch.object(api, "_signed_out"):
            with self.assertRaises(core.Status) as caught:
                self.texts.guide("deck-design", ["critique"], None)
        self.assertEqual(caught.exception.name, "NO_ENTITLEMENT")


class OlderService(TextsCase):
    def test_without_the_batch_route_the_single_routes_answer_and_are_kept_for_a_while(self):
        self.fake.batch_route = False
        first = self.texts.guide("deck-design", ["critique"], None)
        self.assertIn("CRITIQUE TEXT", first[0])
        self.texts.guide("deck-design", ["critique"], None)
        self.assertEqual(len(self.posts()), 1, "the batch route is asked once, then left for a while")
        self.assertEqual(self.fake.served["guide", "critique", 304], 1, "the single route's own ETag, conditional")
        got = self.texts.specimens(["a.jpg", "b.jpg", "missing.jpg"])
        self.assertEqual([raw is not None for _n, raw, _note in got], [True, True, False])
        self.assertEqual(self.texts.specimens(["a.jpg"])[0][1], JPEG)
        self.assertEqual(self.fake.served["specimen", "a.jpg", 304], 1)


class Index(TextsCase):
    def test_the_index_is_one_request_and_held_for_a_tool_list(self):
        self.assertIsNone(self.texts.index("deck-design", held_only=True))
        index = self.texts.index("deck-design")
        self.assertEqual(index["sections"], ["critique"])
        self.assertEqual(self.texts.index("deck-design", held_only=True)["specimens"], ["a.jpg", "b.jpg", "HC3.jpg"])
        self.texts.index("deck-design")
        self.assertEqual(self.fake.served["index", "", 304], 1)

    def test_the_first_names_and_changed_names_notify_the_relays(self):
        told = []
        texts = texts_module.Texts(credential=lambda: "secret-1", require=lambda skill: {}, notify=lambda: told.append(1), pool=self.pool)
        texts.index("deck-design")
        self.assertEqual(told, [1], "the names arrived: the relays learn them")
        texts.index("deck-design")
        self.assertEqual(told, [1], "the same names: nothing to say")
        self.fake.guide_index = {**self.fake.guide_index, "sections": [{"name": "critique"}, {"name": "craft"}]}
        texts.index("deck-design")
        self.assertEqual(told, [1, 1])


class SkillBodies(TextsCase):
    def test_the_deck_design_body_is_one_request_with_the_hosts_wording(self):
        with mock.patch.object(texts_module.install, "take_announcement", return_value=None), \
                mock.patch.object(texts_module.install, "plugin_notice", return_value=None), \
                mock.patch.object(texts_module.commands.account, "require", return_value={"plan": "beta"}):
            body = self.texts.skill_body("deck-design", host="claude-code")
        self.assertIn("ENTRY", body)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.fake.batches[0][0]["bindings"], "launcher" if core.IS_WINDOWS else "tool")


if __name__ == "__main__":
    unittest.main()
