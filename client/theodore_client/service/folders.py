"""A session's folder, watched while the session is open for decks that appear or change (lane BIG's template facts
ahead; the coordinator, 25 Nov).

The session's opening primes its folder once (Pool.prime_folder): a cheap call answered there, after which the engine
makes the template facts of the folder's decks between calls (BIG's facts_ahead: the folder's own decks, the 8 most
recent, each at most 200 MB, a file written in the last 2 s left for later, in a process at idle priority ended within
25 ms of a call starting, kept by template digest). A deck copied into the folder after that waited for its own
`start --from` to read it. Now each one is told the same way: when a .pptx or .potx in the folder itself (never a
subfolder) is new or changed and has stopped changing, the folder is primed again.

The look is a poll every POLL_S: the folder's own mtime (an entry added, removed or renamed; and a whole listing every
LIST_EVERY_S), and between listings the size and mtime of the decks it already knows (a deck overwritten in place leaves
the folder's mtime as it was). Platform change notifications would save at most one POLL_S here, against a directory
handle held open on Windows for the session's life; the poll is a few stat calls. It reads
no file and hashes nothing, so a cloud placeholder is never fetched. Decks Theodore wrote are not news: a deck whose
mtime falls while a call of the service ran (or within OURS_GRACE_S after) is taken for the call's own output (that
call's answer already started the folder's facts). Folders inside Theodore's home are not watched. The watch ends with
the last session open in the folder.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

POLL_S = float(os.environ.get("THEODORE_FOLDER_POLL_S") or 2.0)
LIST_EVERY_S = 20.0               # a whole listing at least this often (a folder mtime too coarse to see two changes)
QUIET_NS = 2_000_000_000          # the engine lists a deck once it is 2 s unwritten (facts_ahead.decks)
OURS_GRACE_S = 2.0
KNOWN_MOST = 256                  # decks of one folder looked at between listings
SUFFIXES = (".pptx", ".potx")


def _decks(folder: str) -> dict[str, tuple[int, int]]:
    """{path: (size, mtime_ns)} of the folder's own decks (no subfolder, no Office lock file, nothing hidden)."""
    found: dict[str, tuple[int, int]] = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name.startswith(("~$", ".")) or not entry.name.lower().endswith(SUFFIXES):
                continue
            with contextlib.suppress(OSError):
                if entry.is_file():
                    st = entry.stat()
                    found[entry.path] = (st.st_size, st.st_mtime_ns)
                    if len(found) >= KNOWN_MOST:
                        break
    return found


class _Watch:
    def __init__(self, watches: "FolderWatches", folder: str):
        self.watches = watches
        self.folder = folder
        self.sessions: set[str] = set()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="service-folder-watch", daemon=True)

    def _run(self) -> None:
        service = self.watches.service
        try:
            folder_mtime = os.stat(self.folder).st_mtime_ns
            known = _decks(self.folder)          # decks there at the opening: its priming covers them
        except OSError:
            return
        pending: dict[str, tuple[int, int]] = {}   # new or changed decks, until they stop changing
        due = False                                # a deck to tell the engine of, once no call runs
        listed = time.monotonic()
        while not self.stopped.wait(POLL_S):
            try:
                mtime = os.stat(self.folder).st_mtime_ns
                if mtime != folder_mtime or time.monotonic() - listed >= LIST_EVERY_S:
                    folder_mtime, now, listed = mtime, _decks(self.folder), time.monotonic()
                else:                              # no entry came or went: the known decks, each by its stat
                    now = {}
                    for path in known:
                        with contextlib.suppress(OSError):
                            st = os.stat(path)
                            now[path] = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue                           # the folder is away for now (a network share, a removed drive)
            for path, sig in now.items():
                if known.get(path) != sig:
                    pending[path] = sig            # seen changing: settled once a look finds it the same
                elif path in pending and time.time_ns() - sig[1] >= QUIET_NS:
                    del pending[path]
                    if _ours(service, sig[1]):
                        service.log(f"folder watch: {os.path.basename(path)} written by a call of Theodore, not news")
                    else:
                        due = True
                        service.log(f"folder watch: {os.path.basename(path)} is new or changed in {self.folder}")
            for path in list(pending):
                if path not in now:
                    del pending[path]
            known = now
            if due and service.pool is not None and service.pool.prime_folder(self.folder, wait_s=0,
                                                                             why="a deck new or changed there"):
                due = False                        # else a call runs: the next look tries again


def _ours(service, mtime_ns: int) -> bool:
    try:
        return service.router.busy_at(mtime_ns / 1e9, OURS_GRACE_S)
    except Exception:  # noqa: BLE001
        return False


class FolderWatches:
    """The folders the open sessions work in, each watched once whatever the number of sessions there."""

    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()
        self._watches: dict[str, _Watch] = {}

    def open(self, session_id: str, folder: str | None) -> None:
        if not folder or not os.path.isabs(folder):
            return
        try:
            real = os.path.realpath(folder)
            if not os.path.isdir(real):
                return
            home = os.path.realpath(str(self.service.home))
            if os.path.normcase(real) == os.path.normcase(home) or \
                    os.path.normcase(real).startswith(os.path.normcase(home) + os.sep):
                return                             # Theodore's own home: its workspaces and outputs
        except (OSError, ValueError):
            return
        key = os.path.normcase(real)
        with self._lock:
            watch = self._watches.get(key)
            started = watch is None
            if started:
                watch = self._watches[key] = _Watch(self, real)
            watch.sessions.add(session_id)
        if started:
            watch.thread.start()
            self.service.log(f"folder watch: watching {real} for decks while its session is open")

    def close(self, session_id: str) -> None:
        ended = []
        with self._lock:
            for key, watch in list(self._watches.items()):
                watch.sessions.discard(session_id)
                if not watch.sessions:
                    ended.append(self._watches.pop(key))
        for watch in ended:
            watch.stopped.set()
            self.service.log(f"folder watch: stopped watching {watch.folder} (its last session closed)")

    def stop(self) -> None:
        with self._lock:
            ended, self._watches = list(self._watches.values()), {}
        for watch in ended:
            watch.stopped.set()
