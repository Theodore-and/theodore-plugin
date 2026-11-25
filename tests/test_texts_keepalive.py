"""The kept-alive pool (api.KeptAlive). Texts and sheets are served online and authorised on every call (contract C6,
24 November 2026), so what a call must not pay is a new TCP and TLS set-up: the pool keeps connections to the API open
between calls, gives concurrent calls a connection each, and answers exactly as api.request() does."""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
import support  # noqa: E402,F401  (puts the client on the path)
from fake_api import FakeApi  # noqa: E402
from theodore_client import api, core  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"sheet bytes " * 50 + b"\xff\xd9"
NO_PROXY = {k: "" for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")}


class IdleClosingServer:
    """HTTP/1.1, kept alive, closing a connection after `idle` seconds without a request (an edge's idle timeout). It
    counts the connections it accepted and the requests each one delivered."""

    def __init__(self, idle: float = 0.2):
        import http.server
        owner = self
        self.connections = 0
        self.requests: list[tuple[int, str]] = []
        self.lock = threading.Lock()

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = idle

            def setup(self):
                super().setup()
                with owner.lock:
                    owner.connections += 1
                    self.number = owner.connections

            def answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                with owner.lock:
                    owner.requests.append((self.number, self.command))
                body = b'{"ok": true, "keys": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = answer

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class IdleTimeout(unittest.TestCase):
    """Lane A's A4 on the rig (25 Nov): two warm `theodore guide` starts in 180 were told OFFLINE, \"Remote end closed
    connection without response\". The service's kept connections had been closed by the remote end while idle; the one
    retry took another idle one, closed too. Now a connection closed while idle is never used, and a connection lost as
    it is used is tried once more on a new one; only a failure on that new one is a network failure."""

    def setUp(self):
        self.server = IdleClosingServer(idle=0.2)
        self.addCleanup(self.server.stop)
        self.enterContext(mock.patch.object(core, "_CONFIG", {"api_base": self.server.base}))
        self.enterContext(mock.patch.dict(os.environ, NO_PROXY))
        self.pool = api.KeptAlive()
        self.addCleanup(self.pool.close)
        self.url = self.server.base + "/v1/keys"

    def fill(self, count: int) -> None:
        start, done = threading.Barrier(count), []

        def one():
            start.wait()
            done.append(self.pool.request("GET", self.url)[0])
        threads = [threading.Thread(target=one) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(done, [200] * count)

    def test_every_method_after_the_idle_timeout_goes_on_a_new_connection(self):
        self.fill(1)
        time.sleep(0.6)                         # the server closed it
        for method in ("POST", "GET"):
            self.assertEqual(self.pool.request(method, self.url, body={} if method == "POST" else None)[0], 200)
            time.sleep(0.6)
        self.assertEqual([m for _n, m in self.server.requests], ["GET", "POST", "GET"], "each delivered once")
        self.assertEqual(self.server.connections, 3)

    def test_two_stale_idle_connections_give_one_fresh_connect_and_one_delivered_request(self):
        self.fill(2)
        self.assertEqual(len(self.pool._idle), 2)
        time.sleep(0.6)                         # both closed by the server
        before = self.server.connections
        self.assertEqual(self.pool.request("POST", self.url, body={})[0], 200)
        self.assertEqual(self.server.connections - before, 1, "one fresh connect")
        self.assertEqual([m for _n, m in self.server.requests].count("POST"), 1, "exactly one delivered request")
        self.assertEqual(len(self.pool._idle), 1, "the stale ones are gone; the fresh one is kept")

    def test_lost_as_it_is_used_the_retry_takes_a_fresh_connection_never_another_idle_one(self):
        self.fill(2)
        time.sleep(0.6)
        before = self.server.connections
        with mock.patch.object(api, "_peer_closed", return_value=False):    # both looked open: the race at use
            self.assertEqual(self.pool.request("GET", self.url)[0], 200)
        self.assertEqual(self.server.connections - before, 1, "the retry opened one new connection")
        self.assertEqual(len(self.pool._idle), 1, "the other stale one was dropped, not tried")

    def test_a_failure_on_the_fresh_connection_is_a_network_failure(self):
        self.fill(1)
        time.sleep(0.6)
        self.server.stop()
        with self.assertRaises(api.NetworkError):
            self.pool.request("GET", self.url, timeout=2.0)


class KeptAliveCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeApi()
        self.accepted: list[socket.socket] = []
        accept = self.fake.server.get_request

        def counting():
            connection, address = accept()
            self.accepted.append(connection)
            return connection, address
        self.fake.server.get_request = counting
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.enterContext(mock.patch.object(core, "_CONFIG", {"api_base": self.fake.base}))
        self.enterContext(mock.patch.dict(os.environ, NO_PROXY))
        self.pool = api.KeptAlive()
        self.addCleanup(self.pool.close)
        self.keys = self.fake.base + "/v1/keys"

    def end_server_connections(self) -> None:
        """The server (or a proxy) ends every connection it holds, as an idle timeout does."""
        for connection in self.accepted:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def signed(self) -> str:
        self.fake.installations["iid-1"] = {"secret": "secret-1", "revoked": False, "gen": 1, "meta": {}}
        return "secret-1"

    def test_sequential_calls_share_one_connection(self):
        for _ in range(6):
            status, _headers, raw = self.pool.request("GET", self.keys)
            self.assertEqual(status, 200)
            self.assertIn(b"keys", raw)
        self.assertEqual(self.pool.opened, 1)
        self.assertEqual(len(self.accepted), 1)

    def test_concurrent_calls_all_succeed_and_what_is_kept_is_reused(self):
        results, errors = [], []
        start = threading.Barrier(6)

        def one():
            start.wait()
            try:
                results.append(self.pool.request("GET", self.keys)[0])
            except Exception as exc:        # noqa: BLE001  reported below
                errors.append(exc)
        threads = [threading.Thread(target=one) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(results, [200] * 6)
        self.assertLessEqual(len(self.pool._idle), api.KEPT_IDLE_MAX)
        opened = self.pool.opened
        for _ in range(api.KEPT_IDLE_MAX):
            self.assertEqual(self.pool.request("GET", self.keys)[0], 200)
        self.assertEqual(self.pool.opened, opened)

    def test_a_connection_the_server_closed_is_opened_again_once(self):
        self.assertEqual(self.pool.request("GET", self.keys)[0], 200)
        self.end_server_connections()
        self.assertEqual(self.pool.request("GET", self.keys)[0], 200)
        self.assertEqual(self.pool.opened, 2)

    def test_a_post_on_a_connection_closed_as_it_is_used_goes_twice_only_when_the_caller_says_it_may(self):
        # Closed just as it is used: it looked open (so it was taken), and the request was lost on it. Whether a one-use
        # action happened cannot be told, so a POST goes again only when its caller says it may (the backend's rule).
        self.assertEqual(self.pool.request("POST", self.keys, body={})[0], 200)
        self.end_server_connections()
        with mock.patch.object(api, "_peer_closed", return_value=False), self.assertRaises(api.NetworkError):
            self.pool.request("POST", self.keys, body={})
        self.assertEqual(self.pool.request("POST", self.keys, body={})[0], 200)     # a new connection
        self.end_server_connections()
        with mock.patch.object(api, "_peer_closed", return_value=False):
            self.assertEqual(self.pool.request("POST", self.keys, body={}, again=True)[0], 200)

    def test_a_connection_closed_while_idle_is_never_sent_a_request(self):
        # An edge's idle timeout closed it: seen before anything is sent, so any method, a POST too, goes on a new one.
        self.assertEqual(self.pool.request("POST", self.keys, body={})[0], 200)
        self.end_server_connections()
        self.assertEqual(self.pool.request("POST", self.keys, body={})[0], 200)
        self.assertEqual(self.pool.opened, 2)

    def test_a_new_connection_that_fails_is_a_network_error_at_once(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        dead = f"http://127.0.0.1:{port}"
        with mock.patch.object(core, "_CONFIG", {"api_base": dead}):
            with self.assertRaises(api.NetworkError):
                self.pool.request("GET", dead + "/v1/keys", timeout=2.0)
        self.assertEqual(self.pool.opened, 0)

    def test_304_and_an_error_answer_keep_the_connection(self):
        secret = self.signed()
        self.fake.guide_index = {"version": "g1", "sections": [{"name": "critique"}], "specimens": [{"file": "a.jpg"}]}
        url = self.fake.base + "/v1/guide/deck-design?form=index"
        status, headers, _raw = self.pool.request("GET", url, bearer=secret)
        self.assertEqual(status, 200)
        status, _headers, raw = self.pool.request("GET", url, bearer=secret, headers={"If-None-Match": headers.get("ETag")})
        self.assertEqual((status, raw), (304, b""))
        with self.assertRaises(api.ApiError) as caught:
            self.pool.request("GET", self.fake.base + "/v1/guide/nothing", bearer=secret, learn_refusals=False)
        self.assertEqual((caught.exception.http_status, caught.exception.code), (403, "entitlement_inactive"))
        self.assertEqual(self.pool.request("GET", self.keys)[0], 200)
        self.assertEqual(self.pool.opened, 1)
        self.assertEqual(self.fake.requests[0]["auth"], True)

    def test_an_answer_past_the_limit_is_refused_and_its_connection_closed(self):
        with self.assertRaises(api.TooLarge):
            self.pool.request("GET", self.keys, limit=10)
        self.assertEqual(self.pool.request("GET", self.keys)[0], 200)
        self.assertEqual(self.pool.opened, 2)

    def test_a_redirect_is_answered_by_request_with_its_rules(self):
        secret = self.signed()
        self.fake.specimens["a.jpg"] = JPEG
        self.fake.specimen_redirect = self.fake.base + "/elsewhere"
        with self.assertRaises(api.NetworkError) as caught:
            self.pool.request("GET", self.fake.base + "/v1/guide/specimens/a.jpg?skill=deck-design", bearer=secret, redirects=False)
        self.assertIn("redirect", caught.exception.detail)

    def test_the_bearer_goes_only_to_the_api_origin(self):
        other = FakeApi().start()
        self.addCleanup(other.stop)
        self.pool.request("GET", other.base + "/v1/keys", bearer="secret-1")       # another origin: request(), no bearer
        self.assertEqual(other.requests[-1]["auth"], False)
        self.assertEqual(self.pool.opened, 0)

    def test_warm_opens_one_connection_in_the_background(self):
        self.pool.warm()
        for _ in range(200):
            if self.pool._idle:
                break
            threading.Event().wait(0.01)
        self.assertEqual(self.pool.opened, 1)
        self.assertEqual(self.pool.request("GET", self.keys)[0], 200)
        self.assertEqual(self.pool.opened, 1)

    def test_warm_opens_as_many_as_asked_side_by_side(self):
        """The tool server opens KEPT_WARM at its start, so the calls of a lead and its designers at once each find one."""
        self.pool.warm(3)
        for _ in range(300):
            if len(self.pool._idle) >= 3:
                break
            threading.Event().wait(0.01)
        self.assertEqual(self.pool.opened, 3)
        self.pool.warm(3)                               # three are idle: nothing more
        threading.Event().wait(0.2)
        self.assertEqual(self.pool.opened, 3)
        start, results = threading.Barrier(3), []

        def one():
            start.wait()
            results.append(self.pool.request("GET", self.keys)[0])
        threads = [threading.Thread(target=one) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual((results, self.pool.opened), ([200] * 3, 3), "three at once, on the three kept")

    def test_each_tls_connection_has_its_own_context(self):
        """truststore holds a lock through each handshake and certificate check of one context: connections opened at
        once on a shared one were set up one after another (about 150 ms apart on the Windows test machine)."""
        made, used = [], []

        def context():
            made.append(object())
            return made[-1]

        class Connection:
            def __init__(self, host, port, context=None, timeout=None):
                used.append(context)

            def connect(self):
                pass

            def close(self):
                pass
        route = ("https", "api.example", 443, None)
        with mock.patch.object(api, "_ssl_context", context), mock.patch.object(api, "_AnyAddressHTTPS", Connection):
            threads = [threading.Thread(target=self.pool._open, args=(route, 5.0)) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(len(used), 4)
        self.assertEqual(len({id(c) for c in used}), 4, "one context per connection")

    def test_call_takes_the_pool(self):
        self.signed()
        self.fake.guides["deck-design"]["critique"] = "# Critique\n"
        for _ in range(3):
            status, _headers, raw = api.call("guide_section", bearer="secret-1", skill="deck-design", section="critique",
                                             query={"form": "whole"}, pool=self.pool)
            self.assertEqual((status, raw), (200, b"# Critique\n"))
        self.assertEqual(self.pool.opened, 1)


if __name__ == "__main__":
    unittest.main()
