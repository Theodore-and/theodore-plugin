"""The service's tools (C1 `call.tool`): pptx, theodore, guide, specimen, list, chat.

Each runs on the call's thread inside its context (context.entered): the environment overlay, the session, the
refusal facts and what it prints are the call's own. A status (core.Status) is answered as the command line answers
it (core.emit: the refusal block once refused, an automatic report when the status asks for one), with its exit code.

- pptx: today's `commands.run_pptx` without its process costs: the licence from memory, readiness facts by stat, the
  engine chosen by its pins, the command run by a warm worker (or one-shot for an engine without one), its pictures
  attached for apply and render, the usage counts and reports sent in the background after finish.
- theodore: the client's own commands, run in this process with their output captured (status, doctor, report,
  prefs, studio, packs, update, version, whoami, login, usage, guide, specimen, skill, and stop).
- guide, specimen, list: the texts (texts.Texts, lane D's): one authorised request per call, bodies in memory.
- chat: the one tool of the Claude app's chat and of Codex (chat.py).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import threading
from pathlib import Path

from .. import account, api, core, install, specimens, usage
from ..contract import ENGINE_REFUSALS, SKILLS
from ..core import Status
from . import capture, context

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
IMAGE_BYTES_MAX = 3 << 20
IMAGES_BYTES_MAX = 12 << 20
PICTURE_VERBS = ("apply", "render")
THEODORE_COMMANDS = ("status", "doctor", "report", "prefs", "studio", "packs", "update", "version", "whoami", "login",
                     "usage", "guide", "specimen", "skill", "stop", "help", "--help", "-h")
KEEP_HELD_ON = ("OFFLINE", "RETRY")
REFUSALS = ("SIGN_IN_REQUIRED", "SIGN_IN_PENDING", "NO_ENTITLEMENT", "UPDATE_REQUIRED", "BLOCKED_BY_POLICY", "CONNECTOR_REQUIRED")


HOSTS = {"claude-code": "claude-code", "codex": "codex", "claude-desktop": "claude-desktop", "cowork": "cowork"}


def run(service, call) -> tuple[int, list]:
    env = context.overlay(call.env)
    if "THEODORE_HOST" not in env and HOSTS.get(call.host):     # the host the hello named, when the call names none
        env["THEODORE_HOST"] = HOSTS[call.host]
    ctx = context.CallContext(session=call.session, host=call.host, env=env, cwd=call.cwd,
                              call_id=call.id, tool=call.tool, progress=call.set_phase, cancelled=call.cancelled)
    with context.entered(ctx):
        handler = HANDLERS[call.tool]
        try:
            result = handler(service, call)
        except Status as status:
            code = _status(service, call, status)
            return code, []
        except api.TooLarge as exc:
            return _status(service, call, core.internal(f"an answer from the Theodore service was longer than {exc.limit} bytes; it was refused.")), []
        code, pending = result if isinstance(result, tuple) else (result, [])
        if core.refused():
            _forget_texts(service, refused=True)
        return code, pending


def _status(service, call, status: Status) -> int:
    """Answer a status as the command line does; drop held texts on a refusal (mcp.call_tool's rule)."""
    with capture.captured() as printed:
        code = core.emit(status)
    call.out(printed.getvalue())
    if core.refused() or status.name not in KEEP_HELD_ON:
        _forget_texts(service, refused=core.refused() or (status.name in REFUSALS and not getattr(status, "keeps_sheets", False)))
    return code


def _forget_texts(service, refused: bool) -> None:
    with contextlib.suppress(Exception):
        service.texts.forget(refused=refused)


# --- pptx -------------------------------------------------------------------------------------------------------------------

class _Readiness:
    """Facts every engine command needed and today re-derived per command, kept by the stat of their files."""

    def __init__(self):
        self._lock = threading.Lock()
        self._packs_key = None
        self._packs_ready = False

    def packs_ready(self) -> bool:
        state_file = core.home().packs / "state.json"
        try:
            info = os.stat(state_file)
            key = (info.st_size, info.st_mtime_ns)
        except OSError:
            key = None
        with self._lock:
            if key == self._packs_key and key is not None:
                return self._packs_ready
        published = install.published_packs()
        ready = published is not None and all(install.pack_ready(p) for p in published)
        with self._lock:
            self._packs_key, self._packs_ready = key, ready
        return ready


READINESS = _Readiness()


def _engine_env(call) -> dict:
    """The overlay a worker applies for this call: the C1 allow-list and the usage ledger while counting is on."""
    env = {k: ("" if v is None else v) for k, v in context.overlay(call.env).items()}
    ledger = usage.engine_env({}).get("THEODORE_USAGE_LEDGER")
    env["THEODORE_USAGE_LEDGER"] = ledger or ""
    return env


def tool_pptx(service, call) -> tuple[int, list]:
    args = list(call.args) or ["--help"]
    with capture.captured(stderr=True) as early:
        service.licence.require("pptx")
        install.require_runtime()
        if not READINESS.packs_ready():
            install.wait_for_packs(args)
        install.wait_for_renderer(args)
        from .. import sessions as session_notes
        session_notes.engine_notice(args[0])
        install.enforce_restrictions()
        version = install.engine_for(args)
        install.enforce_restrictions(version)
        install.touch_used(version)
    if early.getvalue():
        call.out(early.getvalue(), "stderr")
    call.set_phase(f"pptx {args[0]}")
    code, pending = service.pool.run(call, version, args, _engine_env(call), call.stdin)
    with contextlib.suppress(Exception):
        install._pin_workspaces(args, version)          # noqa: SLF001  the workspace's pin, as the client wrote it
    if code in (1, 77):
        refusal = _licence_refusal("".join(call.stdout_kept))
        if refusal is None and code == 77:          # a refusal the engine did not name: the client's general one
            refusal = install._refusal_status("entitlement_malformed")      # noqa: SLF001
        if refusal is not None:
            return _status(service, call, refusal), pending
    if call.images and args[0] in PICTURE_VERBS and not call.worker_images and code == 0:
        attach_pictures(call, "".join(call.stdout_kept))
    if args[0] == "finish":
        threading.Thread(target=_after_finish, name="after-finish", daemon=True).start()
    return code, pending


def _licence_refusal(stdout: str) -> Status | None:
    """The engine's licence refusal, read from its JSON line (no second engine run): the client's status for it."""
    for line in reversed(stdout.strip().splitlines()[-5:]):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        code = answer.get("code") if isinstance(answer, dict) else None
        if ENGINE_REFUSALS.get(code):
            status = install._refusal_status(code)          # noqa: SLF001
            status.cause = code
            return status
        return None
    return None


def _after_finish() -> None:
    """The usage counts and the reports kept for later go at the end of a deck, never on the answer's path."""
    from .. import outbox
    with contextlib.suppress(Exception):
        usage.flush(force=True)
    with contextlib.suppress(Exception):
        outbox.flush()


def attach_pictures(call, output: str) -> None:
    """The pictures an apply or render names, as image frames (at most 12; 3 MiB each, 12 MiB in all)."""
    from .. import chatbuild
    total = 0
    seen: list[str] = []
    for raw in chatbuild._image_candidates(output):             # noqa: SLF001  the same reading as the chat's run
        path = Path(raw)
        if not path.is_absolute() or str(path) in seen:
            continue
        seen.append(str(path))
        try:
            if not path.is_file():
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > IMAGE_BYTES_MAX or total + len(data) > IMAGES_BYTES_MAX:
            continue
        total += len(data)
        mime = IMAGE_TYPES.get(path.suffix.lower(), "image/jpeg")
        if not call.image(mime, str(path), base64.b64encode(data).decode("ascii")):
            break


# --- theodore ------------------------------------------------------------------------------------------------------------

def tool_theodore(service, call) -> int:
    args = list(call.args)
    if not args or args[0] in ("help", "--help", "-h"):
        from .. import cli
        call.out(cli.HELP)
        return 0
    if args[0] not in THEODORE_COMMANDS:
        raise core.usage(f"'{args[0]}' is not run through the Theodore tool; the tool runs: {', '.join(c for c in THEODORE_COMMANDS if not c.startswith('-') and c != 'help')}.",
                         f"run it in a shell: {core.quoted_launcher()} {args[0]}")
    if args[0] == "stop":
        call.out("THEODORE OK: the Theodore service stops now; the next Theodore command starts it again.\n"
                 "NEXT: nothing.\nTELL THE USER: nothing; do not mention this to the user.\n")
        return 0
    stdin_file = None
    if call.stdin is not None and "-" in args:
        stdin_file = _stdin_file(call)
        args = [str(stdin_file) if a == "-" and i and args[i - 1] == "--details" else a for i, a in enumerate(args)]
        args = ["--details-file" if a == "--details" and i + 1 < len(args) and args[i + 1] == str(stdin_file) else a
                for i, a in enumerate(args)]
    from .. import cli
    try:
        with capture.captured(stderr=True) as printed:
            code = cli._run(args)               # noqa: SLF001  the command line's own dispatch and statuses
            last = core.last_emitted()
            if last is not None and last.name != "OK" and code == last.code:
                usage.record_status(args[0], last.name, last.cause)
    finally:
        if stdin_file is not None:
            with contextlib.suppress(OSError):
                stdin_file.unlink()
    call.out(printed.getvalue())
    return code


def _stdin_file(call) -> Path:
    stdin = call.stdin or {}
    data = base64.b64decode(stdin.get("data") or "") if stdin.get("encoding") == "base64" else str(stdin.get("data") or "").encode("utf-8")
    folder = core.home().root / "work" / "scratch"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f".stdin-{os.getpid()}-{threading.get_ident()}.txt"
    core.write_atomic(path, data, 0o600)
    return path


# --- guide, specimen, list ---------------------------------------------------------------------------------------------------

def tool_guide(service, call) -> int:
    params = call.params
    skill, section, role = params.get("skill"), params.get("section"), params.get("role")
    sections = section if isinstance(section, list) else ([section] if section is not None else None)
    texts = service.texts.guide(skill, sections, role, session=call.session, bindings=params.get("bindings") or "tool")
    call.out("\n\n".join(t.rstrip("\n") for t in texts) + "\n" if len(texts) > 1 else (texts[0] if texts else ""))
    return 0


SPECIMEN_MAX = 6


def specimen_names(asked) -> tuple[list[str], bool]:
    """The file names a specimen call asks for (a bare code is its close-up's file), checked as today's tool did."""
    many = isinstance(asked, list)
    names = [specimens.file_name(name) for name in (asked if many else [asked])]
    if many and not 1 <= len(names) <= SPECIMEN_MAX:
        raise core.usage(f"specimen takes one file or a list of 1 to {SPECIMEN_MAX} files; {len(names)} were given. "
                         f"Ask for the rest in another call.")
    if not all(isinstance(name, str) and specimens.valid_name(name) for name in names):
        raise core.usage("a specimen is named as the deck-design text names it: letters, digits, dot, dash and underscore, ending in .jpg.")
    return names, many


def tool_specimen(service, call, again: str = "call the specimen tool again") -> int:
    names, _many = specimen_names(call.params.get("file"))
    results = service.texts.specimens(names, session=call.session)
    notes = [note for _name, raw, note in results if raw is None and note]
    if not any(raw is not None for _name, raw, _note in results):
        raise specimens.none_delivered(notes, again)
    for name, raw, note in results:
        if raw is not None:
            call.image("image/jpeg", name, base64.b64encode(raw).decode("ascii"))
        elif note:
            call.out(note.rstrip("\n") + "\n")
    return 0


def tool_list(service, call) -> int:
    params = call.params
    skill = params.get("skill")
    if params.get("form") == "tools":               # the relay's names for its tool enums (requests-P A-4)
        call.out(json.dumps(tool_names(service)) + "\n")
        return 0
    if skill not in SKILLS:
        raise core.usage(f"unknown skill {skill!r}. The skills are: {', '.join(SKILLS)}.")
    if skill == "pptx":         # the engine's skill folder, on disk
        service.licence.require("pptx")
        version = install.selected_engine()
        if not version:
            raise Status("SETUP_IN_PROGRESS", "Theodore's engine is not installed yet.",
                         f"run {core.quoted_launcher()} guide pptx; it installs the engine.", "Theodore is setting up its PowerPoint engine.")
        root = install.engine_dir(version) / "skills" / "pptx"
        names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith("."))
        call.out("\n".join(names) + "\n")
        return 0
    service.licence.require("deck-design")
    index = service.texts.index("deck-design")
    if index is None:
        raise Status("RETRY", "the list of the deck-design files could not be fetched just now.",
                     "call the same tool again once; the files are also named by the links in the skill's text.",
                     "Theodore could not list its design files just now; I am trying once more.")
    lines = ["SKILL.md"] + [f"references/{n}.md" for n in index["sections"]] + [f"specimens/{f}" for f in index["specimens"]]
    call.out("\n".join(lines) + "\n")
    return 0


def held_index(service, skill: str = "deck-design") -> dict | None:
    """The index the texts hold, with no request (lane D's `held_only`); the session's own request fills it, and the
    texts say so through `notify` (then the relays hear tools/list_changed with the names)."""
    texts = service.texts
    if texts is None:
        return None
    try:
        return texts.index(skill, held_only=True)
    except TypeError:                               # the texts of a client before lane D's (the port of mcp.py)
        return getattr(texts, "_index", None) or texts.index(skill)


def tool_names(service) -> dict:
    """The names the tools' enums carry (requests-P A-4), from what the texts hold: no request."""
    out: dict = {}
    index = held_index(service)
    if index:
        out["deck-design"] = {"sections": list(index.get("sections") or []), "specimens": list(index.get("specimens") or [])}
    return out


def tool_chat(service, call) -> int:
    from . import chat
    return chat.call(service, call)


HANDLERS = {"pptx": tool_pptx, "theodore": tool_theodore, "guide": tool_guide, "specimen": tool_specimen,
            "list": tool_list, "chat": tool_chat}

__all__ = ["run", "HANDLERS", "attach_pictures", "account"]
