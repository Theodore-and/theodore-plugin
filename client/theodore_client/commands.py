"""The commands an assistant runs: guide, report, prefs, status, doctor, setup, packs, tools, update, hosts, uninstall."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import __version__, account, api, core, credentials, install, specimens
from .contract import (DELETE_CREDENTIAL_ON, DELETE_TOKEN_ON, GUIDE_PART_CHARS, HEADER_GUIDE_PARTS, HEADER_GUIDE_VERSION, PACK_ORDER, PLUGIN_LATEST_SCHEMA, PREFERENCE_MAX_CHARS,
                       REPORT_MAX_BYTES, REPORT_MESSAGE_MAX, REPORT_TITLE_MAX, SKILLS)
from .core import Job, Status, quoted_launcher


def _bearer() -> str:
    cred = credentials.load()
    if not cred:
        raise account.login_start()
    return cred["installation_secret"]


def _api(route: str, what: str, *, passing: tuple[str, ...] = (), **kwargs):
    """One authenticated call; every refusal becomes its status, except the error codes in `passing`, which the
    caller answers itself."""
    try:
        return api.call(route, bearer=_bearer(), **kwargs)
    except api.NetworkError as error:
        raise api.offline(error, what) from None
    except api.ApiError as error:
        if error.code in passing:
            raise
        if error.code in ("installation_revoked", "unauthenticated"):
            credentials.delete()
            account.delete_token()
            raise account.login_start("this device was signed out; it needs to sign in again.") from None
        if error.http_status in (401, 403) and error.code != "auto_reports_disabled":
            account.delete_token()
        raise api.status_from(error, what) from None


# --- guide -----------------------------------------------------------------------------------------------------------------

def split_parts(text: str, limit: int = GUIDE_PART_CHARS) -> list[str]:
    """Cut a document into parts of at most `limit` characters, on line ends; the parts joined give the text back."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single line longer than a part: cut it hard
            room = limit - len(current)
            current += line[:room]
            line = line[room:]
            parts.append(current)
            current = ""
        if len(current) + len(line) > limit:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts


