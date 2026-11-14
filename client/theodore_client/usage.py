"""Usage counts: the ledger the engine and the client append to, and the batches sent to POST /v1/usage.

Nothing here is text from the work. A ledger line is one JSON object whose every field is a name from a fixed list,
a number, a boolean, a short code or a random id (the backend's packages/contracts/src/usage.ts); a line with any other
key or shape is dropped here and never sent. The ledger exists only while the last licence refresh said
usage_enabled: true. Sending never changes what a command does: one attempt, two seconds at most, every error
swallowed, nothing printed.

Files, all in Theodore's state folder:
  usage-ledger.jsonl          one event per line; the engine appends (O_APPEND), the client appends its own statuses
  usage-ledger.jsonl.taking   the ledger while the client removes lines from it (renamed away, so appends go on)
  usage.json                  {enabled, at, dropped, pending}: the switch, the dropped count, the batch being sent
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from . import __version__, core
from .contract import (CLIENT_COMMANDS, EXIT, USAGE_BATCH_MAX_BYTES, USAGE_BATCH_MAX_EVENTS, USAGE_CODE_PATTERN,
                       USAGE_DROPPED_MAX, USAGE_FLUSH_AGE_S, USAGE_KINDS, USAGE_LEDGER_MAX_BYTES, USAGE_LEDGER_MAX_LINES,
                       USAGE_MAX_AGE_S, USAGE_OS_PATTERN, USAGE_OUTCOMES, USAGE_TIMEOUT_S, USAGE_TS_MAX, USAGE_TS_MIN,
                       USAGE_VERBS, USAGE_WORKSPACES)

LEDGER = "usage-ledger.jsonl"
STATE = "usage.json"

_CODE = re.compile(USAGE_CODE_PATTERN)
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
_DECK = re.compile(r"^[0-9a-f]{32}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_STATUSES = tuple(name for name in EXIT if name != "OK")


def ledger_path() -> Path:
    return core.home().state / LEDGER


def _taking_path() -> Path:
    return core.home().state / (LEDGER + ".taking")


def _state_path() -> Path:
    return core.home().state / STATE


def _state() -> dict:
    value = core.read_json(_state_path())
    return value if isinstance(value, dict) else {}


def _save(state: dict) -> None:
    core.write_json(_state_path(), state, 0o600)


# --- the switch ---------------------------------------------------------------------------------------------------------------

def enabled() -> bool:
    """True only when the last licence refresh answered usage_enabled: true and no 403 usage_off came after it."""
    return _state().get("enabled") is True


def remember(answer_enabled, asked_at: float) -> None:
    """What a licence refresh answered. Only a literal true turns the counts on (absent is an older server: off). Off
    deletes the ledger. An on from a refresh asked before a usage_off this computer learned meanwhile is ignored."""
    on = answer_enabled is True
    with core.lock("usage", timeout=10):
        state = _state()
        if on and state.get("enabled") is False and state.get("source") == "refused" and float(state.get("at") or 0) >= int(asked_at):
            return
        if not on:
            _forget_ledger(state)
        state.update(enabled=on, at=int(core.now()), source="refresh")
        _save(state)


def forget() -> None:
    """Signed out: the ledger, the pending batch and the switch go (a later sign-in may be another account)."""
    with core.lock("usage", timeout=10):
        for path in (ledger_path(), _taking_path(), _state_path()):
            core.remove_tree(path)


def _forget_ledger(state: dict) -> None:
    for path in (ledger_path(), _taking_path()):
        core.remove_tree(path)
    state.pop("pending", None)
    state["dropped"] = 0


def engine_env(env: dict) -> dict:
    """The engine's environment: the ledger path when usage is on, and never otherwise (not even one inherited)."""
    from .contract import USAGE_LEDGER_ENV
    env.pop(USAGE_LEDGER_ENV, None)
    with contextlib.suppress(Exception):
        if enabled():
            env[USAGE_LEDGER_ENV] = str(ledger_path())
    return env


# --- validation: the contract's strict objects ------------------------------------------------------------------------------

