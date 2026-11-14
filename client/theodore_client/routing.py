"""The routing block: a few lines in each assistant's global instructions saying the user chose Theodore for decks.

Claude Code reads ~/.claude/CLAUDE.md (CLAUDE_CONFIG_DIR when set) and Codex reads ~/.codex/AGENTS.md (CODEX_HOME
when set) at the start of every session. Without these lines, Claude Code's own Slides artifact takes "make me a
deck" before any skill is loaded. Theodore writes only between its two markers, records in state/host-settings.json
which files it wrote and whether it created them, and the uninstall takes back exactly that block (and the file, when
Theodore created it and nothing else was ever added to it).

The block is written by `hosts register` (the installer runs it) and, for an install that never ran the installer, by
the first `guide`, `skill` or `doctor` run. It is updated in place when the wording changes. A block the user deleted
is not written again: the record remembers that Theodore wrote it once.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import core

BEGIN = "<!-- theodore:begin -->"
END = "<!-- theodore:end -->"
# Version of the wording below; recorded with each file so a later client knows which wording it left there.
VERSION = 2
# Measured (2026-11-12, lane HOST): with it, Opus routes "make me a deck" to Theodore 3/3 instead of Claude Slides,
# and a deck asked for "as a Claude Slides artifact" still goes to the artifact. The skill names are the same in
# Claude Code and Codex.
TEXT = ("The user chose Theodore for every presentation, deck or set of slides, in any format: use the Theodore skills "
        "(theodore:deck-design, theodore:pptx), unless the user names another tool for it.")
# Version 2 (2026-11-14, lead-approved words, ACCEPT-LOCAL2 D-D): asked to uninstall Theodore, Claude Code on Windows
# removed the plugin and the folder by hand and left the routing block and the computer's sign-in active. One clause
# names Theodore's own command, with the launcher's absolute path quoted as every shell the assistants use reads it.


def sentence() -> str:
    return TEXT[:-1] + f"; to uninstall Theodore, run {core.quoted_launcher()} uninstall."
RECORD_KEY = "instructions"


def block(newline: str = "\n") -> str:
    return newline.join((BEGIN, sentence(), END)) + newline


def targets() -> list[tuple[str, Path, str | None]]:
    """(assistant, its global instructions file, its command (also off PATH, where its installer puts it) or None)."""
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return [("claude", claude_dir / "CLAUDE.md", core.host_executable("claude")),
            ("codex", codex_dir / "AGENTS.md", core.host_executable("codex"))]


def _present(path: Path, tool: str | None) -> bool:
    """The assistant is on this computer: its command is on PATH, or its folder exists (the desktop app and the IDE
    extensions bring their own command and still read the folder)."""
    return bool(tool) or path.parent.is_dir()


def _read(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _span(text: str) -> tuple[int, int] | None | bool:
    """(start, end) of the first complete block, end including its line break; None when there is no block; False
    when a marker is there without its pair (then the file is not touched)."""
    start = text.find(BEGIN)
    if start < 0:
        return False if END in text else None
    stop = text.find(END, start)
    if stop < 0:
        return False
    stop += len(END)
    if text.startswith("\r\n", stop):
        stop += 2
    elif text.startswith("\n", stop):
        stop += 1
    return start, stop


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _real(path: Path) -> Path:
    """A symlinked instructions file (dotfiles kept in a repository) is edited where it points, never replaced."""
    try:
        return path.resolve() if path.is_symlink() else path
    except OSError:
        return path


def write(path: Path) -> tuple[str, bool]:
    """Write or update the block in one file. Returns (what happened, created the file)."""
    path = _real(path)
    text = _read(path)
    if text is None:
        return f"not changed ({path} could not be read as text)", False
    span = _span(text)
    if span is False:
        return f"not changed ({path} has a Theodore marker without its pair)", False
    nl = _newline(text)
    wanted = block(nl)
    if span is None:
        created = not path.exists()
        if not text:
            new = wanted
        else:
            body = text if text.endswith(("\n", "\r")) else text + nl
            new = body + ("" if body.endswith(nl + nl) else nl) + wanted     # one blank line before the block
    else:
        created = False
        if text[span[0]:span[1]] in (wanted, wanted.rstrip("\r\n")):
            return "already there", False
        new = text[:span[0]] + wanted + text[span[1]:]
    mode = None
    if path.exists():
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = None
    core.write_atomic(path, new, mode if mode is not None else 0o644)
    return ("written" if span is None else "updated"), created


def remove(path: Path, created: bool) -> str | None:
    """Take the block out of one file; delete the file when Theodore created it and nothing else is in it. Returns a
    problem, or None (also when the block or the file is not there any more)."""
    path = _real(path)
    text = _read(path)
    if text is None:
        return f"{path} could not be read as text"
    span = _span(text)
    if span is None or text == "":
        return None
    if span is False:
        return f"{path} has a Theodore marker without its pair; the block was left as it is"
    start, stop = span
    before, after = text[:start], text[stop:]
    nl = _newline(text)
    if before.endswith(nl + nl):
        before = before[:-len(nl)]          # the blank line Theodore put before the block
    rest = before + after
    try:
        if created and not rest.strip():
            path.unlink()
        else:
            mode = path.stat().st_mode & 0o777
            core.write_atomic(path, rest, mode)
    except OSError as exc:
        return f"{path}: {exc.__class__.__name__}"
    return None


def ensure(record: dict, register: bool = False) -> tuple[dict, list[str]]:
    """Bring the block to the current wording in each assistant present. `register` (the installer's step) writes it
    even where the user once deleted it; any other caller writes it only where Theodore never wrote it, and updates it
    where it is. Returns the updated record and one line per assistant changed."""
    entries = dict(record.get(RECORD_KEY) or {})
    lines: list[str] = []
    for name, path, tool in targets():
        entry = dict(entries.get(name) or {})
        if not _present(path, tool):
            continue
        text = _read(path)
        span = _span(text) if text is not None else False
        if text is None or span is False:
            lines.append(f"{name} instructions: not changed ({path} could not be read, or has a Theodore marker without its pair)")
            continue
        if entry and span is None and not register:
            continue                    # Theodore wrote it once and it is gone: the user's choice
        if span is not None and text[span[0]:span[1]].rstrip("\r\n") == block().rstrip("\n").replace("\n", _newline(text)):
            if not entry:               # the current block, there before any record (another copy of Theodore wrote it)
                entries[name] = {"path": str(path), "version": VERSION, "file_created": False}
            continue
        what, created = write(path)
        if what.startswith("not changed"):
            lines.append(f"{name} instructions: {what}")
            continue
        entries[name] = {"path": str(path), "version": VERSION, "file_created": bool(entry.get("file_created")) or created}
        verb = "added this line to" if what == "written" else "updated its line in"
        lines.append(f'{name} instructions: Theodore {verb} {path} (between {BEGIN} and {END}; uninstall removes it): "{sentence()}"')
    if entries:
        record = {**record, RECORD_KEY: entries}
    return record, lines


def take_back(record: dict, left: list[str]) -> dict:
    """Remove each recorded block. Problems go to `left`; the record keeps only the entries not taken back."""
    entries = dict(record.get(RECORD_KEY) or {})
    for name, entry in list(entries.items()):
        path = Path(entry.get("path") or "")
        if not entry.get("path"):
            entries.pop(name)
            continue
        problem = remove(path, bool(entry.get("file_created")))
        if problem:
            left.append(f"Theodore's lines in {path} ({problem})")
        else:
            entries.pop(name)
    record = dict(record)
    if entries:
        record[RECORD_KEY] = entries
    else:
        record.pop(RECORD_KEY, None)
    return record
