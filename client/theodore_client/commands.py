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

from . import __version__, account, api, core, credentials, install, routing, specimens, usage
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
    if part == 1:
        out += [line for line in (install.take_announcement(), install.plugin_notice(daily=True)) if line]
    out += header_lines
    out.append(f"--- {name} ---")
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.write(body if body.endswith("\n") else body + "\n")
    sys.stdout.write(_end_line(skill, name, body, part, total, next_command) + "\n")
    sys.stdout.flush()


def guide(skill: str, section: str | None, part: int, license_text: bool) -> int:
    ensure_hosts()
    ensure_instructions()
    resume_background()
    if skill not in SKILLS:
        raise core.usage(f"unknown skill '{skill}'. The skills are: {', '.join(SKILLS)}.")
    if part < 1:
        raise core.usage("--part starts at 1.")
    if skill == "pptx":
        if license_text:
            raise core.usage("guide pptx has no --license: the pptx manual and its notices are in the engine folder named in its header.")
        if not section:
            return _guide_pptx(part)
        # A section of pptx is one the service serves to this account (lane STUDIO); the rest of the manual is on disk.
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
        raise _engine_downloading("run the same command again in 20 seconds. Do not use another slide tool meanwhile.")
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
        if directions.startswith("THEODORE OFFLINE: "):
            header.insert(0, directions)         # first thing read: the agent fetches it again before building
        elif directions:
            header += directions.rstrip("\n").split("\n")
    _print_document("pptx", version, str(claims.get("plan", "unknown")), "SKILL.md", parts[part - 1], part, len(parts), header,
                    f"{launcher} guide pptx --part {part + 1}")
    return 0


def _engine_downloading(next_step: str) -> Status:
    """No engine yet: SETUP_IN_PROGRESS with the download that is getting it (the setup job's first engine, or an
    update job). An update job that gave up because another Theodore process held the engine lock (the setup job,
    downloading the same engine on a slow link) is not an error: it is cleared, and the next call starts it again if
    the engine is still missing. Any other failure of that job is reported as before."""
    update, setup = Job("engine-update"), Job("setup")
    state = update.read()
    failed = state.get("failed") or {}
    if failed and not update.alive():
        update.clear()
        if not (failed.get("cause") in ("RETRY", "SETUP_IN_PROGRESS") or core.lock_busy(failed.get("detail", ""))):
            raise core.internal(f"the engine could not be installed: {failed.get('detail', 'unknown error')}")
        state = {}
    if setup.alive():
        state = setup.read()
    return Status("SETUP_IN_PROGRESS", f"Theodore's engine is still downloading ({int(state.get('percent') or 0)}%).", next_step,
                  "Theodore is downloading its PowerPoint engine. I will continue as soon as it is ready.")


# The served entry of a skill (directions, saved preferences, and for an account with Studio the first instruction of
# the method) is fetched with a few attempts, never dropped silently: a slow corporate proxy needs 7 to 10 seconds per
# connection (P33), so the first attempt gets 10 seconds and two more share the rest of a 20-second budget.
DIRECTIONS_TIMEOUTS = (10.0, 5.0, 5.0)
DIRECTIONS_PAUSE = 0.5


def _directions(skill: str) -> str:
    """Server-composed entry for this account. Empty when the service has none for it; when the service cannot be
    reached after every attempt, one status line that says so and how to get it, so the agent fetches it again
    before building instead of going on without it."""
    cred = credentials.load()
    if not cred:
        return ""
    failure = ""
    timeouts = DIRECTIONS_TIMEOUTS
    raw_test = os.environ.get("THEODORE_TEST_DIRECTIONS_TIMEOUTS")   # test seam, honoured only against a loopback API
    if raw_test and core.is_loopback_api():
        timeouts = tuple(float(value) for value in raw_test.split(","))
    for attempt, timeout in enumerate(timeouts):
        if attempt:
            time.sleep(DIRECTIONS_PAUSE)
        try:
            _status, _headers, raw = api.call("guide", bearer=cred["installation_secret"], skill=skill, timeout=timeout)
            return raw.decode("utf-8", errors="replace")
        except api.ApiError as error:
            if error.code in DELETE_CREDENTIAL_ON:      # the device was revoked: no instructions, and the sign-in is already cleared
                raise account.login_start("this device was signed out; it needs to sign in again.") from None
            if error.code in DELETE_TOKEN_ON:           # the account no longer has the skill
                raise api.status_from(error, f"guide {skill}") from None
            if error.http_status < 500 and error.http_status != 429:
                return ""                               # the service answered: nothing for this account
            failure = f"HTTP {error.http_status}"
        except api.NetworkError as error:
            failure = error.detail
    return (f"THEODORE OFFLINE: Theodore's online part for {skill} could not be reached ({failure}); "
            f"run {quoted_launcher()} guide {skill} again before building.")


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
    """Which sections there are: the ones the skill's main text links, fetched now (the text is never stored). For pptx
    only the answer: its manual is on disk, and a served section is named by the text that points to it."""
    launcher = quoted_launcher()
    shown = "".join(ch for ch in given if ch.isprintable())[:80]
    if skill == "pptx":
        return core.usage(f"guide pptx has no section '{shown}'.", f"run {launcher} guide pptx for the manual.")
    names: list[str] = []
    try:
        _status, _headers, raw = _api("guide", f"guide {skill}", skill=skill)
        for name in REFERENCE_LINK.findall(raw.decode("utf-8", errors="replace")):
            if name not in names:
                names.append(name)
    except Status as status:
        if status.name != "USAGE":
            raise
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
    if section != "license" and skill != "pptx":   # a reference is a section of this guide, not a file: its link becomes the command that prints it (pptx references are files on disk)
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
    body = _without_frontmatter(b"".join(chunks).decode("utf-8", errors="replace"))
    # The host ran this command, not the agent: "the launcher you used" (or the service's "where it is" wording for
    # tool delivery) becomes this launcher's own path, which is true however the text arrived.
    for line in _LAUNCHER_LINES:
        body = body.replace(line, f"Run {quoted_launcher()} with:")
    return _bounded(body, rest)


_LAUNCHER_LINES = ("Run the Theodore launcher you used to get these instructions with:",
                   "Run the Theodore launcher (scripts/theodore in the folder of either Theodore skill; scripts\\theodore.cmd on Windows) with:")


def _pptx_body() -> str:
    claims = account.require("pptx")
    del claims
    install.require_runtime()
    first = install.current_engine() is None
    install.check_updates(force=first, wait=15.0, timeout=20.0 if first else 3.0)
    version = install.selected_engine()
    if not version:
        raise _engine_downloading(f"run {quoted_launcher()} guide pptx in 20 seconds and follow it. Do not use another slide tool meanwhile.")
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
    if directions.startswith("THEODORE OFFLINE: "):
        header.insert(0, directions)             # first thing read: the agent fetches it again before building
    elif directions:
        header += directions.rstrip("\n").split("\n")
    body = "\n".join(header) + "\n\n" + _without_frontmatter(text)
    return _bounded(body, f"run {launcher} guide pptx")


def skill(name: str) -> int:
    """`theodore skill <name>`: the skill's instructions as a clean document (no framing), or a three-line status.
    Always exit 0: a host that injects this output aborts the whole skill on any other code."""
    ensure_hosts()
    ensure_instructions()
    resume_background()
    try:
        if name not in SKILLS:
            raise core.usage(f"unknown skill '{name}'. The skills are: {', '.join(SKILLS)}.")
        body = _deck_design_body() if name == "deck-design" else _pptx_body()
    except Status as status:
        core.emit(status)               # a revoked or suspended device gets that refusal instead, once
        return 0
    notices = [line for line in (install.take_announcement(), install.plugin_notice(daily=True)) if line]
    if notices:
        body = "\n".join(notices) + "\n" + body
    sys.stdout.write(body if body.endswith("\n") else body + "\n")
    sys.stdout.flush()
    return 0


