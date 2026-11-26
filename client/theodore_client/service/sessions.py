"""The sessions the service serves: each relay's `hello` names one (Claude Code's session id, Codex's thread id, or one
the relay made up); fronts name theirs too but never keep one open. A session is open while at least one relay of it
is connected. Opening one warms what its first command needs (texts connection, engine worker, update check); closing
one drops what belonged to it (its transient sheet files) and starts the service's linger clock when it was the last.
"""

from __future__ import annotations

import threading
import time


class Session:
    __slots__ = ("id", "host", "relays", "opened", "last_seen", "plugin_root", "chat", "cwd")

    def __init__(self, session_id: str, host: str, plugin_root: str | None, cwd: str | None = None):
        self.id = session_id
        self.host = host
        self.cwd = cwd                   # the folder the session works in (its relay's hello), when known
        self.relays: set = set()
        self.opened = time.monotonic()
        self.last_seen = self.opened
        self.plugin_root = plugin_root
        self.chat = None                 # the chat tool's per-session state (chat.py)


class Sessions:
    def __init__(self, on_open=None, on_close=None):
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._on_open = on_open
        self._on_close = on_close
        self.last_detach: float | None = None

    def attach(self, client) -> bool:
        """A relay connected for its session; True when this opened the session."""
        with self._lock:
            session = self._sessions.get(client.session_id)
            new = session is None
            if new:
                session = self._sessions[client.session_id] = Session(client.session_id, client.host, client.plugin_root,
                                                                      getattr(client, "cwd", None))
            session.relays.add(client)
            session.last_seen = time.monotonic()
        if new and self._on_open is not None:
            self._on_open(session)
        return new

    def detach(self, client) -> None:
        closed = None
        with self._lock:
            session = self._sessions.get(client.session_id)
            if session is None or client not in session.relays:
                return
            session.relays.discard(client)
            if not session.relays:
                closed = self._sessions.pop(client.session_id)
            if not any(s.relays for s in self._sessions.values()):
                self.last_detach = time.monotonic()
        if closed is not None and self._on_close is not None:
            self._on_close(closed)

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def relays(self) -> int:
        with self._lock:
            return sum(len(s.relays) for s in self._sessions.values())

    def all(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())
