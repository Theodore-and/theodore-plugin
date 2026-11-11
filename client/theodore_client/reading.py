"""Required reading: one guide print per command, a record of what was printed, and the build gate.

Why: a host that cuts long command output (Claude Code shows about 2 KB of an output over about 30 KB and saves the
rest to a file) hides most of the guidance when several guide commands run in one shell command, and the assistant
then builds without it. So:

1. One guide print per command. A guide command that starts less than CHAIN_WINDOW_S seconds after another guide
   command run by the same shell command line finished (chained with ; or &&, or a loop) prints no guidance:
   ONE_AT_A_TIME. The command line is identified by the shell that runs it: the launcher's parent process. A shell
   that runs its last command by exec makes that command's launcher the shell itself, so a run whose own launcher
   is the earlier run's parent is the same command line too. Each tool call of a host gets its own shell, so two
   tool calls never match, however quickly they follow each other; a host that keeps one shell for every tool call
   is told apart by the time window alone. A run that overlaps another (parallel tool calls) is always allowed.
   Where the output goes is not checked: hosts capture output through pipes and through regular files alike.
2. A record (state/reading.json, per installation, under the "reading" lock): for each part printed and flushed
   without error its skill, section, part, total and time; the last required-reading plan the service sent
   (X-Theodore-Reading and X-Theodore-Review); specimen fetches (counts only). No guidance text is ever written.
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
import sys
import time
from pathlib import Path

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


_SHELLS = ("cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh.exe", "zsh.exe", "dash.exe")


def _windows_ancestry() -> list[tuple[int, str]]:
    """(pid, executable name) of this process's parent and grandparent (Windows), from a process snapshot."""
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
    table: dict[int, tuple[int, str]] = {}
    try:
        entry = ENTRY32()
        entry.dwSize = ctypes.sizeof(ENTRY32)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), str(entry.szExeFile).lower())
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    out, pid = [], os.getpid()
    for _ in range(2):
        pid = table.get(pid, (0, ""))[0]
        if pid <= 4 or pid not in table:
            break
        out.append((pid, table[pid][1]))
    return out


_POSIX_SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "busybox", "yash", "tcsh", "csh")


def _posix_name(pid: int) -> str:
    """The executable name of a process (POSIX), or "" when it cannot be told."""
    with contextlib.suppress(OSError, ValueError):
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
    if sys.platform == "darwin":
        with contextlib.suppress(Exception):
            import ctypes
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            buf = ctypes.create_string_buffer(256)
            if libproc.proc_name(ctypes.c_int(pid), buf, ctypes.c_uint32(256)) > 0:
                return buf.value.decode("utf-8", "replace")
    with contextlib.suppress(Exception):
        import subprocess
        done = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return done.stdout.strip()
    return ""


def _is_shell(name: str) -> bool:
    base = os.path.basename(name.strip()).lstrip("-").lower()
    return base in _POSIX_SHELLS or base in _SHELLS


def command_line() -> tuple[int, int]:
    """(caller, launcher): the process that stands for the command line, and the launcher that ran the client.

    The launcher is the batch launcher's cmd.exe (Windows) or the sh launcher (POSIX, which passes its own pid and its
    parent's; honoured only when that launcher is really our parent; run without it, the client itself). The caller
    is the launcher's parent when that is a shell (a line of several commands: PowerShell, cmd, bash, sh, zsh), else
    the launcher itself: a host that starts each tool call's shell, which then runs the launcher by exec or as
    `cmd /c <launcher>`, has a non-shell parent shared by every tool call, which must not join them."""
    if core.IS_WINDOWS:
        try:
            ancestry = _windows_ancestry()
        except Exception:       # noqa: BLE001  no snapshot: no chain check
            return 0, 0
        if not ancestry:
            return 0, 0
        launcher = ancestry[0][0]
        if len(ancestry) > 1 and _is_shell(ancestry[1][1]):
            return ancestry[1][0], launcher
        return launcher, launcher
    caller, launcher = os.environ.get("THEODORE_CALLER_PID", ""), os.environ.get("THEODORE_LAUNCHER_PID", "")
    if caller.isdigit() and launcher.isdigit() and int(launcher) == os.getppid():
        parent, own = int(caller), int(launcher)
    else:
        parent, own = os.getppid(), os.getpid()
    if parent > 1 and _is_shell(_posix_name(parent)):
        return parent, own
    return own, own


def guard_guide(argv_text: str) -> None:
    """Before a guide command prints anything: refuse a guide command chained after another in one command line."""
    window = chain_window()
    caller, launcher = command_line()
    if window <= 0 or caller <= 1:
        return
    for run in _load().get("runs") or []:
        try:
            before, end = int(run.get("caller") or 0), float(run.get("end") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if before > 1 and before in (caller, launcher) and end <= STARTED and STARTED - end < window:
            raise Status("ONE_AT_A_TIME", "another guide command ran just before this one in the same shell command (chained with ; or &&, "
                                          "or a loop), so this one printed nothing: the host would cut the combined output and the reading would be lost.",
                         f"run {quoted_launcher()} {argv_text} by itself as its own tool call (one guide command per call, never chained) "
                         "and read all of it, then the next one the same way.",
                         "I am reading Theodore's guidance one part at a time so none of it is cut off.")


def record_run() -> None:
    """After every guide command, printed or refused: when it ended, and which command line ran it."""
    caller, _launcher = command_line()
    if caller <= 1:
        return
    with contextlib.suppress(Exception):
        with _locked():
            data = _load()
            now = time.time()
            runs = [r for r in data.get("runs") or [] if isinstance(r, dict) and now - float(r.get("end") or 0) < _RUNS_KEEP_S]
            runs.append({"caller": caller, "end": now})
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
                      f"run {command} by itself as its own tool call (never chained), read all of it, revise the deck against it, "
                      "then run the same finish command again.",
                      f"Theodore's design method asks me to review the draft against its critique before finishing; I am doing that now. "
                      f"Reading counts for {WINDOW_TEXT} on this computer.")
    return Status("READING_REQUIRED", f"pptx {verb} refused: the design method is in use and {name} has not been read in full yet{partial}; "
                                      "no slide is built or changed before the required reading. Nothing was run.",
                  f"run {command} by itself as its own tool call (never chained) and read all of it, then follow its last line; "
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
                      f"run {_command(key, part)} by itself as its own tool call (never chained) and read all of it.",
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
