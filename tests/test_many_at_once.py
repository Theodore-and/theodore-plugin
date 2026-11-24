"""Many designers starting at once (0.4.34). A real session on Windows, 24 November 2026: nine designers ran their
first `theodore guide` together and two were told THEODORE OFFLINE ("access violation writing 0x0000000000000048").
truststore's Windows check freed a certificate chain crypt32 never built, and the address race counted that native
fault as a dead address. Now the system's check is tried again, a chain not built is not freed, and a failure of this
computer's own code is never "offline". And any number of simultaneous starts, through the shell and the tool server,
all succeed."""

from __future__ import annotations

import ctypes
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import CLIENT_DIR, ClientCase  # noqa: E402
from test_verb_path import HomeCase  # noqa: E402
from theodore_client import api, cli, core  # noqa: E402

FAULT = "exception: access violation writing 0x0000000000000048"


def fake_trust(outcomes: list):
    """A stand-in for truststore's `_api`: each call of its check takes the next outcome (an exception raises)."""
    calls = []

    def check(sock_or_sslobj, server_hostname):
        calls.append(server_hostname)
        outcome = outcomes.pop(0) if outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    return types.SimpleNamespace(_verify_peercerts=check), calls


class CertificateCheck(unittest.TestCase):
    def setUp(self):
        sleeps = mock.patch.object(api.time, "sleep", lambda s: self.slept.append(s))
        self.slept = []
        sleeps.start()
        self.addCleanup(sleeps.stop)

    def test_a_fault_of_the_check_itself_is_tried_again(self):
        trust, calls = fake_trust([OSError(FAULT), OSError(FAULT), "checked"])
        api._guard_certificate_check(trust)
        self.assertEqual(trust._verify_peercerts(object(), "api.example"), "checked")
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.slept, list(api.CERT_CHECK_WAITS[:2]))

    def test_a_fault_that_stays_is_a_local_fault_never_a_network_error(self):
        trust, calls = fake_trust([OSError(FAULT)] * 10)
        api._guard_certificate_check(trust)
        with self.assertRaises(api.LocalFault) as caught:
            trust._verify_peercerts(object(), "api.example")
        self.assertEqual(len(calls), len(api.CERT_CHECK_WAITS) + 1)
        self.assertIn("access violation", str(caught.exception))
        self.assertNotIsInstance(caught.exception, OSError, "the address race does not count it as a dead address")
        self.assertIsInstance(caught.exception, api.NetworkError, "a step that may do without the service still does")
        status = api.offline(caught.exception, "deck-design guide")
        self.assertEqual((status.name, status.cause, getattr(status, "auto_report", False)), ("INTERNAL", "LOCAL_FAULT", True),
                         "where the answer was needed: Theodore's own fault, reported, never OFFLINE")

    def test_a_refused_certificate_is_answered_at_once(self):
        trust, calls = fake_trust([ssl.SSLCertVerificationError("unable to get local issuer certificate")])
        api._guard_certificate_check(trust)
        with self.assertRaises(ssl.SSLCertVerificationError):
            trust._verify_peercerts(object(), "api.example")
        self.assertEqual((len(calls), self.slept), (1, []), "a TLS-inspecting network is told as before")

    def test_on_windows_a_chain_that_was_not_built_is_not_freed(self):
        freed = []
        windows = types.SimpleNamespace(CertFreeCertificateChain=freed.append)
        trust, _calls = fake_trust([])
        api._guard_certificate_check(trust, windows)
        windows.CertFreeCertificateChain(ctypes.POINTER(ctypes.c_int)())        # NULL: crypt32 built none
        self.assertEqual(freed, [])
        built = ctypes.pointer(ctypes.c_int(7))
        windows.CertFreeCertificateChain(built)
        self.assertEqual(freed, [built])

    def test_the_vendored_truststore_is_guarded_once_per_process(self):
        from truststore import _api as trust
        api._ssl_context()
        api._ssl_context()
        self.assertEqual(trust._verify_peercerts.__qualname__, "_guard_certificate_check.<locals>.verify")
        self.assertNotEqual(trust._verify_peercerts.__closure__[0].cell_contents.__qualname__,
                            "_guard_certificate_check.<locals>.verify", "wrapped once, not twice")


class OneFileManyThreads(HomeCase):
    def test_threads_writing_one_state_file_at_once_each_land_whole(self):
        """The tool server's calls run side by side: their writes of one file must not share a temporary name."""
        path = self.home / "state" / "shared.json"
        start, errors = threading.Barrier(12), []

        def one(i):
            start.wait()
            try:
                for _ in range(20):
                    core.write_json(path, {"writer": i, "pad": "x" * 20000})
            except Exception as exc:        # noqa: BLE001  reported below
                errors.append(exc)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertIn(core.read_json(path).get("writer"), range(12), "a whole file from one writer")
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["shared.json"], "no temporary file left")


