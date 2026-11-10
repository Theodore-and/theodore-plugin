"""The commands an assistant runs: guide, report, prefs, status, doctor, setup, packs, tools, update, hosts, uninstall."""

from __future__ import annotations

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

from . import __version__, account, api, core, credentials, install
from .contract import (GUIDE_PART_CHARS, HEADER_GUIDE_PARTS, HEADER_GUIDE_VERSION, PACK_ORDER, PLUGIN_LATEST_SCHEMA, PREFERENCE_MAX_CHARS,
                       REPORT_MAX_BYTES, REPORT_MESSAGE_MAX, REPORT_TITLE_MAX, SKILLS)
from .core import Job, Status, quoted_launcher


def _bearer() -> str:
    cred = credentials.load()
    if not cred:
        raise account.login_start()
    return cred["installation_secret"]


def _api(route: str, what: str, **kwargs):
    try:
        return api.call(route, bearer=_bearer(), **kwargs)
    except api.NetworkError as error:
        raise api.offline(error, what) from None
    except api.ApiError as error:
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
    _heartbeat(version)
    return 0


def _directions(skill: str) -> str:
    """Server-composed directions for this account (beta reporting, saved preferences). Optional: silent on failure."""
    cred = credentials.load()
    if not cred:
        return ""
    try:
        _status, _headers, raw = api.call("guide", bearer=cred["installation_secret"], skill=skill, timeout=3.0)
        return raw.decode("utf-8", errors="replace")
    except (api.ApiError, api.NetworkError):
        return ""


