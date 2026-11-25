"""The first words after set-up, and the Studio setting (lanes ONBOARD and L3).

`welcome` is the installers' last status: a short welcome the assistant gives in the same conversation. Every welcome
says Studio's state (on, off, or not in the plan: a sentence, never a question) and the asset packs' state (ready, or
finishing by themselves with about how much is left), so nobody has to ask (the set-up of 24 November 2026: three
questions in thirteen minutes). An installer run by a person, not by an assistant, ends with plain words for that
person and leaves the welcome owed: the first `status` or `doctor` that says OK, or the first instructions an assistant
loads (`guide`, a skill), gives it once (`owed`, `owed_notice`). `studio [on|off]` reads or changes the account's
Studio setting, the same one the portal's Settings switch changes (GET/PUT /v1/studio, this installation's own account
only).

What a user is told says what Studio means for them, never how it works.
"""

from __future__ import annotations

import json
import os

from . import api, core
from .core import Status, quoted_launcher

FILE = "welcome.json"
STUDIO_ON_MEANS = "each slide gets a deeper design pass, which uses more of your AI plan"
STUDIO_OFF_MEANS = "slides get a simpler design pass that uses less of your AI plan"


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
    if want:
        return core.emit(Status("OK", "Studio is on for this account, from its next deck.", "nothing to do.",
                                f"Studio is on again for your account: from your next deck, {STUDIO_ON_MEANS}."))
    return core.emit(Status("OK", "Studio is off for this account, from its next deck.", "nothing to do.",
                            f"Studio is off for your account: from your next deck, {STUDIO_OFF_MEANS}. "
                            "Say if you want it on again."))


# How long the welcome waits for the account's Studio setting. The set-up of 24 November 2026 ran on a laptop where a
# Studio command took about 19 s in all (the launcher and the client's start; the read itself is short): 8 s for the
# read alone, once, at the end of an installer that has run for minutes.
STUDIO_READ_S = 8.0


def studio_state(timeout: float = STUDIO_READ_S) -> str | None:
    """The account's Studio setting as the welcome says it: "on", "off", "not_included" (the plan has no Studio), or
    None when it could not be read (not signed in, offline, slow, refused, or a guidance that does not honour it). Never
    raises: the welcome never waits longer or fails for it."""
    try:
        from . import credentials
        cred = credentials.load()
        if not cred:
            return None
        _status, _headers, raw = api.call("studio_get", bearer=cred["installation_secret"], timeout=timeout,
                                          exact_timeout=True, learn_refusals=False)
        state = _answer(raw)
    except Exception as exc:  # noqa: BLE001  a sentence of the welcome, not the welcome
        core.log(f"welcome: Studio not read ({exc.__class__.__name__})")
        return None
    if not state.get("included"):
        return "not_included"
    if not state.get("available"):
        return None
    return "on" if state.get("studio_enabled") else "off"


def studio_sentence(state: str | None) -> str | None:
    if state == "on":
        return f"Studio is on: {STUDIO_ON_MEANS}."
    if state == "off":
        return f"Studio is off: {STUDIO_OFF_MEANS}."
    if state == "not_included":
        return "Studio, a deeper design pass for each slide, is not part of your Theodore plan."
    return None


PACKS = "Its icon, flag, map and background libraries"


def packs_sentence() -> str | None:
    """The asset packs' state in one sentence: ready, finishing by themselves (about how much is left), or not all
    there yet. None when the channel publishes no pack."""
    from . import install
    from .contract import PACK_ORDER
    from .core import Job
    try:
        published = install.published_packs()
        if published == []:
            return None
        names = [p for p in PACK_ORDER if published is None or p in published]
        state = install.packs_state()
        waiting = [p for p in names if (state.get(p) or {}).get("status") != "ready"]
        if not waiting:
            return f"{PACKS} are ready."
        if Job("packs").alive():
            left = _megabytes_left(waiting, state)
            return (f"{PACKS} are finishing their download by themselves" + (f" (about {left} MB left)" if left else "")
                    + "; a deck can start meanwhile.")
        return f"{PACKS} are not all downloaded yet; the next deck fetches the rest by itself."
    except Exception as exc:  # noqa: BLE001  a sentence of the welcome, not the welcome
        core.log(f"welcome: packs not read ({exc.__class__.__name__})")
        return None


