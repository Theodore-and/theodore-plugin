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
import re
import threading
import time
from pathlib import Path

from .. import account, api, core, install, specimens, usage
from ..contract import SKILLS
from ..core import Status
from . import capture, context, workers

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
IMAGE_BYTES_MAX = 3 << 20
IMAGES_BYTES_MAX = 12 << 20
PICTURE_VERBS = ("apply", "render")
NAMES_WAIT_S = 1.2
# The MCP `theodore` tool (an agent's call) runs these; a shell's command line (role front) runs every command but
# those that set up, remove or replace this installation, which the front runs in a client of its own (lane C).
THEODORE_COMMANDS = ("status", "doctor", "report", "prefs", "studio", "packs", "update", "version", "whoami", "login",
                     "usage", "guide", "specimen", "skill", "stop", "help", "--help", "-h")
SHELL_ONLY_HERE = ("uninstall", "setup", "python", "mcp", "service", "install-plugin", "__bootstrap", "pptx")
KEEP_HELD_ON = ("OFFLINE", "RETRY")
REFUSALS = ("SIGN_IN_REQUIRED", "SIGN_IN_PENDING", "NO_ENTITLEMENT", "UPDATE_REQUIRED", "BLOCKED_BY_POLICY", "CONNECTOR_REQUIRED")


HOSTS = {"claude-code": "claude-code", "codex": "codex", "claude-desktop": "claude-desktop", "cowork": "cowork"}


def run(service, call) -> tuple[int, list]:
    env = context.overlay(call.env)
    if "THEODORE_HOST" not in env and HOSTS.get(call.host):     # the host the hello named, when the call names none
        env["THEODORE_HOST"] = HOSTS[call.host]
    # The reader has the plugin's tools: an MCP call, or a shell command from a session whose relay said hello (its
    # session id is the one the host gives both). The texts then use the tool form of their bindings (lanes D, J).
    session = env.get("CLAUDE_CODE_SESSION_ID") or env.get("CODEX_THREAD_ID") or call.session
    if call.client_role == "relay" or service.sessions.get(session) is not None:
        env["THEODORE_TOOLS"] = "1"
    ctx = context.CallContext(session=call.session, host=call.host, env=env, cwd=call.cwd, call_id=call.id,
                              tool=call.tool, progress=call.set_phase, cancelled=call.cancelled, call=call)
    core.begin_call()                   # this thread learns refusals afresh (core.scope_refusals_per_thread)
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


TIMINGS = os.environ.get("THEODORE_SERVICE_TIMINGS") == "1"    # one line a pptx call in service.log (rig proofs)


def tool_pptx(service, call) -> tuple[int, list]:
    args = list(call.args) or ["--help"]
    begun = time.perf_counter()
    coming = service.parts.arriving(args, call)     # a Studio part on its way to the gathering (parts.py)
    try:
        return _pptx(service, call, args, begun, coming)
    finally:
        if coming is not None and not getattr(call, "arrived", False):
            service.parts.arrived(coming)       # refused on its way (licence, setup): no longer coming