def specimen(files: list[str]) -> int:
    """Fetch deck-design specimen sheets into a private temporary folder of this run and print one path per line.
    A sheet this computer already holds in a recent specimen folder is checked with the service (If-None-Match) and,
    when unchanged, its path is printed instead. All or nothing: a sheet that is refused leaves no folder and no path.
    A refusal that signs the device out or refuses the account removes every specimen folder."""
    if not 1 <= len(files) <= specimens.MAX_FILES:
        raise core.usage(f"specimen takes 1 to {specimens.MAX_FILES} file names, as the deck-design text gives them.")
    if not all(specimens.valid_name(name) for name in files):
        raise core.usage("a specimen is named as the deck-design text names it: letters, digits, dot, dash and underscore, ending in .jpg.")
    try:
        paths = _specimen_paths(files)
    except Status as status:
        if core.refused() or status.name in ("SIGN_IN_REQUIRED", "SIGN_IN_PENDING", "NO_ENTITLEMENT"):
            specimens.sweep(everything=True)
        raise
    for name in files:
        print(paths[name].resolve().as_posix(), flush=True)
    return 0


def _specimen_paths(files: list[str]) -> dict[str, Path]:
    account.require("deck-design")
    copies = {name: specimens.held(name) for name in dict.fromkeys(files)}
    # A sheet not held needs a folder: made before anything is fetched, so a working folder that leaves no private
    # place for sheets stops the call before any sheet is downloaded (as before 0.3.4).
    folder = specimens.make_folder() if any(copy is None for copy in copies.values()) else None
    paths: dict[str, Path] = {}
    try:
        for name, copy in copies.items():
            what = f"specimen {name}"
            asked = {"Accept": "image/jpeg, application/json;q=0.9"}
            if copy is not None:
                try:
                    asked["If-None-Match"] = '"' + hashlib.sha256(copy.read_bytes()).hexdigest() + '"'
                except OSError:
                    copy = None
            try:
                status, headers, raw = _api("guide_specimen", what, file=name, query={"skill": "deck-design"},
                                            headers=asked, redirects=False, limit=specimens.MAX_BYTES)
            except api.TooLarge:
                raise core.internal(f"{what}: the answer is larger than 1 MiB; it was refused and nothing was kept.") from None
            if core.refused():
                raise api.sign_in_required()
            if status == 304 and copy is not None:
                paths[name] = copy
                continue
            if not str(headers.get("Content-Type") or "").lower().startswith("image/jpeg"):
                raise core.internal(f"{what}: the answer is not a JPEG image; it was refused and nothing was kept.")
            if str(headers.get("X-Theodore-Sha256") or "").strip().lower() != hashlib.sha256(raw).hexdigest():
                raise Status("RETRY", f"{what}: the sheet did not arrive as the service sent it (sha256 differs or is missing); nothing was kept.",
                             "run the same command again once. If it fails again, continue without the sheet.",
                             "A design specimen arrived damaged. I am trying once more.")
            if folder is None:          # a held copy went missing or was answered 200: this sheet needs a folder after all
                folder = specimens.make_folder()
            paths[name] = specimens.write(folder, name, raw)
    except BaseException:
        if folder is not None:
            core.remove_tree(folder)
        raise
    if folder is not None and not any(path.parent == folder for path in paths.values()):
        core.remove_tree(folder)        # every sheet was held after all: the empty folder goes
    return paths


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
    code = install.run_engine(args)
    if args[:1] == ["finish"]:
        usage.flush(force=True)         # the usage counts go at the end of a deck (never raises, never prints)
    return code


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
    if not snap["engine"] and Job("setup").alive():          # the setup job is fetching it in the background
        state = Job("setup").read()
        return Status("SETUP_IN_PROGRESS", f"the engine is downloading in the background ({state.get('phase', 'starting')}, "
                                           f"{int(state.get('percent') or 0)}%).",
                      "run the same command again in 20 seconds. Do not use another slide tool meanwhile.",
                      "Theodore is downloading its PowerPoint engine. This takes a few minutes; I will continue when it is ready.")
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
    ensure_hosts()
    ensure_instructions()
    resumed = resume_background()
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
        f"no LibreOffice found: slide previews are unavailable until it is installed; {install.libreoffice_install_action()}")
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
        found = core.host_executable(name)
        if not found:
            add("ok", f"assistant {name}", "not found on PATH or where its installers put it")
            continue
        where = found + ("" if shutil.which(name) else " (not on PATH; found where its installer puts it)")
        # A cloud session's plugins come from the account, not from this command: nothing to check there.
        registered = None if core.cloud_sandbox() else _host_registration(name, found)
        if registered is False:
            add("warn", f"assistant {name}", f"{where}; Theodore's plugin is not registered in it: run {quoted_launcher()} hosts register, "
                "then start a new session")
        else:
            add("ok", f"assistant {name}", where + ("; Theodore's plugin is registered" if registered else
                                                    "" if core.cloud_sandbox() else
                                                    "; whether Theodore's plugin is registered could not be checked (its command did not answer)"))
    written = _host_record().get(routing.RECORD_KEY) or {}
    add("ok", "instructions", "; ".join(f"{n}: {e.get('path')}" for n, e in sorted(written.items())) or "none written")
    if core.IS_WINDOWS and core.host_executable("claude") and not claude_git_bash():
        add("warn", "claude git bash", "not found: Claude Code then runs commands in PowerShell, where Theodore's skills cannot "
            "load their instructions; Git for Windows (https://git-scm.com/downloads/win) provides Git Bash")
    notice = install.plugin_notice(daily=False)
    update = install.update_state().get("plugin_update") or {}
    if Job("plugin-update").alive():
        add("ok", "plugin files", f"updating to {update.get('version')} in the background; used from the next session")
    elif notice:
        add("warn", "plugin files", notice.removeprefix("THEODORE NOTICE: ").removesuffix(" Tell the user once; it does not stop this task."))
    elif update.get("hosts"):
        add("ok", "plugin files", f"last update to {update.get('version')}: " + "; ".join(f"{h} {r}" for h, r in sorted(update["hosts"].items())))
    engine_failed = install.update_state().get("failed") or {}
    if engine_failed.get("version") and engine_failed.get("version") != snap.get("engine"):
        add("warn", "engine update", f"engine {engine_failed['version']} is pending: it failed its check ({engine_failed.get('reason', 'unknown')}); "
            f"engine {snap.get('engine')} keeps working and the update is tried again after six hours")
    missing = _still_missing(snap)
    add("ok" if not missing else "warn", "still missing", "; ".join(missing) if missing else "nothing: every part Theodore needs here is installed")
    if resumed:
        add("ok", "restarted", f"downloads that had stopped before they finished were started again: {', '.join(resumed)}")
    final = _doctor_verdict(checks, snap, home)
    if as_json:         # the same verdict, as the exit code and in the document
        print(json.dumps({"checks": [{"level": l, "name": n, "detail": d} for l, n, d in checks], "snapshot": snap,
                          "status": final.name}, indent=2, sort_keys=True))
        return final.code
    for level, name, detail in checks:
        print(f"{level.upper():4}  {name}: {detail}")
    return core.emit(final)


