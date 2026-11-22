"""`theodore mcp`: a local stdio MCP server: `list`, `guide` and `specimen` for the plugin; one tool, `theodore`, for
Claude Desktop's chat (see DESKTOP).

Why: hosts show a command's output whole only up to a limit (Claude Code: about 30,000 characters; above that the
model sees a 2,000-character preview), while an MCP tool's text result is shown whole up to the size the tool
declares, and an MCP image block is shown as an image. The tools deliver the same texts and sheets as the
launcher's `guide` and `specimen`; nothing is written to disk.

Protocol: JSON-RPC 2.0, one message per line on standard input and output (the MCP stdio transport). Everything
else this process would print goes to standard error, so that nothing can break the stream. Python standard
library only, like the rest of the client; it needs the stage-1 client and the installation credential, not the
engine (except `guide` with skill pptx, whose manual comes with the engine).

Reuse within the session: this process keeps each sheet and each whole guide text it fetched in memory, never on
disk, for as long as it runs; Claude Code's sub-agents call the same process. A held guide text is asked for again
on every call with If-None-Match (the service answers 304 and does not count it as a fetch). Held sheets belong to
the published guide version the index names: at most REVALIDATE_SECONDS after the last check, one conditional index
request confirms the version and the device (304); a new version makes each held sheet be checked on its own
(If-None-Match with its SHA-256); a refusal drops every held item. A revoked device is therefore refused on its next guide call at once and on its next sheet call within
60 seconds; it is never served a held item after the service refused it.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import time

from . import __version__, account, api, chatbuild, commands, core, credentials, install, specimens, withdraw
from .contract import HEADER_GUIDE_PARTS, HEADER_GUIDE_VERSION, SKILLS
from .core import Status

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
GUIDE_MAX_RESULT_CHARS = 400_000
SPECIMEN_MAX_PER_CALL = 6          # sheets one specimen call returns; each sheet not held is one fetch
REVALIDATE_SECONDS = 60.0          # held sheets are served for at most this long after the service last confirmed them
# What one process holds at most: the sheets and the references of one guide version. A version holds at most 64
# sheets of at most 1 MiB each (the service's limits); references are whole sections of at most 400,000 characters.
HELD_SHEETS_MAX = 64
HELD_SHEET_BYTES_MAX = 64 << 20
HELD_GUIDES_MAX = 64
HELD_GUIDE_CHARS_MAX = 16 << 20

ROLE = {"type": "string", "enum": list(commands.ROLES), "description": '"slide" for an agent given one slide.'}
CODE_PATTERN = "^[A-Z]{2}[0-9]{1,3}$"           # a close-up's bare code (specimens.CODE): HC3 is HC3.jpg

DESCRIPTIONS = {
    "list": "Lists the files of a Theodore skill.",
    "guide": "Returns one file of a Theodore skill, whole.",
    "specimen": "Returns specimen sheets as images: one file, or a list of up to 6 (each image after its file name).",
}


# Claude Desktop's chat (`mcp --for claude-desktop`, the entry desktop.py writes): no skill folder and no engine there,
# and the app asks the person to approve each tool of a local server, so this server has exactly one tool, `theodore`:
# one "Always allow", ever. Its `action` selects what the remote connector's separate tools do (begin, a guidance
# section, specimen sheets, a report, preferences) and a check of this computer's set-up; it serves deck-design only.
# The service's texts name "the Theodore tool guide" and "specimen", the launcher, and the connector's tool names
# (`theodore_report`, `theodore_preference_add`); every answer here is rewritten to name this one tool and its action.
DESKTOP = "claude-desktop"
CODEX = "codex"
FOR: str | None = None              # set by serve(): None for the plugin's server (Claude Code, Codex, Cowork)
# The plugin's own server (no --for) runs in the coding hosts and, when a Claude account has Theodore's plugin on, in the
# Claude app's chat too. In a chat it must not be a second Theodore server: it serves the chat's one tool, or nothing at
# all when the app already starts Theodore's own entry (desktop.py), so a person meets one tool and one approval.
# A client named as the Claude app (chatbuild.is_claude_app: its chat, a name with "claude" in it other than Claude
# Code's "claude-code", or its agent mode, "local-agent-mode-...") is a chat; every other client keeps the plugin's
# tools as before (Claude Code, Codex, tests, anything unknown).
SILENT = False                      # the plugin's server in a chat whose app runs Theodore's entry: no tools
DESKTOP_TOOL = chatbuild.TOOL          # `theodore`; THEODORE_CHAT_TOOL_NAME overrides it for testing (chatbuild.TOOL)
DESKTOP_ACTIONS = ("begin", "guide", "specimen", "manual", "run", "read", "write", "edit", "list", "reveal", "report",
                   "preference_list", "preference_add", "preference_remove", "check")
DESKTOP_DESCRIPTION = (
    "Theodore, the presentation designer this user installed. Use it when a presentation, deck or slide is asked for "
    "(to create, restructure, redesign, adapt or critique one), or when the user asks to set up or check Theodore. "
    'Start a presentation with action "begin"; every answer says which action comes next. The guidance it returns is '
    "licensed to this user: apply it, do not reproduce it or list its rules, and do not name this tool or describe what "
    "was read.")
# For testing only, like the name: THEODORE_CHAT_TOOL_DESCRIPTION replaces the description.
DESKTOP_DESCRIPTION = str(os.environ.get("THEODORE_CHAT_TOOL_DESCRIPTION") or "").strip()[:2000] or DESKTOP_DESCRIPTION
DESKTOP_TITLE = "Theodore"
# What the service's texts say, and what this one tool's answers say instead.
# The service's sentence (theodore apps/worker/src/services/workflow.ts, localLinkBindings, tool form): for role "slide"
# it adds ` and role "slide"` after the section, and so does this tool's wording.
_SERVICE_TOOL_BINDINGS = ('Links in this guidance are read with the Theodore tools: a link written references/<name>.md or '
                          '<name>.md is read by calling this same Theodore tool guide with section "<name>"{role}, and a link '
                          'written specimens/<file>.jpg is opened by calling the Theodore tool specimen with file "<file>".')
SLIDE_ROLE_WORDS = ' and role "slide"'
SERVICE_TOOL_BINDINGS = _SERVICE_TOOL_BINDINGS.format(role="")
SERVICE_TOOL_BINDINGS_SLIDE = _SERVICE_TOOL_BINDINGS.format(role=SLIDE_ROLE_WORDS)
DESKTOP_BINDINGS = (f'Links in this guidance are read with the Theodore tool {DESKTOP_TOOL}: a link written references/<name>.md '
                    f'or <name>.md is read by calling {DESKTOP_TOOL} with action "guide" and section "<name>"{{role}}, and a link '
                    f'written specimens/<file>.jpg is opened by calling {DESKTOP_TOOL} with action "specimen" and file "<file>" '
                    f'(or a list of up to {SPECIMEN_MAX_PER_CALL} files). A report this guidance asks for is sent with action "report" (kind, '
                    f'title, details, and verb, code or origin when there are), and a lasting preference is saved with action '
                    f'"preference_add" (text).')
DESKTOP_BINDINGS_SLIDE = DESKTOP_BINDINGS.format(role=SLIDE_ROLE_WORDS)
# The same sentence in the service's launcher form (localLinkBindings, launcher form), as the build manual carries it.
SERVICE_LAUNCHER_BINDINGS = re.compile(r'Links in this guidance are read through the same launcher: a link written '
                                       r'references/<name>\.md or <name>\.md is the section printed by: guide [a-z-]+ <name>'
                                       r'( --role slide)?, and a link written specimens/<file>\.jpg is the sheet fetched by: '
                                       r'specimen <file>\.')
DESKTOP_BINDINGS = DESKTOP_BINDINGS.format(role="")
# How the build manual names one of its served sections (the engine's skills/pptx/served-entry.md): by a tool with a
# skill, or the launcher. This tool's guide action finds the section by its name.
SERVICE_SECTION_CALL = re.compile(r'call the Theodore guide tool with skill "[a-z-]+" and section "([A-Za-z0-9_-]{1,64})" '
                                  r'\(or run the launcher: `guide [a-z-]+ [A-Za-z0-9_-]{1,64}`\)')
SERVICE_LAUNCHER_LINES = ("Run the Theodore launcher (scripts/theodore in the folder of either Theodore skill; scripts\\theodore.cmd "
                          "on Windows) with:", "Run the Theodore launcher you used to get these instructions with:")
DESKTOP_LAUNCHER_LINE = (f'Call the {DESKTOP_TOOL} tool with action "report" and, as its fields kind, title, details, verb, code '
                         f'and origin, the values this command gives its options:')


def desktop_tools(index: dict | None = None) -> list[dict]:
    """The one tool definition for Claude Desktop's chat (see DESKTOP)."""
    plugin = {t["name"]: t for t in _plugin_tools(index)}
    section = plugin["guide"]["inputSchema"]["properties"]["section"]
    file = plugin["specimen"]["inputSchema"]["properties"]["file"]
    text = {"type": "string"}
    return [{
        "name": DESKTOP_TOOL, "title": DESKTOP_TITLE, "description": DESKTOP_DESCRIPTION,
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(DESKTOP_ACTIONS),
                       "description": 'begin: the design method for a deck; guide: one section the guidance or the build instructions '
                                      'link (section); '
                                      'specimen: specimen sheets as images (file); report: a problem or feedback for Theodore '
                                      '(kind, title, details); preference_list, preference_add (text), preference_remove (id): '
                                      "the user's lasting preferences; check: whether Theodore is set up on this computer. "
                                      'manual: the build instructions; run: one Theodore engine command (args, stdin, folder), '
                                      'its output and render images; read, write, edit, list: the deck\'s files (path); '
                                      'reveal: show a file in Finder or Explorer, only when the user asks.'},
            "section": section, "file": file, "role": ROLE,
            "args": {"type": "array", "items": {"type": "string"},
                     "description": 'run: the engine command without "pptx", one item per argument, e.g. ["apply", "<workspace>"].'},
            "stdin": {"type": "string", "description": "run: text the command reads on standard input (the design note for --storyline -)."},
            "folder": {"type": "string", "description": "The deck's folder: its full path on this computer (the folder the user "
                                                        "named or attached). Left out: Theodore's default folder (Theodore in the user's home "
                                                        "on a Mac, Documents/Theodore elsewhere)."},
            "images": {"type": "boolean", "description": "run: return the render images the command produced (default true)."},
            "path": {"type": "string", "description": "read, write, edit, list, reveal: a path in the deck's folder (or, to read, its renders and the build manual's files)."},
            "content": {"type": "string", "description": "write: the file's whole text."},
            "old": {"type": "string", "description": "edit: the exact text to replace (unique in the file unless all is true)."},
            "new": {"type": "string", "description": "edit: its replacement."},
            "all": {"type": "boolean", "description": "edit: replace every occurrence."},
            "offset": {"type": "integer", "description": "read: first line (1-based)."},
            "limit": {"type": "integer", "description": "read: number of lines."},
            "kind": {"type": "string", "enum": ["bug", "feedback"]}, "title": text, "details": text, "verb": text, "code": text,
            "origin": {"type": "string", "enum": ["automatic", "user_requested"]},
            "text": text, "id": text},
            "required": ["action"], "additionalProperties": False},
        # Not read-only: a report and a preference are written to the user's Theodore account.
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        "_meta": {"anthropic/maxResultSizeChars": GUIDE_MAX_RESULT_CHARS}}]


