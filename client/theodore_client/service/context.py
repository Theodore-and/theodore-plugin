"""The call a service thread is answering: its session, host, environment overlay and working folder.

The client's modules learn the assistant they serve from the environment (core.host_name, core.session_id), and a
command learns refusals into process globals (core.mark_refused). One service answers many sessions at once, so:

- `install_environ()` makes `os.environ` read the current call's overlay first (C1's allow-list: CLAUDECODE,
  CLAUDE_CODE_SESSION_ID, CLAUDE_PID, CLAUDE_PLUGIN_ROOT, CODEX_THREAD_ID, THEODORE_*), over the service's own environment, from
  which the host markers are removed at start. A value of None in the overlay hides a variable for the call.
- `entered(ctx)` sets the call for this thread. Its refusal facts are core's per-thread scope (core.begin_call).
"""

from __future__ import annotations

import collections.abc
import contextlib
import os
import re
import threading

_LOCAL = threading.local()

ALLOWED = re.compile(r"^(CLAUDECODE|CLAUDE_CODE_SESSION_ID|CLAUDE_PID|CLAUDE_PLUGIN_ROOT|CODEX_THREAD_ID|THEODORE_[A-Z0-9_]+)$")
# Never taken from a call: they name the service's own home and its service, which one call cannot change.
FIXED = ("THEODORE_HOME", "THEODORE_API_BASE", "THEODORE_CREDENTIAL_STORE", "THEODORE_WORK_DIR", "THEODORE_RESIDENT")
HOST_MARKERS = ("CLAUDECODE", "CLAUDE_PID", "THEODORE_HOST")
HOST_MARKER_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_PLUGIN_", "CODEX_")
KEPT = ("CODEX_HOME",)             # where Codex keeps its configuration: the user's, whichever session asks


class CallContext:
    __slots__ = ("session", "host", "env", "cwd", "call_id", "tool", "progress", "cancelled")

    def __init__(self, *, session: str = "", host: str = "", env: dict | None = None, cwd: str | None = None,
                 call_id: str = "", tool: str = "", progress=None, cancelled=None):
        self.session = session
        self.host = host
        self.env = env if env is not None else {}
        self.cwd = cwd
        self.call_id = call_id
        self.tool = tool
        self.progress = progress            # progress(phase) or None
        self.cancelled = cancelled          # a threading.Event or None


def current() -> CallContext | None:
    return getattr(_LOCAL, "ctx", None)


@contextlib.contextmanager
def entered(ctx: CallContext):
    before = getattr(_LOCAL, "ctx", None)
    _LOCAL.ctx = ctx
    try:
        yield ctx
    finally:
        _LOCAL.ctx = before


def overlay(env: dict | None) -> dict:
    """A call's environment, reduced to the allow-list (values as strings; "" hides the variable for the call)."""
    out: dict = {}
    for key, value in (env or {}).items():
        if isinstance(key, str) and ALLOWED.match(key) and key not in FIXED and isinstance(value, str):
            out[key] = value if value != "" else None
    return out


class _Environ(collections.abc.MutableMapping):
    """os.environ as the current call sees it: its overlay over the service's environment."""

    def __init__(self, base):
        self._base = base

    @staticmethod
    def _overlay():
        ctx = getattr(_LOCAL, "ctx", None)
        return ctx.env if ctx is not None and ctx.env else None

    def __getitem__(self, key):
        layer = self._overlay()
        if layer is not None and key in layer:
            value = layer[key]
            if value is None:
                raise KeyError(key)
            return value
        return self._base[key]

    def __setitem__(self, key, value):
        self._base[key] = value

    def __delitem__(self, key):
        del self._base[key]

    def __iter__(self):
        layer = self._overlay()
        if not layer:
            return iter(list(self._base))
        keys = [k for k in self._base if not (k in layer and layer[k] is None)]
        keys += [k for k, v in layer.items() if v is not None and k not in self._base]
        return iter(keys)

    def __len__(self):
        return sum(1 for _ in self)

    def __contains__(self, key):
        try:
            self[key]
        except KeyError:
            return False
        return True

    def copy(self) -> dict:
        return dict(self)

    def __repr__(self):
        return f"environ({dict(self)!r})"


_INSTALLED = False


def install_environ() -> None:
    """From now on os.environ reads the current call's overlay first. The host markers of whoever started the service
    are removed from its own environment: each call names its host itself."""
    global _INSTALLED
    if _INSTALLED:
        return
    base = os.environ
    for name in [n for n in list(base) if (n in HOST_MARKERS or n.startswith(HOST_MARKER_PREFIXES)) and n not in KEPT]:
        del base[name]
    os.environ = _Environ(base)
    _INSTALLED = True