def _still_missing(snap: dict) -> list[str]:
    """Each part Theodore still lacks on this computer, in plain words, with what will fetch it."""
    launcher = quoted_launcher()
    missing: list[str] = []
    setup = Job("setup")
    if not snap["runtime_ready"]:
        missing.append("the runtime (Python and Node, about 100 MB): " + (
            "it is downloading in the background now" if setup.alive() else
            "the next Theodore command starts its download again (it had stopped before it finished)" if setup.read() else
            "the next Theodore command starts its download"))
    if not snap["engine"]:
        missing.append(f"the engine: it comes with the runtime's setup once this computer is signed in, or `{launcher} guide pptx` installs it")
    published = install.published_packs()
    waiting = [p for p, info in snap["packs"].items() if info["status"] != "ready" and (published is None or p in published)]
    if waiting and snap["runtime_ready"]:
        missing.append(f"the asset packs {', '.join(waiting)}: " + ("downloading in the background now" if Job("packs").alive()
                       else "the next guide or catalogue call starts their download again"))
    lo = Job("libreoffice")
    lo_state = lo.read()
    if install.needs_libreoffice() or (lo_state and not lo_state.get("done") and not (install.system_libreoffice() or install.libreoffice_binary())):
        if lo.alive():
            missing.append("LibreOffice (slide previews): downloading in the background now")
        elif lo_state.get("failed") and not install.resumable_failure(lo_state):
            missing.append(f"LibreOffice (slide previews): its download failed ({lo_state['failed'].get('detail') or lo_state['failed'].get('cause')}); "
                           f"`{launcher} tools install libreoffice` tries again once that is fixed")
        else:
            missing.append(f"LibreOffice (slide previews): the next guide, doctor or rendering call starts its download again, "
                           f"or `{launcher} tools install libreoffice` now")
    if needs_git_bash():
        git = Job("git")
        missing.append("Git Bash for Claude Code: " + ("PortableGit is downloading in the background now" if git.alive() else
                       f"`{launcher} tools install git` fetches PortableGit into Theodore's folder (or install Git for Windows)"))
    return missing


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
    # Never "ready" while an assistant on this computer would start its next session without Theodore's skills.
    unregistered = [n.removeprefix("assistant ") for l, n, d in checks if l == "warn" and n.startswith("assistant ") and "not registered" in d]
    if unregistered and not failed:
        names = " and ".join({"claude": "Claude Code", "codex": "Codex"}.get(n, n) for n in unregistered)
        return Status("RETRY", f"Theodore is installed, but its plugin is not registered in {names} on this computer.",
                      f"run {quoted_launcher()} hosts register, then tell the user to start a new session.",
                      f"Theodore is installed, but it is not added to {names} yet. I am adding it; then start a new session.")
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
        install.refresh_plugin_latest(20.0)
        latest = manifest.get("engine", {}).get("version")
        same = latest == current
        return core.emit(Status("OK", f"installed engine {current or 'none'}; channel {manifest.get('channel')} offers {latest}."
                                + ("" if same else " An update is available."),
                                "nothing to do." if same else f"run {launcher} update --apply, or just continue: it applies at the next guide.",
                                "Theodore is up to date." if same else f"A Theodore update ({latest}) is available and will install itself."))
    install.save_update_state(held=None, failed=None)
    packs_before = {p: (i.get("status"), i.get("version")) for p, i in install.packs_state().items()}
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
    # Say what moved and what was already current: the client, the engine and the asset packs.
    updated, current_parts = [], []
    offered_client = str((manifest.get("client") or {}).get("version") or "")
    if offered_client and offered_client != __version__:
        Job("client-update").wait(60)
        if core.read_text(core.home().client / "current.txt") == offered_client:
            updated.append(f"client {offered_client} (was {__version__}; used from the next command)")
        else:
            updated.append(f"client {offered_client} (downloading in the background; was {__version__})")
    else:
        current_parts.append(f"client {__version__}")
    (updated if changed else current_parts).append(f"engine {now_current}" + (f" (was {current})" if changed else ""))
    after = install.packs_state()
    moved = sorted(p for p, i in after.items() if (i.get("status"), i.get("version")) != packs_before.get(p) and i.get("status") == "ready")
    arriving = sorted(p for p, i in after.items() if i.get("status") != "ready") if Job("packs").alive() else []
    if moved:
        updated.append(f"asset packs {', '.join(moved)}")
    if arriving:
        updated.append(f"asset packs {', '.join(arriving)} (downloading in the background)")
    if not moved and not arriving:
        current_parts.append("asset packs")
    summary = (f"updated: {'; '.join(updated)}" if updated else "nothing needed an update") + \
        (f"; already current: {', '.join(current_parts)}" if current_parts else "") + "."
    return core.emit(Status("OK", summary,
                            f"run {launcher} guide pptx again before continuing." if updated else "nothing to do.",
                            f"Theodore is updated ({'; '.join(u.split(' (')[0] for u in updated)})." if updated else "Theodore is up to date."))


def plugin_update_job() -> int:
    """Background: bring the plugin's files up to the published version in each assistant Theodore is registered in,
    through that assistant's own command (see install.py, "How a release reaches a user"). Records per assistant
    "updated" or why not; announces a success once."""
    job = Job("plugin-update")
    job.begin("plugin")
    state = install.update_state()
    latest = str(state.get("plugin_latest") or "")
    record = _host_record()
    results: dict[str, str] = {}
    try:                                    # Theodore's own copy (Codex's local marketplace; Claude Code's fallback one)
        keys = " ".join(f"{kid}={x}" for kid, x in core.manifest_keys().items())
        with contextlib.redirect_stdout(sys.stderr):
            install_plugin(str(core.config().get("dl_base") or ""), keys)
        results["theodore"] = "updated"
    except Status as status_:
        results["theodore"] = f"its copy could not be refreshed: {status_.summary}"[:200]
    claude = core.host_executable("claude")
    if claude and record.get("claude_registered") is not False and _listed([claude, "plugin", "list", "--json"], "theodore@theodore"):
        _run([claude, "plugin", "marketplace", "update", "theodore"], timeout=300)
        code, out = _run([claude, "plugin", "update", "theodore@theodore"], timeout=300)
        results["claude-code"] = "updated" if code == 0 else f"claude plugin update failed: {(out.splitlines() or ['no output'])[-1]}"[:200]
    codex = core.host_executable("codex")
    if codex and record.get("codex_registered") is not False and _listed([codex, "plugin", "list"], "theodore@theodore"):
        if not _listed([codex, "plugin", "marketplace", "list"], str(core.home().plugin)):
            _run([codex, "plugin", "marketplace", "upgrade", "theodore"], timeout=300)
        code, out = _run([codex, "plugin", "add", "theodore@theodore"], timeout=300)
        results["codex"] = "updated" if code == 0 else f"codex plugin add failed: {(out.splitlines() or ['no output'])[-1]}"[:200]
    hosts = {k: v for k, v in results.items() if k != "theodore"}
    ok = bool(hosts) and all(v == "updated" for v in hosts.values())
    done = install.update_state().get("plugin_update") or {}
    install.save_update_state(plugin_update={**done, "version": latest, "ok": ok, "hosts": results, "finished": int(core.now())})
    if any(v == "updated" for v in hosts.values()):
        install.announce("plugin", latest)
    core.log(f"plugin update to {latest}: {results}")
    job.finish()
    return 0


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
        return _tools_git(if_needed, as_job)
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
            job.fail(_failure_cause(status_), status_.summary)
        raise
    if job:
        job.finish()
    return core.emit(Status("OK", f"LibreOffice is installed at {binary}.", "nothing to do.", "LibreOffice is installed for Theodore's slide previews."))


