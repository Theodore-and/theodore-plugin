"""The client's connection to its hosts when one of their addresses does not answer (moved out of test_client.py,
which had reached the host limit of 256 KiB a file)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import urllib.request as _urlreq
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from test_client import _start_proxy


class EveryAddress(unittest.TestCase):
    """RC9, P039: from some networks one of the addresses Theodore's hosts resolve to does not answer, or takes the TCP
    connection and never the TLS handshake, while another works. The client races the addresses (the next one starts
    0.25 s after the one before, or at once when it failed; 3 s each to connect and finish the handshake), remembers
    the one that worked in this process and, in state/addresses.json, for the next ones, leaves the choice to a proxy
    when there is one, and verifies the certificate for the host name, never the address."""

    HOST = "dl.theodore.test"

    def setUp(self):
        import socket
        import ssl
        import tempfile
        from support import AddressBench
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
        from theodore_client import api
        self.api, self.socket = api, socket
        folder = Path(tempfile.mkdtemp(prefix="theodore-addr-"))
        self.addCleanup(shutil.rmtree, folder, True)
        self.bench = AddressBench(folder, self.HOST)
        self.addCleanup(self.bench.close)
        self.enterContext(mock.patch.object(api, "OWN_BASES", [f"https://{self.HOST}"], create=True))
        self.enterContext(mock.patch.object(api, "_ssl_context", lambda: ssl.create_default_context(cafile=self.bench.cert)))
        self.enterContext(mock.patch.object(api, "_OPENERS", {}))
        self.enterContext(mock.patch.object(api, "_GOOD_ADDRESS", {}, create=True))
        self.folder = folder
        self.kept = folder / "home" / "state" / "addresses.json"
        self.real_address_file = api._address_file
        self.enterContext(mock.patch.object(api, "_address_file", lambda: self.kept))
        self.enterContext(mock.patch.dict(os.environ, {k: "" for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                                                                      "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")}))
        self.enterContext(mock.patch.object(_urlreq, "getproxies", lambda: {}))
        self.addresses: list[int] = []          # the ports the fake resolver answers for HOST, in its order
        self.resolved: list[str] = []
        self.connects: list[tuple] = []
        real_getaddrinfo, real_socket = socket.getaddrinfo, socket.socket
        test = self

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if str(host).endswith(".theodore.test"):
                test.resolved.append(host)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p)) for p in test.addresses]
            return real_getaddrinfo(host, port, *args, **kwargs)

        class Recording(real_socket):
            def connect(self, address):
                test.connects.append(address)
                return super().connect(address)

        self.enterContext(mock.patch.object(socket, "getaddrinfo", fake_getaddrinfo))
        self.enterContext(mock.patch.object(socket, "socket", Recording))

    def get(self, host: str | None = None, timeout: float = 20.0):
        began = time.monotonic()
        status, _headers, body = self.api.request("GET", f"https://{host or self.HOST}:{self.bench.live}/x", timeout=timeout)
        return status, body, time.monotonic() - began

    def within(self, promise: float, regression: float, baseline: float) -> float:
        """The longest a request may take: what the client promises, plus half the way to what a regression would cost
        (no race: the hanging address's whole CONNECT_EACH), plus twice `baseline`, a request straight to the live
        address measured now (slow on a loaded machine). Fixed bounds of 0.75 to 2.5 s failed the same way as the
        installer's under load."""
        return promise + (regression - promise) / 2 + 2 * baseline

    def baseline(self) -> float:
        """A request to the only, live address, the shorter of two; nothing it remembers is left behind."""
        good, kept, addresses = dict(self.api._GOOD_ADDRESS), self.kept.read_bytes() if self.kept.exists() else None, self.addresses
        self.addresses = [self.bench.live]
        took = min(self.get()[2] for _ in range(2))
        self.api._GOOD_ADDRESS.clear()
        self.api._GOOD_ADDRESS.update(good)
        self.kept.write_bytes(kept) if kept is not None else self.kept.unlink(missing_ok=True)
        self.addresses = addresses
        self.connects.clear()
        return took

    def test_a_dead_first_address_falls_over_to_the_next_and_is_remembered(self):
        bound = self.within(self.api.STAGGER, self.api.CONNECT_EACH, self.baseline())
        for dead in ("silent", "no_accept", "refused"):     # the handshake that hangs: the case a user met
            with self.subTest(dead=dead):
                self.api._GOOD_ADDRESS.clear()
                self.kept.unlink(missing_ok=True)        # a new computer: nothing remembered
                self.connects.clear()
                port = getattr(self.bench, dead)
                self.addresses = [port, self.bench.live]
                status, body, took = self.get()
                self.assertEqual((status, body), (200, b"ok"))
                self.assertLess(took, bound, f"{dead}: {took:.1f} s")      # the next address started 0.25 s later
                self.assertEqual([a[1] for a in self.connects], [port, self.bench.live])
                self.assertEqual(self.api._GOOD_ADDRESS[(self.HOST, self.bench.live)], ("127.0.0.1", self.bench.live))
                # the next request starts with the address that worked
                self.connects.clear()
                silent_before = self.bench.accepted["silent"]
                status, body, took = self.get()
                self.assertEqual((status, body), (200, b"ok"))
                self.assertEqual([a[1] for a in self.connects], [self.bench.live])
                self.assertLess(took, bound)
                self.assertEqual(self.bench.accepted["silent"], silent_before)

    DRIVER = r"""
import json, os, socket, ssl, sys, time
sys.path.insert(0, sys.argv[1])
from theodore_client import api
host, ports, cafile, method = sys.argv[2], [int(p) for p in sys.argv[3].split(",")], sys.argv[4], sys.argv[5]
api.OWN_BASES.append("https://" + host)
api._ssl_context = lambda: ssl.create_default_context(cafile=cafile)
real = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, *a, **k: ([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", q)) for q in ports]
                                            if h == host else real(h, p, *a, **k))
connects, Base = [], socket.socket
class Recording(Base):
    def connect(self, address):
        connects.append(address[1])
        return super().connect(address)
socket.socket = Recording
began = time.monotonic()
try:
    if method == "usage":       # the usage send: 2 s in all, no proxy allowance
        status, _h, body = api.request("POST", f"https://{host}:{ports[-1]}/v1/usage", body=b"{}", timeout=2.0, exact_timeout=True)
    else:
        status, _h, body = api.request("GET", f"https://{host}:{ports[-1]}/x", timeout=20.0)
    out = {"status": status}
except Exception as exc:
    out = {"error": str(exc)}
out.update(took=time.monotonic() - began, connects=connects)
print(json.dumps(out))
"""

    def other_process(self, home: Path, method: str = "get") -> dict:
        """One request from a separate Python process with its own Theodore folder: nothing shared but the folder."""
        env = {**os.environ, "THEODORE_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(key, None)
        env["NO_PROXY"] = env["no_proxy"] = "*"
        done = subprocess.run([sys.executable, "-I", "-c", self.DRIVER, str(Path(__file__).resolve().parents[1] / "client"), self.HOST,
                               ",".join(str(p) for p in self.addresses), self.bench.cert, method],
                              capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def process_baseline(self, method: str = "get") -> float:
        """`baseline` for other_process: the live address alone, from a Theodore folder of its own, the shorter of two."""
        addresses, self.addresses = self.addresses, [self.bench.live]
        took = []
        for n in range(2):
            home = self.folder / f"baseline-{method}-{n}"
            (home / "state").mkdir(parents=True)
            took.append(self.other_process(home, method)["took"])
        self.addresses = addresses
        return min(took)

    def test_a_hanging_first_address_costs_once_across_processes(self):
        # the owner's carrier: of the two addresses one takes the connection and never the handshake
        home = self.folder / "proc-home"
        (home / "state").mkdir(parents=True)
        self.addresses = [self.bench.silent, self.bench.live]
        bound = self.within(self.api.STAGGER, self.api.CONNECT_EACH, self.process_baseline())
        first = self.other_process(home)
        self.assertEqual(first.get("status"), 200, first)
        self.assertEqual(first["connects"], [self.bench.silent, self.bench.live])
        self.assertLess(first["took"], bound)
        silent_before = self.bench.accepted["silent"]
        for method in ("get", "usage"):     # every later process, the usage send among them, goes straight to the one that works
            with self.subTest(method=method):
                later = self.other_process(home, method)
                self.assertEqual(later.get("status"), 200, later)
                self.assertEqual(later["connects"], [self.bench.live])     # never the hanging one again
                self.assertLess(later["took"], bound)
        self.assertEqual(self.bench.accepted["silent"], silent_before)

    def test_the_usage_send_gets_through_a_hanging_first_address_on_a_new_computer(self):
        # nothing remembered yet: within the send's 2 s the second address is reached 0.25 s after the first
        home = self.folder / "new-home"
        (home / "state").mkdir(parents=True)
        self.addresses = [self.bench.silent, self.bench.live]
        bound = self.within(self.api.STAGGER, 2.0, self.process_baseline("usage"))      # past its 2 s the send has failed
        sent = self.other_process(home, "usage")
        self.assertEqual(sent.get("status"), 200, sent)
        self.assertLess(sent["took"], bound)

    def test_every_address_dead_says_so(self):
        self.addresses = [self.bench.refused, self.bench.silent]
        with self.assertRaises(self.api.NetworkError) as caught:
            self.get()
        self.assertEqual(caught.exception.cause, "NETWORK")
        self.assertIn(f"all 2 addresses of {self.HOST} failed", caught.exception.detail)
        self.assertIn(f"127.0.0.1: ", caught.exception.detail)

    def test_the_request_timeout_still_bounds_the_whole_connection(self):
        baseline = self.baseline()
        self.addresses = [self.bench.silent, self.bench.no_accept]
        began = time.monotonic()
        with self.assertRaises(self.api.NetworkError) as caught:
            self.get(timeout=1.0)
        # the request's 1 s and the race's last 0.25 s wait; each address's own 3 s would be the regression
        self.assertLess(time.monotonic() - began, self.within(1.0 + 0.25, self.api.CONNECT_EACH, baseline))
        self.assertIn("addresses of", caught.exception.detail)
        # a third address that works is reached within the same second: the dead ones do not use it up
        self.addresses = [self.bench.silent, self.bench.no_accept, self.bench.live]
        status, body, took = self.get(timeout=1.0)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertLess(took, self.within(2 * self.api.STAGGER, 1.0 + 0.25, baseline))     # it started 0.5 s in

    def test_the_address_that_worked_is_kept_for_the_next_process(self):
        self.addresses = [self.bench.silent, self.bench.live]
        self.assertEqual(self.get()[:2], (200, b"ok"))
        kept = json.loads(self.kept.read_text())
        self.assertEqual(kept[f"{self.HOST}:{self.bench.live}"]["address"], "127.0.0.1")
        self.assertEqual(kept[f"{self.HOST}:{self.bench.live}"]["port"], self.bench.live)
        # a new process (nothing in memory) starts with it
        self.api._GOOD_ADDRESS.clear()
        self.connects.clear()
        self.assertEqual(self.get()[:2], (200, b"ok"))
        self.assertEqual([a[1] for a in self.connects], [self.bench.live])

    def test_a_kept_address_that_stopped_working_is_replaced_and_an_old_one_is_not_used(self):
        key = f"{self.HOST}:{self.bench.live}"
        self.kept.parent.mkdir(parents=True, exist_ok=True)
        bound = self.within(self.api.STAGGER, self.api.CONNECT_EACH, self.baseline())
        for at, expect_first in ((time.time() - 60, self.bench.silent), (time.time() - self.api.ADDRESS_TTL - 60, self.bench.live)):
            with self.subTest(age=time.time() - at):
                self.kept.write_text(json.dumps({key: {"address": "127.0.0.1", "port": self.bench.silent, "at": int(at)}}))
                self.api._GOOD_ADDRESS.clear()
                self.connects.clear()
                self.addresses = [self.bench.live, self.bench.silent]
                status, body, took = self.get()
                self.assertEqual((status, body), (200, b"ok"))
                self.assertEqual(self.connects[0][1], expect_first)
                self.assertLess(took, bound)
                self.assertEqual(json.loads(self.kept.read_text())[key]["port"], self.bench.live)

    def test_nothing_is_kept_without_a_theodore_folder(self):
        # an install's first steps and an uninstall: no folder is created for it
        self.enterContext(mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.folder / "absent")}))
        self.enterContext(mock.patch.object(self.api.core, "_HOME", None))
        self.assertIsNone(self.real_address_file())
        (self.folder / "absent").mkdir()
        self.assertEqual(self.real_address_file(), self.folder / "absent" / "state" / "addresses.json")
        (self.folder / "absent" / ".theodore-uninstalling").write_text("1")
        self.assertIsNone(self.real_address_file())

    def test_a_slow_link_still_gets_the_request_s_own_time(self):
        # every address only ran out of its 3 s: not a dead address but a slow link, which the request's own 20 s serve
        self.addresses = [self.bench.slow]
        status, body, took = self.get()
        self.assertEqual((status, body), (200, b"ok"))
        self.assertGreater(took, self.bench.SLOW_HANDSHAKE)
        self.assertEqual([a[1] for a in self.connects], [self.bench.slow, self.bench.slow])

    def test_the_certificate_is_checked_for_the_host_name(self):
        import ssl
        # the address serves a certificate for dl.theodore.test: asked for another name, it is refused, and the
        # refusal is not taken for a dead address (no other address is tried)
        self.addresses = [self.bench.live, self.bench.refused]
        with self.assertRaises(self.api.NetworkError) as caught:
            self.get(host="api.theodore.test")
        self.assertEqual(caught.exception.cause, "TLS_INTERCEPT")
        self.assertEqual(len(self.connects), 1)
        self.assertEqual(self.api._GOOD_ADDRESS, {})
        # the name is what goes to the handshake (SNI and verification), never the address
        names = []
        real_wrap = ssl.SSLContext.wrap_socket

        def wrap(context, sock, *args, **kwargs):
            if not kwargs.get("server_side"):         # the test server wraps its own side too
                names.append((kwargs.get("server_hostname"), context.check_hostname, context.verify_mode))
            return real_wrap(context, sock, *args, **kwargs)
        self.addresses = [self.bench.refused, self.bench.live]
        with mock.patch.object(ssl.SSLContext, "wrap_socket", wrap):
            self.assertEqual(self.get()[:2], (200, b"ok"))
        self.assertEqual(names, [(self.HOST, True, ssl.CERT_REQUIRED)])

    def test_with_a_proxy_the_proxy_chooses_the_address(self):
        proxy, url, seen = _start_proxy(refuse=True)
        self.addCleanup(proxy.shutdown)
        self.addresses = [self.bench.refused, self.bench.live]
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": url, "https_proxy": url}), \
                mock.patch.object(_urlreq, "getproxies", lambda: {"https": url}):
            with self.assertRaises(self.api.NetworkError) as caught:
                self.get()
        self.assertIn("403", caught.exception.detail)
        self.assertEqual(seen, [f"CONNECT {self.HOST}:{self.bench.live}"])
        self.assertEqual(self.resolved, [])
        self.assertEqual([a[1] for a in self.connects], [proxy.server_address[1]])

    def test_a_third_party_host_is_reached_as_before(self):
        # Theodore's hosts are the bases it was set up for and the other names of their domain (the staging forms)
        self.assertFalse(self.api._own_host("download.documentfoundation.org"))
        self.assertTrue(self.api._own_host(self.HOST))
        with mock.patch.object(self.api, "OWN_BASES", ["https://api.own-service.test", "http://127.0.0.1:8080"]):
            for host in ("own-service.test", "api.own-service.test", "dl.own-service.test", "staging-api.own-service.test",
                         "DL.own-service.test.", "127.0.0.1"):
                self.assertTrue(self.api._own_host(host), host)
            for host in ("own-service.test.example.com", "evilown-service.test", "127.0.0.2", "1.0.0.1"):
                self.assertFalse(self.api._own_host(host), host)
