"""Which instructions each assistant session loaded, and the lines that send a session back to them.

Claude Code keeps only the first part of a skill's text after it compacts a conversation, and drops every command's
output; a resumed conversation holds the instructions as old as their first load. So the client notes, per session,
which instructions it served and in which version, and two places read that note:

- `session-start`, run by the plugin's one hook (hooks/hooks.json) when Claude Code compacts or resumes a
  conversation, prints one paragraph telling the assistant to load them again, only for a session that loaded them;
- `pptx start` and `pptx apply` print one line on standard error when the design instructions this session loaded are
  older than the current ones (and `start` one line when it never loaded them), at most once each.

The session is the one the host names: the hook's input, or CLAUDE_CODE_SESSION_ID for a command. Nothing is read from
the conversation. The note is state/sessions.json: {"sessions": {<session id>: {"at": <time>, "loaded": {<skill>:
{"version": <label>, "at": <time>}}, "told": [<line keys>]}}}, at most MAX_SESSIONS sessions and none older than
MAX_AGE_S. It is never sent anywhere.
"""

from __future__ import annotations

import json
import os
import sys
import threading

from . import core

FILE = "sessions.json"
LOCK = "sessions"
LOCK_TIMEOUT_S = 2.0
MAX_SESSIONS = 200
MAX_AGE_S = 30 * 86400
FRESH_TIMEOUT_S = 2.0       # the resume case's one request for the current versions, in all

COMPACT_TEXT = ("Theodore: this conversation loaded Theodore's instructions before it was compacted, and they are no longer "
                "here in full. Before you design or build slides again, load them again ({run}), then read the deck's "
                "design note again before you continue.")
RESUME_TEXT = ("Theodore: this conversation loaded Theodore's instructions as {old}; the current {noun} {new}. Before you "
               "design or build slides again, load them again ({run}), then read the deck's design note again before you "
               "continue.")
DESIGN_NEWER_TEXT = ("Theodore: this conversation loaded Theodore's design instructions as {old}; the current version is "
                     "{new}. Before you design further, load them again: run `{launcher} guide deck-design` and read what "
                     "it names.")
DESIGN_FIRST_TEXT = ("Theodore: if you are designing or redesigning slides, load Theodore's instructions first: run "
                     "`{launcher} guide deck-design` and read what it names.")
ORDER = ("deck-design", "pptx")


def session() -> str:
    """The Claude Code session this command runs in (every command Claude Code runs carries it), else empty."""
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()[:128]


def _path():
    return core.home().state / FILE


def _sessions(kept: bool = False) -> dict:
    """The note's sessions. `kept`: from memory while the file's stat holds (a read-only look on a command's way; a
    writer reads it fresh under the lock)."""
    if kept:
        from . import install
        value = install.read_json_by_stat(_path())
    else:
        value = core.read_json(_path())
    sessions = value.get("sessions") if isinstance(value, dict) else None
    return sessions if isinstance(sessions, dict) else {}


def _entry(sessions: dict, sid: str) -> dict:
    entry = sessions.get(sid)
    return entry if isinstance(entry, dict) else {}


def _save(sessions: dict) -> None:
    """Written whole and atomically, without the sessions older than MAX_AGE_S and past the newest MAX_SESSIONS."""
    now = core.now()
    kept = {sid: entry for sid, entry in sessions.items()
            if isinstance(entry, dict) and now - float(entry.get("at") or 0) < MAX_AGE_S}
    newest = sorted(kept, key=lambda sid: float(kept[sid].get("at") or 0), reverse=True)[:MAX_SESSIONS]
    core.write_json(_path(), {"sessions": {sid: kept[sid] for sid in newest}}, 0o600)


def record_loaded(skill: str, version: str | None) -> None:
    """Notes that this session was given `skill`'s instructions in `version` (None: a section whose label is not the
    skill's version, which keeps the version already noted). Never raises, never prints; no session, nothing."""
    sid = session()
    if not sid or skill not in ORDER:
        return
    try:
        with core.lock(LOCK, timeout=LOCK_TIMEOUT_S):
            sessions = _sessions()
            entry = _entry(sessions, sid)
            loaded = entry.get("loaded") if isinstance(entry.get("loaded"), dict) else {}
            noted = loaded.get(skill) if isinstance(loaded.get(skill), dict) else {}
            now = int(core.now())
            loaded[skill] = {"version": str(version if version is not None else noted.get("version") or ""), "at": now}
            sessions[sid] = {**entry, "at": now, "loaded": loaded}
            _save(sessions)
    except Exception as exc:  # noqa: BLE001  the note never changes what the command does
        core.log(f"sessions: not noted ({exc.__class__.__name__})")


def _loaded(entry: dict) -> dict:
    loaded = entry.get("loaded")
    if not isinstance(loaded, dict):
        return {}
    return {skill: str((loaded.get(skill) or {}).get("version") or "") for skill in ORDER if isinstance(loaded.get(skill), dict)}


def _known(version: str) -> bool:
    return bool(version) and version != "current"


