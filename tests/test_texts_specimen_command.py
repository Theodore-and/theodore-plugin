"""`theodore specimen` against the fake API (moved out of test_client.py, which is at the host limit of 256 KiB a file):
sheets online on every call (contract C6), written as transient files into the session's own folder."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"specimen sheet " * 40 + b"\xff\xd9"


class Specimen(ClientCase):
    """`theodore specimen <file>...`: deck-design sheets come online in one request per call (contract C6, 24 November
    2026) and are written as transient files into this session's private folder for sheets
    (<home>/work/scratch/sheets/<session>/), each named by its ETag: a later call answered 304 prints the same file, a
    changed sheet is a new one, and files go 15 minutes after their last use, when the session ends, or at once after a
    refusal of the device or the account. Never in the deck's folder, never in the system's temporary folder."""

    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {"cover-01.jpg": JPEG, "Grid_2.b.jpg": JPEG + b"2"}
        self.temp = Path(self.box.env["TMPDIR"])
        self.sheets = self.box.home / "work" / "scratch" / "sheets"
        self.deck = self.box.tmp / "deck"
        (self.deck / ".pptx-state").mkdir(parents=True)
        self.work = self.box.tmp / "work"           # a working folder that does not contain the temporary folder
        self.work.mkdir()

    def client(self, *args, **kwargs):
        kwargs.setdefault("cwd", getattr(self, "work", None))
        return super().client(*args, **kwargs)

    def files(self) -> list[Path]:
        return sorted(self.sheets.rglob("*.jpg")) if self.sheets.is_dir() else []

    def old_folders(self, root: Path | None = None) -> list[Path]:
        """Folders of the kind clients up to 0.4.33 made: only ever swept now."""
        root = root or self.temp
        return sorted(p for p in root.iterdir() if p.name.startswith("theodore-specimens-")) if root.is_dir() else []

    def sheet_items(self) -> list[list[str]]:
        return [[item["file"] for item in batch if item["type"] == "sheet"] for batch in self.api.batches]

    def refused(self, result, name: str, code: int):
        before = self.files()
        first, nxt, tell = self.assertStatus(result, name, code)
        self.assertLessEqual(len(self.files()), len(before), "nothing is written when a sheet is refused")
        return first

    def test_sheets_land_in_the_sessions_private_folder_outside_the_deck(self):
        result = self.client("specimen", "cover-01.jpg", "Grid_2.b.jpg", cwd=self.deck)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout)
        one, two = Path(lines[0]), Path(lines[1])
        self.assertTrue(one.is_absolute() and two.is_absolute())
        self.assertTrue(one.name.endswith("-cover-01.jpg") and two.name.endswith("-Grid_2.b.jpg"), (one.name, two.name))
        self.assertEqual((one.read_bytes(), two.read_bytes()), (JPEG, JPEG + b"2"))
        self.assertEqual(one.parent, two.parent)
        self.assertEqual(one.parent, (self.sheets / "shell").resolve(), "the folder of a shell with no session")
        if sys.platform == "win32":     # no POSIX modes: this user's access list alone, nothing inherited (specimens._make_private)
            for target in (one.parent, one, two):
                acl = subprocess.run(["icacls", str(target)], capture_output=True, text=True).stdout
                for other in ("BUILTIN\\Users", "Everyone", "Authenticated Users", "BUILTIN\\Administrators", "NT AUTHORITY\\SYSTEM"):
                    self.assertNotIn(other, acl, acl)
                self.assertEqual(len([line for line in acl.splitlines() if ":(" in line]), 1, acl)
            self.assertNotIn("(I)", subprocess.run(["icacls", str(one.parent)], capture_output=True, text=True).stdout)
        else:
            self.assertEqual(stat.S_IMODE(one.parent.stat().st_mode), 0o700)
            self.assertEqual({stat.S_IMODE(p.stat().st_mode) for p in (one, two)}, {0o600})
        for kept_out in (self.deck, self.temp):
            self.assertNotIn(kept_out.resolve(), one.resolve().parents)
            self.assertEqual(list(kept_out.rglob("*.jpg")), [])
        self.assertEqual(self.sheet_items(), [["cover-01.jpg", "Grid_2.b.jpg"]], "one request for the call")
        self.assertEqual([r["auth"] for r in self.api.requests if r["path"] == "/v1/guide/batch"], [True])
        again = self.client("specimen", "cover-01.jpg", cwd=self.deck)
        self.assertEqual(Path(again.stdout.strip()), one, "the session's file of the sheet, printed again after the check")
        self.assertEqual(len(self.api.batches), 2, "asked again, conditionally")
        self.assertEqual(self.api.served["specimen", "cover-01.jpg", 304], 1)
        self.assertEqual(self.files(), sorted([one, two]), "nothing new written")

    def test_a_session_has_its_own_folder_and_it_goes_with_the_session(self):
        mine = Path(self.client("specimen", "cover-01.jpg", env={"CLAUDE_CODE_SESSION_ID": "session-a"}).stdout.strip())
        theirs = Path(self.client("specimen", "cover-01.jpg", env={"CLAUDE_CODE_SESSION_ID": "session-b"}).stdout.strip())
        self.assertEqual((mine.parent.name, theirs.parent.name), ("session-a", "session-b"))
        self.assertEqual(self.api.served["specimen", "cover-01.jpg", 200], 2, "another session holds nothing of this one's")

    def test_a_bare_code_names_its_close_up_and_shares_its_held_copy(self):
        self.api.specimens["HC3.jpg"] = JPEG + b"HC3"
        result = self.client("specimen", "HC3", "cover-01.jpg")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        code, cover = (Path(line) for line in result.stdout.splitlines())
        self.assertTrue(code.name.endswith("-HC3.jpg"))
        self.assertEqual(code.read_bytes(), JPEG + b"HC3")
        again = self.client("specimen", "HC3.jpg")
        self.assertEqual(Path(again.stdout.strip()), code, "the code and its file name are one held sheet")
        self.assertEqual(self.api.served["specimen", "HC3.jpg", 200], 1)
        self.assertEqual(self.api.served["specimen", "HC3.jpg", 304], 1)
        self.assertEqual(self.sheet_items()[0], ["HC3.jpg", "cover-01.jpg"])
        for wrong in ("hc3", "HC3456", "H3"):
            self.assertStatus(self.client("specimen", wrong), "USAGE", 64)

    def test_reuse_one_fetch_per_distinct_sheet_across_calls(self):
        names = [f"s{i}.jpg" for i in range(10)]
        self.api.specimens = {name: JPEG + name.encode() for name in names}
        calls = [names[0:6], names[3:9], names[5:10] + names[0:2], names[1:7], names[::2], names[4:10]]
        printed = {}
        for asked in calls:          # six callers, each in a process of its own
            result = self.client("specimen", *asked[:8])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name, line in zip(asked, result.stdout.splitlines()):
                self.assertEqual(Path(line).read_bytes(), JPEG + name.encode())
                printed.setdefault(name, set()).add(line)
        for name in names:
            self.assertEqual(self.api.served["specimen", name, 200], 1, name)
            self.assertEqual(len(printed[name]), 1, "the same file each time")
        self.assertEqual(sum(v for (route, _n, status), v in self.api.served.items() if route == "specimen" and status == 304),
                         sum(len(set(c)) for c in calls) - len(names))
        self.assertEqual(len(self.api.batches), len(calls), "one request per call")

    def test_a_changed_sheet_an_expired_file_and_an_older_service(self):
        first = Path(self.client("specimen", "cover-01.jpg").stdout.strip())
        self.api.specimens["cover-01.jpg"] = JPEG + b"v2"            # a new version: the held copy no longer matches
        second = Path(self.client("specimen", "cover-01.jpg").stdout.strip())
        self.assertNotEqual(second, first)
        self.assertEqual(second.read_bytes(), JPEG + b"v2")
        self.assertEqual(self.api.served["specimen", "cover-01.jpg", 200], 2)
        later = Path(self.client("specimen", "cover-01.jpg", env={"THEODORE_TEST_CLOCK_OFFSET": "960"}).stdout.strip())
        self.assertEqual(self.api.served["specimen", "cover-01.jpg", 200], 3, "16 minutes on, the file is gone and fetched again")
        self.assertEqual(later.read_bytes(), JPEG + b"v2")
        self.assertFalse(first.exists(), "the file of the old version went with the 15 minutes")
        self.api.batch_route, self.api.specimen_etag = False, False     # an older service: no batch, no ETag, no 304
        older = Path(self.client("specimen", "Grid_2.b.jpg").stdout.strip())
        again = Path(self.client("specimen", "Grid_2.b.jpg").stdout.strip())
        self.assertEqual(again.read_bytes(), JPEG + b"2")
        self.assertEqual(older, again, "the same bytes: the same file")
        self.assertEqual(self.api.served["specimen", "Grid_2.b.jpg", 200], 2, "asked each time, answered whole each time")

    def test_a_refusal_removes_every_held_sheet(self):
        held = Path(self.client("specimen", "cover-01.jpg", "Grid_2.b.jpg").stdout.splitlines()[0])
        self.assertTrue(held.exists())
        for inst in self.api.installations.values():
            inst["revoked"] = True
        self.assertStatus(self.client("specimen", "cover-01.jpg"), "SIGN_IN_REQUIRED", 65)
        self.assertFalse(held.exists(), "a revoked device keeps no sheet on disk")
        self.assertEqual(self.files(), [])

    def test_any_command_that_learns_of_a_refusal_removes_every_held_sheet(self):
        """Not only `specimen`: whatever command hears that the device is revoked, the account suspended, or that the
        channel is switched off, no sheet stays on this computer."""
        for refuse, command in (("revoked", ("guide", "deck-design")), ("suspended", ("guide", "deck-design")), ("kill", ("guide", "deck-design"))):
            held = Path(self.client("specimen", "cover-01.jpg").stdout.strip())
            self.assertTrue(held.exists(), refuse)
            if refuse == "revoked":
                for inst in self.api.installations.values():
                    inst["revoked"] = True
            elif refuse == "suspended":
                self.api.account_state = "suspended"
            else:
                self.api.guide_kill = True
            self.client(*command)
            self.assertFalse(held.exists(), f"{refuse}: no sheet kept after `{' '.join(command)}`")
            self.assertEqual(self.files(), [], refuse)
            for inst in self.api.installations.values():
                inst["revoked"] = False
            self.api.account_state, self.api.guide_kill = "active", False
            self.sign_in()

    def test_files_go_15_minutes_after_their_last_use_with_any_command(self):
        held = Path(self.client("specimen", "cover-01.jpg").stdout.strip())
        self.client("version", env={"THEODORE_TEST_CLOCK_OFFSET": "600"})
        self.assertTrue(held.exists(), "ten minutes on, still there")
        self.client("version", env={"THEODORE_TEST_CLOCK_OFFSET": "960"})
        self.assertFalse(held.exists(), "16 minutes on, any launcher command removes it")

    def test_the_temporary_folder_is_never_used(self):
        for inside in (self.deck / "tmp", self.box.home / "tmp"):
            inside.mkdir(parents=True)
            result = self.client("specimen", "cover-01.jpg", cwd=self.deck, env={"TMPDIR": str(inside)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            path = Path(result.stdout.strip())
            self.assertEqual(path.read_bytes(), JPEG)
            self.assertEqual(path.parent.parent, self.sheets.resolve())
            self.assertEqual(list(inside.iterdir()), [])
            self.assertEqual(list(self.deck.rglob("*.jpg")), [])

    def test_a_sheet_whose_sha256_does_not_match_is_refused(self):
        self.api.specimen_sha = "0" * 64
        first = self.refused(self.client("specimen", "cover-01.jpg"), "RETRY", 75)
        self.assertIn("sha256", first)
        self.api.specimen_sha = None
        self.api.batch_route = False                 # the single route of an older service: its X-Theodore-Sha256 header
        self.api.specimens["second.jpg"] = JPEG
        real = self.api._send

        def no_header(h, status, body=None, headers=None, content_type="application/json"):
            headers = {k: v for k, v in (headers or {}).items() if k != "X-Theodore-Sha256"}
            return real(h, status, body, headers, content_type)
        self.api._send = no_header
        self.refused(self.client("specimen", "cover-01.jpg"), "RETRY", 75)
        self.api._send = real
        self.assertEqual(self.client("specimen", "cover-01.jpg", "second.jpg").returncode, 0)

    def test_a_name_that_is_not_a_sheet_does_not_cost_the_good_one(self):
        # until 0.4.30 one name the service has no sheet for failed the whole call (tests/test_specimen_names_not_found.py)
        result = self.client("specimen", "cover-01.jpg", "absent.jpg")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path, note = result.stdout.splitlines()
        self.assertEqual(Path(path).read_bytes(), JPEG)
        self.assertTrue(note.startswith("no such sheet: absent.jpg"), note)

    def test_a_sheet_over_one_mebibyte_or_of_another_type_is_refused(self):
        self.api.specimens["big.jpg"] = b"\xff\xd8" + b"x" * (1 << 20)
        first = self.refused(self.client("specimen", "big.jpg"), "INTERNAL", 78)
        self.assertIn("1 MiB", first)
        self.api.specimens["edge.jpg"] = b"\xff\xd8" + b"x" * ((1 << 20) - 2)
        self.assertEqual(self.client("specimen", "edge.jpg").returncode, 0)
        self.api.specimen_type = "text/html"
        self.refused(self.client("specimen", "cover-01.jpg"), "INTERNAL", 78)
        self.assertEqual([p.name.split("-", 1)[1] for p in self.files()], ["edge.jpg"])

    def test_bad_names_and_counts_are_refused_before_any_request(self):
        before = len(self.api.requests)
        for names in (["../cover-01.jpg"], ["a/b.jpg"], ["a\\b.jpg"], ["cover-01.png"], ["cover-01.JPG"], ["cover 01.jpg"], [".jpg"],
                      ["x" * 81 + ".jpg"], ["NUL.jpg"], ["com1.x.jpg"], ["cover-01.jpg", "bad name.jpg"], ["%2e%2e.jpg"], ["é.jpg"],
                      [f"s{i}.jpg" for i in range(9)], []):
            self.refused(self.client("specimen", *names), "USAGE", 64)
        self.assertEqual(len(self.api.requests), before, "no request at all for a bad name")
        self.api.specimens["x" * 80 + ".jpg"] = JPEG
        self.assertEqual(self.client("specimen", "x" * 80 + ".jpg").returncode, 0)

    def test_server_refusals_become_the_usual_statuses(self):
        self.refused(self.client("specimen", "nosuch.jpg"), "USAGE", 64)                         # 404 not_found
        self.api.fail_with = (429, "rate_limited")
        _first, nxt, _tell = self.assertStatus(self.client("specimen", "cover-01.jpg"), "RETRY", 75)
        self.assertIn("wait 7 seconds", nxt)
        self.api.fail_with = None
        self.api.features = ["pptx"]                                                              # 403: the plan lost deck-design
        self.refused(self.client("specimen", "cover-01.jpg"), "NO_ENTITLEMENT", 70)
        self.api.features = ["pptx", "deck-design"]
        self.sign_in_again()
        self.assertEqual(self.client("specimen", "cover-01.jpg").returncode, 0)
        self.api.account_state = "suspended"
        self.refused(self.client("specimen", "cover-01.jpg"), "NO_ENTITLEMENT", 70)
        self.api.account_state = "active"
        for inst in self.api.installations.values():
            inst["revoked"] = True
        self.refused(self.client("specimen", "cover-01.jpg"), "SIGN_IN_REQUIRED", 65)    # 401

    def sign_in_again(self):
        self.client("logout")
        self.assertStatus(self.client("login", "--code", self.api.issue_activation_code("EFGH-5678")), "OK", 0)

    def test_not_signed_in_and_no_redirect(self):
        self.api.batch_route = False                 # a redirect can only come from a single route
        self.api.specimen_redirect = f"{self.api.base}/dl/elsewhere.jpg"
        self.api.files["elsewhere.jpg"] = JPEG
        self.refused(self.client("specimen", "cover-01.jpg"), "OFFLINE", 71)
        self.assertEqual([r for r in self.api.requests if "elsewhere" in r["path"]], [], "the redirect was not followed")
        self.api.specimen_redirect = None
        self.client("logout")
        self.refused(self.client("specimen", "cover-01.jpg"), "SIGN_IN_REQUIRED", 65)

    def test_every_call_removes_specimen_folders_older_than_an_hour(self):
        # Folders of the kind clients up to 0.4.33 made: swept by any command for an hour after the update.
        now = time.time()
        old, fresh, ahead = (self.temp / f"theodore-specimens-{n}" for n in ("old", "fresh", "ahead"))
        for folder, age in ((old, 3601), (fresh, 3300), (ahead, -7200)):
            folder.mkdir()
            (folder / "cover-01.jpg").write_bytes(JPEG)
            os.utime(folder, (now - age, now - age))
        plain = self.temp / "theodore-specimens-file"
        plain.write_bytes(b"not a folder")
        elsewhere = self.box.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep.txt").write_text("keep")
        os.symlink(elsewhere, self.temp / "theodore-specimens-link")
        other = self.temp / "another-program"
        other.mkdir()
        for path in (plain, self.temp / "theodore-specimens-link", other):
            os.utime(path, (now - 99999, now - 99999), follow_symlinks=False)
        self.assertEqual(self.client("version").returncode, 0)
        self.assertFalse(old.exists(), "older than 60 minutes: removed by the next call, whatever the command")
        self.assertFalse(ahead.exists(), "dated ahead of the clock: not trusted to be recent")
        self.assertTrue(fresh.exists())
        self.assertTrue(plain.exists() and other.exists() and (elsewhere / "keep.txt").exists(), "only Theodore's own folders")

    def test_uninstall_removes_every_sheet(self):
        path = Path(self.client("specimen", "cover-01.jpg").stdout.strip())
        self.assertTrue(path.exists())
        self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertFalse(path.exists())
        self.assertEqual(self.old_folders(), [])

    def test_a_folder_uninstall_could_not_remove_keeps_its_record(self):
        # The record of a folder an older client made that could not be removed survives the removal of state/, so a
        # later uninstall finds it although TMPDIR changed.
        leftover = self.temp / "theodore-specimens-0123456789abcdef"
        leftover.mkdir(mode=0o700)
        (leftover / "cover-01.jpg").write_bytes(JPEG)
        record = self.box.home / "state" / "specimens.txt"
        record.write_text(f"#v2\n{int(time.time())} {leftover}\n")
        self.temp.chmod(0o555)                     # the folder cannot be removed from its parent
        self.addCleanup(lambda: self.temp.chmod(0o755))
        other = self.box.tmp / "another-tmp"
        other.mkdir()
        first, _n, _t = self.assertStatus(self.client("uninstall", env={"TMPDIR": str(other)}), "RETRY", 75)
        self.assertIn(str(leftover), first)
        self.assertTrue(record.exists(), "the record of what is left survives")
        self.assertIn(str(leftover), record.read_text())
        self.assertEqual(sorted(p.name for p in (self.box.home / "state").iterdir()), ["specimens.txt"])
        self.temp.chmod(0o755)
        self.assertStatus(self.box.run_launcher("uninstall", env={"TMPDIR": str(other)}), "OK", 0)
        self.assertFalse(leftover.exists(), "found again from the record and removed")
        self.assertFalse(self.box.home.exists())

    def test_the_guide_names_the_sheets_and_says_how_to_look_at_one(self):
        text = "# Covers\n\n![Cover](specimens/cover-01.jpg) and [the grid](specimens/Grid_2.b.jpg)\n\n## Next\n\nMore.\n"
        self.api.guides["deck-design"]["covers"] = text
        result = self.client("guide", "deck-design", "covers")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launcher = f'"{self.box.home / "bin" / "theodore"}"'
        lines = result.stdout.splitlines()
        self.assertIn("![Cover](cover-01.jpg) and [the grid](Grid_2.b.jpg)", lines)
        self.assertNotIn("](specimens/", result.stdout)
        direction = [line for line in lines if "specimen <file>" in line]
        self.assertEqual(len(direction), 1, result.stdout)
        self.assertIn(f"{launcher} specimen <file>", direction[0])
        self.assertGreater(lines.index(direction[0]), lines.index("More."), "after the section")
        body = "\n".join(lines[lines.index("--- covers ---") + 1:-1]) + "\n"
        self.assertIn(f"({len(body)} chars, sha256 {hashlib.sha256(body.encode()).hexdigest()})", lines[-1], "the end line describes what was printed")
        plain = self.client("guide", "deck-design")
        self.assertNotIn("specimen <file>", plain.stdout, "a text without sheet links gets no direction")
        self.assertEqual(self.files(), [], "the guide fetches no sheet")
        self.assertEqual([items for items in self.sheet_items() if items], [])


if __name__ == "__main__":
    import unittest
    unittest.main()
