"""`theodore mcp`: a local stdio MCP server with two tools, `guide` and `specimen`.

Why: hosts show a command's output whole only up to a limit (Claude Code: about 30,000 characters; above that the
model sees a 2,000-character preview), while an MCP tool's text result is shown whole up to the size the tool
declares, and an MCP image block is shown as an image. The tools deliver the same texts and sheets as the
launcher's `guide` and `specimen`; nothing is written to disk.

Protocol: JSON-RPC 2.0, one message per line on standard input and output (the MCP stdio transport). Everything
else this process would print goes to standard error, so that nothing can break the stream. Python standard
library only, like the rest of the client; it needs the stage-1 client and the installation credential, not the
engine (except `guide` with skill pptx, whose manual comes with the engine).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import sys

from . import __version__, account, api, commands, core, specimens
from .contract import HEADER_GUIDE_PARTS, SKILLS
from .core import Status

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
GUIDE_MAX_RESULT_CHARS = 400_000

TOOLS = [
    {
        "name": "guide",
        "description": "Returns the instructions of a Theodore skill, or one named section of them, as one whole text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "enum": list(SKILLS)},
                "section": {"type": "string"},
            },
            "required": ["skill"],
            "additionalProperties": False,
        },
        "_meta": {"anthropic/maxResultSizeChars": GUIDE_MAX_RESULT_CHARS},
    },
    {
        "name": "specimen",
        "description": "Returns one deck-design specimen sheet, named as the deck-design text names it, as an image.",
        "inputSchema": {
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
            "additionalProperties": False,
        },
    },
]


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
        try:
            _s, headers, raw = commands._api("guide_section", what, passing=("not_found",), skill=skill, section=name, query=query)
        except api.ApiError:
            raise commands._no_such_section(skill, name) from None
        route, extra = "guide_section", {"section": name}
    else:
        _s, headers, raw = commands._api("guide", what, skill=skill, query=query)
        route, extra = "guide", {}
    chunks = [raw]
    total = max(1, int(headers.get(HEADER_GUIDE_PARTS) or 1))
    for part in range(2, total + 1):
        _s, _h, more = commands._api(route, what, skill=skill, query={**query, "part": part}, **extra)
        chunks.append(more)
    if core.refused():
        raise api.sign_in_required()
    return b"".join(chunks).decode("utf-8", errors="replace")


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
    if skill == "pptx":
        if section:
            raise core.usage("guide pptx takes no section: the rest of the pptx manual is read from the engine folder named in its header.")
        return _text(_pptx_manual())
    return _text(_fetch_whole(skill, section or None))


# --- specimen ------------------------------------------------------------------------------------------------------------

def tool_specimen(args: dict) -> dict:
    name = args.get("file")
    if not isinstance(name, str) or not specimens.valid_name(name):
        raise core.usage("a specimen is named as the deck-design text names it: letters, digits, dot, dash and underscore, ending in .jpg.")
    account.require("deck-design")
    what = f"specimen {name}"
    try:
        _s, headers, raw = commands._api("guide_specimen", what, file=name, query={"skill": "deck-design"},
                                         headers={"Accept": "image/jpeg, application/json;q=0.9"}, redirects=False,
                                         limit=specimens.MAX_BYTES)
    except api.TooLarge:
        raise core.internal(f"{what}: the answer is larger than 1 MiB; it was refused.") from None
    if not str(headers.get("Content-Type") or "").lower().startswith("image/jpeg"):
        raise core.internal(f"{what}: the answer is not a JPEG image; it was refused.")
    if str(headers.get("X-Theodore-Sha256") or "").strip().lower() != hashlib.sha256(raw).hexdigest():
        raise Status("RETRY", f"{what}: the sheet did not arrive as the service sent it (sha256 differs or is missing).",
                     "call the same tool again once. If it fails again, continue without the sheet.",
                     "A design specimen arrived damaged. I am trying once more.")
    if core.refused():
        raise api.sign_in_required()
    return {"content": [{"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/jpeg"}]}


HANDLERS = {"guide": tool_guide, "specimen": tool_specimen}


# --- the server ----------------------------------------------------------------------------------------------------------

BLOCKED: Status | None = None          # a status that stops every tool (this folder belongs to another service)


def call_tool(name: str, args) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return _text(f"unknown tool {name!r}.", error=True)
    if BLOCKED is not None:
        return _status(BLOCKED)
    core._REFUSAL, core._REFUSAL_EMITTED = None, False      # each call learns afresh (a device may sign in again meanwhile)
    try:
        return handler(args if isinstance(args, dict) else {})
    except Status as status:
        return _status(status)
    except api.TooLarge as exc:
        return _status(core.internal(f"an answer from the Theodore service was longer than {exc.limit} bytes; it was refused."))
    except Exception as exc:  # noqa: BLE001  a tool answers; it never ends the server
        core.log(f"mcp {name}: {exc.__class__.__name__}: {exc}")
        return _status(core.internal(f"{exc.__class__.__name__}: {exc}"))


def handle(message: dict) -> dict | None:
    """One JSON-RPC message in, its answer out (None for a notification)."""
    method, ident = message.get("method"), message.get("id")
    is_request = "id" in message and method is not None
    if method is None:                      # a response to something we never sent: ignored
        return None
    if method == "initialize":
        asked = str((message.get("params") or {}).get("protocolVersion") or "")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "theodore", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
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
        if answer is not None:
            out.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            out.flush()
    return 0
