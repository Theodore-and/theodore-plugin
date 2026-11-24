"""The chat's one tool, `theodore` with an action (the Claude app's chat: `mcp --for claude-desktop`; Codex: `mcp --for
codex`; the plugin's server when its client is the Claude app), served by the service as it serves the others.

What the action does is unchanged (mcp.desktop_call, chatbuild.py): begin, guide, specimen, report, preferences,
check, manual, run, read, write, edit, list, reveal; the confinement of a chat's engine command is chatbuild's. What
chatbuild kept per process (the folders a chat named, the decks it opened, its starts, its notice, whether the client
is the Claude app, the request's _meta and progress) is kept per session here: one service serves every chat.

The answer is the MCP result itself, as JSON on stdout (lane A's request A-5): its content array mixes texts and
images in an order the generic frames do not carry. The relay passes it through as the result.
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading

from .. import account, chatbuild, commands, core, mcp
from ..core import Status
from . import capture, context, tools

_SERVICE = None


class ChatState:
    __slots__ = ("folders", "open", "starts", "notice", "app_client", "meta", "progress")

    def __init__(self):
        self.folders: list = []
        self.open: set = set()
        self.starts: dict = {}
        self.notice: list = []
        self.app_client = False
        self.meta: dict = {}
        self.progress = None


_FALLBACK = ChatState()
_LOCK = threading.Lock()


def _state() -> ChatState:
    ctx = context.current()
    if ctx is None or _SERVICE is None:
        return _FALLBACK
    session = _SERVICE.sessions.get(ctx.session)
    if session is None:
        return _FALLBACK
    with _LOCK:
        if session.chat is None:
            session.chat = ChatState()
            # A new chat session asks the service about the plan's chat feature again (chatbuild.allowed: once per
            # server process, then at most every 10 minutes), as a new server process did.
            chatbuild._FEATURE_ASKED = -chatbuild.FEATURE_RECHECK_S          # noqa: SLF001
        return session.chat


class _Proxy:
    """A chatbuild module global (list, set or dict) that is the current session's own."""

    def __init__(self, field: str):
        object.__setattr__(self, "_field", field)

    def _target(self):
        return getattr(_state(), object.__getattribute__(self, "_field"))

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __iter__(self):
        return iter(self._target())

    def __len__(self):
        return len(self._target())

    def __contains__(self, item):
        return item in self._target()

    def __getitem__(self, key):
        return self._target()[key]

    def __setitem__(self, key, value):
        self._target()[key] = value

    def __delitem__(self, key):
        del self._target()[key]

    def __bool__(self):
        return bool(self._target())


class _Flag:
    def __bool__(self):
        return bool(_state().app_client)


class _Meta(dict):
    """dict for `isinstance(META, dict)`; every read is the session's current _meta."""

    def _target(self):
        return _state().meta

    def get(self, key, default=None):
        return self._target().get(key, default)

    def __getitem__(self, key):
        return self._target()[key]

    def __contains__(self, key):
        return key in self._target()

    def __iter__(self):
        return iter(self._target())

    def __len__(self):
        return len(self._target())

    def keys(self):
        return self._target().keys()

    def items(self):
        return self._target().items()


class _Progress:
    def __bool__(self):
        return _state().progress is not None

    def __call__(self, *args):
        progress = _state().progress
        if progress is not None:
            progress(*args)


def install(service) -> None:
    """In the service: chatbuild's per-process state becomes per-session."""
    global _SERVICE
    _SERVICE = service
    chatbuild.FOLDERS = _Proxy("folders")
    chatbuild.OPEN = _Proxy("open")
    chatbuild.STARTS = _Proxy("starts")
    chatbuild.NOTICE = _Proxy("notice")
    chatbuild.APP_CLIENT = _Flag()
    chatbuild.META = _Meta()
    chatbuild.PROGRESS = _Progress()


def session_closed(session) -> None:
    session.chat = None


# --- the call ---------------------------------------------------------------------------------------------------------------