def _pptx(service, call, args: list[str], begun: float, coming: str | None) -> tuple[int, list]:
    with capture.captured(stderr=True) as early:
        service.licence.require("pptx")
        install.require_runtime()
        if not READINESS.packs_ready():
            install.wait_for_packs(args)
        from .. import sessions as session_notes
        session_notes.engine_notice(args[0])
        install.enforce_restrictions()
        version = install.engine_for(args)
        install.enforce_restrictions(version)
        install.touch_used(version)
    if early.getvalue():
        call.out(early.getvalue(), "stderr")
    call.set_phase(f"pptx {args[0]}")
    admitted = time.perf_counter()
    env = _engine_env(call)
    here: list[bool] = []

    def not_coming() -> None:               # once: no longer counted on its way to the gathering
        if coming is not None and not here:
            here.append(True)
            service.parts.arrived(coming)
            call.arrived = True
    if service.parts.of_burst(args, call.cwd):  # a designer's first call: the prepare's warm-up has had its time
        service.pool.end_warm_up(version, f"its designers' first call (pptx {' '.join(args[:2])})")
    if args[0] == "apply":                  # a part's own build-ahead: dropped, waited for or cancelled (ahead.py);
        waiting = time.perf_counter()       # waiting, it holds no ready part back
        service.ahead.before_apply(args, call.cwd, on_wait=not_coming)
        call.ahead_wait_ms = int((time.perf_counter() - waiting) * 1000)
    wanted = service.parts.wants(version, args, call)
    not_coming()                            # here now
    if wanted is not None:                  # a Studio part arriving with the others: one build for several (parts.py)
        code, pending = service.parts.run(call, version, args, env, wanted)
    else:
        code, pending = service.pool.run(call, version, args, env, call.stdin)
    call.ran_until = time.perf_counter()    # the timing line's "after": its answer read, its pictures attached
    if args[0] == "prepare" and code == 0:
        _parts_coming(service, version, args, call, env)
    elif args[0] == "slide" and code == 0:
        service.ahead.wrote(args, call.cwd)     # a part's deck.js written through the service: still now
    elif args[0] == "apply" and code == 0:
        service.ahead.applied(args, call.cwd, _last_json("".join(call.stdout_kept)))     # reused its ahead?
    with contextlib.suppress(Exception):
        install._pin_workspaces(args, version)          # noqa: SLF001  the workspace's pin, as the client wrote it
    if code in (1, install.LICENCE_EXIT):     # the engine's licence refusal, read from its last JSON object (L1)
        refusal = install.refusal_in_output("".join(call.stdout_kept), code)
        if refusal is not None:
            return _status(service, call, refusal), pending
    if args[0] in PICTURE_VERBS and not pending:
        pending = pending_of("".join(call.stdout_kept))         # a 2.0 answer's pictures still being drawn
    if call.images and args[0] in PICTURE_VERBS and not call.worker_images and code == 0:
        attach_pictures(call, "".join(call.stdout_kept))
    if args[0] == "finish":
        threading.Thread(target=_after_finish, name="after-finish", daemon=True).start()
    if TIMINGS:                             # written once the done frame is out (lane S2's S2-9: 1 to 38 ms before it)
        ended, gathered = time.perf_counter(), wanted is not None
        call.after.append(lambda: _log_timing(service, call, args, gathered, begun, admitted, code, ended))
    return code, pending


def _log_timing(service, call, args: list[str], gathered: bool, begun: float, admitted: float, code: int,
                ended: float | None = None) -> None:
    """`timing: pptx <verb> <target>: <total> ms = admission + [own ahead] + [gathering] + queue + worker (the engine's
    own) + after`: what a part's apply waited for its own build-ahead, what it waited in the gathering for its build to
    go out, and what came after the worker (its answer read, its pictures attached), each as its own term (lane S2)."""
    with contextlib.suppress(Exception):
        now = ended if ended is not None else time.perf_counter()
        timing = getattr(call, "timing", None) or {}
        target = next((Path(a).name for a in args[1:] if not a.startswith("-")), "")
        parts = [f"admission {int((admitted - begun) * 1000)}"]
        if getattr(call, "ahead_wait_ms", 0):
            parts.append(f"own ahead {call.ahead_wait_ms}")
        if gathered:
            parts.append(f"gathering {timing.get('gathering_ms', '?')} ({timing.get('batch') or 1} part(s) in its build)")
        if timing:
            parts.append(f"queue {timing.get('queue_ms')}")
            parts.append(f"worker {timing.get('run_ms')} (the engine's own {timing.get('engine_ms')}; pid "
                         f"{timing.get('worker')}, call {int(timing.get('worker_calls') or 0) + 1} on it)")
        if getattr(call, "ran_until", None):
            parts.append(f"after {int((now - call.ran_until) * 1000)}")
        spawned = (call.counters or {}).get("spawned")
        engine = list(getattr(call, "engine_timings", []) or []) + list(timing.get("engine_lines") or [])
        phases = _last_json("".join(call.stdout_kept)).get("timings")     # its own answer's phases (lanes F, G)
        if isinstance(phases, dict) and not any(line.startswith("timings: ") and "apply-parts" not in line for line in engine):
            engine.append("answer timings: " + ", ".join(f"{name} {value}" for name, value in list(phases.items())[:40]))
        service.log(f"timing: pptx {args[0]} {target}: {int((now - begun) * 1000)} ms = {' + '.join(parts)}; exit "
                    f"{code}{f'; workers started for it {spawned}' if spawned else ''}; since the call reached the "
                    f"service {int((now - call.started_perf) * 1000)} ms"
                    + "".join(f"\n    {line}" for line in engine[:16]))


