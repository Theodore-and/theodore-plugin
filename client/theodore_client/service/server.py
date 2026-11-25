"""`theodore service`: the resident service, one per user and Theodore home (DESIGN §3.1, §3.5, §4.5, §4.6; C1).

Life: started detached by the first relay or front that finds none (endpoint.ensure), it takes the home's instance
lock, listens, writes service.json, and serves. It lives while a relay is attached; when the last relay detaches it
exits after LINGER_S with nothing running; with no relay attached it exits IDLE_S after the last request. A newer
client installed beside it makes it hand over: no new calls, a successor started, every client told to reconnect, the
calls in flight finished, then it exits. Its workers die with it (job object, process group, EOF on their stdin).
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from . import endpoint, frames, router as routing, sessions as sessions_mod

# After the last relay detaches: 100 s, so that the service, its workers and what they started (a warm office, which
# takes up to 10 s to stop) have all ended 2 minutes after it (PLAN §3 A; DESIGN §3.5 "after 2 minutes").
LINGER_S = float(os.environ.get("THEODORE_SERVICE_LINGER_S") or 100.0)
IDLE_S = float(os.environ.get("THEODORE_SERVICE_IDLE_S") or 600.0)       # with no relay attached, after the last request
TICK_S = 0.5
API_WARM_WAIT_S = 1.5             # a session's first request waits this long at most for the connections opened at start
API_FRESH_S = 60.0                # a session opening later than this after them tops the kept connections up again
HANDOVER_CAP_S = 900.0             # a hand-over waits for calls in flight at most this long (a stuck call)
LOG_MAX = 1 << 20


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Client:
    """One connection: a relay (one per session), a front (one per shell command) or a probe."""

    def __init__(self, service: "Service", channel: frames.Channel):
        self.service = service
        self.channel = channel
        self.role = "probe"
        self.session_id = ""
        self.host = "unknown"
        self.name = ""
        self.version = ""
        self.plugin_root = None
        self.pid = None
        self.alive = True

    def send(self, frame: dict) -> None:
        if not self.alive:
            return
        try:
            self.channel.send(frame)
        except (OSError, ValueError, frames.FrameError):
            self.alive = False


class Service:
    def __init__(self, home: Path):
        self.home = Path(home).absolute()
        self.secret = secrets.token_hex(32)
        self.started = utc_now()
        self.started_mono = time.monotonic()
        self.version = __version__
        self.pid = os.getpid()
        self.draining = False
        self.stopping = threading.Event()
        self.exit_code = 0
        self.clients: set[Client] = set()
        self.clients_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.last_unattached_request: float | None = None
        self.relay_ever = False
        self.instance_lock = endpoint.FileLock(endpoint.run_dir(self.home) / endpoint.INSTANCE_LOCK)
        self.listener = None
        self.router = routing.Router(self)
        self.sessions = sessions_mod.Sessions(on_open=self._session_opened, on_close=self._session_closed)
        self.pool = None
        self.parts = None
        self.texts = None
        self.licence = None
        self.timers = None
        self.client_stamp = None
        self.api_warm = threading.Event()     # set once the connections opened at start are up (or after API_WARM_WAIT_S)
        self.api_warm_at = time.monotonic()

    # --- log ------------------------------------------------------------------------------------------------------

    def log(self, message: str) -> None:
        path = self.home / "logs" / "service.log"
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{self.pid}] {message.rstrip()}\n"
        with self.log_lock, contextlib.suppress(OSError):
            path.parent.mkdir(exist_ok=True)        # never the home itself: a removed home stays removed
            if path.exists() and path.stat().st_size > LOG_MAX:
                os.replace(path, path.with_suffix(".log.1"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)

    # --- start and stop ---------------------------------------------------------------------------------------------

    def start(self) -> bool:
        """Take the instance lock, listen, write service.json. False when another live service has the home."""
        if not self.instance_lock.acquire(0.0):
            return False
        from . import capture, context
        context.install_environ()
        capture.install(_LogStream(self))
        self._setup_parts()
        self.listener = endpoint.listen(self.home)
        record = {"v": 1, "pid": self.pid, "started": self.started, "protocol": frames.PROTOCOL, "version": self.version,
                  "endpoint": {"kind": self.listener.kind, "address": self.listener.address}, "secret": self.secret,
                  "home": str(self.home)}
        if self.listener.port:
            record["endpoint"]["port"] = self.listener.port
        endpoint.write_service_json(self.home, record)
        self.client_stamp = self._client_on_disk()
        self.log(f"service {self.version} listening ({self.listener.kind} {self.listener.address}) for {self.home}")
        return True

    def _setup_parts(self) -> None:
        from .. import api, core, credentials
        from . import chat, licence, parts, texts, timers, workers
        core.scope_refusals_per_thread()        # a refusal belongs to the call that learned it (core.begin_call per call)
        from .. import commands
        commands.HOSTS_EVERY_S = 300.0          # the hosts' registration and settings: every 5 minutes, not every guide
        credentials.keep_in_memory()            # read once, again when the files that change with it change
        if getattr(api, "KEPT", None) is not None and hasattr(api, "DEFAULT_POOL"):
            api.DEFAULT_POOL = api.KEPT         # every request of this process over the kept-alive connections
            self._warm_api()
        else:
            self.api_warm.set()
        chat.install(self)
        self.licence = licence.Licence(self)
        self.texts = texts.Texts(credential=self.licence.credential, require=self.licence.require_texts,
                                 notify=self._names_changed)
        use = getattr(texts, "use", None)       # commands.guide, skill and specimen run here share this one (lane D)
        if use is not None:
            use(self.texts)
        self.pool = workers.Pool(self)
        self.parts = parts.Gatherer(self.pool)      # a Studio deck's parts applied together (lanes G and F)
        self.timers = timers.Timers(self)

    def _warm_api(self) -> None:
        """api.KEPT_WARM connections to the API, opened side by side now, each with its TCP and TLS set-up done (lane
        L1, IR1's line 4: the guide calls of a lead and four designers at once each find one). `api_warm` is set once
        they are up, or after API_WARM_WAIT_S (offline, a slow network)."""
        from .. import api
        pool, count = api.KEPT, max(1, int(getattr(api, "KEPT_WARM", 1)))
        before = pool.opened
        pool.warm(count)

        def watch() -> None:
            deadline = time.monotonic() + API_WARM_WAIT_S
            while time.monotonic() < deadline and pool.opened - before < count:
                time.sleep(0.02)
            self.api_warm_at = time.monotonic()
            self.api_warm.set()
        threading.Thread(target=watch, name="service-api-warm", daemon=True).start()

    def serve_forever(self) -> int:
        threading.Thread(target=self._tick_loop, name="service-tick", daemon=True).start()
        self.timers.start()
        while not self.stopping.is_set():
            try:
                conn = self.listener.accept()
            except OSError as exc:
                if self.stopping.is_set() or self.draining:
                    break
                self.log(f"accept failed: {exc}")
                time.sleep(0.1)
                continue
            if conn is None:
                continue
            threading.Thread(target=self._serve_connection, args=(conn,), name="service-conn", daemon=True).start()
        self._shutdown()
        return self.exit_code

    def stop(self, reason: str, unlink: bool = True) -> None:
        """Stop at once (theodore stop, uninstall, the idle rule): no new calls, calls in flight end with the
        workers. `unlink` False leaves the socket path to a service that took the home over."""
        if self.stopping.is_set():
            return
        self.log(f"stopping: {reason}")
        self.draining = True
        self.stopping.set()
        with contextlib.suppress(Exception):
            self.listener.close(unlink=unlink)

    def _shutdown(self) -> None:
        endpoint.remove_service_json(self.home, self.pid)
        with contextlib.suppress(Exception):
            self.timers.stop()
        with contextlib.suppress(Exception):
            self.pool.close()
        with contextlib.suppress(Exception):
            self.texts.close()
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            with contextlib.suppress(Exception):
                client.channel.close()
        self.instance_lock.release()
        self.log("service ended")

    # --- connections ---------------------------------------------------------------------------------------------

    def _serve_connection(self, stream) -> None:
        channel = frames.Channel(stream)
        client = Client(self, channel)
        watchdog = threading.Timer(self.listener.hello_timeout, channel.close)
        watchdog.daemon = True
        watchdog.start()
        try:
            hello = channel.read()
        except (OSError, frames.FrameError, ValueError):
            hello = None
        finally:
            watchdog.cancel()
        if not self._welcome(client, hello):
            channel.close()
            return
        with self.clients_lock:
            self.clients.add(client)
        if client.role == "relay":
            self.relay_ever = True
            # A relay's session now holds the service; when the last relay detaches it lingers LINGER_S, whatever
            # shell commands came before (PLAN §3 A: nothing 2 minutes after the last relay detaches). Shell commands
            # with no relay attached after that start the IDLE_S rule again.
            self.last_unattached_request = None
            self.sessions.attach(client)
        try:
            while not self.stopping.is_set():
                try:
                    frame = channel.read()
                except (frames.FrameError, ValueError) as exc:
                    client.send({"t": "error", "code": "BAD_FRAME", "message": str(exc)[:200]})
                    break
                except OSError:
                    break
                if frame is None:
                    break
                kind = frame.get("t")
                if kind == "call":
                    problem = routing.check_call(frame)
                    if problem:
                        client.send({"t": "error", "id": frame.get("id") if isinstance(frame.get("id"), str) else None,
                                     "code": "BAD_CALL", "message": problem})
                        continue
                    self.router.submit(client, frame)
                elif kind == "cancel":
                    self.router.cancel(client, str(frame.get("id") or ""))
                elif kind == "bye":
                    break
                else:
                    client.send({"t": "error", "code": "PROTOCOL", "message": f"unexpected frame {kind!r}"})
        finally:
            client.alive = False
            with self.clients_lock:
                self.clients.discard(client)
            self.router.client_gone(client)
            if client.role == "relay":
                self.sessions.detach(client)
            channel.close()

    def _welcome(self, client: Client, hello) -> bool:
        if not isinstance(hello, dict) or hello.get("t") != "hello" or hello.get("v") != 1:
            client.send({"t": "error", "code": "PROTOCOL", "message": "the first frame is hello", "protocol": frames.PROTOCOL})
            return False
        secret = hello.get("secret")
        if not isinstance(secret, str) or not hmac.compare_digest(secret.encode("utf-8"), self.secret.encode("utf-8")):
            client.send({"t": "error", "code": "BAD_SECRET", "message": "the secret does not match this service"})
            return False
        protocol = hello.get("protocol")
        if not isinstance(protocol, int) or protocol < 1:
            client.send({"t": "error", "code": "PROTOCOL", "message": "protocol is an integer", "protocol": frames.PROTOCOL})
            return False
        if protocol > frames.PROTOCOL:
            client.send({"t": "error", "code": "PROTOCOL", "message": f"this service speaks protocol {frames.PROTOCOL}",
                         "protocol": frames.PROTOCOL})
            return False
        if self.draining:
            client.send({"t": "error", "code": "SHUTTING_DOWN", "message": "this service is stopping; connect again",
                         "retry_after_ms": 100})
            return False
        role = hello.get("role")
        client.role = role if role in ("relay", "front", "probe") else "probe"
        info = hello.get("client") if isinstance(hello.get("client"), dict) else {}
        client.name, client.version = str(info.get("name") or ""), str(info.get("version") or "")
        session = hello.get("session") if isinstance(hello.get("session"), dict) else {}
        client.session_id = str(session.get("id") or "")[:128] or f"{client.role}-{secrets.token_hex(6)}"
        client.host = str(session.get("host") or "unknown")
        client.pid = session.get("pid")
        client.plugin_root = hello.get("plugin_root") if isinstance(hello.get("plugin_root"), str) else None
        new = client.role == "relay" and self.sessions.get(client.session_id) is None
        client.send({"t": "welcome", "v": 1, "protocol": frames.PROTOCOL,
                     "service": {"version": self.version, "pid": self.pid, "started": self.started},
                     "engines": self.pool.engines_summary() if self.pool else [], "new_session": new})
        return client.alive

    # --- sessions -----------------------------------------------------------------------------------------------------

    def _session_opened(self, session) -> None:
        self.log(f"session opened ({session.host})")

        def texts_open() -> None:
            # The session's cheap first request (lane D) goes on a connection the start opened, not one of its own. A
            # session opening after a quiet while first tops the kept connections up to api.KEPT_WARM for its burst
            # (an idle connection is dropped after 4 minutes).
            self.api_warm.wait(API_WARM_WAIT_S)
            if time.monotonic() - self.api_warm_at > API_FRESH_S:
                with contextlib.suppress(Exception):
                    from .. import api
                    api.KEPT.warm(api.KEPT_WARM)
            with contextlib.suppress(Exception):
                self.texts.session_open(session.id, session.host)
        threading.Thread(target=texts_open, name="service-session-open", daemon=True).start()
        with contextlib.suppress(Exception):
            self.pool.warm_current()
        with contextlib.suppress(Exception):
            self.timers.session_opened(session)

    def _session_closed(self, session) -> None:
        self.log(f"session closed ({session.host})")
        with contextlib.suppress(Exception):
            self.texts.session_closed(session.id)
        from . import chat
        with contextlib.suppress(Exception):
            chat.session_closed(session)

    def relays_attached(self) -> int:
        return self.sessions.relays()

    def note_unattached_request(self) -> None:
        self.last_unattached_request = time.monotonic()

    def call_ended(self, call) -> None:
        if call.tool == "theodore" and call.args[:1] == ["stop"] and call.exit == 0:
            threading.Thread(target=self.stop, args=("theodore stop",), daemon=True).start()

    def _names_changed(self) -> None:
        """The index's names changed: every relay hears it, with the new names (it lists them at the host's next
        tools/list, with no second request)."""
        from . import tools
        frame = {"t": "notify", "method": "tools/list_changed", "params": {"names": tools.tool_names(self)}}
        with self.clients_lock:
            relays = [c for c in self.clients if c.role == "relay"]
        for client in relays:
            client.send(frame)

    def internal_call(self, tool: str, args: list[str]) -> int:
        """A call the service makes for itself (the daily prune): run as a client's would, its frames discarded."""
        client = Client(self, None)
        client.alive = False                # nothing is sent anywhere
        client.role, client.session_id, client.host = "probe", f"service-{self.pid}", "unknown"
        frame = {"t": "call", "id": f"internal-{secrets.token_hex(4)}", "tool": tool, "args": list(args), "cwd": str(self.home),
                 "images": False}
        call = routing.Call(self.router, client, frame)
        from . import tools
        try:
            code, _pending = tools.run(self, call)
        except Exception as exc:  # noqa: BLE001
            self.log(f"internal {tool} {args[:2]}: {exc.__class__.__name__}: {exc}")
            return 78
        self.log(f"internal {tool} {' '.join(args[:2])}: exit {code}")
        return code

    def broadcast(self, frame: dict) -> None:
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.send(frame)

    # --- the clock: progress, lifetime, updates -------------------------------------------------------------------------

    def _tick_loop(self) -> None:
        while not self.stopping.wait(TICK_S):
            try:
                self.router.tick()
                if not self.draining:
                    self._check_lifetime()
                    self._check_client_update()
            except Exception as exc:  # noqa: BLE001  the clock never stops
                self.log(f"tick: {exc.__class__.__name__}: {exc}\n{traceback.format_exc()}")

    def idle_deadline(self) -> float | None:
        """When the service exits if nothing changes, or None while a relay is attached or a call runs."""
        if self.sessions.relays() or self.router.running:
            return None
        marks = []
        if self.sessions.last_detach is not None:
            marks.append(self.sessions.last_detach + LINGER_S)
        if self.last_unattached_request is not None:
            marks.append(self.last_unattached_request + IDLE_S)
        if not marks:
            marks.append(self.started_mono + (LINGER_S if self.relay_ever else IDLE_S))
        return max(marks)

    def home_present(self) -> bool:
        return (self.home / "state").is_dir()

    def _check_lifetime(self) -> None:
        if not self.home_present():
            self.stop("its home folder is gone")
            return
        record = endpoint.read_service_json(self.home)
        if record is not None and record.get("pid") != self.pid and endpoint.pid_alive(record.get("pid")):
            self.stop("another service took this home over", unlink=False)
            return
        deadline = self.idle_deadline()
        if deadline is not None and time.monotonic() >= deadline:
            self.stop("idle")

    def _client_on_disk(self) -> tuple | None:
        """(version, folder) of the client the home names as current, or None (a source checkout, a test)."""
        return endpoint.installed_client(self.home)

    def _check_client_update(self) -> None:
        on_disk = self._client_on_disk()
        if on_disk is None or on_disk == self.client_stamp:
            return
        self.client_stamp = on_disk
        if on_disk[0] == self.version:
            return
        _command, successor = endpoint.service_command(self.home)
        if successor != on_disk[0]:             # nothing newer can be started yet (no runtime named): keep serving
            self.log(f"client {on_disk[0]} installed; no successor can start yet ({successor})")
            return
        threading.Thread(target=self.hand_over, args=(f"client {on_disk[0]} installed",), name="hand-over",
                         daemon=True).start()

    def hand_over(self, reason: str) -> None:
        """C1 "Updates": stop taking calls, start the successor, tell every client to reconnect, finish the calls in
        flight, exit."""
        if self.draining:
            return
        self.draining = True
        self.log(f"hand-over: {reason}")
        self.instance_lock.release()
        with contextlib.suppress(Exception):
            self.listener.close(unlink=False)        # the successor binds the same socket path itself
        endpoint.remove_service_json(self.home, self.pid)
        if self.sessions.relays():
            try:
                endpoint.ensure(self.home, start=True)[1].close()
            except Exception as exc:  # noqa: BLE001  the clients start one themselves when they reconnect
                self.log(f"hand-over: successor not started ({exc})")
        self.broadcast({"t": "notify", "method": "theodore/reconnect", "params": {"reason": "update"}})
        deadline = time.monotonic() + HANDOVER_CAP_S
        while self.router.running and time.monotonic() < deadline:
            time.sleep(0.05)
        self.stopping.set()


