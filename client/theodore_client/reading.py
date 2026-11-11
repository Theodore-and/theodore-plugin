"""Required reading: one guide print per command, a record of what was printed, and the build gate.

Why: a host that cuts long command output (Claude Code shows about 2 KB of an output over about 30 KB and saves the
rest to a file) hides most of the guidance when several guide commands run in one shell command, and the assistant
then builds without it. So:

1. One guide print per command. A guide command that starts less than CHAIN_WINDOW_S seconds after another guide
   command of the same caller finished (chained with ; or &&, or a loop) prints no guidance: ONE_AT_A_TIME. A run
   that overlaps another (parallel tool calls) or comes from another caller is allowed. The caller is the shell
   that runs the command line: its process group and the launcher's parent (POSIX), the launcher's parent and
   grandparent (Windows). A host that keeps one shell for every tool call has one caller for all of them; there the
   time window is what tells a chain from the next tool call. Guidance is also never printed into a regular file or
   the null device (OUTPUT_REDIRECTED): that writes licensed text to disk or reads nothing.
2. A record (state/reading.json, per installation, under the "reading" lock): for each printed part its skill,
   section, part, total and time; the last required-reading plan the service sent (X-Theodore-Reading and
   X-Theodore-Review); specimen fetches (counts only). No guidance text is ever written.
3. The gate: when the deck-design entry was printed in the last WINDOW_S seconds (the design method is engaged)
   and the service named required sections, the engine commands that create or change slides (start, apply,
   finish) refuse (READING_REQUIRED) until the entry and every required section are printed in full within the
   window; finish also needs the after-draft review section(s). Without the entry in the window, or from a server
   that sends no plan, nothing is blocked. The record is per installation, so a sub-agent or a new session within
   the window is not asked to read again.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import sys
import time

from . import core
from .contract import HEADER_GUIDE_READING, HEADER_GUIDE_REVIEW, HEADER_GUIDE_SECTION
from .core import Status, quoted_launcher

WINDOW_S = 12 * 3600
WINDOW_TEXT = "12 hours"
CHAIN_WINDOW_S = 2.0
GATED_VERBS = ("start", "apply", "finish")
DESIGN = "deck-design"
ENTRY = "entry"
_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RUNS_KEEP_S = 60.0
_RUNS_MAX = 32

STARTED = time.time()           # cli.main sets it again first thing: when this command started


def _path():
    return core.home().state / "reading.json"


def _load() -> dict:
    data = core.read_json(_path())
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    core.home().ensure()
    core.write_json(_path(), data, 0o600)


@contextlib.contextmanager
def _locked():
    with core.lock("reading", timeout=10):
        yield


# --- 1. one guide print per command -------------------------------------------------------------------------------------

def chain_window() -> float:
    """CHAIN_WINDOW_S. Test seam: THEODORE_TEST_CHAIN_WINDOW (seconds; 0 turns the check off) is honoured only when the
    API base is loopback, because a test runner starts one command after another as a loop would."""
    seam = os.environ.get("THEODORE_TEST_CHAIN_WINDOW")
    if seam is not None:
        with contextlib.suppress(Exception):
            if core.is_loopback_api():
                return float(seam)
    return CHAIN_WINDOW_S


def _windows_parents() -> list[int]:
    """The parent and grandparent process ids of this process (Windows), from a process snapshot."""
    import ctypes
    from ctypes import wintypes

    class ENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ENTRY32)]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ENTRY32)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)          # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    parents: dict[int, int] = {}
    try:
        entry = ENTRY32()
        entry.dwSize = ctypes.sizeof(ENTRY32)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    out, pid = [], os.getpid()
    for _ in range(2):
        pid = parents.get(pid, 0)
        if pid <= 4:
            break
        out.append(pid)
    return out


def caller_keys() -> list[str]:
    """Who ran this command, as far as it can be told: a chained command shares at least one key with the one
    before it."""
    keys: list[str] = []
    if core.IS_WINDOWS:
        with contextlib.suppress(Exception):
            parents = _windows_parents()
            if len(parents) > 0:
                keys.append(f"p:{parents[0]}")
            if len(parents) > 1:
                keys.append(f"gp:{parents[1]}")
        return keys
    with contextlib.suppress(OSError):
        keys.append(f"pg:{os.getpgrp()}")
    caller = os.environ.get("THEODORE_CALLER_PID", "")         # the launcher's parent: the shell running the line
    if caller.isdigit() and int(caller) > 1:
        keys.append(f"pp:{caller}")
    return keys


def _stdout_kind() -> str:
    """"file" when standard output is a regular file, "null" when it is the null device, else "" (a pipe, a terminal)."""
    try:
        fd = sys.stdout.fileno()
        info = os.fstat(fd)
    except (AttributeError, OSError, ValueError):
        return ""
    if stat.S_ISREG(info.st_mode):
        return "file"
    if not core.IS_WINDOWS and stat.S_ISCHR(info.st_mode):
        with contextlib.suppress(OSError):
            null = os.stat(os.devnull)
            if (info.st_rdev, info.st_ino) == (null.st_rdev, null.st_ino):
                return "null"
    return ""


def guard_guide(argv_text: str) -> None:
    """Before a guide command prints anything: refuse a redirected output and a chained command."""
    kind = _stdout_kind()
    if kind:
        where = "a file" if kind == "file" else "the null device"
        raise Status("OUTPUT_REDIRECTED", f"guide output goes to {where}; Theodore's guidance is printed only to the tool's own output, "
                                          "never written to disk or discarded.",
                     f"run {quoted_launcher()} {argv_text} by itself, without > or any redirection, as its own tool call, and read all of it.",
                     "I ran a Theodore command the wrong way; I am running it again correctly.")
    window = chain_window()
    keys = set(caller_keys())
    if window <= 0 or not keys:
        return
    for run in _load().get("runs") or []:
        try:
            shared = keys & set(run.get("keys") or [])
            end = float(run.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if shared and end <= STARTED and STARTED - end < window:
            raise Status("ONE_AT_A_TIME", "another guide command ran just before this one in the same shell command (chained with ; or &&, "
                                          "or a loop), so this one printed nothing: the host would cut the combined output and the reading would be lost.",
                         f"run {quoted_launcher()} {argv_text} by itself as its own tool call (one guide command per call, never chained, "
                         "never redirected) and read all of it, then the next one the same way.",
                         "I am reading Theodore's guidance one part at a time so none of it is cut off.")


def record_run() -> None:
    """After every guide command, printed or refused: when it ended, and who ran it."""
    keys = caller_keys()
    if not keys:
        return
    with contextlib.suppress(Exception):
        with _locked():
            data = _load()
            now = time.time()
            runs = [r for r in data.get("runs") or [] if isinstance(r, dict) and now - float(r.get("end") or 0) < _RUNS_KEEP_S]
            runs.append({"keys": keys, "end": now})
            data["runs"] = runs[-_RUNS_MAX:]
            _save(data)


# --- 2. the record -------------------------------------------------------------------------------------------------------

def _plan_keys(value) -> list[str] | None:
    if value is None:
        return None
    out = []
    for item in str(value).split(","):
        item = item.strip()
        if item and _KEY.match(item) and item not in out and item != ENTRY:
            out.append(item)
    return out


def _prune(data: dict, now: float) -> None:
    since = now - WINDOW_S
    read = data.get("read") if isinstance(data.get("read"), dict) else {}
    for skill in list(read):
        sections = read[skill] if isinstance(read[skill], dict) else {}
        for key in list(sections):
            parts = {p: t for p, t in (sections[key].get("parts") or {}).items() if isinstance(t, (int, float)) and t >= since} \
                if isinstance(sections[key], dict) else {}
            if parts:
                sections[key]["parts"] = parts
            else:
                del sections[key]
        if sections:
            read[skill] = sections
        else:
            del read[skill]
    data["read"] = read
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    data["plan"] = {s: p for s, p in plan.items() if isinstance(p, dict) and float(p.get("at") or 0) >= since}
    data["specimens"] = [e for e in data.get("specimens") or [] if isinstance(e, list) and len(e) == 2 and e[0] >= since]


def record_print(skill: str, section: str | None, part: int, total: int, headers=None) -> None:
    """A guide part was printed in full: remember it (and the plan the service sent with it)."""
    get = (lambda name: headers.get(name)) if headers is not None else (lambda name: None)
    key = str(get(HEADER_GUIDE_SECTION) or "").strip()
    if not _KEY.match(key):
        key = section or ENTRY
    if key == "license":
        return
    reading, review = _plan_keys(get(HEADER_GUIDE_READING)), _plan_keys(get(HEADER_GUIDE_REVIEW))
    with contextlib.suppress(Exception):
        with _locked():
            data = _load()
            now = core.now()
            _prune(data, now)
            sections = data["read"].setdefault(skill, {})
            entry = sections.get(key) if isinstance(sections.get(key), dict) else {}
            if int(entry.get("total") or 0) != total:           # another edition of the section: start again
                entry = {"total": total, "parts": {}}
            entry["parts"][str(part)] = now
            sections[key] = entry
            if reading is not None:
                data["plan"][skill] = {"reading": reading, "review": review or [], "at": now}
            _save(data)


def record_specimens(count: int) -> None:
    with contextlib.suppress(Exception):
        with _locked():
            data = _load()
            now = core.now()
            _prune(data, now)
            data["specimens"].append([now, int(count)])
            _save(data)


# --- 3. the gate ---------------------------------------------------------------------------------------------------------

def _missing_part(sections: dict, key: str) -> int:
    """0 when the section is printed in full within the window, else the first part still to print."""
    entry = sections.get(key)
    if not isinstance(entry, dict):
        return 1
    total = max(1, int(entry.get("total") or 1))
    parts = entry.get("parts") or {}
    for number in range(1, total + 1):
        if str(number) not in parts:
            return number
    return 0


def _state() -> tuple[dict, dict | None, dict]:
    """(design sections read in the window, the plan or None, the whole record) for the gate and `reading`."""
    data = _load()
    _prune(data, core.now())
    return data["read"].get(DESIGN, {}), data["plan"].get(DESIGN), data


def _command(key: str, part: int) -> str:
    section = "" if key == ENTRY else f" {key}"
    return f"{quoted_launcher()} guide {DESIGN}{section}{f' --part {part}' if part > 1 else ''}"


def _first_missing(sections: dict, keys: list[str]) -> tuple[str, int] | None:
    for key in keys:
        part = _missing_part(sections, key)
        if part:
            return key, part
    return None


def gate(verb: str) -> Status | None:
    """READING_REQUIRED when the design method is engaged and its required reading is not complete; else None."""
    if verb not in GATED_VERBS:
        return None
    sections, plan, _data = _state()
    if ENTRY not in sections or plan is None:
        return None
    before = [ENTRY] + list(plan.get("reading") or [])
    missing = _first_missing(sections, before)
    review = False
    if missing is None and verb == "finish":
        missing, review = _first_missing(sections, list(plan.get("review") or [])), True
    if missing is None:
        return None
    key, part = missing
    name = "the deck-design entry" if key == ENTRY else f"the deck-design section '{key}'"
    partial = f" (part {part} onward)" if part > 1 else ""
    command = _command(key, part)
    if review:
        return Status("READING_REQUIRED", f"pptx finish refused: the design method is in use and {name}, the review to read after the first draft, "
                                          f"has not been read{partial}. Nothing was run.",
                      f"run {command} by itself as its own tool call (never chained, never redirected), read all of it, revise the deck against it, "
                      "then run the same finish command again.",
                      f"Theodore's design method asks me to review the draft against its critique before finishing; I am doing that now. "
                      f"Reading counts for {WINDOW_TEXT} on this computer.")
    return Status("READING_REQUIRED", f"pptx {verb} refused: the design method is in use and {name} has not been read in full yet{partial}; "
                                      "no slide is built or changed before the required reading. Nothing was run.",
                  f"run {command} by itself as its own tool call (never chained, never redirected) and read all of it, then follow its last line; "
                  f"when {quoted_launcher()} reading says nothing is missing, run the same pptx command again.",
                  f"Theodore's design method asks me to read its guidance before building slides; I am reading it now. "
                  f"Reading counts for {WINDOW_TEXT} on this computer.")


def verb_of(args: list[str]) -> str:
    """The engine verb: the first argument that is not an option."""
    for arg in args:
        if arg == "--":
            return ""
        if not arg.startswith("-"):
            return arg
    return ""


def python_verb(args: list[str]) -> str:
    """`python <...>/pptx.py <verb> ...` reaches the engine as well: its verb, else ""."""
    for index, arg in enumerate(args):
        if os.path.basename(arg.replace("\\", "/")) == "pptx.py":
            return verb_of(args[index + 1:])
    return ""


def status() -> Status:
    """`theodore reading`: what is still required, as a status. No guidance text."""
    sections, plan, data = _state()
    sheets = sum(int(n) for _t, n in data.get("specimens") or [])
    if ENTRY not in sections:
        return Status("OK", f"the deck-design method has not been started in the last {WINDOW_TEXT}; no reading is required.",
                      "nothing to do.", None)
    if plan is None:
        return Status("OK", "the deck-design method is in use, but the Theodore service named no required sections; nothing is enforced.",
                      "nothing to do.", None)
    before = [ENTRY] + list(plan.get("reading") or [])
    review = list(plan.get("review") or [])
    missing = _first_missing(sections, before)
    if missing:
        key, part = missing
        left = [k for k in before if _missing_part(sections, k)]
        return Status("READING_REQUIRED", f"still to read before drafting: {', '.join(left)}; after the first draft: {', '.join(review) or 'nothing'}. "
                                          f"Specimen sheets fetched: {sheets}.",
                      f"run {_command(key, part)} by itself as its own tool call (never chained, never redirected) and read all of it.",
                      f"Theodore's design method asks me to read its guidance before building slides; I am reading it now. "
                      f"Reading counts for {WINDOW_TEXT} on this computer.")
    after = _first_missing(sections, review)
    if after:
        key, part = after
        return Status("OK", f"every required deck-design section is read ({', '.join(before)}); the review after the first draft is not "
                            f"read yet ({', '.join(k for k in review if _missing_part(sections, k))}). Specimen sheets fetched: {sheets}.",
                      f"build the first draft; before pptx finish, run {_command(key, part)} by itself and revise the deck against it.", None)
    return Status("OK", f"every required deck-design section is read ({', '.join(before)}), and the review ({', '.join(review) or 'none'}). "
                        f"Specimen sheets fetched: {sheets}.", "nothing to do.", None)