def _megabytes_left(waiting: list[str], state: dict) -> int | None:
    from . import install
    sizes = install.published_pack_sizes()
    total = 0.0
    for pack in waiting:
        size = sizes.get(pack)
        if not size:
            return None
        done = max(0, min(100, int((state.get(pack) or {}).get("percent") or 0)))
        total += size * (100 - done) / 100
    return max(1, round(total / 1_000_000))


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
    if os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    return None


def _new_session_needed(host: str | None, first_deck: bool = True) -> bool:
    """Codex loads plugins, tools and rules only when a session starts. So does Claude Code: a plugin's skills, agents
    and tool, and the instructions line, load when a conversation starts, and the Claude app's Code tab keeps one
    process for a conversation, so the conversation that installed Theodore has none of them. There the first deck
    went to the app's own slide artifact instead of Theodore in one rehearsal (Opus, auto mode, the user's
    configuration), and ran through Theodore's launcher without its agents in two (lane L3, 25 November 2026). The
    welcome (`first_deck`) sends the first deck to a new session in both; doctor, which also runs in conversations
    that have Theodore, says it only for Codex."""
    if host == "codex":
        return True
    return first_deck and host == "claude-code"


def _downloading() -> bool:
    """A download of the runtime, the engine, LibreOffice or Git still going on (the packs have their own sentence)."""
    from .core import Job
    return any(Job(name).alive() for name in ("setup", "libreoffice", "git"))


CLOSING = ("This welcome is your whole closing message: say nothing about permissions, settings or uninstalling unless "
           "the user asks.")
WELCOME = ("Welcome to Theodore: it is set up and signed in on this computer, and from now on I build your presentations "
           "with it, as PowerPoint files.")
TO_START = "To start, share a document or your notes, say who the deck is for, and attach your template if you have one."


def _welcome_status(name: str, summary: str, engine_downloading: bool, download_done: bool = False) -> Status:
    """The welcome as a status. `download_done`: given by the first `status` that says OK after the installer left it
    owed, so no line says a download is still going on."""
    host = _host()
    new_session = _new_session_needed(host)
    studio_ = studio_state()
    launcher = quoted_launcher()

    lines = [WELCOME] + [line for line in (studio_sentence(studio_), packs_sentence()) if line] + [TO_START]
    last = []
    if not download_done and (engine_downloading or _downloading()):
        last.append("A last download finishes by itself in the next few minutes.")
    if new_session:
        last.append("Start a new session, then ask for your first deck.")
    if last:
        lines.append(" ".join(last))

    give = ("give the user this welcome now, in this conversation, in at most five short lines, as written or in your "
            "own words without adding to it; ask nothing else")
    studio_next = (f"; if they say they want Studio off, run {launcher} studio off ({launcher} studio on turns it back "
                   "on); otherwise do nothing about it") if studio_ in ("on", "off") else ""
    after = (". Apart from that, run no Theodore command in this session: Codex loads Theodore's tool and permissions "
             "only when a session starts, so here each Theodore command would ask for approval; if they ask for a deck "
             "here, ask them to start a new session first") if host == "codex" else (
        ". If they ask for a deck in this conversation anyway, ask them to start a new session first: Claude Code loads "
        "Theodore's skills and instructions only when a conversation starts, so here the deck would be made without "
        "them") if new_session else ""
    _remember({"owed": False, "given": True})
    return Status(name, summary, give + studio_next + after + ". " + CLOSING, " ".join(lines))


def _for_a_person(engine_downloading: bool) -> int:
    """The installer's ending when a person ran it in a terminal, not an assistant: plain words, no status block. The
    welcome stays owed, so the assistant the person goes back to gives it (status, doctor, or its first instructions)."""
    first = ("Theodore is installed on this computer and finishes a download by itself in the next few minutes."
             if engine_downloading or _downloading() else "Theodore is set up and signed in on this computer.")
    said = [first] + [line for line in (studio_sentence(studio_state()), packs_sentence()) if line]
    said.append("Go back to your assistant (Claude or Codex) and ask for your presentation there.")
    _remember({"owed": True})
    print("\n".join(said), flush=True)
    return 0