def _parts_coming(service, version: str, args: list[str], call, env: dict | None = None) -> None:
    """`prepare`'s answer says how its parts come (lane G): all at once (`in_turn` false; Studio) warms a worker per
    part up to the pool's size (the coordinator after lane S2b's S2-19: the designers' first calls find them ready, and
    the burst starts none of its own), kept for BURST_KEEP_S, and gathers their applies; one at a time (`in_turn`
    true) needs neither. An answer without these (an engine before them) warms the pool to its size."""
    answer = _last_json("".join(call.stdout_kept))
    if answer.get("ok") is False:
        return
    lead = next((arg for arg in args[1:] if not arg.startswith("-")), None)
    if lead is not None and not os.path.isabs(lead):
        lead = os.path.join(call.cwd or ".", lead)
    parts = answer.get("parts")
    count = answer.get("tasks") if isinstance(answer.get("tasks"), int) else (len(parts) if isinstance(parts, list) else None)
    with contextlib.suppress(Exception):         # what a proof reads: how prepare said the parts come
        service.log(f"parts: prepare says {count if count is not None else 'an unknown number of'} parts come "
                    f"{'in turn' if answer.get('in_turn') is True else 'at once' if answer.get('in_turn') is False else '(in_turn not said)'}"
                    f"{'' if answer.get('prepared') == 'parts' else ' (no prepared: parts mark)'}")
    if answer.get("in_turn") is True:
        if lead:
            service.parts.prepared(lead, at_once=False)
        return
    if lead and answer.get("prepared") == "parts":
        service.parts.prepared(lead, at_once=True, count=count)
        with contextlib.suppress(Exception):
            service.ahead.announced(version, lead, parts, env or {}, call.cwd)
    service.pool.warm_up(version, max(1, count) if isinstance(count, int) else None, keep_s=workers.BURST_KEEP_S)


def _after_finish() -> None:
    """The usage counts and the reports kept for later go at the end of a deck, never on the answer's path."""
    from .. import outbox
    with contextlib.suppress(Exception):
        usage.flush(force=True)
    with contextlib.suppress(Exception):
        outbox.flush()


def _last_json(output: str) -> dict:
    """The engine's answer: the last JSON object its stdout holds at the start of a line, on one line or over several,
    with any lines before or after it. `prepare` prints its object indented and then one SLIDE line per part (lane G's
    form: the SLIDE lines are the last a reader sees), so the object is followed by lines, not alone on the last one."""
    text = output.strip()
    decoder = json.JSONDecoder()
    found: dict = {}
    position = 0
    while True:
        start = text.find("{", position)
        while start > 0 and text[start - 1] != "\n":          # an object that starts a line, never one nested in it
            start = text.find("{", start + 1)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            position = start + 1
            continue
        if isinstance(value, dict):
            found = value
        position = end


_PAGE = re.compile(r"slide-0*(\d+)")