def _int(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _code_or_null(value) -> bool:
    return value is None or (isinstance(value, str) and bool(_CODE.match(value)))


_COMMAND_REQUIRED = {
    "type": lambda v: v == "command",
    "event_id": lambda v: isinstance(v, str) and bool(_UUID.match(v)),
    "ts": lambda v: _int(v, USAGE_TS_MIN, USAGE_TS_MAX),
    "verb": lambda v: v in USAGE_VERBS,
    "deck_id": lambda v: v is None or (isinstance(v, str) and bool(_DECK.match(v))),
    "workspace": lambda v: v is None or v in USAGE_WORKSPACES,
    "kind": lambda v: v is None or v in USAGE_KINDS,
    "outcome": lambda v: v in USAGE_OUTCOMES,
    "code": _code_or_null,
    "duration_ms": lambda v: _int(v, 0, 86_400_000),
}
_COMMAND_OPTIONAL = {
    "reopen": lambda v: isinstance(v, bool),
    "retryable": lambda v: isinstance(v, bool),
    "slides_total": lambda v: _int(v, 0, 2000),
    "slides_added": lambda v: _int(v, 0, 2000),
    "slides_changed": lambda v: _int(v, 0, 2000),
}
_STATUS_REQUIRED = {
    "type": lambda v: v == "client_status",
    "event_id": _COMMAND_REQUIRED["event_id"],
    "ts": _COMMAND_REQUIRED["ts"],
    "command": lambda v: v in CLIENT_COMMANDS,
    "status": lambda v: v in _STATUSES,
    "cause": _code_or_null,
}


def valid_event(event) -> bool:
    """The contract's UsageCommandEvent or UsageClientStatusEvent exactly: every required key, no other key, every
    value of its shape. Anything else would fail the whole batch, and may carry text, so it is never sent."""
    if not isinstance(event, dict):
        return False
    required, optional = {"command": (_COMMAND_REQUIRED, _COMMAND_OPTIONAL),
                          "client_status": (_STATUS_REQUIRED, {})}.get(event.get("type"), (None, None))
    if required is None or not set(required) <= set(event) or not set(event) <= set(required) | set(optional):
        return False
    rules = {**required, **optional}
    try:
        return all(rules[key](value) for key, value in event.items())
    except Exception:       # noqa: BLE001  an unhashable value where a name is expected
        return False


# --- the ledger -------------------------------------------------------------------------------------------------------------

def _append(lines: list[bytes]) -> None:
    if not lines:
        return
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, b"".join(line.rstrip(b"\r\n") + b"\n" for line in lines))
    finally:
        os.close(fd)


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _parse(raw: bytes) -> tuple[list[tuple[dict, bytes]], int]:
    """(valid events with their line, lines dropped: malformed, older than 30 days, then the oldest over the cap).
    A repeated event id is kept once and not counted."""
    now_ms = int(core.now() * 1000)
    events, seen, dropped = [], set(), 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            event = None
        if not valid_event(event) or event["ts"] < now_ms - USAGE_MAX_AGE_S * 1000:
            dropped += 1
            continue
        if event["event_id"] in seen:
            continue
        seen.add(event["event_id"])
        events.append((event, line))
    events.sort(key=lambda pair: pair[0]["ts"])
    over = max(0, len(events) - USAGE_LEDGER_MAX_LINES)
    size = sum(len(line) + 1 for _event, line in events[over:])
    while over < len(events) and size > USAGE_LEDGER_MAX_BYTES:
        size -= len(events[over][1]) + 1
        over += 1
    return events[over:], dropped + over


