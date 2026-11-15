"""Theodore's licensed files leave this computer when the service ends its access (lane PROTECT2).

When an authenticated call gets a definitive, well-formed refusal from the Theodore service, the engine (every version,
with the PowerPoint manual inside it) and the asset packs are removed from Theodore's own folder, as well as the stored
licence the client already deletes. Definitive means one of:

- 401 `installation_revoked`: this installation was revoked (from the portal, by an admin, by an account deletion or
  a suspension, or a cloud workspace that ended by itself), on any route;
- 403 `account_suspended`, on any route;
- 403 `entitlement_inactive`, on the licence refresh only: there it means the account has no live plan. On a guide or
  sheet route the same code can mean that the plan lacks that one skill, which is no reason to remove the engine.

Never on a network error, a timeout, a 5xx, an answer whose body is not the service's error envelope, an expired
licence (offline) or a clock the engine refuses: none of those reaches this module.

What is removed: `engines/` and `packs/` inside Theodore's folder, and the engine and pack archives a download left in
`staging/`. Never anything else: not the runtime (Python and Node are not Theodore's), not LibreOffice or Git, not the
state (the sign-in that a lifted suspension or a restored plan reuses), not the logs, the person's decks, workspaces or
output folders. A top-level folder that is a link is left alone; inside, a link or a junction is removed as a link and
never followed, so nothing outside Theodore's folder is ever reached.

When: at the end of the command that learned the refusal, and at the start of every later command while it is not done.
Not while a Theodore process runs the engine (each run holds a lock of its own, `engine-run-*`, for its whole length) or
installs an engine or a pack: the removal is then marked and made by the next command. A file Windows reports in use
stays marked the same way; the command never fails because of it.

When access comes back (a licence is stored again: the plan was restored, or the person signed in once more after a
lifted suspension), the first-install setup job starts again, and the next status about the download says, once, why it
is downloading. `theodore logout` is not a refusal: it leaves every file where it is.

The record is `state/withdrawn.json`: {"state": "pending" | "removed" | "restored", "code", "at", "left": [...]}.
"""

from __future__ import annotations

import contextlib
import os
import stat
import threading
from pathlib import Path

from . import core

# code -> the HTTP status it must come with, and whether any route may say it (False: the licence refresh only).
DEFINITIVE = {"installation_revoked": (401, True), "account_suspended": (403, True), "entitlement_inactive": (403, False)}
REFRESH_PATH = "/v1/licence/refresh"
TARGETS = ("engines", "packs")
# The files that make an engine or a pack count as installed go first: a removal cut short never leaves something that
# looks installed but is half there (engine_installed reads .theodore-engine.json, pack_ready reads packs/state.json).
_MARKERS = ("engines/current.json", "engines/dirs.json", "packs/state.json")
RUN_LOCK_PREFIX = "engine-run-"
LOCK = "withdraw"
INSTALL_JOBS = ("setup", "engine-update", "packs")
INSTALL_LOCKS = ("update", "current", "packs-state")


def _path() -> Path:
    return core.home().state / "withdrawn.json"


def record() -> dict:
    found = core.read_json(_path())
    return found if isinstance(found, dict) else {}


def _save(value: dict | None) -> None:
    if value is None:
        with contextlib.suppress(OSError):
            _path().unlink()
        return
    core.write_json(_path(), value, 0o600)


def definitive(http_status: int, code: str, url: str = "", well_formed: bool = True) -> bool:
    """True for a refusal after which this installation's access has ended (see the module's text)."""
    rule = DEFINITIVE.get(code)
    if not well_formed or rule is None or rule[0] != http_status:
        return False
    return rule[1] or url.split("?", 1)[0].endswith(REFRESH_PATH)


