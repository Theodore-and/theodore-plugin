"""Faults Theodore files by itself, with no assistant involved, through the report path `theodore report` uses: the same
redaction (commands.scrub), the same /v1/reports with origin "automatic", the account's switch for automatic reports,
the outbox for a service that says "later", and one report per fault per installation per day (commands._auto_report).

Three kinds, none of which an assistant can judge from what it sees: an answer that carried `engine_fault`, a worker
stopped at a call ceiling or lost during a call, and an exception the client catches and only logs in a background job.
Filing is always queued on a thread of its own; nothing here waits, raises or prints, so no answer is slower for it."""

from __future__ import annotations

import contextlib
import re
import threading

from . import core

SEND_WAIT_S = 10.0          # a background send waits this long for the service; nobody waits for the send
_threads: list[threading.Thread] = []
_VERB = re.compile(r"[a-z][a-z-]{0,31}")
_FAULT_CLASS = re.compile(r"fault in the engine: ([A-Za-z_][\w.]{0,80})")
_FAULT_AT = re.compile(r"fault_at\W{1,6}([A-Za-z0-9_.\-]+:\d+ [A-Za-z_][\w<>]*)")


def _module(text: str) -> str:
    """`layout.py:88 place` as `layout:88 place`: the redactor takes a file name for a user's, and these are Theodore's own."""
    return re.sub(r"\b([A-Za-z0-9_]+)\.(?:py|js):(\d+)", r"\1:\2", text)


def _verb(word: str | None) -> str:
    word = (word or "").strip()
    return word if _VERB.fullmatch(word) else "command"


def _send(summary: str, verb: str, key: str, title: str, code: str) -> None:
    try:
        from . import commands
        commands._auto_report(summary, verb, key=key, title=title, code=code, prefix="THEODORE FAULT: ",  # noqa: SLF001
                              wait_s=SEND_WAIT_S)
    except Exception as exc:  # noqa: BLE001  a report never makes anything worse
        with contextlib.suppress(Exception):
            core.log(f"fault not reported: {exc.__class__.__name__}")


def queue(summary: str, verb: str, key: str, title: str, code: str) -> None:
    """Hand one fault to a thread of its own. Returns at once; never raises."""
    with contextlib.suppress(Exception):
        thread = threading.Thread(target=_send, args=(summary, verb, key, title, code), name="fault-report", daemon=True)
        _threads[:] = [t for t in _threads if t.is_alive()] + [thread]
        thread.start()


def wait_idle(timeout: float = 10.0) -> None:
    """For tests and for a process about to end: the queued sends, waited for."""
    for thread in list(_threads):
        thread.join(timeout)


def engine_fault(verb: str | None, output: str) -> None:
    """An answer that carries error_code engine_fault: the verb, the fault's class and fault_at; never its message
    (it can hold a path or a word of the deck)."""
    if "engine_fault" not in output:
        return
    found = _FAULT_CLASS.search(output)
    where = _FAULT_AT.search(output)
    kind = found.group(1) if found else "unknown"
    at = _module(where.group(1)) if where else "unknown"
    word = _verb(verb)
    queue(f"engine fault in {word}: {kind}; fault_at {at}", word, f"engine_fault|{word}|{kind}|{at}",
          f"engine fault in {word}", "engine_fault")


def call_lost(verb: str | None, how: str, elapsed_s: float, stack: str | None, engine_stack: str | None = None) -> None:
    """A worker stopped at a call ceiling, or lost during a call: the verb, the time and the service's last stack lines."""
    word = _verb(verb)
    ceiling = "did not finish" in how or "ceiling" in how
    label = "ceiling" if ceiling else "worker lost"
    queue(f"{label} in {word}: the engine {how}; elapsed {int(elapsed_s)} s; last stack: {_module(stack) if stack else 'not known'}"
          f"{f'; last engine stack: {_module(engine_stack)}' if engine_stack else ''}", word,
          f"{label}|{word}", f"call {'stopped at its ceiling' if ceiling else 'lost its worker'} in {word}",
          "call_ceiling" if ceiling else "worker_lost")


def job_failed(job: str, error: BaseException | str) -> None:
    """A background job's exception the client caught and only logged: the job's name, the class and the message."""
    if isinstance(error, BaseException):
        kind, message = error.__class__.__name__, str(error)
    else:
        kind, message = "Failure", str(error)
    queue(f"background job {job} failed: {kind}: {core.scrub_secrets(message)[:300]}", "job", f"job|{job}|{kind}",
          f"background job failed: {job}", "background_job")