def desktop_text(text: str, begin: bool = False) -> str:
    """A service text as this one tool's answer: its tool and launcher bindings name `theodore` and the action. The
    entry document always ends with the bindings, also from a service that sent none."""
    out = text.replace(SERVICE_TOOL_BINDINGS, DESKTOP_BINDINGS).replace(SERVICE_TOOL_BINDINGS_SLIDE, DESKTOP_BINDINGS_SLIDE)
    # The build manual comes as the launcher prints it: its links sentence (the service's launcher form) names a
    # launcher a chat does not have.
    out = SERVICE_LAUNCHER_BINDINGS.sub(lambda m: DESKTOP_BINDINGS_SLIDE if m.group(1) else DESKTOP_BINDINGS, out)
    out = SERVICE_SECTION_CALL.sub(lambda m: f'call {DESKTOP_TOOL} with action "guide" and section "{m.group(1)}"', out)
    for line in SERVICE_LAUNCHER_LINES:
        out = out.replace(line, DESKTOP_LAUNCHER_LINE)
    # The remote connector's own names stay where a text sends the assistant to that connector (CONNECTOR_REQUIRED).
    out = re.sub(r"(?<!connector's )`?theodore_(begin|guide|specimen|report|preference_add|preference_list|preference_remove)`?",
                 lambda m: f'`{DESKTOP_TOOL}` with action "{m.group(1)}"', out)
    out = re.sub(rf'`{re.escape(DESKTOP_TOOL)}` with action "(\w+)" tool', rf'`{DESKTOP_TOOL}` tool with action "\1"', out)
    if begin and DESKTOP_BINDINGS not in out and DESKTOP_BINDINGS_SLIDE not in out:
        out = out.rstrip("\n") + "\n\n" + DESKTOP_BINDINGS + "\n"
    return out