def _failure_cause(status_: Status) -> str:
    """The cause a SETUP_FAILED status names ("cause NETWORK: ..."), else the status's name."""
    found = re.match(r"cause ([A-Z_]+):", status_.summary or "")
    return found.group(1) if found else status_.name


def needs_git_bash() -> bool:
    """Claude Code on Windows with no Git Bash where it looks: it would run Theodore's skill commands in PowerShell."""
    return core.IS_WINDOWS and bool(core.host_executable("claude")) and claude_git_bash() is None


def _tools_git(if_needed: bool, as_job: bool = False) -> int:
    """Git Bash for Claude Code on Windows: PortableGit into Theodore's folder, and Claude Code pointed at it through
    the setting its documentation names (env.CLAUDE_CODE_GIT_BASH_PATH in its user settings). Recorded, so the
    uninstall takes it back; a value the user set is never replaced."""
    if if_needed and not needs_git_bash():
        if as_job:
            Job("git").clear()
        return core.emit(Status("OK", "Git Bash is not needed here: Claude Code is not on this computer or already finds a Git Bash.",
                                "nothing to do.", "Nothing to install."))
    # Recorded as a job also when it runs in the foreground (the Windows installer's call), so a fetch that stopped
    # before it finished is started again by the next guide, skill or doctor.
    job = Job("git")
    job.begin("git")
    try:
        bash = install.install_git(job)
    except Status as status_:
        job.fail(_failure_cause(status_), status_.summary)
        raise
    job.finish()
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
    current = env.get("CLAUDE_CODE_GIT_BASH_PATH")
    if current and current != str(bash):       # the user already points Claude Code at a bash: never replaced
        return f"claude-code: CLAUDE_CODE_GIT_BASH_PATH is already set in {path}; left as it is"
    record = _host_record()
    if "git_bash_prior" not in record and current != str(bash):     # what was there before Theodore's first change
        record["git_bash_prior"] = {"present": "CLAUDE_CODE_GIT_BASH_PATH" in env,
                                    **({"value": current} if "CLAUDE_CODE_GIT_BASH_PATH" in env else {})}
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
    return code == 0 and re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", out) is not None


def _hosts_pending_path() -> Path:
    return core.home().state / "hosts.pending"


# The installer leaves state/hosts.pending before its long downloads and `hosts register` removes it. When a host's time
# limit stopped the installer before it registered the plugin, the next guide, skill or doctor does it, as `hosts
# register` does; a registration that failed is tried again at most once an hour.
HOSTS_RETRY_S = 3600


def ensure_hosts() -> None:
    """Finish the registration an installer left undone (see above). Never fails or prints in the command it runs in."""
    marker = _hosts_pending_path()
    try:
        if not marker.exists():
            return
        tried = core.read_text(marker)
        if tried and tried.isdigit() and core.now() - int(tried) < HOSTS_RETRY_S:
            return
        core.write_atomic(marker, str(int(core.now())), 0o600)
        for line in _register_hosts():
            core.log(f"hosts: {line}")
    except Exception as exc:  # a convenience: never the reason a guide or doctor fails
        core.log(f"hosts: not registered ({exc.__class__.__name__}: {exc})")


def _host_registration(name: str, command: str) -> bool | None:
    """Whether Theodore's plugin is registered in this assistant, asked the way `hosts register` asks it; None when the
    assistant's command did not answer."""
    if name == "claude":
        code, out = _run([command, "plugin", "list", "--json"], timeout=60)
        return None if code != 0 else '"theodore@theodore"' in out
    code, out = _run([command, "plugin", "list"], timeout=60)
    return None if code != 0 else re.search(r"(?<![A-Za-z0-9_-])theodore@theodore(?![A-Za-z0-9_-])", out) is not None


