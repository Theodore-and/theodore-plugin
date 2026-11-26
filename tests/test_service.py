"""The resident service (client/theodore_client/service/): contract C1 at its endpoint, the worker pool of C2 with a
stand-in engine, crashes, cancellation, deadlines, the lifecycle (no process left after the last session) and the
hand-over to an updated client. macOS and Linux run these; the Windows rig runs lanes/A-rig.md."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from service_support import (Collector, alive, children_of, install_engine, make_runtime, start_service,  # noqa: E402
                             wait_gone)
from support import CLIENT_DIR, ClientCase, sandbox_processes  # noqa: E402

from theodore_client.service import client as service_client, endpoint, frames  # noqa: E402

ENGINE = "1.0.0-rc.90"


class FramesUnit(unittest.TestCase):
    def test_round_trip_and_refusals(self):
        frame = {"t": "out", "id": "1", "stream": "stdout", "data": "é—“x”"}
        data = frames.encode(frame)
        self.assertEqual(struct.unpack("<I", data[:4])[0], len(data) - 4)
        self.assertEqual(frames.decode(data[4:]), frame)
        for bad in (b"[1]", b"{}", b"\xff", b"{\"t\": 1}"):
            with self.assertRaises(frames.FrameError):
                frames.decode(bad)
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack("<I", 0))
            with self.assertRaises(frames.FrameError):
                frames.Channel(right).read()
        finally:
            left.close()
            right.close()

    def test_priorities_and_keys(self):
        from theodore_client.service import workers
        self.assertEqual(workers.classify(["slide", "list", "/w"]), "read")
        self.assertEqual(workers.classify(["slide", "set", "/w", "s001"]), "write")
        self.assertEqual(workers.classify(["slide", "import", "/w"]), "long")
        self.assertEqual(workers.classify(["apply", "/w"]), "long")
        self.assertEqual(workers.classify(["--help"]), "read")
        self.assertEqual(workers.classify(["doctor", "--powerpoint"]), "long")
        self.assertIsNone(workers.workspace_key(["slide", "list", "/nonexistent"], "/"))


class ServiceCase(ClientCase):
    """A signed-in home with a stand-in engine (C2 worker) and its service running."""

    def setUp(self):
        super().setUp()
        self.sign_in()
        make_runtime(self.box.home)
        install_engine(self.box.home, ENGINE)
        # The service checks for updates when a session opens; the fake channel publishes another engine, which would
        # become current mid-test. Checked a moment ago, for the session these tests open.
        now = int(time.time())
        (self.box.home / "state" / "update.json").write_text(json.dumps({"last_check": now, "last_attempt": now,
                                                                          "checked_session": "s1"}))
        self.env = {**self.box.env, "THEODORE_SERVICE_LINGER_S": "2", "THEODORE_SERVICE_IDLE_S": "4",
                    "THEODORE_SERVICE_WORKERS": "2", "THEODORE_SERVICE_HELLO_S": "1"}
        self.env.pop("THEODORE_RESIDENT", None)
        self.env.pop("THEODORE_SERVICE", None)          # the shell route goes through the service here
        self.record = None
        self.addCleanup(self.stop_service)

    def service(self) -> dict:
        self.record = start_service(self.env, self.box.home)
        return self.record

    def wait_log(self, pattern: str, seconds: float = 15.0) -> str:
        """service.log once a line matches `pattern` (a regular expression), or as it is: lines written after a call's
        answer (its timing line, an ahead's done, a primed folder) may come a moment after the answer."""
        deadline = time.monotonic() + seconds
        log = ""
        while time.monotonic() < deadline:
            path = self.box.home / "logs" / "service.log"
            log = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            if re.search(pattern, log):
                break
            time.sleep(0.05)
        return log

    def stop_service(self) -> None:
        """SIGTERM ends every service of this home (a successor too) within 10 s, and their workers with them; the test
        fails otherwise, and the sandbox's removal ends what is left."""
        mine = sandbox_processes(self.box.tmp.name)
        services = {pid for pid, line in mine if " service --home " in line}
        record = endpoint.read_service_json(self.box.home) or self.record
        if record and alive(record["pid"]):
            services.add(int(record["pid"]))
        for pid in services:
            with contextlib.suppress(OSError):
                os.kill(pid, 15)
        stuck = [pid for pid in sorted(services) if not wait_gone(pid, 30)]       # 30 s: a loaded check machine
        self.assertEqual(stuck, [], "a service did not end within 30 s of SIGTERM")
        deadline = time.monotonic() + 15
        while (left := [p for p in sandbox_processes(self.box.tmp.name) if "engine.py serve" in p[1]]) \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(left, [], "workers outlived their service")

    def connect(self, role: str = "relay", session: str = "s1", cwd: str | None = None) -> tuple[service_client.ServiceClient, Collector]:
        collector = Collector()
        client = service_client.ServiceClient(self.box.home, role=role, name="theodore-relay-py" if role == "relay" else "theodore-front-py",
                                              session_id=session, host="claude-code", on_frame=collector,
                                              on_notify=collector.notify, cwd=cwd)
        client.connect(start=False)
        self.addCleanup(client.close)
        return client, collector

    def call(self, client, collector, call_id: str, args: list[str], tool: str = "pptx", wait: bool = True, **extra):
        frame = {"t": "call", "id": call_id, "tool": tool, "cwd": str(self.box.tmp), **extra}
        if tool in ("pptx", "theodore"):
            frame["args"] = args
        else:
            frame["params"] = extra.pop("params", {}) if "params" in extra else {}
        client.send_call(frame, start=False)
        return collector.wait(call_id, 60) if wait else None


class Endpoint(ServiceCase):
    def test_modes_secret_and_refusals(self):
        record = self.service()
        self.assertEqual(record["endpoint"]["kind"], "unix")
        self.assertEqual(set(record), {"v", "pid", "started", "protocol", "version", "endpoint", "secret", "home"})
        run = self.box.home / "state" / "run"
        address = Path(record["endpoint"]["address"])       # <home>/state/run/s.sock, or a short private one for a deep home
        self.assertEqual(os.stat(run).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(address.parent).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(address).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(run / "service.json").st_mode & 0o777, 0o600)
        # a wrong secret: one error frame, then the connection closes
        sock = socket.socket(socket.AF_UNIX)
        sock.connect(record["endpoint"]["address"])
        channel = frames.Channel(sock)
        channel.send({"t": "hello", "v": 1, "secret": "0" * 64, "role": "front", "protocol": 1,
                      "client": {"name": "theodore-probe", "version": "0"}})
        self.assertEqual(channel.read()["code"], "BAD_SECRET")
        self.assertIsNone(channel.read())
        channel.close()
        # no hello in time: closed without an answer
        sock = socket.socket(socket.AF_UNIX)
        sock.connect(record["endpoint"]["address"])
        started = time.monotonic()
        self.assertIsNone(frames.Channel(sock).read())
        self.assertLess(time.monotonic() - started, 15)        # the hello's 1 s, with room for a loaded machine
        sock.close()
        # a newer protocol: refused with this service's own
        sock = socket.socket(socket.AF_UNIX)
        sock.connect(record["endpoint"]["address"])
        channel = frames.Channel(sock)
        channel.send({"t": "hello", "v": 1, "secret": record["secret"], "role": "front", "protocol": 9,
                      "client": {"name": "theodore-probe", "version": "0"}})
        answer = channel.read()
        self.assertEqual((answer["code"], answer["protocol"]), ("PROTOCOL", 1))
        channel.close()
        # unknown tool, duplicate id
        client, got = self.connect()
        welcome = client.welcome()
        self.assertEqual((welcome["protocol"], welcome["new_session"]), (1, True))
        client.send_call({"t": "call", "id": "u1", "tool": "nope", "cwd": "/", "args": []}, start=False)
        self.assertEqual(got.wait("u1")[-1]["code"], "UNKNOWN_TOOL")
        self.call(client, got, "d1", ["echo"])
        client.send_call({"t": "call", "id": "d1", "tool": "pptx", "cwd": "/", "args": ["echo"]}, start=False)
        time.sleep(0.3)
        self.assertEqual(got.frames["d1"][-1]["code"], "BAD_CALL")


class Pool(ServiceCase):
    def test_warm_worker_env_overlay_and_out_of_order(self):
        self.service()
        client, got = self.connect()
        env = {"CLAUDE_CODE_SESSION_ID": "sess-1", "THEODORE_PROBE": "yes", "PATH": "/nowhere", "THEODORE_HOME": "/elsewhere"}
        first = json.loads(Collector.stdout(self.call(client, got, "e1", ["echo", "one"], env=env)))
        self.assertEqual(first["argv"], ["echo", "one"])
        self.assertEqual(first["resident"], "1")
        self.assertEqual(first["env"].get("THEODORE_PROBE"), "yes")
        self.assertEqual(first["env"].get("CLAUDE_CODE_SESSION_ID"), "sess-1")
        self.assertNotIn("THEODORE_HOME", first["env"], "a call never moves the service's home")
        second = json.loads(Collector.stdout(self.call(client, got, "e2", ["echo", "two"])))
        self.assertEqual(second["pid"], first["pid"], "the same warm worker")
        self.assertNotIn("THEODORE_PROBE", second["env"], "an overlay ends with its call")
        stderr = "".join(f["data"] for f in got.frames["e1"] if f["t"] == "out" and f["stream"] == "stderr")
        self.assertIn("a line on standard error", stderr)
        # a long call and a short one on one connection: the short one is answered first
        self.call(client, got, "long", ["sleep", "1.5"], wait=False)
        self.call(client, got, "short", ["echo", "three"])
        self.assertFalse(got.event("long").is_set(), "the short call did not wait for the long one")
        done = Collector.done(got.wait("long"))
        self.assertEqual(done["exit"], 0)
        self.assertTrue(any(f["t"] == "progress" for f in got.frames["long"]), "a call over 1 s sends progress")

    def test_short_calls_answered_while_a_long_call_runs_and_fifteen_at_once(self):
        self.service()
        client, got = self.connect()
        self.call(client, got, "long", ["sleep", "10"], wait=False)
        time.sleep(0.3)
        started = time.monotonic()
        for n in range(5):
            self.call(client, got, f"s{n}", ["echo", str(n)], wait=False)
        for n in range(5):
            self.assertEqual(Collector.done(got.wait(f"s{n}"))["exit"], 0)
        # The long call runs 10 s: the short ones are answered well before it ends, even on a loaded machine.
        self.assertLess(time.monotonic() - started, 9.0, "short calls do not wait behind the long one")
        self.assertFalse(got.event("long").is_set())
        for n in range(15):
            self.call(client, got, f"p{n}", ["sleep", "0.3"], wait=False)
        self.assertEqual([Collector.done(got.wait(f"p{n}", 90))["exit"] for n in range(15)], [0] * 15)
        self.assertEqual(Collector.done(got.wait("long"))["exit"], 0)

    def test_a_dead_worker_answers_retry_and_the_next_call_runs_on_a_new_one(self):
        self.service()
        client, got = self.connect()
        first = json.loads(Collector.stdout(self.call(client, got, "a", ["echo"])))
        frames_ = self.call(client, got, "b", ["crash"])
        self.assertEqual(Collector.done(frames_)["exit"], 78, "it may have read anything: answered, not run again")
        self.assertIn("THEODORE INTERNAL: Theodore's engine ended (exit 9) during this command", Collector.stdout(frames_))
        after = json.loads(Collector.stdout(self.call(client, got, "c", ["echo"])))
        self.assertNotEqual(after["pid"], first["pid"])
        # a read is run again by itself; a write only when its inputs did not change and nothing was committed
        flag = self.box.tmp / "flag-read"
        read = self.call(client, got, "r", ["slide", "list", str(self.box.tmp), "--crash-once", str(flag)])
        self.assertEqual(Collector.done(read)["exit"], 0)
        target = self.box.tmp / "deck.txt"
        target.write_text("a")
        write = self.call(client, got, "w", ["write", str(target), "--crash-once", str(self.box.tmp / "flag-w")])
        self.assertEqual(Collector.done(write)["exit"], 0)
        self.assertEqual(target.read_text(), "ax", "run again once, written once")
        moved = self.call(client, got, "w2", ["write", str(target), "--crash-once", str(self.box.tmp / "flag-w2"), "--touch-first"])
        self.assertEqual(Collector.done(moved)["exit"], 78, "its input changed: not run again")

    def kill_when_running(self, pid_file: Path) -> int:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.02)
        time.sleep(0.3)
        pid = int(pid_file.read_text())
        os.kill(pid, 9)
        return pid

    def test_a_worker_killed_before_its_call_wrote_runs_it_again_on_a_fresh_one(self):
        self.service()
        client, got = self.connect()
        target, pid_file = self.box.tmp / "deck.txt", self.box.tmp / "pid-before"
        target.write_text("a")
        self.call(client, got, "w", ["write", str(target), "--hold-before-commit", "3", "--pid-file", str(pid_file)],
                  wait=False)
        killed = self.kill_when_running(pid_file)
        done = Collector.done(got.wait("w", 30))
        self.assertEqual(done["exit"], 0, "nothing was written: run again on a fresh worker")
        self.assertEqual(target.read_text(), "ax", "written once, by the second run")
        log = (self.box.home / "logs" / "service.log").read_text()
        self.assertIn(f"(pid {killed}) ended: exit -9", log, "its end logged with its exit code")
        self.assertIn("nothing of it was written: run again", log)

    def test_a_worker_killed_after_its_call_wrote_answers_78_with_what_to_do(self):
        self.service()
        client, got = self.connect()
        target, pid_file = self.box.tmp / "deck.txt", self.box.tmp / "pid-after"
        target.write_text("a")
        self.call(client, got, "w", ["write", str(target), "--hold-after-commit", "3", "--pid-file", str(pid_file)],
                  wait=False)
        self.kill_when_running(pid_file)
        frames_ = got.wait("w", 30)
        self.assertEqual(Collector.done(frames_)["exit"], 78)
        self.assertIn("part of its result may be on disk", Collector.stdout(frames_))
        self.assertIn("NEXT: run the same command again", Collector.stdout(frames_))

    def test_a_worker_gone_while_a_child_holds_its_pipe_is_noticed_at_once(self):
        # K's A3b (rig 1): the worker vanished, an office it started kept its output pipe open, and the call waited.
        self.service()
        client, got = self.connect()
        started = time.monotonic()
        self.call(client, got, "o", ["echo", "--orphan", str(self.box.tmp / "orphan-marker")], wait=False)
        frames_ = got.wait("o", 50)
        self.assertLess(time.monotonic() - started, 45, "answered long before the orphan closes the pipe (60 s)")
        self.assertEqual(Collector.done(frames_)["exit"], 78)
        log = (self.box.home / "logs" / "service.log").read_text()
        self.assertIn("ended: exit 9, its process ended; a call was running on it", log)

    def test_a_call_never_waits_past_the_ceiling(self):
        self.env["THEODORE_CALL_CEILING_S"] = "2"
        self.service()
        client, got = self.connect()
        frames_ = self.call(client, got, "slow", ["sleep", "20"])        # it answers, it progresses, it never ends
        self.assertEqual(Collector.done(frames_)["exit"], 78)
        self.assertIn("was stopped: it did not finish in time", Collector.stdout(frames_))
        after = self.call(client, got, "after", ["echo"])
        self.assertEqual(Collector.done(after)["exit"], 0, "the next call runs on a fresh worker")

    def test_a_part_apply_that_runs_on_is_stopped_at_the_part_ceiling_and_told_what_to_do(self):
        # The real run of 26 Nov: a part's apply waited 15 minutes. A part's apply takes seconds: its own, tighter ceiling
        # stops it and says why; the lead's apply (a whole deck) keeps the wide one.
        self.env.update({"THEODORE_PART_CALL_CEILING_S": "2", "FAKE_APPLY_HANG_S": "4"})
        self.service()
        client, got = self.connect()
        part = str(self.box.tmp / "ws" / "parts" / "s01")
        started = time.monotonic()
        frames_ = self.call(client, got, "stuck", ["apply", part])
        self.assertLess(time.monotonic() - started, 3.9, "stopped at the part ceiling, not left to finish")
        self.assertEqual(Collector.done(frames_)["exit"], 78)
        said = Collector.stdout(frames_)
        self.assertIn("this part's apply ran for 2 s", said)
        self.assertIn("deck.js very likely never finishes", said)
        self.assertIn("NEXT: do not run the same command again", said)
        log = (self.box.home / "logs" / "service.log").read_text()
        self.assertIn("did not finish within 2 s (a part's apply) during a call; stopping it", log)
        lead = self.call(client, got, "lead", ["apply", str(self.box.tmp / "ws")])
        self.assertEqual(Collector.done(lead)["exit"], 0, "a whole deck's apply keeps the wide ceiling")

    def test_which_calls_are_a_part_apply(self):
        from theodore_client.service.workers import is_part_apply

        self.assertTrue(is_part_apply(["apply", "C:\\w\\parts\\s01"]))
        self.assertTrue(is_part_apply(["apply", "/w/parts/s01", "/w/parts/s02", "--ahead"]))
        self.assertFalse(is_part_apply(["apply", "/w"]))
        self.assertFalse(is_part_apply(["apply", "/w/parts/s01", "/w"]))
        self.assertFalse(is_part_apply(["check", "/w/parts/s01"]))
        self.assertFalse(is_part_apply([]))

    def test_a_call_with_no_warm_idle_worker_takes_a_cold_idle_one_at_once(self):
        # Lane S2's S2-17 (the coordinator): waiting up to 1 s for a worker past its warm-up (C2 `warm`, lane B) made
        # every cold round's slowest 1.07-1.58 s against 0.13-0.21 s taking any idle worker. A worker still warming is
        # only a second choice: with the warm one busy, a call takes it at once.
        self.env.update({"FAKE_WARM_AFTER": "0.1", "FAKE_WARM_LATE": "60", "FAKE_WARM_FIRST": str(self.box.tmp / "warm-first")})
        self.service()
        client, got = self.connect()
        warm_pid = json.loads(Collector.stdout(self.call(client, got, "w", ["echo"])))["pid"]
        self.assertIn("warm: its warm-up ended", self.wait_log(r"warm: its warm-up ended"), "the `warm` frame is read")
        self.call(client, got, "hold", ["sleep", "3"], wait=False)          # the warm worker, busy
        time.sleep(0.3)
        cold_pid = json.loads(Collector.stdout(self.call(client, got, "c1", ["echo"])))["pid"]   # a second one starts
        self.assertNotEqual(cold_pid, warm_pid)
        began = time.monotonic()
        again = json.loads(Collector.stdout(self.call(client, got, "c2", ["echo"])))
        waited = time.monotonic() - began
        self.assertEqual(again["pid"], cold_pid, "the warm one busy: the idle one still warming")
        self.assertLess(waited, 0.6, "at once, never waiting for the warm one")
        got.wait("hold", 30)

    def test_a_session_opened_in_a_folder_primes_it_with_one_answered_call_there(self):
        # Lane BIG (the coordinator, 25 Nov): the engine makes a folder's template facts ahead after an answered call
        # there, and a session's first call is often `start` itself. The session's folder is primed when it opens.
        record = self.box.tmp / "calls.jsonl"
        folder = self.box.tmp / "the user's folder"
        folder.mkdir()
        self.env["FAKE_CALLS"] = str(record)
        self.service()
        client, got = self.connect(cwd=str(folder))
        deadline = time.monotonic() + 20
        rows = []
        while time.monotonic() < deadline and not rows:
            time.sleep(0.1)
            rows = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
        self.assertEqual([(row["argv"], row["cwd"]) for row in rows], [(["--help"], str(folder))])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [], "nothing written in the folder")
        self.assertIn(f"primed {folder}", self.wait_log(re.escape(f"primed {folder}")))

    def test_a_new_workspaces_next_calls_go_to_the_worker_that_made_it(self):
        # K's r61: after five starts on one worker, the first `slide list` went to a fresh worker (the new workspace's
        # key was not remembered: it has none before `start` runs), 554 ms against 9 ms warm.
        self.service()
        client, got = self.connect()
        # two workers, both idle after: a long call holds the first while a second starts for another call
        self.call(client, got, "hold", ["sleep", "1.5"], wait=False)
        time.sleep(0.2)
        self.call(client, got, "w", ["echo"])
        got.wait("hold", 30)
        live = [p for p in sandbox_processes(self.box.tmp.name) if "engine.py serve" in p[1]]
        self.assertEqual(len(live), 2, live)
        folder = str(self.box.tmp / "new-deck")
        made = json.loads(Collector.stdout(self.call(client, got, "s", ["start", folder])))
        for n in range(3):
            answer = json.loads(Collector.stdout(self.call(client, got, f"l{n}", ["echo", folder])))
            self.assertEqual(answer["pid"], made["pid"], "on the worker that holds the new workspace")

    def test_a_stopped_worker_is_never_given_the_next_call(self):
        # Seen at load 35: a call sent right after a worker was stopped found it still idle in the pool (its process not
        # yet seen gone), failed to reach it and was answered 78 with the stopped call's reason.
        from types import SimpleNamespace
        from unittest import mock

        from theodore_client.service import workers
        with mock.patch.object(workers, "pool_sizes", return_value=(1, 2)):
            pool = workers.Pool(SimpleNamespace(log=lambda _m: None))
            worker = workers.Worker.__new__(workers.Worker)
            worker.pool, worker.spec, worker.retiring, worker.lost = pool, SimpleNamespace(version="9.9"), False, None
            worker.dead, worker._result_ready = threading.Event(), threading.Event()
            worker.process = SimpleNamespace(pid=1, poll=lambda: 0, kill=lambda: None, stdin=SimpleNamespace(close=lambda: None))
            pool.workers["9.9"] = [worker]
            self.assertEqual(pool._live("9.9"), [worker])
            worker._stop("stopped answering", "stopped answering")
            self.assertEqual(pool._live("9.9"), [], "out of the pool at once, before its process is seen gone")

    def test_a_call_never_waits_on_a_worker_that_stopped_answering(self):
        self.env.update({"THEODORE_WORKER_HEARTBEAT_S": "1", "THEODORE_WORKER_HEARTBEAT_GRACE_S": "3"})
        self.service()
        client, got = self.connect()
        frozen = self.call(client, got, "frozen", ["echo", "--freeze"])   # alive, answering nothing, not even stats
        self.assertEqual(Collector.done(frozen)["exit"], 78)
        self.assertIn("stopped answering", Collector.stdout(frozen))
        after = self.call(client, got, "after", ["echo"])
        self.assertEqual(Collector.done(after)["exit"], 0, "the next call runs on a fresh worker")

    def test_cancel_resend_and_the_shell_deadline(self):
        self.service()
        client, got = self.connect()
        self.call(client, got, "c1", ["sleep", "20"], wait=False)
        time.sleep(0.5)
        client.cancel("c1")             # whatever the call is doing by now (finding a worker, starting, running)
        done = Collector.done(got.wait("c1", 30))
        self.assertEqual(done["exit"], 130)
        # cancelled at once: while a worker is found for it or before its run reaches the worker, never lost
        self.call(client, got, "c2", ["sleep", "20"], wait=False)
        client.cancel("c2")
        self.assertEqual(Collector.done(got.wait("c2", 30))["exit"], 130)
        # a finished call sent again is replayed; a running one attached to
        self.call(client, got, "r1", ["echo", "x"])
        other, got2 = self.connect(session="s1")
        other.send_call({"t": "call", "id": "r1", "tool": "pptx", "cwd": "/", "args": ["echo", "x"], "resend": True}, start=False)
        replay = got2.wait("r1")
        self.assertTrue(Collector.done(replay).get("replayed"))
        self.assertEqual(Collector.stdout(replay), Collector.stdout(got.frames["r1"]))
        # the shell route: past its deadline the caller gets 124; the same command again waits for the same call
        front, got3 = self.connect(role="front", session="sh")
        front.send_call({"t": "call", "id": "f1", "tool": "pptx", "cwd": "/", "args": ["sleep", "2.5"], "deadline_ms": 1000}, start=False)
        first = got3.wait("f1", 30)
        self.assertEqual(Collector.done(first)["exit"], 124)
        self.assertEqual(json.loads(Collector.stdout(first))["code"], "STILL_RUNNING")
        front.send_call({"t": "call", "id": "f2", "tool": "pptx", "cwd": "/", "args": ["sleep", "2.5"]}, start=False)
        again = got3.wait("f2", 30)
        self.assertEqual(Collector.done(again)["exit"], 0)
        self.assertEqual(json.loads(Collector.stdout(again))["slept"], "2.5")

    def test_pictures_oneshot_engines_and_a_licence_refusal(self):
        self.service()
        client, got = self.connect()
        pages = []
        for n in (1, 2, 3):
            page = self.box.tmp / f"slide-000{n}-123.jpg"
            page.write_bytes(b"\xff\xd8\xff\xe0" + f"page {n}".encode() * 20 + b"\xff\xd9")
            pages.append(page)
        drawn = self.call(client, got, "img", ["render", *map(str, pages)], images=True)
        images = [f for f in drawn if f["t"] == "image"]
        self.assertEqual([base64.b64decode(f["data"]) for f in images], [p.read_bytes() for p in pages], "render: the pages asked")
        applied = self.call(client, got, "app", ["apply", *map(str, pages)], images=True)
        images = [f for f in applied if f["t"] == "image"]
        self.assertEqual([f["path"] for f in images], [str(pages[1])], "apply: the page drawn anew, not the cached ones")
        v2 = self.call(client, got, "v2", ["render", "--v2", *map(str, pages[:2])], images=True)
        self.assertEqual([f["path"] for f in v2 if f["t"] == "image"], [str(pages[0]), str(pages[1])], "2.0: pictures[].path")
        self.assertEqual(Collector.done(v2).get("pending"), [{"kind": "picture", "slide": "s099"}], "2.0: pending slides")
        quiet = self.call(client, got, "img2", ["render", *map(str, pages)], images=False)
        self.assertFalse([f for f in quiet if f["t"] == "image" and f.get("data")])
        # an engine without engine.py runs one-shot from the service
        install_engine(self.box.home, "1.0.0-rc.33", serve=False)
        old = json.loads(Collector.stdout(self.call(client, got, "o", ["echo"])))
        self.assertTrue(old["oneshot"])
        refused = self.call(client, got, "r", ["refuse"])
        self.assertIn("THEODORE SIGN_IN_REQUIRED", Collector.stdout(refused), "the one-shot engine's licence JSON, read")
        self.assertEqual(Collector.done(refused)["exit"], 65)
        # a worker's licence refusal (exit 77): the client's status for it, never 77
        install_engine(self.box.home, ENGINE)
        worker = self.call(client, got, "l", ["licence"])
        self.assertEqual(Collector.done(worker)["exit"], 65)
        self.assertIn("THEODORE SIGN_IN_REQUIRED", Collector.stdout(worker))