def _compact(state: dict, remove: set[str], extra_dropped: int = 0) -> list[dict]:
    """Rewrite the ledger without the `remove` ids and without what _parse drops, counting the drops. Appends made
    meanwhile are never lost: the ledger is renamed away first (a new append creates a new ledger), what is kept is
    appended to the new one, and anything that still reached the renamed file is carried over before it goes. On
    Windows a ledger the engine has open cannot be renamed: then nothing is rewritten now. Called under the lock."""
    ledger, taking = ledger_path(), _taking_path()
    if taking.exists():                      # left by a client that stopped half way: its lines go back first
        _append([line for line in _read(taking).splitlines() if line.strip()])
        core.remove_tree(taking)
    try:
        os.replace(ledger, taking)
    except FileNotFoundError:
        events, dropped = [], 0
    except OSError:
        events, dropped = _parse(_read(ledger))
        return [event for event, _line in events if event["event_id"] not in remove]
    else:
        raw, previous = b"", -1
        for _ in range(5):                   # an append already under way finishes into the renamed file
            raw = _read(taking)
            if len(raw) == previous:
                break
            previous = len(raw)
            time.sleep(0.02)
        events, dropped = _parse(raw)
        kept = [(event, line) for event, line in events if event["event_id"] not in remove]
        _append([line for _event, line in kept])
        late = _read(taking)[len(raw):]
        _append([line for line in late.splitlines() if line.strip()])
        core.remove_tree(taking)
        events = kept
    state["dropped"] = min(USAGE_DROPPED_MAX, int(state.get("dropped") or 0) + dropped + extra_dropped)
    return [event for event, _line in events if event["event_id"] not in remove]


def _pending_events(state: dict) -> list[dict]:
    """The events waiting, oldest first, after removing (and counting) what may not stay; rewrites only when needed."""
    events, dropped = _parse(_read(ledger_path()))
    if dropped or _taking_path().exists():
        return _compact(state, set())
    return [event for event, _line in events]


def record_status(command: str | None, status_name: str, cause: str | None) -> None:
    """A launcher command that ended with a non-OK status: one client_status line, only while usage is on. Never raises."""
    with contextlib.suppress(Exception):
        if not enabled() or command not in CLIENT_COMMANDS or status_name not in _STATUSES:
            return
        event = {"type": "client_status", "event_id": str(uuid.uuid4()), "ts": int(core.now() * 1000),
                 "command": command, "status": status_name, "cause": cause if _code_or_null(cause) else None}
        if valid_event(event):
            _append([json.dumps(event, separators=(",", ":")).encode("utf-8")])


# --- the batch ------------------------------------------------------------------------------------------------------------------

def os_family() -> str:
    """macos-15, windows-11, linux-6: the family and the major version only."""
    try:
        if sys.platform == "darwin":
            family, major = "macos", platform.mac_ver()[0].split(".")[0]
        elif os.name == "nt":
            build = sys.getwindowsversion().build          # Windows 11 still reports release "10"
            family, major = "windows", "11" if build >= 22000 else str(sys.getwindowsversion().major)
        else:
            family, major = "linux", platform.release().split(".")[0]
    except Exception:       # noqa: BLE001
        family, major = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux"), "0"
    major = major if major.isdigit() and len(major) <= 3 else "0"
    return f"{family}-{major}"


def _engine_version() -> str | None:
    with contextlib.suppress(Exception):
        from . import install
        version = install.current_engine()
        if version and _SEMVER.match(version) and len(version) <= 64:
            return version
    return None


def _envelope(dropped: int, events: list[dict]) -> dict:
    _system, arch = core.platform_arch()
    host = core.host_name()
    plugin = core.plugin_version()
    return {"batch_id": str(uuid.uuid4()), "client_version": __version__,
            "plugin_version": plugin if _SEMVER.match(plugin) else "0.0.0", "engine_version": _engine_version(),
            "host": host if _SLUG.match(host) else "other", "os": os_family(), "arch": arch if arch in ("arm64", "x64") else "x64",
            "dropped": min(USAGE_DROPPED_MAX, dropped), "events": events}


def encode(body: dict) -> bytes:
    return json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def _next_batch(state: dict) -> dict | None:
    """The batch to send next: the one sent before and not answered (same batch_id, same events) while all its events
    are still waiting, else a new one of the oldest events, at most 200 and 64 KB. None when nothing waits. Under the lock."""
    events = _pending_events(state)
    if not events:
        state.pop("pending", None)
        return None
    waiting = {event["event_id"] for event in events}
    pending = state.get("pending")
    if isinstance(pending, dict) and pending.get("events") and all(
            isinstance(e, dict) and e.get("event_id") in waiting for e in pending["events"]):
        return pending
    body = _envelope(int(state.get("dropped") or 0), [])
    for event in events[:USAGE_BATCH_MAX_EVENTS]:
        body["events"].append(event)
        if len(encode(body)) > USAGE_BATCH_MAX_BYTES:
            body["events"].pop()
            break
    if not body["events"]:
        return None
    state["pending"] = body
    return body


