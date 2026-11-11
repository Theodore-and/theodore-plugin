"""The client against the fake API: every command, every status, exact exit codes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import RUNTIME_ID, SITE, ClientCase, Sandbox, engine_zip, pack_zip, status_lines

DAY = 86400


class SignIn(ClientCase):
    def test_activation_code_signs_in_and_is_never_echoed(self):
        code = self.api.issue_activation_code("QWER-5678")
        result = self.client("login", "--code", code)
        self.assertStatus(result, "OK", 0)
        self.assertNotIn(code, result.stdout + result.stderr)
        cred = self.box.home / "state" / "credentials"
        self.assertEqual(stat.S_IMODE(cred.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.box.home / "state" / "entitlement.json").stat().st_mode), 0o600)
        token = self.state("state/entitlement.json")["entitlement_token"]
        self.assertEqual(token.count("."), 2)
        secret = json.loads(cred.read_text())["installation_secret"]
        log = (self.box.home / "logs" / "client.log")
        self.assertNotIn(secret, log.read_text() if log.exists() else "")
        first, _next, _tell = self.assertStatus(self.client("whoami"), "OK", 0)
        self.assertIn("plan beta", first)
        self.assertNotIn(secret, first)

    def test_used_or_unknown_code_is_one_answer(self):
        self.api.issue_activation_code("AAAA-1111")
        self.assertStatus(self.client("login", "--code", "AAAA-1111"), "OK", 0)
        self.client("logout")
        for code in ("AAAA-1111", "ZZZZ-9999"):
            _first, nxt, _tell = self.assertStatus(self.client("login", "--code", code), "SIGN_IN_REQUIRED", 65)
            self.assertIn("login --start", nxt)

    def test_malformed_code_is_usage(self):
        self.assertStatus(self.client("login", "--code", "nope"), "USAGE", 64)

    def test_browser_approval_start_pending_finish(self):
        result = self.client("login", "--start")
        _first, nxt, tell = self.assertStatus(result, "SIGN_IN_REQUIRED", 65)
        self.assertRegex(nxt, r"https://theodoreand\.ai/device\?code=WXYZ-[0-9A-HJKMNP-TV-Z]{4}")
        self.assertIn("login --finish --wait 90", nxt)
        self.assertIn("https://theodoreand.ai/device?code=", tell)
        again = self.client("login", "--start")          # the pending approval is reused, not replaced
        self.assertEqual(status_lines(again.stdout)[1], nxt)
        self.assertEqual(len(self.api.devices), 1)
        self.assertStatus(self.client("login", "--finish", "--wait", "0.5"), "SIGN_IN_PENDING", 66)
        self.api.approve_device()
        self.assertStatus(self.client("login", "--finish", "--wait", "5"), "OK", 0)
        self.assertFalse((self.box.home / "state" / "device.json").exists())

    def test_denied_approval_starts_over(self):
        self.client("login", "--start")
        self.api.approve_device(deny=True)
        first, _n, _t = self.assertStatus(self.client("login", "--finish", "--wait", "1"), "SIGN_IN_REQUIRED", 65)
        self.assertIn("declined", first)

    def test_logout_removes_credential_and_token(self):
        self.sign_in()
        self.assertStatus(self.client("logout"), "OK", 0)
        self.assertFalse((self.box.home / "state" / "credentials").exists())
        self.assertFalse((self.box.home / "state" / "entitlement.json").exists())

    def test_pending_activation_from_the_installer_is_imported(self):
        self.api.issue_activation_code("ABCD-1234")
        import urllib.request
        req = urllib.request.Request(self.api.base + "/v1/device/activate", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"code": "ABCD-1234", "device_name": "t", "platform": "darwin", "arch": "arm64",
                                                      "host": "installer", "client_version": "0.2.0"}).encode())
        answer = urllib.request.urlopen(req).read()
        pending = self.box.home / "state" / "activation.pending"
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_bytes(answer)
        self.assertStatus(self.client("whoami"), "OK", 0)
        self.assertFalse(pending.exists())
        self.assertTrue((self.box.home / "state" / "credentials").exists())

    def test_connector_grant_with_entitlement_only(self):
        token = self.api.mint_entitlement("inst-connector")
        import subprocess as sp
        piped = sp.run([sys.executable, "-I", str(Path(__file__).resolve().parents[1] / "client"), "activate", "--grant", "-"],
                       input=token + "\n", env=self.box.env, capture_output=True, text=True, timeout=60)
        self.assertStatus(piped, "OK", 0)
        grant = self.box.tmp / "grant.txt"
        grant.write_text(token)
        grant.chmod(0o600)
        self.assertStatus(self.client("activate", "--grant-file", str(grant)), "OK", 0)
        grant.chmod(0o644)                                       # readable by others: refused
        first, _n, _t = self.assertStatus(self.client("activate", "--grant-file", str(grant)), "USAGE", 64)
        self.assertIn("0600", first)
        # never on the command line: the bundle can carry the installation secret
        first, _n, _t = self.assertStatus(self.client("activate", "--grant", token), "USAGE", 64)
        self.assertIn("command line", first)
        self.assertNotIn(token, first)
        grant.write_text("garbage")
        grant.chmod(0o600)
        self.assertStatus(self.client("activate", "--grant-file", str(grant)), "USAGE", 64)

    @unittest.skipUnless(sys.platform == "darwin", "macOS keychain only")
    def test_keychain_backend_in_a_scratch_keychain(self):
        keychain = self.box.tmp / "test.keychain-db"
        subprocess.run(["/usr/bin/security", "create-keychain", "-p", "", str(keychain)], check=True, capture_output=True)
        self.addCleanup(lambda: subprocess.run(["/usr/bin/security", "delete-keychain", str(keychain)], capture_output=True))
        env = {"THEODORE_CREDENTIAL_STORE": "keychain", "THEODORE_TEST_KEYCHAIN": str(keychain)}
        self.api.issue_activation_code("KEYC-0001")
        self.assertStatus(self.client("login", "--code", "KEYC-0001", env=env), "OK", 0)
        self.assertFalse((self.box.home / "state" / "credentials").exists(), "with a keychain no secret file is written")
        first, _n, _t = self.assertStatus(self.client("whoami", env=env), "OK", 0)
        self.assertIn("macOS keychain", first)
        self.assertEqual(self.client("guide", "deck-design", env=env).returncode, 0)
        self.assertStatus(self.client("logout", env=env), "OK", 0)
        self.assertStatus(self.client("whoami", env=env), "SIGN_IN_REQUIRED", 65)


JPEG = b"\xff\xd8\xff\xe0" + b"specimen sheet " * 40 + b"\xff\xd9"


class Specimen(ClientCase):
    """`theodore specimen <file>...`: a deck-design sheet is fetched on demand into a private temporary folder, kept
    for at most an hour, and never put in Theodore's folder or in the deck's."""

    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {"cover-01.jpg": JPEG, "Grid_2.b.jpg": JPEG + b"2"}
        self.temp = Path(self.box.env["TMPDIR"])
        self.deck = self.box.tmp / "deck"
        (self.deck / ".pptx-state").mkdir(parents=True)
        self.work = self.box.tmp / "work"           # a working folder that does not contain the temporary folder
        self.work.mkdir()

    def client(self, *args, **kwargs):
        kwargs.setdefault("cwd", getattr(self, "work", None))
        return super().client(*args, **kwargs)

    def folders(self, root: Path | None = None) -> list[Path]:
        return sorted(p for p in (root or self.temp).iterdir() if p.name.startswith("theodore-specimens-"))

    def specimen_requests(self) -> list[dict]:
        return [r for r in self.api.requests if "/guide/specimens/" in r["path"]]

    def refused(self, result, name: str, code: int):
        first, nxt, tell = self.assertStatus(result, name, code)
        self.assertEqual(self.folders(), [], "nothing is kept when a sheet is refused")
        return first

    def test_sheets_land_in_a_private_folder_outside_theodore_and_the_deck(self):
        result = self.client("specimen", "cover-01.jpg", "Grid_2.b.jpg", cwd=self.deck)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout)
        one, two = Path(lines[0]), Path(lines[1])
        self.assertTrue(one.is_absolute() and two.is_absolute())
        self.assertEqual((one.name, two.name), ("cover-01.jpg", "Grid_2.b.jpg"))
        self.assertEqual((one.read_bytes(), two.read_bytes()), (JPEG, JPEG + b"2"))
        self.assertEqual(one.parent, two.parent, "one folder per run")
        self.assertEqual(self.folders(), [one.parent])
        self.assertEqual(stat.S_IMODE(one.parent.stat().st_mode), 0o700)
        self.assertEqual({stat.S_IMODE(p.stat().st_mode) for p in (one, two)}, {0o600})
        for kept_out in (self.box.home, self.deck):
            self.assertNotIn(kept_out.resolve(), one.resolve().parents)
            self.assertEqual([p for p in kept_out.rglob("*") if p.is_file() and b"specimen sheet" in p.read_bytes()], [])
            self.assertEqual([p for p in kept_out.rglob("*.jpg")], [])
        asked = self.specimen_requests()
        self.assertEqual([(r["method"], r["path"], r["auth"], r["query"]) for r in asked],
                         [("GET", "/v1/guide/specimens/cover-01.jpg", True, {"skill": "deck-design"}),
                          ("GET", "/v1/guide/specimens/Grid_2.b.jpg", True, {"skill": "deck-design"})])
        again = self.client("specimen", "cover-01.jpg", cwd=self.deck)
        self.assertNotEqual(Path(again.stdout.strip()).parent, one.parent, "a folder per run, nothing is reused")
        self.assertEqual(len(self.specimen_requests()), 3, "nothing is cached: asked again")

    def test_a_temporary_folder_inside_the_deck_or_inside_theodore_is_not_used(self):
        for inside in (self.deck / "tmp", self.box.home / "tmp"):
            inside.mkdir(parents=True)
            result = self.client("specimen", "cover-01.jpg", cwd=self.deck, env={"TMPDIR": str(inside)})
            if result.returncode == 0:      # another temporary folder of the system was used
                path = Path(result.stdout.strip())
                self.addCleanup(shutil.rmtree, path.parent, True)
                self.assertEqual(path.read_bytes(), JPEG)
                self.assertNotIn(self.deck.resolve(), path.resolve().parents)
                self.assertNotIn(self.box.home.resolve(), path.resolve().parents)
            else:
                self.assertStatus(result, "BLOCKED_BY_POLICY", 73)
            self.assertEqual(self.folders(inside), [])
            self.assertEqual(list(self.deck.rglob("*.jpg")) + list(self.box.home.rglob("*.jpg")), [])

    def test_a_sheet_whose_sha256_does_not_match_is_refused(self):
        self.api.specimen_sha = "0" * 64
        first = self.refused(self.client("specimen", "cover-01.jpg"), "RETRY", 75)
        self.assertIn("sha256", first)
        self.api.specimen_sha = None
        self.api.specimens["second.jpg"] = JPEG
        real = self.api._send

        def no_header(h, status, body=None, headers=None, content_type="application/json"):
            headers = {k: v for k, v in (headers or {}).items() if k != "X-Theodore-Sha256"}
            return real(h, status, body, headers, content_type)
        self.api._send = no_header
        self.refused(self.client("specimen", "cover-01.jpg"), "RETRY", 75)
        self.api._send = real
        self.assertEqual(self.client("specimen", "cover-01.jpg", "second.jpg").returncode, 0)

    def test_the_first_good_sheet_is_not_kept_when_a_later_one_is_refused(self):
        result = self.client("specimen", "cover-01.jpg", "absent.jpg")
        self.refused(result, "USAGE", 64)
        self.assertNotIn(str(self.temp), result.stdout.split("THEODORE")[0], "no path is printed")

    def test_a_sheet_over_one_mebibyte_or_of_another_type_is_refused(self):
        self.api.specimens["big.jpg"] = b"\xff\xd8" + b"x" * (1 << 20)
        first = self.refused(self.client("specimen", "big.jpg"), "INTERNAL", 78)
        self.assertIn("1 MiB", first)
        self.api.specimens["edge.jpg"] = b"\xff\xd8" + b"x" * ((1 << 20) - 2)
        self.assertEqual(self.client("specimen", "edge.jpg").returncode, 0)
        self.api.specimen_type = "text/html"
        self.refused(self.client("specimen", "cover-01.jpg", env={"TMPDIR": str(self.fresh_temp())}), "INTERNAL", 78)

    def fresh_temp(self) -> Path:
        self.temp = self.box.tmp / "tmp2"
        self.temp.mkdir(exist_ok=True)
        return self.temp

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
        self.fresh_temp()
        self.api.account_state = "suspended"
        self.refused(self.client("specimen", "cover-01.jpg", env={"TMPDIR": str(self.temp)}), "NO_ENTITLEMENT", 70)
        self.api.account_state = "active"
        for inst in self.api.installations.values():
            inst["revoked"] = True
        self.refused(self.client("specimen", "cover-01.jpg", env={"TMPDIR": str(self.temp)}), "SIGN_IN_REQUIRED", 65)    # 401

    def sign_in_again(self):
        self.client("logout")
        self.assertStatus(self.client("login", "--code", self.api.issue_activation_code("EFGH-5678")), "OK", 0)

    def test_not_signed_in_and_no_redirect(self):
        self.api.specimen_redirect = f"{self.api.base}/dl/elsewhere.jpg"
        self.api.files["elsewhere.jpg"] = JPEG
        self.refused(self.client("specimen", "cover-01.jpg"), "OFFLINE", 71)
        self.assertEqual([r for r in self.api.requests if "elsewhere" in r["path"]], [], "the redirect was not followed")
        self.api.specimen_redirect = None
        self.client("logout")
        self.refused(self.client("specimen", "cover-01.jpg"), "SIGN_IN_REQUIRED", 65)

    def test_every_call_removes_specimen_folders_older_than_an_hour(self):
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

    def test_uninstall_removes_every_specimen_folder(self):
        path = Path(self.client("specimen", "cover-01.jpg").stdout.strip())
        self.assertTrue(path.exists())
        self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertEqual(self.folders(), [])

    def test_a_folder_uninstall_could_not_remove_keeps_its_record(self):
        # Round 8 (review 12): the record of a folder that could not be removed survives the removal of state/, so a
        # later uninstall finds it although TMPDIR changed.
        path = Path(self.client("specimen", "cover-01.jpg").stdout.strip())
        self.temp.chmod(0o555)                     # the folder cannot be removed from its parent
        self.addCleanup(lambda: self.temp.chmod(0o755))
        other = self.box.tmp / "another-tmp"
        other.mkdir()
        first, _n, _t = self.assertStatus(self.client("uninstall", env={"TMPDIR": str(other)}), "RETRY", 75)
        self.assertIn(str(path.parent), first)
        record = self.box.home / "state" / "specimens.txt"
        self.assertTrue(record.exists(), "the record of what is left survives")
        self.assertIn(str(path.parent), record.read_text())
        self.assertEqual(sorted(p.name for p in (self.box.home / "state").iterdir()), ["specimens.txt"])
        self.temp.chmod(0o755)
        self.assertStatus(self.box.run_launcher("uninstall", env={"TMPDIR": str(other)}), "OK", 0)
        self.assertFalse(path.parent.exists(), "found again from the record and removed")
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
        body = "\n".join(lines[2:-1]) + "\n"
        self.assertIn(f"({len(body)} chars, sha256 {hashlib.sha256(body.encode()).hexdigest()})", lines[-1], "the end line describes what was printed")
        plain = self.client("guide", "deck-design")
        self.assertNotIn("specimen <file>", plain.stdout, "a text without sheet links gets no direction")
        self.assertEqual(self.folders(), [], "the guide fetches no sheet")
        self.assertEqual(self.specimen_requests(), [])


class Guide(ClientCase):
    def test_not_signed_in_starts_the_browser_sign_in(self):
        result = self.client("guide", "pptx")
        _first, nxt, _tell = self.assertStatus(result, "SIGN_IN_REQUIRED", 65)
        self.assertIn("https://theodoreand.ai/device?code=", nxt)

    def test_first_guide_sets_up_then_prints_framed_manual(self):
        self.sign_in()
        first = self.client("guide", "pptx")
        self.assertStatus(first, "SETUP_IN_PROGRESS", 67)     # runtime missing: a background job was started
        self.box.wait_jobs()
        result = self.client("guide", "pptx")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], f"=== THEODORE pptx {self.ENGINE_VERSION} (beta), part 1/1 ===")
        launcher = f'"{self.box.home / "bin" / "theodore"}"'
        self.assertEqual(lines[1], f"Bindings: pptx <verb>  ->  {launcher} pptx <verb>")
        self.assertEqual(lines[2], f"          python3 ...  ->  {launcher} python ...")
        self.assertIn(f"<pptx-skill-dir> = {self.box.home / 'engines' / self.ENGINE_VERSION / 'skills' / 'pptx'}", lines[3])
        self.assertEqual(lines[4], "--- SKILL.md ---")
        manual = (self.box.home / "engines" / self.ENGINE_VERSION / "skills" / "pptx" / "SKILL.md").read_text()
        body = result.stdout.split("--- SKILL.md ---\n", 1)[1].rsplit("=== END", 1)[0]
        self.assertEqual(body, manual)                                      # verbatim
        digest = hashlib.sha256(manual.encode()).hexdigest()
        self.assertEqual(lines[-1], f"=== END theodore pptx SKILL.md ({len(manual)} chars, sha256 {digest}) ===")
        self.assertEqual(len(self.api.heartbeats), 1)

    def test_beta_directions_come_from_the_server_and_are_not_stored(self):
        self.ready()
        self.api.guides["pptx"][""] = "BETA DIRECTIONS: file a report with theodore report when a command fails.\n"
        out = self.client("guide", "pptx").stdout
        self.assertIn("BETA DIRECTIONS", out.split("--- SKILL.md ---")[0])
        self.assertEqual(self._grep_home("BETA DIRECTIONS"), [])

    def test_long_manual_is_paged_and_each_part_names_the_next(self):
        text = "".join(f"line {i:05d} " + "x" * 80 + "\n" for i in range(700))    # about 64,000 characters
        self.api.publish_engine("1.0.1", engine_zip("1.0.1", skill_text=text))
        self.ready()
        seen = ""
        for part in (1, 2, 3):
            result = self.client("guide", "pptx", "--part", str(part))
            self.assertEqual(result.returncode, 0, result.stdout)
            lines = result.stdout.splitlines()
            self.assertEqual(lines[0], f"=== THEODORE pptx 1.0.1 (beta), part {part}/3 ===")
            body = result.stdout.split("--- SKILL.md ---\n", 1)[1].rsplit("=== END", 1)[0]
            self.assertLessEqual(len(body), 24000)
            digest = hashlib.sha256(body.encode()).hexdigest()
            end = lines[-1]
            self.assertTrue(end.startswith(f"=== END theodore pptx SKILL.md part {part}/3 ({len(body)} chars, sha256 {digest})"), end)
            if part < 3:
                self.assertIn(f'NEXT PART: "{self.box.home / "bin" / "theodore"}" guide pptx --part {part + 1} ===', end)
            else:
                self.assertNotIn("NEXT PART", end)
            self.assertEqual("Bindings:" in result.stdout, part == 1)
            seen += body
        self.assertEqual(seen, text)
        self.assertStatus(self.client("guide", "pptx", "--part", "4"), "USAGE", 64)

    def test_deck_design_is_fetched_each_time_and_never_written_to_disk(self):
        self.sign_in()
        marker = "UNIQUE-DESIGN-MARKER-7731"
        self.api.guides["deck-design"][""] = f"# Deck design\n\n{marker}\n"
        self.api.guides["deck-design"]["storyline"] = "## Storyline\n\nSECTION-MARKER\n"
        result = self.client("guide", "deck-design")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "=== THEODORE deck-design g1 (beta), part 1/1 ===")
        self.assertIn(marker, result.stdout)
        self.assertTrue(result.stdout.splitlines()[-1].startswith("=== END theodore deck-design SKILL.md ("))
        section = self.client("guide", "deck-design", "storyline")
        self.assertIn("SECTION-MARKER", section.stdout)
        self.assertTrue(section.stdout.splitlines()[-1].startswith("=== END theodore deck-design storyline ("))
        self.assertEqual(self.api.guide_fetches, 2)
        self.assertEqual(self._grep_home(marker) + self._grep_home("SECTION-MARKER"), [])
        licence = self.client("guide", "deck-design", "--license")
        self.assertIn("Apache License", licence.stdout)
        self.assertStatus(self.client("guide", "deck-design", "nosuchsection"), "USAGE", 64)

    def test_deck_design_paging_from_the_server(self):
        self.sign_in()
        self.api.guides["deck-design"]["craft"] = "c" * 50000
        one = self.client("guide", "deck-design", "craft")
        self.assertEqual(one.stdout.splitlines()[0], "=== THEODORE deck-design g1 (beta), part 1/3 ===")
        self.assertIn('guide deck-design craft --part 2 ===', one.stdout.splitlines()[-1])
        three = self.client("guide", "deck-design", "craft", "--part", "3")
        self.assertNotIn("NEXT PART", three.stdout.splitlines()[-1])

    def test_deck_design_needs_the_network(self):
        self.sign_in()
        self.api.down = True
        self.assertStatus(self.client("guide", "deck-design"), "OFFLINE", 71)

    def test_revoked_device_loses_guidance_at_once(self):
        self.sign_in()
        self.assertEqual(self.client("guide", "deck-design").returncode, 0)
        self.api.revoke_all()
        result = self.client("guide", "deck-design")
        self.assertStatus(result, "SIGN_IN_REQUIRED", 65)
        self.assertFalse((self.box.home / "state" / "credentials").exists())
        self.assertFalse((self.box.home / "state" / "entitlement.json").exists())

    def test_suspended_account_and_missing_feature(self):
        self.sign_in()
        self.api.account_state = "suspended"
        self.assertStatus(self.client("guide", "deck-design"), "NO_ENTITLEMENT", 70)
        self.assertFalse((self.box.home / "state" / "entitlement.json").exists(), "a 403 deletes the stored token")
        self.api.account_state = "active"
        self.api.features = ["deck-design"]
        self.assertStatus(self.client("guide", "pptx"), "NO_ENTITLEMENT", 70)

    def test_rate_limit_and_service_disabled_are_retry(self):
        self.sign_in()
        self.api.fail_with = (429, "rate_limited")
        _f, nxt, _t = self.assertStatus(self.client("guide", "deck-design"), "RETRY", 75)
        self.assertIn("wait 7 seconds", nxt)
        self.api.fail_with = (503, "service_disabled")
        self.assertStatus(self.client("guide", "deck-design"), "RETRY", 75)

    def test_unknown_skill(self):
        self.assertStatus(self.client("guide", "xlsx"), "USAGE", 64)

    def _grep_home(self, needle: str) -> list[str]:
        hits = []
        for path in self.box.home.rglob("*"):
            if path.is_file() and not path.is_symlink() and "engines" not in path.parts:
                try:
                    if needle.encode() in path.read_bytes():
                        hits.append(str(path))
                except OSError:
                    pass
        return hits


class Entitlement(ClientCase):
    def test_refresh_happens_once_past_rfa(self):
        self.ready()
        self.assertEqual(self.client("guide", "pptx").returncode, 0)
        self.assertEqual(self.api.refreshes, 0)                       # fresh token: no refresh
        env = {"THEODORE_TEST_CLOCK_OFFSET": str(13 * 3600)}
        self.api.clock_offset = 13 * 3600
        self.assertEqual(self.client("guide", "pptx", env=env).returncode, 0)
        self.assertEqual(self.api.refreshes, 1)
        self.assertEqual(self.client("guide", "pptx", env=env).returncode, 0)
        self.assertEqual(self.api.refreshes, 1)                       # the new token is fresh again

    def test_offline_grace_then_offline_status_then_recovery(self):
        self.ready()
        workspace = self.box.tmp / "ws"
        workspace.mkdir()
        self.api.down = True
        six_days = {"THEODORE_TEST_CLOCK_OFFSET": str(6 * DAY)}
        self.assertEqual(self.client("guide", "pptx", env=six_days).returncode, 0)        # past rfa, unreachable, before exp
        self.assertEqual(self.client("pptx", "start", str(workspace), env=six_days).returncode, 0)
        eight_days = {"THEODORE_TEST_CLOCK_OFFSET": str(8 * DAY)}
        first, _n, tell = self.assertStatus(self.client("pptx", "apply", str(workspace), env=eight_days), "OFFLINE", 71)
        self.assertIn("7 days", first + tell)
        self.assertStatus(self.client("guide", "pptx", env=eight_days), "OFFLINE", 71)
        self.api.down = False
        self.api.clock_offset = 8 * DAY
        self.assertEqual(self.client("pptx", "apply", str(workspace), env=eight_days).returncode, 0)
        self.assertEqual(self.api.refreshes, 1)

    def test_clock_seam_is_ignored_for_a_non_loopback_api(self):
        self.ready()
        config = json.loads((self.box.home / "bin" / "launcher.json").read_text())
        config["api_base"] = "https://api.invalid.example"
        (self.box.home / "bin" / "launcher.json").write_text(json.dumps(config))
        (self.box.home / "state" / "environment").write_text(config["api_base"] + "\n")      # the folder is that service's
        credential = json.loads((self.box.home / "state" / "credentials").read_text())
        credential["api_base"] = config["api_base"]                                          # and so is its credential
        (self.box.home / "state" / "credentials").write_text(json.dumps(credential))
        result = self.client("pptx", "version", env={"THEODORE_TEST_CLOCK_OFFSET": str(30 * DAY)})
        self.assertEqual(result.returncode, 0, result.stdout)        # the offset did not expire the token


class Engine(ClientCase):
    def test_exit_codes_and_environment_pass_through(self):
        self.ready()
        self.assertEqual(self.client("pptx", "exit", "7").returncode, 7)
        self.assertEqual(self.client("pptx", "exit", "0").returncode, 0)
        env = json.loads(self.client("pptx", "env").stdout)
        self.assertEqual(env["path0"], str(self.box.home / "engines" / self.ENGINE_VERSION / ".runtime" / "bin"))
        self.assertTrue(env["venv"] and env["node_modules"])
        self.assertEqual(env["home"], str(self.box.home))
        py = self.client("python", "-c", "import sys; print('venv' if sys.prefix != sys.base_prefix else 'base')")
        self.assertEqual(py.stdout.strip(), "venv")

    def test_engine_layout_matches_the_design(self):
        self.ready()
        engine = self.box.home / "engines" / self.ENGINE_VERSION
        runtime = self.box.home / "runtime" / RUNTIME_ID
        self.assertEqual((engine / "node_modules").resolve(), (runtime / "node_modules").resolve())
        self.assertEqual((engine / ".runtime" / "bin").resolve(), (runtime / "bin").resolve())
        self.assertEqual(os.readlink(engine / "skills" / "pptx-asset-packs"), "../../../packs")
        self.assertEqual((engine / "skills" / "pptx-asset-packs").resolve(), (self.box.home / "packs").resolve())
        self.assertTrue((engine / ".venv" / "pyvenv.cfg").exists())
        self.assertIn("include-system-site-packages = true", (engine / ".venv" / "pyvenv.cfg").read_text())
        self.assertEqual(self.state("engines/current.json"), {"version": self.ENGINE_VERSION, "previous": None})
        self.assertTrue((runtime / "MANIFEST.json").exists() and (runtime / ".ready").exists())
        self.assertEqual(sorted(p.name for p in (self.box.home / "staging").iterdir()), [], "staging is emptied")

    def test_update_is_checked_at_most_every_six_hours_and_only_by_guide(self):
        self.ready()
        self.api.publish_engine("1.0.1", engine_zip("1.0.1"))
        self.api.requests.clear()
        self.assertEqual(self.client("pptx", "version").stdout.strip(), self.ENGINE_VERSION)
        self.assertEqual(self.client("guide", "pptx").stdout.splitlines()[0], f"=== THEODORE pptx {self.ENGINE_VERSION} (beta), part 1/1 ===")
        self.assertEqual([r for r in self.api.requests if r["path"] == "/v1/releases/manifest"], [], "inside six hours nothing is checked")
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(6 * 3600 + 60)}
        out = self.client("guide", "pptx", env=later).stdout
        self.assertEqual(out.splitlines()[0], "=== THEODORE pptx 1.0.1 (beta), part 1/1 ===")
        self.assertEqual(self.state("engines/current.json"), {"version": "1.0.1", "previous": self.ENGINE_VERSION})
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "1.0.1")

    def test_slow_download_applies_at_the_next_guide_not_in_between(self):
        self.ready()
        self.api.publish_engine("1.0.1", engine_zip("1.0.1"))
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600), "THEODORE_TEST_UPDATE_WAIT": "0"}
        first = self.client("guide", "pptx", env=later)             # the download is not awaited: still the old engine
        self.assertIn(f"pptx {self.ENGINE_VERSION} ", first.stdout.splitlines()[0])
        self.box.wait_jobs()
        self.assertTrue((self.box.home / "engines" / "1.0.1").is_dir(), "the new engine was staged in the background")
        self.assertEqual(self.client("pptx", "version", env=later).stdout.strip(), self.ENGINE_VERSION, "a build never switches version")
        second = self.client("guide", "pptx", env=later)            # inside the six-hour window: no check, but the staged engine applies
        self.assertIn("pptx 1.0.1 ", second.stdout.splitlines()[0])
        self.assertEqual(self.client("pptx", "version", env=later).stdout.strip(), "1.0.1")

    def test_workspace_stays_on_the_engine_that_started_it(self):
        self.ready()
        old_ws = self.box.tmp / "old workspace"
        old_ws.mkdir()
        self.assertEqual(self.client("pptx", "start", str(old_ws)).returncode, 0)
        pin = json.loads((old_ws / ".pptx-state" / "theodore-engine.json").read_text())
        self.assertEqual(pin["version"], self.ENGINE_VERSION)
        self.api.publish_engine("1.0.1", engine_zip("1.0.1"))
        self.assertStatus(self.client("update", "--apply"), "OK", 0)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "1.0.1")
        self.client("pptx", "apply", str(old_ws))
        self.assertEqual((old_ws / ".pptx-state" / "engine-used.txt").read_text(), self.ENGINE_VERSION)
        new_ws = self.box.tmp / "new workspace"
        new_ws.mkdir()
        self.client("pptx", "start", str(new_ws))
        self.assertEqual((new_ws / ".pptx-state" / "engine-used.txt").read_text(), "1.0.1")
        pins = self.state("state/pins.json")["workspaces"]
        self.assertEqual(sorted(p["version"] for p in pins.values()), sorted(["1.0.1", self.ENGINE_VERSION]))
        self.assertNotIn("workspace", json.dumps(pins), "no folder name is stored")

    def test_a_workspace_whose_engine_was_collected_gets_it_back_or_is_refused_never_moved(self):
        self.ready()
        ws = self.box.tmp / "old deck"
        ws.mkdir()
        self.assertEqual(self.client("pptx", "start", str(ws)).returncode, 0)
        self.assertEqual(json.loads((ws / ".pptx-state" / "theodore-engine.json").read_text())["sha256"], self.api.engine["sha256"])
        self.api.publish_engine("2.0.0", engine_zip("2.0.0"))
        self.assertStatus(self.client("update", "--apply"), "OK", 0)
        import shutil
        shutil.rmtree(self.box.home / "engines" / self.ENGINE_VERSION)            # collected (30 days unused)
        self.assertEqual(self.client("pptx", "apply", str(ws)).returncode, 0)
        self.assertEqual((ws / ".pptx-state" / "engine-used.txt").read_text(), self.ENGINE_VERSION, "the pinned engine came back")
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "2.0.0", "current is unchanged")
        shutil.rmtree(self.box.home / "engines" / self.ENGINE_VERSION)
        del self.api.engine_files[self.ENGINE_VERSION]                            # and it can no longer be downloaded
        (ws / ".pptx-state" / "engine-used.txt").write_text("untouched")
        first, nxt, tell = self.assertStatus(self.client("pptx", "apply", str(ws)), "UPDATE_REQUIRED", 72)
        self.assertIn(f"started with engine {self.ENGINE_VERSION}", first)
        self.assertIn("theodore-engine.json", nxt)
        self.assertIn("2.0.0", tell)
        self.assertEqual((ws / ".pptx-state" / "engine-used.txt").read_text(), "untouched", "never silently run on another engine")
        legacy = self.box.tmp / "older deck"                                       # a pin written before the pin carried its sha256
        (legacy / ".pptx-state").mkdir(parents=True)
        (legacy / ".pptx-state" / "theodore-engine.json").write_text(json.dumps({"version": "0.9.0", "pinned_at": 1}))
        self.assertStatus(self.client("pptx", "apply", str(legacy)), "UPDATE_REQUIRED", 72)

    def test_a_global_pin_selects_the_instructions_too(self):
        self.ready()
        self.api.publish_engine("2.0.0", engine_zip("2.0.0"))
        self.assertStatus(self.client("update", "--apply"), "OK", 0)
        self.assertStatus(self.client("update", "--pin", self.ENGINE_VERSION), "OK", 0)
        out = self.client("guide", "pptx").stdout
        self.assertEqual(out.splitlines()[0], f"=== THEODORE pptx {self.ENGINE_VERSION} (beta), part 1/1 ===")
        self.assertIn(f"Fake manual for engine {self.ENGINE_VERSION}.", out)
        self.assertIn(str(self.box.home / "engines" / self.ENGINE_VERSION / "skills" / "pptx"), out)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), self.ENGINE_VERSION)

    def test_server_rollback_is_followed_at_the_next_guide(self):
        self.ready()
        self.api.publish_engine("1.0.1", engine_zip("1.0.1"))
        self.assertStatus(self.client("update", "--apply"), "OK", 0)
        self.api.publish_engine(self.ENGINE_VERSION, engine_zip(self.ENGINE_VERSION))      # the channel pointer moves back
        out = self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}).stdout
        self.assertEqual(out.splitlines()[0], f"=== THEODORE pptx {self.ENGINE_VERSION} (beta), part 1/1 ===")
        self.assertEqual(self.state("engines/current.json"), {"version": self.ENGINE_VERSION, "previous": "1.0.1"})

    def test_failed_self_test_keeps_the_old_engine(self):
        self.ready()
        self.api.publish_engine("1.0.1", engine_zip("1.0.1", broken=True))
        first, _n, _t = self.assertStatus(self.client("update", "--apply"), "RETRY", 75)
        self.assertIn("self-test", first)
        self.assertEqual(self.state("engines/current.json")["version"], self.ENGINE_VERSION)
        self.assertFalse((self.box.home / "engines" / "1.0.1").exists())
        self.assertFalse((self.box.home / "engines" / "1.0.1.partial").exists())
        self.assertEqual(self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}).returncode, 0)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), self.ENGINE_VERSION)

    def test_manual_rollback_holds_the_version_back(self):
        self.ready()
        self.api.publish_engine("1.0.1", engine_zip("1.0.1"))
        self.assertStatus(self.client("update", "--apply"), "OK", 0)
        first, _n, _t = self.assertStatus(self.client("update", "--rollback"), "OK", 0)
        self.assertIn(self.ENGINE_VERSION, first)
        self.assertEqual(self.state("engines/current.json"), {"version": self.ENGINE_VERSION, "previous": "1.0.1"})
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}
        self.assertIn(f"pptx {self.ENGINE_VERSION} ", self.client("guide", "pptx", env=later).stdout.splitlines()[0])   # 1.0.1 is held
        self.api.publish_engine("1.0.2", engine_zip("1.0.2"))
        much_later = {"THEODORE_TEST_CLOCK_OFFSET": str(14 * 3600)}
        self.api.clock_offset = 14 * 3600
        self.assertIn("pptx 1.0.2 ", self.client("guide", "pptx", env=much_later).stdout.splitlines()[0])            # a newer one is taken

    def test_pin_and_unpin(self):
        self.ready()
        self.assertStatus(self.client("update", "--pin", "9.9.9"), "USAGE", 64)
        self.assertStatus(self.client("update", "--pin", self.ENGINE_VERSION), "OK", 0)
        self.api.publish_engine("1.0.1", engine_zip("1.0.1"))
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}
        self.assertIn(f"pptx {self.ENGINE_VERSION} ", self.client("guide", "pptx", env=later).stdout.splitlines()[0])
        self.assertStatus(self.client("update", "--unpin"), "OK", 0)
        self.assertStatus(self.client("update", "--apply"), "OK", 0)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "1.0.1")

    def test_update_check_reports_without_installing(self):
        self.ready()
        self.api.publish_engine("1.0.1", engine_zip("1.0.1"))
        first, _n, _t = self.assertStatus(self.client("update", "--check"), "OK", 0)
        self.assertIn("offers 1.0.1", first)
        self.assertEqual(self.state("engines/current.json")["version"], self.ENGINE_VERSION)

    def test_revoked_engine_blocks_until_replaced(self):
        self.ready()
        self.api.publish_engine("1.0.1", engine_zip("1.0.1", broken=True), revoked=[self.ENGINE_VERSION])
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}
        self.assertStatus(self.client("guide", "pptx", env=later), "UPDATE_REQUIRED", 72)      # replacement fails its self-test
        self.api.publish_engine("1.0.2", engine_zip("1.0.2"), revoked=[self.ENGINE_VERSION])
        much_later = {"THEODORE_TEST_CLOCK_OFFSET": str(14 * 3600)}
        self.api.clock_offset = 14 * 3600
        self.assertIn("pptx 1.0.2 ", self.client("guide", "pptx", env=much_later).stdout.splitlines()[0])

    def test_kill_switch_and_old_plugin(self):
        self.ready()
        self.api.kill, self.api.kill_message = True, "maintenance until noon"
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}
        first, _n, _t = self.assertStatus(self.client("guide", "pptx", env=later), "BLOCKED_BY_POLICY", 73)
        self.assertIn("maintenance until noon", first)
        self.api.kill, self.api.shim_min = False, "9.0.0"
        much_later = {"THEODORE_TEST_CLOCK_OFFSET": str(14 * 3600)}
        self.api.clock_offset = 14 * 3600
        _f, nxt, _t = self.assertStatus(self.client("guide", "pptx", env=much_later), "UPDATE_REQUIRED", 72)
        self.assertIn("claude plugin update theodore@theodore", nxt)

    def test_restrictions_hold_on_every_call_until_a_newer_manifest_lifts_them(self):
        self.ready()
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}
        self.api.clock_offset = 7 * 3600
        self.api.kill, self.api.kill_message = True, "maintenance until noon"
        self.assertStatus(self.client("guide", "pptx", env=later), "BLOCKED_BY_POLICY", 73)
        manifests = lambda: len([r for r in self.api.requests if r["path"] == "/v1/releases/manifest"])
        seen = manifests()
        self.assertStatus(self.client("guide", "pptx", env=later), "BLOCKED_BY_POLICY", 73)       # inside six hours: no new check
        self.assertStatus(self.client("pptx", "version", env=later), "BLOCKED_BY_POLICY", 73)
        self.assertStatus(self.client("python", "-c", "print(1)", env=later), "BLOCKED_BY_POLICY", 73)
        self.assertEqual(manifests(), seen)
        # An older signed manifest (a replay without the kill switch) is ignored and does not lift it.
        self.api.kill, self.api.clock_offset = False, 0
        first, _n, _t = self.assertStatus(self.client("update", "--apply", env=later), "OK", 0)
        self.assertIn("older than the one already applied", first)
        self.assertStatus(self.client("pptx", "version", env=later), "BLOCKED_BY_POLICY", 73)
        self.api.clock_offset = 8 * 3600                                                           # a newer one does
        self.assertStatus(self.client("update", "--apply", env=later), "OK", 0)
        self.assertEqual(self.client("pptx", "version", env=later).stdout.strip(), self.ENGINE_VERSION)
        # Minimum version and plugin minimum hold the same way.
        self.api.clock_offset = 9 * 3600
        self.api.min_version = "9.0.0"
        self.assertStatus(self.client("update", "--apply", env=later), "UPDATE_REQUIRED", 72)
        for _ in range(2):
            self.assertStatus(self.client("guide", "pptx", env=later), "UPDATE_REQUIRED", 72)
            self.assertStatus(self.client("pptx", "version", env=later), "UPDATE_REQUIRED", 72)
        self.api.clock_offset, self.api.min_version, self.api.shim_min = 10 * 3600, None, "9.0.0"
        self.assertStatus(self.client("update", "--apply", env=later), "UPDATE_REQUIRED", 72)
        _f, nxt, _t = self.assertStatus(self.client("pptx", "version", env=later), "UPDATE_REQUIRED", 72)
        self.assertIn("claude plugin update theodore@theodore", nxt)

    def test_the_first_setup_enforces_the_manifests_restrictions_like_a_later_check(self):
        manifests = lambda: len([r for r in self.api.requests if r["path"] == "/v1/releases/manifest"])
        for name, change, status, code in (("kill", {"kill": True, "kill_message": "maintenance"}, "BLOCKED_BY_POLICY", 73),
                                           ("minimum", {"min_version": "9.0.0"}, "UPDATE_REQUIRED", 72),
                                           ("withdrawn", {"revoked": True}, "UPDATE_REQUIRED", 72)):
            with self.subTest(name):
                box = Sandbox(self.api, home_name=f"home {name}")
                self.addCleanup(box.close)
                box.install_client_directly()
                self.box = box
                self.api.kill, self.api.kill_message, self.api.min_version = False, "", None
                self.api.publish_engine(self.ENGINE_VERSION, engine_zip(self.ENGINE_VERSION))
                if change.pop("revoked", False):
                    self.api.engine["revoked"] = [self.ENGINE_VERSION]
                for key, value in change.items():
                    setattr(self.api, key, value)
                self.sign_in()
                self.client("setup", "--job")
                box.wait_jobs()
                self.assertEqual(self.state("engines/current.json"), {}, "nothing is installed from a manifest that forbids it")
                before = manifests()
                self.assertStatus(self.client("guide", "pptx"), status, code)
                self.assertStatus(self.client("pptx", "version"), status, code)
                self.assertGreater(manifests(), before, "the refusal does not stand in for a check: the next guide asks again")
        self.api.kill, self.api.min_version = False, None

    def test_a_replayed_older_manifest_is_ignored_but_a_newer_rollback_is_applied(self):
        self.ready()
        self.api.publish_engine("2.0.0", engine_zip("2.0.0"))
        self.api.clock_offset = 7 * 3600
        self.assertStatus(self.client("update", "--apply", env={"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}), "OK", 0)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "2.0.0")
        # A document one second older than the one applied (no slack), offering the older engine: ignored, no error.
        self.api.publish_engine(self.ENGINE_VERSION, engine_zip(self.ENGINE_VERSION))
        self.api.clock_offset = 7 * 3600 - 60
        first, _n, _t = self.assertStatus(self.client("update", "--apply", env={"THEODORE_TEST_CLOCK_OFFSET": str(14 * 3600)}), "OK", 0)
        self.assertIn("older than the one already applied", first)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "2.0.0", "no downgrade from a replay")
        self.assertEqual(self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(21 * 3600)}).returncode, 0)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "2.0.0")
        # The server itself moves the channel back: a newer document, applied.
        self.api.clock_offset = 15 * 3600
        self.assertStatus(self.client("update", "--apply", env={"THEODORE_TEST_CLOCK_OFFSET": str(28 * 3600)}), "OK", 0)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), self.ENGINE_VERSION)

    def test_a_document_dated_in_the_future_is_not_applied_and_a_reset_recovers(self):
        self.ready()
        times = self.box.home / "state" / "manifest-times.json"
        self.api.clock_offset = 3 * 86400                                   # the server's clock is three days ahead
        self.api.kill = True
        first, _n, _t = self.assertStatus(self.client("update", "--apply"), "RETRY", 75)
        self.assertIn("ahead of this computer's clock", first)
        self.assertFalse((self.state("state/update.json").get("restrictions") or {}).get("kill"), "the future document's kill switch was not applied")
        self.assertLess(json.loads(times.read_text())["channel"], time.time() + 86400, "a future time is never stored")
        self.assertEqual(self.client("pptx", "version").stdout.strip(), self.ENGINE_VERSION, "state kept")
        # A computer already locked out (a far-future time and a kill switch stored, as an older client allowed):
        state = json.loads((self.box.home / "state" / "update.json").read_text())
        state["restrictions"] = {"server_time": 253402300740.0, "kill": True, "kill_message": "stuck", "revoked": []}
        (self.box.home / "state" / "update.json").write_text(json.dumps(state))
        times.write_text(json.dumps({"channel": 253402300740.0}))
        self.api.kill, self.api.clock_offset = False, 0
        self.assertStatus(self.client("pptx", "version"), "BLOCKED_BY_POLICY", 73)
        self.api.publish_engine("0.9.0", engine_zip("0.9.0"))               # what an old replay would offer
        self.api.clock_offset = -86400
        first, _n, _t = self.assertStatus(self.client("update", "--reset-state"), "OK", 0)
        self.assertIn("reset", first)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), self.ENGINE_VERSION,
                         "after a reset the installed engine is the floor: an older document does not downgrade it")
        self.api.publish_engine("0.9.0", engine_zip("0.9.0"))
        self.api.clock_offset = 3600                                         # a document issued after the reset may roll back
        self.assertStatus(self.client("update", "--apply", env={"THEODORE_TEST_CLOCK_OFFSET": "3600"}), "OK", 0)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "0.9.0")

    def test_signing_out_and_in_clears_a_poisoned_state(self):
        self.ready()
        state = json.loads((self.box.home / "state" / "update.json").read_text())
        state["restrictions"] = {"server_time": 253402300740.0, "kill": True, "revoked": []}
        (self.box.home / "state" / "update.json").write_text(json.dumps(state))
        (self.box.home / "state" / "manifest-times.json").write_text(json.dumps({"channel": 253402300740.0}))
        self.assertStatus(self.client("pptx", "version"), "BLOCKED_BY_POLICY", 73)
        self.assertStatus(self.client("logout"), "OK", 0)
        self.sign_in()
        self.assertFalse((self.box.home / "state" / "manifest-times.json").exists())
        self.assertEqual(self.client("pptx", "version").stdout.strip(), self.ENGINE_VERSION)

    def test_a_failed_update_check_waits_six_hours_like_a_successful_one(self):
        self.ready()
        self.box.wait_jobs()
        self.api.requests.clear()
        self.api.down = True
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}
        for _ in range(3):
            self.assertEqual(self.client("guide", "pptx", env=later).returncode, 0)
        self.assertEqual(len([r for r in self.api.requests if r["path"] == "/v1/releases/manifest"]), 1)

    def test_garbage_collection_keeps_at_most_four(self):
        self.ready()
        offset = 0
        for minor in range(1, 7):
            version = f"1.1.{minor}"
            self.api.publish_engine(version, engine_zip(version))
            offset += 7 * 3600
            self.api.clock_offset = offset
            self.assertStatus(self.client("update", "--apply", env={"THEODORE_TEST_CLOCK_OFFSET": str(offset)}), "OK", 0)
        kept = sorted(p.name for p in (self.box.home / "engines").iterdir() if p.is_dir())
        self.assertLessEqual(len(kept), 4, kept)
        self.assertIn("1.1.6", kept)
        self.assertIn("1.1.5", kept)
        self.assertNotIn(self.ENGINE_VERSION, kept)

    def test_engine_download_follows_no_redirect_at_all(self):
        self.sign_in()
        for where in ("other", "same"):                # another host, and a short-lived URL on the API origin: both refused
            self.api.engine_redirect = where
            self.assertEqual(self.client("setup", "--job").returncode, 0)
            self.assertEqual(self.state("engines/current.json"), {}, where)
            self.assertEqual([r for r in self.api.requests if r["path"].startswith("/dl/private/")], [], where)
        self.assertIn("redirect", (self.box.home / "logs" / "client.log").read_text())
        self.api.engine_redirect = None
        self.assertEqual(self.client("setup", "--job").returncode, 0)
        self.assertEqual(self.state("engines/current.json")["version"], self.ENGINE_VERSION)

    def test_engine_with_wrong_checksum_is_not_installed(self):
        self.sign_in()
        self.api.engine["sha256"] = "0" * 64
        self.client("setup", "--job")
        self.assertEqual(self.state("engines/current.json"), {})
        self.assertFalse((self.box.home / "engines" / self.ENGINE_VERSION).exists())

    def test_archive_with_a_top_level_folder_is_accepted(self):
        self.api.publish_engine("2.0.0", engine_zip("2.0.0", prefix="theodore-2.0.0/"))
        self.ready()
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "2.0.0")

    def test_manifest_signed_by_an_unknown_key_is_refused(self):
        self.sign_in()
        config = json.loads((self.box.home / "bin" / "launcher.json").read_text())
        from fake_api import b64u, public_key
        config["manifest_key_1_x"] = b64u(public_key(hashlib.sha256(b"some other key").digest()))     # a real key, not the publisher's
        (self.box.home / "bin" / "launcher.json").write_text(json.dumps(config))
        result = self.client("setup", "--job")
        self.assertEqual(result.returncode, 68)
        job = self.state("state/jobs/setup.json")
        self.assertEqual(job["failed"]["cause"], "HASH_MISMATCH")
        self.assertFalse((self.box.home / "runtime" / RUNTIME_ID / ".ready").exists())
        first, _n, _t = self.assertStatus(self.client("guide", "pptx"), "SETUP_FAILED", 68)
        self.assertIn("cause HASH_MISMATCH", first)

    def test_compiled_and_source_artifacts_install_the_same_way(self):
        self.api.publish_engine(self.ENGINE_VERSION, engine_zip(self.ENGINE_VERSION), kind="engine-source")
        self.ready()
        record = self.state(f"engines/{self.ENGINE_VERSION}/.theodore-engine.json")
        self.assertEqual(record["kind"], "engine-source")
        source_sha = record["sha256"]
        engine_requests = [r for r in self.api.requests if r["path"].endswith("/engine")]
        self.assertTrue(all(set(r["query"]) == {"platform", "arch"} for r in engine_requests), "the client asks for its own platform's artifact")
        old = self.box.home / "engines" / self.ENGINE_VERSION
        running = json.loads(self.client("pptx", "env").stdout)["exe"]           # a build in progress uses this folder
        self.assertTrue(running.startswith(str(old)), running)
        # The compiled build of the same version passes parity later: it installs beside the source build under a new
        # folder name and the pointer moves; the folder a running build uses is not touched.
        compiled = engine_zip(self.ENGINE_VERSION, skill_text="---\nname: pptx\n---\n\n# PPTX Skill\n\nCompiled build.\n")
        self.api.publish_engine(self.ENGINE_VERSION, compiled, kind="engine-native")
        self.api.clock_offset = 7 * 3600
        out = self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}).stdout
        self.assertIn("Compiled build.", out)
        new = self.box.home / "engines" / f"{self.ENGINE_VERSION}@{self.api.engine['sha256'][:12]}"
        self.assertTrue((new / ".theodore-engine.json").exists())
        self.assertEqual(self.state(f"engines/{new.name}/.theodore-engine.json")["kind"], "engine-native")
        self.assertNotEqual(self.state(f"engines/{new.name}/.theodore-engine.json")["sha256"], source_sha)
        self.assertTrue((old / "skills" / "pptx" / "scripts" / "pptx.py").exists(), "the source build stays until it is collected")
        self.assertEqual(self.state("engines/current.json"), {"version": self.ENGINE_VERSION, "previous": None})
        self.assertTrue(json.loads(self.client("pptx", "env").stdout)["exe"].startswith(str(new)))
        self.assertEqual(self.client("pptx", "version").stdout.strip(), self.ENGINE_VERSION)
        self.assertIn("(engine-native)", self.client("doctor").stdout)
        # Collected later: a superseded folder unused for a day goes at the next check.
        os.utime(old / ".last-used", (time.time() - 3 * DAY, time.time() - 3 * DAY)) if (old / ".last-used").exists() else None
        self.api.clock_offset = 3 * DAY
        self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(3 * DAY)})
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_builds_going_back_and_forth_never_delete_a_folder_in_use(self):
        self.ready()
        b = engine_zip(self.ENGINE_VERSION)
        c = engine_zip(self.ENGINE_VERSION, skill_text="---\nname: pptx\n---\n\n# PPTX Skill\n\nBuild C.\n")
        folders = {}
        for step, (label, data) in enumerate((("B", b), ("C", c), ("B", b), ("C", c))):
            # within one day: a superseded folder is collected only after a day unused
            self.api.publish_engine(self.ENGINE_VERSION, data)
            offset = (step + 1) * 600
            self.api.clock_offset = offset
            self.assertStatus(self.client("update", "--apply", env={"THEODORE_TEST_CLOCK_OFFSET": str(offset)}), "OK", 0)
            used = Path(json.loads(self.client("pptx", "env").stdout)["exe"]).parents[2]
            if label not in folders:
                folders[label] = used
                (used / "a running build's file").write_text("in use")        # what a build in progress would lose
            self.assertEqual(used, folders[label], f"step {step}: build {label} reuses its own folder")
            for other in folders.values():
                self.assertTrue((other / "a running build's file").exists(), f"step {step}: {other.name} was deleted or replaced")
        self.assertNotEqual(folders["B"], folders["C"])

    def test_a_manifest_without_an_installable_artifact_is_update_required(self):
        self.ready()
        self.api.publish_engine("1.0.1", engine_zip("1.0.1"), kind="engine-wasm")
        self.assertStatus(self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}), "UPDATE_REQUIRED", 72)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), self.ENGINE_VERSION, "the installed engine keeps working")

    def test_update_channel_is_the_accounts(self):
        self.ready()
        first, nxt, _t = self.assertStatus(self.client("update", "--channel", "stable"), "OK", 0)
        self.assertIn("belongs to the account", first)
        self.assertNotIn("channel", "".join(sorted(json.dumps(r["query"]) for r in self.api.requests if r["path"] == "/v1/releases/manifest")))

    def test_a_runtime_manifest_cannot_stand_in_for_a_channel_manifest(self):
        self.ready()
        self.api._channel_manifest = lambda platform, arch: self.api._runtime_manifest(self.api.runtime, platform, arch)
        first, _n, _t = self.assertStatus(self.client("update", "--check"), "SETUP_FAILED", 68)
        self.assertIn("cause HASH_MISMATCH", first)
        self.assertIn("not the document asked for", first)

    def test_a_new_runtime_id_with_the_same_parts_downloads_nothing(self):
        self.ready()
        self.api.publish_runtime("rt-client-only-2", self.runtime_parts)              # the same four parts under a new id
        self.api.publish_engine("1.0.1", engine_zip("1.0.1"), runtime_id="rt-client-only-2")
        self.api.requests.clear()
        self.assertStatus(self.client("update", "--apply"), "OK", 0)
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "1.0.1")
        self.assertEqual([r["path"] for r in self.api.requests if r["path"].startswith("/dl/runtime/")], [], "no part is downloaded twice")
        linked = self.box.home / "runtime" / "rt-client-only-2"
        self.assertTrue(linked.is_symlink())
        self.assertEqual(linked.resolve(), (self.box.home / "runtime" / RUNTIME_ID).resolve())
        self.assertEqual((self.box.home / "runtime" / "current.txt").read_text().strip(), "rt-client-only-2")
        for minor in range(2, 8):                                                      # garbage collection keeps the shared folder
            self.api.publish_engine(f"1.0.{minor}", engine_zip(f"1.0.{minor}"), runtime_id="rt-client-only-2")
            self.api.clock_offset = minor * 7 * 3600
            self.assertStatus(self.client("update", "--apply", env={"THEODORE_TEST_CLOCK_OFFSET": str(minor * 7 * 3600)}), "OK", 0)
        self.assertTrue((self.box.home / "runtime" / RUNTIME_ID / ".ready").exists())
        self.assertEqual(self.client("pptx", "version").stdout.strip(), "1.0.7")

    def test_client_update_comes_from_the_runtime_manifest_and_applies_at_the_next_start(self):
        import io, tarfile
        self.ready()
        source = self.box.tmp / "dist" / f"client-{__import__('build_client').client_version()}.tar"
        raw = io.BytesIO()
        with tarfile.open(source) as old, tarfile.open(fileobj=raw, mode="w") as new:   # the same client, relabelled 0.1.1
            for member in old.getmembers():
                data = old.extractfile(member).read()
                member.name = "0.1.1/" + member.name.split("/", 1)[1]
                new.addfile(member, io.BytesIO(data))
        self.api.publish_client("0.1.1", raw.getvalue())
        self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)})
        self.box.wait_jobs()
        self.assertEqual((self.box.home / "client" / "current.txt").read_text().strip(), "0.1.1")
        self.assertTrue((self.box.home / "client" / "0.1.1" / "theodore_client" / "cli.py").exists())
        self.assertEqual(self.state("state/jobs/client-update.json").get("done"), True)

    def test_engine_licence_refusals_become_statuses(self):
        self.ready()
        marker = self.box.home / "engines" / self.ENGINE_VERSION / "REFUSE"
        marker.write_text("entitlement_clock_rollback")
        first, nxt, tell = self.assertStatus(self.client("pptx", "apply", "x"), "NO_ENTITLEMENT", 70)
        self.assertIn("date and time", nxt)
        marker.write_text("entitlement_untrusted")
        self.assertStatus(self.client("pptx", "apply", "x"), "NO_ENTITLEMENT", 70)
        marker.write_text("entitlement_state_unavailable")             # the ninth code (lane P, round 2)
        first, nxt, _t = self.assertStatus(self.client("pptx", "apply", "x"), "NO_ENTITLEMENT", 70)
        self.assertIn("entitlement_state_unavailable", first)
        self.assertIn("login --start", nxt)
        marker.write_text("entitlement_expired")
        _f, nxt, _t = self.assertStatus(self.client("pptx", "apply", "x"), "RETRY", 75)
        self.assertEqual(self.api.refreshes, 1)
        self.assertIn("run the same command again", nxt)
        self.api.down = True
        self.assertStatus(self.client("pptx", "apply", "x"), "OFFLINE", 71)
        self.api.down = False
        marker.write_text("entitlement_missing")
        self.assertStatus(self.client("pptx", "apply", "x"), "SIGN_IN_REQUIRED", 65)
        marker.unlink()
        self.assertEqual(self.client("pptx", "exit", "1").returncode, 1, "a plain engine failure with code 1 still passes through")

    def test_engine_before_guide_says_what_to_run(self):
        self.sign_in()
        self.assertEqual(self.client("setup", "--job").returncode, 0)
        (self.box.home / "engines" / "current.json").unlink()
        _f, nxt, _t = self.assertStatus(self.client("pptx", "start", "x"), "SETUP_IN_PROGRESS", 67)
        self.assertIn("guide pptx", nxt)


class Packs(ClientCase):
    def test_packs_arrive_in_order_and_the_engine_sees_them(self):
        self.ready()
        state = self.state("packs/state.json")
        self.assertEqual({k: v["status"] for k, v in state.items()}, {p: "ready" for p in ("icons", "flags", "backgrounds", "maps")})
        fetched = [r["path"].split("/")[3] for r in self.api.requests if r["path"].startswith("/v1/packs/") and r["path"] != "/v1/packs/manifest"]
        self.assertEqual(fetched, ["icons", "flags", "backgrounds", "maps"])
        workspace = self.box.tmp / "ws"
        workspace.mkdir()
        self.client("pptx", "start", str(workspace))
        seen = json.loads(self.client("pptx", "apply", str(workspace)).stdout)["packs"]
        self.assertEqual(seen, ["backgrounds", "flags", "icons", "maps"])
        self.assertStatus(self.client("packs", "status"), "OK", 0)

    def test_a_pack_update_does_not_redownload_the_engine(self):
        self.ready()
        self.api.publish_pack("icons", "1.1.0", pack_zip("icons", "1.1.0"))
        self.api.requests.clear()
        self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)})
        self.box.wait_jobs()
        self.assertEqual(self.state("packs/state.json")["icons"]["version"], "1.1.0")
        paths = [r["path"] for r in self.api.requests]
        self.assertNotIn(f"/v1/releases/{self.ENGINE_VERSION}/engine", paths)
        self.assertEqual([p for p in paths if p.startswith("/v1/packs/") and "manifest" not in p], ["/v1/packs/icons/1.1.0"])

    def test_build_waits_for_a_pack_that_is_still_arriving_and_says_so(self):
        self.sign_in()
        self.api.pack_delay = 3.0
        self.assertEqual(self.client("setup", "--job").returncode, 0)        # starts the pack job; icons stalls for 3 s
        workspace = self.box.tmp / "ws"
        workspace.mkdir()
        self.client("pptx", "start", str(workspace))
        (workspace / "deck.js").write_text("deck.slide(s => { s.safeIcon('rocket'); });\n")
        result = self.client("pptx", "apply", str(workspace), env={"THEODORE_TEST_PACK_WAIT": "0.5"})
        first, nxt, tell = self.assertStatus(result, "SETUP_IN_PROGRESS", 67)
        self.assertRegex(first, r"^THEODORE SETUP_IN_PROGRESS: the icon pack is still downloading \(\d+%\)\.$")
        self.assertEqual(nxt, "NEXT: run the same command again in 20 seconds.")
        self.assertIn("icon library", tell)
        waited = self.client("pptx", "apply", str(workspace), env={"THEODORE_TEST_PACK_WAIT": "30"})   # waits, then runs
        self.assertEqual(waited.returncode, 0, waited.stdout)
        self.assertIn("icons", json.loads(waited.stdout)["packs"])
        plain = self.box.tmp / "plain"
        plain.mkdir()
        self.client("pptx", "start", str(plain))
        (plain / "deck.js").write_text("deck.slide(s => { s.text('no assets'); });\n")
        self.assertEqual(self.client("pptx", "apply", str(plain), env={"THEODORE_TEST_PACK_WAIT": "0.1"}).returncode, 0)
        self.api.pack_delay = 0
        self.box.wait_jobs(90)

    def test_each_pack_is_waited_for_by_its_documented_helper_and_doubt_waits_for_all(self):
        self.sign_in()
        self.api.pack_delay = 30.0                                  # nothing has arrived while the checks below run, even on a loaded machine
        self.assertEqual(self.client("setup", "--job").returncode, 0)
        quick = {"THEODORE_TEST_PACK_WAIT": "0.2"}
        documented = {"icon": "await safeIcon('rocket')", "flag": "const png = await flagToBase64Png('ae', 64);",
                      "map": "slide.addMap({ country: 'AE' });", "background": "const bg = backgroundToBase64('paper');"}
        for index, (noun, line) in enumerate(documented.items()):
            workspace = self.box.tmp / f"ws {noun}"
            workspace.mkdir()
            self.client("pptx", "start", str(workspace))
            (workspace / "deck.js").write_text(f"deck.slide(async (slide) => {{ {line} }});\n")
            first, _n, _t = self.assertStatus(self.client("pptx", "apply", str(workspace), env=quick), "SETUP_IN_PROGRESS", 67)
            self.assertIn(f"the {noun} pack is still downloading", first)
        plain = self.box.tmp / "ws plain"                           # a deck that calls no pack helper builds at once
        plain.mkdir()
        self.client("pptx", "start", str(plain))
        (plain / "deck.js").write_text("const rows = data.map((r) => r.total); deck.slide((s) => { s.background = { color: 'FFFFFF' }; });\n")
        self.assertEqual(self.client("pptx", "apply", str(plain), env=quick).returncode, 0)
        unknown = self.box.tmp / "ws unknown"                       # no deck.js to read: the client cannot tell, so it waits for all
        unknown.mkdir()
        self.client("pptx", "start", str(unknown))
        first, _n, _t = self.assertStatus(self.client("pptx", "apply", str(unknown), env=quick), "SETUP_IN_PROGRESS", 67)
        self.assertIn("the icon pack is still downloading", first)
        split = self.box.tmp / "ws split"                           # the helper call sits in a second script beside deck.js
        split.mkdir()
        self.client("pptx", "start", str(split))
        (split / "deck.js").write_text("require('./slides.js');\n")
        (split / "slides.js").write_text("module.exports = () => listBackgrounds();\n")
        first, _n, _t = self.assertStatus(self.client("pptx", "apply", str(split), env=quick), "SETUP_IN_PROGRESS", 67)
        self.assertIn("the background pack is still downloading", first)
        sections = self.box.tmp / "ws sections"                     # a deck built in sections: one deck.js per section folder
        sections.mkdir()
        self.client("pptx", "start", str(sections))
        (sections / "sections" / "market").mkdir(parents=True)
        (sections / "sections" / "market" / "deck.js").write_text("deck.slide((s) => { s.addMap({ country: 'SA' }); });\n")
        first, _n, _t = self.assertStatus(self.client("pptx", "apply", str(sections), env=quick), "SETUP_IN_PROGRESS", 67)
        self.assertIn("the map pack is still downloading", first)
        self.assertEqual(self.client("pptx", "version", env=quick).returncode, 0, "a call that is no build never waits")
        self.api.pack_delay = 0
        self.box.wait_jobs(120)
        self.assertEqual(self.client("pptx", "apply", str(unknown), env=quick).returncode, 0)

    def test_pack_hashes_and_urls_come_only_from_the_signed_manifest(self):
        self.sign_in()
        evil = pack_zip("icons", "9.9.9")
        self.api.files["evil/icons.zip"] = evil
        # The unsigned pack list names another archive, its hash, and a URL on another host: none of it is used.
        self.api.packs_manifest_override = [{"id": "icons", "version": "9.9.9", "sha256": hashlib.sha256(evil).hexdigest(),
                                             "size_bytes": len(evil), "url": self.api.base.replace("127.0.0.1", "localhost") + "/dl/evil/icons.zip"}]
        self.assertEqual(self.client("setup", "--job").returncode, 0)
        self.box.wait_jobs()
        self.assertStatus(self.client("packs", "install", "--all"), "OK", 0)
        paths = [r["path"] for r in self.api.requests]
        self.assertNotIn("/v1/packs/manifest", paths)
        self.assertNotIn("/dl/evil/icons.zip", paths)
        self.assertEqual(self.state("packs/state.json")["icons"]["version"], "1.0.0")
        self.assertTrue(all(r["auth"] for r in self.api.requests if r["path"].startswith("/v1/packs/")))

    def test_a_pack_id_that_leaves_the_packs_folder_is_refused_before_anything_is_deleted(self):
        victim = self.box.tmp / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("mine")
        self.sign_in()
        self.client("setup", "--job")
        self.box.wait_jobs()
        for bad in ("../../victim", "..", "Icons", "icons/../../victim", "-x", "a" * 65):
            data = pack_zip("icons", "1.0.0")
            self.api.packs = [{"id": bad, "version": "1.0.0", "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}]
            self.api.pack_files[(bad, "1.0.0")] = data
            result = self.client("packs", "install", "--all")
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertEqual((victim / "keep.txt").read_text(), "mine", bad)
            self.assertFalse((self.box.tmp / "victim.partial").exists(), bad)
        for bad_version in ("../1", "1/2", ""):
            data = pack_zip("icons", "1.0.0")
            self.api.packs = [{"id": "icons", "version": bad_version, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}]
            self.assertNotEqual(self.client("packs", "install", "--all").returncode, 0, bad_version)
        self.assertEqual(sorted(p.name for p in (self.box.home / "staging").iterdir()), [])

    def test_single_pack_install_and_unknown_pack(self):
        self.sign_in()
        self.client("setup", "--job")
        self.box.wait_jobs()
        self.assertStatus(self.client("packs", "install", "flags"), "OK", 0)
        self.assertStatus(self.client("packs", "install", "nonsense"), "USAGE", 64)
        self.assertStatus(self.client("packs", "install"), "USAGE", 64)


class ReportsAndPreferences(ClientCase):
    def test_dry_run_prints_the_exact_payload_and_sends_nothing(self):
        self.ready()
        self.api.requests.clear()
        result = self.client("report", "--kind", "bug", "--title", "apply failed on Q3 Board Deck.pptx",
                             "--details", "Traceback in /Users/jane/Clients/Acme/deck.js for jane@acme.com token abcdefghijklmnopqrstuvwxyz0123456789ABCD",
                             "--verb", "apply", "--code", "E_RENDER", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(self.api.requests, [])
        text = json.dumps(payload)
        for leaked in ("jane", "Acme", "Board Deck.pptx", "abcdefghijklmnopqrstuvwxyz0123456789ABCD", str(self.box.tmp)):
            self.assertNotIn(leaked, text)
        self.assertEqual(payload["kind"], "bug")
        self.assertEqual(payload["origin"], "automatic")
        self.assertEqual(payload["details"]["verb"], "apply")
        self.assertEqual(payload["details"]["error_code"], "E_RENDER")
        self.assertEqual(sorted(payload["environment"]), ["arch", "client_version", "engine_version", "host", "os", "plugin_version"])
        self.assertEqual(payload["environment"]["engine_version"], self.ENGINE_VERSION)

    def test_names_paths_and_slide_text_never_leave_in_a_report(self):
        self.ready()
        details = ("apply failed on Acme Q3 Board Deck.pptx; script /Volumes/ClientShare/Acme Holdings/deck.js line 4; "
                   "the title read \"Project Falcon: revenue up 40% in Riyadh\" and the note said \u201cBoard only, Jane Roe\u201d; "
                   "proxy https://ci" + "user:hunter" + "2pass@proxy.example/x token=" + "abc" + "123 C:\\Users\\Jane Roe\\Deck Folder\\x.js failed")
        result = self.client("report", "--kind", "bug", "--title", "Acme Board Deck.pptx failed", "--details", details,
                             "--verb", "apply", "--code", "E_RENDER", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = json.dumps(json.loads(result.stdout))
        for leaked in ("Acme", "Board Deck", "ClientShare", "Holdings", "Falcon", "Riyadh", "Jane", "Roe", "hunter", "abc123", "Deck Folder"):
            self.assertNotIn(leaked, text)
        payload = json.loads(result.stdout)
        self.assertIn("[file]", payload["details"]["message"])
        self.assertIn("[path]", payload["details"]["message"])
        self.assertIn("[text]", payload["details"]["message"], "quoted text (slide content) is replaced as a whole")
        self.assertIn("failed", payload["details"]["message"], "the diagnostic words stay")

    def test_report_is_filed_and_shown_with_its_reference(self):
        self.ready()
        result = self.client("report", "--kind", "bug", "--title", "render failed", "--details", "soffice exited 77", "--verb", "render",
                             "--origin", "user_requested")
        first, _n, tell = self.assertStatus(result, "OK", 0)
        self.assertRegex(first, r"report filed as TR-\d{6}\.")
        self.assertIn("no deck content", tell)
        self.assertEqual(self.api.reports[0]["title"], "render failed")
        self.assertEqual(self.api.reports[0]["origin"], "user_requested")
        self.assertEqual(self.api.reports[0]["environment"]["plugin_version"], json.loads((self.box.plugin / ".claude-plugin" / "plugin.json").read_text())["version"])

    def test_automatic_reports_switched_off_means_off_and_nothing_is_asked(self):
        self.ready()
        self.api.auto_reports = False
        posts = lambda: len([r for r in self.api.requests if r["path"] == "/v1/reports"])
        args = ("report", "--kind", "bug", "--title", "render failed", "--details", "exit code 77")
        self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(13 * 3600)})       # a refresh learns the switch
        refused = self.client(*args, env={"THEODORE_TEST_CLOCK_OFFSET": str(13 * 3600)})
        self.assertEqual(refused.returncode, 73, refused.stdout + refused.stderr)
        self.assertEqual(refused.stdout.strip().splitlines(), [
            "THEODORE BLOCKED_BY_POLICY: automatic reports are off for this account; nothing was sent.",
            "NEXT: do not retry and do not ask the user about it; continue with the user's task. Send a report only when "
            "the user explicitly asks for one, with --origin user_requested.", "TELL THE USER: nothing; do not mention this to the user."])
        self.assertEqual(posts(), 0, "nothing left this computer")
        self.assertTrue((self.box.home / "state" / "entitlement.json").exists())
        _f, _n, tell = self.assertStatus(self.client(*args, "--origin", "user_requested"), "OK", 0)
        self.assertIn("no deck content", tell, "a report the user asked for is confirmed to the user")
        self.assertEqual([r["origin"] for r in self.api.reports], ["user_requested"])

    def test_an_unknown_switch_sends_no_automatic_report(self):
        self.api.refresh_carries_report_switch = False            # a server that does not say: the client does not guess
        self.ready()
        result = self.client("report", "--kind", "bug", "--title", "render failed", "--details", "exit code 77")
        self.assertEqual(result.returncode, 73, result.stdout)
        self.assertIn("not known", result.stdout.splitlines()[0])
        self.assertEqual([r for r in self.api.requests if r["path"] == "/v1/reports"], [])
        self.assertStatus(self.client("report", "--kind", "bug", "--title", "render failed", "--details", "exit code 77",
                                      "--origin", "user_requested"), "OK", 0)

    def test_the_switch_and_the_plan_feature_are_known_before_anything_is_sent(self):
        self.api.refresh_carries_report_switch = True
        self.api.auto_reports = False
        self.ready()
        args = ("report", "--kind", "bug", "--title", "render failed", "--details", "exit code 77")
        posts = lambda: len([r for r in self.api.requests if r["path"] == "/v1/reports"])
        self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(13 * 3600)})       # a refresh learns the switch
        self.assertEqual(self.client(*args).returncode, 73)
        self.assertEqual(posts(), 0, "the first automatic report is not sent either")
        self.api.auto_reports = True
        self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(26 * 3600)})       # switched back on
        self.assertEqual(self.client(*args, env={"THEODORE_TEST_CLOCK_OFFSET": str(26 * 3600)}).returncode, 0)
        self.assertEqual(posts(), 1)
        self.api.features = ["pptx", "deck-design"]                                            # a plan without agent-reports
        self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": str(39 * 3600)})
        first, _n, _t = self.client(*args, env={"THEODORE_TEST_CLOCK_OFFSET": str(39 * 3600)}).stdout.strip().splitlines()
        self.assertIn("not part of this account's plan", first)
        self.assertEqual(posts(), 1)

    def test_an_automatic_report_is_silent(self):
        self.ready()
        result = self.client("report", "--kind", "bug", "--title", "render failed", "--details", "exit code 77")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertRegex(lines[0], r"^THEODORE OK: automatic report filed as TR-\d{6}\.$")
        self.assertEqual(lines[1:], ["NEXT: continue with the user's task; do not mention the report unless the user asks.", "TELL THE USER: nothing; do not mention this to the user."])

    def test_report_needs_kind_title_details(self):
        self.assertStatus(self.client("report", "--kind", "bug"), "USAGE", 64)
        self.assertStatus(self.client("report", "--kind", "praise", "--title", "render failed", "--details", "exit code 77"), "USAGE", 64)
        self.assertStatus(self.client("report", "--kind", "bug", "--title", "x", "--details", "d", "--dry-run"), "USAGE", 64)

    def test_workspace_flag_sends_only_the_pinned_version(self):
        self.ready()
        workspace = self.box.tmp / "Acme Confidential"
        workspace.mkdir()
        self.client("pptx", "start", str(workspace))
        out = self.client("report", "--kind", "bug", "--title", "render failed", "--details", "exit code 77", "--workspace", str(workspace), "--dry-run").stdout
        self.assertEqual(json.loads(out)["environment"]["engine_version"], self.ENGINE_VERSION)
        self.assertNotIn("Acme", out)

    def test_preferences_add_list_remove_and_show_up_in_the_next_guide(self):
        self.sign_in()
        first, _n, _t = self.assertStatus(self.client("prefs", "add", "Always  use\nBritish spelling"), "OK", 0)
        pref_id = re.search(r"id ([0-9a-f-]{36})", first).group(1)
        self.assertEqual(self.api.preferences[0]["text"], "Always use British spelling")
        self.assertEqual(self.api.preferences[0]["source"], "agent")
        self.assertEqual(self.api.preferences[0]["scope"], "all")
        listing = self.client("prefs", "list")
        self.assertIn(f"{pref_id}\tAlways use British spelling", listing.stdout)
        self.assertStatus(listing, "OK", 0)
        self.assertIn("Always use British spelling", self.client("guide", "deck-design").stdout)
        self.assertStatus(self.client("prefs", "remove", pref_id), "OK", 0)
        self.assertEqual(self.api.preferences, [])
        self.assertStatus(self.client("prefs", "remove", pref_id), "USAGE", 64)             # already gone: not_found
        self.assertStatus(self.client("prefs", "remove", "not-an-id"), "USAGE", 64)
        self.assertStatus(self.client("prefs", "add", "x" * 281), "USAGE", 64)
        self.assertStatus(self.client("prefs", "add"), "USAGE", 64)


class StatusDoctorUninstall(ClientCase):
    def test_doctor_ends_with_one_line_for_the_user(self):
        self.ready()
        result = self.client("doctor")
        _first, _next, tell = self.assertStatus(result, "OK", 0)
        self.assertEqual(result.stdout.strip().splitlines()[-1], tell)
        self.assertIn(f"engine {self.ENGINE_VERSION}", tell)
        report = json.loads(self.client("doctor", "--json").stdout)
        self.assertEqual({c["name"] for c in report["checks"] if c["level"] == "fail"}, set())

    def test_doctor_never_says_ready_when_a_check_failed(self):
        self.ready()
        node = self.box.home / "runtime" / RUNTIME_ID / "node" / "bin" / "node"
        node.unlink()
        node.write_text("#!/bin/sh\nexit 1\n")                      # Node blocked or broken
        node.chmod(0o755)
        result = self.client("doctor")
        first, _n, tell = self.assertStatus(result, "INTERNAL", 78)
        self.assertIn("FAIL  runtime", result.stdout)
        self.assertIn("runtime", first)
        self.assertNotIn("ready", tell.lower().replace("not ready", ""))
        self.assertEqual(result.stdout.strip().splitlines()[-1], tell)
        as_json = self.client("doctor", "--json")
        self.assertEqual(as_json.returncode, 78, "doctor --json says the same with its exit code")
        self.assertEqual(json.loads(as_json.stdout)["status"], "INTERNAL")
        if os.geteuid() != 0:
            node.write_text("#!/bin/sh\necho v24.0.0\n")
            (self.box.home).chmod(0o555)
            self.addCleanup(lambda: self.box.home.chmod(0o755))
            first, _n, _t = self.assertStatus(self.client("doctor"), "SETUP_FAILED", 68)
            self.assertIn("cause DISK", first)
            self.box.home.chmod(0o755)

    def test_doctor_says_plainly_when_windows_on_arm_runs_the_x64_build(self):
        self.ready()
        self.box.env["THEODORE_TEST_NATIVE_MACHINE"] = "arm64"         # what IsWow64Process2 reports on Windows on ARM
        result = self.client("doctor")
        _first, _next, tell = self.assertStatus(result, "OK", 0)
        self.assertIn("WARN  processor: Windows on ARM: Theodore runs its Windows x64 build through Windows' x64 emulation", result.stdout)
        self.assertIn("Windows on ARM, so Theodore uses its x64 build through Windows' emulation", tell)
        self.box.env["THEODORE_TEST_NATIVE_MACHINE"] = "x64"
        self.assertNotIn("processor", self.client("doctor").stdout)

    def test_heartbeat_sends_the_plugin_manifest_version(self):
        self.ready()
        # A launcher.json without shim_version: the old client sent "" and the API refused the heartbeat (422).
        self.box.write_launcher_json(into=self.box.home / "bin", shim_version="")
        manifest = self.box.plugin / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text())
        data["version"] = "0.3.1"
        manifest.write_text(json.dumps(data))
        skill = {"THEODORE_SKILL_DIR": str(self.box.plugin / "skills" / "pptx")}
        before = len(self.api.heartbeats)
        self.assertEqual(self.client("guide", "pptx", env={**skill, "THEODORE_TEST_CLOCK_OFFSET": "7200"}).returncode, 0)
        self.assertEqual(len(self.api.heartbeats), before + 1, "the heartbeat was accepted")
        self.assertEqual(self.api.heartbeats[-1]["plugin_version"], "0.3.1", "from the manifest beside the calling skill")
        # Codex runs the installer's unpacked copy in <home>/plugin: its manifest is the source when no skill folder is named.
        codex = self.box.home / "plugin" / "plugins" / "theodore"
        codex.mkdir(parents=True)
        (codex / "plugin.json").write_text(json.dumps({"name": "theodore", "version": "0.3.2"}))
        self.assertEqual(self.client("guide", "pptx", env={"THEODORE_TEST_CLOCK_OFFSET": "14400"}).returncode, 0)
        self.assertEqual(self.api.heartbeats[-1]["plugin_version"], "0.3.2")

    def test_doctor_before_sign_in(self):
        self.client("setup", "--job")
        self.assertStatus(self.client("doctor"), "SIGN_IN_REQUIRED", 65)

    def test_status_json_and_waiting_for_setup(self):
        self.sign_in()
        snap = json.loads(self.client("status", "--json").stdout)
        self.assertFalse(snap["runtime_ready"])
        waited = self.client("status", "--wait", "60")
        self.box.wait_jobs()
        self.assertIn(waited.returncode, (0, 67))
        self.assertStatus(self.client("status"), "OK", 0)
        self.assertEqual(self.client("version").returncode, 0)

    def test_uninstall_removes_only_theodores_own_folders(self):
        foreign = self.box.home / "memory" / "notes.txt"            # the folder existed before Theodore, with someone else's data
        foreign.parent.mkdir(parents=True)
        foreign.write_text("not Theodore's")
        (self.box.home / "loose-file.txt").write_text("also not Theodore's")
        self.ready()
        self.assertEqual(foreign.read_text(), "not Theodore's")
        first, _n, _t = self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertIn("left alone", first)
        self.assertEqual(sorted(p.name for p in self.box.home.iterdir()), ["loose-file.txt", "memory"])
        self.assertEqual(foreign.read_text(), "not Theodore's")

    def test_uninstall_leaves_nothing_behind(self):
        self.ready()
        (self.box.home / "plugin.partial" / "plugins").mkdir(parents=True)          # an interrupted plugin unpack
        workspace = self.box.tmp / "ws"
        workspace.mkdir()
        self.client("pptx", "start", str(workspace))
        cache = self.box.user_home / "Library" / "Caches" / "pptx-runtime" if sys.platform == "darwin" else self.box.user_home / ".cache" / "pptx-runtime"
        cache.mkdir(parents=True)
        (cache / "x").write_text("1")
        result = self.client("uninstall")
        self.assertStatus(result, "OK", 0)
        self.assertFalse(self.box.home.exists())
        self.assertFalse(cache.exists())
        self.assertTrue((workspace / ".pptx-state" / "theodore-engine.json").exists(), "the user's workspaces are not touched")


class Hosts(ClientCase):
    def test_uninstall_says_what_it_could_not_remove(self):
        self.ready()
        bin_dir = self.box.tmp / "fakebin"
        bin_dir.mkdir()
        (bin_dir / "claude").write_text('#!/bin/sh\ncase "$*" in "plugin uninstall"*) echo "Error: plugin database is locked" >&2; exit 1;; esac\nexit 0\n')
        (bin_dir / "claude").chmod(0o755)
        self.box.env["PATH"] = f"{bin_dir}{os.pathsep}{self.box.env['PATH']}"
        result = self.client("uninstall")
        first, _n, tell = self.assertStatus(result, "RETRY", 75)
        self.assertIn("claude-code registration", first)
        self.assertIn("database is locked", first)
        self.assertNotIn("Theodore is uninstalled", tell)
        self.assertFalse((self.box.home / "engines").exists(), "the files are still removed")

    def _fake_tools(self, claude_first_rc: int = 0) -> Path:
        bin_dir = self.box.tmp / "fakebin"
        bin_dir.mkdir()
        log = self.box.tmp / "host-calls.log"
        for name in ("claude", "codex"):
            script = bin_dir / name
            script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n'
                              + (f'case "$*" in "plugin install theodore --marketplace"*) exit {claude_first_rc};; esac\n' if name == "claude" else "")
                              + 'case "$*" in "plugin list --json") echo "[]";; esac\nexit 0\n')
            script.chmod(0o755)
        self.box.env["PATH"] = f"{bin_dir}{os.pathsep}{self.box.env['PATH']}"
        return log

    def test_register_both_assistants(self):
        log = self._fake_tools()
        plugin = self.box.home / "plugin"
        (plugin / ".agents" / "plugins").mkdir(parents=True)
        (plugin / ".agents" / "plugins" / "marketplace.json").write_text("{}")
        result = self.client("hosts", "register")
        self.assertStatus(result, "OK", 0)
        calls = log.read_text().splitlines()
        self.assertIn(f"claude plugin install theodore --marketplace {self.api.base}/marketplace.json", calls)
        self.assertIn(f"codex plugin marketplace add {plugin}", calls)
        self.assertIn("codex plugin add theodore@theodore", calls)

    def test_claude_falls_back_to_two_commands(self):
        log = self._fake_tools(claude_first_rc=1)
        self.client("hosts", "register")
        calls = log.read_text().splitlines()
        self.assertIn(f"claude plugin marketplace add {self.api.base}/marketplace.json", calls)
        self.assertIn("claude plugin install theodore@theodore", calls)

    def test_settings_are_offered_then_applied_then_taken_back_exactly(self):
        self._fake_tools()
        settings = self.box.user_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        original = {"permissions": {"allow": ["Bash(git status)"]}, "model": "opus"}
        settings.write_text(json.dumps(original))
        offer = self.client("hosts", "settings")
        _f, nxt, tell = self.assertStatus(offer, "OK", 0)
        self.assertIn("only if they agree run", nxt)
        self.assertIn("hosts settings --apply", nxt)
        self.assertTrue(tell.startswith("TELL THE USER: Would you like"))
        self.assertEqual(json.loads(settings.read_text()), original, "nothing changes without consent")
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        applied = json.loads(settings.read_text())
        self.assertTrue(applied["extraKnownMarketplaces"]["theodore"]["autoUpdate"])
        launcher = str(self.box.home / "bin" / "theodore")
        self.assertIn(f"Bash({launcher} *)", applied["permissions"]["allow"])
        self.assertIn("Bash(git status)", applied["permissions"]["allow"])
        self.assertStatus(self.client("hosts", "unregister"), "OK", 0)
        self.assertEqual(json.loads(settings.read_text()), original, "exactly as before, no empty key left")

    # --- ruling 9: uninstall restores exactly what was there -----------------------------------------------------------
    def settings_file(self, value=None) -> Path:
        path = self.box.user_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if value is not None:
            path.write_text(json.dumps(value))
        return path

    def registering_tools(self, claude_markets: str = "", codex_markets: str = "") -> Path:
        """claude that adds enabledPlugins to settings.json on install and takes its own entry back on uninstall (leaving
        what it found), codex that creates an empty config.toml on add; both list marketplaces as given."""
        bin_dir = self.box.tmp / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        log = self.box.tmp / "host-calls.log"
        settings = self.settings_file()
        edit = (f'"{sys.executable}" -c "import json,sys,os; p=sys.argv[1]; d=json.load(open(p)) if os.path.exists(p) else {{}}; '
                f'e=d.setdefault(\'enabledPlugins\',{{}}); e.pop(\'theodore@theodore\',None) if sys.argv[2]==\'off\' else e.update({{\'theodore@theodore\':True}}); '
                f'json.dump(d,open(p,\'w\'))" "{settings}"')
        (bin_dir / "claude").write_text(f'#!/bin/sh\necho "claude $*" >> "{log}"\ncase "$*" in\n'
                                        f'"plugin list --json") echo "[]";;\n"plugin marketplace list") echo "{claude_markets}";;\n'
                                        f'"plugin install"*) {edit} on;;\n"plugin uninstall"*) {edit} off;;\nesac\nexit 0\n')
        codex_config = Path(self.box.env["CODEX_HOME"]) / "config.toml"
        (bin_dir / "codex").write_text(f'#!/bin/sh\necho "codex $*" >> "{log}"\ncase "$*" in\n"plugin marketplace list") echo "{codex_markets}";;\n'
                                       f'"plugin add"*) mkdir -p "{codex_config.parent}"; : >> "{codex_config}";;\nesac\nexit 0\n')
        for name in ("claude", "codex"):
            (bin_dir / name).chmod(0o755)
        self.box.env["PATH"] = f"{bin_dir}{os.pathsep}{self.box.env['PATH']}"
        plugin = self.box.home / "plugin"
        (plugin / ".agents" / "plugins").mkdir(parents=True, exist_ok=True)
        (plugin / ".agents" / "plugins" / "marketplace.json").write_text("{}")
        return log

    def test_a_prior_auto_update_false_is_restored_exactly(self):
        self._fake_tools()
        original = {"extraKnownMarketplaces": {"theodore": {"source": {"source": "github", "repo": "me/fork"}, "autoUpdate": False}},
                    "permissions": {"allow": ["Bash(ls)"], "deny": []}, "model": "opus"}
        path = self.settings_file(original)
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        self.assertIs(json.loads(path.read_text())["extraKnownMarketplaces"]["theodore"]["autoUpdate"], True)
        self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertEqual(json.loads(path.read_text()), original, "the user's own autoUpdate: false is back")

    def test_an_entry_theodore_created_and_the_user_changed_is_left_and_said(self):
        self._fake_tools()
        path = self.settings_file({"theme": "dark"})
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        changed = json.loads(path.read_text())
        changed["extraKnownMarketplaces"]["theodore"]["note"] = "mine"
        path.write_text(json.dumps(changed))
        first, _n, _t = self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertIn("changed after Theodore created it", first)
        after = json.loads(path.read_text())
        self.assertEqual(after["extraKnownMarketplaces"]["theodore"], changed["extraKnownMarketplaces"]["theodore"], "left as it is")
        self.assertNotIn("permissions", after, "Theodore's rules and the keys it created still go")
        self.assertEqual(after["theme"], "dark")

    def test_user_authored_empty_structures_stay_and_a_file_theodore_created_goes(self):
        self._fake_tools()
        authored = {"enabledPlugins": {}, "permissions": {"allow": []}, "extraKnownMarketplaces": {}}
        path = self.settings_file(authored)
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        self.assertStatus(self.client("hosts", "unregister"), "OK", 0)
        self.assertEqual(json.loads(path.read_text()), authored, "never removes keys or empty structures it did not create")
        path.unlink()
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        self.assertTrue(path.exists())
        self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertFalse(path.exists(), "a settings file Theodore created, empty again, goes")

    def test_what_registration_added_goes_and_what_was_there_stays(self):
        log = self.registering_tools()
        path = self.settings_file({"theme": "dark"})
        self.assertStatus(self.client("hosts", "register"), "OK", 0)
        self.assertIn("enabledPlugins", json.loads(path.read_text()))
        self.assertTrue((Path(self.box.env["CODEX_HOME"]) / "config.toml").exists())
        self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertEqual(json.loads(path.read_text()), {"theme": "dark"}, "the key the registration created goes once empty")
        self.assertFalse((Path(self.box.env["CODEX_HOME"]) / "config.toml").exists(), "the empty config the registration created goes")
        calls = log.read_text().splitlines()
        self.assertIn("claude plugin marketplace remove theodore", calls)
        self.assertIn("codex plugin marketplace remove theodore", calls)

    def test_marketplaces_and_keys_that_were_there_before_are_kept(self):
        log = self.registering_tools(claude_markets="theodore (https://x.test/marketplace.json)", codex_markets="theodore  ~/.theodore/plugin")
        codex_config = Path(self.box.env["CODEX_HOME"]) / "config.toml"
        codex_config.parent.mkdir(parents=True, exist_ok=True)
        codex_config.write_bytes(b"")
        path = self.settings_file({"enabledPlugins": {"other@x": True}})
        self.assertStatus(self.client("hosts", "register"), "OK", 0)
        first, _n, _t = self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertIn("was there before Theodore and was kept", first)
        calls = log.read_text().splitlines()
        self.assertNotIn("claude plugin marketplace remove theodore", calls)
        self.assertNotIn("codex plugin marketplace remove theodore", calls)
        self.assertIn("claude plugin uninstall theodore@theodore", calls, "the plugin itself is Theodore's and goes")
        self.assertEqual(json.loads(path.read_text()), {"enabledPlugins": {"other@x": True}})
        self.assertTrue(codex_config.exists(), "a config that was there before stays, even empty")

    def test_a_registration_that_was_there_before_is_recorded_as_not_ours_and_kept(self):
        # Round 8 (review 9): the plugin was already registered when Theodore's installer ran.
        log = self.registering_tools(claude_markets="theodore (https://x.test/marketplace.json)")
        claude = self.box.tmp / "fakebin" / "claude"
        claude.write_text(claude.read_text().replace('"plugin list --json") echo "[]";;', '"plugin list --json") echo \'[{"id": "theodore@theodore"}]\';;'))
        self.assertStatus(self.client("hosts", "register"), "OK", 0)
        record = json.loads((self.box.home / "state" / "host-settings.json").read_text())
        self.assertIs(record["claude_registered"], False)
        self.assertIs(record["claude_added_marketplace"], False)
        first, _n, _t = self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertIn("was there before Theodore's installer and was kept", first)
        calls = log.read_text().splitlines()
        self.assertNotIn("claude plugin uninstall theodore@theodore", calls)
        self.assertNotIn("claude plugin marketplace remove theodore", calls)

    def test_a_restored_record_goes_and_never_claims_a_file_the_user_made_later(self):
        # Round 8 (review 11): apply, take back, the user makes their own settings file, apply and take back again.
        self._fake_tools()
        path = self.settings_file()
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        self.assertStatus(self.client("hosts", "unregister"), "OK", 0)
        self.assertFalse(path.exists(), "the file Theodore created is gone again")
        self.assertFalse((self.box.home / "state" / "host-settings.json").exists(), "the record goes once everything is taken back")
        mine = {"permissions": {"allow": []}, "extraKnownMarketplaces": {}}
        path.write_text(json.dumps(mine))
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        self.assertStatus(self.client("hosts", "unregister"), "OK", 0)
        self.assertTrue(path.exists(), "the user's own file stays")
        self.assertEqual(json.loads(path.read_text()), mine)

    def test_a_created_file_the_user_rewrote_is_not_deleted(self):
        self._fake_tools()
        path = self.settings_file()
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        data = json.loads(path.read_text())
        time.sleep(0.05)
        path.write_text(json.dumps(data, indent=4))         # the user's editor rewrote it (same content, a new file)
        self.assertStatus(self.client("hosts", "unregister"), "OK", 0)
        self.assertTrue(path.exists(), "no longer the file Theodore left: it stays, emptied of Theodore's entries")
        self.assertEqual(json.loads(path.read_text()), {})

    def test_not_ours_survives_register_unregister_and_uninstall(self):
        # Round 9 (review 9): pre-existing registrations -> register -> unregister -> register again -> uninstall: the
        # registrations that were there before are never removed, for Claude Code and for Codex.
        log = self.registering_tools(claude_markets="theodore (https://x.test/marketplace.json)", codex_markets="theodore  ~/.theodore/plugin")
        claude = self.box.tmp / "fakebin" / "claude"
        claude.write_text(claude.read_text().replace('"plugin list --json") echo "[]";;', '"plugin list --json") echo \'[{"id": "theodore@theodore"}]\';;'))
        codex = self.box.tmp / "fakebin" / "codex"
        codex.write_text(codex.read_text().replace('case "$*" in\n', 'case "$*" in\n"plugin list") echo "theodore@theodore  enabled";;\n', 1))
        self.assertStatus(self.client("hosts", "register"), "OK", 0)
        self.assertStatus(self.client("hosts", "unregister"), "OK", 0)
        record = json.loads((self.box.home / "state" / "host-settings.json").read_text())
        self.assertEqual(record, {"claude_added_marketplace": False, "claude_registered": False, "codex_added_marketplace": False, "codex_registered": False})
        self.assertStatus(self.client("hosts", "register"), "OK", 0)            # a repeated successful plugin add: still not ours
        self.assertIs(json.loads((self.box.home / "state" / "host-settings.json").read_text())["codex_registered"], False)
        self.assertStatus(self.client("uninstall"), "OK", 0)
        calls = log.read_text().splitlines()
        for removal in ("claude plugin uninstall theodore@theodore", "claude plugin marketplace remove theodore",
                        "codex plugin remove theodore@theodore", "codex plugin marketplace remove theodore"):
            self.assertNotIn(removal, calls)

    def test_a_created_file_the_user_replaced_with_an_empty_one_is_theirs(self):
        # Round 9 (review 11): Theodore creates settings, the user replaces them with {}, settings are applied again,
        # then taken back: the user's file stays.
        self._fake_tools()
        path = self.settings_file()
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        path.unlink()
        path.write_text("{}")
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        self.assertStatus(self.client("hosts", "unregister"), "OK", 0)
        self.assertTrue(path.exists(), "the user's replacement stays")
        self.assertEqual(json.loads(path.read_text()), {})

    def test_broken_settings_file_is_left_alone(self):
        self._fake_tools()
        settings = self.box.user_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{ not json")
        self.client("hosts", "settings", "--apply")
        self.assertEqual(settings.read_text(), "{ not json")


@unittest.skipUnless(sys.platform == "darwin", "the disk-image route is macOS only")
class LibreOffice(ClientCase):
    def test_install_from_a_disk_image_with_a_pinned_checksum(self):
        self.ready()
        stage = self.box.tmp / "dmg-src" / "LibreOffice.app" / "Contents" / "MacOS"
        stage.mkdir(parents=True)
        (stage / "soffice").write_text("#!/bin/sh\necho fake soffice\n")
        (stage / "soffice").chmod(0o755)
        dmg = self.box.tmp / "LibreOffice_fake.dmg"
        subprocess.run(["/usr/bin/hdiutil", "create", "-quiet", "-srcfolder", str(self.box.tmp / "dmg-src"), "-volname", "LibreOffice",
                        "-format", "UDZO", str(dmg)], check=True, capture_output=True)
        data = dmg.read_bytes()
        # the mirror on the download host does not have it (404): the second source, the publisher's own URL, does
        self.api.files["upstream/LibreOffice_fake.dmg"] = data
        pin = {"version": "0.0.1", "url": f"{self.api.base}/dl/upstream/LibreOffice_fake.dmg", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        state_path = self.box.home / "state" / "update.json"
        state = json.loads(state_path.read_text())
        state["tools"] = {"libreoffice": {"darwin-arm64": pin, "darwin-x64": pin}}
        state_path.write_text(json.dumps(state))
        result = self.client("tools", "install", "libreoffice", "--job")
        self.assertStatus(result, "OK", 0)
        tried = [r["path"] for r in self.api.requests if "LibreOffice_fake.dmg" in r["path"]]
        self.assertEqual(tried, ["/dl/tools/LibreOffice_fake.dmg", "/dl/upstream/LibreOffice_fake.dmg"], "mirror first, publisher second")
        binary = self.box.home / "tools" / "libreoffice" / "LibreOffice.app" / "Contents" / "MacOS" / "soffice"
        self.assertTrue(os.access(binary, os.X_OK))
        self.assertEqual(sorted(p.name for p in (self.box.home / "staging").iterdir()), [])
        mounts = subprocess.run(["/sbin/mount"], capture_output=True, text=True).stdout
        self.assertNotIn(str(self.box.home), mounts, "the disk image is detached")
        state["tools"]["libreoffice"]["darwin-arm64"] = state["tools"]["libreoffice"]["darwin-x64"] = {**pin, "sha256": "0" * 64, "version": "0.0.2"}
        state_path.write_text(json.dumps(state))
        first, _n, _t = self.assertStatus(self.client("tools", "install", "libreoffice", "--job"), "SETUP_FAILED", 68)
        self.assertIn("cause HASH_MISMATCH", first)
        self.assertStatus(self.client("tools", "install", "word"), "USAGE", 64)


class HostTest(ClientCase):
    """What a real install against staging found (hand-back H, "Defects")."""

    PRODUCTION = re.compile(r"(?<![A-Za-z0-9.@-])theodoreand\.ai")

    def gone(self):
        self.assertFalse((self.box.home / "state" / "credentials").exists(), "the credential is deleted")
        self.assertFalse((self.box.home / "state" / "entitlement.json").exists(), "the stored licence is deleted")

    def revoke(self):
        for inst in self.api.installations.values():
            inst["revoked"] = True

    # --- 3: revocation -----------------------------------------------------------------------------------------------
    def test_a_revoked_device_loses_the_pptx_guide_on_its_first_call(self):
        self.ready()
        self.assertEqual(self.client("guide", "pptx").returncode, 0)
        self.revoke()
        result = self.client("guide", "pptx")              # the six-hour update check is not due: the guide still asks
        self.assertStatus(result, "SIGN_IN_REQUIRED", 65)
        self.assertNotIn("=== THEODORE pptx", result.stdout, "no instructions for a revoked device")
        self.gone()
        workspace = self.box.tmp / "ws"
        workspace.mkdir()
        self.assertStatus(self.client("pptx", "start", str(workspace)), "SIGN_IN_REQUIRED", 65)

    def test_every_command_that_learns_of_a_revoke_clears_the_sign_in(self):
        for command in (["update", "--check"], ["update", "--apply"], ["prefs", "list"], ["packs", "install", "--all"],
                        ["report", "--kind", "feedback", "--title", "A title", "--details", "Some details", "--origin", "user_requested"],
                        ["specimen", "cover-01.jpg"], ["guide", "deck-design"], ["guide", "pptx", "--part", "1"]):
            with self.subTest(command=" ".join(command)):
                self.box.close()
                self.box = Sandbox(self.api)
                self.box.install_client_directly()
                self.api.installations.clear()
                self.api.activation_codes.clear()
                self.ready()
                self.revoke()
                work = self.box.tmp / "work"            # a working folder that does not hold the temporary folder (specimens)
                work.mkdir()
                result = self.client(*command, cwd=work)
                self.assertStatus(result, "SIGN_IN_REQUIRED", 65)
                self.gone()
                workspace = self.box.tmp / "ws"
                workspace.mkdir()
                self.assertStatus(self.client("pptx", "start", str(workspace)), "SIGN_IN_REQUIRED", 65)
                self.assertStatus(self.client("guide", "pptx"), "SIGN_IN_REQUIRED", 65)

    def test_a_suspended_account_loses_its_stored_licence_on_any_route(self):
        self.ready()
        self.api.account_state = "suspended"
        self.assertStatus(self.client("update", "--check"), "NO_ENTITLEMENT", 70)
        self.assertFalse((self.box.home / "state" / "entitlement.json").exists())
        self.assertTrue((self.box.home / "state" / "credentials").exists(), "a 403 keeps the device signed in")

    # --- the browser opens only when asked ---------------------------------------------------------------------------------
    def test_only_an_explicit_sign_in_command_opens_the_browser(self):
        opener = self.box.tmp / "fake-browser"
        log = self.box.tmp / "opened.log"
        opener.write_text(f'#!/bin/sh\necho "$1" >> "{log}"\n')
        opener.chmod(0o755)
        env = {"THEODORE_NO_BROWSER": "", "THEODORE_TEST_BROWSER": str(opener)}       # the seam: this program instead of the system's
        for command in (["guide", "pptx"], ["guide", "deck-design"], ["prefs", "list"], ["specimen", "a.jpg"], ["pptx", "version"]):
            self.assertStatus(self.client(*command, env=env), "SIGN_IN_REQUIRED", 65)
        time.sleep(0.5)
        self.assertFalse(log.exists(), "a command other than login opened the browser")
        _first, _nxt, tell = self.assertStatus(self.client("guide", "pptx", env=env), "SIGN_IN_REQUIRED", 65)
        self.assertIn("Please open https://theodoreand.ai/device?code=", tell)
        for command in (["login"], ["login", "--start"], ["login", "--finish"]):      # a sign-in, but no request to open anything
            if command[-1] == "--finish":           # nothing pending: it starts a sign-in
                (self.box.home / "state" / "device.json").unlink()
            _first, nxt, tell = self.assertStatus(self.client(*command, env=env), "SIGN_IN_REQUIRED", 65)
            self.assertIn("/device?code=", nxt)
        time.sleep(0.5)
        self.assertFalse(log.exists(), "only login --open opens the browser")
        _first, _nxt, tell = self.assertStatus(self.client("login", "--open", env=env), "SIGN_IN_REQUIRED", 65)
        deadline = time.time() + 10
        while not log.exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(log.exists(), "login --open opens the page")
        self.assertIn("/device?code=", log.read_text())
        self.assertIn("I opened a browser page", tell)

    # --- 1: no production host in what a stamped build prints -----------------------------------------------------------------
    def test_texts_name_the_stamped_site(self):
        self.ready()
        outputs = []
        outputs.append(self.client("prefs", "add", "Always use British spelling").stdout)
        outputs.append(self.client("update", "--channel", "beta").stdout)
        outputs.append(self.client("report", "--kind", "bug", "--title", "A title", "--details", "Some details", "--origin", "user_requested").stdout)
        self.api.features = ["deck-design"]
        self.client("logout")
        self.assertStatus(self.client("login", "--code", self.api.issue_activation_code("EFGH-5678")), "OK", 0)
        outputs.append(self.client("guide", "pptx").stdout)                       # the plan has no pptx
        self.api.features = ["pptx", "deck-design"]
        self.api.account_state = "suspended"
        outputs.append(self.client("prefs", "list").stdout)
        self.api.account_state = "active"
        bin_dir = self.box.tmp / "fakebin"
        bin_dir.mkdir()
        (bin_dir / "claude").write_text('#!/bin/sh\ncase "$*" in "plugin list"*) echo "[]"; exit 0;; esac\necho "Error: no" >&2\nexit 1\n')
        (bin_dir / "claude").chmod(0o755)
        outputs.append(self.client("hosts", "register", env={"PATH": f"{bin_dir}{os.pathsep}{self.box.env['PATH']}"}).stdout)
        text = "\n".join(outputs)
        for path in ("/app/preferences", "/app/plan", "/app/reports", "/install"):
            self.assertIn(SITE.split("//")[1] + path, text)
        self.assertEqual(self.PRODUCTION.findall(text), [], text)

    def test_no_client_source_names_the_production_site(self):
        root = Path(__file__).resolve().parents[1] / "client" / "theodore_client"
        for path in sorted(root.glob("*.py")):
            self.assertEqual(self.PRODUCTION.findall(path.read_text()), [], path.name)

    # --- 4: guide sections ------------------------------------------------------------------------------------------------------
    def test_a_section_is_found_by_name_file_or_path_and_a_missing_one_lists_the_others(self):
        self.sign_in()
        self.api.guides["deck-design"][""] = ("# Deck design\n\nRead [craft](references/craft.md) now, then [forms](references/forms.md) "
                                              "and [worked notes](references/worked-notes.md).\n")
        self.api.guides["deck-design"]["craft"] = "## Craft\n\nCRAFT-MARKER see [critique](references/critique.md)\n"
        self.api.guides["deck-design"]["worked-notes"] = "## Notes\n"
        launcher = f'"{self.box.home / "bin" / "theodore"}"'
        for form in ("craft", "craft.md", "references/craft.md", "./references/craft.md", "references/craft"):
            result = self.client("guide", "deck-design", form)
            self.assertEqual(result.returncode, 0, form + result.stdout)
            self.assertIn("CRAFT-MARKER", result.stdout)
            self.assertEqual(result.stdout.splitlines()[1], "--- craft ---")
        self.assertIn(f"[critique](run: {launcher} guide deck-design critique)", self.client("guide", "deck-design", "craft.md").stdout)
        main = self.client("guide", "deck-design").stdout
        self.assertIn(f"Read [craft](run: {launcher} guide deck-design craft) now, then [forms](run: {launcher} guide deck-design forms) "
                      f"and [worked notes](run: {launcher} guide deck-design worked-notes).", main)
        self.assertNotIn("](references/", main)
        for missing in ("enrich", "references/enrich.md"):
            first, nxt, _tell = self.assertStatus(self.client("guide", "deck-design", missing), "USAGE", 64)
            self.assertIn("no section 'enrich'", first)
            self.assertIn("craft, forms, worked-notes", first)
            self.assertIn(f"{launcher} guide deck-design <section>", nxt)
        first, _n, _t = self.assertStatus(self.client("guide", "deck-design", "../../etc/passwd"), "USAGE", 64)
        self.assertIn("craft, forms, worked-notes", first)
        self.assertEqual([r["path"] for r in self.api.requests if "passwd" in r["path"] or ".." in r["path"]], [])

    # --- 5: uninstall with an assistant that never had Theodore ------------------------------------------------------------------
    def test_uninstall_with_codex_never_registered_is_a_clean_removal(self):
        self.ready()
        bin_dir = self.box.tmp / "fakebin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text('#!/bin/sh\ncase "$*" in\n"plugin remove"*) echo "Error: plugin \'theodore\' is not installed" >&2; exit 1;;\n'
                                       '"plugin marketplace remove"*) echo "Error: marketplace \'theodore\' is not configured or installed" >&2; exit 1;;\nesac\nexit 0\n')
        (bin_dir / "codex").chmod(0o755)
        self.box.env["PATH"] = f"{bin_dir}{os.pathsep}{self.box.env['PATH']}"
        codex_home = Path(self.box.env["CODEX_HOME"])
        (codex_home / "plugins" / "cache" / "theodore").mkdir(parents=True)
        (codex_home / "plugins" / "cache" / "other").mkdir()
        (codex_home / "config.toml").write_bytes(b"")
        settings = Path(self.box.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        authored = {"enabledPlugins": {}, "extraKnownMarketplaces": {}, "permissions": {"allow": []}, "theme": "dark"}
        settings.write_text(json.dumps(authored))
        self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertFalse((codex_home / "plugins" / "cache" / "theodore").exists(), "Codex's empty cache folder for Theodore goes")
        self.assertTrue((codex_home / "plugins" / "cache" / "other").exists())
        self.assertTrue((codex_home / "config.toml").exists(), "a 0-byte config Theodore did not create stays (ruling 9)")
        self.assertEqual(json.loads(settings.read_text()), authored, "empty structures Theodore did not create stay (ruling 9)")

    def test_uninstall_keeps_a_codex_config_and_settings_that_hold_something(self):
        self.ready()
        codex_home = Path(self.box.env["CODEX_HOME"])
        (codex_home / "plugins" / "cache" / "theodore" / "kept").mkdir(parents=True)
        (codex_home / "config.toml").write_text("model = 'x'\n")
        settings = Path(self.box.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        kept = {"enabledPlugins": {"a@b": True}, "extraKnownMarketplaces": {"b": {}}, "permissions": {"allow": ["Bash(ls)"], "deny": []}}
        settings.write_text(json.dumps(kept))
        self.assertStatus(self.client("uninstall"), "OK", 0)
        self.assertTrue((codex_home / "plugins" / "cache" / "theodore" / "kept").exists())
        self.assertEqual((codex_home / "config.toml").read_text(), "model = 'x'\n")
        self.assertEqual(json.loads(settings.read_text()), kept)

    def test_what_uninstall_could_not_remove_is_not_sent_to_a_private_page(self):
        self.ready()
        bin_dir = self.box.tmp / "fakebin"
        bin_dir.mkdir()
        (bin_dir / "claude").write_text('#!/bin/sh\ncase "$*" in "plugin uninstall"*) echo "Error: plugin database is locked" >&2; exit 1;; esac\nexit 0\n')
        (bin_dir / "claude").chmod(0o755)
        self.box.env["PATH"] = f"{bin_dir}{os.pathsep}{self.box.env['PATH']}"
        result = self.client("uninstall")
        self.assertStatus(result, "RETRY", 75)
        self.assertNotIn("github.com", result.stdout)
        root = Path(__file__).resolve().parents[1]
        for path in [*sorted((root / "client" / "theodore_client").glob("*.py")), root / "plugins/theodore/skills/pptx/scripts/theodore",
                     root / "plugins/theodore/skills/pptx/scripts/theodore.cmd", root / "install/install.sh", root / "install/install.cmd"]:
            self.assertNotIn("github.com/Theodore-and", path.read_text(encoding="utf-8"), path.name)

    # --- 6: no pack published ----------------------------------------------------------------------------------------------------
    def test_with_no_pack_published_nothing_claims_a_download(self):
        self.api.packs, self.api.pack_files = [], {}
        self.ready()
        none = "no asset packs are published for this channel yet"
        first, nxt, tell = self.assertStatus(self.client("packs", "status"), "OK", 0)
        self.assertIn(none, first)
        self.assertNotIn("packs install", nxt)
        self.assertNotIn("fetching", tell)
        for command in (["packs", "install", "--all"], ["packs", "install", "icons"], ["packs", "install", "--all", "--background"]):
            first, _n, tell = self.assertStatus(self.client(*command), "OK", 0)
            self.assertIn(none, first)
            self.assertNotIn("None", first)
            self.assertNotIn("downloading", first + tell)
        self.box.wait_jobs()
        self.assertFalse(self.state("state/jobs/packs.json").get("failed"), "the background job does not fail on an empty list")
        first, _n, _t = self.assertStatus(self.client("status"), "OK", 0)
        self.assertIn(none, first)
        self.assertNotIn("arriving", first)
        doctor = self.client("doctor")
        self.assertNotIn("still downloading in the background", doctor.stdout)
        self.assertIn(none, doctor.stdout)
        self.api.publish_pack("icons", "1.0.0", pack_zip("icons", "1.0.0"))
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}
        self.api.clock_offset = 7 * 3600
        self.assertEqual(self.client("guide", "pptx", env=later).returncode, 0)
        self.box.wait_jobs()
        self.assertEqual(self.state("packs/state.json")["icons"]["status"], "ready", "a pack published later still arrives")
        first, _n, _t = self.assertStatus(self.client("packs", "install", "nonsense"), "USAGE", 64)
        self.assertIn("Packs: icons", first)

    # --- 9, 10: statuses ------------------------------------------------------------------------------------------------------------
    def test_an_automatic_report_prints_three_lines(self):
        self.ready()
        result = self.client("report", "--kind", "bug", "--title", "A title", "--details", "Some details")
        first, nxt, tell = self.assertStatus(result, "OK", 0)
        self.assertIn("automatic report filed", first)
        self.assertIn("nothing", tell)
        self.api.auto_reports = False
        self.client("logout")
        self.assertStatus(self.client("login", "--code", self.api.issue_activation_code("EFGH-5678")), "OK", 0)
        _f, _n, tell = self.assertStatus(self.client("report", "--kind", "bug", "--title", "A title", "--details", "Some details"), "BLOCKED_BY_POLICY", 73)
        self.assertIn("nothing", tell)

    def test_rollback_with_nothing_to_go_back_to_and_channel_are_not_wrong_commands(self):
        self.ready()
        for command, words in ((["update", "--rollback"], "no earlier engine"), (["update", "--channel", "beta"], "belongs to the account")):
            result = self.client(*command)
            first, _nxt, tell = self.assertStatus(result, "OK", 0)
            self.assertIn(words, first)
            self.assertIn("nothing", first)
            self.assertNotIn("wrong command", tell)
        self.assertEqual(self.state("engines/current.json")["version"], self.ENGINE_VERSION)


class Revocation(ClientCase):
    """Ruling 5: whatever command learns that this device was revoked clears the credential and the licence and ends
    with SIGN_IN_REQUIRED 65 and the sign-in block, whatever else happened afterwards; nothing else is printed."""

    LONG = "".join(f"line {i:05d} " + "x" * 80 + "\n" for i in range(400))      # two parts

    def fresh(self):
        self.box.close()
        self.box = Sandbox(self.api)
        self.box.install_client_directly()
        self.api.installations.clear()
        self.api.activation_codes.clear()
        self.api.drop_paths = set()
        self.ready()

    def heartbeat_due(self):
        path = self.box.home / "state" / "update.json"
        state = json.loads(path.read_text())
        state["last_heartbeat"] = 0
        state["last_check"] = state["last_attempt"] = int(time.time())
        path.write_text(json.dumps(state))

    def switch_unknown(self):
        path = self.box.home / "state" / "entitlement.json"
        record = json.loads(path.read_text())
        record.pop("auto_reports", None)
        path.write_text(json.dumps(record))

    def offline_sign_in(self):
        self.api.drop_paths = {"/v1/device/code"}

    def test_every_command_that_contacts_the_service(self):
        report = ["report", "--kind", "bug", "--title", "A title", "--details", "Some details"]
        table = [
            (["update", "--check"], None), (["update", "--apply"], None), (["update", "--reset-state"], None),
            (["prefs", "list"], None), (["prefs", "add", "Use British spelling"], None), (["prefs", "remove", "p1"], None),
            (["packs", "install", "--all"], None), (report + ["--origin", "user_requested"], None),
            (report, self.switch_unknown),                                   # automatic: the licence refresh learns it
            (["specimen", "cover-01.jpg"], None), (["guide", "deck-design"], None), (["guide", "deck-design", "craft"], None),
            (["guide", "pptx"], None), (["guide", "pptx", "--part", "2"], self.heartbeat_due),     # only the heartbeat asks
            (["setup", "--job"], None),
            (["prefs", "list"], self.offline_sign_in),                       # and the new sign-in cannot start
        ]
        self.api.publish_engine("1.0.1", engine_zip("1.0.1", skill_text=self.LONG))
        for command, prepare in table:
            with self.subTest(command=" ".join(command), prepare=getattr(prepare, "__name__", None)):
                self.fresh()
                self.api.specimens = {"cover-01.jpg": JPEG}
                self.api.guides["deck-design"]["craft"] = "## Craft\n"
                if prepare:
                    prepare()
                self.api.revoke_all()
                work = self.box.tmp / "work"            # a working folder that does not hold the temporary folder (specimens)
                work.mkdir()
                result = self.client(*command, cwd=work)
                first, nxt, tell = self.assertStatus(result, "SIGN_IN_REQUIRED", 65)
                self.assertEqual(sum(line.startswith("THEODORE ") for line in result.stdout.splitlines()), 1, result.stdout)
                self.assertNotIn("=== THEODORE", result.stdout, "no guide text once revoked")
                self.assertNotIn(str(self.box.env["TMPDIR"]), result.stdout, "no specimen path once revoked")
                self.assertIn("signed out", first)
                if prepare == self.offline_sign_in:
                    self.assertIn("could not be reached", nxt)
                    self.assertIn("login --start", nxt)
                else:
                    self.assertIn("/device?code=", nxt)
                self.assertFalse((self.box.home / "state" / "credentials").exists(), "the credential is deleted")
                self.assertFalse((self.box.home / "state" / "entitlement.json").exists(), "the stored licence is deleted")
                self.api.drop_paths = set()
                workspace = self.box.tmp / "ws"
                workspace.mkdir()
                self.assertStatus(self.client("pptx", "start", str(workspace)), "SIGN_IN_REQUIRED", 65)


class Environments(ClientCase):
    """Ruling 6, in the client: a build stamped for another service refuses this folder; printed links come from the
    running build's stamp."""

    def skill(self, **overrides) -> dict:
        folder = self.box.tmp / "other build" / "skills" / "pptx"
        self.box.write_launcher_json(into=folder / "scripts", **overrides)
        return {"THEODORE_SKILL_DIR": str(folder)}

    def test_a_build_of_another_service_refuses_and_uninstall_is_allowed(self):
        self.ready()
        self.assertEqual((self.box.home / "state" / "environment").read_text().strip(), self.api.base)
        env = self.skill(api_base="https://api.staging.example.test")
        for command in (["guide", "pptx"], ["version"], ["prefs", "list"], ["login", "--start"]):
            first, nxt, _tell = self.assertStatus(self.client(*command, env=env), "USAGE", 64)
            self.assertIn("https://api.staging.example.test", first)
            self.assertIn(self.api.base, first)
            self.assertIn("THEODORE_HOME", nxt)
        self.assertEqual(len([r for r in self.api.requests if "staging" in r["path"]]), 0)
        self.assertIn(self.client("uninstall", env=env).returncode, (0, 75))
        self.assertFalse((self.box.home / "engines").exists())

    def test_printed_links_come_from_the_running_build(self):
        self.ready()
        env = self.skill(site_base="https://running-build.example.test")
        _f, nxt, tell = self.assertStatus(self.client("update", "--channel", "beta", env=env), "OK", 0)
        self.assertIn("https://running-build.example.test/app/plan", nxt)
        self.assertIn("running-build.example.test/app/plan", tell)