def welcome(engine_downloading: bool = False) -> int:
    """The installers' last status. `engine_downloading`: the runtime or engine is still downloading (the installer's
    SETUP_IN_PROGRESS case). The welcome must be the last thing the assistant is given (an assistant finishes its tool
    work before it speaks): while the download goes on, the installer only sends it to `status --wait`, marks the
    welcome owed, and the first `status` that says OK gives it (`owed`). Codex runs nothing more in the set-up session,
    so there the installer gives it at once. Run by a person, plain words (`_for_a_person`)."""
    host = _host()
    if host is None:
        return _for_a_person(engine_downloading)
    if engine_downloading and host != "codex":
        return core.emit(_owe())
    return core.emit(_welcome_status("OK", "Theodore is set up and signed in" + (
        "; the rest of its runtime and its engine finish downloading by themselves in the background."
        if engine_downloading else " (the check above)."), engine_downloading))


def _owe() -> Status:
    """The welcome left owed while the runtime or engine downloads: the first `status` that says OK gives it."""
    _remember({"owed": True})
    return Status("SETUP_IN_PROGRESS",
                  "Theodore is installed and signed in; the rest of its runtime and its engine are still "
                  "downloading in the background, which goes on after this installer has ended.",
                  f"run {quoted_launcher()} status --wait 100 (it returns as soon as the download is done), again "
                  "while it says SETUP_IN_PROGRESS; when it says OK, its answer holds a short welcome for the "
                  "user. Meanwhile, tell the user only the line below.",
                  "Theodore is installed and is finishing a download in the background; this takes a few minutes.")


def after_sign_in(signed: Status) -> Status:
    """`login --finish`'s answer. A set-up whose one-time code was old or missing ends with a sign-in link instead of
    the welcome: on a computer that never had it, the sign-in that link completes gives it, by the installer's rules.
    A computer that had its welcome gets `signed` unchanged. No engine and no set-up job (it ended before this sign-in):
    the job is started here, as the installer does after activation, and never waited for."""
    if _remembered().get("given"):
        return signed
    from .core import Job
    try:
        from . import install
        if not install.current_engine() and not Job("setup").alive():
            started = install.setup_status()        # starts the set-up job, which goes on to the engine when signed in
            if not Job("setup").alive():
                core.log(f"welcome: the set-up job did not start ({started.name}: {started.summary})")
    except Exception as exc:  # noqa: BLE001  the sign-in and its welcome go on without it
        core.log(f"welcome: the set-up job did not start ({exc.__class__.__name__})")
    engine_downloading = Job("setup").alive()
    if engine_downloading and _host() != "codex":
        return _owe()
    return _welcome_status("OK", signed.summary, engine_downloading)


def owed(summary: str) -> Status | None:
    """The welcome, once, for the first `status` that says OK after the installer left it owed; else None."""
    if not _remembered().get("owed"):
        return None
    return _welcome_status("OK", summary + " Set-up is finished.", engine_downloading=False, download_done=True)


def owed_notice() -> str | None:
    """The welcome an installer run by a person left owed, as one notice line at the head of the first instructions an
    assistant loads (`guide`, a skill), once; None when nothing is owed or the engine is still downloading (then the
    `status --wait` the installer named gives it). Never raises."""
    try:
        if not _remembered().get("owed") or _downloading():
            return None
        said = " ".join(line for line in ("Theodore is set up and signed in on this computer.", studio_sentence(studio_state()),
                                          packs_sentence()) if line)
        _remember({"owed": False, "given": True})
        return f"THEODORE NOTICE: {said} Tell the user once, in one or two lines; it does not stop this task."
    except Exception as exc:  # noqa: BLE001  a notice is never the reason instructions fail to load
        core.log(f"welcome: notice not given ({exc.__class__.__name__})")
        return None