class FolderWatch(ServiceCase):
    """A session's folder watched for decks that appear or change while it is open (lane BIG's facts ahead; the
    coordinator, 25 Nov): each is told to the engine by one more call answered there, as at the session's opening."""

    def setUp(self):
        super().setUp()
        self.calls_file = self.box.tmp / "calls.jsonl"
        self.folder = self.box.tmp / "the user's folder"
        self.folder.mkdir()
        self.deck("old.pptx", b"an invented deck")
        self.env.update({"FAKE_CALLS": str(self.calls_file), "THEODORE_FOLDER_POLL_S": "0.2"})
        self.service()
        self.client_, self.got = self.connect(cwd=str(self.folder))
        self.assertEqual(self.primes(1), 1, "the session's opening primes its folder")
        self.wait_log(r"folder watch: watching ")

    def deck(self, name: str, data: bytes, age: float = 10.0) -> Path:
        path = self.folder / name
        path.write_bytes(data)
        when = time.time() - age                # unwritten for `age` seconds (a copy keeps its source's time)
        os.utime(path, (when, when))
        return path

    def primes(self, want: int, seconds: float = 15.0) -> int:
        deadline = time.monotonic() + seconds
        count = 0
        while time.monotonic() < deadline:
            rows = [json.loads(line) for line in self.calls_file.read_text().splitlines()] if self.calls_file.exists() else []
            count = sum(1 for row in rows if row["argv"] == ["--help"] and row["cwd"] == str(self.folder))
            if count >= want:
                break
            time.sleep(0.05)
        return count

    def test_a_new_deck_is_told_to_the_engine(self):
        self.deck("copied in.pptx", b"another invented deck")
        self.assertEqual(self.primes(2), 2, "one more call answered in the folder")
        log = self.wait_log(r"\(a deck new or changed there\)")
        self.assertIn("folder watch: copied in.pptx is new or changed", log)
        self.deck("notes.txt", b"not a deck")
        self.deck("~$copied in.pptx", b"an Office lock file")
        self.assertEqual(self.primes(3, seconds=1.5), 2, "only decks are news")

    def test_a_deck_changed_in_place_is_told_to_the_engine(self):
        self.deck("old.pptx", b"the invented deck, saved again with more in it", age=5.0)
        self.assertEqual(self.primes(2), 2, "the folder's mtime stays: the known deck's own stat says it changed")
        self.assertIn("folder watch: old.pptx is new or changed", self.wait_log(r"old\.pptx is new or changed"))

    def test_a_deck_written_while_a_call_ran_is_ours(self):
        self.call(self.client_, self.got, "busy", ["sleep", "0.6"], wait=False)
        time.sleep(0.3)
        self.deck("output.pptx", b"what a call wrote", age=0)
        self.got.wait("busy", 30)
        log = self.wait_log(r"output\.pptx written by a call of Theodore")
        self.assertIn("folder watch: output.pptx written by a call of Theodore, not news", log)
        self.assertEqual(self.primes(2, seconds=1.0), 1, "nothing told for it")

    def test_the_watch_ends_with_the_session(self):
        self.client_.close()
        self.wait_log(r"folder watch: stopped watching ")
        self.deck("later.pptx", b"a deck after the session")
        self.assertEqual(self.primes(2, seconds=1.5), 1, "no session there: no watch")
        self.assertNotIn("later.pptx", self.wait_log(r"later\.pptx", seconds=0.5))

    def test_theodores_own_home_is_never_watched(self):
        from types import SimpleNamespace

        from theodore_client.service import folders
        logged = []
        watches = folders.FolderWatches(SimpleNamespace(home=self.box.home, log=logged.append))
        inside = self.box.home / "workspaces"
        inside.mkdir(exist_ok=True)
        watches.open("s2", str(inside))
        watches.open("s2", str(self.box.home))
        self.assertEqual(logged, [], "its workspaces and outputs are Theodore's own")
        watches.stop()


