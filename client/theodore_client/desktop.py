"""Claude Desktop: Theodore's local server as an entry in the app's own configuration file.

Why: on a Claude Team or Enterprise plan only an Owner can add a remote connector, while local MCP servers in
claude_desktop_config.json are on for every member unless the organisation turns them off (MDM key
`isLocalDevMcpEnabled`, default true; Enterprise: "Allow local MCP servers"). The entry runs this installation's
stable launcher, `theodore mcp --for claude-desktop`, which serves the guidance and the specimen sheets through this
computer's own sign-in, held in memory only (mcp.py). The app reads the file when it starts.

Only the `mcpServers.theodore` entry is ever written or removed; every other key, server and preference stays as it
was. The record (state/host-settings.json, key `claude_desktop`) says which file it was written to, the entry as
written and whether Theodore created the `mcpServers` object, so the uninstall takes back exactly that, and an entry
the user made or changed since is never removed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import core

ENTRY_NAME = "theodore"
RECORD_KEY = "claude_desktop"
FOR = "claude-desktop"


LEFT_MARKER = ".user-data-left."


def data_left(folder: Path) -> bool:
    """The Claude app no longer keeps its data in this folder. When the Windows app moves its data to
    %LOCALAPPDATA%\\Claude-Data it leaves a file named `.user-data-left.<its package family>` where the data was (seen
    2026-11-22, app 6.28899: the folder held nothing else afterwards). Nothing reads a configuration file written
    there after that."""
    try:
        return any(child.name.startswith(LEFT_MARKER) for child in folder.iterdir())
    except OSError:
        return False


def config_dirs() -> list[Path]:
    """The app's folders that exist on this computer (it creates its folder on first start)."""
    if core.IS_WINDOWS:
        found = []
        local = os.environ.get("LOCALAPPDATA")
        # Since November 2026 the app keeps its data, this file included, in %LOCALAPPDATA%\Claude-Data\<its package
        # family>: a plain folder, the same one from inside the app's package and from outside it.
        moved = Path(local) / "Claude-Data" if local else None
        if moved and moved.is_dir():
            found += [folder for folder in sorted(moved.iterdir()) if folder.is_dir()]
        older = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            older.append(Path(appdata) / "Claude")
        # The Microsoft Store build kept its roaming folder inside its package.
        packages = Path(local) / "Packages" if local else None
        if packages and packages.is_dir():
            older += [package / "LocalCache" / "Roaming" / "Claude" for package in sorted(packages.glob("Claude_*"))]
        # A folder the app has left is not one of its folders any more: an entry written there would load nowhere.
        found += [folder for folder in older if folder.is_dir() and not data_left(folder)]
        return found
    if os.uname().sysname == "Darwin":
        folder = Path.home() / "Library" / "Application Support" / "Claude"
        return [folder] if folder.is_dir() else []
    return []                               # Claude Desktop is published for macOS and Windows only


# Characters that make cmd's /c drop the quotes around a path (or, %, expand it): C:\Users\Tom & Jerry.
CMD_SPECIAL = set('&<>()@^|%!"')


def entry() -> dict:
    """The entry for this installation. On Windows the launcher is a batch file, which the app starts through cmd.
    A launcher path with a character cmd treats specially goes in the entry's env, quoted, and cmd reads it with
    delayed expansion, which comes after cmd has parsed & ( ) ^ |: they stay part of the path."""
    launcher = str(core.home().launcher())
    if core.IS_WINDOWS:
        cmd = os.environ.get("COMSPEC") or "cmd.exe"
        if CMD_SPECIAL & set(launcher):
            return {"command": cmd, "args": ["/d", "/v:on", "/c", "!THEODORE_DESKTOP_LAUNCHER!", "mcp", "--for", FOR],
                    "env": {"THEODORE_DESKTOP_LAUNCHER": f'"{launcher}"'}}
        return {"command": cmd, "args": ["/d", "/c", launcher, "mcp", "--for", FOR]}
    return {"command": launcher, "args": ["mcp", "--for", FOR]}


def entry_present() -> bool:
    """Theodore's entry is in one of the app's configuration files (the app then starts Theodore's server itself)."""
    for folder in config_dirs():
        settings = _load(folder / "claude_desktop_config.json")
        servers = settings.get("mcpServers") if isinstance(settings, dict) else None
        if isinstance(servers, dict) and isinstance(servers.get(ENTRY_NAME), dict):
            return True
    return False


def _ours(value, written: dict | None) -> bool:
    """An entry Theodore wrote: the one recorded, or one that runs this installation's launcher with `mcp --for`."""
    if not isinstance(value, dict):
        return False
    if written is not None and value == written:
        return True
    return value == entry()


