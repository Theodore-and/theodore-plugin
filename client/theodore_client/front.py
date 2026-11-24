"""The native front (DESIGN §2.2, contract C1): its copy in <home>/bin, and the launch.json it reads there.

The plugin carries the front built for each system in its scripts/ folder (theodore-darwin, theodore-linux-x64,
theodore-linux-arm64, Windows_NT/theodore.exe; a plugin may not have a top-level bin/, which claude.ai and Cowork refuse). This
module copies the one for this computer to <home>/bin/theodore-front (theodore-front.exe on Windows), where the short
command and the plugin's MCP entry reach it, and writes <home>/bin/launch.json (C1 launch-json v1): how the front starts
the service and the client. Both are rewritten whenever what they say changes: an install, an update of the plugin,
the client or the engine (refresh(), from those places and from guide, skill and doctor).

On Windows the front is placed only when it carries an Authenticode signature: an unsigned program in the user folder is
what Defender's "block rare executables" rule and SmartScreen stop (PLAN lane C). Until then Windows keeps the launcher
and the client's own MCP server.
"""

from __future__ import annotations

import contextlib
import json
import os
import struct
from pathlib import Path

from . import core

IS_WINDOWS = core.IS_WINDOWS
FRONT_NAME = "theodore-front.exe" if IS_WINDOWS else "theodore-front"
LAUNCH_NAME = "launch.json"


def binary_name() -> str | None:
    """The plugin's file for this system, under its scripts/ folder, or None when there is no front for it (Windows on
    ARM runs the x64 one). The Windows one is in Windows_NT/, the folder the plugin's MCP entry names there."""
    system, arch = core.platform_arch()
    if IS_WINDOWS:
        return "Windows_NT/theodore.exe" if arch in ("x64", "arm64", "amd64") else None
    if system == "darwin":
        return "theodore-darwin"          # one universal file: arm64 and x64
    if system == "linux":
        return {"x64": "theodore-linux-x64", "arm64": "theodore-linux-arm64"}.get(arch)
    return None


def plugin_script_dirs() -> list[Path]:
    """Where a copy of the plugin's scripts/ folder is on this computer: Theodore's own copy first, then the plugin the
    calling skill came from."""
    found = [core.home().plugin / "plugins" / "theodore" / "scripts"]
    skill = os.environ.get("THEODORE_SKILL_DIR")
    if skill:
        found.append(Path(skill).parent.parent / "scripts")
    return found


