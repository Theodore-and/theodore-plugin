"""The call a service thread is answering: its session, host, environment overlay and working folder.

The client's modules learn the assistant they serve from the environment (core.host_name, core.session_id), and a
command learns refusals into process globals (core.mark_refused). One service answers many sessions at once, so:

- `install_environ()` makes `os.environ` read the current call's overlay first (C1's allow-list: CLAUDECODE,
  CLAUDE_CODE_SESSION_ID, CLAUDE_PID, CLAUDE_PLUGIN_ROOT, CODEX_THREAD_ID, THEODORE_*), over the service's own environment, from
  which the host markers are removed at start. A value of None in the overlay hides a variable for the call.
- `entered(ctx)` sets the call for this thread. Its refusal facts are core's per-thread scope (core.begin_call).
- The calling client's proxy settings (the call's THEODORE_CALLER_PROXY, a name C1's allow-list takes as it stands)
  are part of what the call's thread reads, and of nothing an engine is given: the service was started by whichever
  command came first and kept that one's environment, and a proxy named there is that caller's alone (Claude Code's
  sandbox gives each session its own, which ends with the session). urllib reads the proxy variables from
  `os.environ` for each request, so a call's network work goes by its caller's settings; work outside any call goes
  by the last caller's, not by the starter's for ever.
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
PROXY_NAMES = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")  # hot.py's
CALLER_PROXY = "THEODORE_CALLER_PROXY"      # hot.py's: the caller's proxy settings, a JSON object, in the call's env
HOST_MARKER_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_PLUGIN_", "CODEX_")
KEPT = ("CODEX_HOME",)             # where Codex keeps its configuration: the user's, whichever session asks


class CallContext:
    __slots__ = ("session", "host", "env", "view", "cwd", "call_id", "tool", "progress", "cancelled", "call")

    def __init__(self, *, session: str = "", host: str = "", env: dict | None = None, cwd: str | None = None,
                 call_id: str = "", tool: str = "", progress=None, cancelled=None, call=None, proxy: dict | None = None):
        self.session = session
        self.host = host
        self.env = env if env is not None else {}           # C1's overlay: also what the call's engine is given
        # What this thread's os.environ reads first: the overlay and the caller's proxy settings, or the last caller's
        # for a call that brought none (a client from before 0.5.1, a timer's work, the service's own call).
        layer = proxy or _OUTSIDE
        self.view = {**layer, **self.env} if layer else self.env
        self.cwd = cwd
        self.call_id = call_id
        self.tool = tool
        self.progress = progress            # progress(phase) or None
        self.cancelled = cancelled          # a threading.Event or None
        self.call = call                    # the router's Call being answered


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


_OUTSIDE: dict = {}         # the last caller's proxy settings: what a thread answering no call reads first


def proxy_layer(sent: str | None) -> dict:
    """A call's proxy settings (its THEODORE_CALLER_PROXY) as a layer over the environment: each proxy variable the
    caller has, and None (hidden) for each it has not, so nothing of the service's own shows through. {} for a caller
    that sent none (a client from before 0.5.1) or something unreadable: it reads the last caller's, or the service's.
    Credentials in a proxy's address go to the scrubber before anything can be printed or logged, and the settings are
    remembered for work outside any call."""
    global _OUTSIDE
    if not isinstance(sent, str):
        return {}
    try:
        import json
        proxy = json.loads(sent)
    except ValueError:
        return {}
    if not isinstance(proxy, dict):
        return {}
    layer: dict = {}
    for name in PROXY_NAMES:
        value = proxy.get(name)
        layer[name] = value if isinstance(value, str) and value else None
        if layer[name] and "@" in layer[name]:
            from .. import core
            userinfo = layer[name].rpartition("@")[0].rpartition("//")[2]
            core.register_secret(userinfo)
            core.register_secret(userinfo.partition(":")[2])
    _OUTSIDE = layer
    return layer


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
        if ctx is None:
            return _OUTSIDE or None
        return ctx.view or None

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
