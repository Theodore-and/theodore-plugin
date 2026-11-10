"""Deck-design specimen sheets. They are the publisher's images: fetched one at a time when the assistant asks,
held in a private temporary folder of this run for at most an hour, and never put in Theodore's folder or in a deck's.

`theodore specimen <file>...` fetches into a new folder <temp>/theodore-specimens-XXXXXXXX (the current user's
alone). Every client call first removes such folders older than an hour; `uninstall` removes them all.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import subprocess
import tempfile
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


def roots() -> list[Path]:
    """The temporary folders of this computer where a specimen folder may be: the system's choice first."""
    candidates = [tempfile.gettempdir()]
    if core.IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "Temp"))
    else:
        candidates += ["/tmp", "/var/tmp"]
    found: list[Path] = []
    for candidate in candidates:
        with contextlib.suppress(OSError, RuntimeError):
            path = Path(candidate).resolve()
            if path.is_dir() and path not in found:
                found.append(path)
    return found


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _usable(root: Path) -> bool:
    """Not inside Theodore's folder, not inside a deck (a folder holding .pptx-state), and not inside the folder the
    assistant works in, unless that folder holds the user's home (then it is no project: a session started in the
    home folder or above it, where the system's temporary folder often is)."""
    with contextlib.suppress(OSError, RuntimeError):
        if _within(root, core.home().root.resolve()):
            return False
        if any((folder / ".pptx-state").is_dir() for folder in (root, *root.parents)):
            return False
        here = Path.cwd().resolve()
        if _within(root, here) and not _within(Path.home().resolve(), here):
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
    """A new folder for this run's sheets. No private folder outside Theodore's and the deck's: no sheet is fetched."""
    problem = "no temporary folder outside Theodore's folder and the working folder"
    for root in roots():
        if not _usable(root):
            continue
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
            return folder
        except (OSError, subprocess.SubprocessError) as exc:
            core.remove_tree(folder)
            problem = f"a folder in {root} could not be made private ({exc})"
    raise Status("INTERNAL", f"the specimen sheet was not fetched: {problem}.",
                 "continue without the sheet; do not fetch it another way.",
                 "I could not open a design specimen on this computer; I am continuing without it.")


def write(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
        handle.write(data)
    return path


def _folders() -> list[Path]:
    """Theodore's specimen folders in the temporary folders: real folders (never a link), this user's own."""
    found = []
    for root in roots():
        with contextlib.suppress(OSError):
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.name.startswith(PREFIX):
                        continue
                    with contextlib.suppress(OSError):
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode) and (core.IS_WINDOWS or info.st_uid == os.getuid()):
                            found.append((Path(entry.path), info.st_mtime))
    return found


def sweep(everything: bool = False) -> list[Path]:
    """Remove the specimen folders older than an hour (or dated ahead of the clock); with `everything`, all of them.
    Returns the folders that should be gone and are still there. Never raises: it runs before every command."""
    left = []
    try:
        now = core.now()
        for folder, made in _folders():
            if everything or not 0 <= now - made <= KEEP_SECONDS:
                with contextlib.suppress(OSError):
                    core.remove_tree(folder)
                if folder.exists():
                    left.append(folder)
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