def signed(path: Path) -> bool:
    """A Windows program that carries an Authenticode signature (its certificate table is not empty). Whether the
    signature is valid is Windows' own check when the program starts; this only keeps an unsigned build out."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if head[:2] != b"MZ" or len(head) < 64:
        return False
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if pe + 24 > len(head) or head[pe:pe + 4] != b"PE\0\0":
        return False
    optional = pe + 24
    magic = struct.unpack_from("<H", head, optional)[0]
    directories = optional + (96 if magic == 0x10B else 112 if magic == 0x20B else -1)
    if directories < optional or directories + 5 * 8 > len(head):
        return False
    _address, size = struct.unpack_from("<II", head, directories + 4 * 8)      # entry 4: the certificate table
    return size > 0


def usable(path: Path) -> bool:
    return path.is_file() and (signed(path) if IS_WINDOWS else True)


def source() -> Path | None:
    name = binary_name()
    if not name:
        return None
    for folder in plugin_script_dirs():
        candidate = folder / name
        if usable(candidate):
            return candidate
    return None


def installed() -> Path | None:
    """The front in <home>/bin when it is there and may be used."""
    path = core.home().bin / FRONT_NAME
    return path if usable(path) else None


def place() -> str | None:
    """Copies the plugin's front to <home>/bin when it differs from the copy there. On Windows a running copy cannot be
    replaced, but can be renamed: it is moved aside (removed at a later refresh) and the new one put in its place."""
    src = source()
    if src is None:
        return None
    dest = core.home().bin / FRONT_NAME
    data = src.read_bytes()
    with contextlib.suppress(OSError):
        if dest.read_bytes() == data:
            return None
    if IS_WINDOWS and dest.exists():
        with contextlib.suppress(OSError):
            os.replace(dest, dest.with_name(f"{FRONT_NAME}.old-{os.getpid()}"))
    core.write_atomic(dest, data, 0o755)
    return f"front: {dest} from {src}"


def _remove_old_copies() -> None:
    for old in core.home().bin.glob("theodore*.exe.old-*") if core.home().bin.is_dir() else []:
        with contextlib.suppress(OSError):
            old.unlink()            # in use by a front still running: tried again next time


def discard(path: Path) -> None:
    """Removes a copy of the front. On Windows a running program cannot be deleted (the front that runs `theodore
    uninstall` is one), but it can be moved: into <home>/bin, which the uninstall's cleanup removes once it has ended,
    and a later refresh otherwise. Raises OSError when neither works."""
    try:
        path.unlink()
    except PermissionError:
        if not IS_WINDOWS:
            raise
        core.home().bin.mkdir(parents=True, exist_ok=True)
        os.replace(path, core.home().bin / f"{path.name}.old-{os.getpid()}")


def launch_document(oneshot: bool | None = None) -> dict | None:
    """C1 launch-json v1 for this installation, or None while its runtime, client or engine is not there yet."""
    from . import install
    home = core.home()
    python = home.runtime_python()
    client_version = core.read_text(home.client / "current.txt")
    engine = install.current_engine()
    if python is None or not python.is_file() or not client_version or not engine:
        return None
    client_dir = home.client / client_version
    if not (client_dir / "__main__.py").is_file():
        return None
    doc = {"v": 1, "home": str(home.root), "runtime_python": str(python), "client_dir": str(client_dir),
           "client_version": client_version, "engine_dir": str(install.engine_dir(engine)), "engine_version": engine}
    previous = core.read_json(home.bin / LAUNCH_NAME)
    keep = previous.get("oneshot") if isinstance(previous, dict) and oneshot is None else oneshot
    if keep:
        doc["oneshot"] = True
    return doc


def write_launch(oneshot: bool | None = None) -> str | None:
    """Writes <home>/bin/launch.json when what it says changed. `oneshot` True: the service is never started (a policy
    refused it, or doctor chose the one-shot mode); None keeps what the file says."""
    doc = launch_document(oneshot)
    if doc is None:
        return None
    path = core.home().bin / LAUNCH_NAME
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    with contextlib.suppress(OSError):
        if path.read_text(encoding="utf-8") == text:
            return None
    core.write_atomic(path, text, 0o644)
    return f"launch.json: client {doc['client_version']}, engine {doc['engine_version']}" + (", one-shot" if doc.get("oneshot") else "")


def refresh() -> list[str]:
    """The front and launch.json brought in step with what is installed. Never raises, never prints."""
    done: list[str] = []
    for step in (place, write_launch, _remove_old_copies):
        try:
            line = step()
            if line:
                done.append(line)
        except Exception as exc:  # noqa: BLE001  a convenience: the launcher and the client still serve every command
            core.log(f"front: {step.__name__} failed ({exc.__class__.__name__}: {exc})")
    for line in done:
        core.log(line)
    return done


def shim_bytes() -> dict[str, bytes] | None:
    """The short command as the front, when it is placed: on Windows its copy, theodore.exe (Git Bash, PowerShell and cmd
    all find it; no sh in front of it, no batch file); on macOS and Linux a two-line script that runs it (a shell start is
    a few milliseconds there, and the script carries a non-default home). None when the front is not placed."""
    front = installed()
    if front is None:
        return None
    if IS_WINDOWS:
        if core.shim_texts() is None:           # the short command is only for a default folder there
            return None
        return {"theodore.exe": front.read_bytes()}
    head = f"#!/bin/sh\n# {core.SHIM_MARK}, written by Theodore's set-up; `theodore hosts settings --remove` and its uninstall remove it.\n"
    lines = head
    if os.path.abspath(core.home().root) != os.path.abspath(Path.home() / ".theodore"):
        lines += f"THEODORE_HOME={core._sh_quote(str(core.home().root))}\nexport THEODORE_HOME\n"
    launcher = core._sh_quote(str(core.home().launcher()))
    lines += f'[ -x {core._sh_quote(str(front))} ] && exec {core._sh_quote(str(front))} "$@"\nexec {launcher} "$@"\n'
    return {"theodore": lines.encode("utf-8")}