def _oldest_ms() -> int | None:
    events, _dropped = _parse(_read(ledger_path()))
    return events[0][0]["ts"] if events else None


def _post(bearer: str, data: bytes):
    """One POST, answered within two seconds or abandoned: (HTTP status, error code) or None for no answer."""
    from . import api
    result: list = []

    def attempt():
        try:
            status, _headers, _raw = api.call("usage", bearer=bearer, body=data, timeout=USAGE_TIMEOUT_S,
                                              exact_timeout=True, learn_refusals=False, limit=64 << 10)
            result.append((status, ""))
        except api.ApiError as error:
            result.append((error.http_status, error.code))
        except Exception:       # noqa: BLE001  network, TLS, a body too long: no answer
            result.append(None)

    worker = threading.Thread(target=attempt, name="theodore-usage", daemon=True)
    worker.start()
    worker.join(USAGE_TIMEOUT_S)
    return result[0] if result else None


def flush(force: bool = False) -> None:
    """Send one batch when usage is on, this computer is signed in and something waits: always when `force` (the end
    of pptx finish), else only when the oldest waiting event is older than 15 minutes. Never raises, never prints."""
    try:
        if not enabled() or not ledger_path().exists() and not _taking_path().exists():
            return
        if not force:
            oldest = _oldest_ms()
            if oldest is None or oldest > int(core.now() * 1000) - USAGE_FLUSH_AGE_S * 1000:
                return
        from . import credentials
        cred = credentials.load()
        if not cred:
            return
        with core.lock("usage", timeout=0.5):        # another process is sending: it sends for both
            state = _state()
            body = _next_batch(state)
            _save(state)
            if body is None:
                return
            answer = _post(cred["installation_secret"], encode(body))
            sent = {event["event_id"] for event in body["events"]}
            state = _state()
            if answer is not None and 200 <= answer[0] < 300:
                state["dropped"] = max(0, int(state.get("dropped") or 0) - int(body.get("dropped") or 0))
                state.pop("pending", None)
                _compact(state, sent)
            elif answer is not None and answer[0] == 403 and answer[1] == "usage_off":
                _forget_ledger(state)
                state.update(enabled=False, at=int(core.now()), source="refused")
            elif answer is not None and answer[0] == 422:
                state.pop("pending", None)
                _compact(state, sent, extra_dropped=len(sent))
            elif answer is not None and answer[0] == 413:
                state.pop("pending", None)          # kept, and cut again next time
            core.log(f"usage: batch of {len(sent)} answered {answer[0] if answer else 'nothing'}")
            _save(state)
    except Exception as exc:        # noqa: BLE001  usage counts never change what a command does
        with contextlib.suppress(Exception):
            core.log(f"usage: not sent ({exc.__class__.__name__})")


# --- theodore usage [--show] --------------------------------------------------------------------------------------------------

def show(batch: bool) -> int:
    """For people: `usage --show` prints the next batch exactly as it would be sent; `usage` says where things stand."""
    if not enabled():
        print("Usage counts are off for this computer: nothing is recorded and nothing is sent.")
        return 0
    with core.lock("usage", timeout=10):
        state = _state()
        if not batch:
            waiting = len(_pending_events(state))
            _save(state)
            print(f"Usage counts are on. Ledger: {ledger_path()}\n{waiting} event(s) waiting, {int(state.get('dropped') or 0)} "
                  f"dropped since the last batch sent. `usage --show` prints the next batch exactly as it would be sent.")
            return 0
        body = _next_batch(state)
        _save(state)
    if body is None:
        print("Nothing is waiting to be sent.")
    else:
        sys.stdout.write(encode(body).decode("utf-8") + "\n")
    return 0