def _register_hosts() -> list[str]:
    """Register the plugin in Claude Code and Codex when they are on this computer, and write the lines naming Theodore
    in their instructions. One line per assistant ("... FAILED (...)" when it did not work); prints nothing. Done, the
    installer's marker (state/hosts.pending) goes: nothing is left for the next command to register."""
    cfg, home = core.config(), core.home()
    lines: list[str] = []
    claude, codex = core.host_executable("claude"), core.host_executable("codex")
    plugin_dir = home.plugin
    record = _host_record()
    if claude:
        record["claude_path"] = claude                              # found again at uninstall, also when off PATH
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
                entries = record.get("claude_entries") or {}        # the entries it added, with their values
                for parent, key in (("extraKnownMarketplaces", "theodore"), ("enabledPlugins", "theodore@theodore")):
                    was, now = before.get(parent), after.get(parent)
                    if isinstance(now, dict) and key in now and not (isinstance(was, dict) and key in was):
                        entries.setdefault(parent, {}).setdefault(key, now[key])
                if entries:
                    record["claude_entries"] = entries
                record["created_keys"] = sorted(set(record.get("created_keys") or []) | {k for k in after if k not in before})
                record["file_created"] = bool(record.get("file_created")) or (not before_file and settings_path.exists())
                if record["file_created"]:
                    record["file_identity"] = _identity(settings_path)
    elif core.host_name() == "claude-code" and not core.cloud_sandbox():
        # Claude Code runs this, so it is on the computer: never "not found" and a success with no skills.
        lines.append("claude-code: FAILED (this runs inside Claude Code, but its claude command was not found on PATH "
                     "or where Claude Code's installers put it)")
    else:
        lines.append("claude-code: not found on this computer")
    if codex:
        if not (plugin_dir / ".agents" / "plugins" / "marketplace.json").exists():
            lines.append(f"codex: FAILED (no plugin copy in {plugin_dir}; run the installer again)")
        else:
            record["codex_path"] = codex
            config_before = _codex_config().exists()
            tables_before = _codex_tables()
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
                entries, tables_after = record.get("codex_entries") or {}, _codex_tables()
                for parent, key in _CODEX_TABLES:
                    was, now = (tables_before or {}).get(parent), (tables_after or {}).get(parent)
                    if tables_before is not None and isinstance(now, dict) and key in now and not (isinstance(was, dict) and key in was):
                        entries.setdefault(parent, {}).setdefault(key, now[key])
                if entries:
                    record["codex_entries"] = entries
    elif any(os.environ.get(v) for v in ("CODEX_SANDBOX", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_MANAGED_BY_NPM")) \
            and not core.cloud_sandbox():          # Codex runs this (CODEX_HOME alone is also a user's setting)
        lines.append("codex: FAILED (this runs inside Codex, but its codex command was not found on PATH or where Codex's "
                     "installers put it)")
    else:
        lines.append("codex: not found on this computer")
    record, routed = routing.ensure(record, register=True)        # the lines naming Theodore for decks
    lines += routed
    if record:
        _save_host_record(record)
    marker = _hosts_pending_path()
    if not any("FAILED" in line for line in lines):
        core.remove_tree(marker)
    elif marker.exists():           # tried now: the next commands try again only after HOSTS_RETRY_S
        core.write_atomic(marker, str(int(core.now())), 0o600)
    return lines


def hosts(action: str, apply: bool) -> int:
    home = core.home()
    lines: list[str] = []
    claude, codex = core.host_executable("claude"), core.host_executable("codex")
    if action == "register":
        lines += _register_hosts()
    elif action == "unregister":
        left: list[str] = []
        edits: list[str] = []
        notes = _unregister_hosts(claude, codex, left, edits)
        lines += [f"{name}: removed" for name, tool in (("claude-code", claude), ("codex", codex)) if tool]
        lines += edits + [f"not removed: {item}" for item in left] + notes
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


def _claude_settings_restore(file_ours: bool = False, registration: dict | None = None,
                             unremoved: list[str] | None = None) -> tuple[str | None, list[str]]:
    """Take back exactly what the record says Theodore added or changed: its rules, the marketplace entry it created
    (only if nobody changed it since) or the autoUpdate value it changed (restored, unless changed since), and the
    keys and the file it created, only when they hold nothing. Anything else, empty or not, stays. With `registration`
    ({parent: {key: [accepted values]}}, when the assistant's own command could not be found) also the entries the
    registration added, each only while it still holds a value Theodore's registration wrote; any other is named in
    `unremoved`. Returns (a problem when the file could not be read or written, notes for what was left on purpose)."""
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
            prior = record.get("git_bash_prior") or {}
            if prior.get("present"):
                env["CLAUDE_CODE_GIT_BASH_PATH"] = prior.get("value")      # exactly what was there, an empty string too
            else:
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
    for parent, keys in (registration or {}).items():
        holder = settings.get(parent)
        for key, accepted in keys.items():
            if not isinstance(holder, dict) or key not in holder:
                continue
            if holder[key] in accepted:
                del holder[key]
            elif unremoved is not None:
                unremoved.append(f'{parent}."{key}"')
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


def _take_back_instructions(left: list[str]) -> None:
    """Remove the routing block from each file the record names, and those entries from the record."""
    record = _host_record()
    if routing.RECORD_KEY not in record:
        return
    record = routing.take_back(record, left)
    if record:
        _save_host_record(record)
    else:
        core.remove_tree(_host_record_path())


# Downloads in the background that stopped before they finished (the computer slept, the user signed out, the process
# was killed) are started again by the next guide, skill or doctor, as the asset packs are (0.3.3): LibreOffice and
# PortableGit here; the runtime and the first engine by the setup job, which every command restarts when it is not
# running. Only a fetch that was started and did not finish: one that never started is not begun here. A failure that
# needs the user (a proxy sign-in, TLS inspection, a full disk, a refusal) is not retried; doctor names it.
def resume_background() -> list[str]:
    """Restart each unfinished background fetch (see above). Returns what was started. Never fails the command."""
    started: list[str] = []
    try:
        for name, argv, present in (
                ("libreoffice", ["tools", "install", "libreoffice", "--job"],
                 lambda: install.system_libreoffice() or install.libreoffice_binary()),
                ("git", ["tools", "install", "git", "--if-needed", "--job"],
                 lambda: install.theodore_git_bash() or not needs_git_bash())):
            job = Job(name)
            state = job.read()
            if not state or state.get("done") or job.alive():
                continue
            if present():
                job.clear()
                continue
            if state.get("failed") and not install.resumable_failure(state):
                continue
            job.spawn(argv)
            core.log(f"resumed the {name} download that had not finished")
            started.append(name)
    except Exception as exc:  # a convenience: never the reason a guide or doctor fails
        core.log(f"resume: not started ({exc.__class__.__name__}: {exc})")
    return started


def ensure_instructions() -> None:
    """An install that never ran the installer (the plugin added from a marketplace) gets the routing block at its
    first guide, skill or doctor run; later runs update its wording in place. Never fails the command it runs in."""
    try:
        record = _host_record()
        updated, lines = routing.ensure(record)
        if updated != record:
            _save_host_record(updated)
        for line in lines:
            core.log(line)
    except Exception as exc:  # the block is a convenience: a guide or doctor run never fails because of it
        core.log(f"instructions: not written ({exc.__class__.__name__}: {exc})")


def _claude_registration(record: dict) -> dict:
    """The entries Theodore's registration added to Claude Code's settings.json, as {parent: {key: [accepted values]}}:
    those it recorded with their values, or, for a record written before 0.3.5 (which says only that Theodore registered
    the plugin and added the marketplace), the values a registration writes. Nothing that was there before."""
    found: dict = {}
    entries = record.get("claude_entries") if isinstance(record.get("claude_entries"), dict) else None
    ours = {"enabledPlugins": record.get("claude_registered") is True or bool(entries and "enabledPlugins" in entries),
            "extraKnownMarketplaces": record.get("claude_added_marketplace") is True or bool(entries and "extraKnownMarketplaces" in entries)}
    if record.get("claude_registered") is False:
        ours["enabledPlugins"] = False
    if record.get("claude_added_marketplace") is False:
        ours["extraKnownMarketplaces"] = False
    url = str(core.config().get("marketplace_url") or "")
    legacy = {"enabledPlugins": {"theodore@theodore": [True]},
              "extraKnownMarketplaces": {"theodore": [{"source": {"source": "url", "url": url}},
                                                      {"source": {"source": "directory", "path": str(core.home().plugin)}}]}}
    for parent, keys in legacy.items():
        if not ours[parent]:
            continue
        recorded = (entries or {}).get(parent)
        found[parent] = {k: [v] for k, v in recorded.items()} if isinstance(recorded, dict) and recorded else keys
    return found


def _claude_files_take_back(registration: dict, left: list[str], config_dir: Path) -> list[str]:
    """Without Claude Code's command: what `claude plugin uninstall` and `claude plugin marketplace remove` would also
    change in its plugins folder for an entry Theodore's registration added: the theodore@theodore entry in
    installed_plugins.json, the theodore entry in known_marketplaces.json, and the plugin's copies (cache/theodore,
    marketplaces/theodore). Returns the files changed; problems go to `left`."""
    base = config_dir / "plugins"
    changed: list[str] = []
    for name, ours, path_keys in (("installed_plugins.json", "enabledPlugins" in registration, ("plugins", "theodore@theodore")),
                                  ("known_marketplaces.json", "extraKnownMarketplaces" in registration, (None, "theodore"))):
        path = base / name
        if not ours or not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            left.append(f'the theodore entry in {path} (not readable as JSON, so not changed)')
            continue
        parent, key = path_keys
        holder = data.get(parent) if parent and isinstance(data, dict) else data
        if not isinstance(holder, dict) or key not in holder:
            continue
        del holder[key]
        try:
            core.write_atomic(path, json.dumps(data, indent=2) + ("\n" if raw.endswith("\n") else ""), 0o644)
            changed.append(str(path))
        except OSError as exc:
            left.append(f'the "{key}" entry in {path} ({exc.__class__.__name__})')
    for sub, ours in (("cache", "enabledPlugins" in registration), ("marketplaces", "extraKnownMarketplaces" in registration)):
        folder = base / sub / "theodore"
        if ours and folder.is_dir() and not folder.is_symlink():
            core.remove_tree(folder)
            if folder.exists():
                left.append(f"Claude Code's copy of the Theodore plugin in {folder}")
    return changed


# Codex keeps its registration in config.toml as two tables.
_CODEX_TABLES = (("marketplaces", "theodore"), ("plugins", "theodore@theodore"))


def _codex_tables() -> dict | None:
    """Codex's config.toml parsed ({} for no file), or None when it cannot be read or parsed."""
    try:
        import tomllib
    except ImportError:             # Python before 3.11: Theodore's runtime has 3.12
        return None
    path = _codex_config()
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return None


def _codex_take_back(record: dict, left: list[str]) -> list[str]:
    """Without Codex's command: remove from config.toml the two tables Theodore's registration added
    ([marketplaces.theodore], [plugins."theodore@theodore"]), each only while it holds what the registration wrote, by
    deleting exactly their lines (everything else in the file stays byte for byte; the result is parsed again and
    must equal the file without those two tables), and Codex's copy of the plugin. Returns the files changed."""
    entries = record.get("codex_entries") if isinstance(record.get("codex_entries"), dict) else {}
    ours = {"marketplaces": record.get("codex_added_marketplace") is True or "marketplaces" in entries,
            "plugins": record.get("codex_registered") is True or "plugins" in entries}
    if record.get("codex_added_marketplace") is False:
        ours["marketplaces"] = False
    if record.get("codex_registered") is False:
        ours["plugins"] = False
    if not any(ours.values()):
        return []
    path = _codex_config()
    parsed = _codex_tables()
    changed: list[str] = []
    if parsed is None:
        left.append(f'Theodore\'s tables [marketplaces.theodore] and [plugins."theodore@theodore"] in {path} '
                    f"(the file could not be read as TOML, so it was not changed)")
        return changed
    accepted = {"marketplaces": [entries.get("marketplaces", {}).get("theodore")] if "marketplaces" in entries else
                [{"source_type": "local", "source": str(core.home().plugin)}],
                "plugins": [entries.get("plugins", {}).get("theodore@theodore")] if "plugins" in entries else [{"enabled": True}]}
    drop, kept = [], []
    for parent, key in _CODEX_TABLES:
        holder = parsed.get(parent)
        if not ours[parent] or not isinstance(holder, dict) or key not in holder:
            continue
        (drop if holder[key] in accepted[parent] else kept).append((parent, key))
    for parent, key in kept:
        left.append(f'the [{parent}.{_toml_key(key)}] table in {path} (changed after Theodore\'s registration wrote it, so it was left as it is)')
    if drop:
        text = path.read_text(encoding="utf-8")
        headers = {f"[{parent}.{_toml_key(key)}]" for parent, key in drop}
        new = _toml_without(text, headers)
        expected = json.loads(json.dumps(parsed))
        for parent, key in drop:
            expected[parent].pop(key, None)
            if not expected[parent]:
                expected.pop(parent)
        try:
            import tomllib
            same = tomllib.loads(new) == expected
        except ValueError:
            same = False
        if not same:
            left.append(f"Theodore's tables {', '.join(sorted(headers))} in {path} (they could not be removed without changing anything else)")
        else:
            try:
                core.write_atomic(path, new, 0o600 if core.IS_WINDOWS else (path.stat().st_mode & 0o777))
                changed.append(str(path))
            except OSError as exc:
                left.append(f"Theodore's tables {', '.join(sorted(headers))} in {path} ({exc.__class__.__name__})")
    folder = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "plugins" / "cache" / "theodore"
    if ours["plugins"] and folder.is_dir() and not folder.is_symlink():
        core.remove_tree(folder)
        if folder.exists():
            left.append(f"Codex's copy of the Theodore plugin in {folder}")
    return changed


def _toml_without(text: str, headers: set[str]) -> str:
    """The text without the tables whose header line is one of `headers`, each from its header to the next header. The
    blank lines before a removed table go with it unless a kept table or a comment follows, and the comments just above
    the next table stay with that table."""
    out: list[str] = []
    blanks: list[str] = []          # blank lines not yet written: they separate what comes before from what comes next
    pending: list[str] = []         # blank and comment lines at the end of a removed table
    skipping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            drop = stripped.split("#", 1)[0].strip() in headers
            if drop:
                skipping, pending = True, []
                continue
            if skipping:
                while pending and not pending[0].strip():
                    pending.pop(0)
                out += blanks + pending
                blanks, pending, skipping = [], [], False
            out += blanks + [line]
            blanks = []
            continue
        if skipping:
            if not stripped or stripped.startswith("#"):
                pending.append(line)
            else:
                pending = []
            continue
        if not stripped:
            blanks.append(line)
        else:
            out += blanks + [line]
            blanks = []
    if skipping:
        comments = [line for line in pending if line.strip()]
        out += (blanks + comments) if comments else []
    else:
        out += blanks
    return "".join(out)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else f'"{key}"'


def _unregister_hosts(claude: str | None, codex: str | None, left: list[str], edits: list[str] | None = None) -> list[str]:
    """Remove the plugin from each assistant; its marketplace only when Theodore added it (or when no record says);
    then the settings Theodore recorded. An assistant whose command cannot be found (off PATH and not where its
    installers put it) has the entries Theodore's registration added taken out of its files directly, said in `edits`.
    Problems go to `left`; returns notes for what stays on purpose."""
    record = _host_record()
    edits = [] if edits is None else edits
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
            continue                # taken back from the files below
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
    registration = _claude_registration(record) if not claude else {}
    unremoved: list[str] = []
    settings_text = settings_path.read_bytes() if registration and settings_path.is_file() else None
    problem, settings_notes = _claude_settings_restore(file_ours, registration, unremoved)
    if problem:
        left.append(f"the Theodore entries in {settings_path} ({problem})")
    notes += settings_notes
    if not claude:
        changed = _claude_files_take_back(registration, left, settings_path.parent) if registration else []
        if settings_text is not None and (not settings_path.exists() or settings_path.read_bytes() != settings_text):
            changed.insert(0, str(settings_path))
        if changed:
            edits.append(f"claude-code: its claude command was not found, so Theodore took back its registration by editing "
                         f"{', '.join(changed)} directly (read and written again as JSON: the same content, the spacing may differ)")
        residue = unremoved + [f'{p}."{k}"' for p, k in (("enabledPlugins", "theodore@theodore"),) if record.get("claude_registered") is not False
                               and _theodore_entry(settings_path, p, k) and f'{p}."{k}"' not in unremoved]
        if residue:
            left.append(f"Theodore's registration in Claude Code: {' and '.join(residue)} in {settings_path} (Claude Code's claude command "
                        f"was not found, and these entries no longer hold what Theodore's registration wrote; in Claude Code, "
                        f"/plugin uninstall theodore@theodore removes them)")
    if not codex:
        changed = _codex_take_back(record, left)
        if changed:
            edits.append(f"codex: its codex command was not found, so Theodore took back its registration by removing its two tables "
                         f"from {', '.join(changed)} directly (nothing else in the file changed)")
    _take_back_instructions(left)
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
    """The engine's render caches: its current folder (Theodore) and the one engines before 1.0.0-rc.4 used."""
    if sys.platform == "darwin":
        caches = Path.home() / "Library" / "Caches"
        found = [caches / "pptx-runtime", caches / "Theodore"]
        if os.environ.get("XDG_CACHE_HOME"):          # engines up to rc.6 honoured it on every OS
            found.append(Path(os.environ["XDG_CACHE_HOME"]) / "pptx-runtime")
        return found
    if core.IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        found = [local / "pptx-runtime", local / "Theodore" / "Cache"]
        if os.environ.get("XDG_CACHE_HOME"):          # engines up to rc.6 honoured it on every OS
            found.append(Path(os.environ["XDG_CACHE_HOME"]) / "pptx-runtime")
        return found
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return [cache / "pptx-runtime", cache / "theodore"]


# The engine's caches before 1.0.0-rc.7, outside its render cache: ~/.cache/pptx-slide-raster (images of the user's
# slides, <sha256 of the deck>/slide-<nnnn>-<dpi>.jpg), ~/.cache/pptx-icons and ~/.cache/pptx-flags (<sha1>.png and
# <sha1>.uri), and the icons' fallback in the temporary folder. Node's os.homedir() is %USERPROFILE% on Windows, so the
# same names there. Only files of the names the engine writes are removed; a folder goes once nothing else is in it.
_ICON_NAME = re.compile(r"[0-9a-f]{40}\.(png|uri)")
_RASTER_DIR = re.compile(r"[0-9a-f]{64}")
_RASTER_NAME = re.compile(r"slide-[0-9]{4}-[0-9]+\.jpg")


def _old_engine_caches() -> list[Path]:
    user = Path.home()
    folders = [user / ".cache" / "pptx-slide-raster", user / ".cache" / "pptx-icons", user / ".cache" / "pptx-flags"]
    # Node's os.tmpdir(): TMPDIR, TMP or TEMP (on Windows TEMP or TMP), else /tmp.
    names = ("TEMP", "TMP") if core.IS_WINDOWS else ("TMPDIR", "TMP", "TEMP")
    temp = next((os.environ[n] for n in names if os.environ.get(n)), None if core.IS_WINDOWS else "/tmp")
    if temp:
        folders.append(Path(temp.rstrip("/\\") or "/") / "pptx-icons")
    return folders


def _remove_old_engine_caches(left: list[str]) -> list[str]:
    """Remove the engine's former caches (see above). Returns the folders removed; a folder that still holds anything
    else is named in `left`."""
    removed: list[str] = []
    for folder in _old_engine_caches():
        try:
            if folder.is_symlink() or not folder.is_dir():
                continue
            if not core.IS_WINDOWS and folder.stat().st_uid != os.getuid():       # a shared /tmp: another user's
                continue
            raster = folder.name == "pptx-slide-raster"
            for entry in list(folder.iterdir()):
                if raster and _RASTER_DIR.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink():
                    for item in list(entry.iterdir()):
                        if _RASTER_NAME.fullmatch(item.name) and item.is_file() and not item.is_symlink():
                            item.unlink()
                    with contextlib.suppress(OSError):
                        entry.rmdir()
                elif not raster and _ICON_NAME.fullmatch(entry.name) and entry.is_file() and not entry.is_symlink():
                    entry.unlink()
            folder.rmdir()
            removed.append(str(folder))
        except OSError:
            if folder.exists():
                left.append(f"the folder {folder} (a cache of the engine before 1.0.0-rc.7: its files of the engine's were removed, "
                            f"and what else it holds is not Theodore's and was left)")
    return removed


# What the engine keeps in the temporary folder (Python's tempfile.gettempdir(), the engine's own rule): LibreOffice's
# profile and its warm copy, the profile's lock file, and the folder of empty lock files. ACCEPT-LOCAL2 D-B.
_TEMP_PROFILE = ("pptx-soffice-profile", "pptx-soffice-profile.warm-seed")
_TEMP_PROFILE_LOCK = "pptx-soffice-profile.lock"
_TEMP_LOCKS = "theodore-pptx-locks"


def _remove_engine_temp(left: list[str]) -> list[str]:
    """Remove those, only by the engine's names, only this user's, never through a link. Returns what was removed."""
    import tempfile
    base = Path(tempfile.gettempdir())
    removed: list[str] = []

    def mine(path: Path) -> bool:
        try:
            return not path.is_symlink() and (core.IS_WINDOWS or path.lstat().st_uid == os.getuid())
        except OSError:
            return False
    for name in _TEMP_PROFILE:
        folder = base / name
        if folder.is_dir() and mine(folder):
            core.remove_tree(folder)
            (left.append(f"the LibreOffice profile Theodore's engine used in {folder}") if folder.exists() else removed.append(str(folder)))
    lock = base / _TEMP_PROFILE_LOCK
    if lock.is_file() and mine(lock):
        with contextlib.suppress(OSError):
            lock.unlink()
            removed.append(str(lock))
    locks = base / _TEMP_LOCKS
    if locks.is_dir() and mine(locks):
        for item in list(locks.iterdir()):
            if item.suffix == ".lock" and item.is_file() and not item.is_symlink():
                with contextlib.suppress(OSError):
                    item.unlink()
        try:
            locks.rmdir()
            removed.append(str(locks))
        except OSError:
            left.append(f"the folder {locks} (the engine's lock files were removed; what else it holds is not Theodore's and was left)")
    return removed


# An assistant that is on the computer but never had Theodore registered: nothing to remove, not a failure.
_NOT_THERE = ("not found", "not installed", "not configured", "no such", "does not exist", "unknown plugin", "unknown marketplace",
              "not registered", "is not a known", "no plugin named", "no marketplace named")


def _unregistered(command: list[str]) -> str | None:
    """Run one unregistration; None when it worked or there was nothing to remove, else what went wrong."""
    code, out = _run(command)
    if code == 0 or any(text in out.lower() for text in _NOT_THERE):
        return None
    return (out.splitlines()[-1] if out else f"exit {code}")[:160]


def _theodore_entry(path: Path, parent: str, key: str) -> bool:
    settings = _load_settings(path) if path.exists() else None
    return isinstance(settings, dict) and isinstance(settings.get(parent), dict) and key in settings[parent]


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


def _theodore_pids() -> list[int]:
    """Theodore's own running processes, each found by a file Theodore wrote for it, never by name: every background
    job (state/jobs/*.json), the launcher's bootstrap (state/bootstrap/pid) and every tool server
    (state/servers/<pid>). This process is never among them."""
    home = core.home()
    pids: set[int] = set()
    for path in (sorted(home.jobs.glob("*.json")) if home.jobs.is_dir() else []):
        with contextlib.suppress(ValueError, TypeError):
            pids.add(int(Job(path.stem).read().get("pid") or 0))
    with contextlib.suppress(OSError, ValueError):
        pids.add(int((home.state / "bootstrap" / "pid").read_text(encoding="utf-8").strip() or 0))
    servers = home.state / "servers"
    for path in (servers.iterdir() if servers.is_dir() else []):
        with contextlib.suppress(ValueError):
            pids.add(int(path.name))
    return sorted(p for p in pids if p > 0 and p != os.getpid() and core.pid_alive(p))


def _processes_in_folder(root: Path) -> list[int]:
    """Windows: the processes whose program file is inside Theodore's folder (its Python, its Node, LibreOffice, Git
    Bash), whatever started them; never this process or its parents. Read with WMI. Empty elsewhere."""
    if not core.IS_WINDOWS or not root.exists():
        return []
    ps = os.path.join(os.environ.get("SystemRoot") or "C:" + os.sep + "Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    os.environ["THEODORE_ROOT_SCAN"] = str(root).rstrip("\\") + "\\"
    code, out = _run([ps, "-NoProfile", "-NonInteractive", "-Command",
                      "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
                      "$_.ExecutablePath.StartsWith($env:THEODORE_ROOT_SCAN, [StringComparison]::OrdinalIgnoreCase) } | "
                      "ForEach-Object { $_.ProcessId }"], timeout=60)
    mine = {os.getpid(), os.getppid()}
    return sorted({int(x) for x in out.split() if x.isdigit()} - mine) if code == 0 else []


def _stop_theodore_processes(wait: float = 15.0) -> list[int]:
    """Stop Theodore's own processes and the processes they started (on Windows the whole tree: a download or an
    unpacker holds files in the folder, and Windows cannot delete a file in use), then wait until they are gone. On
    Windows also every process running a program from inside Theodore's folder (the launcher's bootstrap has no pid
    record there), each logged."""
    pids = _theodore_pids()
    for pid in _processes_in_folder(core.home().root):
        if pid not in pids:
            pids.append(pid)
            core.log(f"uninstall: process {pid} runs a program from inside {core.home().root}: stopped")
    for pid in pids:
        if core.IS_WINDOWS:
            _run([os.path.join(os.environ.get("SystemRoot") or "C:" + os.sep + "Windows", "System32", "taskkill.exe"),
                  "/PID", str(pid), "/T", "/F"], timeout=30)
        else:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(pid), 15) if os.getpgid(pid) == pid else os.kill(pid, 15)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and any(core.pid_alive(p) for p in pids):
        time.sleep(0.25)
    if not core.IS_WINDOWS:
        for pid in pids:
            if core.pid_alive(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, 9)
    return pids


UNINSTALL_MARKER = ".theodore-uninstalling"


def _windows_cleanup(root: Path, names: list[str], keep_specimen_record: bool) -> Path:
    """Delete Theodore's folders after this process ends (its Python runs from inside them): a script in a private
    temporary folder, started outside this console's job (a closed console or SSH session does not stop it), retries
    every two seconds for up to two minutes while Windows releases the last files, then writes its result beside it
    and removes itself. Returns the result file."""
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="theodore-uninstall-"))
    result = work / "result.txt"
    # The cleanup works only while this marker is there. An installer or launcher that starts in the folder meanwhile
    # (an install right after the uninstall) removes it first, so the cleanup stops instead of deleting the new
    # installation's files as they are written (its sign-in was lost: ACCEPT-LOCAL2, observation b).
    marker = root / UNINSTALL_MARKER
    core.write_atomic(marker, f"{os.getpid()}\n", 0o644)
    # A few seconds first: the launcher that called this client reads its own file to its end once the client exits.
    lines = ["@echo off", "setlocal", "set N=0", "ping -n 3 127.0.0.1 >nul", ":again",
             f'if exist "{marker}" goto pass', f'>"{result}" echo stopped: a new installation started in {root}', "goto end",
             ":pass"]
    for name in names:
        target = root / name
        if name == "state" and keep_specimen_record:
            lines.append(f'for /d %%D in ("{target}\\*") do rmdir /s /q "%%D" >nul 2>&1')
            lines.append(f'for %%F in ("{target}\\*") do if /i not "%%~nxF"=="{specimens.registry().name}" del /f /q "%%F" >nul 2>&1')
        else:
            lines.append(f'if exist "{target}" rmdir /s /q "{target}" >nul 2>&1')
    checks = [f'if exist "{root / n}" goto wait' for n in names if not (n == "state" and keep_specimen_record)]
    # Every second for up to five minutes; the result says how many passes it took and what, if anything, stayed.
    lines += checks + ["goto done", ":wait", "set /a N+=1", "if %N% geq 300 goto done",
                       "ping -n 2 127.0.0.1 >nul", "goto again", ":done", f'del /f /q "{marker}" >nul 2>&1',
                       f'rmdir "{root}" >nul 2>&1', f'>>"{result}" echo passes: %N%']
    lines += [f'if exist "{root / n}" (>>"{result}" echo left: {root / n})' for n in names
              if not (n == "state" and keep_specimen_record)]
    # The result is kept five minutes for whoever wants to confirm, then this folder goes too: nothing is left.
    lines += [f'if not exist "{root}" (>>"{result}" echo removed: {root})', ":end", "ping -n 301 127.0.0.1 >nul",
              f'(goto) 2>nul & rmdir /s /q "{work}"']
    script = work / "cleanup.cmd"
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    system32 = os.path.join(os.environ.get("SystemRoot") or "C:" + os.sep + "Windows", "System32")
    command = f'"{os.path.join(system32, "cmd.exe")}" /d /c "{script}"'
    detached, new_group, no_window, breakaway = 0x00000008, 0x00000200, 0x08000000, 0x01000000
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(work)}
    try:                                    # outside this console's job object, when the job allows it
        subprocess.Popen(command, creationflags=detached | new_group | no_window | breakaway, **quiet)
        return result
    except OSError:
        pass
    # A job that forbids breaking away (some SSH servers, some terminals): WMI creates the process outside it.
    ps = os.path.join(system32, "WindowsPowerShell", "v1.0", "powershell.exe")
    os.environ["THEODORE_CLEANUP"], os.environ["THEODORE_CLEANUP_DIR"] = command, str(work)
    code, _out = _run([ps, "-NoProfile", "-NonInteractive", "-Command",
                       "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$env:THEODORE_CLEANUP; "
                       "CurrentDirectory=$env:THEODORE_CLEANUP_DIR}; exit $r.ReturnValue"], timeout=60) \
        if os.path.exists(ps) else (1, "")
    if code != 0:
        subprocess.Popen(command, creationflags=detached | new_group | no_window, **quiet)
    return result


