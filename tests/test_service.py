"""The resident service (client/theodore_client/service/): contract C1 at its endpoint, the worker pool of C2 with a
stand-in engine, crashes, cancellation, deadlines, the lifecycle (no process left after the last session) and the
hand-over to an updated client. macOS and Linux run these; the Windows rig runs lanes/A-rig.md."""

from __future__ import annotations

import base64
import contextlib
import json
import os
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
        stuck = [pid for pid in sorted(services) if not wait_gone(pid, 10)]
        self.assertEqual(stuck, [], "a service did not end within 10 s of SIGTERM")
        deadline = time.monotonic() + 5
        while (left := [p for p in sandbox_processes(self.box.tmp.name) if "engine.py serve" in p[1]]) \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(left, [], "workers outlived their service")

    def connect(self, role: str = "relay", session: str = "s1") -> tuple[service_client.ServiceClient, Collector]:
        collector = Collector()
        client = service_client.ServiceClient(self.box.home, role=role, name="theodore-relay-py" if role == "relay" else "theodore-front-py",
                                              session_id=session, host="claude-code", on_frame=collector,
                                              on_notify=collector.notify)
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
        self.assertLess(time.monotonic() - started, 5)
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
        self.call(client, got, "long", ["sleep", "4"], wait=False)
        time.sleep(0.3)
        started = time.monotonic()
        for n in range(5):
            self.call(client, got, f"s{n}", ["echo", str(n)], wait=False)
        for n in range(5):
            self.assertEqual(Collector.done(got.wait(f"s{n}"))["exit"], 0)
        self.assertLess(time.monotonic() - started, 3.5, "short calls do not wait behind the long one")
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
        frames_ = got.wait("o", 30)
        self.assertLess(time.monotonic() - started, 20, "answered at once, not when the pipe closes")
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
                    mock.patch.object(workers, "_ram_bytes", return_value=ram):
                self.assertEqual(workers.pool_sizes(), (1, most))
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
            warm = SimpleNamespace(build_threads=3, busy=False, rank=None, keys=set(), calls=9)
            other = SimpleNamespace(build_threads=1, busy=False, rank=None, keys=set(), calls=0)
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
            same = [SimpleNamespace(build_threads=3, busy=False, rank=None, keys=set(), calls=c) for c in (5, 1)]
            self.assertIs(pool._choose(same, same, None, long_, 0.0), same[1], "all alike: the least used")

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


