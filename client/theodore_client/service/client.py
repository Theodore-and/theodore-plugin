"""The client end of C1, for the Python relay (`theodore mcp`) and the Python front (`theodore pptx ...` in a shell).

`ServiceClient.connect()` finds the running service or starts it (endpoint.ensure), sends `hello` and reads `welcome`.
`send_call(frame)` sends a call; frames for it come back to `on_frame(frame)` on the reader thread. When the service
says `theodore/reconnect` (an update), new calls go to a new connection at once while the old one finishes the calls it
holds; when a connection ends with calls unanswered (a crash), they are sent again with `resend: true` once a service
answers (backoff 50, 100, 200, 400, 800 ms, then every 800 ms up to 3 s, then the start rule).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path

from .. import __version__
from . import endpoint, frames

BACKOFF_S = (0.05, 0.1, 0.2, 0.4, 0.8)
RECONNECT_FOR_S = 3.0


class Connection:
    def __init__(self, record: dict, stream):
        self.record = record
        self.channel = frames.Channel(stream)
        self.calls: set[str] = set()         # ids sent on this connection and not answered yet
        self.draining = False
        self.welcome: dict | None = None


class ServiceClient:
    def __init__(self, home: Path, *, role: str, name: str, session_id: str, host: str, plugin_root: str | None = None,
                 on_frame=None, on_notify=None, log=None, cwd: str | None = None):
        self.home = Path(home)
        self.role = role
        self.name = name
        self.session_id = session_id
        self.host = host
        self.plugin_root = plugin_root
        self.cwd = cwd                      # the session's working folder (a relay's): the service primes it (C1)
        self.on_frame = on_frame or (lambda frame: None)
        self.on_notify = on_notify or (lambda frame: None)
        self.log = log or (lambda message: None)
        self._lock = threading.RLock()
        self._current: Connection | None = None
        self._calls: dict[str, dict] = {}          # id -> the call frame, while unanswered
        self._closing = False
        self._reconnecting = False

    # --- connections -------------------------------------------------------------------------------------------------

    def connect(self, start: bool = True, wait: float | None = None) -> Connection:
        """The current connection, or a new one (starting the service when `start`). Raises endpoint.Unreachable."""
        with self._lock:
            if self._current is not None and not self._current.draining and not self._current.channel.closed:
                return self._current
        record, stream = endpoint.ensure(self.home, start=start, wait=wait)
        conn = Connection(record, stream)
        hello = {"t": "hello", "v": 1, "secret": record["secret"], "role": self.role, "protocol": frames.PROTOCOL,
                 "client": {"name": self.name, "version": __version__},
                 "session": {"id": self.session_id, "host": self.host, "pid": os.getpid(),
                             **({"cwd": self.cwd} if self.cwd and os.path.isabs(self.cwd) else {})}}
        if self.plugin_root:
            hello["plugin_root"] = self.plugin_root
        try:
            conn.channel.send(hello)
            welcome = conn.channel.read()
        except (OSError, frames.FrameError, ValueError) as exc:
            conn.channel.close()
            raise endpoint.Unreachable(f"no welcome ({exc})") from None
        if not welcome or welcome.get("t") != "welcome":
            conn.channel.close()
            raise endpoint.Unreachable(f"the service answered {str((welcome or {}).get('code') or welcome)[:200]}")
        conn.welcome = welcome
        with self._lock:
            self._current = conn
        threading.Thread(target=self._read_loop, args=(conn,), name="service-client-read", daemon=True).start()
        return conn

    def welcome(self) -> dict | None:
        with self._lock:
            return self._current.welcome if self._current is not None else None

    def close(self) -> None:
        with self._lock:
            self._closing = True
            conn, self._current = self._current, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.channel.send({"t": "bye"})
            conn.channel.close()

    # --- calls ---------------------------------------------------------------------------------------------------------

    def send_call(self, frame: dict, start: bool = True) -> None:
        """Send one call (connecting, or starting the service, when needed). Raises endpoint.Unreachable."""
        conn = self.connect(start=start)
        with self._lock:
            self._calls[frame["id"]] = frame
            conn.calls.add(frame["id"])
        try:
            conn.channel.send(frame)
        except (OSError, frames.FrameError, ValueError):
            with self._lock:
                conn.calls.discard(frame["id"])
            conn.channel.close()
            self._resend_after_loss()

    def cancel(self, call_id: str) -> None:
        with self._lock:
            conn = next((c for c in (self._current,) if c is not None and call_id in c.calls), self._current)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.channel.send({"t": "cancel", "id": call_id})

    def forget(self, call_id: str) -> None:
        with self._lock:
            self._calls.pop(call_id, None)

    def pending(self) -> int:
        with self._lock:
            return len(self._calls)

    # --- frames ---------------------------------------------------------------------------------------------------------

    def _read_loop(self, conn: Connection) -> None:
        try:
            while True:
                frame = conn.channel.read()
                if frame is None:
                    break
                kind = frame.get("t")
                if kind == "notify":
                    if frame.get("method") == "theodore/reconnect":
                        self._handover(conn)
                    else:
                        self.on_notify(frame)
                    continue
                if kind == "error" and frame.get("code") == "SHUTTING_DOWN" and frame.get("id"):
                    self._retry_elsewhere(conn, frame["id"])
                    continue
                if kind in ("done", "error") and frame.get("id"):
                    with self._lock:
                        conn.calls.discard(frame["id"])
                        if kind == "done" or frame.get("code") in ("BAD_CALL", "UNKNOWN_TOOL"):
                            self._calls.pop(frame["id"], None)
                self.on_frame(frame)
        except (OSError, frames.FrameError, ValueError) as exc:
            self.log(f"service connection ended: {exc}")
        finally:
            conn.channel.close()
            with self._lock:
                if self._current is conn:
                    self._current = None
                lost = [self._calls[i] for i in conn.calls if i in self._calls]
                conn.calls.clear()
            if lost and not self._closing:
                self._resend(lost)

    def _handover(self, old: Connection) -> None:
        """The service is handing over: new calls go to its successor; `old` keeps the calls it holds."""
        old.draining = True
        with self._lock:
            if self._current is old:
                self._current = None
        threading.Thread(target=self._connect_with_backoff, name="service-reconnect", daemon=True).start()

    def _retry_elsewhere(self, conn: Connection, call_id: str) -> None:
        conn.draining = True
        with self._lock:
            conn.calls.discard(call_id)
            frame = self._calls.get(call_id)
            if self._current is conn:
                self._current = None
        if frame is not None:
            self._resend([{k: v for k, v in frame.items() if k != "resend"}], mark=False)

    def _resend_after_loss(self) -> None:
        with self._lock:
            frames_ = list(self._calls.values())
        self._resend(frames_)

    def _connect_with_backoff(self) -> Connection | None:
        start = time.monotonic()
        attempt = 0
        while not self._closing:
            try:
                started = time.monotonic() - start >= RECONNECT_FOR_S
                return self.connect(start=started)
            except endpoint.Unreachable:
                if time.monotonic() - start >= RECONNECT_FOR_S + endpoint.START_WAIT_S:
                    return None
                time.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                attempt += 1
        return None

    def _resend(self, frames_: list[dict], mark: bool = True) -> None:
        def run() -> None:
            conn = self._connect_with_backoff()
            for frame in frames_:
                frame = {**frame, "resend": True} if mark else frame
                if conn is None:
                    self.on_frame({"t": "error", "id": frame["id"], "code": "SHUTTING_DOWN",
                                   "message": "the Theodore service could not be reached again"})
                    continue
                with self._lock:
                    conn.calls.add(frame["id"])
                with contextlib.suppress(Exception):
                    conn.channel.send(frame)
        threading.Thread(target=run, name="service-resend", daemon=True).start()
