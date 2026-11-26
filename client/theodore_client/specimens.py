r"""Deck-design specimen sheets: names, checks and the sweep of the folders older clients made.

Sheets are the publisher's images. Since contract C6 (24 November 2026) they come online on every call, through this
process's Texts (service/texts.py): as image blocks from the MCP tools, or, in a shell, as transient files in
<work>/scratch/sheets/<session>/, removed when the session ends or after 15 minutes. Nothing here fetches.

Clients up to 0.4.33 fetched each sheet into a folder <root>/theodore-specimens-XXXXXXXX (the user's cache folder, or
the system's temporary folder) recorded in <home>/state/specimens.txt, which the launchers and installers read too.
`sweep` still removes those folders (recorded, or found in the roots by their name) once they are older than an hour,
and all of them at an uninstall or after a refusal of the device or the account.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import core
from .core import Status

secrets = core.LazyModule("secrets")         # only when sheets are written
tempfile = core.LazyModule("tempfile")

PREFIX = "theodore-specimens-"
KEEP_SECONDS = 3600
AHEAD_SECONDS = 60              # dated further ahead of the clock than this: not trusted to be recent (the launchers allow the same)
MAX_FILES = 8
MAX_BYTES = 1 << 20
NAME = re.compile(r"[A-Za-z0-9._-]{1,80}\.jpg")
CODE = re.compile(r"[A-Z]{2}[0-9]{1,3}")          # a close-up's code, as the texts print it under a drawing: HC3 is HC3.jpg
LINK = re.compile(r"\]\(specimens/([A-Za-z0-9._-]{1,80}\.jpg)\)")
# A worked example's image (0.4.32): served by its exact name, never listed in the published index. The worked-notes
# text names each one. So a name of this form that the service does not have is never matched against the index, whose
# names are all of other kinds: a nearest name from it would be a wrong sheet offered with confidence.
WORKED = re.compile(r"worked-[a-z0-9-]+\.jpg")
# How a text links a sheet: `specimens/<file>.jpg`. A reader may give the link's path as it is (0.4.32).
LINK_FOLDER = ("./specimens/", "specimens/")
# Names Windows gives to devices, whatever follows the first dot: never a file name here, on any system.
DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


# The service ties each sheet it hands out to the account and installation it went to (lane PROTECT2): a JPEG comment
# segment (FF FE) among the header segments (after the leading APPn ones), holding MARK_PREFIX and an opaque token. Decoders skip comment
# segments, so the image the assistant sees is the published one to the pixel; only the file's bytes differ per account.
# Integrity is checked twice: the response's X-Theodore-Sha256 covers the bytes as they arrived, and the bytes without
# that one segment must be the published sheet (its ETag, the quoted SHA-256 the service stores). If-None-Match names
# that published hash, so a held sheet is still answered 304.
MARK_PREFIX = b"theodore-mark/1 "


def unmarked(raw: bytes) -> bytes:
    """The sheet without the service's mark segment (the first comment segment holding MARK_PREFIX, among the segments
    before the image data); anything else unchanged."""
    if raw[:2] != b"\xff\xd8":
        return raw
    offset = 2
    while offset + 4 <= len(raw) and raw[offset] == 0xFF:
        marker = raw[offset + 1]
        if marker in (0xD9, 0xDA) or marker == 0xFF or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            return raw                  # the image data, the end, or no segment here: no mark in the header
        length = int.from_bytes(raw[offset + 2:offset + 4], "big")
        if length < 2 or offset + 2 + length > len(raw):
            return raw
        if marker == 0xFE and raw[offset + 4:offset + 4 + len(MARK_PREFIX)] == MARK_PREFIX:
            return raw[:offset] + raw[offset + 2 + length:]
        offset += 2 + length
    return raw


def content_sha256(raw: bytes) -> str:
    """The published sheet's SHA-256: the one its ETag names, whatever account the copy was marked for."""
    import hashlib
    return hashlib.sha256(unmarked(raw)).hexdigest()