class Timings(ServiceCase):
    def test_with_timings_on_each_pptx_call_and_each_worker_say_their_time_in_the_log(self):
        self.env["THEODORE_SERVICE_TIMINGS"] = "1"
        self.service()
        client, got = self.connect()
        self.call(client, got, "t", ["echo"], env={"THEODORE_TIMINGS": "1"})
        log = (self.box.home / "logs" / "service.log").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(log, r"worker \S+ ready \d+ ms after its start \(its warm-up .*features: none\)")
        self.assertRegex(log, r"timing: pptx echo : \d+ ms = admission \d+ \+ queue \d+ \+ worker \d+ \(the engine's own")
        self.assertIn("timings: echo 5 ms (fake.step 5)", log, "the engine's own line, under the call's")


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
        # of the burst is gathered, in equal shares over the free workers.
        self.assertLessEqual(batches.count(1), 1, f"at most the first ran alone: {batches}")
        self.assertGreaterEqual(max(batches), 2, f"the others went in one build: {batches}")
        self.assertLessEqual(len(set(batches) - {1}), 2, f"in equal shares: {batches}")
        lone = json.loads(Collector.stdout(self.burst(lead, ["s01"], hold="0", tag="later-")["s01"]).splitlines()[0])
        self.assertNotIn("batch", lone, "a part alone afterwards: the plain apply")
        cores = os.cpu_count() or 2
        self.assertIn(lone["build_threads"], {str(min(4, max(1, cores - 1))), str(min(4, max(1, cores // 2)))},
                      "THEODORE_BUILD_THREADS: the warm worker's full counts, or the cap of one started under load")
        self.assertEqual(lone["render_threads"], lone["build_threads"], "THEODORE_RENDER_THREADS, the same cap")
        log = (self.box.home / "logs" / "service.log").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(log, r"parts: \d+ of a deck in \d+ build\(s\)", "the log says how a burst was built")

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
        self.assertIn(exits, ([2] * 5, [0] + [2] * 4), "every designer gathered got the run's own refusal (the first "
                                                         "may have run alone, before the others came)")
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


class BuildAhead(ServiceCase):
    """A Studio part built before its designer applies it (ahead.py; the coordinator, 25 Nov; lanes S2 and G): a saved
    part's deck.js gets `apply <part> --ahead` on an idle worker; never the last idle one, never without the engine's
    feature; a newer save or the designer's apply on a newer deck.js cancels it; a call with no idle worker takes it."""

    prepared = StudioParts.prepared

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
        log = (self.box.home / "logs" / "service.log").read_text()
        self.assertIn("ahead: watching 3 part(s) of deck", log)
        self.assertIn("done: built", log)

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
        self.env["FAKE_AHEAD_S"] = "5"
        self.service()
        lead = self.prepared(2)
        self.save(lead, "s01", "slide one, saved\n")
        self.assertTrue(self.wait_for(("ahead-start", "s01")), self.events())
        self.save(lead, "s01", "slide one, saved again with more words\n")
        began = time.monotonic()
        applied = self.call(self.client_, self.got, "a1", ["apply", str(lead / "parts" / "s01")])
        self.assertEqual(Collector.done(applied)["exit"], 0)
        self.assertLess(time.monotonic() - began, 3.0, "not after the 5 s ahead")
        self.assertTrue(self.wait_for(("ahead-cancelled", "s01")), self.events())

    def test_a_call_that_finds_no_idle_worker_takes_the_aheads(self):
        self.env["FAKE_AHEAD_S"] = "8"
        self.service()
        lead = self.prepared(2)
        self.save(lead, "s01", "slide one, saved\n")
        self.assertTrue(self.wait_for(("ahead-start", "s01")), self.events())
        self.call(self.client_, self.got, "long", ["sleep", "4"], wait=False)     # the other worker, now busy
        time.sleep(0.5)
        began = time.monotonic()
        quick = self.call(self.client_, self.got, "quick", ["echo"])
        self.assertEqual(Collector.done(quick)["exit"], 0)
        self.assertLess(time.monotonic() - began, 3.0, "on the ahead's worker, not after the sleep or the ahead")
        self.assertTrue(self.wait_for(("ahead-cancelled", "s01")), self.events())
        self.assertIn("cancelling: a call waits for a worker", (self.box.home / "logs" / "service.log").read_text())
        self.got.wait("long", 30)

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
        self.assertTrue(wait_gone(record["pid"], 10), "the service exits after its linger")
        self.assertTrue(wait_gone(worker_pid, 5), "its worker ends with it")
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
        self.assertTrue(wait_gone(record["pid"], 15), "gone after the relay's linger, not 60 s after the shell command")

    def test_a_sessions_hung_call_does_not_keep_the_service_after_the_session_ends(self):
        # K's A3b (rig 1): fifteen hung calls kept the service and its workers up past the 3-minute count.
        self.env["THEODORE_ORPHAN_GRACE_S"] = "1"
        record = self.service()
        client, got = self.connect()
        self.call(client, got, "hung", ["echo", "--freeze"], wait=False)       # stuck: it answers nothing, not a cancel
        time.sleep(1.0)
        client.close()                                                        # the session ends
        self.assertTrue(wait_gone(record["pid"], 15), "the call stopped after the grace, then the linger: gone")
        log = (self.box.home / "logs" / "service.log").read_text()
        self.assertIn("session ended with 1 call(s) running: cancelled", log)
        self.assertIn("still running after its session ended: its worker stopped", log)

    def test_a_removed_home_ends_the_service_and_stays_removed(self):
        record = self.service()
        client, got = self.connect()
        worker_pid = json.loads(Collector.stdout(self.call(client, got, "e", ["echo"])))["pid"]
        shutil.rmtree(self.box.home)            # removed under a running service (its sessions still open)
        self.assertTrue(wait_gone(record["pid"], 10), "the service ends once its home is gone")
        self.assertTrue(wait_gone(worker_pid, 5), "its worker ends with it")
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
        self.assertTrue(wait_gone(record["pid"], 10))
        self.assertTrue(wait_gone(answer["pid"], 5))

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
        self.assertTrue(wait_gone(record["pid"], 10), "the old service exits")
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
            self.assertTrue(wait_gone(record["pid"], 15))
        self.assertTrue(wait_gone(first["pid"], 10))
        self.assertFalse((self.box.home / "state" / "chat" / "state" / "entitlement.json").exists(), "and goes with it")


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


if __name__ == "__main__":
    unittest.main()