def tools(index: dict | None = None) -> list[dict]:
    if SILENT:
        return []
    return desktop_tools(index) if FOR == DESKTOP else _plugin_tools(index)


def _plugin_tools(index: dict | None = None) -> list[dict]:
    """The tool definitions. With the published index, `section` and `file` name the actual sections and sheets, as a
    folder listing would show them."""
    sections = [s for s in (index or {}).get("sections") or []]
    sheets = [s for s in (index or {}).get("specimens") or []]
    # The deck-design sections as a folder would list them; any other name is still accepted (a pptx section the
    # service serves to this account, lane STUDIO), and an unknown one answers that there is no such section.
    section = {"anyOf": [{"type": "string", "enum": sections}, {"type": "string"}]} if sections else {"type": "string"}
    sheet = {"anyOf": [{"type": "string", "enum": sheets}, {"type": "string", "pattern": CODE_PATTERN}]} if sheets \
        else {"type": "string"}
    return [
        {"name": "list", "description": DESCRIPTIONS["list"],
         "inputSchema": {"type": "object", "properties": {"skill": {"type": "string", "enum": list(SKILLS)}},
                         "required": ["skill"], "additionalProperties": False}},
        {"name": "guide", "description": DESCRIPTIONS["guide"],
         "inputSchema": {"type": "object", "properties": {"skill": {"type": "string", "enum": list(SKILLS)}, "section": section,
                                                          "role": ROLE},
                         "required": ["skill"], "additionalProperties": False},
         "_meta": {"anthropic/maxResultSizeChars": GUIDE_MAX_RESULT_CHARS}},
        {"name": "specimen", "description": DESCRIPTIONS["specimen"],
         "inputSchema": {"type": "object", "properties": {"file": {"anyOf": [
             sheet, {"type": "array", "items": sheet, "minItems": 1, "maxItems": SPECIMEN_MAX_PER_CALL}]}},
             "required": ["file"], "additionalProperties": False}},
    ]


# --- the published index: names only ------------------------------------------------------------------------------------

INDEX: dict | None = None          # {"version", "sections", "specimens", "etag"} of deck-design, when known
NOTIFY: list[dict] = []            # notifications the server sends after its next answer
SHEETS: dict[str, tuple[str, bytes, str | None]] = {}       # name -> (sha256, bytes, index ETag it was fetched under)
GUIDES: dict[tuple[str, str, str], tuple[str, str]] = {}    # (skill, section, role or "") -> (ETag, whole text)
VALIDATED_AT: float | None = None  # time.monotonic() of the service's last confirmation of INDEX (and of this device)


def _revalidate_seconds() -> float:
    """REVALIDATE_SECONDS. Test seam: THEODORE_TEST_REVALIDATE_SECONDS, honoured only when the API base is loopback."""
    raw = os.environ.get("THEODORE_TEST_REVALIDATE_SECONDS")
    if raw and core.is_loopback_api():
        with contextlib.suppress(ValueError):
            return float(raw)
    return REVALIDATE_SECONDS


