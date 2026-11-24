"""What a call prints, per thread: the service answers many calls at once, and the client's commands print.

`install()` replaces sys.stdout and sys.stderr with streams that write to the calling thread's own buffer while it is
inside `captured()`, and to the service log otherwise. `contextlib.redirect_stdout` swaps one process-wide stream and
would mix the output of calls running at the same time; this does not. Outside the service (the command line, tests),
`captured()` falls back to `contextlib.redirect_stdout`, which is right for one thread.
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading

_LOCAL = threading.local()
_INSTALLED = False


class _ThreadStream(io.TextIOBase):
    """A text stream that writes to the current thread's buffer when one is set, else to `fallback`."""

    def __init__(self, name: str, fallback):
        super().__init__()
        self._name = name
        self._fallback = fallback

    def _target(self):
        return getattr(_LOCAL, self._name, None) or self._fallback

    def write(self, text) -> int:
        target = self._target()
        if target is None:
            return len(text)
        return target.write(text)

    def flush(self) -> None:
        target = self._target()
        if target is not None:
            with contextlib.suppress(Exception):
                target.flush()

    @property
    def encoding(self) -> str:      # the client checks it (cli._utf8_streams); everything here is UTF-8 text
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False


def install(fallback) -> None:
    """Route sys.stdout and sys.stderr per thread from now on; `fallback` takes what no call captures (the log)."""
    global _INSTALLED
    sys.stdout = _ThreadStream("stdout", fallback)
    sys.stderr = _ThreadStream("stderr", fallback)
    _INSTALLED = True


def installed() -> bool:
    return _INSTALLED


@contextlib.contextmanager
def captured(stderr: bool = False):
    """Everything this thread prints inside the block goes into the StringIO it yields (standard error too when
    `stderr` is true; otherwise standard error keeps going where it went)."""
    buffer = io.StringIO()
    if not _INSTALLED:
        with contextlib.redirect_stdout(buffer):
            if stderr:
                with contextlib.redirect_stderr(buffer):
                    yield buffer
            else:
                yield buffer
        return
    before = getattr(_LOCAL, "stdout", None), getattr(_LOCAL, "stderr", None)
    _LOCAL.stdout = buffer
    if stderr:
        _LOCAL.stderr = buffer
    try:
        yield buffer
    finally:
        _LOCAL.stdout, _LOCAL.stderr = before


def printed(run) -> str:
    """What `run()` prints, as text."""
    with captured() as buffer:
        run()
    return buffer.getvalue()