class Suspension(Revocation):
    """Round 8, ruling 5: a suspended account (403 account_suspended) goes through the same central handler: every
    command that learns it ends with NO_ENTITLEMENT 70, prints no instructions, and deletes the licence. The credential
    is kept on purpose (the contract's 403: the installation is still valid and a suspension can be lifted)."""

    def test_every_command_that_contacts_the_service(self):
        report = ["report", "--kind", "bug", "--title", "A title", "--details", "Some details"]
        table = [(["update", "--check"], None), (["prefs", "list"], None), (report + ["--origin", "user_requested"], None),
                 (report, self.switch_unknown), (["guide", "deck-design"], None), (["guide", "pptx"], None),
                 (["guide", "pptx", "--part", "2"], self.heartbeat_due), (["setup", "--job"], None), (["specimen", "cover-01.jpg"], None)]
        self.api.publish_engine("1.0.1", engine_zip("1.0.1", skill_text=self.LONG))
        for command, prepare in table:
            with self.subTest(command=" ".join(command), prepare=getattr(prepare, "__name__", None)):
                self.fresh()
                self.api.specimens = {"cover-01.jpg": JPEG}
                if prepare:
                    prepare()
                self.api.account_state = "suspended"
                work = self.box.tmp / "work"
                work.mkdir()
                result = self.client(*command, cwd=work)
                first, _nxt, _tell = self.assertStatus(result, "NO_ENTITLEMENT", 70)
                self.assertEqual(sum(line.startswith("THEODORE ") for line in result.stdout.splitlines()), 1, result.stdout)
                self.assertNotIn("=== THEODORE", result.stdout, "no instructions after a suspension")
                self.assertIn("suspended", first)
                self.assertTrue((self.box.home / "state" / "credentials").exists(), "the credential is kept: a suspension can be lifted")
                self.assertFalse((self.box.home / "state" / "entitlement.json").exists(), "the stored licence is deleted")
                self.api.account_state = "active"


