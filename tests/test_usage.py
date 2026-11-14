"""Usage counts (lane ANALYTICS, client side): the ledger, its limits, the batches sent to POST /v1/usage and what each
answer does, the engine's environment, the client's own statuses and `theodore usage --show`.

In process against the fake API where the behaviour is the client's alone; through the client's command line where it
is what a command does (the engine's environment, the end of pptx finish, a slow service)."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase, FakeApi  # noqa: E402
from test_hardening import client_home  # noqa: E402

from theodore_client import account, api, cli, contract, core, credentials, install, usage  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Strings that look like a client's work. They must never reach a batch, a log line or the screen's JSON.
CLIENT_LIKE = ("Acme Confidential Board", "acme-board-q3.pptx", "/Users/someone/Acme Confidential Board")


def now_ms(offset_s: float = 0) -> int:
    return int((time.time() + offset_s) * 1000)


def command_event(offset_s: float = 0, **extra) -> dict:
    event = {"type": "command", "event_id": str(uuid.uuid4()), "ts": now_ms(offset_s), "verb": "finish",
             "deck_id": uuid.uuid4().hex, "workspace": "task", "kind": "template", "outcome": "ok", "code": None,
             "duration_ms": 9400, "slides_total": 12, "slides_added": 3, "slides_changed": 5}
    event.update(extra)
    return event


class InProcess(unittest.TestCase):
    """A signed-in scratch home pointed at the fake API, usage switched on by a licence refresh."""

    def setUp(self):
        self.api = FakeApi().start()
        self.addCleanup(self.api.stop)
        ctx = client_home(api_base=self.api.base)
        self.home = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        core.home().ensure()
        bundle = self.api._bundle({})
        credentials.store(bundle["installation_id"], bundle["installation_secret"])
        self.api.usage_enabled = True
        account.refresh(timeout=5)
        self.assertTrue(usage.enabled())

    def write(self, *events, raw: list[str] = ()) -> None:
        with open(usage.ledger_path(), "a", encoding="utf-8") as ledger:
            for event in events:
                ledger.write(json.dumps(event) + "\n")
            for line in raw:
                ledger.write(line + "\n")

    def lines(self) -> list[dict]:
        path = usage.ledger_path()
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []

    def state(self) -> dict:
        return json.loads((self.home / "state" / "usage.json").read_text())

    def show(self) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(usage.show(True), 0)
        return out.getvalue()


class Switch(InProcess):
    def test_only_a_literal_true_turns_usage_on_and_off_deletes_the_ledger(self):
        env = install.engine_env(self.home)
        self.assertEqual(env.get("THEODORE_USAGE_LEDGER"), str(self.home / "state" / "usage-ledger.jsonl"))
        self.write(command_event())
        for answer in (None, False):            # absent (an older server) or false: off
            self.api.usage_enabled = answer
            account.refresh(timeout=5)
            with self.subTest(answer=answer):
                self.assertFalse(usage.enabled())
                self.assertNotIn("THEODORE_USAGE_LEDGER", set(install.engine_env(self.home)))
                self.assertFalse(usage.ledger_path().exists(), "off: nothing recorded stays")
        with mock.patch.dict(os.environ, {"THEODORE_USAGE_LEDGER": str(self.home / "elsewhere.jsonl")}):
            self.assertNotIn("THEODORE_USAGE_LEDGER", set(install.engine_env(self.home)), "an inherited path is not passed on")
        self.api.usage_enabled = True
        account.refresh(timeout=5)
        self.assertIn("THEODORE_USAGE_LEDGER", set(install.engine_env(self.home)))

    def test_an_on_asked_before_a_usage_off_does_not_turn_it_back_on(self):
        asked = core.now() - 5
        self.api.usage_answer = (403, "usage_off")
        self.write(command_event())
        usage.flush(force=True)
        self.assertFalse(usage.enabled())
        usage.remember(True, asked)
        self.assertFalse(usage.enabled(), "a refresh that started before the refusal")
        usage.remember(True, core.now() + 1)
        self.assertTrue(usage.enabled())

    def test_logout_removes_the_ledger_and_the_switch(self):
        self.write(command_event())
        account.logout()
        self.assertFalse(usage.ledger_path().exists())
        self.assertFalse(usage.enabled())


class Sending(InProcess):
    def test_ok_deletes_exactly_the_lines_sent_and_resets_dropped(self):
        events = [command_event(-60 - i) for i in range(3)]
        self.write(*events, raw=["not json"])
        usage.flush(force=True)
        self.assertEqual(len(self.api.usage_bodies), 1)
        sent = json.loads(self.api.usage_bodies[0])
        self.assertEqual(sorted(e["event_id"] for e in sent["events"]), sorted(e["event_id"] for e in events))
        self.assertEqual(sent["dropped"], 1, "the malformed line is counted, never sent")
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.state().get("dropped"), 0)
        self.assertNotIn("pending", self.state())

    def test_duplicates_are_deleted_too(self):
        events = [command_event(-60), command_event(-30)]
        self.write(*events)
        for event in events[:1]:
            self.api.usage_events[event["event_id"]] = event       # the server has it already (a lost answer)
        usage.flush(force=True)
        self.assertEqual(self.lines(), [])

    def test_lines_appended_after_the_batch_was_cut_stay(self):
        self.write(command_event(-60))
        late = command_event()
        real_post = usage._post

        def post(bearer, data):
            self.write(late)            # the engine appends while the batch is on its way
            return real_post(bearer, data)

        with mock.patch.object(usage, "_post", side_effect=post):
            usage.flush(force=True)
        self.assertEqual([e["event_id"] for e in self.lines()], [late["event_id"]])

    def test_offline_keeps_the_lines_and_the_batch_id_and_event_ids_on_replay(self):
        events = [command_event(-60), command_event(-30)]
        self.write(*events)
        self.api.down = True
        usage.flush(force=True)
        self.assertEqual(len(self.lines()), 2)
        first = self.state()["pending"]
        self.api.down = False
        with mock.patch.dict(core.config(), {"api_base": "http://127.0.0.1:9"}):     # connection refused
            usage.flush(force=True)
        self.assertEqual(self.state()["pending"], first)
        self.assertEqual(len(self.lines()), 2)
        self.assertEqual(len(self.api.usage_bodies), 1, "only the dropped connection reached the service")
        usage.flush(force=True)
        replay = json.loads(self.api.usage_bodies[-1])
        self.assertEqual(replay["batch_id"], first["batch_id"])
        self.assertEqual([e["event_id"] for e in replay["events"]], [e["event_id"] for e in first["events"]])
        self.assertEqual(self.lines(), [])

    def test_a_slow_service_is_abandoned_after_two_seconds_and_the_lines_stay(self):
        self.write(command_event(-60))
        self.api.usage_delay = 4
        started = time.monotonic()
        usage.flush(force=True)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(len(self.lines()), 1)
        self.assertIn("pending", self.state())

    def test_the_two_seconds_hold_through_a_proxy(self):
        self.write(command_event(-60))
        waits = []
        real = api._opener

        def opener(redirects=True):
            inner = real(redirects)
            return mock.Mock(open=lambda req, timeout: waits.append(timeout) or inner.open(req, timeout=timeout))

        with mock.patch.object(api, "_proxied", return_value=True), mock.patch.object(api, "_opener", side_effect=opener):
            usage.flush(force=True)
        self.assertEqual(waits, [2.0], "not raised to the proxy minimum")

    def test_a_ledger_that_cannot_be_renamed_loses_nothing(self):
        events = [command_event(-60), command_event(-30)]
        self.write(*events)
        real = os.replace

        def refuse(src, dst):
            if Path(src) == usage.ledger_path():
                raise PermissionError("in use")         # Windows: the engine has it open
            return real(src, dst)

        with mock.patch.object(usage.os, "replace", side_effect=refuse):
            usage.flush(force=True)
        self.assertEqual(len(self.api.usage_bodies), 1)
        self.assertEqual(len(self.lines()), 2, "kept until it can be rewritten")
        usage.flush(force=True)         # sent again as a new batch: the service counts them as duplicates
        self.assertEqual(self.lines(), [])
        self.assertEqual(len(self.api.usage_events), 2)

    def test_422_drops_the_batch_and_counts_it(self):
        self.write(command_event(-60), command_event(-30))
        self.api.usage_answer = (422, "validation_failed")
        usage.flush(force=True)
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.state()["dropped"], 2)
        self.assertNotIn("pending", self.state())
        self.api.usage_answer = None
        self.write(command_event())
        usage.flush(force=True)
        self.assertEqual(json.loads(self.api.usage_bodies[-1])["dropped"], 2)
        self.assertEqual(self.state()["dropped"], 0)

    def test_403_usage_off_deletes_the_ledger_and_stops_the_path(self):
        self.write(command_event(-60))
        self.api.usage_answer = (403, "usage_off")
        usage.flush(force=True)
        self.assertFalse(usage.ledger_path().exists())
        self.assertFalse(usage.enabled())
        self.assertNotIn("THEODORE_USAGE_LEDGER", set(install.engine_env(self.home)))
        self.assertTrue((self.home / "state" / "entitlement.json").exists(), "usage_off is no refusal of the account")

    def test_413_429_and_5xx_keep_the_lines(self):
        for answer in ((413, "payload_too_large"), (429, "rate_limited"), (503, "service_disabled"), (401, "unauthenticated")):
            with self.subTest(answer=answer):
                self.write(command_event(-60))
                self.api.usage_answer = answer
                usage.flush(force=True)
                self.assertTrue(self.lines())
                self.assertTrue(credentials.load(), "a usage answer never signs this computer out")

    def test_no_flush_before_fifteen_minutes_unless_finish(self):
        self.write(command_event(-14 * 60))
        usage.flush()
        self.assertEqual(self.api.usage_bodies, [])
        self.write(command_event(-16 * 60))
        usage.flush()
        self.assertEqual(len(self.api.usage_bodies), 1)

    def test_errors_are_swallowed_and_nothing_is_printed(self):
        self.write(command_event(-60))
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(usage, "_post", side_effect=RuntimeError("boom")):
            usage.flush(force=True)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(self.lines()), 1)


class Limits(InProcess):
    def test_cap_of_2000_lines_drops_the_oldest_and_counts_them(self):
        events = [command_event(-3600 + i) for i in range(2010)]
        self.write(*reversed(events))
        body = json.loads(self.show())
        self.assertEqual(body["dropped"], 10)
        self.assertEqual(body["events"][0]["event_id"], events[10]["event_id"], "oldest first")
        self.assertEqual(len(body["events"]), 200)
        self.assertEqual(len(self.lines()), 2000)

    def test_cap_of_one_megabyte(self):
        # Valid lines stay far under 1 MB at 2,000 lines; whitespace JSON allows between tokens makes them larger.
        events = [command_event(-3600 + i) for i in range(1500)]
        lines = ["{" + " " * 800 + json.dumps(e)[1:] for e in events]
        self.assertGreater(sum(len(l) + 1 for l in lines), contract.USAGE_LEDGER_MAX_BYTES)
        self.write(raw=lines)
        self.show()
        self.assertLessEqual(usage.ledger_path().stat().st_size, contract.USAGE_LEDGER_MAX_BYTES)
        kept = self.lines()
        self.assertEqual(self.state()["dropped"], len(events) - len(kept))
        self.assertEqual(kept[-1]["event_id"], events[-1]["event_id"], "the newest stay")
        self.assertNotEqual(kept[0]["event_id"], events[0]["event_id"], "the oldest go")

    def test_lines_older_than_thirty_days_are_dropped(self):
        old, fresh = command_event(-31 * 86400), command_event(-60)
        self.write(old, fresh)
        body = json.loads(self.show())
        self.assertEqual([e["event_id"] for e in body["events"]], [fresh["event_id"]])
        self.assertEqual(body["dropped"], 1)

    def test_batch_is_at_most_64_kb(self):
        self.write(*[command_event(-3600 + i, outcome="failed", code="E" + "x" * 63) for i in range(200)])
        raw = self.show().rstrip("\n").encode()
        self.assertLessEqual(len(raw), contract.USAGE_BATCH_MAX_BYTES)
        self.assertLess(len(json.loads(raw)["events"]), 200)


class Validation(InProcess):
    def test_malformed_lines_are_never_sent(self):
        good = command_event(-60)
        bad = [
            command_event(-60, title="Acme Confidential Board"),           # an extra key
            command_event(-60, code="has a space"),                         # a message, not a code
            command_event(-60, verb="open"),                                # not a verb
            command_event(-60, deck_id="acme-board-q3.pptx"),
            command_event(-60, slides_total=True),                          # a boolean is not a count
            command_event(-60, duration_ms=1.5),
            command_event(-60, reopen=None),                                # optional fields are omitted, never null
            {k: v for k, v in command_event(-60).items() if k != "code"},  # a required key missing
            command_event(-60, event_id="not-a-uuid"),
            {"type": "client_status", "event_id": str(uuid.uuid4()), "ts": now_ms(-60), "command": "specimen", "status": "OFFLINE", "cause": None},
            {"type": "client_status", "event_id": str(uuid.uuid4()), "ts": now_ms(-60), "command": "pptx", "status": "OK", "cause": None},
            {"type": "comment", "text": "Acme Confidential Board"},
        ]
        self.write(good, *bad, raw=["{", '"/Users/someone/Acme Confidential Board"', "[]"])
        usage.flush(force=True)
        raw = self.api.usage_bodies[0].decode()
        sent = json.loads(raw)
        self.assertEqual([e["event_id"] for e in sent["events"]], [good["event_id"]])
        self.assertEqual(sent["dropped"], len(bad) + 3)
        for text in CLIENT_LIKE:
            self.assertNotIn(text, raw)
        log = (self.home / "logs" / "client.log").read_text() if (self.home / "logs" / "client.log").exists() else ""
        for text in CLIENT_LIKE:
            self.assertNotIn(text, log)

    def test_a_repeated_event_id_is_sent_once(self):
        event = command_event(-60)
        self.write(event, event)
        body = json.loads(self.show())
        self.assertEqual(([e["event_id"] for e in body["events"]], body["dropped"]), ([event["event_id"]], 0))

    def test_the_envelope_has_the_contract_keys_and_shapes(self):
        self.write(command_event(-60))
        body = json.loads(self.show())
        self.assertEqual(set(body), {"batch_id", "client_version", "plugin_version", "engine_version", "host", "os", "arch", "dropped", "events"})
        self.assertRegex(body["os"], contract.USAGE_OS_PATTERN)
        self.assertIn(body["arch"], ("arm64", "x64"))
        self.assertEqual(str(uuid.UUID(body["batch_id"])), body["batch_id"])

    def test_show_prints_exactly_the_body_that_is_posted(self):
        self.write(command_event(-60), command_event(-30))
        shown = self.show()
        usage.flush(force=True)
        self.assertEqual(shown.rstrip("\n").encode("utf-8"), self.api.usage_bodies[0])

    def test_os_family(self):
        cases = [("darwin", {"mac_ver": ("15.1.1", ("", "", ""), "")}, "macos-15"),
                 ("linux", {"release": "6.8.0-45-generic"}, "linux-6")]
        for plat, patches, expected in cases:
            with self.subTest(plat=plat), mock.patch.object(sys, "platform", plat), mock.patch.object(os, "name", "posix"), \
                    mock.patch.object(usage.platform, "mac_ver", return_value=patches.get("mac_ver", ("", ("", "", ""), ""))), \
                    mock.patch.object(usage.platform, "release", return_value=patches.get("release", "")):
                self.assertEqual(usage.os_family(), expected)
        for build, expected in ((22631, "windows-11"), (19045, "windows-10")):
            winver = mock.Mock(build=build, major=10)
            with self.subTest(build=build), mock.patch.object(sys, "platform", "win32"), mock.patch.object(os, "name", "nt"), \
                    mock.patch.object(sys, "getwindowsversion", create=True, return_value=winver):
                self.assertEqual(usage.os_family(), expected)

    def test_client_commands_match_the_contract(self):
        snapshot = json.loads((FIXTURES / "contract-v1.json").read_text())
        self.assertEqual(list(contract.CLIENT_COMMANDS), snapshot["launcher"]["client_commands"])
        self.assertEqual(snapshot["routes"]["v1.usage.create"]["path"], "/v1/usage")


class ClientStatus(InProcess):
    def run_cli(self, *argv) -> int:
        with redirect_stdout(io.StringIO()), mock.patch.object(usage, "flush"):
            return cli._main(list(argv))

    def test_a_non_ok_status_is_one_event_with_a_code_cause(self):
        with mock.patch("theodore_client.commands.prefs", side_effect=api.setup_failed("TLS_INTERCEPT", "Acme Confidential Board")):
            self.assertEqual(self.run_cli("prefs", "list"), 68)
        events = self.lines()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual({k: event[k] for k in ("type", "command", "status", "cause")},
                         {"type": "client_status", "command": "prefs", "status": "SETUP_FAILED", "cause": "TLS_INTERCEPT"})
        self.assertTrue(usage.valid_event(event))
        self.assertNotIn("Acme", json.dumps(event))

    def test_blocked_by_policy_and_an_engine_refusal(self):
        blocked = core.Status("BLOCKED_BY_POLICY", "the engine could not be started", "x", "y")
        with mock.patch("theodore_client.commands.run_pptx", side_effect=blocked):
            self.assertEqual(self.run_cli("pptx", "render", "/Users/someone/Acme Confidential Board"), 73)
        for code, status in contract.ENGINE_REFUSALS.items():
            if code in ("entitlement_missing", "entitlement_expired"):
                continue        # these sign in or refresh first; their status is covered by the launcher suite
            refusal = install._refusal_status(code)
            refusal.cause = code
            with mock.patch("theodore_client.commands.run_pptx", side_effect=refusal):
                self.run_cli("pptx", "apply", "x")
        events = self.lines()
        self.assertEqual((events[0]["status"], events[0]["cause"]), ("BLOCKED_BY_POLICY", None))
        self.assertEqual([(e["status"], e["cause"]) for e in events[1:]],
                         [(s, c) for c, s in contract.ENGINE_REFUSALS.items() if c not in ("entitlement_missing", "entitlement_expired")])
        self.assertNotIn("Acme", usage.ledger_path().read_text())

    def test_engine_refusal_carries_its_code(self):
        with mock.patch.object(install.subprocess, "run", return_value=mock.Mock(returncode=1, stdout='{"code": "entitlement_untrusted"}')):
            status = install._engine_refusal(self.home)
        self.assertEqual((status.name, status.cause), ("NO_ENTITLEMENT", "entitlement_untrusted"))

    def test_nothing_when_usage_is_off_or_ok_or_not_a_contract_command(self):
        with mock.patch("theodore_client.commands.prefs", return_value=core.emit(core.Status("OK", "a", "b", "c"))):
            self.run_cli("prefs", "list")
        with mock.patch("theodore_client.commands.specimen", side_effect=core.usage("x")):
            self.run_cli("specimen", "a.jpg")
        self.assertEqual(self.lines(), [])
        self.api.usage_enabled = False
        account.refresh(timeout=5)
        with mock.patch("theodore_client.commands.prefs", side_effect=core.usage("x")):
            self.run_cli("prefs", "list")
        self.assertFalse(usage.ledger_path().exists())

    def test_a_command_sends_at_its_start_only_when_the_oldest_line_is_fifteen_minutes_old(self):
        self.write(command_event(-16 * 60))
        with redirect_stdout(io.StringIO()):
            cli._main(["usage"])
            self.assertEqual(self.api.usage_bodies, [], "usage itself never sends")
            cli._main(["version"])
        self.assertEqual(len(self.api.usage_bodies), 1)
        self.assertEqual(self.lines(), [])

    def test_usage_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli._dispatch(["help"])
        self.assertIn("usage [--show]", out.getvalue())


class Paths(unittest.TestCase):
    def test_ledger_lives_in_theodores_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = core._HOME
            try:
                with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(Path(tmp) / "Local")}, clear=False):
                    os.environ.pop("THEODORE_HOME", None)
                    with mock.patch.object(core, "IS_WINDOWS", True):
                        core._HOME = None
                        self.assertEqual(usage.ledger_path(), Path(tmp) / "Local" / "Theodore" / "state" / "usage-ledger.jsonl")
                    with mock.patch.object(core, "IS_WINDOWS", False), mock.patch.object(Path, "home", return_value=Path(tmp)):
                        core._HOME = None
                        self.assertEqual(usage.ledger_path(), Path(tmp) / ".theodore" / "state" / "usage-ledger.jsonl")
            finally:
                core._HOME = saved


class Disclosure(unittest.TestCase):
    def test_security_names_the_ledger_its_place_every_field_and_the_off_switch(self):
        text = (Path(__file__).resolve().parents[1] / "SECURITY.md").read_text(encoding="utf-8")
        section = text[text.index("## Usage counts"):text.index("## Bug reports")]
        for needle in ("~/.theodore/state/usage-ledger.jsonl", "%LOCALAPPDATA%\\Theodore\\state\\usage-ledger.jsonl",
                       "theodore usage --show", "support@theodoreand.ai", "pptx finish", "15", "2,000", "1 MB", "30 days"):
            self.assertIn(needle, section)
        for field in [*usage._COMMAND_REQUIRED, *usage._COMMAND_OPTIONAL, *usage._STATUS_REQUIRED]:
            self.assertIn(f"`{field}`", section, field)
        self.assertNotIn("sign in", section.lower())


class Commands(ClientCase):
    """Through the client's command line: the engine's environment, the end of pptx finish, a slow service."""

    def ledger(self) -> Path:
        return self.box.home / "state" / "usage-ledger.jsonl"

    def test_engine_gets_the_path_only_when_usage_is_on_and_finish_sends(self):
        self.ready()
        env = json.loads(self.client("pptx", "env").stdout.strip().splitlines()[-1])
        self.assertIsNone(env["usage_ledger"], "a refresh without usage_enabled: no path")
        self.api.usage_enabled = True
        self._refresh()
        env = json.loads(self.client("pptx", "env").stdout.strip().splitlines()[-1])
        self.assertEqual(env["usage_ledger"], str(self.ledger()))
        workspace = self.box.tmp / "Acme Confidential Board"
        self.assertEqual(self.client("pptx", "start", str(workspace)).returncode, 0)
        self.assertEqual(len(self.ledger().read_text().splitlines()), 1)
        self.assertEqual(self.client("pptx", "finish", str(workspace)).returncode, 0)
        self.assertEqual(len(self.api.usage_bodies), 1)
        body = self.api.usage_bodies[0].decode()
        self.assertEqual([e["verb"] for e in json.loads(body)["events"]], ["start", "finish"])
        for text in CLIENT_LIKE + (str(workspace),):
            self.assertNotIn(text, body)
        self.assertFalse(self.ledger().exists() and self.ledger().read_text().strip(), "the lines sent are gone")

    def _refresh(self):
        """A licence refresh, as the client makes one when the licence is due (in a process of its own)."""
        import subprocess
        code = "import sys; sys.path.insert(0, sys.argv[1]); from theodore_client import account; account.refresh(timeout=10)"
        result = subprocess.run([sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1] / "client")],
                                env=self.box.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_slow_service_does_not_hold_up_finish(self):
        self.ready()
        self.api.usage_enabled = True
        self._refresh()
        workspace = self.box.tmp / "deck"
        self.assertEqual(self.client("pptx", "start", str(workspace)).returncode, 0)
        self.api.usage_delay = 6
        started = time.monotonic()
        self.client("pptx", "version")
        baseline = time.monotonic() - started
        started = time.monotonic()
        result = self.client("pptx", "finish", str(workspace))
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(elapsed, baseline + 3.0, "the usage send is abandoned after two seconds")
        self.assertNotIn("usage", result.stdout.lower())
        self.assertEqual(len(self.ledger().read_text().splitlines()), 2, "kept for the next time")

    def test_usage_show_through_the_command_line(self):
        self.ready()
        self.assertIn("off", self.client("usage").stdout)
        self.api.usage_enabled = True
        self._refresh()
        self.assertEqual(self.client("pptx", "start", str(self.box.tmp / "deck")).returncode, 0)
        shown = self.client("usage", "--show")
        self.assertEqual(shown.returncode, 0)
        body = json.loads(shown.stdout)
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(self.api.usage_bodies, [], "--show sends nothing")
        self.assertIn("1 event(s) waiting", self.client("usage").stdout)


if __name__ == "__main__":
    unittest.main()