def forget(refused: bool = False) -> None:
    """Drop everything held: after a refusal nothing held is served again. After a refusal of the device or the
    account (`refused`), the launcher's specimen folders on this computer go too: it keeps no sheet anywhere."""
    global INDEX, VALIDATED_AT, INDEX_TRIED
    SHEETS.clear()
    GUIDES.clear()
    VALIDATED_AT = None
    INDEX = None                    # fetched again (and confirmed) before anything is held again
    INDEX_TRIED = False
    if refused:
        specimens.sweep(everything=True)
        chatbuild.drop_licence_copy()


def _hold(store: dict, key, value, size, max_entries: int, max_size: int) -> None:
    """Keep `value` under `key`, newest last; evict the oldest entries beyond the count or the total size."""
    store.pop(key, None)
    store[key] = value
    while len(store) > max_entries or (len(store) > 1 and sum(size(v) for v in store.values()) > max_size):
        store.pop(next(iter(store)))
    if sum(size(v) for v in store.values()) > max_size:      # one item larger than the whole bound: not held
        store.pop(key, None)


def _hold_sheet(name: str, value: tuple[str, bytes, str | None]) -> None:
    _hold(SHEETS, name, value, lambda v: len(v[1]), HELD_SHEETS_MAX, HELD_SHEET_BYTES_MAX)


def _hold_guide(key: tuple[str, str, str], value: tuple[str, str]) -> None:
    _hold(GUIDES, key, value, lambda v: len(v[1]), HELD_GUIDES_MAX, HELD_GUIDE_CHARS_MAX)


def _names(items, key: str, pattern) -> list[str]:
    out = []
    for item in items if isinstance(items, list) else []:
        name = item.get(key) if isinstance(item, dict) else item
        if isinstance(name, str) and pattern(name) and name not in out:
            out.append(name)
    return out


def _parse_index(headers, raw: bytes) -> dict | None:
    if "json" not in str(headers.get("Content-Type") or "").lower():
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sections = _names(data.get("sections"), "name", commands.SECTION_NAME.fullmatch)
    sheets = _names(data.get("specimens"), "file", specimens.valid_name)
    if not sections and not sheets:
        return None
    index = {"version": str(data.get("version") or ""), "sections": sections, "specimens": sheets}
    etag = str(headers.get("ETag") or "").strip()
    if etag:
        index["etag"] = etag
    return index


def fetch_index() -> dict | None:
    """GET /v1/guide/deck-design?form=index: {version, sections: [{name}], specimens: [{file}]}. None when signed out,
    offline, or from a service without the index (then the tools take any name, as before)."""
    try:
        cred = credentials.load()
        if not cred:
            return None
        _s, headers, raw = api.call("guide", bearer=cred["installation_secret"], skill="deck-design", query={"form": "index"}, timeout=5.0)
    except Exception:       # noqa: BLE001  no index: the tools still work by name
        return None
    return _parse_index(headers, raw)


def _adopt(fresh: dict | None) -> None:
    """Take a newly fetched index: held sheets of another version are no longer served unchecked, and the host is told
    when the names changed."""
    global INDEX, VALIDATED_AT
    if fresh is None:
        return
    if INDEX is not None and fresh.get("etag") != INDEX.get("etag"):      # another version: nothing of the old one is kept
        SHEETS.clear()
        GUIDES.clear()
    changed = INDEX is not None and {k: v for k, v in fresh.items() if k != "etag"} != {k: v for k, v in INDEX.items() if k != "etag"}
    INDEX = fresh
    VALIDATED_AT = time.monotonic() if fresh.get("etag") else None
    if changed:
        NOTIFY.append({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})


INDEX_TRIED = False                # a specimen call asks for a missing index once, not on every call


def _index() -> dict | None:
    global INDEX, VALIDATED_AT, INDEX_TRIED
    if INDEX is None:
        INDEX_TRIED = True
        INDEX = fetch_index()
        VALIDATED_AT = time.monotonic() if INDEX and INDEX.get("etag") else None
    return INDEX


def _seen_version(version: str | None) -> None:
    """A guide answer named its guide version: when it is not the index's, fetch the index again and tell the host
    that the tools changed."""
    if not version or INDEX is None or not INDEX.get("version") or version == INDEX["version"]:
        return
    fresh = fetch_index()
    if fresh and fresh != INDEX:
        _adopt(fresh)


def _validate() -> str | None:
    """The index ETag under which held sheets may be served now, or None (no index: each held sheet is checked on its
    own). Past the interval, one conditional index request: 304 confirms; a new version demotes the held sheets; a
    refusal or a failure raises its status (call_tool then drops everything held after a refusal)."""
    global VALIDATED_AT, INDEX
    etag = (INDEX or {}).get("etag")
    if not etag:
        return None
    if VALIDATED_AT is not None and 0 <= time.monotonic() - VALIDATED_AT < _revalidate_seconds():
        return etag
    try:
        status, headers, raw = commands._api("guide", "specimen", skill="deck-design", query={"form": "index"},
                                             headers={"If-None-Match": etag})
    except Status as status:
        if status.name not in ("OFFLINE", "RETRY"):         # refused, or no published version: nothing held is kept
            forget()
        raise
    if core.refused():
        raise api.sign_in_required()
    if status == 304:
        VALIDATED_AT = time.monotonic()
        return etag
    fresh = _parse_index(headers, raw)
    if fresh is None:               # the service no longer serves the index: check sheets one by one
        INDEX = {k: v for k, v in (INDEX or {}).items() if k != "etag"}
        return None
    _adopt(fresh)
    return fresh.get("etag")