class Internals(unittest.TestCase):
    def test_ed25519_rfc8032_vectors(self):
        from theodore_client import ed25519
        from fake_api import public_key, sign
        vectors = [
            ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
             "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
            ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
             "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
        ]
        for seed, pub, message, signature in vectors:
            seed_b, pub_b, msg_b, sig_b = bytes.fromhex(seed), bytes.fromhex(pub), bytes.fromhex(message), bytes.fromhex(signature)
            self.assertEqual(public_key(seed_b), pub_b)
            self.assertEqual(sign(seed_b, msg_b), sig_b)
            self.assertTrue(ed25519.verify(pub_b, msg_b, sig_b))
            self.assertFalse(ed25519.verify(pub_b, msg_b + b"x", sig_b))
            self.assertFalse(ed25519.verify(pub_b, msg_b, sig_b[:-1] + bytes([sig_b[-1] ^ 1])))
        self.assertFalse(ed25519.verify(b"\x00" * 31, b"", b"\x00" * 64))

    def test_pack_detection_for_every_documented_helper(self):
        from theodore_client.install import PACK_HELPERS, packs_in_script
        self.assertEqual(sorted(PACK_HELPERS), ["backgrounds", "flags", "icons", "maps"])
        for name in ("safeIcon", "iconToBase64Png", "getIconMeta", "resolveIconId", "getCatalog"):
            self.assertIn("icons", packs_in_script(f"const x = await {name}('rocket');"), name)
        for name in ("flagToBase64Png", "getFlagMeta", "getFlagCatalog"):
            self.assertEqual(packs_in_script(f"const x = await {name}('ae');"), {"flags"}, name)
        self.assertEqual(packs_in_script("slide.addMap({ country: 'AE' });"), {"maps"})
        for name in ("backgroundToBase64", "getBackgroundMeta", "listBackgrounds"):
            self.assertEqual(packs_in_script(f"const x = {name} ('paper');"), {"backgrounds"}, name)
        # a helper the list does not know yet still counts when its name carries the pack's word
        self.assertEqual(packs_in_script("const x = flagStrip(['ae', 'sa']);"), {"flags"})
        self.assertEqual(packs_in_script("icons.renderIconGrid(slide);"), {"icons"})
        # ordinary JavaScript is not a pack call
        for plain in ("rows.map((r) => r.total)", "const seen = new Map();", "slide.background = { color: 'FFFFFF' };",
                      "const flags = { draft: true }; if (flags.draft) {}", "const iconName = 'rocket';"):
            self.assertEqual(packs_in_script(plain), set(), plain)

    def test_split_parts_gives_the_text_back(self):
        from theodore_client.commands import split_parts
        text = "".join(f"paragraph {i}\n" + "y" * (i * 37 % 900) + "\n\n" for i in range(200)) + "z" * 30000
        parts = split_parts(text, 24000)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < len(p) <= 24000 for p in parts))
        self.assertEqual(split_parts("short"), ["short"])

    def test_version_order(self):
        from theodore_client.core import version_key
        ordered = ["0.9.0", "1.0.0-rc.2", "1.0.0-rc.3", "1.0.0-rc.10", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
        self.assertEqual(sorted(ordered[::-1], key=version_key), ordered)

    def test_scrub_removes_bare_script_and_document_names_too(self):
        from theodore_client.commands import scrub
        self.assertEqual(scrub("build failed in Acme Holdings deck.js line 4"), "build failed in [file] line 4")
        self.assertEqual(scrub("cannot open x.pptx"), "cannot open [file]")
        self.assertEqual(scrub("error in Northwind slides.py while reading Board notes.md"), "error in [file] while reading [file]")
        self.assertEqual(scrub("E_LAYOUT: overflow on slide 4 at 12pt (engine 1.4.0)"), "E_LAYOUT: overflow on slide 4 at 12pt (engine 1.4.0)")

    def test_scrub(self):
        from theodore_client.commands import scrub
        text = scrub(r"failed at C:\Users\omar\Decks\Acme Q3.pptx and /home/omar/x.py, mail omar@example.com")
        for leaked in ("omar", "Q3.pptx", "example.com"):
            self.assertNotIn(leaked, text)

    def test_canonical_json_matches_the_contract_vector(self):
        from fake_api import canonical_json
        manifest = {"schema": "theodore-runtime-manifest@1", "runtime_id": "rt-0001", "platform": "darwin", "arch": "arm64",
                    "parts": [{"part": "python", "version": None, "url": "https://dl.theodoreand.ai/runtime/rt-0001/darwin-arm64/python.tar.gz", "sha256": "a" * 64, "size": 123},
                              {"part": "client", "version": "0.1.0", "url": "https://dl.theodoreand.ai/runtime/rt-0001/any/client.tar.xz", "sha256": "b" * 64, "size": 45}],
                    "server_time": "2026-11-09T19:00:00.000Z"}
        self.assertEqual(canonical_json(manifest),
                         '{"arch":"arm64","parts":[{"part":"python","sha256":"' + "a" * 64 + '","size":123,"url":"https://dl.theodoreand.ai/runtime/rt-0001/darwin-arm64/python.tar.gz","version":null},'
                         '{"part":"client","sha256":"' + "b" * 64 + '","size":45,"url":"https://dl.theodoreand.ai/runtime/rt-0001/any/client.tar.xz","version":"0.1.0"}],'
                         '"platform":"darwin","runtime_id":"rt-0001","schema":"theodore-runtime-manifest@1","server_time":"2026-11-09T19:00:00.000Z"}')

    def test_exit_codes_are_the_frozen_ones(self):
        from theodore_client.contract import EXIT
        self.assertEqual(EXIT, {"OK": 0, "USAGE": 64, "SIGN_IN_REQUIRED": 65, "SIGN_IN_PENDING": 66, "SETUP_IN_PROGRESS": 67, "SETUP_FAILED": 68,
                                "UNSUPPORTED_PLATFORM": 69, "NO_ENTITLEMENT": 70, "OFFLINE": 71, "UPDATE_REQUIRED": 72, "BLOCKED_BY_POLICY": 73,
                                "CONNECTOR_REQUIRED": 74, "RETRY": 75, "INTERNAL": 78})

    def test_status_is_always_three_single_lines(self):
        from theodore_client.core import Status
        text = Status("RETRY", "a\nb", "c\r\nd", "e\tf").render()
        self.assertEqual(text.splitlines(), ["THEODORE RETRY: a b", "NEXT: c d", "TELL THE USER: e f"])
        # Automatic reports are silent: the third line says there is nothing to tell the user.
        self.assertEqual(Status("OK", "a", "b", None).render().splitlines(), ["THEODORE OK: a", "NEXT: b", "TELL THE USER: nothing; do not mention this to the user."])


if __name__ == "__main__":
    unittest.main()