def pictures_of(argv: list[str], output: str) -> list[str]:
    """The pictures an answer is about (lane J's rule), at most 12, in the engine's order. A 2.0 engine names them in
    `pictures: [{slide, page, path | pending | error}]` (apply and render alike, lanes F and R), its grid in `grid`
    and its zoom in `zoom`. Today's engine: apply's pages drawn anew (render_summary.regenerated_pages, never the
    cached ones it also lists), render's `grid_files`, `zoom_images` or the pages asked for (`rendered_images`)."""
    payload = _last_json(output)
    verb = argv[0] if argv else ""
    strings = lambda values: [v for v in values or [] if isinstance(v, str)]      # noqa: E731
    if verb == "render" and "--grid" in argv:
        chosen = strings(payload.get("grid") or payload.get("grid_files") or ([payload["grid_path"]] if payload.get("grid_path") else []))
    elif verb == "render" and "--zoom" in argv:
        chosen = strings(payload.get("zoom") or payload.get("zoom_images"))
    elif isinstance(payload.get("pictures"), list):
        chosen = [row["path"] for row in payload["pictures"] if isinstance(row, dict) and isinstance(row.get("path"), str)]
    elif verb == "apply":
        images = strings(payload.get("rendered_images"))
        summary = payload.get("render_summary") or {}
        if "regenerated_pages" in summary:
            drawn = {int(p) for p in summary.get("regenerated_pages") or [] if isinstance(p, int)}
            chosen = []
            for position, path in enumerate(images, 1):
                match = _PAGE.search(Path(path).name)
                if (int(match.group(1)) if match else position) in drawn:
                    chosen.append(path)
        elif summary.get("scoped"):
            # A deck past the full auto-render cap (40 slides): the engine drew only the changed or in-scope slides,
            # and `rendered_images` lists just those (rc.34 and rc.35's apply_auto_render, `render_summary.scoped`).
            chosen = images
        else:
            chosen = []
    elif verb == "render":
        chosen = strings(payload.get("rendered_images"))
    else:
        chosen = []
    return chosen[:12]


def pending_of(output: str) -> list[dict]:
    """The pictures a 2.0 answer says are still being drawn (`pending: [slide ids]`), as C1 `done.pending` items."""
    pending = _last_json(output).get("pending")
    return [{"kind": "picture", "slide": s} for s in pending if isinstance(s, str)] if isinstance(pending, list) else []


def attach_pictures(call, output: str) -> None:
    """The pictures an apply or render is about, as image frames (at most 12; 3 MiB each, 12 MiB in all)."""
    total = 0
    for raw in pictures_of(call.args, output):
        path = Path(raw) if Path(raw).is_absolute() else Path(call.cwd or ".") / raw
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
    allowed = args[0] not in SHELL_ONLY_HERE if call.client_role == "front" else args[0] in THEODORE_COMMANDS
    if not allowed:
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
        # At a session's start the service's own index request is usually on its way: worth a moment, so the host's
        # first tools/list carries the names (later ones come with tools/list_changed).
        deadline = time.monotonic() + NAMES_WAIT_S
        while held_index(service) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        names = tool_names(service)
        # The native relay (lane C) takes the whole list for its mode from here; the Python relay builds it itself.
        names["tools"] = relay_tools(params.get("for"), params.get("client"), names.get("deck-design"))
        call.out(json.dumps(names) + "\n")
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


def relay_tools(mode, client, index: dict | None) -> list[dict]:
    """The tool list a relay serves: the plugin's (C1), or the chat's one tool for codex and the Claude app's chat,
    or none for the plugin's server inside a Claude app that already starts Theodore's own entry."""
    from .. import chatbuild, mcp
    name = str((client or {}).get("name") or "") if isinstance(client, dict) else ""
    if mode in (mcp.DESKTOP, mcp.CODEX):
        return mcp.desktop_tools(index)
    if chatbuild.is_claude_app(name):
        from .. import desktop
        with contextlib.suppress(Exception):
            if desktop.entry_present():
                return []
        return mcp.desktop_tools(index)
    return mcp._plugin_tools(index)              # noqa: SLF001


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