def _load(path: Path):
    """The file's object, {} when there is no file, None when it is not a JSON object (then nothing is touched). A
    file that starts with a byte-order mark is read all the same (Windows PowerShell and some editors write one; until
    0.4.25 such a file was refused as not valid JSON and Theodore's entry was not written)."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: dict) -> None:
    core.write_atomic(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n", 0o644 if not core.IS_WINDOWS else None)


def register(record: dict) -> list[str]:
    """Add the entry to each Claude Desktop configuration file found; one line per file. Idempotent."""
    folders = config_dirs()
    if not folders:
        return ["claude-desktop: not found on this computer"]
    lines = []
    files = record.get(RECORD_KEY) or {}
    for folder in folders:
        path = folder / "claude_desktop_config.json"
        settings = _load(path)
        if settings is None:
            lines.append(f"claude-desktop: FAILED ({path} is not a valid JSON object; it was not changed)")
            continue
        servers = settings.get("mcpServers")
        if servers is not None and not isinstance(servers, dict):
            lines.append(f"claude-desktop: FAILED ({path} has mcpServers in another shape than expected; it was not changed)")
            continue
        mine = files.get(str(path)) or {}
        current = (servers or {}).get(ENTRY_NAME)
        wanted = entry()
        if current is not None and not _ours(current, mine.get("entry")):
            lines.append(f"claude-desktop: an entry named {ENTRY_NAME} that Theodore did not write is in {path}; it was kept")
            files.setdefault(str(path), {"ours": False})
            continue
        if current == wanted:
            lines.append("claude-desktop: already registered")
        else:
            created_servers = servers is None
            settings.setdefault("mcpServers", {})[ENTRY_NAME] = wanted
            try:
                _write(path, settings)
            except OSError as exc:
                lines.append(f"claude-desktop: FAILED ({path} could not be written: {exc.__class__.__name__})")
                continue
            # No step for the user here: read in the installer's output, "(it loads when the Claude app is quit and
            # opened again)" became "quit and reopen the Claude app" in most set-ups by Opus from the app's Code tab
            # (lane L3's rehearsals, 24 Nov 2026), where Theodore already works. doctor says it when the user asks.
            lines.append("claude-desktop: registered for the Claude app's chats")
            mine = {"ours": True, "created_servers": bool(mine.get("created_servers")) or created_servers, "written_at": time.time()}
        mine.update({"ours": True, "entry": wanted})
        files[str(path)] = mine
    if files:
        record[RECORD_KEY] = files
    return lines


def is_desktop_app(path: str) -> bool:
    """A Windows program file of the Claude app: its per-user build (%LOCALAPPDATA%\\AnthropicClaude\\app-<version>\\)
    or its package (WindowsApps\\Claude_<...>\\). Not Claude Code's claude.exe (~/.local/bin, npm) or anything else."""
    parts = [p.lower() for p in re.split(r"[\\/]+", path) if p]
    if not parts or parts[-1] != "claude.exe":
        return False
    folders = parts[:-1]
    if "anthropicclaude" in folders:
        rest = folders[folders.index("anthropicclaude") + 1:]
        return bool(rest) and rest[0].startswith("app-")
    return "windowsapps" in folders and any(f.startswith("claude_") for f in folders[folders.index("windowsapps") + 1:][:1])


