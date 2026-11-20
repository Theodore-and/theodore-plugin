"""Reports the service could not take yet: kept on this computer and sent by Theodore itself, never by the assistant.

A report the service answers "later" (HTTP 429 or 503) or that met no answer at all (no connection, a timeout) is saved
in state/outbox/, one file per report, holding exactly the payload that would have been sent (already scrubbed) and the
earliest time to try again. Flushes run at the start of a `report`, after the hourly heartbeat and at the end of a deck
(`pptx finish`): oldest first, a few per run, within a few seconds, stopping at the first "later". A report keeps its
client_report_id, which the service uses to recognise a repeat, so an answer lost on the way never makes a second copy.

Bounds: at most MAX_WAITING files (the oldest goes first), none older than MAX_AGE_S, a file removed once delivered or
refused for good. logout and uninstall remove the folder. Nothing here raises into a command or prints."""

from __future__ import annotations

import contextlib
import email.utils
import json
import re
import threading
import time
from pathlib import Path

from . import core

FOLDER = "outbox"                 # in state/
MAX_WAITING = 20
MAX_AGE_S = 14 * 86400
DEFAULT_WAIT_S = 60               # "later" without a Retry-After, and a network failure
MAX_WAIT_S = 86400                # a Retry-After further ahead than a day is read as a day
FLUSH_BUDGET_S = 4.0              # a flush never holds a command longer than this
FLUSH_MAX = 3                     # reports sent per flush at most
SEND_TIMEOUT_S = 3.0
_NAME = re.compile(r"^[A-Za-z0-9._:-]{8,64}\.json$")


def folder() -> Path:
    return core.home().state / FOLDER


def later(error: Exception) -> bool:
    """True when the service said "later" (429, 503) or could not be reached on an ordinary network; every other
    answer is final and is not kept."""
    from . import api
    if isinstance(error, api.ApiError):
        return error.http_status in (429, 503)
    if isinstance(error, api.NetworkError):
        return error.cause == "NETWORK" and not api.sandbox_blocked(error.detail)
    return False


def _wait_s(error: Exception | None) -> float:
    raw = str(getattr(error, "retry_after", None) or "").strip()
    if raw.isdigit():
        return float(min(MAX_WAIT_S, max(1, int(raw))))
    if raw:
        with contextlib.suppress(Exception):
            when = email.utils.parsedate_to_datetime(raw).timestamp()
            return float(min(MAX_WAIT_S, max(1.0, when - core.now())))
    return float(DEFAULT_WAIT_S)


def _entries() -> list[tuple[Path, dict]]:
    """The waiting reports, oldest first. A file that cannot be read or is older than MAX_AGE_S is removed."""
    base = folder()
    if not base.is_dir():
        return []
    found = []
    for path in base.iterdir():
        if not _NAME.match(path.name):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            queued = float(record["queued_at"])
            if not isinstance(record.get("payload"), dict):
                raise ValueError("no payload")
        except (OSError, ValueError, KeyError, TypeError):
            core.log(f"outbox: {path.name} could not be read; removed")
            core.remove_tree(path)
            continue
        if core.now() - queued > MAX_AGE_S:
            core.log(f"outbox: report {path.stem} waited 14 days; removed unsent")
            core.remove_tree(path)
            continue
        found.append((path, record))
    found.sort(key=lambda item: (float(item[1]["queued_at"]), item[0].name))
    return found


def _prune() -> None:
    waiting = _entries()
    for path, _record in waiting[:max(0, len(waiting) - MAX_WAITING)]:
        core.log(f"outbox: more than {MAX_WAITING} reports waiting; the oldest, {path.stem}, was removed unsent")
        core.remove_tree(path)