def note_refusal(http_status: int, code: str, url: str = "", well_formed: bool = True) -> None:
    """Called for every refusal of this installation's own credential (api._signed_out). Never raises."""
    try:
        if not definitive(http_status, code, url, well_formed):
            return
        with core.lock(LOCK, timeout=30):
            known = record()
            if known.get("state") in ("pending", "removed"):
                return
            _save({"state": "pending", "code": code, "at": int(core.now()), "left": []})
        core.log(f"access ended ({code}): Theodore's engine, manual and packs are to be removed from this computer")
    except Exception as exc:        # noqa: BLE001  never in the way of the refusal itself
        core.log(f"withdraw: not noted ({exc.__class__.__name__})")


def pending() -> bool:
    return record().get("state") == "pending"


# --- the engine runs: one lock each, for the whole run ---------------------------------------------------------------

_RUNS = threading.local()


@contextlib.contextmanager
def engine_run():
    """Held around every engine run (install.run_engine, chatbuild): while any is held, nothing is removed. A run that
    starts once the removal is under way waits for it, then finds the engine gone (and the licence with it)."""
    if getattr(_RUNS, "depth", 0):
        yield
        return
    name = f"{RUN_LOCK_PREFIX}{os.getpid()}-{threading.get_ident()}"
    with core.lock(name, timeout=30):
        with core.lock(LOCK, timeout=120):      # a removal in progress finishes first
            pass
        _RUNS.depth = 1
        try:
            yield
        finally:
            _RUNS.depth = 0
    with contextlib.suppress(OSError):
        (core.home().locks / f"{name}.lock").unlink()


def _busy() -> str | None:
    """Why nothing may be removed now, or None. Lock files of runs that ended are cleared on the way."""
    locks = core.home().locks
    for path in sorted(locks.glob(f"{RUN_LOCK_PREFIX}*.lock")) if locks.is_dir() else []:
        try:
            with core.lock(path.stem, timeout=0):
                pass
        except core.Status:
            return "an engine run is in progress"
        with contextlib.suppress(OSError):
            path.unlink()
    for name in INSTALL_JOBS:
        job = core.Job(name)
        if int(job.read().get("pid") or 0) != os.getpid() and job.alive():
            return f"the {name} job is running"
    return None


# --- the removal -----------------------------------------------------------------------------------------------------

def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if core.IS_WINDOWS:     # a junction is a reparse point that is_symlink does not report on every Python
            return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False
    return False


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except PermissionError:
        if not core.IS_WINDOWS:
            raise
        os.chmod(path, stat.S_IWRITE)       # a read-only file on Windows
        os.unlink(path)


def _delete(path: Path, left: list[str]) -> None:
    """Delete `path` without ever following a link: a link (or junction) is removed itself, a folder is emptied first."""
    try:
        if _is_link(path):
            try:
                _unlink(path)
            except OSError:
                os.rmdir(path)      # a directory link or a junction on Windows
            return
        if path.is_dir():
            with os.scandir(path) as entries:
                children = [Path(entry.path) for entry in entries]
            for child in children:
                _delete(child, left)
            os.rmdir(path)
        elif path.exists():
            _unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        left.append(f"{path} ({exc.__class__.__name__})")


def _inside(root: Path, path: Path) -> bool:
    try:
        return path.resolve().parent == root
    except OSError:
        return False


def _remove(left: list[str]) -> None:
    home = core.home()
    root = home.root.resolve()
    for relative in _MARKERS:
        path = home.root / relative
        if not _is_link(path.parent) and _inside(root, path.parent) and not _is_link(path):
            _delete(path, left)
    for name in TARGETS:
        folder = home.root / name
        if not folder.exists() and not _is_link(folder):
            continue
        if _is_link(folder) or not _inside(root, folder):
            left.append(f"{folder} (a link: left alone, nothing is removed through it)")
            continue
        for child in sorted(folder.glob("*/.theodore-engine.json")) if name == "engines" else []:
            if not _is_link(child.parent):
                _delete(child, left)
        _delete(folder, left)
    staging = home.root / "staging"
    if staging.is_dir() and not _is_link(staging) and _inside(root, staging):
        for child in staging.iterdir():
            if child.name.startswith(("engine-", "pack-")):
                _delete(child, left)


