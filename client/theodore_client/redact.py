"""The free-text redactor, ported from the server's (theodore: apps/worker/src/lib/redact.ts, with its word list in
apps/worker/src/lib/redact-vocabulary.ts). Both must stay in step: a change to one is made to the other in the same
round, and the case table in tests/fixtures/redact-cases.json is shared (tools/snapshot_redact_cases.mjs cuts it
out of the server's test, and tests/test_redact.py runs it here).

It removes home paths, email addresses, document and media file names, credentials in URLs and in name=value
pairs, and anything that looks like a key. Never leaking a name wins over keeping words; the generic diagnostic
words before a file name (DIAGNOSTIC_WORDS) are kept so that two different problems do not collapse into the same
text.

Each kind is one left-to-right scan that never goes back past what an earlier match consumed, so the cost is
linear in the text: no pattern here rescans a run from each of its positions.

Port notes. The server's regular expressions have no `u` flag, so `\\b`, `[A-Za-z]` and case-insensitive matching
are ASCII there: the patterns here use re.ASCII. JavaScript's `\\s` is spelled out (JS_SPACE) because Python's
differs at the edges. `\\p{L}` is str.isalpha() and `\\p{N}` the rest of str.isalnum(). Indexes are code points
here and UTF-16 units there; every length the redactor counts is over ASCII characters, so the results agree.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

__all__ = ["DIAGNOSTIC_WORDS", "redact_with_hits", "redact_free_text"]

# Generic diagnostic words the redactor may keep in front of a file name, copied verbatim from
# redact-vocabulary.ts: lower case; add only words that cannot name a person, client or company.
DIAGNOSTIC_WORDS = frozenset([
    "a", "aborted", "about", "above", "accept", "access", "accessed", "across", "action", "actions", "active",
    "add", "added", "address", "addressed", "adopt", "after", "again", "against", "alias", "align", "all",
    "allowed", "already", "also", "although", "always", "an", "anchor", "and", "another", "any", "app", "appeared",
    "application", "apply", "arabic", "are", "arg", "args", "around", "array", "arrays", "as", "assemble",
    "assembled", "assembly", "asset", "assets", "at", "attempt", "attempted", "attempting", "attempts", "authors",
    "available", "away", "axis", "back", "backed", "background", "bad", "base", "baseline", "basename", "be",
    "because", "been", "before", "being", "below", "between", "blank", "block", "blocked", "both", "bottom",
    "bound", "brief", "broken", "bubble", "build", "busy", "but", "by", "cache", "call", "callback", "called",
    "can", "cancel", "canceled", "cancelled", "cannot", "cant", "canvas", "catalog", "category", "cell", "cells",
    "changed", "changes", "chart", "check", "checker", "choose", "claimed", "cleanup", "client", "close", "closed",
    "closing", "code", "codec", "collisions", "color", "column", "comma", "command", "comment", "comments",
    "completed", "compose", "composite", "computation", "concurrent", "connect", "connection", "connector",
    "contain", "contains", "content", "conversion", "convert", "copied", "copy", "corrupt", "corrupted", "could",
    "count", "counts", "coverage", "crash", "crashed", "create", "created", "creation", "current", "daemon",
    "damaged", "data", "deadline", "deck", "decks", "declared", "decode", "decoding", "deferred", "delete", "delta",
    "denied", "destination", "detected", "determine", "dict", "did", "diff", "different", "digit", "dir",
    "direction", "directly", "directories", "directory", "discovery", "disk", "do", "document", "does", "doesnt",
    "done", "down", "during", "each", "edit", "editor", "either", "element", "embedded", "emit", "empty", "encode",
    "encoding", "end", "ended", "engine", "entry", "error", "errors", "escape", "escapes", "every", "exactly",
    "exceeded", "exception", "exclusive", "exist", "existing", "exists", "exit", "exited", "expected", "expired",
    "explicitly", "export", "exported", "extension", "extensions", "extract", "fail", "failed", "failing", "fails",
    "failure", "fallback", "false", "family", "fatal", "fetch", "field", "file", "filename", "files", "fill",
    "find", "findings", "finish", "first", "flag", "font", "for", "format", "formula", "found", "frame", "free",
    "from", "full", "gate", "gave", "geometry", "get", "got", "gradient", "group", "had", "handle", "has", "have",
    "having", "helper", "hex", "hexadecimal", "hidden", "hold", "holds", "host", "hyperlink", "id", "identifier",
    "ids", "if", "image", "images", "implement", "import", "imported", "in", "include", "incomplete", "index",
    "input", "insert", "inspect", "install", "installed", "integer", "integers", "into", "invalid", "is", "isnt",
    "it", "its", "itself", "just", "key", "keys", "labels", "language", "large", "last", "later", "launch",
    "launched", "layout", "layouts", "least", "less", "level", "limit", "line", "list", "literal", "load", "loaded",
    "loading", "lock", "locked", "long", "longer", "lost", "low", "malformed", "manifest", "many", "map", "marker",
    "markers", "match", "matched", "may", "member", "memory", "metadata", "method", "middle", "might", "missing",
    "mode", "modern", "module", "more", "most", "multiple", "must", "mutually", "name", "named", "names", "native",
    "near", "need", "needed", "needs", "negative", "network", "new", "next", "no", "node", "non", "none", "nor",
    "not", "notes", "nothing", "now", "number", "numerals", "numeric", "object", "objective", "objects", "of",
    "off", "office", "ok", "old", "on", "once", "one", "only", "open", "opened", "opening", "operation", "options",
    "or", "order", "original", "other", "out", "output", "over", "overflow", "overwritten", "pack", "package",
    "packaging", "page", "parent", "parse", "parser", "parsing", "part", "partition", "path", "payload",
    "permission", "permutation", "persist", "placeholder", "placeholders", "please", "plot", "point", "points",
    "port", "position", "positional", "positive", "post", "powerpoint", "prepare", "present", "presentation",
    "preset", "prior", "process", "processing", "produce", "produced", "program", "project", "projects", "promoted",
    "prop", "props", "provide", "publish", "published", "publishing", "query", "quota", "range", "ranges", "read",
    "readable", "reading", "ready", "reason", "record", "reference", "referenced", "references", "refresh",
    "refused", "region", "registry", "rejected", "relationship", "remote", "render", "rendered", "rendering",
    "reorder", "repaired", "replace", "replacement", "reply", "reported", "request", "requested", "required",
    "requires", "reset", "resolve", "resolved", "response", "restore", "result", "retried", "retry", "retrying",
    "return", "returned", "reviewed", "rewritten", "role", "root", "rotation", "row", "rows", "run", "running",
    "runs", "runtime", "same", "save", "saved", "saving", "scatter", "schema", "scope", "search", "second",
    "section", "sections", "seed", "selected", "selection", "selector", "separated", "serializer", "series",
    "server", "set", "severity", "shape", "shapes", "shared", "sheet", "should", "show", "shows", "sidecar",
    "since", "size", "skill", "slide", "slides", "slow", "slug", "small", "snapshot", "so", "socket", "some",
    "source", "space", "spec", "specified", "specs", "splice", "split", "stabilize", "stage", "stale", "start",
    "started", "state", "status", "stay", "still", "stopped", "strategy", "string", "strings", "structural",
    "style", "sub", "subclasses", "subcommand", "subset", "succeeded", "success", "such", "supported", "sync",
    "synthesizable", "table", "target", "targeted", "targets", "task", "tasks", "template", "text", "than", "that",
    "the", "then", "there", "these", "think", "this", "those", "through", "time", "timed", "timeout", "to", "too",
    "top", "trailing", "transform", "transforms", "translated", "translation", "transport", "tried", "truncated",
    "try", "trying", "two", "type", "types", "unable", "unavailable", "under", "unexpected", "unix", "unknown",
    "unpacked", "unreadable", "unreferenced", "unsupported", "unterminated", "until", "up", "update", "upload",
    "url", "use", "used", "user", "using", "valid", "validate", "validation", "value", "values", "variant", "very",
    "via", "visible", "waiting", "warning", "was", "wasnt", "were", "what", "when", "where", "which", "while",
    "width", "will", "with", "within", "without", "wont", "wontfix", "word", "work", "workbook", "worker",
    "workspace", "workspaces", "would", "write", "writer", "writing", "written", "wrong", "yes", "yet", "zero",
])

Replacement = Tuple[int, int, str]       # start, end, text

# JavaScript's \s, which BEFORE_PATH and the bearer look-ahead use.
JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a" \
           "\u2028\u2029\u202f\u205f\u3000\ufeff"
_JS_SPACE_CLASS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def _at(text: str, i: int) -> str:
    """text[i], or "" outside the text (JavaScript's undefined; Python would wrap a negative index)."""
    return text[i] if 0 <= i < len(text) else ""


def _apply(text: str, replacements: List[Replacement]) -> str:
    """Applies non-overlapping replacements, sorted by start."""
    if not replacements:
        return text
    out = []
    at = 0
    for start, end, new in replacements:
        out.append(text[at:start])
        out.append(new)
        at = end
    out.append(text[at:])
    return "".join(out)


_SPACES = frozenset(" \t\n\r\f\v")


def _is_space(ch: str) -> bool:
    return ch in _SPACES


def _is_separator(ch: str) -> bool:
    return ch == "/" or ch == "\\"


def _is_diagnostic_word(word: str) -> bool:
    """True when a word may stay in front of a file name: a generic diagnostic word, or one with no letter at all
    (a number, a dash). Surrounding punctuation is ignored."""
    lower = word.lower()
    a, b = 0, len(lower)
    while a < b and not lower[a].isalnum():
        a += 1
    while b > a and not lower[b - 1].isalnum():
        b -= 1
    bare = lower[a:b]
    return not any(ch.isalpha() for ch in bare) or bare in DIAGNOSTIC_WORDS


# ------------------------------------------------------------------ credentials

# Names whose value is a credential, as the last part of an identifier (`access_token`, `client_secret`).
_CREDENTIAL_PAIR = re.compile(
    r"""\b[A-Za-z0-9_]*?(?:password|passwd|pwd|token|secret|apikey|api_key|api-key|key)["']?[ \t]*[:=][ \t]*""",
    re.IGNORECASE | re.ASCII)


def _credentials(text: str) -> List[Replacement]:
    """Credentials whatever their length: the userinfo of a URL (`https://user:pass@host` ->
    `https://[redacted]@host`) and the value of a pair whose name is a credential name (`password=...`,
    `"token": "..."`)."""
    n = len(text)
    urls: List[Replacement] = []
    scanned = -1      # where the last userinfo scan stopped: a later "://" before it stops at the same place
    at = text.find("://")
    while at != -1:
        if at + 3 <= scanned:
            end = scanned
        else:
            end = at + 3
            while end < n and not _is_space(text[end]) and text[end] not in "/?#@":
                end += 1
            scanned = end
        if end < n and text[end] == "@" and end > at + 3:
            urls.append((at + 3, end, "[redacted]"))
        at = text.find("://", at + 3)
    out = list(urls)
    u = 0             # the first URL replacement that does not end before the current pair: both lists are in order
    pos = 0
    while True:
        match = _CREDENTIAL_PAIR.search(text, pos)
        if match is None:
            break
        start = match.end()
        quote = _at(text, start)
        if quote == '"' or quote == "'":
            start += 1
            end = start
            while end < n and text[end] != quote and text[end] != "\n":
                end += 1
        else:
            end = start
            while end < n and not _is_space(text[end]) and text[end] not in "\"',;&":
                end += 1
        while u < len(urls) and urls[u][1] <= start:
            u += 1
        overlaps_url = u < len(urls) and urls[u][0] < end
        if end > start and not overlaps_url:
            out.append((start, end, "[redacted]"))
        pos = max(end, match.end())
    out.sort(key=lambda r: r[0])
    return out


# ------------------------------------------------------------------ absolute paths

# What may come right before an absolute path: the start, a space, a quote, a bracket, `=`, `,`, `;`, `:`.
_BEFORE_PATH = frozenset(JS_SPACE + "\"'`([{<=,;:")
_ASCII_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def _path_starts_at(text: str, i: int) -> int:
    """Non-zero when an absolute path starts at `i`: `/x`, `~/`, `~\\`, `C:\\`, `C:/`, `\\\\server`."""
    if i > 0 and text[i - 1] not in _BEFORE_PATH:
        return 0
    a = _at(text, i)
    b = _at(text, i + 1)
    if a == "/" and b != "" and b != "/" and not _is_space(b):
        return 1
    if a == "~" and _is_separator(b):
        return 2
    if a == "\\" and b == "\\":
        c = _at(text, i + 2)
        if c != "" and c != "\\" and not _is_space(c):
            return 2
    if a in _ASCII_LETTERS and b == ":" and _is_separator(_at(text, i + 2)):
        return 3
    return 0


# Characters that end a clause for a path: it does not continue across them.
_PATH_CLAUSE_END = frozenset([",", ";", "\n", "\r", '"', "'", "`"])
# Punctuation that ends a path's last word; it stays outside the path.
_CLAUSE_END = frozenset([",", ";", ".", "!", "?", ":", '"', "'"])
_SENTENCE_END = frozenset(".!?")


def _absolute_paths(text: str) -> List[Replacement]:
    """An absolute path (POSIX root, `~`, a drive letter, a UNC prefix; a home folder is one) runs over every
    character up to the next whitespace, whatever punctuation it holds. It then continues over each following word
    on the line while a separator of the same path follows later in the clause (a folder with a space), or while
    the word is not a diagnostic word (a name with a space); it stops before a word that starts another path. When
    in doubt it removes more."""
    n = len(text)
    # For each position: the next separator in the same clause, and the start of the word it is in.
    next_separator = [-1] * (n + 1)
    nxt = -1
    for i in range(n - 1, -1, -1):
        ch = text[i]
        if ch in _PATH_CLAUSE_END or (_is_space(ch) and _at(text, i - 1) in _SENTENCE_END):
            nxt = -1
        elif ch == "/" or ch == "\\":
            nxt = i
        next_separator[i] = nxt
    word_start = [0] * (n + 1)
    start_of_word = 0
    for i in range(n):
        if _is_space(text[i]):
            start_of_word = i + 1
        word_start[i] = start_of_word

    out: List[Replacement] = []
    i = 0
    while i < n:
        if not _path_starts_at(text, i):
            i += 1
            continue
        start = i
        end = start
        while end < n and not _is_space(text[end]):
            end += 1
        while _at(text, end - 1) not in _CLAUSE_END:
            nxt = end
            while nxt < n and (text[nxt] == " " or text[nxt] == "\t"):
                nxt += 1
            if nxt == end or nxt >= n or _is_space(text[nxt]) or _path_starts_at(text, nxt):
                break
            word_end = nxt
            while word_end < n and not _is_space(text[word_end]):
                word_end += 1
            separator = next_separator[nxt]
            # A separator later in the clause that is not the start of another path belongs to this one.
            same_path = separator != -1 and not _path_starts_at(text, word_start[separator])
            if not same_path and _is_diagnostic_word(text[nxt:word_end]):
                break
            end = word_end
        cut = end
        while cut > start + 1 and _at(text, cut - 1) in _CLAUSE_END:
            cut -= 1
        out.append((start, cut, "[path]"))
        i = max(end, start + 1)
    return out


# ------------------------------------------------------------------ email addresses

_LOCAL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-")
_DOMAIN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-")
_DOMAIN = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", re.ASCII)


def _emails(text: str) -> List[Replacement]:
    n = len(text)
    out: List[Replacement] = []
    floor = 0
    at = text.find("@")
    while at != -1:
        start = at
        while start > floor and text[start - 1] in _LOCAL_CHARS:
            start -= 1
        end = at + 1
        while end < n and text[end] in _DOMAIN_CHARS:
            end += 1
        while end > at + 1 and (text[end - 1] == "." or text[end - 1] == "-"):
            end -= 1
        if start < at and _DOMAIN.fullmatch(text, at + 1, end):
            out.append((start, end, "[email]"))
            floor = end
        else:
            floor = max(floor, at + 1)
        at = text.find("@", at + 1)
    return out


# ------------------------------------------------------------------ file names

# Extensions of documents and media a user works with.
_FILE_EXTENSION = re.compile(
    r"\.(?:pptx|pptm|ppt|potx|potm|ppsx|ppsm|key|odp|xlsx|xlsm|xlsb|xls|xltx|csv|numbers|ods|docx|docm|doc|dotx"
    r"|rtf|odt|pages|pdf|png|jpe?g|gif|bmp|tiff?|webp|heic|svg|emf|wmf|mp4|mov|m4v|avi|wmv|mp3|wav|m4a)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE | re.ASCII)
# Characters that start a new clause when walking back from an extension.
_CLAUSE_BREAK = frozenset(['"', "'", "`", "/", "\\", ":", ",", ";", "(", ")", "[", "]", "{", "}", "<", ">", "|",
                           "\n", "\r"])
_QUOTES = frozenset(['"', "'", "`"])


_CLOSING_BRACKET = {")": "(", "]": "[", "}": "{"}


def _bracket_group_start(text: str, stem_end: int, floor: int) -> int:
    """Where the bracketed parts that end a stem begin: `stem_end` itself when the stem does not end in a closing
    bracket; the matching opening bracket when it does (repeated for "a (1) [2].pptx"); just before the closing
    bracket when none matches on the line, so that the name is still removed."""
    at = stem_end
    while at > floor and _at(text, at - 1) in _CLOSING_BRACKET:
        close = text[at - 1]
        opening = _CLOSING_BRACKET[close]
        depth = 0
        i = at - 1
        while i >= floor:
            ch = text[i]
            if ch in ("\n", "\r"):
                i = floor - 1
                break
            if ch == close:
                depth += 1
            elif ch == opening:
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        if i < floor:
            return at - 1
        at = i
    return at


def _file_names(text: str, extension=None) -> List[Replacement]:
    """A file name is everything from the first word of its clause that is not a diagnostic word up to and
    including the extension; diagnostic words before it stay. A quoted name goes with its quotes. A clause starts
    after a quote, a separator, a colon, a comma, a semicolon, a bracket, a line end or a sentence end, except that
    bracketed parts at the end of the stem belong to the name."""
    out: List[Replacement] = []
    floor = 0
    pos = 0
    while True:
        match = (extension or _FILE_EXTENSION).search(text, pos)
        if match is None:
            break
        dot = match.start()
        end = match.end()
        pos = end
        stem_end = dot
        before = _at(text, stem_end - 1)
        # A stem that ends in a bracketed part ("Pitch (final).pptx", "Budget [2026].xlsx"): the part belongs to the
        # name, so the clause is looked for before its opening bracket.
        group_start = _bracket_group_start(text, stem_end, floor) if stem_end > floor else stem_end
        if group_start == stem_end and (stem_end <= floor or _is_space(before) or before in _CLAUSE_BREAK):
            # An extension on its own (".pptx files") names nothing.
            floor = max(floor, end)
            continue
        clause_start = group_start
        while clause_start > floor:
            ch = text[clause_start - 1]
            if ch in _CLAUSE_BREAK:
                break
            if _is_space(ch) and _at(text, clause_start - 2) in _SENTENCE_END:
                break
            clause_start -= 1
        quote = _at(text, clause_start - 1)
        if quote in _QUOTES and _at(text, end) == quote and clause_start - 1 >= floor:
            out.append((clause_start - 1, end + 1, "[file]"))
            floor = end + 1
            pos = end + 1
            continue
        # Keep the diagnostic words before the name; the word that carries the extension always goes.
        start = clause_start
        while True:
            while start < stem_end and _is_space(text[start]):
                start += 1
            word_end = start
            while word_end < stem_end and not _is_space(text[word_end]):
                word_end += 1
            if word_end >= stem_end or word_end > group_start or not _is_diagnostic_word(text[start:word_end]):
                break
            start = word_end
        out.append((start, end, "[file]"))
        floor = end
    return out


# ------------------------------------------------------------------ tokens

# Whatever follows "Bearer", whatever its length.
_BEARER = re.compile(r"\bBearer[ \t]+(?=[^" + _JS_SPACE_CLASS + r"\"',;])", re.IGNORECASE | re.ASCII)
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE | re.ASCII)


def _bearer_values(text: str) -> List[Replacement]:
    n = len(text)
    out: List[Replacement] = []
    pos = 0
    while True:
        match = _BEARER.search(text, pos)
        if match is None:
            break
        start = match.end()
        end = start
        while end < n and not _is_space(text[end]) and text[end] not in "\"',;":
            end += 1
        out.append((start, end, "[token]"))
        pos = end
    return out


def _long_tokens(text: str) -> List[Replacement]:
    """A JWT, or 32 or more characters of key material, with or without digits. A UUID is kept (request ids help
    triage, and an id is not a credential). One pass over runs of token characters, dots included for a JWT."""
    n = len(text)
    out: List[Replacement] = []
    at = 0
    while at < n:
        if text[at] not in _TOKEN_CHARS:
            at += 1
            continue
        end = at
        while end < n and (text[end] in _TOKEN_CHARS
                           or (text[end] == "." and end + 1 < n and text[end + 1] in _TOKEN_CHARS)):
            end += 1
        run = text[at:end]
        parts = run.split(".")
        if run.startswith("eyJ") and len(parts) == 3 and all(len(part) >= 8 for part in parts):
            out.append((at, end, "[token]"))
        else:
            # Without a JWT shape, each dot-separated piece stands alone.
            offset = at
            for part in parts:
                if len(part) >= 32 and not _UUID.fullmatch(part) and part != "[token]":
                    out.append((offset, offset + len(part), "[token]"))
                offset += len(part) + 1
        at = end
    return out


# ------------------------------------------------------------------ all of it

# In this order: credentials (before addresses, so `user:pass@host` is not read as an email); a path takes the
# file name at its end; then addresses, file names, tokens. Every absolute path is reported as `home_path`, the
# contract's only path kind.
_PASSES = (
    ("long_token", _credentials),
    ("home_path", _absolute_paths),
    ("email", _emails),
    ("office_file_name", _file_names),
    ("long_token", _bearer_values),
    ("long_token", _long_tokens),
)


# Not in the server's redactor: names of the scripts and text files a deck is built from ("Acme Holdings deck.js"),
# which the client also removes, by the same rule as document names. The server's table is unchanged by it.
_SCRIPT_EXTENSION = re.compile(r"\.(?:js|mjs|cjs|ts|jsx|tsx|py|json|md|txt|html?|css|xml|ya?ml|sh|ps1|cmd|bat)(?![A-Za-z0-9_])",
                               re.IGNORECASE | re.ASCII)


def redact_script_names(text: str) -> str:
    """The client-side extra pass: bare script and text file names, removed with the non-diagnostic words before them."""
    return _apply(text, _file_names(text, _SCRIPT_EXTENSION))


def redact_with_hits(text: str, field: str, hits: Dict[str, dict]) -> str:
    """Redacts `text`, adding what it removed to `hits` under `field` (one entry per kind and field, keyed
    "<kind>\\n<field>", with a count: {"kind", "field", "count"})."""
    out = text
    for kind, find in _PASSES:
        found = find(out)
        if not found:
            continue
        key = kind + "\n" + field
        hit = hits.get(key)
        if hit is None:
            hit = {"kind": kind, "field": field, "count": 0}
            hits[key] = hit
        hit["count"] += len(found)
        out = _apply(out, found)
    return out


def redact_free_text(text: str) -> str:
    """`text` with home paths, email addresses, file names and tokens replaced by `[path]`, `[email]`, `[file]`,
    `[token]`, and credentials by `[redacted]`."""
    return redact_with_hits(text, "text", {})