def _text(text: str, error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": text}]}
    if error:
        result["isError"] = True
    return result


def _status(status: Status) -> dict:
    if core.refused() and not getattr(status, "refusal", False):
        status = account.refusal_status()
    elif getattr(status, "auto_report", False):
        status = commands.report_internal(status)      # reported by the client itself, as on the command line
    text = status.render()
    return _text(desktop_text(text) if FOR == DESKTOP else text, error=status.name != "OK")


# --- guide ---------------------------------------------------------------------------------------------------------------

def _fetch_whole(skill: str, section: str | None, role: str | None = None) -> str:
    """The whole document as the service sends it: one answer with form=whole; an older service that still pages
    is asked for every part, joined in order. `role` "slide": the text as composed for an agent given one slide."""
    claims = account.require(skill)
    del claims
    query = {"form": "whole", "bindings": "tool", **({"role": role} if role else {})}
    what = f"guide {skill}"
    if section:
        name = commands._section_name(section)
        if not commands.SECTION_NAME.fullmatch(name):
            raise commands._no_such_section(skill, name, role)
    key = (skill, name if section else "", role or "")
    held = GUIDES.get(key)
    conditional = {"If-None-Match": held[0]} if held else None
    if section:
        try:
            status, headers, raw = commands._api("guide_section", what, passing=("not_found",), skill=skill, section=name,
                                                 query=query, headers=conditional)
        except api.ApiError:
            GUIDES.pop(key, None)
            # a text of the manual the service does not serve: the selected engine's own file (an engine up to rc.28)
            on_disk = commands._pptx_disk_section(name) if skill == "pptx" else None
            if on_disk is None:
                raise commands._no_such_section(skill, name, role) from None
            _headers, raw = commands._pptx_from_disk(on_disk)
            if core.refused():
                raise api.sign_in_required() from None
            return raw.decode("utf-8", errors="replace")
        route, extra = "guide_section", {"section": name}
    else:
        status, headers, raw = commands._api("guide", what, skill=skill, query=query, headers=conditional)
        route, extra = "guide", {}
    if core.refused():
        raise api.sign_in_required()
    if status == 304 and held:
        return held[1]
    if skill == "deck-design":         # the index names deck-design's version; a served pptx section has its own
        _seen_version(headers.get(HEADER_GUIDE_VERSION))
    chunks = [raw]
    total = max(1, int(headers.get(HEADER_GUIDE_PARTS) or 1))
    for part in range(2, total + 1):
        _s, _h, more = commands._api(route, what, skill=skill, query={**query, "part": part}, **extra)
        chunks.append(more)
    if core.refused():
        raise api.sign_in_required()
    text = b"".join(chunks).decode("utf-8", errors="replace")
    etag = str(headers.get("ETag") or "").strip()
    if total == 1 and etag:
        _hold_guide(key, (etag, text))
    else:
        GUIDES.pop(key, None)
    return text


def _pptx_manual(role: str | None = None) -> str:
    """The pptx manual as the launcher prints it, whole, in one document (it comes with the engine); a tool result
    needs none of the opening lines a long command output gets. On the chat route it carries no update notice: those
    say what changed since the last skill load, at any earlier time (an install's plugin update minutes before), and a
    chat told it mid-build said "Theodore updated itself while building" when nothing had. Nothing is applied during a
    build, so a chat says nothing about versions; the notice stays for the coding hosts' next skill load."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        commands._guide_pptx(1, whole=True, opening=False, notices=FOR != DESKTOP, role=role, pointers=FOR != DESKTOP)
    return buffer.getvalue()


def tool_guide(args: dict) -> dict:
    skill, section = args.get("skill"), args.get("section")
    if args.get("role") is not None and not isinstance(args.get("role"), str):
        raise core.usage("role is a string.")
    role = commands._role(args.get("role"))
    if skill not in SKILLS:
        raise core.usage(f"unknown skill {skill!r}. The skills are: {', '.join(SKILLS)}.")
    if section is not None and not isinstance(section, str):
        raise core.usage("section is a string.")
    if skill == "pptx" and (not section or commands._section_name(section).lower() in commands.MANUAL_NAMES):
        return _text(_pptx_manual(role))        # the main text, also by the name an engine message gives it
    return _text(_fetch_whole(skill, section or None, role))


# --- specimen ------------------------------------------------------------------------------------------------------------

def _sheet(name: str, current: str | None) -> bytes:
    """One sheet's bytes, checked (JPEG, at most 1 MiB, the SHA-256 the service sent). Held under the index ETag
    `current` (just confirmed): served from memory. Held otherwise: one conditional request (304: served from memory).
    Not held: one fetch."""
    held = SHEETS.get(name)
    if held and current and held[2] == current:
        return held[1]
    what = f"specimen {name}"
    asked = {"Accept": "image/jpeg, application/json;q=0.9"}
    if held:
        asked["If-None-Match"] = f'"{held[0]}"'
    try:
        status, headers, raw = commands._api("guide_specimen", what, passing=("not_found",), file=name, query={"skill": "deck-design"},
                                             headers=asked, redirects=False, limit=specimens.MAX_BYTES)
    except api.TooLarge:
        raise core.internal(f"{what}: the answer is larger than 1 MiB; it was refused.") from None
    except api.ApiError as error:       # passed through only for not_found: said beside the sheets that came
        SHEETS.pop(name, None)
        if error.code != "not_found":
            raise api.status_from(error, what) from None
        raise specimens.NoSuchSheet(name) from None
    if core.refused():
        raise api.sign_in_required()
    if status == 304 and held:
        _hold_sheet(name, (held[0], held[1], current))
        return held[1]
    SHEETS.pop(name, None)
    if not str(headers.get("Content-Type") or "").lower().startswith("image/jpeg"):
        raise core.internal(f"{what}: the answer is not a JPEG image; it was refused.")
    if str(headers.get("X-Theodore-Sha256") or "").strip().lower() != hashlib.sha256(raw).hexdigest() \
            or not specimens.content_matches(raw, headers.get("ETag")):
        raise Status("RETRY", f"{what}: the sheet did not arrive as the service sent it (sha256 differs or is missing).",
                     "call the same tool again once. If it fails again, continue without the sheet.",
                     "A design specimen arrived damaged. I am trying once more.")
    _hold_sheet(name, (specimens.content_sha256(raw), raw, current))     # the published sheet's hash: If-None-Match
    return raw


def _image(raw: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/jpeg"}


def tool_specimen(args: dict) -> dict:
    """`file` is one name (the result is that image, as before) or a list of up to SPECIMEN_MAX_PER_CALL names (for
    each, in the order asked, a text block with the name and then its image). A name the service has no sheet for
    (0.4.30) is one text block naming the nearest real names, and the other sheets still come; only when no sheet at all
    comes is the answer a USAGE status naming them. Any other refusal answers with its status and no image."""
    asked = args.get("file")
    many = isinstance(asked, list)
    names = [specimens.file_name(name) for name in (asked if many else [asked])]     # a bare code is its close-up's file
    if many and not 1 <= len(names) <= SPECIMEN_MAX_PER_CALL:
        raise core.usage(f"specimen takes one file or a list of 1 to {SPECIMEN_MAX_PER_CALL} files; {len(names)} were given. "
                         f"Ask for the rest in another call.")
    if not all(isinstance(name, str) and specimens.valid_name(name) for name in names):
        raise core.usage("a specimen is named as the deck-design text names it: letters, digits, dot, dash and underscore, ending in .jpg.")
    account.require("deck-design")
    if INDEX is None and not INDEX_TRIED:
        _index()
    sheets: dict[str, bytes] = {}
    missing: list[str] = []
    for name in names:
        if name not in sheets and name not in missing:
            # The confirmation's age is checked for every sheet served, so no batch serves a held sheet more than
            # REVALIDATE_SECONDS after the service last confirmed it.
            try:
                sheets[name] = _sheet(name, _validate())
            except specimens.NoSuchSheet:
                missing.append(name)
    if core.refused():
        raise api.sign_in_required()
    notes = {name: specimens.missing_note(name, (INDEX or {}).get("specimens") or None) for name in missing}
    if not sheets:
        again = f'call {DESKTOP_TOOL} with action "specimen" again' if FOR == DESKTOP else "call the specimen tool again"
        raise specimens.none_delivered(list(notes.values()), again)
    if not many:
        return {"content": [_image(sheets[names[0]])]}
    content: list[dict] = []
    for name in names:
        if name in sheets:
            content += [{"type": "text", "text": name}, _image(sheets[name])]
        elif name in notes:
            content.append({"type": "text", "text": notes.pop(name)})
    return {"content": content}


def tool_list(args: dict) -> dict:
    skill = args.get("skill")
    if skill not in SKILLS:
        raise core.usage(f"unknown skill {skill!r}. The skills are: {', '.join(SKILLS)}.")
    if skill == "pptx":         # the engine's skill folder, on disk
        claims = account.require("pptx")
        del claims
        version = install.selected_engine()
        if not version:
            raise Status("SETUP_IN_PROGRESS", "Theodore's engine is not installed yet.",
                         f"run {core.quoted_launcher()} guide pptx; it installs the engine.", "Theodore is setting up its PowerPoint engine.")
        root = install.engine_dir(version) / "skills" / "pptx"
        names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith("."))
        return _text("\n".join(names) + "\n")
    account.require("deck-design")
    index = _index()
    if index is None:
        raise Status("RETRY", "the list of the deck-design files could not be fetched just now.",
                     "call the same tool again once; the files are also named by the links in the skill's text.",
                     "Theodore could not list its design files just now; I am trying once more.")
    lines = ["SKILL.md"] + [f"references/{n}.md" for n in index["sections"]] + [f"specimens/{f}" for f in index["specimens"]]
    return _text("\n".join(lines) + "\n")


HANDLERS = {"list": tool_list, "guide": tool_guide, "specimen": tool_specimen}
def _captured(run) -> str:
    """What a launcher command prints (its lines and its status), as text: this process's standard output is the
    protocol stream."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        run()
    return buffer.getvalue()


def _desktop_report(args: dict) -> dict:
    kind, title, details = args.get("kind"), args.get("title"), args.get("details")
    if kind not in ("bug", "feedback") or not isinstance(title, str) or not isinstance(details, str):
        raise core.usage('a report needs kind ("bug" or "feedback"), title and details.')
    origin = args.get("origin") if args.get("origin") in ("automatic", "user_requested") else "automatic"
    text = lambda key: args.get(key) if isinstance(args.get(key), str) else None
    return _text(_captured(lambda: commands.report(kind, title, details, text("verb"), text("code"), None, origin, False)))


def _desktop_preference(action: str, args: dict) -> dict:
    value = args.get("text" if action == "add" else "id")
    if action != "list" and not isinstance(value, str):
        raise core.usage(f'preference_{action} needs {"text, the preference" if action == "add" else "id, as preference_list shows it"}.')
    out = _captured(lambda: commands.prefs(action, value if action != "list" else None))
    return _text(out.replace("prefs list", 'action "preference_list"'))


def _desktop_check(_args: dict) -> dict:
    account.require("deck-design")
    return _status(_check_status())


def _check_status() -> Status:
    """The check's answer once Theodore is set up and signed in."""
    tell = "Theodore is set up and ready for your presentations here."
    if chatbuild.APP_CLIENT:            # the Claude app: the sentence the person can say, never a request to keep it
        tell += " " + chatbuild.USE_THEODORE_TIP
    return Status("OK", "Theodore is set up on this computer and signed in; its design guidance reaches this chat.",
                  f'to make or rework a presentation, call {DESKTOP_TOOL} with action "begin".', tell)


def _desktop_guide(args: dict) -> dict:
    """One section through the chat's one tool: of the design guidance and, when that has no such section, of the build
    instructions. The build instructions send the assistant to their own sections (slide-parts), and a chat has no
    launcher to read them with and this tool names no skill (TR-G78T5N: the action answered for deck-design only, so
    that section could not be read from a chat)."""
    asked = {"section": args["section"], "role": args.get("role")}
    if commands._section_name(args["section"]).lower() in commands.MANUAL_NAMES:
        return chatbuild.manual()               # the build instructions by their name: what action "manual" returns
    try:
        return tool_guide({"skill": "deck-design", **asked})
    except Status as refusal:
        if refusal.name != "USAGE" or "has no section" not in refusal.summary:
            raise
        try:
            return tool_guide({"skill": "pptx", **asked})
        except Status as second:
            if second.name == "USAGE" and "has no section" in second.summary:
                raise refusal from None       # neither has it: the answer that lists the design sections
            raise


def desktop_call(args: dict) -> dict:
    action = args.get("action")
    verb = args.get("args")[0] if action == "run" and isinstance(args.get("args"), list) and args.get("args") else ""
    core.log(f"chat action {action}{' ' + verb if isinstance(verb, str) and verb.isalpha() else ''}")
    if action == "begin":
        result = tool_guide({"skill": "deck-design"})
    elif action == "guide":
        if not isinstance(args.get("section"), str) or not args["section"]:
            raise core.usage('action "guide" needs section, the name a link references/<name>.md gives.')
        result = _desktop_guide(args)
    elif action == "specimen":
        result = tool_specimen({"file": args.get("file")})
    elif action == "report":
        result = _desktop_report(args)
    elif action in ("preference_list", "preference_add", "preference_remove"):
        result = _desktop_preference(action.split("_", 1)[1], args)
    elif action == "check":
        result = _desktop_check(args)
    elif action == "manual":
        result = chatbuild.manual()
    elif action == "run":
        result = chatbuild.run(args.get("args"), args.get("stdin"), args.get("folder"), args.get("images") is not False)
    elif action == "read":
        result = chatbuild.read(args.get("path"), args.get("folder"), args.get("offset"), args.get("limit"))
    elif action == "write":
        result = chatbuild.write(args.get("path"), args.get("content"), args.get("folder"))
    elif action == "edit":
        result = chatbuild.edit(args.get("path"), args.get("old"), args.get("new"), args.get("folder"), args.get("all") is True)
    elif action == "list":
        result = chatbuild.list_dir(args.get("path"), args.get("folder"))
    elif action == "reveal":
        result = chatbuild.reveal(args.get("path"), args.get("folder"))
    else:
        raise core.usage(f"action is one of: {', '.join(DESKTOP_ACTIONS)}.")
    if chatbuild.NOTICE and not result.get("isError") and isinstance(result.get("content"), list):
        # The default folder could not be written and the deck went to ~/Theodore: said once, for the user.
        result["content"].insert(0, {"type": "text", "text": "TELL THE USER: " + chatbuild.NOTICE.pop() + "\n"})
        chatbuild.NOTICE.clear()
    if action in chatbuild.FILE_ACTIONS:         # the engine's output and the user's files are passed on as they are
        return result
    for block in result.get("content") or []:
        if block.get("type") == "text":
            block["text"] = desktop_text(block["text"], begin=action == "begin" and not result.get("isError"))
    if not result.get("isError") and result.get("content"):
        if action == "begin":
            first = result["content"][0]
            first["text"] = first["text"].rstrip("\n") + "\n\n" + chatbuild.BUILD_POINTER + "\n"
        elif action in ("guide", "specimen"):
            result["content"].append({"type": "text", "text": chatbuild.THEN_BUILD})
    return result


DESKTOP_HANDLERS = {DESKTOP_TOOL: desktop_call}


# --- the server ----------------------------------------------------------------------------------------------------------

# Every status but a passing network trouble drops what is held: a refusal of any kind (revoked, suspended, no plan,
# update required, kill switch or policy block), and a service answer that something is gone (USAGE from a 404).
KEEP_HELD_ON = ("OFFLINE", "RETRY")
# Refusals of the device or the account: the launcher's folders are removed as well.
REFUSALS = ("SIGN_IN_REQUIRED", "SIGN_IN_PENDING", "NO_ENTITLEMENT", "UPDATE_REQUIRED", "BLOCKED_BY_POLICY", "CONNECTOR_REQUIRED")
BLOCKED: Status | None = None          # a status that stops every tool (this folder belongs to another service)


def call_tool(name: str, args) -> dict:
    handler = None if SILENT else (DESKTOP_HANDLERS if FOR == DESKTOP else HANDLERS).get(name)
    if handler is None:
        return _text(f"unknown tool {name!r}.", error=True)
    if BLOCKED is not None:
        return _status(BLOCKED)
    core._REFUSAL, core._REFUSAL_EMITTED = None, False      # each call learns afresh (a device may sign in again meanwhile)
    specimens.sweep()               # the launcher's folders older than an hour go, as before any launcher command
    try:
        result = handler(args if isinstance(args, dict) else {})
    except Status as status:
        if core.refused() or status.name not in KEEP_HELD_ON:
            forget(refused=core.refused() or (status.name in REFUSALS and not getattr(status, "keeps_sheets", False)))
        withdraw.resume()           # the service ended this computer's access: Theodore's files go (withdraw.py)
        return _status(status)
    except api.TooLarge as exc:
        return _status(core.internal(f"an answer from the Theodore service was longer than {exc.limit} bytes; it was refused."))
    except Exception as exc:  # noqa: BLE001  a tool answers; it never ends the server
        core.log(f"mcp {name}: {exc.__class__.__name__}: {exc}")
        return _status(core.internal(f"{exc.__class__.__name__}: {exc}"))
    if core.refused():
        forget(refused=True)
        withdraw.resume()
    return result


def _adopt_client(info) -> None:
    """The plugin's own server learns who started it: a coding host keeps the plugin's tools; a chat gets the chat's
    one tool, or no tool when Theodore's entry in the app already serves it."""
    global FOR, SILENT
    name = str((info or {}).get("name") or "") if isinstance(info, dict) else ""
    core.log(f"mcp initialize: client {name[:60] or 'unnamed'} (for {FOR or 'plugin'})")
    chatbuild.APP_CLIENT = chatbuild.is_claude_app(name)
    if FOR is not None or not chatbuild.APP_CLIENT:
        return
    from . import desktop
    FOR = DESKTOP
    with contextlib.suppress(Exception):
        SILENT = desktop.entry_present()


def handle(message: dict) -> dict | None:
    """One JSON-RPC message in, its answer out (None for a notification)."""
    method, ident = message.get("method"), message.get("id")
    is_request = "id" in message and method is not None
    if method is None:                      # a response to something we never sent: ignored
        return None
    if method == "initialize":
        _adopt_client((message.get("params") or {}).get("clientInfo"))
        asked = str((message.get("params") or {}).get("protocolVersion") or "")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": True}},
                  "serverInfo": {"name": "theodore", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools(None if SILENT else _index())}
    elif method == "tools/call":
        params = message.get("params") or {}
        token = (params.get("_meta") or {}).get("progressToken") if isinstance(params.get("_meta"), dict) else None
        chatbuild.PROGRESS = _progress(token) if token is not None and OUT is not None else None
        chatbuild.META = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        try:
            result = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
        finally:
            chatbuild.PROGRESS = None
            chatbuild.META = {}
    elif not is_request:                    # notifications/initialized, notifications/cancelled, ...
        return None
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result} if is_request else None