def content_matches(raw: bytes, etag: str | None) -> bool:
    """True unless the answer names its sheet (ETag) and these bytes, unmarked, are another one."""
    tag = str(etag or "").strip().removeprefix("W/").strip('"').lower()
    return not re.fullmatch(r"[0-9a-f]{64}", tag) or content_sha256(raw) == tag


def file_name(name):
    """A bare code names its close-up's file; a name given as the texts link it (`specimens/<file>.jpg`) is that file;
    any other name is left as it is (and checked by valid_name)."""
    if isinstance(name, str):
        for folder in LINK_FOLDER:
            if name.startswith(folder):
                name = name[len(folder):]
                break
    return f"{name}.jpg" if isinstance(name, str) and CODE.fullmatch(name) else name


def valid_name(name: str) -> bool:
    return bool(NAME.fullmatch(name)) and name.split(".", 1)[0].upper() not in DEVICES


class NoSuchSheet(Exception):
    """The service has no sheet of this name (its 404): said beside the sheets that were delivered, never instead of them."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


FAMILY = re.compile(r"([A-Za-z]{1,4})([0-9]{1,3})")       # a code-like name: letters, then digits (AX9)


def _words(stem: str) -> set[str]:
    return {word for word in re.split(r"[-_.]+", stem.lower()) if word}


def nearest(name: str, names: list[str] | None, most: int = 3) -> list[str]:
    """The real sheet names nearest to `name`, nearest first. A code-like name (AX9): the codes of its family that exist,
    the closest numbers first. Any other: the names that share the most of its words in any order, then the closest
    spellings."""
    if not names:
        return []
    stem = name[:-4] if name.lower().endswith(".jpg") else name
    code = FAMILY.fullmatch(stem)
    if code:
        family, number = code.group(1).upper(), int(code.group(2))
        same = [(int(m.group(2)), n) for n in names if (m := FAMILY.fullmatch(n[:-4])) and m.group(1).upper() == family]
        if same:
            return [n for _num, n in sorted(same, key=lambda item: (abs(item[0] - number), item[0]))]
    import difflib
    asked = _words(stem)
    scored = []
    for candidate in names:
        theirs = _words(candidate[:-4])
        shared = len(asked & theirs) / len(asked | theirs) if asked | theirs else 0.0
        spelling = difflib.SequenceMatcher(None, stem.lower(), candidate[:-4].lower()).ratio()
        if shared >= 0.5 or spelling >= 0.6:
            scored.append((-shared, -spelling, candidate))
    return [candidate for _s, _p, candidate in sorted(scored)[:most]]


def missing_note(name: str, names: list[str] | None) -> str:
    """One line for a name that is not a sheet, with the nearest real names when the list of sheets is known. A worked
    example's image is not in that list (WORKED): its line names no other sheet and says where the exact names are."""
    if WORKED.fullmatch(name):
        return f"no such sheet: {name}; the worked-notes text gives each worked image's exact name"
    near = nearest(name, names)
    code = FAMILY.fullmatch(name[:-4] if name.lower().endswith(".jpg") else name)
    if near and code and FAMILY.fullmatch(near[0][:-4]):
        number = int(code.group(2))
        ordered = sorted(near, key=lambda n: int(FAMILY.fullmatch(n[:-4]).group(2)))
        below = [n for n in ordered if int(FAMILY.fullmatch(n[:-4]).group(2)) < number][-1:]
        above = [n for n in ordered if int(FAMILY.fullmatch(n[:-4]).group(2)) > number][:1]
        family = FAMILY.fullmatch(near[0][:-4]).group(1).upper()
        return (f"no such sheet: {name}; nearest: {', '.join(below + above)} "
                f"(the {family} codes that exist: {', '.join(n[:-4] for n in ordered)})")
    if near:
        return f"no such sheet: {name}; nearest: {', '.join(near)}"
    if names:
        return f"no such sheet: {name}; no sheet has a similar name"
    return f"no such sheet: {name}"


