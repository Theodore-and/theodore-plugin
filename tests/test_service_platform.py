"""The service on this system, end to end, with no fake API: the start rule (a detached start, the start lock), the
endpoint of this platform (a Windows named pipe; a Unix socket elsewhere), a warm worker and its crash, the relay, the
lifecycle (no process after the last relay, nor after a hard kill of the service) and `theodore stop`.

The suite that runs on the Windows rig for lane A (lanes/A-rig.md); it runs here too. The licence is a token in the
environment (THEODORE_ENTITLEMENT), as an engine test gives it; nothing reaches a network."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from service_support import FAKE_ENGINE_PY, FAKE_ONESHOT_PY, Collector  # noqa: E402
from support import CLIENT_DIR, sandbox_processes, stop_sandbox_processes  # noqa: E402

from theodore_client.service import client as service_client, endpoint  # noqa: E402

IS_WINDOWS = os.name == "nt"
ENGINE = "1.0.0-rc.91"


def _token() -> str:
    b64 = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    now = int(time.time())
    return b64({"alg": "EdDSA"}) + "." + b64({"aud": "theodore-engine", "feat": ["pptx", "deck-design"], "exp": now + 86400,
                                              "iat": now, "nbf": now - 60, "plan": "test"}) + ".x"


class PlatformCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theodore-svc-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.work = self.tmp / "work"
        self.work.mkdir()
        engine = self.home / "engines" / ENGINE
        scripts = engine / "skills" / "pptx" / "scripts"
        scripts.mkdir(parents=True)
        (engine / "VERSION").write_text(ENGINE)
        (engine / ".theodore-engine.json").write_text(json.dumps({"version": ENGINE, "sha256": "0" * 64}))
        (scripts / "engine.py").write_text(FAKE_ENGINE_PY)
        (scripts / "pptx.py").write_text(FAKE_ONESHOT_PY)
        (self.home / "engines" / "current.json").write_text(json.dumps({"version": ENGINE}))
        (self.home / "runtime" / "rt").mkdir(parents=True)
        (self.home / "runtime" / "rt" / ".ready").write_text("1")
        (self.home / "runtime" / "current.txt").write_text("rt")
        (self.home / "state").mkdir()
        now = int(time.time())
        (self.home / "state" / "update.json").write_text(json.dumps({"published_packs": [], "last_check": now,
                                                                     "last_attempt": now, "checked_session": "p1"}))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "THEODORE_"))}
        self.env.update({"THEODORE_HOME": str(self.home), "THEODORE_ENTITLEMENT": _token(), "THEODORE_CREDENTIAL_STORE": "file",
                         "THEODORE_SERVICE_LINGER_S": "2", "THEODORE_SERVICE_IDLE_S": "4", "THEODORE_SERVICE_HELLO_S": "1",
                         "THEODORE_SERVICE_WORKERS": "2"})
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)                     # the start rule starts the service with this process's environment

        def restore():
            os.environ.clear()
            os.environ.update(saved)
        self.addCleanup(restore)
        self.addCleanup(self._stop)

    def _stop(self):
        """The service ends within 10 s of its stop and its workers with it (on Windows its job object ends them); the
        test fails otherwise, after what is left is ended."""
        record = endpoint.read_service_json(self.home)
        gone = True
        if record and endpoint.pid_alive(record["pid"]):
            from theodore_client.service import control
            os.environ["THEODORE_HOME"] = str(self.home)
            control._terminate(record["pid"])            # noqa: SLF001
            gone = self.gone(record["pid"], 10)
        workers = []
        if not IS_WINDOWS:
            deadline = time.monotonic() + 5
            while (workers := [p for p in sandbox_processes(self.tmp.name) if "engine.py serve" in p[1]]) \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
        left = stop_sandbox_processes(self.tmp, self.home)
        self.assertTrue(gone, "the service did not end within 10 s of its stop")
        self.assertEqual(workers, [], "workers outlived their service")
        self.assertEqual(left, [], "processes outlived SIGKILL")

    @staticmethod
    def gone(pid, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not endpoint.pid_alive(pid):
                return True
            time.sleep(0.05)
        return not endpoint.pid_alive(pid)

    def client(self, role: str = "relay", session: str = "p1"):
        collector = Collector()
        client = service_client.ServiceClient(self.home, role=role, name="theodore-relay-py", session_id=session,
                                              host="claude-code", on_frame=collector)
        client.connect(start=True)
        self.addCleanup(client.close)
        return client, collector

    def call(self, client, collector, call_id: str, args: list[str], wait: bool = True):
        client.send_call({"t": "call", "id": call_id, "tool": "pptx", "args": args, "cwd": str(self.work)})
        return collector.wait(call_id, 90) if wait else None


class Platform(PlatformCase):
    def test_start_endpoint_worker_crash_and_stop(self):
        started = time.perf_counter()               # perf_counter: Windows' monotonic clock ticks every 15.6 ms
        client, got = self.client()
        cold = time.perf_counter() - started
        record = endpoint.read_service_json(self.home)
        self.assertEqual(record["endpoint"]["kind"], "pipe" if IS_WINDOWS else "unix")
        if IS_WINDOWS:
            self.assertTrue(record["endpoint"]["address"].startswith(r"\\.\pipe\theodore-"))
        first = json.loads(Collector.stdout(self.call(client, got, "a", ["echo", "one"])).splitlines()[0])
        self.assertEqual(first["resident"], "1")
        timed = []
        for n in range(5):
            began = time.perf_counter()
            again = json.loads(Collector.stdout(self.call(client, got, f"w{n}", ["echo", str(n)])).splitlines()[0])
            timed.append((time.perf_counter() - began) * 1000)
            self.assertEqual(again["pid"], first["pid"], "the same warm worker")
        print(f"\n[platform] cold start and connect {cold:.2f} s; warm calls at the C1 boundary (ms): "
              f"{', '.join(f'{t:.1f}' for t in timed)}", file=sys.stderr)
        crashed = self.call(client, got, "c", ["crash"])
        self.assertEqual(Collector.done(crashed)["exit"], 78, "a worker gone during a call: 78 and what to do")
        self.assertIn("THEODORE INTERNAL: Theodore's engine ended", Collector.stdout(crashed))
        after = json.loads(Collector.stdout(self.call(client, got, "d", ["echo"])).splitlines()[0])
        self.assertNotEqual(after["pid"], first["pid"])
        # theodore stop: the service and its worker end
        result = subprocess.run([sys.executable, "-I", str(CLIENT_DIR), "stop"], env=self.env, capture_output=True, text=True,
                                timeout=60)
        self.assertTrue(result.stdout.startswith("THEODORE OK: "), result.stdout + result.stderr)
        self.assertTrue(self.gone(record["pid"], 15), "the service ended")
        self.assertTrue(self.gone(after["pid"], 15), "its worker ended with it")

    def test_a_hard_kill_of_the_service_takes_its_workers(self):
        client, got = self.client()
        record = endpoint.read_service_json(self.home)
        worker = json.loads(Collector.stdout(self.call(client, got, "a", ["echo"])).splitlines()[0])["pid"]
        from theodore_client.service import control
        if IS_WINDOWS:
            control._terminate(record["pid"])            # noqa: SLF001  TerminateProcess: no clean-up runs
        else:
            os.kill(record["pid"], 9)
        self.assertTrue(self.gone(record["pid"], 10))
        self.assertTrue(self.gone(worker, 15), "the job object (Windows) or the end of its stdin (elsewhere) ends it")
        # the next command finds no service and starts a new one
        client2, got2 = self.client(session="p2")
        again = json.loads(Collector.stdout(self.call(client2, got2, "b", ["echo"])).splitlines()[0])
        self.assertEqual(again["argv"], ["echo"])

    def test_a_client_update_hands_over_without_losing_the_call_in_flight(self):
        client, got = self.client()
        record = endpoint.read_service_json(self.home)
        self.call(client, got, "inflight", ["sleep", "2"], wait=False)
        time.sleep(0.4)
        # a newer client installed beside it, named by launch.json (C1) and client/current.txt
        new = self.home / "client" / "9.9.9"
        shutil.copytree(CLIENT_DIR, new, ignore=shutil.ignore_patterns("__pycache__"))
        init = new / "theodore_client" / "__init__.py"
        import theodore_client
        init.write_text(init.read_text().replace(f'"{theodore_client.__version__}"', '"9.9.9"'))
        (self.home / "bin").mkdir(exist_ok=True)
        (self.home / "bin" / "launch.json").write_text(json.dumps({
            "v": 1, "home": str(self.home), "runtime_python": sys.executable, "client_dir": str(new), "client_version": "9.9.9",
            "engine_dir": str(self.home / "engines" / ENGINE), "engine_version": ENGINE}))
        (self.home / "client" / "current.txt").write_text("9.9.9")
        self.assertEqual(Collector.done(got.wait("inflight", 60))["exit"], 0, "the call in flight finished")
        deadline = time.monotonic() + 30
        successor = None
        while time.monotonic() < deadline:
            successor = endpoint.read_service_json(self.home)
            if successor and successor["pid"] != record["pid"]:
                break
            time.sleep(0.1)
        self.assertEqual((successor or {}).get("version"), "9.9.9")
        if IS_WINDOWS:          # the old service's pipe instances may still hold the plain name: then its own (A-3)
            self.assertTrue(successor["endpoint"]["address"].startswith(r"\\.\pipe\theodore-"))
        after = json.loads(Collector.stdout(self.call(client, got, "after", ["echo", "new"])).splitlines()[0])
        self.assertEqual(after["argv"], ["echo", "new"])
        self.assertTrue(self.gone(record["pid"], 15), "the old service left")

    def test_the_relay_and_the_lifecycle(self):
        proc = subprocess.Popen([sys.executable, "-I", str(CLIENT_DIR), "mcp"], env=self.env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(self.work))
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        answers: dict = {}
        cond = threading.Condition()

        def read():
            for line in proc.stdout:
                message = json.loads(line)
                with cond:
                    answers[message.get("id", f"n{len(answers)}")] = message
                    cond.notify_all()
        threading.Thread(target=read, daemon=True).start()

        def ask(ident, method, params=None):
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, **({"params": params} if params else {})})
                              + "\n").encode())
            proc.stdin.flush()
            with cond:
                cond.wait_for(lambda: ident in answers, 90)
                return answers[ident]
        ask(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "2"}})
        tools = sorted(t["name"] for t in ask(2, "tools/list")["result"]["tools"])
        self.assertEqual(tools, ["guide", "list", "pptx", "specimen", "theodore"])
        result = ask(3, "tools/call", {"name": "pptx", "arguments": {"args": ["echo", "relay"]}})["result"]
        self.assertEqual(json.loads(result["content"][0]["text"].splitlines()[0])["argv"], ["echo", "relay"])
        record = endpoint.read_service_json(self.home)
        worker = json.loads(result["content"][0]["text"].splitlines()[0])["pid"]
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertTrue(self.gone(record["pid"], 15), "no service after the last relay (linger 2 s here)")
        self.assertTrue(self.gone(worker, 15), "nor its worker")


if __name__ == "__main__":
    unittest.main()
