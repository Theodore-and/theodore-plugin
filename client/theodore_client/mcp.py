"""`theodore mcp`: a local stdio MCP server with two tools, `guide` and `specimen`.

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
import sys
import time

from . import __version__, account, api, commands, core, credentials, install, specimens
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

DESCRIPTIONS = {
    "list": "Lists the files of a Theodore skill.",
    "guide": "Returns one file of a Theodore skill, whole.",
    "specimen": "Returns specimen sheets as images: one file, or a list of up to 6 (each image after its file name).",
}


def tools(index: dict | None = None) -> list[dict]:
    """The tool definitions. With the published index, `section` and `file` name the actual sections and sheets, as a
    folder listing would show them."""
    sections = [s for s in (index or {}).get("sections") or []]
    sheets = [s for s in (index or {}).get("specimens") or []]
    # The deck-design sections as a folder would list them; any other name is still accepted (a pptx section the
    # service serves to this account, lane STUDIO), and an unknown one answers that there is no such section.
    section = {"anyOf": [{"type": "string", "enum": sections}, {"type": "string"}]} if sections else {"type": "string"}
    sheet = {"type": "string", **({"enum": sheets} if sheets else {})}
    return [
        {"name": "list", "description": DESCRIPTIONS["list"],
         "inputSchema": {"type": "object", "properties": {"skill": {"type": "string", "enum": list(SKILLS)}},
                         "required": ["skill"], "additionalProperties": False}},
        {"name": "guide", "description": DESCRIPTIONS["guide"],
         "inputSchema": {"type": "object", "properties": {"skill": {"type": "string", "enum": list(SKILLS)}, "section": section},
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
GUIDES: dict[tuple[str, str], tuple[str, str]] = {}         # (skill, section) -> (ETag, whole text)
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


def _hold_guide(key: tuple[str, str], value: tuple[str, str]) -> None:
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
    return _text(status.render(), error=status.name != "OK")


# --- guide ---------------------------------------------------------------------------------------------------------------

def _fetch_whole(skill: str, section: str | None) -> str:
    """The whole document as the service sends it: one answer with form=whole; an older service that still pages
    is asked for every part, joined in order."""
    claims = account.require(skill)
    del claims
    query = {"form": "whole", "bindings": "tool"}
    what = f"guide {skill}"
    if section:
        name = commands._section_name(section)
        if not commands.SECTION_NAME.fullmatch(name):
            raise commands._no_such_section(skill, name)
    key = (skill, name if section else "")
    held = GUIDES.get(key)
    conditional = {"If-None-Match": held[0]} if held else None
    if section:
        try:
            status, headers, raw = commands._api("guide_section", what, passing=("not_found",), skill=skill, section=name,
                                                 query=query, headers=conditional)
        except api.ApiError:
            GUIDES.pop(key, None)
            raise commands._no_such_section(skill, name) from None
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


def _pptx_manual() -> str:
    """The pptx manual as the launcher prints it, every part in order (it comes with the engine)."""
    out = []
    part, total = 1, 1
    while part <= total:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            commands._guide_pptx(part)
        text = buffer.getvalue()
        first = text.split("\n", 1)[0]
        with contextlib.suppress(ValueError, IndexError):
            total = int(first.rsplit("part ", 1)[1].split("/", 1)[1].split(" ", 1)[0])
        out.append(text)
        part += 1
    return "".join(out)


def tool_guide(args: dict) -> dict:
    skill, section = args.get("skill"), args.get("section")
    if skill not in SKILLS:
        raise core.usage(f"unknown skill {skill!r}. The skills are: {', '.join(SKILLS)}.")
    if section is not None and not isinstance(section, str):
        raise core.usage("section is a string.")
    if skill == "pptx" and not section:
        return _text(_pptx_manual())
    return _text(_fetch_whole(skill, section or None))


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
        status, headers, raw = commands._api("guide_specimen", what, file=name, query={"skill": "deck-design"},
                                             headers=asked, redirects=False, limit=specimens.MAX_BYTES)
    except api.TooLarge:
        raise core.internal(f"{what}: the answer is larger than 1 MiB; it was refused.") from None
    if core.refused():
        raise api.sign_in_required()
    if status == 304 and held:
        _hold_sheet(name, (held[0], held[1], current))
        return held[1]
    SHEETS.pop(name, None)
    if not str(headers.get("Content-Type") or "").lower().startswith("image/jpeg"):
        raise core.internal(f"{what}: the answer is not a JPEG image; it was refused.")
    digest = hashlib.sha256(raw).hexdigest()
    if str(headers.get("X-Theodore-Sha256") or "").strip().lower() != digest:
        raise Status("RETRY", f"{what}: the sheet did not arrive as the service sent it (sha256 differs or is missing).",
                     "call the same tool again once. If it fails again, continue without the sheet.",
                     "A design specimen arrived damaged. I am trying once more.")
    _hold_sheet(name, (digest, raw, current))
    return raw


def _image(raw: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/jpeg"}


def tool_specimen(args: dict) -> dict:
    """`file` is one name (the result is that image, as before) or a list of up to SPECIMEN_MAX_PER_CALL names (for
    each, in the order asked, a text block with the name and then its image). All or nothing: a refused sheet answers
    with its status and no image."""
    asked = args.get("file")
    many = isinstance(asked, list)
    names = asked if many else [asked]
    if many and not 1 <= len(names) <= SPECIMEN_MAX_PER_CALL:
        raise core.usage(f"specimen takes one file or a list of 1 to {SPECIMEN_MAX_PER_CALL} files; {len(names)} were given. "
                         f"Ask for the rest in another call.")
    if not all(isinstance(name, str) and specimens.valid_name(name) for name in names):
        raise core.usage("a specimen is named as the deck-design text names it: letters, digits, dot, dash and underscore, ending in .jpg.")
    account.require("deck-design")
    if INDEX is None and not INDEX_TRIED:
        _index()
    sheets: dict[str, bytes] = {}
    for name in names:
        if name not in sheets:
            # The confirmation's age is checked for every sheet served, so no batch serves a held sheet more than
            # REVALIDATE_SECONDS after the service last confirmed it.
            sheets[name] = _sheet(name, _validate())
    if core.refused():
        raise api.sign_in_required()
    if not many:
        return {"content": [_image(sheets[names[0]])]}
    content: list[dict] = []
    for name in names:
        content += [{"type": "text", "text": name}, _image(sheets[name])]
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


# --- the server ----------------------------------------------------------------------------------------------------------

# Every status but a passing network trouble drops what is held: a refusal of any kind (revoked, suspended, no plan,
# update required, kill switch or policy block), and a service answer that something is gone (USAGE from a 404).
KEEP_HELD_ON = ("OFFLINE", "RETRY")
# Refusals of the device or the account: the launcher's folders are removed as well.
REFUSALS = ("SIGN_IN_REQUIRED", "SIGN_IN_PENDING", "NO_ENTITLEMENT", "UPDATE_REQUIRED", "BLOCKED_BY_POLICY", "CONNECTOR_REQUIRED")
BLOCKED: Status | None = None          # a status that stops every tool (this folder belongs to another service)


def call_tool(name: str, args) -> dict:
    handler = HANDLERS.get(name)
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
            forget(refused=core.refused() or status.name in REFUSALS)
        return _status(status)
    except api.TooLarge as exc:
        return _status(core.internal(f"an answer from the Theodore service was longer than {exc.limit} bytes; it was refused."))
    except Exception as exc:  # noqa: BLE001  a tool answers; it never ends the server
        core.log(f"mcp {name}: {exc.__class__.__name__}: {exc}")
        return _status(core.internal(f"{exc.__class__.__name__}: {exc}"))
    if core.refused():
        forget(refused=True)
    return result


def handle(message: dict) -> dict | None:
    """One JSON-RPC message in, its answer out (None for a notification)."""
    method, ident = message.get("method"), message.get("id")
    is_request = "id" in message and method is not None
    if method is None:                      # a response to something we never sent: ignored
        return None
    if method == "initialize":
        asked = str((message.get("params") or {}).get("protocolVersion") or "")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": True}},
                  "serverInfo": {"name": "theodore", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools(_index())}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
    elif not is_request:                    # notifications/initialized, notifications/cancelled, ...
        return None
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result} if is_request else None


def serve(stdin=None, stdout=None, blocked: Status | None = None) -> int:
    """Read messages until the input ends. Our standard output carries only protocol messages."""
    global BLOCKED
    BLOCKED = blocked
    raw_in = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    sys.stdout = sys.stderr                 # anything printed by accident goes to standard error
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