def _guide_remote(skill: str, section: str | None, part: int) -> int:
    claims = account.require(skill)
    query = {"part": part} if part > 1 else None
    what = f"guide {skill}"
    if section:
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
    next_command = f"{quoted_launcher()} guide {skill}{' ' + section if section and section != 'license' else ''}{' --license' if section == 'license' else ''} --part {part + 1}"
    _print_document(skill, label, str(claims.get("plan", "unknown")), name, body, part, total, [], next_command)
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
    from .redact import redact_free_text
    text = redact_free_text(text)
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
        if not allowed:     # known before sending: nothing leaves this computer
            return core.emit(Status("BLOCKED_BY_POLICY", "automatic reports are off for this account; nothing was sent." if why == "off"
                                    else "automatic reports are not part of this account's plan; nothing was sent.", _REPORTS_OFF_NEXT, None))
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
    if origin != "user_requested":  # automatic reports are silent: no line for the user (the owner's decision)
        return core.emit(Status("OK", f"automatic report filed as {reference}.",
                                "continue with the user's task; do not mention the report unless the user asks.", None))
    return core.emit(Status("OK", f"report filed as {reference}.", "continue with the user's task.",
                            "I sent Theodore a technical bug report (no deck content). You can see it at theodoreand.ai/app/reports."
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
                                "I saved that as a lasting Theodore preference. You can edit it at theodoreand.ai/app/preferences."))
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
                version = install.stage_engine(install.engine_artifact(manifest), job)
                if not install.current_engine():
                    install.activate_engine(version)
                install.save_update_state(last_check=int(core.now()), latest=version, guides=manifest.get("guides") or {},
                                          tools=manifest.get("tools") or {})
                install.start_packs_job(manifest.get("packs") or [])
            except (Status, api.ApiError, api.NetworkError, install.EngineError, KeyError) as exc:
                core.log(f"setup: engine not installed yet: {getattr(exc, 'summary', exc)}")
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
    waiting = [p for p, s in snap["packs"].items() if s["status"] != "ready"]
    packs_note = "all asset packs are installed" if not waiting else f"asset packs still arriving: {', '.join(waiting)}"
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
    for pack, info in snap["packs"].items():
        add("ok" if info["status"] == "ready" else "warn", f"pack {pack}",
            f"{info['version']}" if info["status"] == "ready" else f"{info['status']} ({info['percent']}%)")
    for name in ("claude", "codex"):
        add("ok", f"assistant {name}", shutil.which(name) or "not found on PATH")
    if as_json:
        print(json.dumps({"checks": [{"level": l, "name": n, "detail": d} for l, n, d in checks], "snapshot": snap}, indent=2, sort_keys=True))
        return 0
    for level, name, detail in checks:
        print(f"{level.upper():4}  {name}: {detail}")
    blocking = _blocking_status(snap)
    if blocking:
        return core.emit(blocking)
    failed = [(n, d) for l, n, d in checks if l == "fail"]
    if any(n == "data directory" for n, _d in failed):
        return core.emit(api.setup_failed("DISK", f"{home.root} is not writable"))
    if failed:          # the last line never says ready when a check failed
        what = "; ".join(f"{n} ({d})" for n, d in failed)
        return core.emit(Status("INTERNAL", f"Theodore is installed but not ready: {what}.",
                                f"tell the user what failed; run {quoted_launcher()} doctor again after fixing it, or report it with: "
                                f"{quoted_launcher()} report --kind bug --origin user_requested",
                                f"Theodore is installed but not working yet: {', '.join(n for n, _d in failed)} failed its check. I can report this to Theodore."))
    warned = {n for l, n, _d in checks if l == "warn"}
    notes = []
    if any(n.startswith("pack ") for n in warned):
        notes.append("Its icon, flag, map and background libraries are still downloading in the background.")
    if "renderer" in warned:
        notes.append("Slide previews need LibreOffice, which is still downloading or not installed." if Job("libreoffice").alive()
                     else "Slide previews need LibreOffice, which is not installed.")
    if "processor" in warned:
        notes.append("This computer runs Windows on ARM, so Theodore uses its x64 build through Windows' emulation, which is slower.")
    if "service" in warned:
        notes.append("The Theodore service could not be reached just now.")
    if "rasteriser" in warned or "launcher" in warned:
        notes.append("See the check list above for details.")
    note = (" " + " ".join(notes)) if notes else ""
    return core.emit(Status("OK", f"Theodore is ready: engine {snap['engine']}, plan {snap['plan']}.{note}",
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
        raise core.usage(f"the update channel belongs to the account, not to this computer (it is {claims.get('chan', 'unknown')} now).",
                         "tell the user the channel is changed in their Theodore account at https://theodoreand.ai/app/plan, then continue.")
    account.require("pptx")
    install.require_runtime()
    current = install.current_engine()
    if action == "check":
        try:
            manifest = install.fetch_channel_manifest()
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
    manifest = install.check_updates(force=True, wait=240.0, timeout=20.0)
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


def client_update_job(version_: str, sha256: str, runtime_id: str | None = None) -> int:
    job = Job("client-update")
    job.begin("client")
    try:
        install.update_client(version_, sha256, runtime_id or None)
        job.finish()
        return 0
    except Exception as exc:
        job.fail("INTERNAL", str(getattr(exc, "summary", exc)))
        return 1


# --- packs and tools -----------------------------------------------------------------------------------------------------------

def packs(action: str, name: str | None, everything: bool, background: bool, as_job: bool) -> int:
    if action == "status":
        state = install.packs_state()
        for pack in PACK_ORDER:
            info = state.get(pack, {})
            print(f"{pack}\t{info.get('status', 'missing')}\t{info.get('version', '')}\t{info.get('percent', 0)}%")
        missing = [p for p in PACK_ORDER if state.get(p, {}).get("status") != "ready"]
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
    if job:
        job.finish()
    return core.emit(Status("OK", f"asset pack(s) installed: {', '.join(e['id'] for e in wanted)}.", "nothing to do.",
                            "Theodore's asset libraries are installed."))


def tools(name: str, background: bool, as_job: bool) -> int:
    if name != "libreoffice":
        raise core.usage(f"unknown tool '{name}'. The only tool is: libreoffice.")
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


def hosts(action: str, apply: bool) -> int:
    cfg, home = core.config(), core.home()
    lines: list[str] = []
    claude, codex = shutil.which("claude"), shutil.which("codex")
    plugin_dir = home.plugin
    if action == "register":
        if claude:
            code, out = _run([claude, "plugin", "list", "--json"])
            if code == 0 and '"theodore@theodore"' in out:
                lines.append("claude-code: already registered")
            else:
                url = str(cfg.get("marketplace_url") or "")
                code, out = _run([claude, "plugin", "install", "theodore", "--marketplace", url]) if url else (1, "")
                if code != 0 and url:
                    _run([claude, "plugin", "marketplace", "add", url])
                    code, out = _run([claude, "plugin", "install", "theodore@theodore"])
                if code != 0 and (plugin_dir / ".claude-plugin" / "marketplace.json").exists():
                    _run([claude, "plugin", "marketplace", "add", str(plugin_dir)])
                    code, out = _run([claude, "plugin", "install", "theodore@theodore"])
                lines.append("claude-code: registered" if code == 0 else f"claude-code: FAILED ({out.splitlines()[-1] if out else 'no output'})")
        else:
            lines.append("claude-code: not found on this computer")
        if codex:
            if not (plugin_dir / ".agents" / "plugins" / "marketplace.json").exists():
                lines.append(f"codex: FAILED (no plugin copy in {plugin_dir}; run the installer again)")
            else:
                _run([codex, "plugin", "marketplace", "add", str(plugin_dir)])
                code, out = _run([codex, "plugin", "add", "theodore@theodore"])
                lines.append("codex: registered" if code == 0 else f"codex: FAILED ({out.splitlines()[-1] if out else 'no output'})")
        else:
            lines.append("codex: not found on this computer")
    elif action == "unregister":
        if claude:
            _run([claude, "plugin", "uninstall", "theodore@theodore"])
            _run([claude, "plugin", "marketplace", "remove", "theodore"])
            lines.append("claude-code: removed")
        if codex:
            _run([codex, "plugin", "remove", "theodore@theodore"])
            _run([codex, "plugin", "marketplace", "remove", "theodore"])
            lines.append("codex: removed")
        _claude_settings(remove=True)
    elif action == "settings":
        if not claude:
            lines.append("claude-code: not found; nothing to set")
        elif apply:
            lines.append(_claude_settings(remove=False))
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
                                "register by hand with the commands under 'Other ways' at https://theodoreand.ai/install, then continue.",
                                "Theodore is installed, but I could not add it to one of your assistants automatically. I will add it by hand."))
    return core.emit(Status("OK", "; ".join(lines) or "nothing to do.", "continue.",
                            "Theodore is registered in the assistants found on this computer." if action == "register" else "Done."))


