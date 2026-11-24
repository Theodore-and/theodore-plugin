"""The native front (front/, DESIGN §2.1 and §2.2, contract C1), end to end against a stand-in service (front_fake.py).

The binary is THEODORE_FRONT_BIN when set; otherwise it is built here with cargo (a debug build in .cache/); without
either the tests are skipped. Each test makes a throwaway Theodore folder whose launch.json starts the stand-in.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"
_BINARY: list[Path | None] = []


def front_binary() -> Path | None:
    if _BINARY:
        return _BINARY[0]
    found = os.environ.get("THEODORE_FRONT_BIN")
    path = Path(found) if found else None
    if path is None:
        cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo")
        if Path(cargo).exists() or shutil.which("cargo"):
            target = ROOT / ".cache" / "front-target"
            env = dict(os.environ)
            done = subprocess.run([cargo, "build", "-q", "-j", "2"], cwd=ROOT / "front", env={**env, "CARGO_TARGET_DIR": str(target)},
                                  capture_output=True, text=True)
            if done.returncode == 0:
                path = target / "debug" / ("theodore.exe" if IS_WINDOWS else "theodore")
            else:
                sys.stderr.write(done.stderr)
    _BINARY.append(path if path and path.exists() else None)
    return _BINARY[0]


def plugin_version() -> str:
    return json.loads((ROOT / "plugins" / "theodore" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


class FrontCase(unittest.TestCase):
    """A throwaway home whose launch.json runs the stand-in; every stand-in service is stopped afterwards."""

    def setUp(self):
        self.bin = front_binary()
        if self.bin is None:
            self.skipTest("no front binary (set THEODORE_FRONT_BIN or install cargo)")
        base = "/tmp" if not IS_WINDOWS and Path("/tmp").is_dir() else None     # Unix socket paths stay short
        self.tmp = Path(tempfile.mkdtemp(prefix="tf", dir=base)).resolve()
        self.home = self.tmp / "h"
        self.client = self.tmp / "client"
        self.client.mkdir(parents=True)
        (self.client / "__main__.py").write_text(
            f"import sys\nsys.path.insert(0, {str(TESTS)!r})\nimport front_fake\nraise SystemExit(front_fake.main(sys.argv[1:]))\n",
            encoding="utf-8")
        self.write_launch()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("THEODORE_", "CLAUDE", "CODEX_", "FAKE_", "BASH_MAX"))}
        self.env.update({"THEODORE_HOME": str(self.home), "FAKE_SERVICE_IDLE": "15"})

    def tearDown(self):
        if getattr(self, "tmp", None) is None:
            return
        for event in self.events():
            if event.get("e") == "service-started":
                try:
                    os.kill(event["pid"], signal.SIGTERM if not IS_WINDOWS else signal.SIGTERM)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_launch(self, **extra):
        (self.home / "bin").mkdir(parents=True, exist_ok=True)
        doc = {"v": 1, "home": str(self.home), "runtime_python": sys.executable, "client_dir": str(self.client),
               "client_version": "0.0.0-test", "engine_dir": str(self.home / "engines" / "x"), "engine_version": "x", **extra}
        (self.home / "bin" / "launch.json").write_text(json.dumps(doc), encoding="utf-8")

    def events(self, kind: str | None = None) -> list[dict]:
        path = self.home / "state" / "run" / "events.jsonl"
        if not path.exists():
            return []
        found = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [e for e in found if kind is None or e.get("e") == kind]

    def start_service(self, **env) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-I", str(self.client), "service", "--home", str(self.home)],
                                env={**self.env, **env}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.poll() is None and proc.terminate(), proc.wait(timeout=10)))
        deadline = time.monotonic() + 10
        while not (self.home / "state" / "run" / "service.json").exists():
            if time.monotonic() > deadline:
                self.fail("the stand-in service did not start")
            time.sleep(0.02)
        return proc

    def front(self, *args: str, stdin: bytes | None = None, env: dict | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.bin), *args], input=stdin, capture_output=True, timeout=timeout,
                              env={**self.env, **(env or {})}, cwd=str(self.work))

    def answer(self, done: subprocess.CompletedProcess) -> dict:
        return json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])


class Front(FrontCase):
    def test_a_command_goes_to_the_running_service(self):
        self.start_service()
        done = self.front("pptx", "echo", "a", "b c", "é", env={"THEODORE_PROBE": "1", "UNRELATED": "x"})
        self.assertEqual(done.returncode, 0, done.stderr)
        answer = self.answer(done)
        self.assertEqual(answer["tool"], "pptx")
        self.assertEqual(answer["args"], ["echo", "a", "b c", "é"])
        self.assertEqual(Path(answer["cwd"]).resolve(), self.work.resolve())
        self.assertEqual(answer["env"].get("THEODORE_PROBE"), "1")
        self.assertNotIn("UNRELATED", answer["env"])
        self.assertIsNone(answer["deadline_ms"])
        hello = self.events("hello")[-1]
        self.assertEqual((hello["role"], hello["client"]["name"], hello["protocol"]), ("front", "theodore-front", 1))
        self.assertEqual(hello["client"]["version"], plugin_version())
        self.assertEqual(hello["session"]["host"], "shell")
        self.assertFalse(self.events("oneshot"))
        call = self.events("call")[-1]
        self.assertFalse(call["images"])

    def test_client_commands_go_as_the_theodore_tool(self):
        self.start_service()
        answer = self.answer(self.front("guide", "pptx", "slide-parts", "--role", "slide"))
        self.assertEqual((answer["tool"], answer["args"]), ("theodore", ["guide", "pptx", "slide-parts", "--role", "slide"]))

    def test_exit_codes_and_streams_are_the_commands(self):
        self.start_service()
        for code in (1, 2, 75, 77):
            done = self.front("pptx", "fail", str(code))
            self.assertEqual(done.returncode, code)
            self.assertIn(b"it failed", done.stderr)
            self.assertEqual(self.answer(done), {"ok": False})

    def test_skill_prints_one_body_and_exits_zero(self):
        self.start_service()
        done = self.front("skill", "pptx")
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"BODY of the skill", done.stdout)
        self.assertIn(b"a warning", done.stdout)

    def test_standard_input_after_a_dash(self):
        self.start_service()
        answer = self.answer(self.front("pptx", "echo", "--from", "-", stdin="ligne é\n".encode()))
        self.assertEqual(answer["stdin"], {"encoding": "utf-8", "data": "ligne é\n"})
        answer = self.answer(self.front("pptx", "echo", "--from", "-", stdin=b"\xff\xfe\x00"))
        self.assertEqual(answer["stdin"], {"encoding": "base64", "data": "//4A"})
        self.assertIsNone(self.answer(self.front("pptx", "echo", stdin=b"unread"))["stdin"])

    def test_deadline_under_an_assistant(self):
        self.start_service()
        self.assertEqual(self.answer(self.front("pptx", "echo", env={"CLAUDECODE": "1"}))["deadline_ms"], 570000)
        self.assertEqual(self.answer(self.front("pptx", "echo", env={"CLAUDECODE": "1", "BASH_MAX_TIMEOUT_MS": "200000"}))["deadline_ms"], 170000)
        self.assertEqual(self.answer(self.front("pptx", "echo", env={"THEODORE_DEADLINE_MS": "5000"}))["deadline_ms"], 5000)
        hello = self.events("hello")[-3]
        self.assertEqual(hello["session"]["host"], "claude-code")

    def test_large_output_streams_whole(self):
        self.start_service()
        done = self.front("pptx", "big", str(3 * 1024 * 1024 + 7))
        self.assertEqual(done.returncode, 0)
        self.assertEqual(len(done.stdout), 3 * 1024 * 1024 + 7)

    def test_busy_is_waited_out(self):
        self.start_service()
        done = self.front("pptx", "busy", "1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self.events("call")), 2)

    def test_a_lost_connection_is_resent(self):
        self.start_service()
        done = self.front("pptx", "drop", "1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.answer(done), {"resent": True})
        self.assertEqual([c["resend"] for c in self.events("call")], [False, True])
        self.assertEqual(len({c["id"] for c in self.events("call")}), 1)

    def test_starts_the_service_detached_when_none_runs(self):
        done = self.front("pptx", "echo", "first")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.answer(done)["args"], ["echo", "first"])
        started = self.events("service-started")
        self.assertEqual(len(started), 1)
        if not IS_WINDOWS:
            self.assertEqual(started[0]["ppid"], 1, "the service is nobody's child")
        self.assertFalse(self.events("oneshot"))
        os.kill(started[0]["pid"], 0)       # still running after the front ended
        done = self.front("pptx", "echo", "second")
        self.assertEqual(len(self.events("service-started")), 1)
        self.assertEqual(self.answer(done)["args"], ["echo", "second"])

    def test_one_service_for_many_fronts_at_once(self):
        procs = [subprocess.Popen([str(self.bin), "pptx", "echo", str(i)], env=self.env, cwd=str(self.work),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(8)]
        outs = [p.communicate(timeout=30) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * 8, [o[1] for o in outs])
        self.assertEqual(len(self.events("service-started")), 1)
        self.assertEqual(sorted(json.loads(o[0])["args"][1] for o in outs), sorted(str(i) for i in range(8)))

    def test_one_shot_when_the_service_cannot_start(self):
        env = {"FAKE_SERVICE_FAIL": "1"}
        started = time.monotonic()
        done = self.front("pptx", "echo", "x", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.answer(done)["oneshot"], ["pptx", "echo", "x"])
        self.assertIn(b"without Theodore's service", done.stderr)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue((self.home / "state" / "run" / "start-failed").exists())
        started = time.monotonic()
        done = self.front("pptx", "echo", "y", env=env)
        self.assertEqual(self.answer(done)["oneshot"], ["pptx", "echo", "y"])
        self.assertNotIn(b"without Theodore's service", done.stderr, "said once")
        self.assertLess(time.monotonic() - started, 4, "no second wait for a start that fails")

    def test_one_shot_keeps_standard_input(self):
        done = self.front("pptx", "slide", "set", "w", "s1", "--from", "-", stdin=b"block text\n", env={"FAKE_SERVICE_FAIL": "1"})
        answer = self.answer(done)
        self.assertEqual(answer["file_stdin"], "block text\n")
        self.assertFalse(list((self.home / "state" / "run").glob("stdin-*")))

    def test_one_shot_by_configuration(self):
        self.write_launch(oneshot=True)
        done = self.front("pptx", "echo", "z", "exit7")
        self.assertEqual(done.returncode, 7)
        self.assertEqual(self.answer(done)["oneshot"], ["pptx", "echo", "z", "exit7"])
        self.assertEqual(self.answer(done)["home"], str(self.home))
        self.assertFalse(self.events("service-started"))
        self.write_launch()
        done = self.front("status", env={"THEODORE_RESIDENT": "0"})
        self.assertEqual(self.answer(done)["oneshot"], ["status"])
        self.assertFalse(self.events("service-started"))

    def test_local_commands_never_use_the_service(self):
        self.start_service()
        for command in ("uninstall", "python", "setup"):
            done = self.front(command, "--x")
            self.assertEqual(self.answer(done)["oneshot"], [command, "--x"])
        self.assertFalse(self.events("call"))

    def test_an_installation_without_launch_json_runs_one_shot(self):
        (self.home / "bin" / "launch.json").unlink()
        runtime = self.home / "runtime" / "rt1" / "python" / ("python.exe" if IS_WINDOWS else "bin/python3")
        runtime.parent.mkdir(parents=True)
        try:
            os.symlink(sys.executable, runtime)
        except OSError:
            shutil.copy2(sys.executable, runtime)
        (self.home / "runtime" / "current.txt").write_text("rt1\n")
        shutil.copytree(self.client, self.home / "client" / "0.4.33")
        (self.home / "client" / "current.txt").write_text("0.4.33\n")
        done = self.front("doctor")
        self.assertEqual(self.answer(done)["oneshot"], ["doctor"])
        self.assertFalse(self.events("service-started"))

    @unittest.skipIf(IS_WINDOWS, "the set-up launcher there is theodore.cmd")
    def test_nothing_installed_hands_over_to_the_set_up_launcher(self):
        shutil.rmtree(self.home / "bin")
        (self.home / "bin").mkdir()
        (self.home / "bin" / "theodore").write_text('#!/bin/sh\necho "setup launcher: $*"\nexit 67\n')
        done = self.front("guide", "pptx")
        self.assertEqual(done.returncode, 67)
        self.assertEqual(done.stdout.decode().strip(), "setup launcher: guide pptx")
        (self.home / "bin" / "theodore").unlink()
        done = self.front("guide", "pptx")
        self.assertEqual(done.returncode, 78)
        self.assertIn(b"theodoreand.ai/install", done.stdout)

    @unittest.skipIf(IS_WINDOWS, "Ctrl+C is a console event there")
    def test_ctrl_c_cancels_the_call(self):
        self.start_service()
        proc = subprocess.Popen([str(self.bin), "pptx", "sleep", "20000"], env=self.env, cwd=str(self.work),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while not self.events("call") and time.monotonic() < deadline:
            time.sleep(0.02)
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 130, err)
        self.assertEqual([e["id"] for e in self.events("cancel")], [self.events("call")[0]["id"]])

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists(), "macOS sandbox profiles")
    def test_a_warm_command_starts_no_program(self):
        """The Bash route's pass line 'zero helper programs': with process creation denied, a command still runs whole
        through the service (one-shot and service starts both need a new process; neither may be taken)."""
        self.start_service()
        done = subprocess.run(["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny process-fork)", str(self.bin),
                               "pptx", "echo", "warm"], capture_output=True, env=self.env, cwd=str(self.work), timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.answer(done)["args"], ["echo", "warm"])
        self.assertFalse(self.events("oneshot"))

    def test_diagnostics(self):
        done = self.front("__front", "version")
        self.assertEqual(done.returncode, 0)
        doc = json.loads(done.stdout)
        self.assertEqual(doc["front"], plugin_version())
        self.assertEqual(doc["protocol"], 1)
        self.assertEqual(Path(doc["home"]), self.home)
        self.assertEqual(len(doc["id16"]), 16)


class Relay:
    """One `theodore mcp` relay: messages in, every line out collected by a reader thread."""

    def __init__(self, case: FrontCase, *args: str, env: dict | None = None):
        self.proc = subprocess.Popen([str(case.bin), "mcp", *args], env={**case.env, **(env or {})}, cwd=str(case.work),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.lines: queue.Queue = queue.Queue()
        self.seen: list[dict] = []
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 0

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(json.loads(line))
        self.lines.put(None)

    def send(self, method: str, params: dict | None = None, notify: bool = False, raw: bytes | None = None) -> int | None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        mid = None
        if not notify:
            self.next_id += 1
            mid = message["id"] = self.next_id
        self.proc.stdin.write(raw if raw is not None else json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()
        return mid

    def wait_for(self, test, timeout: float = 20.0) -> dict:
        for message in self.seen:
            if test(message):
                self.seen.remove(message)
                return message
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"nothing matched within {timeout} s; seen {self.seen[-5:]}")
            try:
                message = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if message is None:
                raise AssertionError(f"the relay ended; stderr {self.proc.stderr.read()[-2000:]!r}")
            if test(message):
                return message
            self.seen.append(message)

    def answer(self, mid: int, timeout: float = 20.0) -> dict:
        return self.wait_for(lambda m: m.get("id") == mid and ("result" in m or "error" in m), timeout)

    def request(self, method: str, params: dict | None = None) -> dict:
        return self.answer(self.send(method, params))

    def call(self, name: str, **arguments) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def initialize(self, client: str = "claude-code") -> dict:
        answer = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": client, "version": "2.1.330"}})
        self.send("notifications/initialized", notify=True)
        return answer

    def close(self) -> int:
        self.proc.stdin.close()
        code = self.proc.wait(timeout=20)
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


def text_of(result: dict) -> str:
    return result["result"]["content"][0]["text"]


class RelayTests(FrontCase):
    def relay(self, *args: str, env: dict | None = None) -> Relay:
        relay = Relay(self, *args, env=env)

        def stop():
            if relay.proc.poll() is None:
                relay.proc.kill()
                relay.proc.wait(timeout=10)
            for stream in (relay.proc.stdin, relay.proc.stdout, relay.proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        self.addCleanup(stop)
        return relay

    def test_initialize_and_tools_list_are_answered_at_once(self):
        relay = self.relay(env={"FAKE_SERVICE_DELAY": "3"})
        started = time.monotonic()
        answer = relay.initialize()
        self.assertEqual(answer["result"]["serverInfo"], {"name": "theodore", "version": plugin_version()})
        self.assertEqual(answer["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(answer["result"]["capabilities"], {"tools": {"listChanged": True}})
        tools = relay.request("tools/list")["result"]["tools"]
        self.assertLess(time.monotonic() - started, 2.5, "no wait for the service")
        self.assertEqual([t["name"] for t in tools], ["pptx", "guide", "specimen", "theodore", "list"])
        self.assertTrue(tools[0]["_meta"]["anthropic/alwaysLoad"])
        self.assertEqual(relay.request("ping")["result"], {})
        self.assertEqual(relay.request("resources/list")["error"]["code"], -32601)
        relay.send("", raw=b"{not json\n")
        self.assertEqual(relay.wait_for(lambda m: "error" in m and m.get("id") is None)["error"]["code"], -32700)
        self.assertEqual(relay.close(), 0)

    def test_calls_are_relayed_and_answered_out_of_order(self):
        """PLAN lane C: 100 interleaved calls answered out of order and correctly."""
        self.start_service()
        relay = self.relay()
        relay.initialize()
        sent = {}
        for i in range(100):
            if i % 2:
                mid = relay.send("tools/call", {"name": "pptx", "arguments": {"args": ["sleep", str((97 * i) % 400), f"t{i}"]}})
            else:
                mid = relay.send("tools/call", {"name": "pptx", "arguments": {"args": ["echo", f"e{i}"]}})
            sent[mid] = i
        order = []
        for _ in range(100):
            message = relay.wait_for(lambda m: m.get("id") in sent and "result" in m, timeout=30)
            i = sent[message["id"]]
            order.append(i)
            body = json.loads(text_of(message))
            if i % 2:
                self.assertEqual(body["tag"], f"t{i}")
            else:
                self.assertEqual(body["args"], ["echo", f"e{i}"])
            self.assertFalse(message["result"]["isError"])
            self.assertEqual(message["result"]["_meta"]["exitCode"], 0)
        self.assertEqual(sorted(order), list(range(100)))
        self.assertNotEqual(order, list(range(100)), "answers come as they complete")
        self.assertEqual(len(self.events("hello")), 1, "one connection for the whole session")
        self.assertEqual(relay.close(), 0)
        deadline = time.monotonic() + 5
        while not self.events("bye") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.events("bye"))

    def test_mapping_of_tools_errors_progress_and_pictures(self):
        self.start_service()
        relay = self.relay("--plugin-root", str(self.tmp))
        relay.initialize()
        failed = relay.call("pptx", args=["fail", "1"])
        self.assertTrue(failed["result"]["isError"])
        self.assertEqual(failed["result"]["content"][1]["text"], "--- stderr ---\nit failed\n")
        self.assertEqual(failed["result"]["_meta"]["exitCode"], 1)
        mid = relay.send("tools/call", {"name": "pptx", "arguments": {"args": ["progress", "2"]}, "_meta": {"progressToken": "tok"}})
        first = relay.wait_for(lambda m: m.get("method") == "notifications/progress")
        self.assertEqual(first["params"], {"progressToken": "tok", "progress": 1.0, "message": "step 1"})
        relay.answer(mid)
        pictures = relay.call("pptx", args=["image"])["result"]
        self.assertEqual([c["type"] for c in pictures["content"]], ["text", "image", "image"])
        self.assertEqual(pictures["content"][1]["mimeType"], "image/png")
        self.assertEqual(pictures["_meta"]["pending"], [{"kind": "picture", "slide": "s002", "tier": "exact"}])
        without = relay.call("pptx", args=["image"], images=False)["result"]
        self.assertEqual([c["type"] for c in without["content"]], ["text"])
        guide = json.loads(text_of(relay.call("guide", skill="pptx", section="x")))
        self.assertEqual(guide, {"tool": "guide", "params": {"skill": "pptx", "section": "x"}})
        echo = json.loads(text_of(relay.call("theodore", args=["status"], cwd="sub")))
        self.assertEqual(Path(echo["cwd"]), self.work / "sub")
        self.assertEqual(echo["env"]["CLAUDE_PLUGIN_ROOT"], str(self.tmp))
        self.assertTrue(relay.call("nothing")["result"]["isError"])
        self.assertTrue(relay.call("pptx", args="slide list")["result"]["isError"])
        hello = self.events("hello")[0]
        self.assertEqual((hello["role"], hello["client"]["name"], hello["session"]["host"]), ("relay", "theodore-relay", "claude-code"))
        self.assertEqual(hello["plugin_root"], str(self.tmp))

    def test_a_cancelled_call_is_stopped_and_never_answered(self):
        self.start_service()
        relay = self.relay()
        relay.initialize()
        mid = relay.send("tools/call", {"name": "pptx", "arguments": {"args": ["sleep", "20000"]}})
        deadline = time.monotonic() + 10
        while not self.events("call") and time.monotonic() < deadline:
            time.sleep(0.02)
        relay.send("notifications/cancelled", {"requestId": mid, "reason": "user"}, notify=True)
        after = relay.call("pptx", args=["echo", "after"])
        self.assertEqual(json.loads(text_of(after))["args"], ["echo", "after"])
        time.sleep(0.3)
        slept = [c["id"] for c in self.events("call") if c["args"] == ["sleep", "20000"]]
        self.assertEqual([e["id"] for e in self.events("cancel")], slept)
        self.assertFalse([m for m in relay.seen if m.get("id") == mid])

    def test_the_services_list_replaces_the_static_one(self):
        self.start_service(FAKE_TOOLS="pptx,guide,specimen,theodore,list,extra")
        relay = self.relay(env={"FAKE_SERVICE_DELAY": "0"})
        relay.initialize()
        self.assertEqual(len(relay.request("tools/list")["result"]["tools"]), 5)
        relay.wait_for(lambda m: m.get("method") == "notifications/tools/list_changed")
        names = [t["name"] for t in relay.request("tools/list")["result"]["tools"]]
        self.assertEqual(names[-1], "extra")
        call = [c for c in self.events("call") if c["tool"] == "list"][0]
        self.assertEqual(call["params"]["form"], "tools")
        self.assertEqual(call["params"]["client"], {"name": "claude-code", "version": "2.1.330"})

    def test_the_service_is_started_and_restarted_with_unanswered_calls_resent(self):
        relay = self.relay()
        relay.initialize()
        mid = relay.send("tools/call", {"name": "pptx", "arguments": {"args": ["sleep", "800", "long"]}})
        deadline = time.monotonic() + 15
        while not self.events("call") and time.monotonic() < deadline:
            time.sleep(0.02)
        first = self.events("service-started")
        self.assertEqual(len(first), 1)
        os.kill(first[0]["pid"], signal.SIGKILL if not IS_WINDOWS else signal.SIGTERM)
        answer = relay.answer(mid, timeout=20)
        self.assertEqual(json.loads(text_of(answer))["tag"], "long")
        self.assertEqual(len(self.events("service-started")), 2)
        resent = [c for c in self.events("call") if c["tool"] == "pptx"]
        self.assertEqual([c["resend"] for c in resent], [False, True])

    def test_the_client_serves_the_session_when_no_service_can_start(self):
        relay = self.relay(env={"FAKE_SERVICE_FAIL": "1"})
        relay.initialize()
        mid = relay.send("tools/call", {"name": "pptx", "arguments": {"args": ["echo"]}})
        answer = relay.answer(mid, timeout=20)
        self.assertEqual(text_of(answer), "fake client: pptx")
        relay.wait_for(lambda m: m.get("method") == "notifications/tools/list_changed") if relay.seen else None
        self.assertEqual([t["name"] for t in relay.request("tools/list")["result"]["tools"]], ["fake-tool"])
        methods = [e["method"] for e in self.events("client-mcp-message")]
        self.assertEqual(methods[:2], ["initialize", "notifications/initialized"])
        self.assertEqual(relay.close(), 0)

    def test_the_chat_tool_for_codex(self):
        self.start_service()
        relay = self.relay("--for", "codex")
        relay.initialize("codex-mcp-client")
        tools = relay.request("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["theodore"])
        answer = json.loads(text_of(relay.call("theodore", action="run", args=["slide", "list"], folder="/x")))
        self.assertEqual(answer["chat"], {"action": "run", "args": ["slide", "list"], "folder": "/x"})
        self.assertEqual(self.events("hello")[0]["session"]["host"], "codex")

    @unittest.skipIf(IS_WINDOWS, "the set-up launcher there is theodore.cmd")
    def test_nothing_installed_hands_the_session_to_the_set_up_launcher(self):
        shutil.rmtree(self.home / "bin")
        plugin = self.tmp / "plugin"
        scripts = plugin / "skills" / "pptx" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "theodore").write_text('#!/bin/sh\nread line\necho "{\\"setup\\": \\"$*\\"}"\n')
        relay = self.relay("--for", "codex", "--plugin-root", str(plugin))
        relay.proc.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        relay.proc.stdin.flush()
        self.assertEqual(relay.wait_for(lambda m: "setup" in m), {"setup": "mcp --for codex"})


class Repository(unittest.TestCase):
    def test_the_chat_tool_asset_is_the_clients(self):
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "front_assets.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_crate_has_no_dependencies(self):
        manifest = (ROOT / "front" / "Cargo.toml").read_text(encoding="utf-8")
        deps = manifest.split("[dependencies]", 1)[1].split("[", 1)[0]
        self.assertEqual(deps.strip(), "", "the front builds from the standard library alone")


if __name__ == "__main__":
    unittest.main()