class RemovedHome(unittest.TestCase):
    """A home removed under a running service stays removed (the coordinator, 25 Nov: Lifecycle's removed-home test
    failed once under load). A write already on its way when the folder went (a sign-in record, a lock, a log line, an
    apply's pin written after its answer) made home/state again, and the service, which ends when that folder is gone,
    kept serving: in a process that keeps a removed home removed, no folder below it is made again, and a home made
    again is not the service's home."""

    def setUp(self):
        import tempfile
        from unittest import mock

        from theodore_client import core
        self.root = Path(tempfile.mkdtemp(prefix="theodore-removed-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        (self.home / "state").mkdir(parents=True)
        for patch in (mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.home)}), mock.patch.object(core, "_HOME", None),
                      mock.patch.object(core, "_KEEP_REMOVED", True), mock.patch.object(core, "_LOG_FILE", None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_no_write_brings_a_removed_home_back(self):
        from theodore_client import core, usage
        core.write_json(self.home / "state" / "before.json", {"ok": True})
        shutil.rmtree(self.home)
        with self.assertRaises(FileNotFoundError):
            core.write_json(self.home / "state" / "login-pending.json", {"late": True})
        with self.assertRaises(FileNotFoundError):
            with core.lock("update-state", timeout=1):
                pass
        core.log("a line on its way")
        with contextlib.suppress(Exception):
            usage._append([b"a usage line"])                                    # noqa: SLF001
        self.assertFalse(self.home.exists(), "nothing made the home again")
        outside = self.root / "elsewhere" / "x.json"
        core.write_json(outside, {"ok": True})
        self.assertTrue(outside.is_file(), "a folder outside the home is made as before")

    def test_a_home_made_again_is_not_the_services_home(self):
        from theodore_client.service import server
        service = server.Service(self.home)
        self.assertTrue(service.home_present())
        shutil.rmtree(self.home)
        self.assertFalse(service.home_present())
        (self.home / "state").mkdir(parents=True)               # a new install, or a write that brought it back
        self.assertFalse(service.home_present(), "a new folder by the same name is not the one it served")


class ServiceLog(unittest.TestCase):
    def test_one_open_a_line_the_old_log_kept_once_full_and_a_removed_home_stays_removed(self):
        import tempfile
        from unittest import mock

        from theodore_client.service import server
        root = Path(tempfile.mkdtemp(prefix="theodore-log-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "home").mkdir()
        service = server.Service(root / "home")
        shutil.rmtree(root / "home")
        service.log("no home: nothing written")
        self.assertFalse((root / "home").exists(), "a removed home stays removed")
        (root / "home").mkdir()
        with mock.patch.object(server, "LOG_MAX", 300):
            for n in range(12):
                service.log(f"line {n} " + "x" * 40)
        log, old = root / "home" / "logs" / "service.log", root / "home" / "logs" / "service.log.1"
        self.assertTrue(old.is_file(), "a full log moves aside")
        self.assertLessEqual(log.stat().st_size if log.exists() else 0, 300 + 80)
        self.assertIn("line 11", (log.read_text() if log.exists() else "") + old.read_text())


class Cleanup(unittest.TestCase):
    """The LibreOffice a Theodore before 0.5.0 fetched into <home>/tools, removed by the 0.5.1 service (the coordinator,
    25 Nov: engine 2.0 calls none): only there, never through a link, yielding to calls, never in a session's way."""

    def setUp(self):
        import tempfile
        from types import SimpleNamespace
        self.root = Path(tempfile.mkdtemp(prefix="theodore-cleanup-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.tools = self.home / "tools"
        self.tools.mkdir(parents=True)
        self.outside = self.root / "not Theodore's"
        (self.outside / "program").mkdir(parents=True)
        (self.outside / "program" / "soffice").write_bytes(b"someone else's" * 100)
        self.logged: list[str] = []
        self.service = SimpleNamespace(home=self.home, log=self.logged.append, router=SimpleNamespace(running=0),
                                       stopping=threading.Event())

    def fetched(self, name: str = "libreoffice", megabytes: int = 3) -> Path:
        top = self.tools / name
        for i in range(3):
            folder = top / "program" / f"part{i}"
            folder.mkdir(parents=True)
            (folder / "lib.bin").write_bytes(b"x" * (megabytes * 1_000_000 // 3))
        locked = top / "program" / "read-only.txt"
        locked.write_text("kept read-only")
        os.chmod(locked, 0o444)
        return top

    def link(self, path: Path, to: Path) -> None:
        try:
            os.symlink(to, path, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"no symbolic link here: {exc}")

    def test_the_copy_an_earlier_theodore_fetched_is_removed_and_the_space_logged(self):
        from theodore_client.service import cleanup
        top, partial = self.fetched(), self.fetched("libreoffice.partial", 1)
        (self.tools / "git" / "bin").mkdir(parents=True)
        cleanup.run(self.service)
        self.assertFalse(top.exists())
        self.assertFalse(partial.exists(), "an interrupted fetch's folder beside it too")
        self.assertTrue((self.tools / "git" / "bin").is_dir(), "the rest of the tools folder stays")
        self.assertTrue(any(f"removed {top}" in line and "3 MB freed" in line for line in self.logged), self.logged)
        self.logged.clear()
        cleanup.run(self.service)
        self.assertEqual(self.logged, [], "nothing there: nothing done, nothing said")

    def test_a_link_in_its_place_is_refused_and_what_it_leads_to_kept(self):
        from theodore_client.service import cleanup
        self.link(self.tools / "libreoffice", self.outside)
        cleanup.run(self.service)
        self.assertTrue((self.tools / "libreoffice").is_symlink(), "the link is left")
        self.assertTrue((self.outside / "program" / "soffice").is_file(), "and nothing it leads to is touched")
        self.assertTrue(any("is a link" in line and "left as it is" in line for line in self.logged), self.logged)

    def test_a_link_inside_it_is_removed_as_a_link_and_never_followed(self):
        from theodore_client.service import cleanup
        top = self.fetched()
        self.link(top / "program" / "share", self.outside)
        cleanup.run(self.service)
        self.assertFalse(os.path.lexists(top))
        self.assertTrue((self.outside / "program" / "soffice").is_file(), "the folder the link led to is untouched")

    def test_a_folder_whose_real_path_is_outside_the_tools_folder_is_refused(self):
        from unittest import mock

        from theodore_client.service import cleanup
        top = self.fetched()
        real = os.path.realpath

        def elsewhere(path):
            return str(self.outside) if str(path).endswith("libreoffice") else real(path)
        with mock.patch.object(cleanup.os.path, "realpath", side_effect=elsewhere):
            cleanup.run(self.service)
        self.assertTrue((top / "program" / "part0" / "lib.bin").is_file(), "nothing removed")
        self.assertTrue((self.outside / "program" / "soffice").is_file())
        self.assertTrue(any("leads outside Theodore's tools folder" in line for line in self.logged), self.logged)

    def test_it_yields_to_calls_and_a_failure_never_stops_the_service(self):
        from unittest import mock

        from theodore_client.service import cleanup
        top = self.fetched()
        self.service.router.running = 1             # a call runs: the removal waits
        began = time.monotonic()
        thread = cleanup.start(self.service)
        self.assertLess(time.monotonic() - began, 0.5, "the service's start never waits for it")
        time.sleep(0.6)
        self.assertTrue(top.exists(), "nothing removed while a call runs")
        self.service.router.running = 0
        thread.join(30)
        self.assertFalse(top.exists())
        top = self.fetched()
        with mock.patch.object(cleanup, "_delete_tree", side_effect=RuntimeError("a disk error")):
            cleanup.start(self.service).join(30)        # no exception out of it
        self.assertTrue(any("not removed (RuntimeError: a disk error); the next start tries again" in line
                            for line in self.logged), self.logged)


class CleanupAtStart(ServiceCase):
    def test_the_service_answers_while_it_removes_the_old_copy(self):
        top = self.box.home / "tools" / "libreoffice" / "program"
        top.mkdir(parents=True)
        (top / "soffice.bin").write_bytes(b"x" * 2_000_000)
        self.service()
        client, got = self.connect()
        self.assertEqual(Collector.done(self.call(client, got, "e", ["echo"]))["exit"], 0)
        self.assertIn("2 MB freed", self.wait_log(r"cleanup: removed .*MB freed"))
        self.assertFalse((self.box.home / "tools" / "libreoffice").exists())


class Fatal(ServiceCase):
    def test_a_worker_that_cannot_start_falls_back_to_oneshot(self):
        self.env["FAKE_WORKER_FATAL"] = "1"
        self.service()
        client, got = self.connect()
        answer = json.loads(Collector.stdout(self.call(client, got, "x", ["echo"])))
        self.assertTrue(answer["oneshot"])


class PicturesRule(unittest.TestCase):
    def test_an_apply_past_the_auto_render_cap_attaches_the_slides_it_drew(self):
        # rc.34 and rc.35 on a deck of more than 40 slides (checked against the real engines, 25 Nov): the render is
        # scoped to the changed slides, `rendered_images` lists only those, and render_summary says `scoped`.
        from theodore_client.service import tools
        scoped = json.dumps({"ok": True, "rendered_images": ["/r/slide-2.jpg"], "render_summary": {
            "scoped": "2", "reason": "deck exceeds full auto-render cap; rendered the slides no current render shows",
            "slide_count": 95, "max_slides": 40}})
        self.assertEqual(tools.pictures_of(["apply", "/w"], scoped), ["/r/slide-2.jpg"])
        full = json.dumps({"ok": True, "rendered_images": ["/r/slide-1.jpg", "/r/slide-2.jpg"],
                           "render_summary": {"regenerated_pages": [2]}})
        self.assertEqual(tools.pictures_of(["apply", "/w"], full), ["/r/slide-2.jpg"], "the drawn ones, not the cached")
        unchanged = json.dumps({"ok": True, "render_summary": {"skipped": "deck.js unchanged since the last apply"}})
        self.assertEqual(tools.pictures_of(["apply", "/w"], unchanged), [])


class AssetPreviewsInline(unittest.TestCase):
    """`assets show icons ... --render` answers with a path in the engine's cache; a desktop host may keep that folder
    apart from the agent's file tools (run 2, 26 Nov: "File does not exist"). The pictures come back with the answer, as
    render's do."""

    def test_the_contact_sheet_or_each_preview_is_what_the_answer_is_about(self):
        from theodore_client.service import tools
        argv = ["assets", "show", "icons", "a", "b", "--render"]
        self.assertTrue(tools.shows_pictures(argv))
        self.assertFalse(tools.shows_pictures(["assets", "show", "icons", "a"]))
        self.assertFalse(tools.shows_pictures(["assets", "search", "icons", "a", "--render"]))
        sheet = json.dumps({"sheet": "/c/icon-sheet-1.png", "ok": True, "by_id": {
            "a": {"ok": True, "icon": {"png_path": "/c/a.png"}}, "b": {"ok": True, "icon": {"png_path": "/c/b.png"}}}})
        self.assertEqual(tools.pictures_of(argv, sheet), ["/c/icon-sheet-1.png"])
        no_sheet = json.dumps({"ok": True, "by_id": {"a": {"ok": True, "icon": {"png_path": "/c/a.png"}},
                                                     "z": {"ok": False, "error": "no"}}})
        self.assertEqual(tools.pictures_of(argv, no_sheet), ["/c/a.png"])
        one = json.dumps({"ok": True, "icon": {"id": "a", "png_path": "/c/a.png"}})
        self.assertEqual(tools.pictures_of(["assets", "show", "icons", "a", "--render"], one), ["/c/a.png"])

    def test_the_preview_is_attached_as_an_image_frame(self):
        import tempfile
        from types import SimpleNamespace

        from theodore_client.service import tools
        folder = Path(tempfile.mkdtemp(prefix="theodore-previews-"))
        self.addCleanup(shutil.rmtree, folder, True)
        sheet = folder / "icon-sheet-1.png"
        sheet.write_bytes(b"\x89PNG" + b"z" * 500)
        sent = []
        call = SimpleNamespace(args=["assets", "show", "icons", "a", "b", "--render"], cwd=str(folder), timing={},
                               image=lambda mime, path, data: sent.append((mime, path)) or True)
        tools.attach_pictures(call, json.dumps({"sheet": str(sheet), "ok": True, "by_id": {}}))
        self.assertEqual(sent, [("image/png", str(sheet))])


class PicturesOut(unittest.TestCase):
    """A burst's answers each carry a picture of about 100 KB (lane S2b's S2-22, rig 10 round 1 on a fresh store: 90 to
    304 ms, one designer after another, between a part's answer and its done frame, where the same build's next run had
    3 to 11): the service may write a client more than a 64 KB pipe held, and the timing line says what reading and
    sending the pictures took."""

    def test_the_timing_line_says_what_the_pictures_took(self):
        import tempfile
        from types import SimpleNamespace

        from theodore_client.service import tools
        folder = Path(tempfile.mkdtemp(prefix="theodore-pictures-"))
        self.addCleanup(shutil.rmtree, folder, True)
        picture = folder / "slide-1.jpg"
        picture.write_bytes(b"\xff\xd8" + b"x" * 100_000)
        sent = []

        def image(mime, path, data):
            time.sleep(0.05)                # a client reading slowly
            sent.append((mime, path, len(data)))
            return True
        call = SimpleNamespace(args=["apply", str(folder)], cwd=str(folder), image=image, timing={})
        tools.attach_pictures(call, json.dumps({"ok": True, "rendered_images": [str(picture)],
                                                "render_summary": {"regenerated_pages": [1]}}))
        self.assertEqual(len(sent), 1)
        read_ms, sent_ms = call.timing["pictures"]
        self.assertGreaterEqual(sent_ms, 45)
        self.assertLess(read_ms, 45)

    def test_a_picture_the_worker_sent_is_passed_on_as_it_came_and_the_others_are_read(self):
        # Lane S2's S2-23: a run of several parts keeps the pictures its worker sent from memory (`held_pictures`, by
        # the path each answer names); an answer's picture found there is sent on without its file being read, the
        # others from their files, in the answer's order and within the same limits.
        import tempfile
        from types import SimpleNamespace

        from theodore_client.service import tools
        folder = Path(tempfile.mkdtemp(prefix="theodore-pictures-"))
        self.addCleanup(shutil.rmtree, folder, True)
        on_disk = folder / "slide-2.png"
        on_disk.write_bytes(b"\x89PNG" + b"y" * 1000)
        in_memory, too_big = str(folder / "never-written.jpg"), str(folder / "huge.jpg")
        held = {in_memory: ("image/jpeg", base64.b64encode(b"from the worker").decode("ascii")),
                too_big: ("image/jpeg", "A" * (4 * (tools.IMAGE_BYTES_MAX + 3) // 3)),
                str(folder / "another-parts.jpg"): ("image/jpeg", "QUJD")}
        sent = []
        call = SimpleNamespace(args=["apply", str(folder)], cwd=str(folder), timing={}, held_pictures=held,
                               image=lambda mime, path, data: sent.append((mime, path, data)) or True)
        rows = [{"slide": "s001", "path": in_memory}, {"slide": "s002", "path": str(on_disk)},
                {"slide": "s003", "path": too_big}, {"slide": "s004", "pending": True}]
        tools.attach_pictures(call, json.dumps({"ok": True, "pictures": rows}))
        self.assertEqual(sent, [("image/jpeg", in_memory, base64.b64encode(b"from the worker").decode("ascii")),
                                ("image/png", str(on_disk), base64.b64encode(on_disk.read_bytes()).decode("ascii"))])
        self.assertEqual(call.timing["pictures_from_worker"], 1)
        self.assertEqual(len(call.timing["pictures"]), 2, "read and sent, as before")
        del sent[:]
        plain = SimpleNamespace(args=["apply", str(folder)], cwd=str(folder), timing={},
                                image=lambda mime, path, data: sent.append(path) or True)
        tools.attach_pictures(plain, json.dumps({"ok": True, "pictures": rows}))   # nothing held: every file read
        self.assertEqual(sent, [str(on_disk)])
        self.assertNotIn("pictures_from_worker", plain.timing)

    @unittest.skipIf(os.name == "nt", "the Unix socket's send buffer")
    def test_the_service_may_write_a_client_more_than_a_picture_before_it_reads(self):
        import socket
        import tempfile

        from theodore_client.service import endpoint
        folder = Path(tempfile.mkdtemp(prefix="thsock-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, folder, True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(folder / "s"))
        server.listen(1)
        listener = endpoint.UnixListener.__new__(endpoint.UnixListener)
        listener._sock = server                                                 # noqa: SLF001
        server.settimeout(5)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(folder / "s"))
        conn = listener.accept()
        self.addCleanup(conn.close)
        self.addCleanup(client.close)
        fresh = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        default = fresh.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        fresh.close()
        self.assertGreater(conn.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF), default)
        self.assertGreaterEqual(endpoint.SEND_BUFFER, 1 << 20, "the Windows pipe's out buffer too")


class RouterCeiling(unittest.TestCase):
    def test_a_call_stuck_anywhere_is_answered_by_the_ceiling_and_its_stack_logged(self):
        from types import SimpleNamespace

        from theodore_client.service import router as routing
        logged = []
        service = SimpleNamespace(log=logged.append, call_ended=lambda _c: None)
        router = routing.Router(service)
        stuck = threading.Event()
        holder = {}

        def stuck_somewhere():
            holder["thread"] = threading.get_ident()
            stuck.wait(30)
        thread = threading.Thread(target=stuck_somewhere, daemon=True)
        thread.start()
        while "thread" not in holder:
            time.sleep(0.01)
        client = SimpleNamespace(session_id="s1", host="claude-code", role="relay", send=lambda _f: None)
        call = routing.Call(router, client, {"id": "c1", "tool": "pptx", "args": ["apply", "/w"], "cwd": "/"})
        call.thread = holder["thread"]
        call.started -= routing.ROUTER_CEILING_S + 1
        router.calls[("s1", "c1")] = call
        router.running = 1
        router.tick()
        self.assertTrue(call.finished.is_set(), "answered, however it is stuck")
        self.assertEqual(call.exit, 78)
        self.assertTrue(any(line.startswith("slow call pptx apply /w (c1)") and "stuck_somewhere" in line
                            for line in logged), logged)
        self.assertTrue(any("past the ceiling" in line for line in logged))
        said = "".join(f.get("data", "") for f in call.replay)
        self.assertIn("part of its result may be on disk", said, "not known whether it wrote: it may have")
        call.out("late output\n")
        self.assertNotIn("late output", "".join(f.get("data", "") for f in call.replay), "dropped after the answer")
        stuck.set()


class PoolSizing(unittest.TestCase):
    def test_a_worker_per_core_within_the_memory_share(self):
        from unittest import mock

        from theodore_client.service import workers
        gb = 1 << 30
        # The warm worker at the machine's full threads (min(4, cores - 1)), those a burst adds at min(4, cores // most),
        # 600 MB a worker and 250 MB more for each render thread beyond its first, within 40% of the memory:
        # (4, 16 GB) 1,100 + 3 x 600; (12, 16 GB) 1,350 + 8 x 600, 9 not 10; (8, 8 GB) 3 workers, the added ones at 2
        # threads; (64, 32 GB) 9 x 1,350; (16, 8 GB) 2 x 1,350.
        for cores, ram, most in ((4, 16 * gb, 4), (12, 16 * gb, 9), (8, 8 * gb, 3), (2, 4 * gb, 2), (1, 2 * gb, 2),
                                 (64, 256 * gb, 16), (64, 32 * gb, 9), (16, 8 * gb, 2), (8, 16 * gb, 8)):
            with self.subTest(cores=cores, ram=ram), mock.patch.dict(os.environ, {"THEODORE_SERVICE_WORKERS": ""}), \
                    mock.patch.object(workers.os, "cpu_count", return_value=cores), \
                    mock.patch.object(workers, "physical_cores", return_value=None), \
                    mock.patch.object(workers, "_ram_bytes", return_value=ram):
                self.assertEqual(workers.pool_sizes(), (1, most))
        # Physical cores and one where the system says (the coordinator after lane S2b's S2-19: rig 7's 4 vCPU on 2
        # cores started a 4th worker whose start-up ran beside the burst's builds): rig 7 3, rig 10 (8 on 4) 5; a
        # processor without hyperthreads keeps one a core.
        for logical, physical, most in ((4, 2, 3), (8, 4, 5), (10, 10, 10), (16, 8, 9), (2, 1, 2)):
            with self.subTest(logical=logical, physical=physical), \
                    mock.patch.dict(os.environ, {"THEODORE_SERVICE_WORKERS": ""}), \
                    mock.patch.object(workers.os, "cpu_count", return_value=logical), \
                    mock.patch.object(workers, "physical_cores", return_value=physical), \
                    mock.patch.object(workers, "_ram_bytes", return_value=256 * gb):
                self.assertEqual(workers.pool_sizes(), (1, most))
        found = workers.physical_cores()
        self.assertTrue(found is None or 1 <= found <= (os.cpu_count() or 1), f"this machine: {found}")
        with mock.patch.dict(os.environ, {"THEODORE_SERVICE_WORKERS": "3"}):
            self.assertEqual(workers.pool_sizes(), (3, 3))
        with mock.patch.object(workers.os, "cpu_count", return_value=64):
            self.assertEqual((workers.worker_threads(16), workers.worker_threads(9), workers.worker_threads(64)), (4, 4, 1))
        with mock.patch.object(workers.os, "cpu_count", return_value=4):
            self.assertEqual((workers.full_threads(), workers.worker_threads(4)), (3, 1), "rig 2: the warm one 3, the rest 1")

    def test_long_calls_take_the_warm_worker_and_short_ones_the_others(self):
        from types import SimpleNamespace
        from unittest import mock

        from theodore_client.service import workers
        with mock.patch.object(workers.os, "cpu_count", return_value=4), \
                mock.patch.object(workers, "pool_sizes", return_value=(1, 4)):
            pool = workers.Pool(SimpleNamespace(log=lambda _m: None))
            on = threading.Event()
            on.set()
            warm = SimpleNamespace(build_threads=3, busy=False, rank=None, keys=set(), calls=9, warmed=on)
            other = SimpleNamespace(build_threads=1, busy=False, rank=None, keys=set(), calls=0, warmed=on)
            live = [warm, other]
            long_, short = workers.RANKS["long"], workers.RANKS["write"]
            self.assertIs(pool._choose(live, [warm, other], None, long_, 0.0), warm, "a lead's apply: the warm worker")
            self.assertIs(pool._choose(live, [warm, other], None, short, 0.0), other, "a part's apply: another first")
            self.assertIs(pool._choose(live, [warm], None, short, 0.0), warm, "the warm one when it is the one idle")
            warm.busy, warm.rank = True, short
            self.assertIsNone(pool._choose(live, [other], None, long_, 0.0), "it finishes a short call: wait for it")
            self.assertIs(pool._choose(live, [other], None, long_, 0.6), other, "but not past WARM_WAIT_S")
            warm.rank = long_
            self.assertIs(pool._choose(live, [other], None, long_, 0.0), other, "busy with a long call: another at once")
            same = [SimpleNamespace(build_threads=3, busy=False, rank=None, keys=set(), calls=c, warmed=on) for c in (5, 1)]
            self.assertIs(pool._choose(same, same, None, long_, 0.0), same[1], "all alike: the least used")
            # The workspace's own worker first when idle: its memory, and the blocks built ahead after a slide verb
            # (lane F), which only an apply on it reuses.
            warm.busy, warm.rank = False, None
            pool.affinity["k1"] = other
            self.assertIs(pool._choose(live, [warm, other], "k1", long_, 0.0), other, "a lead's apply on its own worker")
            pool.affinity["k2"] = warm
            self.assertIs(pool._choose(live, [warm, other], "k2", short, 0.0), warm, "a slide verb on its own worker")
            self.assertIs(pool._choose(live, [warm], "k1", long_, 0.0), warm, "its own busy: the warm one")

    def test_a_worker_past_its_warm_up_is_preferred_among_the_idle_and_never_waited_for(self):
        # C2 `warm` (lane B) only orders the idle workers a call or an ahead may take (lane S2's S2-17, the coordinator).
        from types import SimpleNamespace
        from unittest import mock

        from theodore_client.service import workers
        with mock.patch.object(workers.os, "cpu_count", return_value=4), \
                mock.patch.object(workers, "pool_sizes", return_value=(1, 4)):
            pool = workers.Pool(SimpleNamespace(log=lambda _m: None))

            def worker(threads, warmed, calls):
                flags = {name: threading.Event() for name in ("ready", "warmed", "dead")}
                flags["ready"].set()
                if warmed:
                    flags["warmed"].set()
                return SimpleNamespace(build_threads=threads, busy=False, rank=None, keys=set(), calls=calls,
                                       retiring=False, fatal=None, **flags)
            full, cold, warm = worker(3, True, 0), worker(1, False, 0), worker(1, True, 9)
            live = [full, cold, warm]
            short, long_ = workers.RANKS["write"], workers.RANKS["long"]
            self.assertIs(pool._choose(live, [full, cold, warm], None, short, 0.0), warm,
                          "both idle: the one past its warm-up, though the other has fewer calls")
            self.assertIs(pool._choose(live, [full, cold], None, short, 0.0), cold, "no warm one besides: the cold one")
            full.busy, full.rank = True, long_
            self.assertIs(pool._choose(live, [cold, warm], None, long_, 0.0), warm, "a long call too")
            self.assertIs(pool._choose(live, [cold], None, long_, 0.0), cold, "and with none warm, the cold one at once")
            full.busy, full.rank = False, None
            pool.workers["9.9"] = live
            self.assertEqual(pool.ahead_slots("9.9"), 2, "the aheads count every idle worker but one")
            self.assertIs(pool.take_for_ahead("9.9"), full, "an ahead: the most build threads first")
            self.assertIs(pool.take_for_ahead("9.9"), warm, "then, of equal ones, the one past its warm-up")
            self.assertIsNone(pool.take_for_ahead("9.9"), "never the last idle worker")

    def test_the_warm_worker_has_the_full_threads_and_is_the_one_kept(self):
        from types import SimpleNamespace
        from unittest import mock

        from theodore_client.service import workers
        with mock.patch.object(workers.os, "cpu_count", return_value=4), \
                mock.patch.object(workers, "pool_sizes", return_value=(1, 4)):
            pool = workers.Pool(SimpleNamespace(log=lambda _m: None))
            self.assertEqual(pool._threads_for_new([]), 3, "the first of an engine: the machine's full counts")
            full = SimpleNamespace(build_threads=3)
            self.assertEqual(pool._threads_for_new([full]), 1, "one started beside it, under load: the per-worker cap")
            self.assertEqual(pool._threads_for_new([SimpleNamespace(build_threads=1)]), 3, "the full one went: taken again")
            retired = []

            def worker(name, threads, used):
                return SimpleNamespace(build_threads=threads, last_used=used, busy=False, retiring=False,
                                       dead=SimpleNamespace(is_set=lambda: False), ready=SimpleNamespace(is_set=lambda: True),
                                       retire=lambda grace, name=name: retired.append(name), ask_stats=lambda: None)
            now = time.monotonic()
            # the full-thread worker used longest ago, the burst's ones since: the shrink still keeps the full one
            # (names, not times: on a fresh machine the monotonic clock is small, and now - 900 is negative)
            pool.workers["9.9"] = [worker("warm", 3, now - 900), worker("added-1", 1, now - 800),
                                   worker("added-2", 1, now - 700)]
            with mock.patch.object(workers.install, "selected_engine", return_value="9.9"):
                pool.kept["9.9"] = (3, now + 60)       # a Studio deck's workers, kept until its designers' saves
                pool.tick()
                self.assertEqual(retired, [], "kept against the idle shrink while its burst may come")
                pool.kept["9.9"] = (3, now - 1)
                pool.tick()
            self.assertEqual(sorted(retired), ["added-1", "added-2"], "the warm worker stays")

    def test_only_the_services_end_tells_a_worker_to_stop_the_shared_office(self):
        # R's one office per user, joined by every worker: a recycle or a shrink lets go of it (B and R, 25 Nov)
        from types import SimpleNamespace

        from theodore_client.service import workers
        sent = []
        worker = SimpleNamespace(retiring=False, channel=SimpleNamespace(send=sent.append))
        workers.Worker.retire(worker, 0)                    # a shrink
        workers.Worker.retire(worker)                       # a recycle
        workers.Worker.retire(worker, 2000, final=True)     # the service's end (Pool.close)
        self.assertEqual(sent, [{"t": "exit", "grace_ms": 0}, {"t": "exit", "grace_ms": workers.EXIT_GRACE_MS},
                                {"t": "exit", "grace_ms": 2000, "offices": "stop"}])
        self.assertTrue(worker.retiring)

    def test_the_queue_goes_by_expected_cost(self):
        from theodore_client.service import workers
        self.assertEqual(workers.rank(["slide", "list", "/w"], "/"), 0)
        self.assertEqual(workers.rank(["apply", "/deck/parts/s03"], "/"), 1, "a Studio part's apply is short")
        self.assertEqual(workers.rank(["apply", "parts/s03"], "/deck"), 1)
        self.assertEqual(workers.rank(["slide", "set", "/w", "s1"], "/"), 1)
        self.assertEqual(workers.rank(["apply", "/deck"], "/"), 2)
        self.assertEqual(workers.rank(["render", "/deck"], "/"), 2)


class HostsStep(unittest.TestCase):
    def test_the_service_checks_the_hosts_every_five_minutes_not_every_guide(self):
        from unittest import mock

        from theodore_client import commands
        with mock.patch.object(commands.front, "refresh") as refresh, mock.patch.object(commands, "ensure_settings"), \
                mock.patch.object(commands, "_follow_claude_app"), mock.patch.object(commands, "_hosts_pending_path",
                                                                                   return_value=Path("/nonexistent/x")):
            with mock.patch.object(commands, "HOSTS_EVERY_S", 0.0), mock.patch.object(commands, "_hosts_checked", []):
                commands.ensure_hosts()
                commands.ensure_hosts()
                self.assertEqual(refresh.call_count, 2, "a one-shot command: every time")
            refresh.reset_mock()
            with mock.patch.object(commands, "HOSTS_EVERY_S", 300.0), mock.patch.object(commands, "_hosts_checked", []):
                for _ in range(15):
                    commands.ensure_hosts()
                self.assertEqual(refresh.call_count, 1, "the service: once in five minutes")


class Burst(ServiceCase):
    """Many designers at once (lane G's 15 part applies; the coordinator's sizing, 25 Nov)."""

    def answer(self, got, call_id: str) -> dict:
        return json.loads(Collector.stdout(got.wait(call_id, 60)).splitlines()[0])

    def test_long_calls_keep_a_worker_for_short_ones_and_the_queue_goes_by_cost(self):
        self.env["THEODORE_SERVICE_WORKERS"] = "2"
        self.service()
        client, got = self.connect()
        lead, other = self.box.tmp / "deck", self.box.tmp / "other deck"
        (lead / "parts" / "s01").mkdir(parents=True)
        other.mkdir()
        for i in range(2):                      # two warm workers
            self.call(client, got, f"w{i}", ["echo", "--hold", "0.3"], wait=False)
        for i in range(2):
            got.wait(f"w{i}", 60)
        self.call(client, got, "long1", ["apply", str(lead), "--hold", "2"], wait=False)
        time.sleep(0.2)
        self.call(client, got, "long2", ["render", str(other), "--hold", "0.5"], wait=False)
        time.sleep(0.2)
        self.call(client, got, "part", ["apply", str(lead / "parts" / "s01"), "--hold", "0.3"], wait=False)
        self.call(client, got, "read", ["slide", "list", str(lead), "--hold", "0.1"], wait=False)
        done = {name: self.answer(got, name) for name in ("long1", "long2", "part", "read")}
        self.assertLess(done["part"]["t1"], done["long1"]["t1"], "a part's apply did not wait behind a long call")
        self.assertLess(done["read"]["t1"], done["long1"]["t1"])
        self.assertLess(done["read"]["t0"], done["long2"]["t0"], "the read went before the long call waiting longer")
        self.assertGreaterEqual(done["long2"]["t0"], done["long1"]["t1"] - 0.05,
                                "long calls never held both workers: one stayed for short calls")

    def test_a_burst_starts_its_workers_side_by_side(self):
        self.env.update({"THEODORE_SERVICE_WORKERS": "4", "FAKE_WORKER_START_S": "1"})
        self.service()
        client, got = self.connect()
        for i in range(4):
            self.call(client, got, f"b{i}", ["echo", "--hold", "1.5"], wait=False)
        pids = {self.answer(got, f"b{i}")["pid"] for i in range(4)}
        self.assertEqual(len(pids), 4, "four workers started at once, one a call (one after another, the first would "
                                       "have served several before the last was up)")

    def serving(self) -> int:
        return len([p for p in sandbox_processes(self.box.tmp.name) if "engine.py serve" in p[1]])

    def test_prepare_warms_a_worker_a_part_within_the_pools_size(self):
        # The coordinator after lane S2b's S2-19: the designers' first calls find their workers ready, and the burst
        # starts none of its own. Two parts: two workers; nine on a pool of four: four (with an engine that takes
        # several parts in a build, which warmed one worker for three parts before).
        self.env.update({"THEODORE_SERVICE_WORKERS": "4", "FAKE_APPLY_PARTS": "1"})
        self.service()
        client, got = self.connect()
        for parts, want in ((2, 2), (9, 4)):
            deck = self.box.tmp / f"deck {parts}"
            deck.mkdir()
            self.call(client, got, f"prep{parts}", ["prepare", str(deck), "--parts", str(parts)])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and self.serving() < want:
                time.sleep(0.1)
            time.sleep(1.0)
            self.assertEqual(self.serving(), want, f"{parts} parts")
        self.assertIn("parts: prepare says 9 parts come at once", self.wait_log(r"prepare says 9 parts"))

    def test_prepares_warm_up_ends_at_its_designers_first_call(self):
        # Warming at prepare never runs beside the designers' calls: the workers still starting when the first of them
        # names a part stop, and the call runs on a worker that is ready.
        self.env.update({"THEODORE_SERVICE_WORKERS": "3", "FAKE_WORKER_START_S": "4"})
        self.service()
        client, got = self.connect()
        lead = self.box.tmp / "deck"
        lead.mkdir()
        self.call(client, got, "prep", ["prepare", str(lead), "--parts", "3"])     # its own worker, then two more
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and self.serving() < 3:
            time.sleep(0.1)
        opened = self.call(client, got, "open", ["slide", "list", str(lead / "parts" / "s01")])
        self.assertEqual(Collector.done(opened)["exit"], 0)
        self.assertRegex(self.wait_log(r"warm-up: \d worker"), r"warm-up: [123] worker\(s\) still starting stopped at "
                         r"its designers' first call \(pptx slide list\)", "the one it had not started yet never starts")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.serving() > 1:
            time.sleep(0.1)
        self.assertEqual(self.serving(), 1, "only the worker that was ready")
        again = self.call(client, got, "again", ["echo"])
        self.assertEqual(json.loads(Collector.stdout(again))["resident"], "1", "a cut warm-up is no failed start")

    def test_a_call_that_would_queue_less_than_a_start_starts_no_worker(self):
        self.env.update({"THEODORE_SERVICE_WORKERS": "2", "THEODORE_COLD_START_S": "2"})
        self.service()
        client, got = self.connect()
        self.call(client, got, "w", ["echo"])
        first = self.answer(got, "w")
        self.call(client, got, "short", ["echo", "--hold", "0.5"], wait=False)
        time.sleep(0.1)
        self.call(client, got, "next", ["echo"], wait=False)
        done = got.wait("next", 30)
        answer = json.loads(Collector.stdout(done).splitlines()[0])
        self.assertEqual(answer["pid"], first["pid"], "it waited for the busy worker")
        self.assertEqual(int((Collector.done(done).get("counters") or {}).get("spawned") or 0), 0)
        self.assertEqual(self.serving(), 1, "no worker started beside it")

    def test_a_call_queued_longer_than_a_start_starts_one(self):
        self.env.update({"THEODORE_SERVICE_WORKERS": "2", "THEODORE_COLD_START_S": "0.5"})
        self.service()
        client, got = self.connect()
        self.call(client, got, "w", ["echo"])
        first = self.answer(got, "w")
        self.call(client, got, "long", ["echo", "--hold", "6"], wait=False)
        time.sleep(0.2)
        began = time.monotonic()
        done = self.call(client, got, "next", ["echo"])
        self.assertLess(time.monotonic() - began, 4.5, "a worker started for it after about a start's wait")
        self.assertNotEqual(json.loads(Collector.stdout(done).splitlines()[0])["pid"], first["pid"])
        self.assertEqual(int((Collector.done(done).get("counters") or {}).get("spawned") or 0), 1)
        got.wait("long", 30)

    def test_prepare_warms_the_workers_its_parts_will_need(self):
        self.env["THEODORE_SERVICE_WORKERS"] = "3"
        self.service()
        client, got = self.connect()
        deck = self.box.tmp / "deck"
        deck.mkdir()
        self.call(client, got, "prep", ["prepare", str(deck)])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and \
                len([p for p in sandbox_processes(self.box.tmp.name) if "engine.py serve" in p[1]]) < 3:
            time.sleep(0.1)
        time.sleep(0.5)                         # their warm-up (the stand-in's is quick)
        for i in range(3):
            self.call(client, got, f"p{i}", ["apply", str(deck / "parts" / f"s0{i}"), "--hold", "0.5"], wait=False)
        frames_ = [got.wait(f"p{i}", 60) for i in range(3)]
        spawned = sum(int((Collector.done(f).get("counters") or {}).get("spawned") or 0) for f in frames_)
        self.assertEqual(spawned, 0, "no part's apply waited for a worker to start")
        self.assertEqual(len({json.loads(Collector.stdout(f).splitlines()[0])["pid"] for f in frames_}), 3)


class FirstCall(ServiceCase):
    """A long call that takes the pool's last idle worker has the machine (lane firstrun, 26 Nov): the spare for the
    short calls to come never starts beside it. A new user's first command (`start --from` as the service starts) took
    18 to 29 s on rig 9 (2 processors) with a spare worker started beside it, against 0.2 to 3 s the same command 30 s
    later; a `start` 30 s after its session opened took 2.2 to 2.3 s on rig 6 (4 processors) against 1.7 to 1.9 s with
    no spare (starts-f). The pool's second worker starts the moment that call has answered."""

    def answer(self, got, call_id: str) -> dict:
        return json.loads(Collector.stdout(got.wait(call_id, 60)).splitlines()[0])

    def serving(self) -> int:
        return len([p for p in sandbox_processes(self.box.tmp.name) if "engine.py serve" in p[1]])

    def test_no_worker_starts_beside_a_long_call_and_a_call_that_waits_gets_one_at_once(self):
        # A start would take 30 s to be decided by the cold-start estimate: the call that waits must not wait for that.
        # Both on a worker still in its warm-up (the first command ever) and on one long past it.
        for warming in ("60", "0.1"):
            with self.subTest(warm_after=warming):
                self.env.update({"THEODORE_SERVICE_WORKERS": "2", "FAKE_WARM_AFTER": warming, "THEODORE_COLD_START_S": "30"})
                self.service()
                client, got = self.connect()
                deck = self.box.tmp / f"deck {warming}"
                deck.mkdir()
                if warming == "0.1":
                    self.call(client, got, "w", ["echo"])
                    self.wait_log(r"warm: its warm-up ended")
                self.call(client, got, "first", ["render", str(deck), "--hold", "6"], wait=False)
                time.sleep(1.5)
                self.assertEqual(self.serving(), 1, "a second worker started beside the long call")
                self.assertFalse(got.event("first").is_set())
                began = time.monotonic()
                done = self.call(client, got, "next", ["echo"])
                self.assertLess(time.monotonic() - began, 4.0, "a call that found the one worker busy waited for the estimate")
                self.assertEqual(int((Collector.done(done).get("counters") or {}).get("spawned") or 0), 1)
                first = self.answer(got, "first")
                self.assertNotEqual(json.loads(Collector.stdout(done).splitlines()[0])["pid"], first["pid"])
                self.stop_service()


    def test_the_second_worker_starts_at_the_first_calls_answer_and_a_short_queue_still_grows_nothing(self):
        """The coordinator's ruling (26 Nov): no spare while the first call runs; the pool's second worker within a
        moment of its answer, so it is warm when a Studio deck's burst begins; and a burst's short queue still starts
        no worker (S2b's rule: a call that would queue less than a start pays none)."""
        self.env.update({"THEODORE_SERVICE_WORKERS": "4", "FAKE_WARM_AFTER": "60", "THEODORE_COLD_START_S": "30"})
        self.service()
        client, got = self.connect()
        deck = self.box.tmp / "deck"
        deck.mkdir()
        self.call(client, got, "first", ["render", str(deck), "--hold", "2"], wait=False)
        time.sleep(1.2)
        self.assertEqual(self.serving(), 1, "a second worker started beside the first call")
        first = self.answer(got, "first")
        answered = time.monotonic()
        while self.serving() < 2 and time.monotonic() - answered < 3.0:
            time.sleep(0.02)
        self.assertEqual(self.serving(), 2, "the second worker did not start within a moment of the first call's answer")
        self.wait_log(r"(?s)worker \S+ ready.*worker \S+ ready")
        # A burst's short queue on the two workers, the pool with room for four: the calls that wait a moment start none.
        for n in range(4):
            self.call(client, got, f"b{n}", ["echo", "--hold", "0.4"], wait=False)
        done = [got.wait(f"b{n}", 30) for n in range(4)]
        self.assertEqual([Collector.done(frames)["exit"] for frames in done], [0] * 4)
        self.assertEqual(sum(int((Collector.done(frames).get("counters") or {}).get("spawned") or 0) for frames in done), 0)
        self.assertEqual(self.serving(), 2, "a short queue grew the pool")
        self.assertEqual(len({json.loads(Collector.stdout(frames).splitlines()[0])["pid"] for frames in done} | {first["pid"]}), 2)


class FirstCallUnit(unittest.TestCase):
    """The spare's rule on the pool itself (workers.Pool._spare, _acquire's wait, _release), no process started."""

    def pool(self, cores: int, most: int = 3):
        from types import SimpleNamespace
        from unittest import mock

        from theodore_client.service import workers
        patches = [mock.patch.object(workers.os, "cpu_count", return_value=cores),
                   mock.patch.object(workers, "pool_sizes", return_value=(1, most))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        pool = workers.Pool(SimpleNamespace(log=lambda _m: None))
        started = []
        pool._spawn = lambda *how: started.append(how)
        return workers, pool, started

    def worker(self, warmed: bool, busy: bool = True):
        from types import SimpleNamespace
        flags = {name: threading.Event() for name in ("ready", "warmed", "dead")}
        flags["ready"].set()
        if warmed:
            flags["warmed"].set()
        return SimpleNamespace(group="9.9", busy=busy, rank=2, retiring=False, fatal=None, spare_due=None, calls=1,
                               rss_mb=20.0, keys=set(), **flags)

    def settle(self, started: list, count: int) -> list:
        deadline = time.monotonic() + 2.0
        while len(started) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        return started

    def test_a_long_call_starts_no_spare_beside_it_whether_its_worker_warms_or_not(self):
        workers, pool, started = self.pool(cores=8)
        how = ("spec", "9.9", None, 3)
        for warmed in (False, True):
            taken = self.worker(warmed=warmed)
            with pool.cond:
                pool._spare(taken, how)
            self.assertEqual(self.settle(started, 0), [], f"warmed {warmed}")
            self.assertEqual(taken.spare_due, how)

    def test_once_the_long_call_has_answered_the_second_worker_starts_on_any_machine(self):
        how = ("spec", "9.9", None, 3)
        for cores in (1, 2, 4, 8):
            workers, pool, started = self.pool(cores=cores)
            taken = self.worker(warmed=cores % 2 == 0)
            taken.spare_due = how
            pool.workers["9.9"] = [taken]
            pool._release(taken)
            self.assertEqual(self.settle(started, 1), [how], f"{cores} processors")
            self.assertIsNone(taken.spare_due)
            self.assertFalse(taken.busy)
        # never a third worker for it (a call that waited meanwhile started the second), nothing owed where none was
        # held back, and nothing once the service is closing
        workers, pool, started = self.pool(cores=8)
        taken, other = self.worker(warmed=False), self.worker(warmed=True, busy=False)
        taken.spare_due = how
        pool.workers["9.9"] = [taken, other]
        pool._release(taken)
        pool._release(other)
        taken.spare_due, pool.workers["9.9"], pool.closed = how, [taken], True
        pool._release(taken)
        self.assertEqual(self.settle(started, 0), [])


class Timings(ServiceCase):
    def log_with(self, text: str, seconds: float = 15.0) -> str:
        """service.log once it holds `text` (a call's timing line is written just after its answer), or as it is."""
        deadline = time.monotonic() + seconds
        log = ""
        while time.monotonic() < deadline:
            log = (self.box.home / "logs" / "service.log").read_text(encoding="utf-8", errors="replace")
            if text in log:
                break
            time.sleep(0.05)
        return log

    def test_with_timings_on_each_pptx_call_and_each_worker_say_their_time_in_the_log(self):
        self.env.update({"THEODORE_SERVICE_TIMINGS": "1", "FAKE_APPLY_PARTS": "1"})
        self.service()
        client, got = self.connect()
        self.call(client, got, "t", ["echo"], env={"THEODORE_TIMINGS": "1"})
        log = self.log_with("timing: pptx echo")       # written once the call's done frame is out (588da9e)
        self.assertRegex(log, r"worker \S+ ready \d+ ms after its start \(its warm-up .*features: apply-parts; threads: "
                              r"build \d+, render \d+\)")
        self.assertRegex(log, r"timing: pptx echo : \d+ ms = admission \d+ \+ queue \d+ \+ worker \d+ \(the engine's own"
                              r"[^)]*\) \+ after \d+;")
        self.assertIn("timings: echo 5 ms (fake.step 5)", log, "the engine's own line, under the call's")
        self.assertIn("    gc: full collection 21 ms", log, "and its gc line (lane F), under the call's too")
        # A Studio part's apply: what it waited in the gathering, as its own term (lane S2)
        (self.box.tmp / "deck").mkdir()
        self.call(client, got, "prep", ["prepare", str(self.box.tmp / "deck"), "--parts", "2"])
        self.call(client, got, "p1", ["apply", str(self.box.tmp / "deck" / "parts" / "s01")])
        log = self.log_with("timing: pptx apply s01")
        self.assertRegex(log, r"timing: pptx apply s01: \d+ ms = admission \d+ \+ (?:own ahead \d+ \+ )?gathering \d+ "
                              r"\(1 part\(s\) in its build\) \+ queue")


class StudioParts(ServiceCase):
    """A Studio deck's parts applied together (lanes G and F; the coordinator's decision, 25 Nov): a lone part's apply
    runs at once; the others of a burst gather and go in one build per free worker; each designer gets its own answer."""

    def prepared(self, parts: int, in_turn: bool = False) -> Path:
        lead = self.box.tmp / "deck"
        lead.mkdir(exist_ok=True)
        self.client_, self.got = self.connect()
        args = ["prepare", str(lead), "--parts", str(parts)] + (["--in-turn"] if in_turn else [])
        self.call(self.client_, self.got, "prep", args)
        deadline = time.monotonic() + 30
        want = int(self.env["THEODORE_SERVICE_WORKERS"])
        while time.monotonic() < deadline and \
                len([p for p in sandbox_processes(self.box.tmp.name) if "engine.py serve" in p[1]]) < want:
            time.sleep(0.1)
        time.sleep(0.5)
        return lead

    def burst(self, lead: Path, names: list[str], hold: str = "0.3", tag: str = "") -> dict:
        for name in names:
            self.call(self.client_, self.got, tag + name, ["apply", str(lead / "parts" / name), "--hold", hold], wait=False)
        return {name: self.got.wait(tag + name, 60) for name in names}

    def test_a_build_takes_at_most_the_burst_over_the_pool_and_the_rest_go_as_workers_free(self):
        # Lane S2's rig 7: with three workers busy, 12 parts went to the one free worker as one build of 2 to 4 s while
        # the others came free within 0.5 s. A build now takes at most ceil(parts / workers): 4 here (8 on 2).
        self.env.update({"THEODORE_SERVICE_WORKERS": "2", "FAKE_APPLY_PARTS": "1"})
        self.service()
        lead = self.prepared(8)
        self.call(self.client_, self.got, "busy", ["sleep", "1.0"], wait=False)     # one worker held for a while
        time.sleep(0.3)
        names = [f"s{i:02d}" for i in range(1, 9)]
        frames_ = self.burst(lead, names)
        batches = []
        for name in names:
            self.assertEqual(Collector.done(frames_[name])["exit"], 0, frames_[name])
            batches.append(json.loads(Collector.stdout(frames_[name]).splitlines()[0]).get("batch", 1))
        self.assertLessEqual(max(batches), 4, f"at most ceil(8 parts / 2 workers) in one build: {batches}")
        self.got.wait("busy", 30)

    def test_a_burst_goes_in_one_build_per_free_worker_and_each_gets_its_own_answer(self):
        self.env.update({"THEODORE_SERVICE_WORKERS": "2", "FAKE_APPLY_PARTS": "1"})
        self.service()
        lead = self.prepared(7)
        names = [f"s{i:02d}" for i in range(1, 8)]
        frames_ = self.burst(lead, names)
        batches = []
        for name in names:
            done = Collector.done(frames_[name])
            self.assertEqual(done["exit"], 0, frames_[name])
            answer = json.loads(Collector.stdout(frames_[name]).splitlines()[0])
            self.assertEqual(Path(answer["argv"][1]).name, name, "each designer got its own part's answer")
            batches.append(answer.get("batch", 1))
        # The first may run alone when it reaches the gathering before the others reached the service; every other part
        # of the burst is gathered, in equal shares over the free workers, at most ceil(7 parts / 2 workers) = 4 in a
        # build; under load the cap can leave one part over, which then goes alone too.
        self.assertLessEqual(batches.count(1), 2, f"at most the first and one left over ran alone: {batches}")
        self.assertGreaterEqual(max(batches), 2, f"the others went in builds of several: {batches}")
        self.assertLessEqual(max(batches), 4, f"never more than the cap in a build: {batches}")
        self.assertLessEqual(len(set(batches) - {1}), 2, f"in equal shares: {batches}")
        lone = json.loads(Collector.stdout(self.burst(lead, ["s01"], hold="0", tag="later-")["s01"]).splitlines()[0])
        self.assertNotIn("batch", lone, "a part alone afterwards: the plain apply")
        cores = os.cpu_count() or 2
        self.assertIn(lone["build_threads"], {str(min(4, max(1, cores - 1))), str(min(4, max(1, cores // 2)))},
                      "THEODORE_BUILD_THREADS: the warm worker's full counts, or the cap of one started under load")
        self.assertEqual(lone["render_threads"], lone["build_threads"], "THEODORE_RENDER_THREADS, the same cap")
        log = (self.box.home / "logs" / "service.log").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(log, r"parts: \d+ of a deck in \d+ build\(s\)", "the log says how a burst was built")

    def test_each_designer_gets_its_own_picture_from_the_workers_memory_and_no_file_is_read(self):
        # Lane S2's S2-23: the service's first read of a picture a worker drew moments before cost 32 to 476 ms of a
        # designer's answer on Windows. A worker that holds the pictures sends their bytes with the run; a run of
        # several parts keeps them by path and each designer's answer carries its own. No file exists here at all.
        import base64
        pictures = self.box.tmp / "no-such-store"
        self.env.update({"THEODORE_SERVICE_WORKERS": "2", "FAKE_APPLY_PARTS": "1", "FAKE_PARTS_PICTURES": str(pictures),
                         "THEODORE_SERVICE_TIMINGS": "1"})
        self.service()
        lead = self.prepared(6)
        names = [f"s{i:02d}" for i in range(1, 7)]
        for name in names:
            self.call(self.client_, self.got, name, ["apply", str(lead / "parts" / name), "--hold", "0.3"], wait=False,
                      images=True)
        batches = []
        for name in names:
            frames_ = self.got.wait(name, 60)
            self.assertEqual(Collector.done(frames_)["exit"], 0, frames_)
            answer = json.loads(Collector.stdout(frames_).splitlines()[0])
            self.assertTrue(answer["images"], "the run was asked for its pictures, alone or with others")
            batches.append(answer["batch"])
            images = [f for f in frames_ if f["t"] == "image"]
            self.assertEqual([(f["index"], f["path"], f["mime"]) for f in images],
                             [(0, str(pictures / f"{name}.jpg"), "image/jpeg")], "its own picture, once")
            self.assertEqual(base64.b64decode(images[0]["data"]), f"the picture of {name}".encode())
        self.assertFalse(pictures.exists(), "no file was there to read")
        self.assertGreaterEqual(max(batches), 2, f"some went in a build of several: {batches}")
        quiet = self.call(self.client_, self.got, "quiet", ["apply", str(lead / "parts" / "s01"), "--hold", "0"],
                          images=False)
        self.assertEqual([f for f in quiet if f["t"] == "image"], [], "a caller that takes no pictures gets none")
        self.assertFalse(json.loads(Collector.stdout(quiet).splitlines()[0])["images"])
        log = (self.box.home / "logs" / "service.log").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(log, r"timing: pptx apply s0\d: .*\(pictures read 0, sent \d+, 1 from the worker\)",
                         "the timing line says the picture came from the worker")

    def test_a_lone_part_apply_runs_at_once_as_a_plain_apply(self):
        self.env.update({"THEODORE_SERVICE_WORKERS": "2", "FAKE_APPLY_PARTS": "1"})
        self.service()
        lead = self.prepared(3)
        frames_ = self.burst(lead, ["s02"], hold="0")
        answer = json.loads(Collector.stdout(frames_["s02"]).splitlines()[0])
        self.assertNotIn("batch", answer, "alone: the plain apply, no window, no batch")
        self.assertEqual(answer["argv"][:2], ["apply", str(lead / "parts" / "s02")])

    def test_a_run_refused_as_a_whole_answers_every_designer(self):
        self.env.update({"THEODORE_SERVICE_WORKERS": "2", "FAKE_APPLY_PARTS": "1", "FAKE_PARTS_REFUSED": "1"})
        self.service()
        lead = self.prepared(5)
        frames_ = self.burst(lead, [f"s{i:02d}" for i in range(1, 6)])
        exits = sorted(Collector.done(got)["exit"] for got in frames_.values())
        # Every designer in a build of several parts gets the run's own refusal. A part in a build alone runs as a plain
        # apply (exit 0): the first, when it came before the others, and one the cap of ceil(5 parts / 2 workers) = 3
        # left over when one worker was free.
        self.assertTrue(set(exits) <= {0, 2} and exits.count(0) <= 2 and exits.count(2) >= 3, exits)
        refused = [got for got in frames_.values() if Collector.done(got)["exit"] == 2]
        self.assertTrue(all('"ok": false' in Collector.stdout(got) for got in refused))

    def test_parts_that_come_in_turn_are_never_gathered(self):
        self.env.update({"THEODORE_SERVICE_WORKERS": "2", "FAKE_APPLY_PARTS": "1"})
        self.service()
        lead = self.prepared(4, in_turn=True)
        frames_ = self.burst(lead, ["s01", "s02", "s03", "s04"])
        for name, got in frames_.items():
            answer = json.loads(Collector.stdout(got).splitlines()[0])
            self.assertNotIn("batch", answer, name)

    def test_a_burst_on_workers_still_warming_is_gathered_onto_them_at_once(self):
        # Lane S2's S2-17 (the coordinator): the gathering counted only workers past their warm-up (C2 `warm`, lane B)
        # as free, so a burst right after prepare waited 700-1,030 ms. It cuts its builds for every idle worker.
        self.env.update({"THEODORE_SERVICE_WORKERS": "2", "FAKE_APPLY_PARTS": "1", "FAKE_WARM_AFTER": "60"})
        self.service()
        lead = self.prepared(6)
        self.wait_log(r"(?s)ready \d+ ms after.*ready \d+ ms after")          # both workers ready, neither warm
        names = [f"s{i:02d}" for i in range(1, 7)]
        began = time.monotonic()
        frames_ = self.burst(lead, names, hold="0.2")
        took = time.monotonic() - began
        for name in names:
            self.assertEqual(Collector.done(frames_[name])["exit"], 0, frames_[name])
        log = self.wait_log(r"parts: \d+ of a deck in \d+ build\(s\)")
        self.assertRegex(log, r"parts: \d+ of a deck in \d+ build\(s\)", "gathered onto the workers still warming")
        self.assertNotIn("warm: its warm-up ended", log, "neither worker was past its warm-up")
        self.assertLess(took, 1.0, f"never waiting for a warm worker (the gathering's 1 s for a free one): {took:.2f} s")


class BuildAhead(ServiceCase):
    """A Studio part built before its designer applies it (ahead.py; the coordinator, 25 Nov; lanes S2 and G): a saved
    part's deck.js gets `apply <part> --ahead` on an idle worker; never the last idle one, never without the engine's
    feature; a newer save or the designer's apply on a newer deck.js cancels it; a call with no idle worker takes it."""

    prepared = StudioParts.prepared
    burst = StudioParts.burst

    def setUp(self):
        super().setUp()
        self.aheads_file = self.box.tmp / "aheads.jsonl"
        self.env.update({"FAKE_APPLY_PARTS": "1", "FAKE_APPLY_AHEAD": "1", "FAKE_AHEADS": str(self.aheads_file),
                         "THEODORE_SERVICE_WORKERS": "2"})

    def events(self) -> list[tuple[str, str]]:
        try:
            lines = self.aheads_file.read_text().splitlines()
        except FileNotFoundError:
            return []
        return [(row["event"], row["part"]) for row in map(json.loads, lines)]

    def wait_for(self, event: tuple[str, str], seconds: float = 10.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if event in self.events():
                return True
            time.sleep(0.05)
        return False

    def save(self, lead: Path, part: str, text: str) -> None:
        (lead / "parts" / part / "deck.js").write_text(text)     # a designer's file tool: one whole-file write

    def test_a_saved_part_is_built_ahead_and_its_apply_comes_after(self):
        self.service()
        lead = self.prepared(3)
        self.save(lead, "s01", "slide one, saved\n")
        self.assertTrue(self.wait_for(("ahead-built", "s01")), self.events())
        applied = self.call(self.client_, self.got, "a1", ["apply", str(lead / "parts" / "s01")])
        self.assertEqual(Collector.done(applied)["exit"], 0)
        self.assertEqual(self.events(), [("ahead-start", "s01"), ("ahead-built", "s01"), ("apply", "s01")],
                         "one ahead for the saved part, then the designer's apply; none for the parts not saved")
        log = self.wait_log(r"ahead: 1 part\(s\) \(s01\) done in")
        self.assertIn("ahead: watching 3 part(s) of deck", log)
        self.assertRegex(log, r"ahead: 1 part\(s\) \(s01\) done in \d+ ms \(exit 0\): 1 built")

    def test_the_designers_apply_waits_for_its_own_ahead_on_the_same_save(self):
        self.env["FAKE_AHEAD_S"] = "1.0"
        self.service()
        lead = self.prepared(2)
        self.save(lead, "s01", "slide one, saved\n")
        self.assertTrue(self.wait_for(("ahead-start", "s01")), self.events())
        self.call(self.client_, self.got, "a1", ["apply", str(lead / "parts" / "s01")])
        self.assertEqual(self.events(), [("ahead-start", "s01"), ("ahead-built", "s01"), ("apply", "s01")],
                         "the apply went after its own ahead, which it reuses")

    def test_an_apply_on_a_newer_save_cancels_the_ahead_and_goes_at_once(self):
        self.env["FAKE_AHEAD_S"] = "12"
        self.service()
        lead = self.prepared(2)
        self.save(lead, "s01", "slide one, saved\n")
        self.assertTrue(self.wait_for(("ahead-start", "s01")), self.events())
        self.save(lead, "s01", "slide one, saved again with more words\n")
        began = time.monotonic()
        applied = self.call(self.client_, self.got, "a1", ["apply", str(lead / "parts" / "s01")])
        self.assertEqual(Collector.done(applied)["exit"], 0)
        self.assertLess(time.monotonic() - began, 9.0, "not after the 12 s ahead (room for a loaded machine)")
        self.assertTrue(self.wait_for(("ahead-cancelled", "s01")), self.events())

    def test_a_call_that_finds_no_idle_worker_takes_the_aheads(self):
        self.env["FAKE_AHEAD_S"] = "20"
        self.service()
        lead = self.prepared(2)
        self.save(lead, "s01", "slide one, saved\n")
        self.assertTrue(self.wait_for(("ahead-start", "s01")), self.events())
        self.call(self.client_, self.got, "long", ["sleep", "12"], wait=False)    # the other worker, now busy
        time.sleep(0.5)
        began = time.monotonic()
        quick = self.call(self.client_, self.got, "quick", ["echo"])
        self.assertEqual(Collector.done(quick)["exit"], 0)
        self.assertLess(time.monotonic() - began, 9.0, "on the ahead's worker, not after the 12 s call or the ahead")
        self.assertTrue(self.wait_for(("ahead-cancelled", "s01")), self.events())
        self.assertIn("cancelling: a call waits for a worker", (self.box.home / "logs" / "service.log").read_text())
        self.got.wait("long", 30)

    def test_a_bursts_saves_go_out_together_as_several_part_aheads(self):
        # Lane S2's rig 7: single-part aheads ran three at a time and 12 of 15 were still queued when their designers
        # applied. A burst's saved parts now go as one several-part ahead per worker allowed (one here: two workers,
        # one kept idle), so they all start building at once.
        self.env["THEODORE_TIMINGS"] = "1"          # the engine's own line goes under the ahead's (lane S2)
        self.service()
        lead = self.prepared(3)
        for name in ("s01", "s02", "s03"):
            self.save(lead, name, f"slide {name}, saved\n")
        self.assertTrue(all(self.wait_for(("ahead-built", name)) for name in ("s01", "s02", "s03")), self.events())
        rows = [json.loads(line) for line in self.aheads_file.read_text().splitlines()]
        starts = [row for row in rows if row["event"] == "ahead-start"]
        self.assertEqual(sorted(row["part"] for row in starts), ["s01", "s02", "s03"], "each part once")
        self.assertEqual(len({row["pid"] for row in starts}), 1, "in one call, on one worker")
        first_built = min(row["t"] for row in rows if row["event"] == "ahead-built")
        self.assertTrue(all(row["t"] <= first_built for row in starts), "all started before any was built")
        log = self.wait_log(r"ahead: 3 part\(s\) \(s01, s02, s03\) done in \d+ ms[^\n]*\n    timings[^\n]*\n    gc: ")
        self.assertRegex(log, r"ahead: 3 part\(s\) \(s01, s02, s03\) done in \d+ ms \(exit 0\): 3 built; [^\n]*\n"
                              r"    timings: apply-parts 3 part\(s\) 300 ms[^\n]*\n    gc: full collection 22 ms")

    def test_parts_built_ahead_on_one_worker_are_applied_there_together(self):
        # Lane S2's S2-6: a burst's applies spread over every worker, and each worker but the one that built the
        # ahead read it afresh from disk (96 to 430 ms against ~1 ms). They now go to that worker, in one call.
        self.service()
        lead = self.prepared(3)
        for name in ("s01", "s02", "s03"):
            self.save(lead, name, f"slide {name}, saved\n")
        self.assertTrue(all(self.wait_for(("ahead-built", name)) for name in ("s01", "s02", "s03")), self.events())
        built_on = {row["pid"] for row in map(json.loads, self.aheads_file.read_text().splitlines())
                    if row["event"] == "ahead-built"}
        self.assertEqual(len(built_on), 1, "one several-part ahead")
        time.sleep(0.3)
        frames_ = self.burst(lead, ["s01", "s02", "s03"], hold="0")
        for name, got in frames_.items():
            self.assertEqual(Collector.done(got)["exit"], 0, got)
            answer = json.loads(Collector.stdout(got).splitlines()[0])
            self.assertEqual({answer["pid"]}, built_on, f"{name}: applied on the worker that built its ahead")
        self.assertIn("on the worker that built their ahead", (self.box.home / "logs" / "service.log").read_text())

    def test_applies_that_wait_for_their_ahead_go_to_its_worker_together_when_it_ends(self):
        # Lane S2's S2-31 on rig 7: the designers applied 30 to 80 ms before their 4-part ahead ended. They waited for
        # it, then the first went alone to the ahead's worker and the rest to another worker. They now go together,
        # to the worker that holds what the ahead built.
        self.env["FAKE_AHEAD_S"] = "1.2"          # two workers (setUp): one ahead of the three, one worker idle
        self.service()
        lead = self.prepared(3)
        names = ["s01", "s02", "s03"]
        for name in names:
            self.save(lead, name, f"slide {name}, saved\n")
        self.assertTrue(all(self.wait_for(("ahead-start", name)) for name in names), self.events())
        self.assertNotIn(("ahead-built", "s01"), self.events(), "the ahead is still running when they apply")
        frames_ = self.burst(lead, names, hold="0")
        rows = [json.loads(line) for line in self.aheads_file.read_text().splitlines()]
        built_on = {row["pid"] for row in rows if row["event"] == "ahead-built"}
        self.assertEqual(len(built_on), 1, rows)
        for name, got in frames_.items():
            self.assertEqual(Collector.done(got)["exit"], 0, got)
            answer = json.loads(Collector.stdout(got).splitlines()[0])
            self.assertEqual({answer["pid"]}, built_on, f"{name}: applied on the worker that built its ahead")
            self.assertEqual(answer.get("batch"), 3, f"{name}: in one build with the others that waited: {answer}")
        self.assertEqual([e for e in self.events() if e[0] == "ahead-cancelled"], [])
        log = self.wait_log(r"parts: 3 of a deck in 1 build\(s\) \(3\) on the worker that built their ahead")
        self.assertRegex(log, r"parts: 3 of a deck in 1 build\(s\) \(3\) on the worker that built their ahead")

    def test_a_designers_apply_never_waits_behind_another_parts_ahead(self):
        # Aheads take every idle worker but one (lane S2b's S2-18: the always-warm worker too), so the apply of a part
        # with no ahead of its own runs at once, and the ahead running goes on.
        self.env["FAKE_AHEAD_S"] = "4"
        self.service()
        lead = self.prepared(2)
        self.save(lead, "s01", "slide one, saved\n")
        self.assertTrue(self.wait_for(("ahead-start", "s01")), self.events())
        began = time.monotonic()
        applied = self.call(self.client_, self.got, "a2", ["apply", str(lead / "parts" / "s02")])
        self.assertEqual(Collector.done(applied)["exit"], 0)
        self.assertLess(time.monotonic() - began, 2.5, "at once, not after the 4 s ahead")
        self.assertTrue(self.wait_for(("ahead-built", "s01")), self.events())
        self.assertNotIn(("ahead-cancelled", "s01"), self.events(), "the ahead was left alone")

    def test_nothing_is_built_ahead_without_the_feature_or_a_second_idle_worker(self):
        for env in ({"FAKE_APPLY_AHEAD": ""}, {"THEODORE_SERVICE_WORKERS": "1"}):
            with self.subTest(env=env):
                self.env.update(env)
                self.service()
                lead = self.prepared(2)
                self.save(lead, "s01", "slide one, saved\n")
                time.sleep(1.5)
                self.assertEqual([e for e in self.events() if e[0].startswith("ahead")], [])
                self.call(self.client_, self.got, "bye", ["--help"])
                self.client_.close()
                self.stop_service()
                self.env.update({"FAKE_APPLY_AHEAD": "1", "THEODORE_SERVICE_WORKERS": "2"})


class GatheringUnit(unittest.TestCase):
    """What a designer's apply spends in the gathering besides waiting for the others of its burst (lane S2b's S2-20ab,
    rig 10 round 1: 35 ms of each apply there, against about 15 warm)."""

    def gatherer(self, logged: list, log_s: float = 0.0):
        from types import SimpleNamespace

        from theodore_client.service import parts

        def log(line):
            if line.startswith("parts: "):
                time.sleep(log_s)
            logged.append((time.perf_counter(), line))
        pool = SimpleNamespace(on_release=[], parts_capability=lambda _v: True, idle_count=lambda _v: 2,
                               idle_home=lambda _k: None, group_left=lambda _k: set(), leave_group=lambda _k: None,
                               most=2, service=SimpleNamespace(log=log))
        gatherer = parts.Gatherer(pool)
        lead = "/decks/deck"
        gatherer.prepared(lead, at_once=True, count=3)
        sent: list = []

        def run_batch(_version, _argv, _env, batch, _home=None):
            sent.append((time.perf_counter(), [m.part for m in batch]))
            for member in batch:
                member.exit = 0
                member.done.set()
        gatherer._run_batch = run_batch                                  # noqa: SLF001
        return gatherer, os.path.realpath(lead), sent

    def burst(self, gatherer, lead: str, gap_s: float = 0.01) -> list:
        from types import SimpleNamespace
        members = []
        gatherer.on_way[lead] = 3                     # three designers' applies admitted (tools: parts.coming)
        threads = []
        for name in ("s01", "s02", "s03"):
            call = SimpleNamespace(cwd="/decks", cancelled=threading.Event(), timing={}, env={}, stdin=None,
                                   out=lambda *_a, **_k: None, count=lambda *_a, **_k: None)
            shape = ("9.9", lead, ("apply", "--json"), ())
            # on its way until the gathering takes it in (tools passes the lead it was counted under)
            thread = threading.Thread(target=gatherer.run, args=(call, "9.9", ["apply", f"{lead}/parts/{name}"], {},
                                                                (shape, f"{lead}/parts/{name}"), lead))
            thread.start()
            threads.append(thread)
            members.append(call)
            time.sleep(gap_s)
        for thread in threads:
            thread.join(10)
        return members

    def test_applies_that_waited_for_one_ahead_come_to_the_gathering_as_a_group(self):
        # Lane S2's S2-31 on rig 7 (2 cores): four designers' applies waited 56 to 83 ms for their build-ahead, each
        # counted off its way to the gathering meanwhile. When it ended, the first to wake found nobody coming and
        # ran alone on the ahead's worker; the other three found that worker busy and went to another, which drew
        # their pictures again (500 ms). The ahead now counts its waiting applies on their way again as it ends
        # (Gatherer.expect), and each hands that count to the gathering with its apply.
        from types import SimpleNamespace

        def come(gatherer, lead, names, counted):
            alone, threads = [], []
            gatherer._alone = lambda call, *_a, **_k: alone.append(call.name) or (0, [])     # noqa: SLF001
            for name in names:
                call = SimpleNamespace(cwd="/decks", cancelled=threading.Event(), timing={}, env={}, stdin=None, name=name,
                                       out=lambda *_a, **_k: None, count=lambda *_a, **_k: None)
                shape = ("9.9", lead, ("apply", "--json"), ())
                thread = threading.Thread(target=gatherer.run, args=(call, "9.9", ["apply", f"{lead}/parts/{name}"], {},
                                                                    (shape, f"{lead}/parts/{name}"), lead if counted else None))
                thread.start()
                threads.append(thread)
                time.sleep(0.005)
            for thread in threads:
                thread.join(10)
            return alone
        logged: list = []
        gatherer, lead, sent = self.gatherer(logged)
        gatherer.prepared(lead, at_once=True, count=8)                 # eight parts announced: these three are not all
        self.assertTrue(gatherer.expect(["apply", f"{lead}/parts/s05"], "/decks", 3))
        self.assertEqual(gatherer.on_way, {lead: 3})
        self.assertEqual(come(gatherer, lead, ("s05", "s06", "s07"), counted=True), [], "none ran alone")
        self.assertEqual(sorted(os.path.basename(name) for _t, names in sent for name in names), ["s05", "s06", "s07"])
        self.assertEqual(len({round(t, 2) for t, _names in sent}), 1, f"one cut for the three: {sent}")
        self.assertEqual(gatherer.on_way, {})
        # A deck whose parts do not come at once, or a count of none, is not counted.
        self.assertFalse(gatherer.expect(["apply", "/elsewhere/deck/parts/s01"], "/decks", 3))
        self.assertFalse(gatherer.expect(["apply", f"{lead}/parts/s05"], "/decks", 0))
        self.assertEqual(gatherer.on_way, {})

    def test_a_part_still_on_its_way_when_the_window_closes_is_waited_for(self):
        # Windows, 26 Nov (plugin 017e9b0): a burst of three went out as two parts, then one a second later. A part
        # was counted off its way (tools) before the gathering had it, and its thread was held between the two for
        # longer than the 15 ms window, which then closed on the two already waiting as though none were coming. The
        # gathering now counts a part off its way in the step that takes it in. Here each part comes four windows
        # after the one before: the window re-arms while parts are on their way, and all three go out together.
        from theodore_client.service import parts
        logged: list = []
        gatherer, lead, sent = self.gatherer(logged)
        self.burst(gatherer, lead, gap_s=4 * parts.GATHER_S)
        self.assertEqual(sorted(name for _t, names in sent for name in names),
                         [f"{lead}/parts/{name}" for name in ("s01", "s02", "s03")])
        self.assertEqual(len({round(t, 2) for t, _names in sent}), 1, f"one cut, when the third came: {sent}")
        self.assertTrue(any(line.startswith("parts: 3 of a deck") for _t, line in logged), logged)
        self.assertEqual(gatherer.on_way, {}, "none is counted on its way once all are in")
        # A part that never reaches the gathering (refused on its way) is counted off by `arrived`, as before.
        gatherer.on_way[lead] = 1
        gatherer.arrived(lead)
        self.assertEqual(gatherer.on_way, {})

    def test_a_burst_whose_every_part_is_in_goes_at_once(self):
        from unittest import mock

        from theodore_client.service import parts
        logged: list = []
        gatherer, lead, sent = self.gatherer(logged)
        with mock.patch.object(parts, "GATHER_S", 5.0):
            began = time.perf_counter()
            self.burst(gatherer, lead)
        self.assertTrue(sent, "the builds went out")
        self.assertLess(sent[0][0] - began, 1.0, "with every announced part in, no window")
        self.assertEqual(sorted(p for _t, names in sent for p in names),
                         sorted(f"{lead}/parts/{n}" for n in ("s01", "s02", "s03")), "the same parts, batched as before")
        self.assertEqual(len(sent), 2, "one build per free worker, as before")

    def test_its_log_line_is_written_after_its_builds_go_out(self):
        logged: list = []
        gatherer, lead, sent = self.gatherer(logged, log_s=0.4)
        calls = self.burst(gatherer, lead)
        line_at = next((t for t, line in logged if line.startswith("parts: 3 of a deck")), None)
        self.assertIsNotNone(line_at, logged)
        self.assertLessEqual(max(t for t, _names in sent), line_at, "the builds before the line")
        for call in calls:
            self.assertLess(call.timing.get("gathering_ms", 0), 300, "a slow log never counts in the gathering")


class AheadUnit(unittest.TestCase):
    def test_an_ahead_takes_the_most_threads_first_and_leaves_the_smallest_worker_idle(self):
        # Lane S2b's S2-18 on rig 7: a burst's two 4-part aheads went to the two 1-thread workers while the 3-thread
        # always-warm one sat idle. The aheads now take the most threads first and leave the smallest idle.
        from types import SimpleNamespace
        from unittest import mock

        from theodore_client.service import workers
        with mock.patch.object(workers.os, "cpu_count", return_value=4), \
                mock.patch.object(workers, "pool_sizes", return_value=(1, 4)):
            pool = workers.Pool(SimpleNamespace(log=lambda _m: None))

            def worker(threads):
                flags = {name: threading.Event() for name in ("ready", "warmed", "dead")}
                flags["ready"].set()
                flags["warmed"].set()
                return SimpleNamespace(build_threads=threads, render_threads=threads, busy=False, rank=None, keys=set(),
                                       calls=0, retiring=False, fatal=None, **flags)
            small, full, other = worker(1), worker(3), worker(1)
            pool.workers["9.9"] = [small, full, other]
            self.assertEqual(pool.ahead_slots("9.9"), 2)
            self.assertIs(pool.take_for_ahead("9.9"), full, "the always-warm worker's 3 threads first")
            self.assertIn(pool.take_for_ahead("9.9"), (small, other))
            self.assertIsNone(pool.take_for_ahead("9.9"), "a 1-thread worker stays idle for the designers' applies")

    def test_a_bursts_parts_are_shared_so_that_the_latest_workers_last_picture_comes_earliest(self):
        # Lane S2's S2-23, a second deck's round 1: shares by build threads alone gave the always-warm worker 9 of rig
        # 10's 15 parts (4 threads against 1, 1, 1) and 6 of rig 7's 8; its build ended at 1.33 s against 0.80 to 0.88,
        # and its pictures, drawn after its build, were 124 to 193 ms late for its designers. A part costs a worker
        # its serial share whatever the threads, its Node build over the build threads and its picture a round of
        # the render threads at a time (ahead.last_picture); each part goes where the last picture stays earliest.
        import itertools

        from theodore_client.service import ahead
        full10, full7, small = (4, 4), (3, 3), (1, 1)
        self.assertEqual(ahead.shares(15, [full10, small, small, small], 32, 8), [6, 3, 3, 3], "8 processors, one worker idle")
        self.assertEqual(ahead.shares(15, [full10, small, small, small], 32), [6, 3, 3, 3], "the same when not said")
        self.assertEqual(ahead.shares(8, [full7, small], 32, 8), [6, 2], "by the last picture, were there the processors")
        self.assertEqual(ahead.shares(8, [full7, small, small], 32), [4, 2, 2], "were three workers taken")
        # Four processors or fewer (lane S2's S2-28 on rig 7, 2 cores, one boot: 6 and 2 gave round 1 at 0.304 to
        # 0.387 s on a fresh store, 4 and 4 at 0.188 to 0.275 s): equal shares whatever the threads, since a worker's
        # threads share the few cores with the other worker's.
        self.assertEqual(ahead.shares(8, [full7, small], 32, 4), [4, 4], "4 processors: equal halves")
        self.assertEqual(ahead.shares(7, [full7, small], 32, 4), [4, 3], "the odd part to the worker taken first")
        self.assertEqual(ahead.shares(15, [(1, 1), small], 32, 2), [8, 7], "2 processors")
        self.assertEqual(ahead.shares(3, [full7, small], 32, 4), [2, 1])
        self.assertEqual(ahead.shares(80, [full7, small], 32, 4), [32, 32], "the cap holds there too")
        self.assertEqual(ahead.shares(8, [full7, small], 32, 5), [6, 2], "above four processors: by the last picture")
        self.assertEqual(ahead.EQUAL_SHARES_PROCESSORS, 4)
        self.assertEqual(ahead.shares(15, [small] * 4, 32), [4, 4, 4, 3], "equal workers: equal shares")
        self.assertEqual(ahead.shares(3, [(10, 10), small, small], 32), [1, 1, 1], "each worker taken gets a part")
        self.assertEqual(ahead.shares(2, [full10, small, small], 32), [1, 1, 0], "fewer parts than workers")
        self.assertEqual(ahead.shares(40, [small, small], 32), [20, 20])
        self.assertEqual(ahead.shares(80, [small, small], 32), [32, 32], "the cap: the rest wait for the next ahead")
        self.assertEqual(ahead.shares(0, [full10, small], 32), [0, 0])
        self.assertEqual(ahead.shares(5, [], 32), [])
        # More threads never cost a worker parts, and no worker's designers wait for another's while a part could move.
        for parts in range(1, 41):
            for pool in ([full10, small, small, small], [full7, small], [full10, (2, 2), small], [small, small, small]):
                sizes = ahead.shares(parts, pool, 32)
                self.assertEqual(sum(sizes), parts, (parts, pool, sizes))
                self.assertEqual(sizes, sorted(sizes, reverse=True), f"the most threads, the most parts: {parts} {sizes}")
                if parts >= len(pool):
                    self.assertGreaterEqual(min(sizes), 1, (parts, pool, sizes))
        # The latest last picture is the earliest any split gives (every split tried, each worker with a part).
        for pool in ([full10, small, small, small], [full7, small], [full7, small, small], [full10, (2, 2), small]):
            for parts in range(len(pool), 17):
                latest = lambda sizes: max(ahead.last_picture(n, *t) for n, t in zip(sizes, pool) if n)   # noqa: E731
                best = min(latest(split) for split in itertools.product(range(1, parts + 1), repeat=len(pool))
                           if sum(split) == parts)
                self.assertEqual(latest(ahead.shares(parts, pool, 32)), best, (parts, pool))
        # What a part costs: the serial share is not divided by threads, the build and the picture are.
        self.assertLess(ahead.last_picture(6, 4, 4), ahead.last_picture(9, 4, 4))
        self.assertLess(ahead.last_picture(4, 4, 4), ahead.last_picture(4, 1, 1))
        self.assertGreater(ahead.last_picture(4, 4, 4), ahead.last_picture(4, 1, 1) / 4, "threads do not divide a part's whole cost")
        self.assertEqual(ahead.last_picture(5, 4, 4) - ahead.last_picture(4, 4, 4),
                         ahead.PART_SERIAL + ahead.PART_BUILD / 4 + ahead.PICTURE_ROUND, "a fifth picture is a second round")

    def test_a_home_group_goes_out_as_soon_as_all_of_it_is_in(self):
        # Lane S2's S2-9: the parts an ahead built on one worker waited 32 to 118 ms for the window while the rest of the
        # burst came. Once every part of the group not yet applied is waiting and its worker is idle, it goes at once.
        from types import SimpleNamespace
        from unittest import mock

        from theodore_client.service import parts, workers
        with mock.patch.object(workers, "pool_sizes", return_value=(1, 4)):
            pool = workers.Pool(SimpleNamespace(log=lambda _m: None))

        def idle_worker():
            ready = threading.Event()
            ready.set()
            return SimpleNamespace(busy=False, dead=threading.Event(), retiring=False, ready=ready, warmed=ready, fatal=None)
        home, other = idle_worker(), idle_worker()
        pool.workers["9.9"] = [home, other]
        pool.remember_group(["k1", "k2", "k3"], home)
        gatherer = parts.Gatherer(pool)
        lead = parts._Lead()                                                        # noqa: SLF001
        member = lambda key: parts._Member(SimpleNamespace(), f"/d/parts/{key}", key)   # noqa: E731, SLF001
        lead.pending = [member("k1"), member("k2"), member("x9")]
        self.assertEqual(gatherer._home_groups("9.9", lead), [], "k3 not in yet: the group waits")     # noqa: SLF001
        pool.leave_group("k3")                                                      # k3 applied already, alone
        found = gatherer._home_groups("9.9", lead)                                  # noqa: SLF001
        self.assertEqual([(sorted(m.key for m in members), worker) for members, worker in found], [(["k1", "k2"], home)])
        self.assertEqual([m.key for m in lead.pending], ["x9"], "the rest waits for the window")

    def test_a_2_0_parts_key_is_derived_from_its_leads_marker(self):
        # Lane S2's S2-8: a 2.0 part has no marker of its own (lanes/G.md: ws2.part_key derives it), so the service
        # knew no key for it and the routing to the worker that built its ahead never engaged.
        import hashlib
        import tempfile

        from theodore_client.service import workers
        with tempfile.TemporaryDirectory() as tmp:
            lead = Path(tmp) / "deck"
            (lead / "parts" / "s07").mkdir(parents=True)
            (lead / ".theodore.json").write_text(json.dumps({"key": "00112233aabbccdd"}))
            want = hashlib.sha256(b"00112233aabbccdd/s07").hexdigest()[:16]
            self.assertEqual(workers.workspace_key(["apply", str(lead / "parts" / "s07")], None), want)
            self.assertEqual(workers.workspace_key(["apply", str(lead)], None), "00112233aabbccdd")
            (Path(tmp) / "loose" / "parts" / "s01").mkdir(parents=True)
            self.assertIsNone(workers.workspace_key(["apply", str(Path(tmp) / "loose" / "parts" / "s01")], None),
                              "no lead marker: no key")

    def test_a_burst_saved_whole_goes_out_at_the_first_poll_that_sees_it(self):
        # Lane S2's S2-7: all fifteen deck.js land within milliseconds, and the still poll cost each ahead ~100 ms of its
        # head start. When every part of a burst is saved anew, its aheads go at once; one part alone still waits.
        import tempfile
        from types import SimpleNamespace

        from theodore_client.service import ahead, workers
        sent = []
        keys = workers._KeyLock()                                                   # noqa: SLF001
        pool = SimpleNamespace(on_starved=[], has_feature=lambda _v, _n: True, ahead_slots=lambda _v: 1, keys=keys,
                               take_for_ahead=lambda _v: SimpleNamespace(), remember_group=lambda _keys, _worker: None,
                               run_ahead=lambda call, worker, argv, env: sent.append(argv) or 0)
        service = SimpleNamespace(log=lambda _m: None, stopping=threading.Event(), relays_attached=lambda: 1)
        builder = ahead.Ahead(service, pool)
        with tempfile.TemporaryDirectory() as tmp:
            burst = ahead._Burst(str(Path(tmp) / "deck"))
            parts = []
            for name in ("s01", "s02", "s03"):
                folder = Path(tmp) / "deck" / "parts" / name
                folder.mkdir(parents=True)
                (folder / "deck.js").write_text("starter\n")
                part = ahead._Part(os.path.realpath(folder), "9.9", {}, tmp, burst)
                part.key = ""
                builder.parts[part.path] = part
                parts.append(part)
            parts[0].deck.write_text("saved, alone\n")
            for part in parts:
                builder._poll(part)                                                 # noqa: SLF001
            builder._dispatch()                                                     # noqa: SLF001
            time.sleep(0.2)
            self.assertEqual(sent, [], "one part saved: it waits for its still poll")
            for part in parts[1:]:
                part.deck.write_text(f"saved, {os.path.basename(part.path)}, with the others\n")
            for part in parts:
                builder._poll(part)                                                 # noqa: SLF001
            builder._dispatch()                                                     # noqa: SLF001
            deadline = time.monotonic() + 15
            while not sent and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(sent), 1, "the whole burst saved: one ahead at once, no still poll")
            self.assertEqual(sorted(os.path.basename(a) for a in sent[0][1:-1]), ["s01", "s02", "s03"])

    def test_an_apply_waiting_for_its_own_ahead_stops_being_on_its_way_before_it_waits(self):
        # Lane S2's rig 7: every apply of 8 reused its ahead, yet each waited ~160 ms in the gathering for the parts
        # still waiting on theirs (counted on their way, they held the cut up to GATHER_CAP_S).
        import tempfile
        from types import SimpleNamespace

        from theodore_client.service import ahead
        service = SimpleNamespace(log=lambda _m: None, stopping=threading.Event(), relays_attached=lambda: 1)
        builder = ahead.Ahead(service, SimpleNamespace(on_starved=[], has_feature=lambda _v, _n: True))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "deck" / "parts" / "s01"
            folder.mkdir(parents=True)
            (folder / "deck.js").write_text("slide one\n")
            part = ahead._Part(os.path.realpath(folder), "9.9", {}, tmp, ahead._Burst(str(Path(tmp) / "deck")))
            builder.parts[part.path] = part
            running = ahead._Call([part])
            part.ahead = running
            told = []
            waiter = threading.Thread(target=builder.before_apply, args=(["apply", str(folder)], tmp),
                                      kwargs={"on_wait": lambda: told.append(running.done.is_set())})
            waiter.start()
            time.sleep(0.3)
            self.assertEqual(told, [False], "told before it waits, while its ahead still runs")
            self.assertTrue(waiter.is_alive(), "and it waits for its own ahead")
            running.done.set()
            waiter.join(5)
            self.assertFalse(waiter.is_alive())


class HotPath(ServiceCase):
    """The short command's hot path (hot.py; the coordinator, 26 Nov: on Windows the launcher runs this client for every
    command): a command for a running service loads only what reaching it needs, and anything else goes to the full
    client exactly as before."""

    HEAVY = ("theodore_client.cli", "theodore_client.core", "theodore_client.account", "theodore_client.api",
             "theodore_client.service.shell", "theodore_client.service.endpoint", "pathlib", "subprocess", "hashlib",
             "ssl", "urllib", "email", "http", "re", "fnmatch", "json", "threading", "ctypes", "struct", "socket",
             "site", "runpy", "__future__")
    OWN = {"theodore_client", "theodore_client.hot"}        # every module the hot path reads from a file of Python source

    def run_client(self, *args: str, env: dict | None = None, stdin: str | None = None):
        return subprocess.run([sys.executable, "-I", "-S", "-X", "importtime", str(CLIENT_DIR / "__main__.py"), *args],
                              env={**self.env, **(env or {})}, cwd=str(self.box.tmp), input=stdin, capture_output=True,
                              text=True, timeout=60)

    @staticmethod
    def imported(result) -> set:
        return {line.split("|")[-1].strip() for line in result.stderr.splitlines() if line.startswith("import time:")}

    @staticmethod
    def from_source(names) -> set:
        """Those of `names` this Python reads from a .py file: not built in, frozen or an extension module."""
        import importlib.util
        found = set()
        for name in names:
            try:
                spec = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                spec = None
            if spec is not None and (spec.origin or "").endswith(".py"):
                found.add(name)
        return found

    def test_importing_it_loads_nothing_heavy(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import theodore_client.hot; "
                "print('\\n'.join(sorted(sys.modules)))")
        loaded = set(subprocess.run([sys.executable, "-I", "-S", "-c", code, str(CLIENT_DIR)], capture_output=True, text=True,
                                    timeout=60).stdout.split())
        self.assertIn("theodore_client.hot", loaded)
        self.assertEqual(sorted(name for name in loaded if name.split(".")[0] in {h.split(".")[0] for h in self.HEAVY}
                                and any(name == h or name.startswith(h + ".") for h in self.HEAVY)), [])

    def test_a_command_for_the_running_service_goes_the_hot_path(self):
        self.service()
        first = self.run_client("pptx", "echo", "first")     # the client records the folder's service (state/environment)
        self.assertIn("theodore_client.cli", self.imported(first), "a folder with no record yet: the client's path")
        result = self.run_client("pptx", "echo", "hot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-2000:])
        answer = json.loads(result.stdout.splitlines()[0])
        self.assertEqual((answer["argv"], answer["resident"]), (["echo", "hot"], "1"), "run in the service's worker")
        self.assertIn("a line on standard error", result.stderr)
        loaded = self.imported(result)
        self.assertIn("theodore_client.hot", loaded)
        self.assertFalse(loaded & set(self.HEAVY), sorted(loaded & set(self.HEAVY)))
        # It stays light: on a Windows test machine each module read from a file is about 1.5 ms of a short command.
        # Besides Python's own first modules and `os`, a call reads two: the package and hot.py. One more is a
        # decision, never an accident: put what it needs into hot.py, or load it only where a call needs it.
        bare = subprocess.run([sys.executable, "-I", "-S", "-X", "importtime", "-c", "import os, sys, time"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(self.from_source(loaded - self.imported(bare)), self.OWN)
        skill = self.run_client("skill", "pptx")
        self.assertEqual(skill.returncode, 0, "a skill's injected command exits 0, as through the client")

    def test_the_full_client_runs_what_the_hot_path_does_not(self):
        self.service()
        self.run_client("pptx", "echo", "first")
        cases = {"the shell route off": ({"THEODORE_SERVICE": "0"}, None, ["pptx", "echo", "x"]),
                 "standard input to send": ({}, "piped text", ["pptx", "echo", "-"]),
                 "not a service command": ({}, None, ["version"])}
        for name, (env, stdin, args) in cases.items():
            with self.subTest(name):
                result = self.run_client(*args, env=env, stdin=stdin)
                self.assertIn("theodore_client.cli", self.imported(result), name)
        (self.box.home / "state" / "environment").write_text("https://another.example\n")
        result = self.run_client("pptx", "echo", "x")
        self.assertIn("theodore_client.cli", self.imported(result), "another service's folder: the client says so")
        self.assertIn("belongs to the Theodore service at https://another.example", result.stdout)

    def test_the_full_client_has_the_runtimes_packages_as_in_a_start_without_S(self):
        # The launchers start the client with -S (no site: the hot path needs none of it). What the hot path does not
        # answer runs in the full client, which loads site first: sys.path is what a start without -S has.
        code = ("import json, runpy, sys\nmain = sys.argv[1]\nsys.argv = [main, 'help']\n"
                "try:\n    runpy.run_path(main, run_name='__main__')\nexcept SystemExit:\n    pass\n"
                "print('SYS.PATH ' + json.dumps(sys.path))")
        paths = {}
        for flags in ("-I -S", "-I"):
            result = subprocess.run([sys.executable, *flags.split(), "-c", code, str(CLIENT_DIR / "__main__.py")], env=self.env,
                                    cwd=str(self.box.tmp), capture_output=True, text=True, timeout=60)
            line = next((line for line in result.stdout.splitlines() if line.startswith("SYS.PATH ")), None)
            self.assertTrue(line, result.stdout[-600:] + result.stderr[-600:])
            paths[flags] = json.loads(line.split(" ", 1)[1])
        self.assertEqual(paths["-I -S"], paths["-I"])
        bare = subprocess.run([sys.executable, "-I", "-S", "-c", "import json, sys; print(json.dumps(sys.path))"],
                              capture_output=True, text=True, timeout=60)
        self.assertLess(len(json.loads(bare.stdout)), len(paths["-I -S"]), "this Python has packages for site to add")
        self.assertIn("site", self.imported(self.run_client("help")), "a command of the full client: site is loaded for it")

    @unittest.skipIf(os.name == "nt", "an interrupt signal sent to one process: macOS and Linux")
    def test_ctrl_c_cancels_the_call_and_waits_for_its_end(self):
        import signal
        self.service()
        self.run_client("pptx", "echo", "first")
        proc = subprocess.Popen([sys.executable, "-I", "-S", str(CLIENT_DIR / "__main__.py"), "pptx", "sleep", "30"], env=self.env,
                                cwd=str(self.box.tmp), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.default_int_handler))
        self.wait_log(r"sleep", 15)
        time.sleep(1.0)
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, out + err)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(json.loads(out.splitlines()[-1]), {"slept": "30", "cancelled": True}, "the engine was told, and its end waited for")
        self.assertNotIn("Traceback", err)

    def test_a_read_that_ctrl_c_interrupts_on_windows_goes_on_where_it_was(self):
        # The pipe's read waits in _winapi, which Ctrl-C ends with EINTR and no KeyboardInterrupt of its own: that comes
        # in the sleep that follows. The read in progress and the bytes that came stay with the stream, so the frames
        # after the cancel are read whole.
        from types import SimpleNamespace
        from unittest import mock

        from theodore_client import hot
        body = hot.dumps({"t": "done", "id": "c", "exit": 130}).encode("utf-8")
        data = len(body).to_bytes(4, "little") + body

        class Overlapped:
            event = 7

            def __init__(self, came: bytes):
                self.came = came

            def GetOverlappedResult(self, wait):
                return len(self.came), 0

            def getbuffer(self):
                return self.came

        class Winapi:
            reads = [data[:3], data[3:9], data[9:]]         # a frame in three pieces
            waits = [0, InterruptedError(), 0x102, 0, 0]    # the second read is interrupted, then waits once more
            started = 0

            def ReadFile(self, handle, size, overlapped):
                Winapi.started += 1
                return Overlapped(Winapi.reads.pop(0)), 997

            def WaitForMultipleObjects(self, handles, wait_all, milliseconds):
                step = Winapi.waits.pop(0)
                if isinstance(step, Exception):
                    raise step
                return step

        def interrupted(_seconds):
            raise KeyboardInterrupt

        stream = hot._Stream.__new__(hot._Stream)       # noqa: SLF001
        stream._sock, stream._handle, stream._ov, stream._buffer, stream._winapi = None, 1, None, bytearray(), Winapi()
        with mock.patch.object(hot, "time", SimpleNamespace(sleep=interrupted, monotonic=time.monotonic)):
            with self.assertRaises(KeyboardInterrupt):
                stream.read()
        self.assertEqual((Winapi.started, bytes(stream._buffer)), (2, data[:3]), "one piece came; the second read is in progress")
        self.assertIsNotNone(stream._ov)
        self.assertEqual(stream.read(), {"t": "done", "id": "c", "exit": 130})
        self.assertEqual((Winapi.started, Winapi.reads, bytes(stream._buffer)), (3, [], b""))

    def test_with_no_service_it_hands_over_to_the_client_which_starts_one(self):
        result = self.run_client("pptx", "echo", "cold")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-2000:])
        self.assertEqual(json.loads(result.stdout.splitlines()[0])["argv"], ["echo", "cold"])
        self.assertIn("theodore_client.cli", self.imported(result))
        self.record = endpoint.read_service_json(self.box.home)

    def test_its_rules_are_the_clients(self):
        from theodore_client import cli, hot
        from theodore_client.service import context, shell
        self.assertEqual(hot.LAUNCHER_TAIL, cli.LAUNCHER_TAIL)
        self.assertEqual(hot.FRONT_COMMANDS, shell.FRONT_COMMANDS)
        self.assertEqual(hot.FIXED, context.FIXED)
        for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_PLUGIN_ROOT", "CODEX_THREAD_ID",
                     "THEODORE_HOME", "THEODORE_X", "THEODORE_", "THEODORE_lower", "THEODORE_A-B", "PATH", "HOME",
                     "CLAUDE_CODE_ENTRYPOINT", "THEODORE_RESIDENT", "THEODORE_TIMINGS", "XTHEODORE_A"):
            with self.subTest(name):
                self.assertEqual(hot.passed(name), bool(context.ALLOWED.match(name)) and name not in context.FIXED)
        from unittest import mock
        for env, host in (({"THEODORE_HOST": "My Host!"}, "myhost"), ({"CLAUDE_CODE_REMOTE": "1"}, "claude-web"),
                          ({"CLAUDECODE": "1"}, "claude-code"), ({"CODEX_HOME": "/x"}, "codex"),
                          ({"CLAUDE_PLUGIN_ROOT": "/p"}, "claude-code"), ({}, "other")):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(hot.host_name(), host)


class Lifecycle(ServiceCase):
    def test_nothing_runs_after_the_last_relay_detaches(self):
        exits = self.box.tmp / "exit-frames.jsonl"
        self.env["FAKE_EXITS"] = str(exits)
        record = self.service()
        client, got = self.connect()
        worker_pid = json.loads(Collector.stdout(self.call(client, got, "e", ["echo"])))["pid"]
        self.assertTrue(alive(worker_pid))
        self.assertIn(worker_pid, children_of(record["pid"]))
        client.close()
        self.assertTrue(wait_gone(record["pid"], 30), "the service exits after its linger")
        self.assertTrue(wait_gone(worker_pid, 15), "its worker ends with it")
        self.assertFalse((self.box.home / "state" / "run" / "service.json").exists())
        frames_ = [json.loads(line) for line in exits.read_text().splitlines()]
        self.assertTrue(frames_, "the worker was told to exit")
        for frame in frames_:       # the service's end: the worker stops the office the workers share (C2)
            self.assertEqual(frame, {"t": "exit", "grace_ms": 2000, "offices": "stop"})

    def test_after_a_relay_detaches_earlier_shell_commands_keep_no_idle_window(self):
        # K's A4 re-run (25 Nov): shell commands with no relay attached, then a relay's session: the service stayed
        # 10 minutes after the shell commands. A relay's session now holds the service; after it, the linger alone.
        self.env["THEODORE_SERVICE_IDLE_S"] = "60"
        record = self.service()
        front, got_front = self.connect(role="front", session="sh")
        front.send_call({"t": "call", "id": "f", "tool": "pptx", "cwd": "/", "args": ["echo"]}, start=False)
        got_front.wait("f", 60)
        front.close()
        relay, _got = self.connect()
        time.sleep(0.3)
        relay.close()
        self.assertTrue(wait_gone(record["pid"], 30), "gone after the relay's linger, not 60 s after the shell command")

    def test_a_sessions_hung_call_does_not_keep_the_service_after_the_session_ends(self):
        # K's A3b (rig 1): fifteen hung calls kept the service and its workers up past the 3-minute count.
        self.env["THEODORE_ORPHAN_GRACE_S"] = "1"
        record = self.service()
        client, got = self.connect()
        self.call(client, got, "hung", ["echo", "--freeze"], wait=False)       # stuck: it answers nothing, not a cancel
        time.sleep(1.0)
        client.close()                                                        # the session ends
        self.assertTrue(wait_gone(record["pid"], 30), "the call stopped after the grace, then the linger: gone")
        log = (self.box.home / "logs" / "service.log").read_text()
        self.assertIn("session ended with 1 call(s) running: cancelled", log)
        self.assertIn("still running after its session ended: its worker stopped", log)

    def test_a_removed_home_ends_the_service_and_stays_removed(self):
        record = self.service()
        client, got = self.connect()
        worker_pid = json.loads(Collector.stdout(self.call(client, got, "e", ["echo"])))["pid"]
        shutil.rmtree(self.box.home)            # removed under a running service (its sessions still open)
        self.assertTrue(wait_gone(record["pid"], 30), "the service ends once its home is gone")
        self.assertTrue(wait_gone(worker_pid, 15), "its worker ends with it")
        time.sleep(1.5)                          # a timer's second, and the service's last log line
        self.assertFalse(self.box.home.exists(), "nothing the service wrote brought its home back")

    def test_theodore_stop_and_the_shell_route(self):
        record = self.service()
        env = dict(self.env)
        result = subprocess.run([sys.executable, "-I", str(CLIENT_DIR), "pptx", "echo", "from-shell"], env=env, cwd=str(self.box.tmp),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        answer = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(answer["argv"], ["echo", "from-shell"])
        self.assertEqual(answer["resident"], "1", "the shell command ran in the service's warm worker")
        self.assertIn("a line on standard error", result.stderr)
        stopped = subprocess.run([sys.executable, "-I", str(CLIENT_DIR), "stop"], env=env, capture_output=True, text=True, timeout=60)
        self.assertTrue(stopped.stdout.startswith("THEODORE OK: "), stopped.stdout)
        self.assertTrue(wait_gone(record["pid"], 30))
        self.assertTrue(wait_gone(answer["pid"], 15))

    def test_a_client_update_during_a_call_loses_nothing(self):
        record = self.service()
        client, got = self.connect()
        self.call(client, got, "inflight", ["sleep", "2"], wait=False)
        time.sleep(0.3)
        # a newer client installed beside it: the home names it, with a runtime to start it on
        new = self.box.home / "client" / "9.9.9"
        shutil.copytree(CLIENT_DIR, new, ignore=shutil.ignore_patterns("__pycache__"))
        init = new / "theodore_client" / "__init__.py"
        init.write_text(init.read_text().replace(f'"{__import__("theodore_client").__version__}"', '"9.9.9"'))
        python = self.box.home / "runtime" / "rt-test" / "python" / "bin" / "python3"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.symlink_to(sys.executable)
        (self.box.home / "client" / "current.txt").write_text("9.9.9")
        done = Collector.done(got.wait("inflight", 30))
        self.assertEqual(done["exit"], 0, "the call in flight finished on the old service")
        deadline = time.monotonic() + 20
        successor = None
        while time.monotonic() < deadline:
            successor = endpoint.read_service_json(self.box.home)
            if successor and successor["pid"] != record["pid"] and alive(successor["pid"]):
                break
            time.sleep(0.1)
        self.assertIsNotNone(successor)
        self.assertEqual(successor["version"], "9.9.9")
        after = self.call(client, got, "after", ["echo"])
        answer = json.loads(Collector.stdout(after).splitlines()[0])
        self.assertEqual(answer.get("argv"), ["echo"], f"a new call reached the successor: {after}")
        self.assertTrue(wait_gone(record["pid"], 30), "the old service exits")
        self.record = successor


class ChatWarm(ServiceCase):
    def test_a_chats_command_runs_on_a_warm_worker_confined_to_its_folder(self):
        from theodore_client import chatbuild
        self.api.features = [*self.api.features, chatbuild.CHAT_BUILD_FEATURE]
        self.sign_in()                          # again: the licence now carries the chat's feature
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        self.env["HOME"] = str(self.box.tmp)
        self.service()
        client, got = self.connect(session="chat-1")
        answers = []
        for n in range(2):
            client.send_call({"t": "call", "id": f"r{n}", "tool": "chat", "cwd": str(self.box.tmp),
                              "params": {"action": "run", "folder": str(folder), "args": ["list", str(n)], "_app_client": True}},
                             start=False)
            result = json.loads(Collector.stdout(got.wait(f"r{n}", 60)))
            self.assertFalse(result.get("isError"), result)
            answers.append(json.loads(result["content"][0]["text"].split("\n", 1)[1].splitlines()[0]))
        first, second = answers
        self.assertEqual((first["resident"], first["argv"]), ("1", ["list", "0"]), "a warm worker ran it")
        self.assertEqual(second["pid"], first["pid"], "the same warm worker for the same folder")
        self.assertEqual(Path(first["home"]), self.box.home / "state" / "chat", "its private home, as a chat's process had")
        self.assertIn("--permission", first["node_options"])
        self.assertIn(str(folder.resolve()), first["node_options"])
        self.assertTrue((self.box.home / "state" / "chat" / "state" / "entitlement.json").exists(), "the copy lives with the worker")
        client.close()
        record = endpoint.read_service_json(self.box.home)
        if record:
            self.assertTrue(wait_gone(record["pid"], 30))
        self.assertTrue(wait_gone(first["pid"], 30))
        self.assertFalse((self.box.home / "state" / "chat" / "state" / "entitlement.json").exists(), "and goes with it")


class CallersProxy(ServiceCase):
    """One home has one service, and it starts with the whole environment of whichever command came first. A proxy
    named there is that caller's alone: Claude Code's sandbox gives each session its own, which ends with the session,
    and a terminal whose profile exports one sits beside an app that has none. So each call's network work uses the
    calling client's proxy settings, sent in the call's env as THEODORE_CALLER_PROXY (a name C1's allow-list takes as
    it stands) and kept from every engine: never the starter's, never another caller's."""

    NAMES = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
    BODY = "ENTRY BODY THROUGH THE CALLER'S OWN NETWORK"

    def setUp(self):
        super().setUp()
        self.api.guides["deck-design"][""] = f"# Deck design\n\n{self.BODY}\n"
        self.env.update({name: "" for name in self.NAMES})

    def guide(self, **proxy):
        """`theodore guide deck-design` from a client whose environment names this proxy (none: no proxy at all)."""
        return subprocess.run([sys.executable, "-I", str(CLIENT_DIR), "guide", "deck-design"], env={**self.env, **proxy},
                              cwd=str(self.box.tmp), capture_output=True, text=True, timeout=90)

    def test_a_caller_with_no_proxy_is_not_sent_through_the_starters_dead_one(self):
        gone = socket.socket()
        gone.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{gone.getsockname()[1]}"
        gone.close()                                    # the starter's proxy: it ended with its session
        self.record = start_service({**self.env, "HTTP_PROXY": dead, "http_proxy": dead}, self.box.home)
        for front in ("the client's own front (the folder's first command)", "the hot path"):
            with self.subTest(front):
                result = self.guide()
                self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
                self.assertIn(self.BODY, result.stdout)
        self.assertTrue(alive(self.record["pid"]))

    def test_a_callers_own_proxy_carries_its_call_and_no_other_callers(self):
        from test_client import _start_proxy
        proxy, url, seen = _start_proxy()
        self.addCleanup(proxy.shutdown)
        own = url.replace("http://", "http://someone:hunter2-not-for-any-log@")
        self.service()                                  # started by a command with no proxy
        counts = []
        for front in ("the client's own front (the folder's first command)", "the hot path"):
            with self.subTest(front):
                result = self.guide(HTTP_PROXY=own, http_proxy=own)
                self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
                self.assertIn(self.BODY, result.stdout)
                self.assertTrue(any(self.api.base in line for line in seen[sum(counts):]),
                                f"the service's requests for this call went past the caller's proxy: {seen}")
                counts.append(len(seen) - sum(counts))
                result = self.guide()                   # another caller, with no proxy: not through the first one's
                self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
                self.assertEqual(len(seen), sum(counts), "a caller with no proxy was sent through another caller's")
        # The settings are the service's own for the call: the engine's environment for it holds neither them nor
        # the variable that carried them, and its answer names no credential.
        engine = subprocess.run([sys.executable, "-I", str(CLIENT_DIR), "pptx", "echo", "x"],
                                env={**self.env, "HTTP_PROXY": own, "http_proxy": own}, cwd=str(self.box.tmp),
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(engine.returncode, 0, engine.stdout[-1500:] + engine.stderr[-1500:])
        given = json.loads(engine.stdout.splitlines()[0])["env"]
        self.assertIn("THEODORE_HOST", given)
        self.assertEqual([name for name in given if "PROXY" in name.upper()], [])
        self.assertNotIn("hunter2-not-for-any-log", engine.stdout + engine.stderr)
        logs = "".join(path.read_text(errors="replace") for path in (self.box.home / "logs").rglob("*") if path.is_file())
        self.assertTrue(logs)
        self.assertNotIn("hunter2-not-for-any-log", logs, "a proxy's credentials are in no log")

    def test_the_settings_a_call_brings_are_a_layer_for_its_thread_alone(self):
        from theodore_client import hot
        from theodore_client.service import context
        self.assertEqual((hot.CALLER_PROXY, hot.PROXY_NAMES), (context.CALLER_PROXY, context.PROXY_NAMES))
        self.assertTrue(context.ALLOWED.match(hot.CALLER_PROXY), "C1's allow-list takes the name as it stands")
        from unittest import mock
        with mock.patch.dict(os.environ, {**{name: "" for name in self.NAMES}, "HTTPS_PROXY": "http://u:secret-word@p:8"}):
            sent = hot.proxy_settings()
        self.assertEqual(json.loads(sent), {"HTTPS_PROXY": "http://u:secret-word@p:8"})
        with mock.patch.dict(os.environ, {name: "" for name in self.NAMES}):
            self.assertEqual(hot.proxy_settings(), "{}", "a caller with no proxy says so: two characters")
        with mock.patch.object(context, "_OUTSIDE", {}):
            layer = context.proxy_layer(sent)
            self.assertEqual(layer, {**{name: None for name in self.NAMES}, "HTTPS_PROXY": "http://u:secret-word@p:8"})
            self.assertEqual(context._OUTSIDE, layer, "work outside any call goes by the last caller's")     # noqa: SLF001
            from theodore_client import core
            self.assertEqual(core.scrub_secrets("via http://u:secret-word@p:8"), "via http://[redacted]@p:8")
            base = {"HTTPS_PROXY": "http://the-starters:1", "http_proxy": "http://the-starters:2", "PATH": "/bin"}
            environ = context._Environ(base)                                                                  # noqa: SLF001
            ctx = context.CallContext(env={"THEODORE_STDIO": "shared"}, proxy=layer)
            self.assertEqual(ctx.env, {"THEODORE_STDIO": "shared"}, "what an engine is given holds no proxy setting")
            with context.entered(ctx):
                self.assertEqual((environ.get("HTTPS_PROXY"), environ.get("http_proxy"), environ["PATH"]),
                                 ("http://u:secret-word@p:8", None, "/bin"))
                self.assertNotIn("http_proxy", dict(environ))
            none = context.CallContext(env={}, proxy=context.proxy_layer("{}"))
            with context.entered(none):
                self.assertEqual([name for name in dict(environ) if name.lower().endswith("_proxy")], [],
                                 "a caller with no proxy sees none of the service's")
            for unreadable in (None, "", "not json", "[1]", 7):
                self.assertEqual(context.proxy_layer(unreadable), {}, repr(unreadable))
            self.assertEqual(context._OUTSIDE, {name: None for name in self.NAMES})                           # noqa: SLF001


class ShellTexts(ServiceCase):
    def test_skill_and_guide_through_the_service(self):
        self.api.guides["deck-design"][""] = "# Deck design\n\nENTRY BODY FOR THE SHELL\n"
        record = self.service()
        for args in (["skill", "deck-design"], ["guide", "deck-design"]):
            result = subprocess.run([sys.executable, "-I", str(CLIENT_DIR), *args], env=self.env, cwd=str(self.box.tmp),
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ENTRY BODY FOR THE SHELL", result.stdout)
        client_log = self.box.home / "logs" / "client.log"
        self.assertNotIn("front: no Theodore service", client_log.read_text() if client_log.exists() else "")
        self.assertTrue(alive(record["pid"]))

    def test_fifteen_first_guides_at_once_start_one_service_and_all_answer(self):
        """ISSUES row 37 on the shell route through the service: fifteen designers' first `theodore guide` at the same
        moment, with no service running. One service starts (the start lock), and every front gets the same text."""
        self.api.guides["deck-design"][""] = "# Deck design\n\nENTRY BODY FOR A BURST\n"
        starts = 15
        barrier, results = threading.Barrier(starts), [None] * starts

        def one(i: int) -> None:
            barrier.wait()
            results[i] = subprocess.run([sys.executable, "-I", str(CLIENT_DIR), "guide", "deck-design"], env=self.env,
                                        cwd=str(self.box.tmp), capture_output=True, text=True, timeout=120,
                                        stdin=subprocess.DEVNULL)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(starts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(150)
        failed = [(r.returncode, r.stdout[:200], r.stderr[-300:]) for r in results if r is None or r.returncode != 0]
        self.assertEqual(failed, [], f"{len(failed)} of {starts} failed")
        self.assertEqual({r.stdout for r in results}, {results[0].stdout})
        self.assertIn("ENTRY BODY FOR A BURST", results[0].stdout)
        services = [pid for pid, line in sandbox_processes(self.box.tmp.name) if " service --home " in line]
        self.assertEqual(len(services), 1, "one service for the home")
        client_log = self.box.home / "logs" / "client.log"
        self.assertNotIn("front: no Theodore service", client_log.read_text() if client_log.exists() else "",
                         "every front reached the service")


class Relay(ServiceCase):
    """`theodore mcp` against the real service: the plugin's tools and calls answered out of order."""

    def test_pptx_tool_through_the_relay(self):
        self.env["THEODORE_SERVICE_TIMINGS"] = "1"      # the relay's own line a tools/call too (lane S2)
        proc = subprocess.Popen([sys.executable, "-I", str(CLIENT_DIR), "mcp"], env=self.env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(self.box.tmp))
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        answers: dict = {}
        lock = threading.Condition()

        def read():
            for line in proc.stdout:
                message = json.loads(line)
                with lock:
                    answers[message.get("id", f"n{len(answers)}")] = message
                    lock.notify_all()
        threading.Thread(target=read, daemon=True).start()

        def send(ident, method, params=None):
            message = {"jsonrpc": "2.0", "method": method, **({"id": ident} if ident is not None else {}),
                       **({"params": params} if params is not None else {})}
            proc.stdin.write(json.dumps(message).encode() + b"\n")
            proc.stdin.flush()

        def wait(ident, timeout=60):
            with lock:
                lock.wait_for(lambda: ident in answers, timeout)
                return answers[ident]
        send(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "2"}})
        self.assertEqual(wait(1)["result"]["serverInfo"]["name"], "theodore")
        send(2, "tools/list")
        names = sorted(t["name"] for t in wait(2)["result"]["tools"])
        self.assertEqual(names, ["guide", "list", "pptx", "specimen", "theodore"])
        send(3, "tools/call", {"name": "pptx", "arguments": {"args": ["sleep", "1.5"]}, "_meta": {"progressToken": "tok"}})
        send(4, "tools/call", {"name": "pptx", "arguments": {"args": ["echo", "relay"]}})
        quick = wait(4)
        with lock:
            self.assertNotIn(3, answers, "the quick call was answered first")
        text = quick["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text.splitlines()[0])["argv"], ["echo", "relay"])
        self.assertEqual(quick["result"]["_meta"]["exitCode"], 0)
        self.assertNotIn("isError", quick["result"])
        slow = wait(3)
        self.assertEqual(slow["result"]["_meta"]["exitCode"], 0)
        counters = quick["result"]["_meta"]["counters"]
        self.assertEqual((counters["files_written"], counters["deckjs_full_parses"]), (0, 0), "the worker's counts (K-1)")
        self.assertIn(counters.get("spawned", 0), (0, 1), "a worker started for the call is counted")
        with lock:
            progress = [m for m in answers.values() if m.get("method") == "notifications/progress"]
        self.assertTrue(progress and progress[0]["params"]["progressToken"] == "tok")
        send(5, "tools/call", {"name": "pptx", "arguments": {"args": ["fail", "3"]}})
        failed = wait(5)["result"]
        self.assertTrue(failed["isError"])
        self.assertEqual(failed["_meta"]["exitCode"], 3)
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=30), 0)
        log = (self.box.home / "logs" / "client.log").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(log, r"relay: pptx echo relay: \d+ ms = forwarded \d+, first frame \d+, done \d+, written \d+; "
                              r"exit 0, response \d+ bytes, 0 image\(s\) of 0 bytes; the service's own \d+ ms")


if __name__ == "__main__":
    unittest.main()