def save(payload: dict, error: Exception | None = None) -> bool:
    """Keep one report for later; True when it is on disk. Never raises."""
    try:
        report_id = str(payload.get("client_report_id") or "")
        path = folder() / f"{report_id}.json"
        if not _NAME.match(path.name):
            return False
        record = {"version": 1, "queued_at": core.now(), "not_before": core.now() + _wait_s(error),
                  "payload": payload}
        core.write_json(path, record, 0o600)
        try:
            with core.lock("outbox", timeout=1.0):
                _prune()
        except core.Status:         # another process holds it: the next save or flush prunes
            pass
        core.log(f"outbox: report {report_id} saved; the service is tried again later")
        return path.exists()
    except Exception as exc:        # noqa: BLE001
        with contextlib.suppress(Exception):
            core.log(f"outbox: report not saved ({exc.__class__.__name__}: {exc})")
        return False


def count() -> int:
    """How many reports wait (for doctor). Never raises."""
    try:
        base = folder()
        return sum(1 for p in base.iterdir() if _NAME.match(p.name)) if base.is_dir() else 0
    except OSError:
        return 0


def forget() -> None:
    core.remove_tree(folder())


def flush(budget: float = FLUSH_BUDGET_S, limit: int = FLUSH_MAX) -> None:
    """Send what waits and is due, within `budget` seconds: a send still running then is left to finish while the
    process lives (its file stays until its answer arrives, and its id makes a later repeat harmless). Never raises,
    never prints, never changes what the command does."""
    try:
        if count() == 0:
            return
    except Exception:       # noqa: BLE001
        return
    deadline = time.monotonic() + budget
    worker = threading.Thread(target=_flush, args=(deadline, limit), name="theodore-outbox", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))


def _flush(deadline: float, limit: int) -> None:
    try:
        from . import account, api, credentials
        cred = credentials.load()
        if not cred or core.refused():
            return
        with core.lock("outbox", timeout=0.5):         # another process is sending: it sends for both
            sent = 0
            for path, record in _entries():
                if sent >= limit or deadline - time.monotonic() < 0.5:
                    break
                if float(record.get("not_before") or 0) > core.now():
                    continue
                payload = record["payload"]
                report_id = path.stem
                if payload.get("origin") != "user_requested":
                    allowed, why = account.automatic_reports()
                    if why in ("off", "plan"):
                        core.log(f"outbox: automatic report {report_id} removed unsent: automatic reports {why}")
                        core.remove_tree(path)
                        continue
                    if not allowed:         # not known just now: it waits for the next licence refresh
                        continue
                sent += 1
                try:
                    _status, _headers, raw = api.call("reports", bearer=cred["installation_secret"], body=payload,
                                                      timeout=max(0.5, min(SEND_TIMEOUT_S, deadline - time.monotonic())),
                                                      exact_timeout=True, learn_refusals=False)
                except (api.ApiError, api.NetworkError) as error:
                    if later(error):
                        _later(error)
                        core.log(f"outbox: the service is not taking reports yet ({error}); {report_id} waits")
                        return
                    if isinstance(error, api.ApiError) and error.code == "auto_reports_disabled":
                        account.remember_automatic_reports_off()
                    core.log(f"outbox: report {report_id} refused ({error}); removed")
                    core.remove_tree(path)
                    continue
                except api.TooLarge:
                    core.remove_tree(path)
                    continue
                with contextlib.suppress(Exception):
                    answer = json.loads(raw.decode("utf-8") or "{}")
                    report_id = f"{report_id} as {str(answer.get('ref') or answer.get('id') or 'received')[:64]}"
                core.remove_tree(path)
                core.log(f"outbox: report {report_id} delivered")
    except Exception as exc:        # noqa: BLE001  a flush never makes a command fail
        with contextlib.suppress(Exception):
            core.log(f"outbox: flush stopped ({exc.__class__.__name__}: {exc})")


def _later(error: Exception) -> None:
    """The service said "later": every waiting report waits until the new earliest time."""
    not_before = core.now() + _wait_s(error)
    for path, record in _entries():
        record["not_before"] = max(float(record.get("not_before") or 0), not_before)
        with contextlib.suppress(OSError):
            core.write_json(path, record, 0o600)