def call(service, call_) -> int:
    params = dict(call_.params)
    state = _state()
    state.app_client = bool(params.pop("_app_client", state.app_client))
    meta = params.pop("_meta", None)
    state.meta = meta if isinstance(meta, dict) else {}
    state.progress = lambda elapsed, message: call_.set_phase(message)
    try:
        result = _answer(service, params)
    finally:
        state.progress = None
        state.meta = {}
    call_.out(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("isError") else 0


def _answer(service, params: dict) -> dict:
    try:
        return desktop_call(service, params)
    except Status as status:
        return _status(service, status)
    except Exception as exc:  # noqa: BLE001  a tool answers; it never ends the service
        core.log(f"chat {params.get('action')}: {exc.__class__.__name__}: {exc}")
        return _status(service, core.internal(f"{exc.__class__.__name__}: {exc}"))


def _text(text: str, error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": text}]}
    if error:
        result["isError"] = True
    return result


def _status(service, status: Status) -> dict:
    if core.refused() and not getattr(status, "refusal", False):
        status = account.refusal_status()
    elif getattr(status, "auto_report", False):
        status = commands.report_internal(status)
    if core.refused() or status.name not in tools.KEEP_HELD_ON:
        tools._forget_texts(service, refused=core.refused() or (status.name in tools.REFUSALS                # noqa: SLF001
                                                                and not getattr(status, "keeps_sheets", False)))
    return _text(mcp.desktop_text(status.render()), error=status.name != "OK")


def _guide(service, skill: str, section: str | None, role) -> dict:
    text = service.texts.guide(skill, [section] if section else None, role, bindings="chat")[0]
    return _text(text)


def _specimen(service, asked) -> dict:
    names, many = tools.specimen_names(asked)
    results = service.texts.specimens(names, session=(context.current() or context.CallContext()).session)
    notes = [note for _n, raw, note in results if raw is None and note]
    if not any(raw is not None for _n, raw, _note in results):
        from .. import specimens
        raise specimens.none_delivered(notes, f'call {mcp.DESKTOP_TOOL} with action "specimen" again')
    image = lambda raw: {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/jpeg"}
    if not many:
        return {"content": [image(results[0][1])]}
    content: list[dict] = []
    for name, raw, note in results:
        if raw is not None:
            content += [{"type": "text", "text": name}, image(raw)]
        elif note:
            content.append({"type": "text", "text": note})
    return {"content": content}


def _report(params: dict) -> dict:
    kind, title, details = params.get("kind"), params.get("title"), params.get("details")
    if kind not in ("bug", "feedback") or not isinstance(title, str) or not isinstance(details, str):
        raise core.usage('a report needs kind ("bug" or "feedback"), title and details.')
    origin = params.get("origin") if params.get("origin") in ("automatic", "user_requested") else "automatic"
    text = lambda key: params.get(key) if isinstance(params.get(key), str) else None
    return _text(capture.printed(lambda: commands.report(kind, title, details, text("verb"), text("code"), None, origin, False)))


def _preference(action: str, params: dict) -> dict:
    value = params.get("text" if action == "add" else "id")
    if action != "list" and not isinstance(value, str):
        raise core.usage(f'preference_{action} needs {"text, the preference" if action == "add" else "id, as preference_list shows it"}.')
    out = capture.printed(lambda: commands.prefs(action, value if action != "list" else None))
    return _text(out.replace("prefs list", 'action "preference_list"'))


def _check(service) -> dict:
    service.licence.require("deck-design")
    return _status(service, mcp._check_status())             # noqa: SLF001


def texts():
    """The texts of the service this runs in, or of this process (a relay answering on its own)."""
    if _SERVICE is not None and _SERVICE.texts is not None:
        return _SERVICE.texts
    global _OWN_TEXTS
    if _OWN_TEXTS is None:
        from . import texts as texts_module
        current = getattr(texts_module, "current", None)
        _OWN_TEXTS = current() if current is not None else texts_module.Texts()
    return _OWN_TEXTS


_OWN_TEXTS = None


def _desktop_guide(service, params: dict) -> dict:
    """A section of the design guidance, or else of the build instructions (mcp._desktop_guide)."""
    section, role = params["section"], params.get("role")
    if commands._section_name(section).lower() in commands.MANUAL_NAMES:     # noqa: SLF001
        return chatbuild.manual()
    try:
        return _guide(service, "deck-design", section, role)
    except Status as refusal:
        if refusal.name != "USAGE" or "has no section" not in refusal.summary:
            raise
        try:
            return _guide(service, "pptx", section, role)
        except Status as second:
            if second.name == "USAGE" and "has no section" in second.summary:
                raise refusal from None
            raise


def desktop_call(service, params: dict) -> dict:
    """mcp.desktop_call, with the texts from the service."""
    action = params.get("action")
    verb = params.get("args")[0] if action == "run" and isinstance(params.get("args"), list) and params.get("args") else ""
    core.log(f"chat action {action}{' ' + verb if isinstance(verb, str) and verb.isalpha() else ''}")
    if action == "begin":
        result = _guide(service, "deck-design", None, None)
    elif action == "guide":
        if not isinstance(params.get("section"), str) or not params["section"]:
            raise core.usage('action "guide" needs section, the name a link references/<name>.md gives.')
        result = _desktop_guide(service, params)
    elif action == "specimen":
        result = _specimen(service, params.get("file"))
    elif action == "report":
        result = _report(params)
    elif action in ("preference_list", "preference_add", "preference_remove"):
        result = _preference(action.split("_", 1)[1], params)
    elif action == "check":
        result = _check(service)
    elif action == "manual":
        result = chatbuild.manual()
    elif action == "run":
        result = chatbuild.run(params.get("args"), params.get("stdin"), params.get("folder"), params.get("images") is not False)
    elif action == "read":
        result = chatbuild.read(params.get("path"), params.get("folder"), params.get("offset"), params.get("limit"))
    elif action == "write":
        result = chatbuild.write(params.get("path"), params.get("content"), params.get("folder"))
    elif action == "edit":
        result = chatbuild.edit(params.get("path"), params.get("old"), params.get("new"), params.get("folder"), params.get("all") is True)
    elif action == "list":
        result = chatbuild.list_dir(params.get("path"), params.get("folder"))
    elif action == "reveal":
        result = chatbuild.reveal(params.get("path"), params.get("folder"))
    else:
        raise core.usage(f"action is one of: {', '.join(mcp.DESKTOP_ACTIONS)}.")
    if chatbuild.NOTICE and not result.get("isError") and isinstance(result.get("content"), list):
        # The default folder could not be written and the deck went to ~/Theodore: said once, for the user.
        result["content"].insert(0, {"type": "text", "text": "TELL THE USER: " + chatbuild.NOTICE.pop() + "\n"})
        chatbuild.NOTICE.clear()
    if action in chatbuild.FILE_ACTIONS:         # the engine's output and the user's files are passed on as they are
        return result
    for block in result.get("content") or []:
        if block.get("type") == "text":
            block["text"] = mcp.desktop_text(block["text"], begin=action == "begin" and not result.get("isError"))
    if not result.get("isError") and result.get("content"):
        if action == "begin":
            first = result["content"][0]
            first["text"] = first["text"].rstrip("\n") + "\n\n" + chatbuild.BUILD_POINTER + "\n"
        elif action in ("guide", "specimen"):
            result["content"].append({"type": "text", "text": chatbuild.THEN_BUILD})
    return result


__all__ = ["call", "install", "session_closed", "contextlib"]