def _end_line(skill: str, name: str, body: str, part: int, total: int, next_command: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    label = f"{name} part {part}/{total}" if total > 1 else name
    tail = f". NEXT PART: {next_command}" if part < total else ""
    return f"=== END theodore {skill} {label} ({len(body)} chars, sha256 {digest}){tail} ==="


def _print_document(skill: str, version: str, plan: str, name: str, body: str, part: int, total: int,
                    header_lines: list[str], next_command: str) -> None:
    if core.refused():          # a revoked device or a suspended account gets no instructions, whichever call learned it
        raise api.sign_in_required()
    out = [f"=== THEODORE {skill} {version} ({plan}), part {part}/{total} ==="]
    out += header_lines
    out.append(f"--- {name} ---")
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.write(body if body.endswith("\n") else body + "\n")
    sys.stdout.write(_end_line(skill, name, body, part, total, next_command) + "\n")
    sys.stdout.flush()


def guide(skill: str, section: str | None, part: int, license_text: bool) -> int:
    if skill not in SKILLS:
        raise core.usage(f"unknown skill '{skill}'. The skills are: {', '.join(SKILLS)}.")
    if part < 1:
        raise core.usage("--part starts at 1.")
    if skill == "pptx":
        if section or license_text:
            raise core.usage("guide pptx takes no section: the rest of the pptx manual is read from the engine folder named in its header.")
        return _guide_pptx(part)
    return _guide_remote(skill, "license" if license_text else section, part)


def _guide_pptx(part: int) -> int:
    claims = account.require("pptx")
    install.require_runtime()
    first = install.current_engine() is None
    wait = 15.0
    if core.is_loopback_api() and os.environ.get("THEODORE_TEST_UPDATE_WAIT"):
        wait = float(os.environ["THEODORE_TEST_UPDATE_WAIT"])
    install.check_updates(force=first, wait=wait, timeout=20.0 if first else 3.0)
    version = install.selected_engine()          # the global pin's instructions when pinned: they belong to that engine
    if not version:
        state = Job("engine-update").read()
        if state.get("failed") and not Job("engine-update").alive():
            Job("engine-update").clear()
            raise core.internal(f"the engine could not be installed: {state['failed'].get('detail', 'unknown error')}")
        raise Status("SETUP_IN_PROGRESS", f"Theodore's engine is still downloading ({int(state.get('percent') or 0)}%).",
                     "run the same command again in 20 seconds. Do not use another slide tool meanwhile.",
                     "Theodore is downloading its PowerPoint engine. I will continue as soon as it is ready.")
    directory = install.engine_dir(version)
    try:
        text = (directory / "skills" / "pptx" / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        raise core.internal(f"engine {version} has no skills/pptx/SKILL.md.") from None
    install.touch_used(version)
    _heartbeat(version)             # before anything is printed: a revoked device gets no part of the manual
    parts = split_parts(text)
    if part > len(parts):
        raise core.usage(f"guide pptx has {len(parts)} part(s); there is no part {part}.")
    launcher = quoted_launcher()
    header = []
    if part == 1:
        header = [f"Bindings: pptx <verb>  ->  {launcher} pptx <verb>",
                  f"          python3 ...  ->  {launcher} python ...",
                  f"          <pptx-skill-dir> = {directory / 'skills' / 'pptx'}   (read pptxgenjs.md and references/ there)"]
        directions = _directions("pptx")
        if directions:
            header += directions.rstrip("\n").split("\n")
    _print_document("pptx", version, str(claims.get("plan", "unknown")), "SKILL.md", parts[part - 1], part, len(parts), header,
                    f"{launcher} guide pptx --part {part + 1}")
    return 0


def _directions(skill: str) -> str:
    """Server-composed directions for this account (beta reporting, saved preferences). Optional: silent on failure."""
    cred = credentials.load()
    if not cred:
        return ""
    try:
        _status, _headers, raw = api.call("guide", bearer=cred["installation_secret"], skill=skill, timeout=3.0)
        return raw.decode("utf-8", errors="replace")
    except api.ApiError as error:
        if error.code in DELETE_CREDENTIAL_ON:      # the device was revoked: no instructions, and the sign-in is already cleared
            raise account.login_start("this device was signed out; it needs to sign in again.") from None
        if error.code in DELETE_TOKEN_ON:           # the account no longer has the skill
            raise api.status_from(error, f"guide {skill}") from None
        return ""
    except api.NetworkError:
        return ""


REFERENCE_LINK = re.compile(r"\]\(references/([A-Za-z0-9_-]{1,64})\.md\)")
SECTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _section_name(given: str) -> str:
    """A section as the text names it: `critique`, `critique.md` and `references/critique.md` are the same section."""
    name = given.strip()
    for prefix in ("./", "references/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name[:-3] if name.endswith(".md") else name


def _no_such_section(skill: str, given: str) -> Status:
    """Which sections there are: the ones the skill's main text links, fetched now (the text is never stored)."""
    names: list[str] = []
    try:
        _status, _headers, raw = _api("guide", f"guide {skill}", skill=skill)
        for name in REFERENCE_LINK.findall(raw.decode("utf-8", errors="replace")):
            if name not in names:
                names.append(name)
    except Status as status:
        if status.name != "USAGE":
            raise
    launcher = quoted_launcher()
    shown = "".join(ch for ch in given if ch.isprintable())[:80]
    if names:
        return core.usage(f"guide {skill} has no section '{shown}'. Sections: {', '.join(names)}.",
                          f"run {launcher} guide {skill} <section> with one of those names.")
    return core.usage(f"guide {skill} has no section '{shown}'.", f"run {launcher} guide {skill}; its text names the sections it has.")


def _guide_remote(skill: str, section: str | None, part: int) -> int:
    claims = account.require(skill)
    query = {"part": part} if part > 1 else None
    what = f"guide {skill}"
    if section and section != "license":
        given, section = section, _section_name(section)
        if not SECTION_NAME.fullmatch(section):
            raise _no_such_section(skill, _section_name(given))
        try:
            _status, headers, raw = _api("guide_section", what, passing=("not_found",), skill=skill, section=section, query=query)
        except api.ApiError:
            if part > 1:
                raise core.usage(f"{what} {section} has no part {part}.") from None
            raise _no_such_section(skill, section) from None
    elif section:
        _status, headers, raw = _api("guide_section", what, skill=skill, section=section, query=query)
    else:
        _status, headers, raw = _api("guide", what, skill=skill, query=query)
    body = raw.decode("utf-8", errors="replace")
    total = max(1, int(headers.get(HEADER_GUIDE_PARTS) or 1))
    label = headers.get(HEADER_GUIDE_VERSION) or "current"
    if total == 1 and len(body) > GUIDE_PART_CHARS:  # the server did not page: page here so no output is truncated
        parts = split_parts(body)
        total = len(parts)
        if part > total:
            raise core.usage(f"{what} has {total} part(s); there is no part {part}.")
        body = parts[part - 1]
    elif part > total:
        raise core.usage(f"{what} has {total} part(s); there is no part {part}.")
    name = section or "SKILL.md"
    if section != "license":       # a reference is a section of this guide, not a file: its link becomes the command that prints it
        body = REFERENCE_LINK.sub(lambda match: f"](run: {quoted_launcher()} guide {skill} {match.group(1)})", body)
    if skill == "deck-design" and section != "license":      # sheets are named, never shipped: say how to look at one
        body = specimens.guide_links(body, quoted_launcher())
    next_command = f"{quoted_launcher()} guide {skill}{' ' + section if section and section != 'license' else ''}{' --license' if section == 'license' else ''} --part {part + 1}"
    _print_document(skill, label, str(claims.get("plan", "unknown")), name, body, part, total, [], next_command)
    return 0


# --- skill: the instructions as a skill file's body -------------------------------------------------------------------------

SKILL_BODY_MAX_CHARS = 28000      # a host shows an injected output whole only below about 30,000 characters


def _without_frontmatter(text: str) -> str:
    """A skill file's body: the text after its YAML front matter, as a host shows a SKILL.md."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5:].lstrip("\n")
    return text


def _bounded(text: str, rest: str) -> str:
    """At most SKILL_BODY_MAX_CHARS: past that, the text up to a line end within the limit and one line naming where
    the rest is."""
    if len(text) <= SKILL_BODY_MAX_CHARS:
        return text
    pointer = f"\n[The rest of this document: {rest}]\n"
    room = SKILL_BODY_MAX_CHARS - len(pointer)
    cut = text.rfind("\n", 0, room)
    return text[:cut + 1 if cut > 0 else room] + pointer


def _deck_design_body() -> str:
    account.require("deck-design")
    # Claude Code has the plugin's guide tool (its wording for the links), except on Windows, where the plugin cannot
    # register its server (sh is not on the path Claude Code spawns from): there the launcher's wording.
    tool = core.host_name() == "claude-code" and not core.IS_WINDOWS
    query = {"form": "whole", **({"bindings": "tool"} if tool else {})}
    _s, headers, raw = _api("guide", "guide deck-design", skill="deck-design", query=query)
    chunks = [raw]
    for part in range(2, max(1, int(headers.get(HEADER_GUIDE_PARTS) or 1)) + 1):     # an older service that still pages
        _s, _h, more = _api("guide", "guide deck-design", skill="deck-design", query={**query, "part": part})
        chunks.append(more)
    if core.refused():
        raise api.sign_in_required()
    rest = 'call the Theodore guide tool with skill "deck-design"' if tool else f"run {quoted_launcher()} guide deck-design"
    return _bounded(_without_frontmatter(b"".join(chunks).decode("utf-8", errors="replace")), rest)


def _pptx_body() -> str:
    claims = account.require("pptx")
    del claims
    install.require_runtime()
    first = install.current_engine() is None
    install.check_updates(force=first, wait=15.0, timeout=20.0 if first else 3.0)
    version = install.selected_engine()
    if not version:
        state = Job("engine-update").read()
        raise Status("SETUP_IN_PROGRESS", f"Theodore's engine is still downloading ({int(state.get('percent') or 0)}%).",
                     f"run {quoted_launcher()} guide pptx in 20 seconds and follow it. Do not use another slide tool meanwhile.",
                     "Theodore is downloading its PowerPoint engine. I will continue as soon as it is ready.")
    directory = install.engine_dir(version)
    try:
        text = (directory / "skills" / "pptx" / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        raise core.internal(f"engine {version} has no skills/pptx/SKILL.md.") from None
    install.touch_used(version)
    _heartbeat(version)
    if core.refused():
        raise api.sign_in_required()
    launcher = quoted_launcher()
    header = [f"Bindings: pptx <verb>  ->  {launcher} pptx <verb>",
              f"          python3 ...  ->  {launcher} python ...",
              f"          <pptx-skill-dir> = {directory / 'skills' / 'pptx'}   (read pptxgenjs.md and references/ there)"]
    directions = _directions("pptx")
    if directions:
        header += directions.rstrip("\n").split("\n")
    body = "\n".join(header) + "\n\n" + _without_frontmatter(text)
    return _bounded(body, f"run {launcher} guide pptx")


def skill(name: str) -> int:
    """`theodore skill <name>`: the skill's instructions as a clean document (no framing), or a three-line status.
    Always exit 0: a host that injects this output aborts the whole skill on any other code."""
    try:
        if name not in SKILLS:
            raise core.usage(f"unknown skill '{name}'. The skills are: {', '.join(SKILLS)}.")
        body = _deck_design_body() if name == "deck-design" else _pptx_body()
    except Status as status:
        core.emit(status)               # a revoked or suspended device gets that refusal instead, once
        return 0
    sys.stdout.write(body if body.endswith("\n") else body + "\n")
    sys.stdout.flush()
    return 0


def specimen(files: list[str]) -> int:
    """Fetch deck-design specimen sheets into a private temporary folder of this run and print one path per line.
    All or nothing: a sheet that is refused leaves no folder and no path."""
    if not 1 <= len(files) <= specimens.MAX_FILES:
        raise core.usage(f"specimen takes 1 to {specimens.MAX_FILES} file names, as the deck-design text gives them.")
    if not all(specimens.valid_name(name) for name in files):
        raise core.usage("a specimen is named as the deck-design text names it: letters, digits, dot, dash and underscore, ending in .jpg.")
    account.require("deck-design")
    folder = specimens.make_folder()
    try:
        paths: dict[str, Path] = {}
        for name in files:
            if name in paths:
                continue
            what = f"specimen {name}"
            try:
                _status, headers, raw = _api("guide_specimen", what, file=name, query={"skill": "deck-design"},
                                             headers={"Accept": "image/jpeg, application/json;q=0.9"}, redirects=False,
                                             limit=specimens.MAX_BYTES)
            except api.TooLarge:
                raise core.internal(f"{what}: the answer is larger than 1 MiB; it was refused and nothing was kept.") from None
            if not str(headers.get("Content-Type") or "").lower().startswith("image/jpeg"):
                raise core.internal(f"{what}: the answer is not a JPEG image; it was refused and nothing was kept.")
            if str(headers.get("X-Theodore-Sha256") or "").strip().lower() != hashlib.sha256(raw).hexdigest():
                raise Status("RETRY", f"{what}: the sheet did not arrive as the service sent it (sha256 differs or is missing); nothing was kept.",
                             "run the same command again once. If it fails again, continue without the sheet.",
                             "A design specimen arrived damaged. I am trying once more.")
            paths[name] = specimens.write(folder, name, raw)
    except BaseException:
        core.remove_tree(folder)
        raise
    for name in files:
        print(paths[name].resolve().as_posix(), flush=True)
    return 0


def _heartbeat(engine_version: str) -> None:
    state = install.update_state()
    if core.now() - float(state.get("last_heartbeat") or 0) < 3600:
        return
    cred = credentials.load()
    if not cred:
        return
    install.save_update_state(last_heartbeat=int(core.now()))
    body = {"engine_version": engine_version, "plugin_version": core.plugin_version(),
            "client_version": __version__, "host": core.host_name()}
    try:
        api.call("heartbeat", bearer=cred["installation_secret"], body=body, timeout=2.0)
    except (api.ApiError, api.NetworkError):
        pass


# --- engine pass-through -----------------------------------------------------------------------------------------------------

def run_pptx(args: list[str]) -> int:
    account.require("pptx", allow_refresh=False)
    install.require_runtime()
    install.wait_for_packs(args)
    install.wait_for_renderer(args)
    return install.run_engine(args)


def run_python(args: list[str]) -> int:
    account.require("pptx", allow_refresh=False)
    install.require_runtime()
    return install.run_engine(args, python_passthrough=True)


# --- reports and preferences -------------------------------------------------------------------------------------------------

# Text in quotes is what an assistant copies from a slide, a note or a file: it goes as a whole, whatever it says.
_QUOTED = re.compile(r'"[^"\n]{1,2000}"|\u201c[^\u201d\n]{1,2000}\u201d|\u2018[^\u2019\n]*\s[^\u2019\n]*\u2019|\u00ab[^\u00bb\n]{1,2000}\u00bb|'
                     r"(?<![A-Za-z0-9])'[^'\n]*\s[^'\n]*'(?![A-Za-z0-9])")


def scrub(text: str) -> str:
    """What may leave the machine in a report: the server's redactor applied here first (redact.py, the same rules
    and the same case table: paths with spaces removed whole, file names with the words before them unless they are
    diagnostic words, credentials in URLs and pairs, addresses, keys), then quoted text, which is slide or file
    content, replaced as a whole."""
    from .redact import redact_free_text, redact_script_names
    text = redact_script_names(redact_free_text(text))
    text = _QUOTED.sub("[text]", text)
    return " ".join(text.split()).strip()


def _os_name() -> str:
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if core.IS_WINDOWS:
        return f"Windows {platform.version()}"
    return f"{platform.system()} {platform.release()}"


def report(kind: str, title: str, details: str, verb: str | None, code: str | None, workspace: str | None,
           origin: str, dry_run: bool) -> int:
    _system, arch = core.platform_arch()
    detail_block = {"message": scrub(details)[:REPORT_MESSAGE_MAX]}
    if verb:
        detail_block["verb"] = scrub(verb)[:64]
    if code:
        detail_block["error_code"] = scrub(code)[:64]
    engine_version = install.current_engine()
    if workspace:  # only the engine version that workspace is pinned to; never its path or content
        engine_version = core.read_json(Path(workspace) / ".pptx-state" / "theodore-engine.json").get("version") or engine_version
    title = scrub(title)[:REPORT_TITLE_MAX]
    if len(title) < 3:
        raise core.usage("--title needs at least three characters that are not a path, a file name or an address.")
    environment = {"os": _os_name()[:64], "arch": arch, "host": core.host_name(), "client_version": __version__,
                   "plugin_version": core.plugin_version()}
    if engine_version:
        environment["engine_version"] = engine_version
    payload = {
        "client_report_id": uuid.uuid4().hex,
        "kind": kind,
        "origin": origin,
        "title": title,
        "summary": (scrub(details) or "(no description)")[:500],
        "details": detail_block,
        "environment": environment,
    }
    if len(json.dumps(payload).encode("utf-8")) > REPORT_MAX_BYTES:
        raise core.usage("the report is larger than 64 KB.", "shorten --details and run the command again.")
    if dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if origin != "user_requested":
        allowed, why = account.automatic_reports()
        if why == "unknown":        # learn the switch from a licence refresh first; never guess "on"
            try:
                account.refresh(timeout=8.0)
            except (api.ApiError, api.NetworkError):
                pass
            allowed, why = account.automatic_reports()
        if not allowed:     # known before sending: nothing leaves this computer
            summary = {"off": "automatic reports are off for this account; nothing was sent.",
                       "plan": "automatic reports are not part of this account's plan; nothing was sent.",
                       "unknown": "whether automatic reports are on for this account is not known yet; nothing was sent."}[why]
            return core.emit(Status("BLOCKED_BY_POLICY", summary, _REPORTS_OFF_NEXT, None))
    try:
        _status, _headers, raw = _api("reports", "report", body=payload)
    except Status as status_:
        if status_.name == "BLOCKED_BY_POLICY":      # the server's backstop: the report reached it and was refused
            account.remember_automatic_reports_off()
            return core.emit(Status("BLOCKED_BY_POLICY", "automatic reports are off for this account; the Theodore service refused this "
                                    "report and did not store it.", _REPORTS_OFF_NEXT, None))
        raise
    answer = json.loads(raw.decode("utf-8") or "{}")
    reference = answer.get('ref') or answer.get('id') or 'received'
    if origin != "user_requested":  # automatic reports are silent: no line for the user
        return core.emit(Status("OK", f"automatic report filed as {reference}.",
                                "continue with the user's task; do not mention the report unless the user asks.", None))
    return core.emit(Status("OK", f"report filed as {reference}.", "continue with the user's task.",
                            f"I sent Theodore a technical bug report (no deck content). You can see it at {core.site_name('/app/reports')}."
                            if kind == "bug" else "I sent your feedback to Theodore."))


_REPORTS_OFF_NEXT = ("do not retry and do not ask the user about it; continue with the user's task. Send a report only when the "
                     "user explicitly asks for one, with --origin user_requested.")


def prefs(action: str, value: str | None) -> int:
    if action == "list":
        _status, _headers, raw = _api("preferences_list", "preferences")
        items = json.loads(raw.decode("utf-8") or "{}").get("preferences") or []
        for item in items:
            print(f"{item.get('id')}\t{item.get('text')}")
        return core.emit(Status("OK", f"{len(items)} saved preference(s).", "nothing to do.",
                                f"You have {len(items)} saved Theodore preference(s)."))
    if not value:
        raise core.usage(f"prefs {action} needs {'the preference text' if action == 'add' else 'the id shown by prefs list'}.")
    if action == "add":
        text = " ".join(value.split())
        if len(text) > PREFERENCE_MAX_CHARS:
            raise core.usage(f"a preference is one line of at most {PREFERENCE_MAX_CHARS} characters ({len(text)} given).",
                             "shorten it and run the command again.")
        _status, _headers, raw = _api("preferences_add", "preferences", body={"text": text})
        answer = json.loads(raw.decode("utf-8") or "{}").get("preference") or {}
        return core.emit(Status("OK", f"preference saved (id {answer.get('id', 'unknown')}).",
                                "apply it now; it will also be included in later design instructions.",
                                f"I saved that as a lasting Theodore preference. You can edit it at {core.site_name('/app/preferences')}."))
    _api("preferences_remove", "preferences", id=value)
    return core.emit(Status("OK", f"preference {value} removed.", "nothing to do.", "I removed that Theodore preference."))


# --- setup, status, doctor ---------------------------------------------------------------------------------------------------

def setup_job() -> int:
    """Stage 2 of the bootstrap, run detached: the rest of the runtime, then the engine and packs when signed in."""
    job = Job("setup")
    job.begin("runtime")
    try:
        install.install_runtime(job)
        account.import_pending_activation()
        if credentials.load():
            try:
                manifest = install.fetch_channel_manifest()
                # The first manifest counts like any later one: its restrictions are kept and enforced, and a
                # forbidden engine is not installed. A refusal is not recorded as a check, so the next guide asks again.
                install.save_restrictions(manifest)
                install.enforce_restrictions()
                entry = install.engine_artifact(manifest)
                if install.forbidden(entry.get("version")):
                    raise Status("UPDATE_REQUIRED", f"the engine this channel offers ({entry.get('version')}) is withdrawn or below the minimum.",
                                 "wait for a newer release.", "Theodore is waiting for a new release.")
                version = install.stage_engine(entry, job)
                if not install.current_engine():
                    install.activate_checked(version, install._manifest_time(manifest.get("server_time")), "setup")
                install.save_update_state(last_check=int(core.now()), latest=version, guides=manifest.get("guides") or {},
                                          tools=manifest.get("tools") or {})
                install.start_packs_job(manifest.get("packs") or [])
            except (Status, api.ApiError, api.NetworkError, install.EngineError, install.StaleDocument, KeyError) as exc:
                core.log(f"setup: engine not installed yet: {getattr(exc, 'summary', None) or getattr(exc, 'message', exc)}")
        job.finish()
        return 0
    except Status as status:
        match = re.match(r"cause ([A-Z_]+): (.*)", status.summary)
        job.fail(match.group(1) if match else status.name, match.group(2) if match else status.summary)
        return status.code
    except Exception as exc:  # the job has no terminal: record anything
        job.fail("INTERNAL", f"{exc.__class__.__name__}: {exc}")
        return 78


def setup(wait: float) -> int:
    started = time.monotonic()
    install.require_runtime(wait=wait)
    Job("setup").wait(max(0.0, wait - (time.monotonic() - started)))     # the job goes on to the engine when signed in
    return core.emit(Status("OK", "Theodore's runtime is installed.", "continue.", "Theodore's runtime is ready."))


def _snapshot() -> dict:
    home = core.home()
    rid = core.read_text(home.runtime / "current.txt")
    token, claims = account.stored_token()
    engines = install.engines_state()
    packs_state = install.packs_state()
    return {
        "home": str(home.root),
        "client_version": __version__,
        "plugin_version": core.plugin_version(),
        "launcher_generation": core.config().get("launcher_generation"),
        "runtime_id": rid,
        "runtime_ready": install.runtime_ready(rid) if rid else False,
        "setup_job": Job("setup").read(),
        "engine": install.current_engine(),
        "engine_kind": core.read_json(install.engine_dir(install.current_engine() or "none") / ".theodore-engine.json").get("kind"),
        "engine_previous": engines.get("previous"),
        "engine_pinned": install.pins().get("engine"),
        "signed_in": bool(credentials.load()) or bool(token),
        "plan": claims.get("plan"),
        "features": claims.get("feat") or [],
        "offline_until": int(claims.get("exp") or 0),
        "packs": {p: {"status": packs_state.get(p, {}).get("status", "missing"), "version": packs_state.get(p, {}).get("version"),
                      "percent": packs_state.get(p, {}).get("percent", 0)} for p in PACK_ORDER},
        "renderer": {"libreoffice": install.system_libreoffice() or (str(install.libreoffice_binary()) if install.libreoffice_binary() else None),
                     "pdftoppm": shutil.which("pdftoppm")},
    }


def _blocking_status(snap: dict) -> Status | None:
    if not snap["runtime_ready"]:
        return install.setup_status()
    if not snap["signed_in"]:
        return api.sign_in_required()
    if not snap["engine"]:
        return Status("SETUP_IN_PROGRESS", "the engine is not installed yet.",
                      f"run {quoted_launcher()} guide pptx; it installs the engine and prints the instructions.",
                      "Theodore's engine is not installed yet. I am setting it up.")
    return None


def status(as_json: bool, wait: float) -> int:
    account.import_pending_activation()
    deadline = time.monotonic() + max(0.0, wait)
    snap = _snapshot()
    while not snap["runtime_ready"] and time.monotonic() < deadline:
        if not Job("setup").alive():
            install.setup_status()
        time.sleep(1.0)
        snap = _snapshot()
    if as_json:
        print(json.dumps(snap, indent=2, sort_keys=True))
        return 0
    blocking = _blocking_status(snap)
    if blocking:
        return core.emit(blocking)
    published = install.published_packs()
    waiting = [p for p, s in snap["packs"].items() if s["status"] != "ready" and (published is None or p in published)]
    packs_note = install.NO_PACKS if published == [] else "all asset packs are installed" if not waiting else \
        f"asset packs {'still arriving' if Job('packs').alive() else 'not installed'}: {', '.join(waiting)}"
    return core.emit(Status("OK", f"engine {snap['engine']} on runtime {snap['runtime_id']}, plan {snap['plan']}; {packs_note}.",
                            f"run {quoted_launcher()} guide pptx for the instructions.", "Theodore is ready."))


def doctor(as_json: bool) -> int:
    account.import_pending_activation()
    home = core.home()
    snap = _snapshot()
    checks: list[tuple[str, str, str]] = []

    def add(level: str, name: str, detail: str) -> None:
        checks.append((level, name, detail))

    writable = os.access(home.root, os.W_OK)
    add("ok" if writable else "fail", "data directory", f"{home.root}{'' if writable else ' is not writable'}")
    add("ok" if home.launcher().exists() else "warn", "launcher", f"{home.launcher()} (generation {snap['launcher_generation']}, plugin {snap['plugin_version']})")
    add("ok", "client", f"{__version__} on Python {platform.python_version()}")
    if core.windows_on_arm():
        add("warn", "processor", "Windows on ARM: Theodore runs its Windows x64 build through Windows' x64 emulation "
            "(no native ARM build yet); it works, more slowly than on an x64 computer")
    if snap["runtime_ready"]:
        node = home.runtime / snap["runtime_id"] / "bin" / ("node.exe" if core.IS_WINDOWS else "node")
        try:
            node_version = subprocess.run([str(node), "--version"], capture_output=True, text=True, timeout=30).stdout.strip() or "did not answer"
        except (OSError, subprocess.TimeoutExpired):
            node_version = "did not start"
        add("ok" if node_version.startswith("v") else "fail", "runtime", f"{snap['runtime_id']} (Node {node_version})")
    else:
        add("fail", "runtime", "not installed yet")
    add("ok" if snap["engine"] else "fail", "engine", f"{snap['engine'] or 'not installed'}" + (f" ({snap['engine_kind']})" if snap["engine_kind"] else "")
        + (f" (previous {snap['engine_previous']})" if snap["engine_previous"] else "") + (f" (pinned to {snap['engine_pinned']})" if snap["engine_pinned"] else ""))
    if snap["signed_in"]:
        until = time.strftime("%Y-%m-%d", time.gmtime(snap["offline_until"])) if snap["offline_until"] else "unknown"
        add("ok", "sign-in", f"plan {snap['plan']}, skills {', '.join(snap['features']) or 'none'}, works offline until {until}, credential in {credentials.describe()}")
    else:
        add("fail", "sign-in", "this computer is not signed in")
    try:
        api.call("keys", timeout=5.0)
        add("ok", "service", f"{core.config().get('api_base')} reachable")
    except api.NetworkError as error:
        add("warn", "service", f"{core.config().get('api_base')} not reachable ({error.cause}: {error.detail})")
    except api.ApiError as error:
        add("warn", "service", f"{core.config().get('api_base')} answered HTTP {error.http_status}")
    office = snap["renderer"]["libreoffice"]
    add("ok" if office else "warn", "renderer", f"LibreOffice at {office}" if office else
        f"no LibreOffice found: slide previews are unavailable until `{quoted_launcher()} tools install libreoffice` has run")
    try:
        import importlib.util
        pdfium = importlib.util.find_spec("pypdfium2") is not None
    except (ImportError, ValueError):
        pdfium = False
    raster = snap["renderer"]["pdftoppm"] or ("built-in PDFium" if pdfium else None)
    add("ok" if raster else "warn", "rasteriser", str(raster or "neither pdftoppm nor PDFium found"))
    published = install.published_packs()
    if published == []:
        add("ok", "packs", install.NO_PACKS)
    for pack, info in snap["packs"].items():
        if published is not None and pack not in published:
            continue
        add("ok" if info["status"] == "ready" else "warn", f"pack {pack}",
            f"{info['version']}" if info["status"] == "ready" else f"{info['status']} ({info['percent']}%)")
    for name in ("claude", "codex"):
        add("ok", f"assistant {name}", shutil.which(name) or "not found on PATH")
    if core.IS_WINDOWS and shutil.which("claude") and not claude_git_bash():
        add("warn", "claude git bash", "not found: Claude Code then runs commands in PowerShell, where Theodore's skills cannot "
            "load their instructions; Git for Windows (https://git-scm.com/downloads/win) provides Git Bash")
    final = _doctor_verdict(checks, snap, home)
    if as_json:         # the same verdict, as the exit code and in the document
        print(json.dumps({"checks": [{"level": l, "name": n, "detail": d} for l, n, d in checks], "snapshot": snap,
                          "status": final.name}, indent=2, sort_keys=True))
        return final.code
    for level, name, detail in checks:
        print(f"{level.upper():4}  {name}: {detail}")
    return core.emit(final)


def claude_git_bash() -> str | None:
    """The Git Bash Claude Code would use on Windows, found the way it looks: its setting, then Git's folder from the
    `git` on PATH, then the usual install folders. None when there is none (Claude Code then uses PowerShell)."""
    settings = core.read_json(_claude_settings_path())
    configured = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or ((settings.get("env") or {}) if isinstance(settings, dict) else {}).get("CLAUDE_CODE_GIT_BASH_PATH")
    candidates = [Path(configured)] if configured else []
    git = shutil.which("git")
    if git:
        candidates.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramW6432"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs") if os.environ.get("LOCALAPPDATA") else None):
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _doctor_verdict(checks: list, snap: dict, home) -> Status:
    blocking = _blocking_status(snap)
    if blocking:
        return blocking
    failed = [(n, d) for l, n, d in checks if l == "fail"]
    if any(n == "data directory" for n, _d in failed):
        return api.setup_failed("DISK", f"{home.root} is not writable")
    if failed:          # the last line never says ready when a check failed
        what = "; ".join(f"{n} ({d})" for n, d in failed)
        return (Status("INTERNAL", f"Theodore is installed but not ready: {what}.",
                                f"tell the user what failed; run {quoted_launcher()} doctor again after fixing it, or report it with: "
                                f"{quoted_launcher()} report --kind bug --origin user_requested",
                                f"Theodore is installed but not working yet: {', '.join(n for n, _d in failed)} failed its check. I can report this to Theodore."))
    warned = {n for l, n, _d in checks if l == "warn"}
    notes = []
    if any(n.startswith("pack ") for n in warned):
        notes.append("Its icon, flag, map and background libraries are still downloading in the background." if Job("packs").alive()
                     else "Some of its icon, flag, map and background libraries are not installed yet.")
    if "renderer" in warned:
        notes.append("Slide previews need LibreOffice, which is still downloading or not installed." if Job("libreoffice").alive()
                     else "Slide previews need LibreOffice, which is not installed.")
    if "claude git bash" in warned:
        notes.append("Claude Code on this computer needs Git for Windows (git-scm.com) to load Theodore's skills; install it, then start a new session.")
    if "processor" in warned:
        notes.append("This computer runs Windows on ARM, so Theodore uses its x64 build through Windows' emulation, which is slower.")
    if "service" in warned:
        notes.append("The Theodore service could not be reached just now.")
    if "rasteriser" in warned or "launcher" in warned:
        notes.append("See the check list above for details.")
    note = (" " + " ".join(notes)) if notes else ""
    return (Status("OK", f"Theodore is ready: engine {snap['engine']}, plan {snap['plan']}.{note}",
                            "start a new session if the Theodore skills are not listed yet, then ask for a deck.",
                            f"Theodore is ready (engine {snap['engine']}).{note} Start a new session and ask for a deck."))


def version() -> int:
    snap = _snapshot()
    print(f"theodore client {__version__}, plugin {snap['plugin_version']} (launcher generation {snap['launcher_generation']}), "
          f"engine {snap['engine'] or 'none'}, runtime {snap['runtime_id'] or 'none'}")
    return 0


# --- update --------------------------------------------------------------------------------------------------------------------

def update(action: str, value: str | None) -> int:
    launcher = quoted_launcher()
    if action == "rollback":
        return core.emit(install.rollback())
    if action == "pin":
        if not value or not install.engine_installed(value):
            have = install.installed_versions()
            raise core.usage(f"engine {value or '(none given)'} is not installed. Installed: {', '.join(have) or 'none'}.")
        record = install.pins()
        record["engine"] = value
        core.write_json(core.home().state / "pins.json", record)
        return core.emit(Status("OK", f"engine pinned to {value}; updates are paused until `update --unpin`.",
                                f"run {launcher} guide pptx again so the instructions match the pinned engine.", f"Theodore is pinned to version {value}."))
    if action == "unpin":
        record = install.pins()
        record.pop("engine", None)
        core.write_json(core.home().state / "pins.json", record)
        return core.emit(Status("OK", "engine pin removed; updates resume at the next guide.", "nothing to do.", "Theodore will update itself again."))
    if action == "channel":
        _token, claims = account.stored_token()
        return core.emit(Status("OK", f"the update channel belongs to the account, not to this computer (it is {claims.get('chan', 'unknown')} now); "
                                      "nothing was changed here.",
                                f"if the user wants another channel, tell them it is changed in their Theodore account at {core.site_url('/app/plan')}; then continue.",
                                f"Theodore's update channel is set in your account at {core.site_name('/app/plan')}, not on this computer."))
    account.require("pptx")
    install.require_runtime()
    current = install.current_engine()
    if action == "reset":
        install.reset_update_state("update --reset-state")
        try:
            install.check_updates(force=True, wait=240.0, timeout=20.0)
        except install.StaleDocument as stale:
            return core.emit(_stale_status(stale))
        return core.emit(Status("OK", f"update state reset: the replay times and stored restrictions were cleared and a current signed "
                                      f"document was applied; engine {install.current_engine() or 'none'} stays the floor until a document "
                                      f"issued after this reset says otherwise.",
                                "run the command you were running before.", "Theodore's update state was reset."))
    if action == "check":
        try:
            manifest = install.fetch_channel_manifest()
        except install.StaleDocument as stale:
            return core.emit(_stale_status(stale))
        except api.NetworkError as error:
            raise api.offline(error, "update check") from None
        except api.ApiError as error:
            raise api.status_from(error, "update check") from None
        latest = manifest.get("engine", {}).get("version")
        same = latest == current
        return core.emit(Status("OK", f"installed engine {current or 'none'}; channel {manifest.get('channel')} offers {latest}."
                                + ("" if same else " An update is available."),
                                "nothing to do." if same else f"run {launcher} update --apply, or just continue: it applies at the next guide.",
                                "Theodore is up to date." if same else f"A Theodore update ({latest}) is available and will install itself."))
    install.save_update_state(held=None, failed=None)
    try:
        manifest = install.check_updates(force=True, wait=240.0, timeout=20.0)
    except install.StaleDocument as stale:
        return core.emit(_stale_status(stale))
    if manifest is None:
        raise Status("OFFLINE", "the update could not be checked: the Theodore service could not be reached.",
                     "check the internet connection, then run the same command again.", "Theodore could not reach its service to check for updates.")
    now_current = install.current_engine()
    target = manifest.get("engine", {}).get("version")
    if now_current != target:
        failed = install.update_state().get("failed") or {}
        if failed.get("version") == target:
            raise Status("RETRY", f"engine {target} failed its self-test ({failed.get('reason', 'unknown')}); engine {now_current} stays in use.",
                         "continue with the current engine; do not retry more than once a day.", f"A Theodore update did not pass its checks, so I am staying on version {now_current}.")
        raise Status("SETUP_IN_PROGRESS", f"engine {target} is still downloading.", "run the same command again in 30 seconds.",
                     "Theodore is downloading an update.")
    changed = now_current != current
    return core.emit(Status("OK", f"engine {now_current} is current" + (f" (was {current})." if changed else "; nothing to update."),
                            f"run {launcher} guide pptx again before continuing." if changed else "nothing to do.",
                            f"Theodore updated to version {now_current}." if changed else "Theodore is up to date."))


def _stale_status(stale) -> Status:
    """A signed document that was not applied. An older one (a replay) is ignored without error; one dated far ahead
    of this computer's clock is reported, since the clock (or the service's) needs looking at."""
    if stale.future:
        return Status("RETRY", f"{stale.message}. Nothing on this computer was changed.",
                      f"ask the user to check this computer's date and time, then run the same command again; if the clock is right, "
                      f"run it again later (or {quoted_launcher()} update --reset-state).",
                      "Theodore received an update notice dated in the future and set it aside. Please check this computer's date and time.")
    return Status("OK", f"{stale.message}; nothing changed.", "nothing to do.", "Theodore is up to date.")


def engine_update_job() -> int:
    job = Job("engine-update")
    job.begin("engine")
    entry = core.read_json(core.home().state / "engine-update.json")
    try:
        install.stage_engine(entry, job)
        job.finish()
        return 0
    except install.EngineError as exc:
        job.fail("SELF_TEST", str(exc))
        return 1
    except Status as status:
        job.fail(status.name, status.summary)
        return status.code
    except Exception as exc:
        job.fail("INTERNAL", f"{exc.__class__.__name__}: {exc}")
        return 78


def client_update_job(version_: str, sha256: str, runtime_id: str | None = None, issued: str | None = None) -> int:
    job = Job("client-update")
    job.begin("client")
    try:
        install.update_client(version_, sha256, runtime_id or None, float(issued) if issued else None)
        job.finish()
        return 0
    except Exception as exc:
        job.fail("INTERNAL", str(getattr(exc, "summary", exc)))
        return 1


# --- packs and tools -----------------------------------------------------------------------------------------------------------

def packs(action: str, name: str | None, everything: bool, background: bool, as_job: bool) -> int:
    none_published = Status("OK", f"{install.NO_PACKS}; nothing was installed.",
                            "continue; a build that needs an icon, flag, map or background library will say so.",
                            "Theodore's icon, flag, map and background libraries are not available for this account's channel yet.")
    if action == "status":
        state = install.packs_state()
        published = install.published_packs()
        for pack in PACK_ORDER:
            info = state.get(pack, {})
            print(f"{pack}\t{info.get('status', 'missing')}\t{info.get('version', '')}\t{info.get('percent', 0)}%")
        if published == []:
            return core.emit(Status("OK", f"{install.NO_PACKS}.", none_published.next_step, none_published.tell_user))
        wanted_packs = [p for p in PACK_ORDER if published is None or p in published]
        missing = [p for p in wanted_packs if state.get(p, {}).get("status") != "ready"]
        if missing and Job("packs").alive():
            return core.emit(Status("SETUP_IN_PROGRESS", f"asset packs still downloading: {', '.join(missing)}.",
                                    "continue; a build that needs one of them will wait for it.", "Theodore is still downloading some of its asset libraries in the background."))
        return core.emit(Status("OK", "all asset packs are installed." if not missing else f"asset packs not installed: {', '.join(missing)}.",
                                "nothing to do." if not missing else f"run {quoted_launcher()} packs install --all", "Theodore's asset libraries are installed."
                                if not missing else "Some Theodore asset libraries are missing; I am fetching them."))
    if not everything and not name:
        raise core.usage("packs install needs a pack name or --all.")
    if background:
        account.require("pptx", allow_refresh=False)
        if install.published_packs() == []:
            return core.emit(none_published)
        install.start_packs_job()
        return core.emit(Status("OK", "the asset packs are downloading in the background (about 95 MB).",
                                "continue; a build that needs a pack still arriving will wait for it.",
                                "Theodore is downloading its icon, flag, map and background libraries in the background (about 95 MB)."))
    job = Job("packs") if as_job else None
    if job:
        job.begin("packs")
    try:
        account.require("pptx", allow_refresh=False)
        entries = install.fetch_packs_manifest()
        wanted = [e for e in entries if everything or e.get("id") == name]
        if not entries:
            if job:
                job.finish()
            return core.emit(none_published)
        if not wanted:
            raise core.usage(f"unknown pack '{name}'. Packs: {', '.join(e.get('id', '?') for e in entries)}.")
        for index, entry in enumerate(wanted):
            if job:
                job.progress(f"pack {entry['id']}", index * 100.0 / len(wanted))
            install.install_pack(entry)
    except api.NetworkError as error:
        status_ = api.offline(error, "asset packs")
        if job:
            job.fail("NETWORK", status_.summary)
        raise status_ from None
    except api.ApiError as error:
        status_ = api.status_from(error, "asset packs")
        if job:
            job.fail(status_.name, status_.summary)
        raise status_ from None
    except Status as status_:
        if job:
            job.fail(status_.name, status_.summary)
        raise
    except install.StaleDocument as stale:          # nothing is installed from a document that was not applied
        if job:
            job.finish()
        return core.emit(_stale_status(stale))
    if job:
        job.finish()
    return core.emit(Status("OK", f"asset pack(s) installed: {', '.join(e['id'] for e in wanted)}.", "nothing to do.",
                            "Theodore's asset libraries are installed."))


def tools(name: str, background: bool, as_job: bool, if_needed: bool = False) -> int:
    if name == "git":
        return _tools_git(if_needed)
    if name != "libreoffice":
        raise core.usage(f"unknown tool '{name}'. The tools are: libreoffice, git.")
    if if_needed and not install.needs_libreoffice():      # the Windows installer's call: only when nothing can render
        return core.emit(Status("OK", "LibreOffice is not needed here: a converter is available or none is fetched on this system.",
                                "nothing to do.", "Nothing to install."))
    existing = install.system_libreoffice() or install.libreoffice_binary()
    if existing and not as_job:
        return core.emit(Status("OK", f"LibreOffice is already available at {existing}.", "nothing to do.", "LibreOffice is already installed."))
    pin = install.libreoffice_pin() or {}
    megabytes = int(pin.get("size", 0) / 1e6) or 300
    if background:
        if not Job("libreoffice").alive():
            Job("libreoffice").spawn(["tools", "install", "libreoffice", "--job"])
        return core.emit(Status("OK", f"LibreOffice {pin.get('version', '')} is downloading in the background (about {megabytes} MB, from The Document Foundation) into {core.home().tools}.",
                                "continue; slide previews work once it has arrived.",
                                f"Theodore is downloading LibreOffice (about {megabytes} MB) in the background so it can preview slides. Nothing is installed system-wide."))
    job = Job("libreoffice") if as_job else None
    if job:
        job.begin("libreoffice")
    try:
        binary = install.install_libreoffice(job)
    except Status as status_:
        if job:
            job.fail(status_.name, status_.summary)
        raise
    if job:
        job.finish()
    return core.emit(Status("OK", f"LibreOffice is installed at {binary}.", "nothing to do.", "LibreOffice is installed for Theodore's slide previews."))


def needs_git_bash() -> bool:
    """Claude Code on Windows with no Git Bash where it looks: it would run Theodore's skill commands in PowerShell."""
    return core.IS_WINDOWS and bool(shutil.which("claude")) and claude_git_bash() is None


def _tools_git(if_needed: bool) -> int:
    """Git Bash for Claude Code on Windows: PortableGit into Theodore's folder, and Claude Code pointed at it through
    the setting its documentation names (env.CLAUDE_CODE_GIT_BASH_PATH in its user settings). Recorded, so the
    uninstall takes it back; a value the user set is never replaced."""
    if if_needed and not needs_git_bash():
        return core.emit(Status("OK", "Git Bash is not needed here: Claude Code is not on this computer or already finds a Git Bash.",
                                "nothing to do.", "Nothing to install."))
    bash = install.install_git()
    line = _claude_git_bash_apply(bash)
    print(line)
    return core.emit(Status("OK", f"Git Bash (PortableGit {install.git_pin()['version']}) is in {bash.parent.parent}; {line}.",
                            "continue; a new Claude Code session uses it.",
                            "Theodore added Git Bash for Claude Code (PortableGit, in Theodore's folder) so its skills can load; "
                            "uninstalling Theodore takes it back."))


def _claude_git_bash_apply(bash: Path) -> str:
    path = _claude_settings_path()
    settings = _load_settings(path)
    if settings is None:
        return f"claude-code: settings not changed ({path} is not a valid JSON object)"
    env = settings.get("env", {})
    if not isinstance(env, dict):
        return f"claude-code: settings not changed ({path} has env in another shape than expected)"
    if env.get("CLAUDE_CODE_GIT_BASH_PATH") and env.get("CLAUDE_CODE_GIT_BASH_PATH") != str(bash):
        return f"claude-code: CLAUDE_CODE_GIT_BASH_PATH is already set in {path}; left as it is"
    record = _host_record()
    created = set(record.get("created_keys") or [])
    file_created = bool(record.get("file_created")) or not path.exists()
    if "env" not in settings:
        created.add("env")
    settings.setdefault("env", {})["CLAUDE_CODE_GIT_BASH_PATH"] = str(bash)
    path.parent.mkdir(parents=True, exist_ok=True)
    core.write_atomic(path, json.dumps(settings, indent=2) + "\n", 0o644)
    record.update({"git_bash": str(bash), "created_keys": sorted(created), "file_created": file_created, "path": str(path)})
    if file_created:
        record["file_identity"] = _identity(path)
    _save_host_record(record)
    return f"claude-code: Git Bash set in {path}"


# --- assistants on this machine ------------------------------------------------------------------------------------------------

def _run(command: list[str], timeout: float = 300.0) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return done.returncode, (done.stdout + done.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _claude_settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


def _permission_rules() -> list[str]:
    launcher = str(core.home().launcher())
    return [f"Bash({launcher} *)", f'Bash("{launcher}" *)']


# What Theodore added or changed in each assistant's settings is recorded in state/host-settings.json when it does it
# (registration and `hosts settings --apply`), so the uninstall takes back exactly that and nothing else. The record
# is flat JSON on purpose: the launchers' own uninstall reads two of its keys with grep and findstr.
def _host_record_path() -> Path:
    return core.home().state / "host-settings.json"


def _host_record() -> dict:
    return core.read_json(_host_record_path())


def _save_host_record(record: dict) -> None:
    core.home().ensure()
    core.write_json(_host_record_path(), record, 0o600)


def _load_settings(path: Path):
    """The settings object, {} for no file, or None when the file is not a JSON object (then nothing is touched)."""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def _listed(command: list[str], name: str = "theodore") -> bool:
    code, out = _run(command, timeout=60)
    return code == 0 and re.search(rf"(?<![A-Za-z0-9_-]){name}(?![A-Za-z0-9_-])", out) is not None


def hosts(action: str, apply: bool) -> int:
    cfg, home = core.config(), core.home()
    lines: list[str] = []
    claude, codex = shutil.which("claude"), shutil.which("codex")
    plugin_dir = home.plugin
    if action == "register":
        record = _host_record()
        if claude:
            code, out = _run([claude, "plugin", "list", "--json"])
            if code == 0 and '"theodore@theodore"' in out:
                lines.append("claude-code: already registered")
                record.setdefault("claude_registered", False)          # there before Theodore's own: not ours to remove
                record.setdefault("claude_added_marketplace", False)
            else:
                settings_path = _claude_settings_path()
                before_file, before = settings_path.exists(), _load_settings(settings_path) or {}
                market_before = _listed([claude, "plugin", "marketplace", "list"])
                url = str(cfg.get("marketplace_url") or "")
                code, out = _run([claude, "plugin", "install", "theodore", "--marketplace", url]) if url else (1, "")
                if code != 0 and url:
                    _run([claude, "plugin", "marketplace", "add", url])
                    code, out = _run([claude, "plugin", "install", "theodore@theodore"])
                if code != 0 and (plugin_dir / ".claude-plugin" / "marketplace.json").exists():
                    _run([claude, "plugin", "marketplace", "add", str(plugin_dir)])
                    code, out = _run([claude, "plugin", "install", "theodore@theodore"])
                lines.append("claude-code: registered" if code == 0 else f"claude-code: FAILED ({out.splitlines()[-1] if out else 'no output'})")
                if code == 0:           # what the registration itself added to the settings file
                    after = _load_settings(settings_path) or {}
                    record.setdefault("claude_registered", True)       # "not ours" (False) is never overwritten
                    record.setdefault("claude_added_marketplace", not market_before)
                    record["path"] = str(settings_path)
                    record["created_keys"] = sorted(set(record.get("created_keys") or []) | {k for k in after if k not in before})
                    record["file_created"] = bool(record.get("file_created")) or (not before_file and settings_path.exists())
                    if record["file_created"]:
                        record["file_identity"] = _identity(settings_path)
        else:
            lines.append("claude-code: not found on this computer")
        if codex:
            if not (plugin_dir / ".agents" / "plugins" / "marketplace.json").exists():
                lines.append(f"codex: FAILED (no plugin copy in {plugin_dir}; run the installer again)")
            else:
                config_before = _codex_config().exists()
                market_before = _listed([codex, "plugin", "marketplace", "list"])
                if _listed([codex, "plugin", "list"], "theodore@theodore"):
                    record.setdefault("codex_registered", False)       # there before Theodore's own: not ours to remove
                    record.setdefault("codex_added_marketplace", False)
                _run([codex, "plugin", "marketplace", "add", str(plugin_dir)])
                code, out = _run([codex, "plugin", "add", "theodore@theodore"])
                lines.append("codex: registered" if code == 0 else f"codex: FAILED ({out.splitlines()[-1] if out else 'no output'})")
                if code == 0:
                    record.setdefault("codex_registered", True)        # "not ours" (False) is never overwritten
                    record.setdefault("codex_added_marketplace", not market_before)
                    record["codex_config_created"] = bool(record.get("codex_config_created")) or (not config_before and _codex_config().exists())
        else:
            lines.append("codex: not found on this computer")
        if record:
            _save_host_record(record)
    elif action == "unregister":
        left: list[str] = []
        notes = _unregister_hosts(claude, codex, left)
        lines += [f"{name}: removed" for name, tool in (("claude-code", claude), ("codex", codex)) if tool]
        lines += [f"not removed: {item}" for item in left] + notes
    elif action == "settings":
        if not claude:
            lines.append("claude-code: not found; nothing to set")
        elif apply:
            lines.append(_claude_settings_apply())
        else:
            return core.emit(Status("OK", f"optional, not applied: Claude Code can update the Theodore plugin by itself and run {home.launcher()} "
                                          f"without a permission prompt (two entries in {_claude_settings_path()}).",
                                    f"ask the user whether they want that; only if they agree run: {quoted_launcher()} hosts settings --apply",
                                    "Would you like Theodore's plugin to update itself and to run without asking permission each time? "
                                    "That changes two entries in your Claude Code settings; uninstalling Theodore takes them back."))
    else:
        raise core.usage(f"unknown hosts action '{action}'.")
    for line in lines:
        print(line)
    failed = [l for l in lines if "FAILED" in l]
    if failed:
        return core.emit(Status("RETRY", f"an assistant could not be set up: {'; '.join(failed)}",
                                f"register by hand with the commands under 'Other ways' at {core.site_url('/install')}, then continue.",
                                "Theodore is installed, but I could not add it to one of your assistants automatically. I will add it by hand."))
    return core.emit(Status("OK", "; ".join(lines) or "nothing to do.", "continue.",
                            "Theodore is registered in the assistants found on this computer." if action == "register" else "Done."))


def _claude_settings_apply() -> str:
    """Add exactly two things in Claude Code's user settings (marketplace auto-update and the launcher rule) and
    record what was added or changed: the rules, the keys and the file created, the marketplace entry created or the
    autoUpdate value it had before."""
    path = _claude_settings_path()
    settings = _load_settings(path)
    if settings is None:
        return f"claude-code: settings not changed ({path} is not a valid JSON object)"
    permissions, markets = settings.get("permissions", {}), settings.get("extraKnownMarketplaces", {})
    if not isinstance(permissions, dict) or not isinstance(permissions.get("allow", []), list) or not isinstance(markets, dict) \
            or not isinstance(markets.get("theodore", {}), dict):
        return f"claude-code: settings not changed ({path} has these keys in another shape than expected)"
    record = _host_record()
    if record.get("file_created") and path.exists() and record.get("file_identity") != _identity(path):
        record["file_created"] = False      # replaced since Theodore made it: the user's file now, never Theodore's to delete
        record.pop("file_identity", None)
    created = set(record.get("created_keys") or [])
    file_created = bool(record.get("file_created")) or not path.exists()
    if "permissions" not in settings:
        created.add("permissions")
    permissions = settings.setdefault("permissions", {})
    if "allow" not in permissions:
        created.add("permissions.allow")
    allow = permissions.setdefault("allow", [])
    added = [r for r in _permission_rules() if r not in allow]
    allow.extend(added)
    if "extraKnownMarketplaces" not in settings:
        created.add("extraKnownMarketplaces")
    markets = settings.setdefault("extraKnownMarketplaces", {})
    market = record.get("marketplace")
    if "theodore" not in markets:
        markets["theodore"] = {"source": {"source": "url", "url": str(core.config().get("marketplace_url") or "")}, "autoUpdate": True}
        market = {"created": True, "entry": json.loads(json.dumps(markets["theodore"]))}
    else:
        entry = markets["theodore"]
        if market is None:              # the first change Theodore makes to an entry it did not create: remember what was there
            market = {"created": False, "set_auto_update": entry.get("autoUpdate") is not True,
                      "auto_update_prior": {"present": "autoUpdate" in entry, **({"value": entry["autoUpdate"]} if "autoUpdate" in entry else {})}}
        entry["autoUpdate"] = True
    core.write_atomic(path, json.dumps(settings, indent=2) + "\n", 0o644)
    record.update({"rules": sorted(set(record.get("rules") or []) | set(added)), "created_keys": sorted(created), "marketplace": market,
                   "file_created": file_created, "path": str(path)})
    if file_created:
        record["file_identity"] = _identity(path)
    _save_host_record(record)
    return f"claude-code: automatic updates on and launcher allowed in {path}"


def _legacy_marketplace(record: dict) -> dict | None:
    """A record written by an earlier client (created_marketplace / set_auto_update, no prior value)."""
    if record.get("created_marketplace"):
        return {"created": True, "entry": None}
    if record.get("set_auto_update"):
        return {"created": False, "set_auto_update": True, "auto_update_prior": {"present": False}}
    return None


def _identity(path: Path) -> list | None:
    """Which file this is (inode, modification time, size): a settings file Theodore created is deleted only while it is
    still the file Theodore (or its registration) left, never one the user made or rewrote since."""
    try:
        info = path.stat()
    except OSError:
        return None
    return [info.st_ino, info.st_mtime_ns, info.st_size]


def _claude_settings_restore(file_ours: bool = False) -> tuple[str | None, list[str]]:
    """Take back exactly what the record says Theodore added or changed: its rules, the marketplace entry it created
    (only if nobody changed it since) or the autoUpdate value it changed (restored, unless changed since), and the
    keys and the file it created, only when they hold nothing. Anything else, empty or not, stays. Returns (a problem
    when the file could not be read or written, notes for what was left on purpose)."""
    record = _host_record()
    path = Path(record.get("path") or _claude_settings_path())
    notes: list[str] = []
    if not record or not path.exists():
        return None, notes
    settings = _load_settings(path)
    if settings is None:
        return f"settings not changed ({path} is not a valid JSON object)", notes
    before = json.dumps(settings, sort_keys=True)
    permissions = settings.get("permissions")
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    if isinstance(allow, list):
        permissions["allow"] = [r for r in allow if r not in (record.get("rules") or [])]
    env = settings.get("env")
    if record.get("git_bash") and isinstance(env, dict):
        if env.get("CLAUDE_CODE_GIT_BASH_PATH") == record["git_bash"]:
            env.pop("CLAUDE_CODE_GIT_BASH_PATH")
        elif "CLAUDE_CODE_GIT_BASH_PATH" in env:
            notes.append(f"env.CLAUDE_CODE_GIT_BASH_PATH in {path} was changed after Theodore set it, so it was left as it is")
    markets = settings.get("extraKnownMarketplaces")
    entry = markets.get("theodore") if isinstance(markets, dict) else None
    market = record.get("marketplace") if "marketplace" in record else _legacy_marketplace(record)
    if isinstance(entry, dict) and market:
        if market.get("created"):
            ours = market.get("entry") or {"source": {"source": "url", "url": str(core.config().get("marketplace_url") or "")}, "autoUpdate": True}
            if entry == ours:
                markets.pop("theodore", None)
            else:
                notes.append(f"the extraKnownMarketplaces.theodore entry in {path} was changed after Theodore created it, so it was left as it is")
        elif market.get("set_auto_update"):
            if entry.get("autoUpdate") is True:
                prior = market.get("auto_update_prior") or {}
                if prior.get("present"):
                    entry["autoUpdate"] = prior.get("value")
                else:
                    entry.pop("autoUpdate", None)
            else:
                notes.append(f"extraKnownMarketplaces.theodore.autoUpdate in {path} was changed after Theodore set it, so it was left as it is")
    for key in sorted(record.get("created_keys") or [], key=lambda k: -k.count(".")):        # the deepest first
        parent, _, leaf = key.rpartition(".")
        holder = settings.get(parent) if parent else settings
        if isinstance(holder, dict) and holder.get(leaf, None) in ([], {}):
            del holder[leaf]
    try:
        if record.get("file_created") and file_ours and settings == {}:
            path.unlink()
        elif json.dumps(settings, sort_keys=True) != before:
            core.write_atomic(path, json.dumps(settings, indent=2) + "\n", 0o644)
    except OSError as exc:
        return f"settings not changed ({path}: {exc.__class__.__name__})", notes
    return None, notes


def _unregister_hosts(claude: str | None, codex: str | None, left: list[str]) -> list[str]:
    """Remove the plugin from each assistant; its marketplace only when Theodore added it (or when no record says);
    then the settings Theodore recorded. Problems go to `left`; returns notes for what stays on purpose."""
    record = _host_record()
    notes: list[str] = []
    problems_before = len(left)
    settings_path = Path(record.get("path") or _claude_settings_path())
    # Still the settings file Theodore created and last wrote? Decided before the assistants' own unregistration,
    # which rewrites the file.
    file_ours = bool(record.get("file_created")) and record.get("file_identity") == _identity(settings_path)
    for name, tool, key, plugin_args, market_args in (
            ("claude-code", claude, "claude", ["plugin", "uninstall", "theodore@theodore"], ["plugin", "marketplace", "remove", "theodore"]),
            ("codex", codex, "codex", ["plugin", "remove", "theodore@theodore"], ["plugin", "marketplace", "remove", "theodore"])):
        if not tool:
            continue
        steps = []
        if record.get(f"{key}_registered") is False:
            notes.append(f"{name}: its Theodore plugin registration was there before Theodore's installer and was kept")
        else:
            steps.append(plugin_args)
        if record.get(f"{key}_added_marketplace") is False:
            notes.append(f"{name}: its theodore marketplace was there before Theodore and was kept")
        else:
            steps.append(market_args)
        for args in steps:
            problem = _unregistered([tool, *args])
            if problem:
                left.append(f"the {name} registration ({' '.join(args)}: {problem})")
    problem, settings_notes = _claude_settings_restore(file_ours)
    if problem:
        left.append(f"the Theodore entries in {_claude_settings_path()} ({problem})")
    notes += settings_notes
    _codex_leftovers()                      # reads the record: before it goes
    if len(left) == problems_before:
        # Everything taken back: the record has done its job (a stale one could later claim a settings file the user
        # made). What it says is not Theodore's stays recorded until the uninstall has finished, so a later
        # register, unregister or uninstall never removes a registration that was there before.
        not_ours = {k: v for k, v in _host_record().items() if k.endswith(("_registered", "_added_marketplace")) and v is False}
        if not_ours:
            _save_host_record(not_ours)
        else:
            core.remove_tree(_host_record_path())
    return notes


# --- the installer's plugin step ----------------------------------------------------------------------------------------

def install_plugin(dl_base: str, keys_text: str) -> int:
    """Fetch <dl>/plugin/latest.json, verify it with the keys the installer carries (they are pinned in the
    installer, whose own sha256 the site shows), then fetch the archive it names, check its sha256 and size, and
    unpack it into <home>/plugin. The installer runs this with the Python and client it fetched against pinned
    sha256 values, so nothing unauthenticated runs before or after it."""
    keys = {}
    for item in keys_text.split():
        kid, _, x = item.partition("=")
        if kid and x:
            keys[kid] = x
    home = core.home()
    home.ensure()
    base = dl_base.rstrip("/")
    what = "the plugin release document"
    try:
        _status, _headers, raw = api.request("GET", f"{base}/plugin/latest.json", timeout=60.0, redirects=False)
        envelope = json.loads(raw.decode("utf-8"))
    except api.NetworkError as error:
        raise api.setup_failed(error.cause, f"{what} could not be downloaded from {base} ({error.detail})") from None
    except api.ApiError as error:
        raise api.setup_failed("NETWORK", f"{what} could not be downloaded from {base} (HTTP {error.http_status})") from None
    except ValueError:
        raise api.setup_failed("HASH_MISMATCH", f"{what} is not a signed document") from None
    if not isinstance(envelope, dict):
        raise api.setup_failed("HASH_MISMATCH", f"{what} is not a signed document")
    doc = account.verify_signed(envelope, what, PLUGIN_LATEST_SCHEMA, keys=keys, platform=False)
    version, archive_name = str(doc.get("version") or ""), str(doc.get("archive") or "")
    sha, size = str(doc.get("sha256") or "").lower(), doc.get("size")
    if not core._SEMVER.match(version) or archive_name != f"theodore-plugin-{version}.tar.gz" or not re.match(r"^[0-9a-f]{64}$", sha) \
            or not isinstance(size, int) or not 0 < size < 64 * 1024 * 1024:
        raise api.setup_failed("HASH_MISMATCH", f"{what} is signed but does not describe a plugin archive")
    installed = core.read_json(home.plugin / "plugins" / "theodore" / ".claude-plugin" / "plugin.json").get("version")
    if installed and core._SEMVER.match(str(installed)) and core.version_key(version) < core.version_key(str(installed)):
        # A signed document cannot be told from a replayed old one; an older plugin than the one installed is refused.
        raise api.setup_failed("HASH_MISMATCH", f"the plugin release document offers {version}, older than the installed plugin {installed}; "
                                                f"refused (a replayed document, or a rollback that only an uninstall can apply)")
    archive = home.staging / archive_name
    try:
        api.download(f"{base}/plugin/{archive_name}", archive, sha256=sha, size=size, timeout=60.0, redirects=False)
    except api.NetworkError as error:
        raise api.setup_failed(error.cause, f"the plugin could not be downloaded from {base} ({error.detail})") from None
    except api.ApiError as error:
        raise api.setup_failed("NETWORK", f"the plugin could not be downloaded from {base} (HTTP {error.http_status})") from None
    if archive.stat().st_size != size:
        core.remove_tree(archive)
        raise api.setup_failed("HASH_MISMATCH", "the plugin archive is not the size its signed document gives")
    partial = home.root / "plugin.partial"
    core.remove_tree(partial)
    try:
        install.extract_tar(archive, partial)
    finally:
        core.remove_tree(archive)
    if not (partial / "plugins" / "theodore" / "skills" / "pptx" / "scripts" / "launcher.json").is_file():
        core.remove_tree(partial)
        raise core.internal("the plugin archive has no launcher.")
    core.remove_tree(home.plugin)
    os.rename(partial, home.plugin)
    if not core.IS_WINDOWS:
        for name in ("pptx", "deck-design"):
            launcher = home.plugin / "plugins" / "theodore" / "skills" / name / "scripts" / "theodore"
            if launcher.exists():
                launcher.chmod(0o755)
    print(f"theodore-install: plugin {version} is in {home.plugin} (signed by {envelope.get('kid')})", flush=True)
    return 0


# --- uninstall -------------------------------------------------------------------------------------------------------------------

def _render_cache_dirs() -> list[Path]:
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Caches" / "pptx-runtime"]
    if core.IS_WINDOWS:
        return [Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "pptx-runtime"]
    return [Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pptx-runtime"]


# An assistant that is on the computer but never had Theodore registered: nothing to remove, not a failure.
_NOT_THERE = ("not found", "not installed", "not configured", "no such", "does not exist", "unknown plugin", "unknown marketplace",
              "not registered", "is not a known", "no plugin named", "no marketplace named")


def _unregistered(command: list[str]) -> str | None:
    """Run one unregistration; None when it worked or there was nothing to remove, else what went wrong."""
    code, out = _run(command)
    if code == 0 or any(text in out.lower() for text in _NOT_THERE):
        return None
    return (out.splitlines()[-1] if out else f"exit {code}")[:160]


def _codex_leftovers() -> None:
    """What Codex keeps after Theodore is unregistered and that holds nothing: its empty plugin cache folder for
    Theodore, and, when Theodore's registration created it, a configuration file left with zero bytes. Anything with
    content, or that was there before, stays. Runs before the record is deleted with state/."""
    base = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    with contextlib.suppress(OSError):
        (base / "plugins" / "cache" / "theodore").rmdir()          # only when empty
    if _host_record().get("codex_config_created"):
        with contextlib.suppress(OSError):
            config = _codex_config()
            if config.is_file() and not config.is_symlink() and config.stat().st_size == 0:
                config.unlink()


def uninstall(keep_hosts: bool) -> int:
    home = core.home()
    left: list[str] = []
    notes: list[str] = []
    if not keep_hosts:
        notes = _unregister_hosts(shutil.which("claude"), shutil.which("codex"), left)
    for name in ("setup", "packs", "engine-update", "client-update", "libreoffice"):
        pid = int(Job(name).read().get("pid") or 0)
        if pid and pid != os.getpid() and Job(name).alive():
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    if not credentials.delete():
        left.append(f"the sign-in item in the login keychain (service {credentials.SERVICE})")
    for cache in _render_cache_dirs():
        core.remove_tree(cache)
        if cache.exists():
            left.append(f"the render cache {cache}")
    specimens_left = specimens.sweep(everything=True)
    for folder in specimens_left:
        left.append(f"the specimen folder {folder} (its record is kept in {specimens.registry()}; running uninstall again retries it)")
    root = home.root
    # Only Theodore's own subfolders go. Anything else in the folder was not put there by Theodore and stays.
    if core.IS_WINDOWS:  # the running interpreter lives inside the folder: delete it after this process ends
        # One string, not a list: Python would escape the inner quotes as \" and cmd does not read that escape (a
        # path with a space was never removed). /s makes cmd strip exactly the outer pair of quotes.
        def removal(name: str) -> str:
            if name == "state" and specimens_left:          # keep only the record of folders still to remove
                state = root / name
                return (f'(for /d %D in ("{state}\\*") do rmdir /s /q "%D") & '
                        f'(for %F in ("{state}\\*") do if /i not "%~nxF"=="{specimens.registry().name}" del /f /q "%F")')
            return f'rmdir /s /q "{root / name}"'
        removals = " & ".join(removal(name) for name in core.OWNED)
        line = f'ping -n 6 127.0.0.1 >nul & {removals} & rmdir "{root}"'
        subprocess.Popen(f'cmd /d /s /c "{line}"', creationflags=0x00000008 | 0x08000000,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        kept = [p.name for p in root.iterdir() if p.name not in core.OWNED] if root.exists() else []
        # The deletion runs after this process has ended, so it cannot be confirmed here: say so, and let a second
        # run (by then without Theodore's Python: the launcher's or the installer's own uninstall) confirm it.
        print(f"THEODORE RETRY: Theodore's registrations and sign-in were removed; its folders in {root} are deleted a few "
              f"seconds after this command ends, which is scheduled but not confirmed"
              + (f"; also not removed: {'; '.join(left)}" if left else "") + (f"; left as they are: {'; '.join(notes)}" if notes else "") + ".\n"
              "NEXT: run the same command again in 15 seconds; it confirms the deletion (or removes what is left) and says so.\n"
              "TELL THE USER: Theodore is being removed from this computer; I am checking that its files are gone. Your decks were not touched.",
              flush=True)
        return 75
    else:
        for name in core.OWNED:
            if name == "state" and specimens_left:          # the record of folders still to remove survives (it is read again)
                for child in (root / name).iterdir() if (root / name).is_dir() else []:
                    if child.name != specimens.registry().name:
                        core.remove_tree(child)
                continue
            core.remove_tree(root / name)
            if (root / name).exists():
                left.append(f"{root / name}")
        kept = sorted(p.name for p in root.iterdir() if p.name not in core.OWNED) if root.exists() else []
        if root.exists() and not any(root.iterdir()):
            try:
                root.rmdir()
            except OSError:
                pass
    what = f"its folders in {root} were deleted; {len(kept)} other item(s) there were not Theodore's and were left alone" if kept else f"{root} was deleted"
    if notes:           # what was left on purpose because it was not Theodore's to take back
        what += "; left as they are: " + "; ".join(notes)
    if left:          # say exactly what is still there, never "removed"
        print(f"THEODORE RETRY: Theodore's files were removed ({what}), but these could not be: {'; '.join(left)}.\n"
              "NEXT: tell the user what is left (each item above names the folder, the keychain item or the command); it can be removed by hand, "
              "or run the same command again once the assistant is closed.\n"
              "TELL THE USER: Theodore's files are removed, but a few pieces could not be: " + "; ".join(left) + ". Your decks were not touched.", flush=True)
        return 75
    print(f"THEODORE OK: Theodore was removed from this computer ({what}; sign-in removed).\n"
          "NEXT: nothing to do. Deck workspaces keep a small .pptx-state/theodore-engine.json file; it is harmless.\n"
          "TELL THE USER: Theodore is uninstalled. Your decks and workspaces were not touched.", flush=True)
    return 0
