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
        self.assertEqual(Collector.done(frames_)["exit"], 75)
        self.assertIn("THEODORE RETRY", Collector.stdout(frames_))
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
        self.assertEqual(Collector.done(moved)["exit"], 75, "its input changed: not run again")

    def test_cancel_resend_and_the_shell_deadline(self):
        self.service()
        client, got = self.connect()
        self.call(client, got, "c1", ["sleep", "20"], wait=False)
        time.sleep(0.5)
        client.cancel("c1")
        done = Collector.done(got.wait("c1", 10))
        self.assertEqual(done["exit"], 130)
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
        first = got3.wait("f1", 10)
        self.assertEqual(Collector.done(first)["exit"], 124)
        self.assertEqual(json.loads(Collector.stdout(first))["code"], "STILL_RUNNING")
        front.send_call({"t": "call", "id": "f2", "tool": "pptx", "cwd": "/", "args": ["sleep", "2.5"]}, start=False)
        again = got3.wait("f2", 10)
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


class Lifecycle(ServiceCase):
    def test_nothing_runs_after_the_last_relay_detaches(self):
        record = self.service()
        client, got = self.connect()
        worker_pid = json.loads(Collector.stdout(self.call(client, got, "e", ["echo"])))["pid"]
        self.assertTrue(alive(worker_pid))
        self.assertIn(worker_pid, children_of(record["pid"]))
        client.close()
        self.assertTrue(wait_gone(record["pid"], 10), "the service exits after its linger")
        self.assertTrue(wait_gone(worker_pid, 5), "its worker ends with it")
        self.assertFalse((self.box.home / "state" / "run" / "service.json").exists())

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