def resume() -> None:
    """Make a removal that is due, when nothing is in the way. Called at the start and at the end of every command and
    after every tool call of the local server. Never raises, never prints."""
    try:
        if getattr(_RUNS, "depth", 0) or not pending():     # never from inside a run of this thread
            return
        why = _busy()
        if why:
            core.log(f"withdraw: postponed ({why})")
            return
        held = []
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(core.lock(LOCK, timeout=5))
                for name in INSTALL_LOCKS:
                    stack.enter_context(core.lock(name, timeout=2))
                known = record()
                if known.get("state") != "pending":
                    return
                why = _busy()           # again, now that a new run must wait for the withdraw lock
                if why:
                    core.log(f"withdraw: postponed ({why})")
                    return
                _remove(held)
                known["left"] = held[:20]
                known["state"] = "pending" if held else "removed"
                known["checked"] = int(core.now())
                _save(known)
        except core.Status:             # a lock is held by another Theodore process: the next command tries again
            core.log("withdraw: postponed (another Theodore process holds a lock)")
            return
        if held:
            core.log(f"withdraw: {len(held)} item(s) could not be removed yet (in use?); retried at the next command: {held[:3]}")
        else:
            core.log("withdraw: Theodore's engine, manual and asset packs were removed from this computer")
    except Exception as exc:        # noqa: BLE001  never in the way of a command
        core.log(f"withdraw: not made ({exc.__class__.__name__}: {exc})")


# --- when access comes back ----------------------------------------------------------------------------------------

def access_back() -> None:
    """A licence from the service was stored: access is active again. A removal not made yet is called off; once made,
    the first-install setup job starts again and the next download status says why, once. Never raises."""
    try:
        with core.lock(LOCK, timeout=30):
            known = record()
            state = known.get("state")
            if state not in ("pending", "removed"):
                return
            from . import install          # imported here: install imports account, which imports this module
            if state == "pending" and install.current_engine():
                _save(None)             # nothing was removed yet: everything stays
                core.log("withdraw: access is active again before the removal; nothing removed")
                return
            _save({"state": "restored", "code": known.get("code"), "at": known.get("at"), "restored_at": int(core.now())})
        core.log("withdraw: access is active again; Theodore's engine and packs download again")
        job = core.Job("setup")
        if not job.alive():
            job.spawn(["setup", "--job"])
    except Exception as exc:        # noqa: BLE001  never in the way of the sign-in or the licence
        core.log(f"withdraw: restore not noted ({exc.__class__.__name__})")


NOTICE_SUMMARY = ("Theodore's access on this computer is active again. Its engine, PowerPoint manual and asset packs were "
                  "removed from this computer when access ended, so they are downloading again, as on a first install.")
NOTICE_TELL = ("Your Theodore access is active again. Theodore removed its engine from this computer while access was ended, "
               "so it is downloading it again; this takes a few minutes.")


def take_notice() -> bool:
    """True once after a restore: the caller says it. The record goes with it."""
    try:
        with core.lock(LOCK, timeout=5):
            if record().get("state") != "restored":
                return False
            _save(None)
            return True
    except Exception:       # noqa: BLE001
        return False


def annotate(status: core.Status) -> core.Status:
    """The refusal's status says that Theodore's own files go too, when they do."""
    with contextlib.suppress(Exception):
        if record().get("state") in ("pending", "removed") and "files" not in status.summary:
            status.summary = (status.summary.rstrip(".") + ". Theodore's engine, its PowerPoint manual and its asset packs are "
                              "removed from this computer as well; they download again once access is active again.")
    return status


def explain_setup(status: core.Status) -> core.Status:
    """A download status after a restore says, once, why Theodore downloads again (core.emit)."""
    if status.name == "SETUP_IN_PROGRESS" and take_notice():
        status.summary = f"{NOTICE_SUMMARY} {status.summary}"
        status.tell_user = NOTICE_TELL
    return status


def notice_line() -> str | None:
    """The same, as one notice line in a printed guide (install.take_announcement)."""
    if take_notice():
        return f"THEODORE NOTICE: {NOTICE_SUMMARY} Tell the user once, in one line; it does not stop this task."
    return None