def _current(fresh: bool) -> dict:
    """The current version of each skill's instructions: the design guidance's label and the engine of the build
    instructions, from the last update check; with `fresh`, from one request for the release manifest when it answers
    within FRESH_TIMEOUT_S."""
    from . import install
    state = install.update_state()
    guides = state.get("guides") if isinstance(state.get("guides"), dict) else {}
    latest = state.get("latest")
    if fresh:
        box: list = []

        def ask() -> None:
            try:
                box.append(install.fetch_channel_manifest(timeout=FRESH_TIMEOUT_S))
            except BaseException:  # noqa: BLE001  any failure: the cached values stand
                pass

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        worker.join(FRESH_TIMEOUT_S)
        if box and isinstance(box[0], dict):
            guides = box[0].get("guides") if isinstance(box[0].get("guides"), dict) else guides
            latest = (box[0].get("engine") or {}).get("version") or latest
    return {"deck-design": str(guides.get("deck-design") or ""),
            "pptx": str(install.pins().get("engine") or latest or install.current_engine() or "")}


def _run(skills: list[str], launcher: str) -> str:
    commands = " and ".join(f"`{launcher} guide {skill}`" for skill in skills)
    return f"run {commands}, and read what they name" if len(skills) > 1 else f"run {commands} and read what it names"


def session_start() -> int:
    """`session-start`: Claude Code's SessionStart hook for a compacted or resumed conversation, its JSON on standard
    input. Prints a paragraph only for a session that loaded Theodore's instructions: after a compaction always, on a
    resume only when a newer version of what it loaded is current. Silent and exit 0 on anything else."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return 0
        data = json.loads(sys.stdin.read(1 << 16) or "null")
        if not isinstance(data, dict) or data.get("source") not in ("compact", "resume"):
            return 0
        sid = str(data.get("session_id") or "").strip()[:128]
        loaded = _loaded(_entry(_sessions(), sid)) if sid else {}
        if not loaded:
            return 0
        skills = [skill for skill in ORDER if skill in loaded]
        launcher = core.quoted_launcher()
        if data["source"] == "compact":
            text = COMPACT_TEXT.format(run=_run(skills, launcher))
        else:
            current = _current(fresh=True)
            changed = [skill for skill in skills if _known(loaded[skill]) and _known(current[skill]) and loaded[skill] != current[skill]]
            if not changed:
                return 0
            # One label each when the changed skills moved together (an engine release labels both alike): "as
            # 1.0.0-rc.18; the current version is 1.0.0-rc.19", not the same label twice.
            old = list(dict.fromkeys(loaded[skill] for skill in changed))
            new = list(dict.fromkeys(current[skill] for skill in changed))
            if len(old) > 1 or len(new) > 1:
                old, new = [loaded[skill] for skill in changed], [current[skill] for skill in changed]
            text = RESUME_TEXT.format(old=" and ".join(old), noun="versions are" if len(new) > 1 else "version is",
                                      new=" and ".join(new), run=_run(skills, launcher))
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001  a hook never fails and never shows a problem
        pass
    return 0


def engine_notice(verb: str) -> None:
    """Before `pptx start` or `pptx apply`: one line on standard error (standard output carries the engine's own answer,
    one JSON object when piped). The design instructions this session loaded are older than the current ones: once per
    session and version. On `start`, a session that never loaded them: once per session. Never raises."""
    sid = session()
    if not sid or verb not in ("start", "apply"):
        return
    try:
        # Looked at first without the lock: almost always there is nothing to say (or it was said), nothing is written,
        # and the many designers of one session applying at once never queue on the lock file (a waiter sleeps 50 ms).
        if _notice(_sessions(kept=True), sid, verb)[0] is None:
            return
        with core.lock(LOCK, timeout=LOCK_TIMEOUT_S):
            sessions = _sessions()          # read again under the lock: another command may have said it meanwhile
            line, key, entry, told = _notice(sessions, sid, verb)
            if line is None:
                return
            sessions[sid] = {**entry, "at": int(core.now()), "told": told + [key]}
            _save(sessions)
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception as exc:  # noqa: BLE001  the line never changes what the engine command does
        core.log(f"sessions: no line ({exc.__class__.__name__})")


def _notice(sessions: dict, sid: str, verb: str) -> tuple:
    """(line, key, entry, told): the line this session is owed before `verb`, or None when there is none or it was said."""
    entry = _entry(sessions, sid)
    told = [key for key in entry.get("told") or [] if isinstance(key, str)]
    design = _loaded(entry).get("deck-design")
    line = key = None
    if design is not None:
        current = _current(fresh=False)["deck-design"]
        if _known(design) and _known(current) and design != current:
            key = f"deck-design {current}"
            line = DESIGN_NEWER_TEXT.format(old=design, new=current, launcher=core.quoted_launcher())
    elif verb == "start":
        key = "design-first"
        line = DESIGN_FIRST_TEXT.format(launcher=core.quoted_launcher())
    if line is None or key in told:
        return None, None, entry, told
    return line, key, entry, told
