"""The first words after set-up, and the Studio setting (lane ONBOARD).

`welcome` is the installers' last status: a short welcome the assistant gives in the same conversation. It asks about
Studio once per computer (state/welcome.json), only when the account has Studio on and it applies; nothing repeats the
question later. `studio [on|off]` reads or changes the account's Studio setting, the same one the portal's Settings
switch changes (GET/PUT /v1/studio, this installation's own account only).

What a user is told says what Studio means for them, never how it works.
"""

from __future__ import annotations

import json
import os

from . import api, core
from .core import Status, quoted_launcher

FILE = "welcome.json"
STUDIO_ON_MEANS = "slides get a deeper design pass, which takes longer and uses more of your AI plan"
STUDIO_OFF_MEANS = "decks are faster and lighter"


# --- Studio ------------------------------------------------------------------------------------------------------------

def _answer(raw: bytes) -> dict:
    value = json.loads(raw.decode("utf-8") or "{}")
    return value if isinstance(value, dict) else {}


def studio(action: str | None) -> int:
    from .commands import _api          # imported here: commands is large and imports most modules
    if action not in (None, "on", "off"):
        raise core.usage(f"studio takes on, off or nothing ('{action}' given).")
    _status, _headers, raw = _api("studio_get", "Studio")
    state = _answer(raw)
    if not state.get("included"):
        return core.emit(Status("OK", "the account's plan does not include Studio; it is off and cannot be turned on here.",
                                "nothing to do.", "Studio is not part of your Theodore plan, so it is off."))
    if action is None:
        on = bool(state.get("studio_enabled"))
        return core.emit(Status("OK", f"Studio is {'on' if on else 'off'} for this account.",
                                f"nothing to do; {quoted_launcher()} studio {'off' if on else 'on'} changes it only if the user asks.",
                                f"Studio is on: {STUDIO_ON_MEANS}." if on else f"Studio is off: {STUDIO_OFF_MEANS}."))
    want = action == "on"
    if bool(state.get("studio_enabled")) != want:
        _status, _headers, raw = _api("studio_set", "Studio", body={"studio_enabled": want})
        state = _answer(raw)
    _remember({"studio_asked": True})
    if want:
        return core.emit(Status("OK", "Studio is on for this account, from its next deck.", "nothing to do.",
                                f"Studio is on again for your account: from your next deck, {STUDIO_ON_MEANS}."))
    return core.emit(Status("OK", "Studio is off for this account, from its next deck.", "nothing to do.",
                            f"Studio is off for your account: from your next deck, {STUDIO_OFF_MEANS}. "
                            "Say if you want it on again."))


def _studio_to_ask() -> bool:
    """Whether the welcome asks about Studio: never asked on this computer, and the account has it on, in its plan and
    honoured by its guidance. Any failure (offline, slow, refused) leaves it out: the welcome never waits or fails."""
    if _remembered().get("studio_asked"):
        return False
    try:
        from . import credentials
        cred = credentials.load()
        if not cred:
            return False
        _status, _headers, raw = api.call("studio_get", bearer=cred["installation_secret"], timeout=4.0,
                                          exact_timeout=True, learn_refusals=False)
        state = _answer(raw)
    except Exception as exc:  # noqa: BLE001  the question is optional; the welcome is not
        core.log(f"welcome: Studio not read ({exc.__class__.__name__})")
        return False
    return bool(state.get("studio_enabled") and state.get("included") and state.get("available"))


# --- the welcome -------------------------------------------------------------------------------------------------------

def _remembered() -> dict:
    value = core.read_json(core.home().state / FILE)
    return value if isinstance(value, dict) else {}


def _remember(fields: dict) -> None:
    try:
        core.home().ensure()
        core.write_json(core.home().state / FILE, {**_remembered(), **fields, "at": int(core.now())}, 0o600)
    except Exception as exc:  # noqa: BLE001  a note only
        core.log(f"welcome: not noted ({exc.__class__.__name__})")


def _host() -> str | None:
    """The assistant running this command: "codex" or "claude-code" by the variables each sets for its commands."""
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    if os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDECODE"):
        return "claude-code"
    return None


def _new_session_needed(host: str | None) -> bool:
    """Codex loads plugins, tools and rules only when a session starts. Claude Code loads the plugin's skills and the
    instructions line at session start too, unless this very session already loaded Theodore (a set-up run again)."""
    if host == "codex":
        return True
    if host != "claude-code":
        return False
    try:
        from . import sessions
        sid = sessions.session()
        return not (sid and sessions._loaded(sessions._entry(sessions._sessions(), sid)))
    except Exception:  # noqa: BLE001  unknown: say it, a new session never hurts
        return True


def _downloading() -> bool:
    from .core import Job
    return any(Job(name).alive() for name in ("setup", "packs", "libreoffice", "git"))


def welcome(engine_downloading: bool = False) -> int:
    """The installers' last status. `engine_downloading`: the runtime or engine is still downloading (the installer's
    SETUP_IN_PROGRESS case)."""
    from . import desktop
    from .commands import _host_record
    host = _host()
    new_session = _new_session_needed(host)
    ask_studio = _studio_to_ask()
    launcher = quoted_launcher()

    lines = ["Welcome to Theodore. It is set up and signed in on this computer.",
             "From now on, when you ask me for a presentation, I build it as a PowerPoint file with Theodore.",
             "To start, share a document or your notes, say who the deck is for, and attach your template if you "
             "have one."]
    if ask_studio:
        lines.append("Studio is on, so slides get a deeper design pass that takes longer and uses more of your AI plan. "
                     "Would you like it off, for faster and lighter decks?")
    last = []
    if engine_downloading or _downloading():
        last.append("A last download finishes by itself in the next few minutes.")
    if new_session:
        last.append("Start a new session, then ask for your first deck.")
    try:
        if desktop.reopen_needed(_host_record()):
            last.append("To use it in the Claude app's chats too, quit the app and open it again once.")
    except Exception:  # noqa: BLE001  a note only
        pass
    if last:
        lines.append(" ".join(last))

    give = ("give the user this welcome now, in this conversation, in at most five short lines, as written or in your "
            "own words without adding to it; ask nothing else")
    studio_next = (f"; if they answer that they want Studio off, run {launcher} studio off (and {launcher} studio on if "
                   "they later want it back); otherwise do nothing about it and do not ask again") if ask_studio else ""
    if host == "codex":
        after = (". Apart from that, run no Theodore command in this session: Codex loads Theodore's tool and permissions "
                 "only when a session starts, so here each Theodore command would ask for approval; if they ask for a deck "
                 "here, ask them to start a new session first")
    elif engine_downloading:
        after = (f". Then run {launcher} status --wait 100 (it returns as soon as the download is done), again while it "
                 "says SETUP_IN_PROGRESS; when it says OK, tell the user in one short line that Theodore has finished "
                 "downloading")
    else:
        after = ""
    if ask_studio:
        _remember({"studio_asked": True})
    if engine_downloading and host != "codex":
        return core.emit(Status("SETUP_IN_PROGRESS",
                                "Theodore is installed and signed in; the rest of its runtime and its engine are still "
                                "downloading in the background, which goes on after this installer has ended.",
                                give + studio_next + after + ".", " ".join(lines)))
    return core.emit(Status("OK", "Theodore is set up and signed in" + (
                            "; the rest of its runtime and its engine finish downloading by themselves in the background."
                            if engine_downloading else " (the check above)."),
                            give + studio_next + after + ".", " ".join(lines)))