def none_delivered(notes: list[str], again: str) -> Status:
    """No sheet of the call exists: the nearest names, and how to ask for them (`again`: the command or the tool)."""
    return Status("USAGE", f"no sheet was delivered: {'. '.join(notes)}.",
                  f"{again} with the nearest name if it is the sheet you meant; the sheet names are the .jpg links in the "
                  f"deck-design text. Otherwise continue without it.",
                  "A design specimen I asked for has another name; I am looking for the right one.")


def cache_root() -> Path:
    """The user's cache folder for specimen folders: the root used when the temporary folder is inside the working folder."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "Theodore" / "specimens"
    if core.IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Theodore" / "Cache" / "specimens"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "theodore" / "specimens"


def roots() -> list[Path]:
    """Where a specimen folder may be, in order: the system's temporary folder, then the user's cache folder (made
    when it is used)."""
    candidates = [tempfile.gettempdir()]
    if core.IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "Temp"))
    candidates.append(str(cache_root()))
    found: list[Path] = []
    for candidate in candidates:
        with contextlib.suppress(OSError, RuntimeError):
            path = Path(candidate).resolve()
            if path not in found:
                found.append(path)
    return found


def registry() -> Path:
    return core.home().state / "specimens.txt"


# The record's format: a first line "#v2", then "<created epoch> <path>" per folder, the path with "%", carriage return
# and line feed written as %25, %0D and %0A (so any path fits on one line). A record without the first line is the
# First format (paths as they are) and is still read. The launchers' shell and batch readers need no decoding: they
# remove only folders that exist under the name as written, and an encoded name never does.
REGISTRY_HEADER = "#v2"


def _encode(path: Path) -> str:
    return str(path).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _decode(text: str) -> str:
    return re.sub(r"%(25|0[ADad])", lambda m: {"25": "%", "0A": "\n", "0D": "\r"}[m.group(1).upper()], text)


def _read_registry() -> list[tuple[float, Path]]:
    raw = ""
    with contextlib.suppress(OSError):
        raw = registry().read_text(encoding="utf-8")
    lines = raw.split("\n")
    encoded = bool(lines) and lines[0].rstrip("\r") == REGISTRY_HEADER
    entries = []
    for line in lines[1:] if encoded else lines:
        line = line.rstrip("\r") if encoded else line.strip()
        made, _, path = line.partition(" ")
        with contextlib.suppress(ValueError):
            if path:
                entries.append((float(made), Path(_decode(path) if encoded else path)))
    return entries


def _write_registry(entries: list[tuple[float, Path]]) -> None:
    if entries:
        core.write_atomic(registry(), REGISTRY_HEADER + "\n" + "".join(f"{int(made)} {_encode(path)}\n" for made, path in entries), 0o600)
    else:
        core.remove_tree(registry())


def _make_private(folder: Path) -> None:
    """The current user alone. POSIX: mode 0700. Windows: the folder's access list is replaced by one entry, this
    user with full control, inherited by the files, and nothing is inherited from the parent (icacls /inheritance:r
    leaves entries a parent's own list handed down as explicit ones, so the list is set as a whole)."""
    if not core.IS_WINDOWS:
        os.chmod(folder, 0o700)
        return
    import ctypes
    from ctypes import wintypes
    system = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"
    who = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"], creationflags=0x08000000,
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    sid = who.stdout.strip().rsplit(",", 1)[-1].strip().strip('"') if who.returncode == 0 else ""
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise OSError("the current user's identifier could not be read")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(f"D:P(A;OICI;FA;;;{sid})", 1, ctypes.byref(descriptor), None):
        raise OSError(f"the access list could not be built (error {ctypes.get_last_error()})")
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present.value:
            raise OSError("the access list could not be read back")
        # SE_FILE_OBJECT; DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION: this list, nothing inherited
        code = advapi.SetNamedSecurityInfoW(str(folder), 1, 0x00000004 | 0x80000000, None, None, dacl, None)
        if code:
            raise OSError(f"the access list could not be set (error {code})")
    finally:
        kernel.LocalFree(descriptor)


def local_block(status: Status) -> Status:
    """A BLOCKED_BY_POLICY that this computer decided about this one call (automatic reports are off): not a refusal of
    the device or the account, so it removes no sheet. Until 0.4.30 it did (core.emit), and a designer refused in one
    folder emptied the folders of the designers working beside it at that moment: a path printed a second before was
    gone when it was opened."""
    status.keeps_sheets = True
    return status


def _ours(path: Path) -> float | None:
    """The folder's modification time when it is one of Theodore's specimen folders: the name, a real folder (never
    a link), this user's own. None otherwise."""
    if not path.name.startswith(PREFIX):
        return None
    with contextlib.suppress(OSError):
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode) and (core.IS_WINDOWS or info.st_uid == os.getuid()):
            return info.st_mtime
    return None