def _claude_settings(remove: bool) -> str:
    """Add (or take back) exactly two things in Claude Code's user settings: marketplace auto-update and the launcher rule."""
    path = _claude_settings_path()
    record_path = core.home().state / "host-settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return f"claude-code: settings not changed ({path} is not valid JSON)"
    if not isinstance(settings, dict):
        return f"claude-code: settings not changed ({path} is not an object)"
    record = core.read_json(record_path)
    if remove:
        if not record:
            return "claude-code: no settings to take back"
        allow = settings.get("permissions", {}).get("allow")
        if isinstance(allow, list):
            settings["permissions"]["allow"] = [r for r in allow if r not in record.get("rules", [])]
        market = settings.get("extraKnownMarketplaces", {}).get("theodore")
        if isinstance(market, dict):
            if record.get("created_marketplace"):
                settings["extraKnownMarketplaces"].pop("theodore", None)
            elif record.get("set_auto_update"):
                market.pop("autoUpdate", None)
        core.write_atomic(path, json.dumps(settings, indent=2) + "\n", 0o644)
        core.remove_tree(record_path)
        return "claude-code: settings taken back"
    rules = _permission_rules()
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    added = [r for r in rules if r not in allow]
    allow.extend(added)
    markets = settings.setdefault("extraKnownMarketplaces", {})
    created = "theodore" not in markets
    if created:
        markets["theodore"] = {"source": {"source": "url", "url": str(core.config().get("marketplace_url") or "")}}
    set_auto = markets["theodore"].get("autoUpdate") is not True
    markets["theodore"]["autoUpdate"] = True
    core.write_atomic(path, json.dumps(settings, indent=2) + "\n", 0o644)
    core.write_json(record_path, {"rules": sorted(set(record.get("rules", []) + added)), "created_marketplace": created or record.get("created_marketplace", False),
                                  "set_auto_update": set_auto or record.get("set_auto_update", False), "path": str(path)})
    return f"claude-code: automatic updates on and launcher allowed in {path}"


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
        _status, _headers, raw = api.request("GET", f"{base}/plugin/latest.json", timeout=60.0)
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
    archive = home.staging / archive_name
    try:
        api.download(f"{base}/plugin/{archive_name}", archive, sha256=sha, size=size, timeout=60.0)
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


def uninstall(keep_hosts: bool) -> int:
    home = core.home()
    if not keep_hosts:
        claude, codex = shutil.which("claude"), shutil.which("codex")
        if claude:
            _run([claude, "plugin", "uninstall", "theodore@theodore"])
            _run([claude, "plugin", "marketplace", "remove", "theodore"])
        if codex:
            _run([codex, "plugin", "remove", "theodore@theodore"])
            _run([codex, "plugin", "marketplace", "remove", "theodore"])
        _claude_settings(remove=True)
    for name in ("setup", "packs", "engine-update", "client-update", "libreoffice"):
        pid = int(Job(name).read().get("pid") or 0)
        if pid and pid != os.getpid() and Job(name).alive():
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    credentials.delete()
    for cache in _render_cache_dirs():
        core.remove_tree(cache)
    root = home.root
    # Only Theodore's own subfolders go. Anything else in the folder was not put there by Theodore and stays.
    if core.IS_WINDOWS:  # the running interpreter lives inside the folder: delete it after this process ends
        # One string, not a list: Python would escape the inner quotes as \" and cmd does not read that escape (a
        # path with a space was never removed). /s makes cmd strip exactly the outer pair of quotes.
        removals = " & ".join(f'rmdir /s /q "{root / name}"' for name in core.OWNED)
        line = f'ping -n 6 127.0.0.1 >nul & {removals} & rmdir "{root}"'
        subprocess.Popen(f'cmd /d /s /c "{line}"', creationflags=0x00000008 | 0x08000000,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        kept = [p.name for p in root.iterdir() if p.name not in core.OWNED] if root.exists() else []
    else:
        for name in core.OWNED:
            core.remove_tree(root / name)
        kept = sorted(p.name for p in root.iterdir()) if root.exists() else []
        if not kept:
            try:
                root.rmdir()
            except OSError:
                pass
    what = f"its folders in {root} were deleted; {len(kept)} other item(s) there were not Theodore's and were left alone" if kept else f"{root} was deleted"
    print(f"THEODORE OK: Theodore was removed from this computer ({what}; sign-in removed).\n"
          "NEXT: nothing to do. Deck workspaces keep a small .pptx-state/theodore-engine.json file; it is harmless.\n"
          "TELL THE USER: Theodore is uninstalled. Your decks and workspaces were not touched.", flush=True)
    return 0