class Listener:
    """A local TCP port that accepts and holds connections: the network answers."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(32)
        self.port = self.server.getsockname()[1]
        self.held = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                self.held.append(self.server.accept()[0])
            except OSError:
                return

    def close(self):
        self.server.close()
        for connection in self.held:
            connection.close()


class Wrapper:
    """A TLS context stand-in: wrap_socket raises the given exception, or answers a socket-like object."""

    def __init__(self, raises=None):
        self.raises, self.calls = raises, 0

    def wrap_socket(self, sock, server_hostname=None):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return types.SimpleNamespace(settimeout=lambda t: None, close=sock.close, sock=sock)


class AddressRace(HomeCase):
    def setUp(self):
        super().setUp()
        self.listener = Listener()
        self.addCleanup(self.listener.close)

    def connect(self, context):
        return api._connect_any("127.0.0.1", self.listener.port, 5.0, None, context)

    def test_a_native_fault_in_the_check_is_this_computers_own_never_offline(self):
        for raised in (OSError(FAULT), api.LocalFault("the system's certificate check failed 5 times"), TypeError("a bug")):
            with mock.patch.object(core, "IS_WINDOWS", False), self.assertRaises(api.LocalFault, msg=repr(raised)):
                self.connect(Wrapper(raised))

    def test_a_network_failure_is_still_one(self):
        with self.assertRaises(ConnectionError) as caught:
            self.connect(Wrapper(ConnectionResetError(54, "Connection reset by peer")))
        self.assertIn("addresses of 127.0.0.1 failed", str(caught.exception))

    def test_on_windows_the_same_address_is_checked_once_more_by_openssl_and_the_system_store(self):
        second = Wrapper()
        with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(api, "_system_store_context", lambda: second):
            tls = self.connect(Wrapper(api.LocalFault("the system's certificate check failed 5 times")))
        self.assertEqual(second.calls, 1)
        self.assertIsNotNone(tls.sock)
        tls.close()
        failing = Wrapper(ConnectionResetError(54, "reset"))
        with mock.patch.object(core, "IS_WINDOWS", True), mock.patch.object(api, "_system_store_context", lambda: failing), \
                self.assertRaises(api.LocalFault) as caught:
            self.connect(Wrapper(api.LocalFault("the system's certificate check failed 5 times")))
        self.assertIn("then through OpenSSL", str(caught.exception))

    def test_the_command_answers_internal_and_reports_it(self):
        out = io.StringIO()
        with mock.patch.object(cli, "_dispatch", side_effect=api.LocalFault(FAULT)), redirect_stdout(out), \
                mock.patch("theodore_client.commands.report_internal", side_effect=lambda status: status) as reported:
            code = cli._run(["guide", "deck-design"])
        self.assertEqual(code, core.EXIT["INTERNAL"])
        self.assertEqual(reported.call_count, 1, "reported to Theodore by the client itself")
        self.assertTrue(out.getvalue().startswith("THEODORE INTERNAL: "), out.getvalue())
        self.assertNotIn("OFFLINE", out.getvalue())


class ManyAtOnce(ClientCase):
    """Fifteen designers' first commands at the same moment, each its own client process, all on one home."""

    STARTS = 15

    def test_fifteen_first_guides_at_once_all_succeed(self):
        self.ready()
        env = self.box.env
        start = threading.Barrier(self.STARTS)
        results = [None] * self.STARTS

        def one(i):
            start.wait()
            command = [sys.executable, "-I", str(CLIENT_DIR), "guide", "deck-design", "--role", "slide"]
            results[i] = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300,
                                        stdin=subprocess.DEVNULL, cwd=str(self.box.tmp))
        threads = [threading.Thread(target=one, args=(i,)) for i in range(self.STARTS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(330)
        failed = [(r.returncode, (r.stdout or "").splitlines()[:1], r.stderr[-300:]) for r in results if r is None or r.returncode != 0]
        self.assertEqual(failed, [], f"{len(failed)} of {self.STARTS} failed")
        self.assertEqual(len({r.stdout for r in results}), 1, "every designer got the same text")
        self.box.wait_jobs()

    def test_fifteen_guide_calls_at_once_through_one_tool_server(self):
        from test_mcp import Server             # the tool server's own harness
        self.ready()
        server = Server(self)
        self.addCleanup(lambda: server.proc.poll() is None and server.proc.kill())
        server.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        server.send("notifications/initialized", notify=True)
        ids = []
        for _ in range(self.STARTS):
            server.next_id += 1
            ids.append(server.next_id)
            message = {"jsonrpc": "2.0", "id": server.next_id, "method": "tools/call",
                       "params": {"name": "guide", "arguments": {"skill": "deck-design", "role": "slide"}}}
            server.proc.stdin.write((json.dumps(message) + "\n").encode())
        server.proc.stdin.flush()
        answers = {}
        while len(answers) < self.STARTS:
            line = server.proc.stdout.readline()
            self.assertTrue(line, "the tool server ended before answering every call")
            message = json.loads(line)
            if message.get("id") in ids:
                answers[message["id"]] = message
        errors = [a for a in answers.values() if a.get("error") or a["result"].get("isError")]
        self.assertEqual(errors, [], "every call answered with the text")


if __name__ == "__main__":
    unittest.main()