def _folders() -> list[tuple[Path, float]]:
    """Theodore's specimen folders in the current roots, with their modification times."""
    found = []
    for root in roots():
        with contextlib.suppress(OSError):
            with os.scandir(root) as entries:
                for entry in entries:
                    made = _ours(Path(entry.path))
                    if made is not None:
                        found.append((Path(entry.path), made))
    return found


# A command's own sweep (quick) reads the record alone, with no lock, and sweeps only when a folder it names is past its
# hour: every folder a call makes is recorded before a sheet goes in, so the record finds them all, at the same moment as
# before. The listing of the temporary folders, for one no record names (a record lost, an earlier client), runs at every
# command that is no engine verb, and on the verbs' path at most every SCAN_EVERY_S (state/specimens-scan): listing the
# temporary folder (thousands of entries) and its probe file cost 20 to 100 ms a verb on Windows.
SCAN_EVERY_S = 600
SCAN_STAMP = "specimens-scan"


def _scan_due(now: float) -> bool:
    try:
        last = float(core.read_text(core.home().state / SCAN_STAMP) or 0)
    except ValueError:
        return True
    return not 0 <= now - last < SCAN_EVERY_S


def _expired_on_record(now: float) -> bool:
    return any(now - made > KEEP_SECONDS or made - now > AHEAD_SECONDS for made, _folder in _read_registry())


def sweep(everything: bool = False, quick: bool = False) -> list[Path]:
    """Remove the specimen folders older than an hour (or dated more than a minute ahead of the clock); with `everything`,
    all of them.
    Both what the registry records (wherever it is: a changed TMPDIR cannot strand one) and what is in the current
    roots. Returns the folders that should be gone and are still there. Never raises: it runs before every command.
    `quick` (an engine verb's path): nothing but a read of the record while no recorded folder has expired and the
    roots were listed less than SCAN_EVERY_S ago (see above)."""
    left: list[Path] = []
    try:
        now = core.now()
        if quick and not everything and not _expired_on_record(now) and not _scan_due(now):
            return left
        # A folder a moment ahead of `now` is one another Theodore call made, or put a sheet in, after this call read
        # the time (it then waits for the record's lock and reads the folders): never expired. Removed as "dated ahead",
        # it went from under that call's next sheet (TR-S9N9N8: INTERNAL FileNotFoundError naming a sheet of the batch).
        expired = lambda made: everything or now - made > KEEP_SECONDS or made - now > AHEAD_SECONDS
        remove = []
        if registry().exists():
            with core.lock("specimens", timeout=10 if everything else 2):
                kept = []
                for made, folder in _read_registry():
                    if _ours(folder) is None:
                        continue                         # gone already, or never ours to delete: the line goes
                    if expired(made):
                        remove.append(folder)
                    else:
                        kept.append((made, folder))
                for folder in remove:
                    with contextlib.suppress(OSError):
                        core.remove_tree(folder)
                    if folder.exists():
                        left.append(folder)
                        kept.append((now - KEEP_SECONDS - 1, folder))      # tried again at the next call
                _write_registry(kept)
        for folder, made in _folders():
            if folder not in remove and expired(made):
                with contextlib.suppress(OSError):
                    core.remove_tree(folder)
                if folder.exists() and folder not in left:
                    left.append(folder)
        if everything:              # the cache root and its Theodore folder are Theodore's: they go when empty
            for empty in (cache_root(), cache_root().parent):
                with contextlib.suppress(OSError):
                    empty.rmdir()
        elif core.home().state.is_dir() and _scan_due(now):        # written at most every SCAN_EVERY_S
            core.write_atomic(core.home().state / SCAN_STAMP, f"{int(now)}\n", 0o600)
    except Exception:       # noqa: BLE001  a cleanup must not stop the command it precedes
        pass
    sweep_sheet_files(everything)
    return left