def uninstall(keep_hosts: bool) -> int:
    home = core.home()
    left: list[str] = []
    notes: list[str] = []
    edits: list[str] = []
    if not keep_hosts:
        notes = _unregister_hosts(core.host_executable("claude"), core.host_executable("codex"), left, edits)
    else:
        _take_back_instructions(left)           # its record goes with state/: the block cannot be left without it
    stopped = _stop_theodore_processes()
    if stopped:
        core.log(f"uninstall: stopped Theodore's own processes {stopped}")
    if not credentials.delete():
        left.append(f"the sign-in item in the login keychain (service {credentials.SERVICE})")
    for cache in _render_cache_dirs():
        core.remove_tree(cache)
        if cache.exists():
            left.append(f"the render cache {cache}")
    old_caches = _remove_old_engine_caches(left)
    if old_caches:
        edits.append(f"the engine's former caches were removed ({', '.join(old_caches)})")
    temp = _remove_engine_temp(left)
    if temp:
        edits.append(f"the engine's LibreOffice profile and lock files were removed from the temporary folder ({', '.join(temp)})")
    specimens_left = specimens.sweep(everything=True)
    for folder in specimens_left:
        left.append(f"the specimen folder {folder} (its record is kept in {specimens.registry()}; running uninstall again retries it)")
    root = home.root
    # Only Theodore's own subfolders go. Anything else in the folder was not put there by Theodore and stays.
    if core.IS_WINDOWS:  # the running interpreter lives inside the folder: a cleanup deletes it after this process ends
        # Everything this process does not run from goes now; its own Python (runtime), its code (client) and the
        # launcher that called it (bin) go a moment after it exits.
        for name in core.OWNED:
            if name in ("runtime", "client", "bin") or (name == "state" and specimens_left):
                continue
            core.remove_tree(root / name)
        names = list(core.OWNED)        # each guarded by "if exist" in the script: a folder written meanwhile (a log) goes too
        result = _windows_cleanup(root, names, bool(specimens_left)) if any((root / x).exists() for x in names) else None
        what = (f"its folder {root} is deleted by a cleanup that starts when this command ends (Theodore's own processes are "
                f"stopped; it retries every second for up to five minutes while Windows releases the last files; its result is written to "
                f"{result} for five minutes)") if result else f"{root} was deleted"
        what += (f"; {'; '.join(edits)}" if edits else "") + (f"; left as they are: {'; '.join(notes)}" if notes else "")
        if left:
            print(f"THEODORE RETRY: Theodore's registrations and sign-in were removed and {what}, but these could not be: "
                  f"{'; '.join(left)}.\n"
                  "NEXT: tell the user what is left (each item above names the folder, the file or the command); it can be removed by hand.\n"
                  "TELL THE USER: Theodore is removed, but a few pieces could not be: " + "; ".join(left) + ". Your decks were not touched.",
                  flush=True)
            return 75
        print(f"THEODORE OK: Theodore was removed from this computer (registrations and sign-in removed; {what}).\n"
              f"NEXT: nothing to do. If {root} is still there after two minutes, it can be deleted by hand: nothing of "
              "Theodore runs any more. Deck workspaces keep a small .pptx-state/theodore-engine.json file; it is harmless.\n"
              "TELL THE USER: Theodore is uninstalled; its folder disappears within a minute or two. Your decks and workspaces were not touched.",
              flush=True)
        return 0
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
    if edits:           # registrations taken back by editing an assistant's files (its command was not found)
        what += "; " + "; ".join(edits)
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