def app_started_at() -> float | None:
    """When the Claude app that is running now started (epoch seconds), or None when it is not running or that cannot be
    told. The app reads its configuration file only when it starts, and nothing makes it read it again (Model Context
    Protocol docs, "Connect to local MCP servers": "The application needs to restart to load the new configuration")."""
    override = os.environ.get("THEODORE_TEST_CLAUDE_APP_STARTED")
    if override is not None:
        return float(override) if override else None
    try:
        if core.IS_WINDOWS:
            # Every claude.exe, with its program file: Claude Code's own CLI is claude.exe too (~/.local/bin), and only
            # the app's count (WINDOWS W-7: the CLI alone made the install say to reopen a Claude app that was closed).
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                   "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}; "
                                   "Get-Process claude -ErrorAction SilentlyContinue | Where-Object { $_.Path } | ForEach-Object "
                                   "{ $_.StartTime.ToUniversalTime().ToString('o') + '|' + $_.Path }"],
                                  capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
            from datetime import datetime
            times = [datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
                     for when, _bar, path in (line.strip().partition("|") for line in core.child_text(done.stdout).splitlines())
                     if path and is_desktop_app(path)]
            return min(times) if times else None
        if os.uname().sysname != "Darwin":
            return None
        out = subprocess.run(["ps", "-axo", "lstart=,comm="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    started = [time.mktime(time.strptime(line[:24], "%a %b %d %H:%M:%S %Y"))
               for line in out.splitlines() if line.rstrip().endswith("/Claude.app/Contents/MacOS/Claude")]
    return min(started) if started else None


def reopen_needed(record: dict) -> bool:
    """The Claude app is running and started before Theodore's entry was written, so its chats do not have Theodore
    until it is quit and opened again. Never quits it: the app may be the one running this install (its Code tab)."""
    written = [mine.get("written_at") for mine in (record.get(RECORD_KEY) or {}).values() if mine.get("ours") and mine.get("written_at")]
    if not written:
        return False
    started = app_started_at()
    return started is not None and started < max(written)


def unregister(record: dict, left: list[str], notes: list[str]) -> list[str]:
    """Remove the entries the record says Theodore wrote, each only while it is still what Theodore wrote. Returns the
    files changed. Updates `record` in place (the key goes once nothing is left to take back)."""
    files = record.get(RECORD_KEY) or {}
    changed = []
    recorded = set(files)
    written = [mine.get("entry") for mine in files.values() if mine.get("ours")]
    made_servers = any(mine.get("created_servers") for mine in files.values() if mine.get("ours"))
    for name, mine in list(files.items()):
        path = Path(name)
        if not mine.get("ours"):
            files.pop(name)
            notes.append(f"claude-desktop: the {ENTRY_NAME} entry in {path} was there before Theodore and was kept")
            continue
        settings = _load(path)
        if settings is None:
            left.append(f"the {ENTRY_NAME} entry in {path} (the file is not a valid JSON object now; remove the entry by hand)")
            continue
        servers = settings.get("mcpServers")
        current = servers.get(ENTRY_NAME) if isinstance(servers, dict) else None
        if current is None:
            files.pop(name)
            continue
        if not _ours(current, mine.get("entry")):
            files.pop(name)
            notes.append(f"claude-desktop: the {ENTRY_NAME} entry in {path} was changed after Theodore wrote it, so it was left as it is")
            continue
        del servers[ENTRY_NAME]
        if mine.get("created_servers") and servers == {}:
            del settings["mcpServers"]
        try:
            _write(path, settings)
        except OSError as exc:
            left.append(f"the {ENTRY_NAME} entry in {path} ({exc.__class__.__name__}; remove it by hand)")
            continue
        files.pop(name)
        changed.append(str(path))
    # The app may have moved its configuration file since Theodore wrote to it, the entry with it (Windows, November
    # 2026: from its package's folder to %LOCALAPPDATA%\Claude-Data). An entry Theodore wrote that is now in a file the
    # record does not name is taken back as well; an entry that is not what Theodore wrote stays, as above.
    for folder in config_dirs() if written else []:
        path = folder / "claude_desktop_config.json"
        if str(path) in recorded:
            continue
        settings = _load(path)
        servers = settings.get("mcpServers") if isinstance(settings, dict) else None
        current = servers.get(ENTRY_NAME) if isinstance(servers, dict) else None
        if current is None or not any(_ours(current, entry_written) for entry_written in written):
            continue
        del servers[ENTRY_NAME]
        if made_servers and servers == {}:
            del settings["mcpServers"]
        try:
            _write(path, settings)
        except OSError as exc:
            left.append(f"the {ENTRY_NAME} entry in {path} ({exc.__class__.__name__}; remove it by hand)")
            continue
        changed.append(str(path))
    if files:
        record[RECORD_KEY] = files
    else:
        record.pop(RECORD_KEY, None)
    return changed


def left_behind(record: dict) -> bool:
    """Theodore's entry was written to a folder the Claude app has since left (data_left): the app's chat no longer
    loads it from there. Cheap: the record and one folder listing per recorded file."""
    return any(mine.get("ours") and data_left(Path(name).parent) for name, mine in (record.get(RECORD_KEY) or {}).items())


def follow(record: dict) -> list[str]:
    """After the app moved its data: Theodore's entry goes from the file in the folder the app left (a file nothing
    reads) and is written where the app keeps its configuration now, when it is not there already (the app usually
    carries the file along, entry included). Updates `record` in place; one line per file, as register."""
    files = record.get(RECORD_KEY) or {}
    for name, mine in list(files.items()):
        path = Path(name)
        if not (mine.get("ours") and data_left(path.parent)):
            continue
        settings = _load(path) if path.exists() else {}
        servers = settings.get("mcpServers") if isinstance(settings, dict) else None
        if isinstance(servers, dict) and _ours(servers.get(ENTRY_NAME), mine.get("entry")):
            del servers[ENTRY_NAME]
            if mine.get("created_servers") and servers == {}:
                del settings["mcpServers"]
            try:
                if settings and settings != {"mcpServers": {}}:
                    _write(path, settings)
                else:
                    path.unlink()               # the file held Theodore's entry and nothing else: Theodore made it there
            except OSError:
                pass                            # a file nothing reads: it stays, and is not Theodore's concern again
        files.pop(name)
    if files:
        record[RECORD_KEY] = files
    else:
        record.pop(RECORD_KEY, None)
    return register(record)