# --- the shell route's sheet files (contract C6) ----------------------------------------------------------------------
SHEET_FILES_KEEP_S = 15 * 60       # a sheet file lives 15 minutes after its last use, or until its session ends


def sheets_root() -> Path:
    """Where the shell route's sheet files are: <work>/scratch/sheets/<session>/ (<work> is THEODORE_WORK_DIR, else
    <home>/work), Theodore's own folder, never the working folder or the system's temporary folder."""
    work = os.environ.get("THEODORE_WORK_DIR")
    return (Path(work) if work else core.home().root / "work") / "scratch" / "sheets"


def sheet_file_recent(mtime: float, now: float) -> bool:
    """Written or used within the last 15 minutes. A file just written can be dated a little ahead of the clock (on
    Windows before Python 3.13 time.time() ticks every 15.6 ms while the file system dates finer): up to AHEAD_SECONDS
    ahead counts as recent, as for the specimen folders; further ahead is not trusted."""
    return -AHEAD_SECONDS <= now - mtime <= SHEET_FILES_KEEP_S


def sweep_sheet_files(everything: bool = False) -> None:
    """The sheet files older than 15 minutes go, and the session folders left empty; with `everything` (an uninstall,
    a refusal of the device or the account), all of them. Never raises."""
    try:
        root = sheets_root()
        if not root.is_dir() or root.is_symlink():
            return
        if everything:
            core.remove_tree(root)
            return
        now = core.now()
        for folder in list(root.iterdir()):
            with contextlib.suppress(OSError):
                if folder.is_symlink() or not folder.is_dir():
                    folder.unlink()
                    continue
                for path in list(folder.iterdir()):
                    with contextlib.suppress(OSError):
                        if not sheet_file_recent(path.lstat().st_mtime, now):
                            path.unlink()
                if not any(folder.iterdir()) and not -AHEAD_SECONDS <= now - folder.lstat().st_mtime <= 60:
                    folder.rmdir()
    except Exception:       # noqa: BLE001  a cleanup must not stop the command it precedes
        pass


def guide_links(text: str, launcher: str) -> str:
    """Served deck-design text links its sheets as `](specimens/<file>.jpg)`. The link keeps its words and gets the bare
    file name as target; after a text with such links comes, once, how to look at a sheet. Other text is unchanged."""
    out, count = LINK.subn(lambda match: f"]({match.group(1)})" if valid_name(match.group(1)) else match.group(0), text)
    if out == text:
        return text
    return (out if out.endswith("\n") else out + "\n") + (
        "\n--- Specimen sheets (from Theodore's launcher) ---\n"
        f"The links above that end in .jpg name specimen sheets. To look at a sheet, run {launcher} specimen <file> "
        f"(up to {MAX_FILES} files in one call) and open the path it prints. Open only the sheets you need. "
        "Do not copy or keep the sheets, and do not describe them to the user beyond what the design needs.\n")