class _LogStream:
    """What the service prints outside any call goes to its log."""

    def __init__(self, service: Service):
        self._service = service
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._service.log(line)
        return len(text)

    def flush(self) -> None:
        pass


def _on_signals(service: Service) -> None:
    """SIGTERM, SIGINT and SIGHUP stop the service as `theodore stop` does: workers ended, service.json removed."""
    import signal
    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            with contextlib.suppress(OSError, ValueError):
                signal.signal(getattr(signal, name), lambda _n, _f: service.stop(f"signal {_n}"))


def main(argv: list[str]) -> int:
    """`theodore service --home <home>`."""
    home = None
    if "--home" in argv[:-1]:
        home = argv[argv.index("--home") + 1]
    home_path = Path(home or os.environ.get("THEODORE_HOME") or Path.home() / ".theodore").absolute()
    os.environ["THEODORE_HOME"] = str(home_path)
    service = Service(home_path)
    _on_signals(service)
    try:
        if not service.start():
            return 0                    # another service serves this home: nothing to do
    except Exception as exc:  # noqa: BLE001
        service.log(f"service could not start: {exc.__class__.__name__}: {exc}\n{traceback.format_exc()}")
        service.instance_lock.release()
        return 1
    code = 0
    try:
        code = service.serve_forever()
    except KeyboardInterrupt:
        code = 0
    except Exception as exc:  # noqa: BLE001
        service.log(f"service failed: {exc.__class__.__name__}: {exc}\n{traceback.format_exc()}")
        code = 1
    # Everything is written and every worker ended: leave at once. The interpreter's own shutdown would wait on, or
    # abort over, a daemon thread still inside a read (a worker that has not finished dying).
    with contextlib.suppress(Exception):
        sys.stdout.flush()
    os._exit(code)


__all__ = ["Service", "main", "json"]
