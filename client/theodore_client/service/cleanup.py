"""What an earlier Theodore fetched into its own folder and Theodore no longer uses, removed by the service (0.5.1).

Before 0.5.0 Theodore fetched LibreOffice into `<home>/tools/libreoffice` (about 800 MB) on a computer that had none;
engine 2.0 draws every slide itself and calls no LibreOffice, so the copy only takes space. At each start the service
looks once; when it is there it is removed in the background, a few hundred entries at a time, pausing while any call
runs, and the space freed goes in service.log. It ends where a command would start, and an unfinished removal goes on
at the next start.

Only that folder (and the `.partial` one an interrupted fetch left beside it), only inside Theodore's own tools folder:
its real path must sit directly in the tools folder's real path, a link there (a symbolic link, a Windows junction or
any reparse point) is refused and left as it is, and links inside it are removed as links, never followed. A
LibreOffice installed on the computer, or anything outside the tools folder, is never touched. A failure is logged and
never stops the service or a session.
"""

from __future__ import annotations

import contextlib
import os
import stat
import threading
import time
from pathlib import Path

NAMES = ("libreoffice", "libreoffice.partial")     # what a Theodore before 0.5.0 fetched into <home>/tools
BATCH = 200                                        # entries removed between looks at whether a call runs
QUIET_POLL_S = 0.2
_REPARSE_POINT = 0x400                             # Windows FILE_ATTRIBUTE_REPARSE_POINT: a junction, a link


def start(service) -> threading.Thread:
    """The removal, in a thread of its own: the service's start never waits for it."""
    thread = threading.Thread(target=run, args=(service,), name="service-cleanup", daemon=True)
    thread.start()
    return thread


def run(service) -> None:
    for name in NAMES:
        try:
            _remove(service, Path(service.home) / "tools", name)
        except Exception as exc:  # noqa: BLE001  never a session's problem
            with contextlib.suppress(Exception):
                service.log(f"cleanup: {name} in Theodore's tools folder not removed ({type(exc).__name__}: {exc}); "
                            f"the next start tries again")


def _is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _remove(service, tools: Path, name: str) -> None:
    target = tools / name
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return
    if _is_link(st):
        service.log(f"cleanup: {target} is a link, not a folder Theodore fetched: left as it is")
        return
    real_tools = os.path.normcase(os.path.realpath(tools))
    real = os.path.realpath(target)
    if os.path.normcase(os.path.dirname(real)) != real_tools:
        service.log(f"cleanup: {target} leads outside Theodore's tools folder ({real}): left as it is")
        return
    _quiet(service)
    if stat.S_ISDIR(st.st_mode):
        freed, failed = _delete_tree(service, real)
    else:
        freed, failed = st.st_size, 0
        try:
            _unlink(real)
        except OSError:
            freed, failed = 0, 1
    gone = not os.path.lexists(real)
    if gone:
        service.log(f"cleanup: removed {target}, which an earlier Theodore fetched and Theodore no longer uses; "
                    f"{freed / 1e6:.0f} MB freed")
    elif not service.stopping.is_set() or failed:
        service.log(f"cleanup: {target} only partly removed ({freed / 1e6:.0f} MB freed, {failed} entries left in "
                    f"use or refused); the next start goes on")


def _quiet(service) -> None:
    """Wait while any call runs (it goes first), or until the service stops."""
    while not service.stopping.is_set() and getattr(service.router, "running", 0):
        time.sleep(QUIET_POLL_S)


def _delete_tree(service, top: str) -> tuple[int, int]:
    """Remove `top` and everything in it, children first, never following a link. (bytes freed, entries not removed)"""
    freed = failed = seen = 0
    stack: list[tuple[str, bool]] = [(top, False)]
    while stack:
        if service.stopping.is_set():
            break
        path, emptied = stack.pop()
        if emptied:
            try:
                os.rmdir(path)
            except OSError:
                failed += 1
            continue
        stack.append((path, True))
        try:
            with os.scandir(path) as entries:
                listed = list(entries)
        except OSError:
            failed += 1
            continue
        for entry in listed:
            seen += 1
            if seen % BATCH == 0:
                _quiet(service)
            try:
                st = entry.stat(follow_symlinks=False)
                if _is_link(st):
                    _unlink_link(entry.path, st)       # the link itself, never what it leads to
                elif stat.S_ISDIR(st.st_mode):
                    stack.append((entry.path, False))
                else:
                    _unlink(entry.path)
                    freed += st.st_size
            except OSError:
                failed += 1
    return freed, failed


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except PermissionError:                     # a read-only file (Windows): writable first, then removed
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        os.unlink(path)


def _unlink_link(path: str, st: os.stat_result) -> None:
    try:
        os.unlink(path)
    except OSError:
        if stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
            os.rmdir(path)                      # a Windows junction or directory link: the link, not its target
        else:
            raise