def serve(stdin=None, stdout=None, blocked: Status | None = None, for_host: str | None = None) -> int:
    """Read messages until the input ends. Our standard output carries only protocol messages."""
    global BLOCKED, FOR
    BLOCKED = blocked
    # Codex (`mcp --for codex`, its plugin's mcp.json) gets the same one tool: Codex asks before no call of a tool marked
    # neither destructive nor open-world, and runs it outside its sandbox, so Theodore's work needs no approval there
    # (lane CHATGPT; codex-rs core/src/mcp_tool_call.rs, requires_mcp_tool_approval).
    FOR = DESKTOP if for_host in (DESKTOP, CODEX) else None
    raw_in = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    sys.stdout = sys.stderr                 # anything printed by accident goes to standard error
    # Named by its process id while it runs, so `theodore uninstall` can stop it (Windows cannot delete the runtime a
    # running server uses). Removed at exit; one left by a killed server names a dead process, which is ignored.
    marker = None
    if stdin is None:
        with contextlib.suppress(OSError):
            marker = core.home().state / "servers" / str(os.getpid())
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return _serve_lines(raw_in, out)
    finally:
        if marker:
            with contextlib.suppress(OSError):
                marker.unlink()


OUT = None                          # the protocol stream, for progress notifications sent during a long call


def _progress(token):
    """A long engine command sends progress while it runs, so a host that resets its timeout on progress waits."""
    def send(elapsed: float, message: str) -> None:
        item = {"jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": token, "progress": round(elapsed, 1), "message": message}}
        OUT.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        OUT.flush()
    return send


def _serve_lines(raw_in, out) -> int:
    global OUT
    OUT = out
    for line in raw_in:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            if isinstance(message, list):           # a batch (older protocol versions)
                answers = [a for a in (handle(m) for m in message if isinstance(m, dict)) if a is not None]
                answer = answers or None
            elif isinstance(message, dict):
                answer = handle(message)
            else:
                answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        for item in ([answer] if answer is not None else []) + NOTIFY:
            out.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        NOTIFY.clear()
        out.flush()
    return 0
