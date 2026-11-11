r"""Deck-design specimen sheets. They are the publisher's images: fetched one at a time when the assistant asks,
held in a private temporary folder of this run, removed by the first Theodore run after an hour (there is no background
process) and by the uninstall, and never put in Theodore's folder or in a deck's.

`theodore specimen <file>...` fetches into a new folder <root>/theodore-specimens-XXXXXXXX (the current user's
alone), where <root> is the system's temporary folder or, when that lies inside the working folder, the user's cache
folder (~/Library/Caches/Theodore/specimens, $XDG_CACHE_HOME/theodore/specimens, %LOCALAPPDATA%\Theodore\Cache\specimens).
Every folder made is recorded in <home>/state/specimens.txt, one line "<created epoch> <path>", which the launchers and
installers read too. Every client call first removes recorded folders, and folders in the current roots, older than an
hour; `uninstall` removes them all. A changed TMPDIR therefore cannot strand a folder.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import core
from .core import Status

PREFIX = "theodore-specimens-"
KEEP_SECONDS = 3600
MAX_FILES = 8
MAX_BYTES = 1 << 20
NAME = re.compile(r"[A-Za-z0-9._-]{1,80}\.jpg")
LINK = re.compile(r"\]\(specimens/([A-Za-z0-9._-]{1,80}\.jpg)\)")
# Names Windows gives to devices, whatever follows the first dot: never a file name here, on any system.
DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def valid_name(name: str) -> bool:
    return bool(NAME.fullmatch(name)) and name.split(".", 1)[0].upper() not in DEVICES


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
# round 7 format (paths as they are) and is still read. The launchers' shell and batch readers need no decoding: they
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


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _usable(root: Path) -> bool:
    r"""Never inside the folder the assistant works in (whatever it holds, the user's home included), never inside a
    deck (a folder holding .pptx-state), never inside Theodore's folder (the cache root that Windows keeps under
    %LOCALAPPDATA%\Theodore\Cache is the one exception: it is made for this)."""
    with contextlib.suppress(OSError, RuntimeError):
        if _within(root, Path.cwd().resolve()):
            return False
        if root != cache_root().resolve() and _within(root, core.home().root.resolve()):
            return False
        if any((folder / ".pptx-state").is_dir() for folder in (root, *root.parents)):
            return False
        return True
    return False


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


def make_folder() -> Path:
    """A new folder for this run's sheets, recorded before anything is put in it. No private folder outside the
    working folder, the deck and Theodore's folder: no sheet is fetched."""
    problem = None
    for root in roots():
        if not _usable(root):
            continue
        if root == cache_root().resolve():
            with contextlib.suppress(OSError):
                root.mkdir(parents=True, exist_ok=True)
                if not core.IS_WINDOWS:
                    os.chmod(root, 0o700)
        folder = root / (PREFIX + secrets.token_hex(8))
        try:        # a new folder: mkdir fails when the name exists, so it is never one somebody else prepared
            if core.IS_WINDOWS:
                os.mkdir(folder)
            else:
                os.mkdir(folder, 0o700)
        except OSError as exc:
            problem = f"{root}: {exc.__class__.__name__}"
            continue
        try:
            _make_private(folder)
        except (OSError, subprocess.SubprocessError) as exc:
            core.remove_tree(folder)
            problem = f"a folder in {root} could not be made private ({exc})"
            continue
        try:
            with core.lock("specimens", timeout=10):
                _write_registry(_read_registry() + [(core.now(), folder)])
        except (OSError, Status) as exc:
            core.remove_tree(folder)
            raise Status("RETRY", f"the specimen folder could not be recorded ({getattr(exc, 'summary', exc)}); nothing was fetched.",
                         "run the same command again once.", "A design specimen could not be opened just now; I am trying once more.") from None
        return folder
    if problem is None:
        here = Path.cwd()
        raise Status("BLOCKED_BY_POLICY", f"the specimen sheet was not fetched: the working folder ({here}) contains every place a "
                                          f"specimen folder may go ({', '.join(str(r) for r in roots())}), and sheets are never put "
                                          f"inside the working folder.",
                     "continue without the sheet; do not fetch it another way. Sheets work when the assistant works in a project "
                     "folder that does not contain the user's temporary or cache folder.",
                     "I could not open a design specimen from this working folder; I am continuing without it.")
    raise Status("INTERNAL", f"the specimen sheet was not fetched: {problem}.",
                 "continue without the sheet; do not fetch it another way.",
                 "I could not open a design specimen on this computer; I am continuing without it.")


def write(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
        handle.write(data)
    return path


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


def sweep(everything: bool = False) -> list[Path]:
    """Remove the specimen folders older than an hour (or dated ahead of the clock); with `everything`, all of them.
    Both what the registry records (wherever it is: a changed TMPDIR cannot strand one) and what is in the current
    roots. Returns the folders that should be gone and are still there. Never raises: it runs before every command."""
    left: list[Path] = []
    try:
        now = core.now()
        expired = lambda made: everything or not 0 <= now - made <= KEEP_SECONDS
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
    except Exception:       # noqa: BLE001  a cleanup must not stop the command it precedes
        pass
    return left


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
