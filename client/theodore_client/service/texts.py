"""Texts and sheets, served online and authorised on every call; held in this process's memory only (contract C6).

Carl, 24 November 2026: "we still need to serve them online for IP protection etc. but still make the network calls
and needs really fast." So every guide, specimen, list and skill call sends exactly one request to the Theodore service,
and nothing is served unless that request succeeded. What makes it fast:
- the request goes on a kept-alive connection (api.KEPT, opened at session start), so no call pays a TCP and TLS set-up;
- when this process already holds a body, the request is conditional: the service answers 304 without composing or
  sending anything, and the body comes from memory; a 200 replaces it;
- a call's items (several sections, up to 8 sheets, a skill's two texts) go in ONE request, POST /v1/guide/batch,
  answered by a zip whose first member is index.json (C6-batch-index) and whose other members are the bodies sent.
A service without the batch route (before lane D's backend is deployed) is asked item by item on the single routes,
concurrently on the pool: still one request per item per call, still conditional.

Held bodies live in memory (64 MiB at most, least recently used first), never on disk, and go on any refusal and when
the process ends. The one exception on disk is the shell route's sheet files (`sheet_files`): a sheet as a file in
<work>/scratch/sheets/<session>/ (0600 in a 0700 folder), removed when its session ends or after 15 minutes.

One `Texts` per process: the service makes its own (with the credential and the licence claims it holds in memory) and
installs it with `use()`; a one-shot command gets a default one from `current()`. Every method may raise `core.Status`.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from .. import account, api, commands, core, credentials, install, specimens
from ..contract import HEADER_GUIDE_PARTS, HEADER_GUIDE_VERSION, SKILLS
from ..core import Status

SPECIMEN_MAX_PER_CALL = 6           # the specimen tool's list (its schema says so)
BATCH_MAX_ITEMS = 16                # C6-batch-request
BATCH_MAX_SHEETS = 8
BATCH_ANSWER_MAX = 24 << 20         # 8 sheets of at most 1 MiB, 16 texts of at most 400,000 bytes, the index
TEXT_MAX_BYTES = 400_000            # a whole section (GUIDE_WHOLE_MAX_BYTES)
HELD_MAX_BYTES = 64 << 20           # what one process holds, least recently used first
SINGLE_ROUTES_SECONDS = 600.0       # after a 404 for the batch route, the single routes for this long, then the batch again
# How a text names its links: "tool" (the MCP tools' wording), "launcher" (the shell route's), "chat" (the tool wording,
# and the pptx manual without update notices or launcher pointers, as the chat's one tool has it).
BINDINGS = ("tool", "launcher", "chat")
SHEET_FILES_KEEP_S = specimens.SHEET_FILES_KEEP_S   # the shell route's transient sheet files
WARM_IDLE_S = 60.0                  # keep_warm(): a connection idle this long gets one cheap request
REQUEST_SECONDS = 20.0              # one request's time; a call answers OFFLINE past it (no second attempt: one request per call)
MANUAL_SECTION = "manual"           # the pptx manual, served for engines from rc.29


def _default_credential() -> str:
    """The installation secret, read from its store (a keychain process on macOS): the service passes its own holder."""
    cred = credentials.load()
    if not cred:
        raise account.login_start()
    return cred["installation_secret"]


def _names(items, key: str, pattern) -> list[str]:
    out = []
    for item in items if isinstance(items, list) else []:
        name = item.get(key) if isinstance(item, dict) else item
        if isinstance(name, str) and pattern(name) and name not in out:
            out.append(name)
    return out


def parse_index(raw: bytes, etag: str = "", content_type: str = "application/json") -> dict | None:
    """{version, sections, specimens, etag} from a GuideIndexResponse, or None when it is not one."""
    if "json" not in content_type.lower():
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
    if etag:
        index["etag"] = etag
    return index


_SHEET_FILE = re.compile(r"([0-9a-f]{32}|[0-9a-f]{64})-([A-Za-z0-9._-]{1,80}\.jpg)")


def _etag_hex(etag: str) -> str:
    """The hex of an ETag as a sheet file's name carries it: `"b1-<32 hex>"` (the batch's) or `"<64 hex>"` (a single
    route's published SHA-256)."""
    bare = etag.strip().removeprefix("W/").strip('"')
    return bare[3:] if bare.startswith("b1-") else bare


def _etag_of(hex_: str) -> str:
    return f'"b1-{hex_}"' if len(hex_) == 32 else f'"{hex_}"'


def _timeout() -> float:
    """One request's time: REQUEST_SECONDS (raised behind a proxy by api._timeout). Test seam:
    THEODORE_TEST_TEXTS_TIMEOUT, honoured only when the API base is loopback."""
    raw = os.environ.get("THEODORE_TEST_TEXTS_TIMEOUT")
    if raw and core.is_loopback_api():
        with contextlib.suppress(ValueError):
            return float(raw)
    return REQUEST_SECONDS


class Item:
    """One thing a call needs: a section of a skill as composed for a role and bindings, a sheet, or a skill's index."""

    __slots__ = ("kind", "skill", "name", "role", "bindings")

    def __init__(self, kind: str, skill: str, name: str = "", role: str = "lead", bindings: str = "launcher"):
        self.kind, self.skill, self.name, self.role, self.bindings = kind, skill, name, role, bindings

    @property
    def key(self) -> tuple:
        if self.kind == "sheet":
            return ("sheet", self.name)
        if self.kind == "index":
            return ("index", self.skill, self.role)
        return ("section", self.skill, self.name, self.role, self.bindings)

    def __eq__(self, other) -> bool:
        return isinstance(other, Item) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __repr__(self) -> str:
        return f"Item{self.key}"


class Got:
    """One item's answer: `status` 200 (its body, sent now or held: `fresh` says which), 403 (`code`) or 404."""

    __slots__ = ("status", "body", "etag", "version", "code", "fresh")

    def __init__(self, status: int, body: bytes | None = None, etag: str = "", version: str = "", code: str = "", fresh: bool = False):
        self.status, self.body, self.etag, self.version, self.code, self.fresh = status, body, etag, version, code, fresh

    def text(self) -> str:
        return (self.body or b"").decode("utf-8", errors="replace")


class _Held:
    """Bodies held in memory: item key -> (ETag, bytes, version), least recently used first, bounded by total size."""

    def __init__(self, limit: int = HELD_MAX_BYTES):
        self.limit = limit
        self._items: collections.OrderedDict = collections.OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key, etag: str, body: bytes, version: str) -> None:
        if len(body) > self.limit:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= len(old[1])
            self._items[key] = (etag, body, version)
            self._size += len(body)
            while self._size > self.limit and self._items:
                _key, (_etag, dropped, _version) = self._items.popitem(last=False)
                self._size -= len(dropped)

    def drop(self, key) -> None:
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= len(old[1])

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0

    def __len__(self) -> int:
        return len(self._items)


class Texts:
    """See the module's text. `credential()` returns the installation secret (from memory in the service) and raises
    SIGN_IN_REQUIRED when there is none; `require(skill)` checks the licence claims for a skill; `notify()` is called
    when the deck-design index's names changed (the service then sends tools/list_changed to every relay)."""

    def __init__(self, credential=None, require=None, notify=None, pool: api.KeptAlive | None = None):
        self._credential = credential or _default_credential
        self._require = require or account.require
        self._notify = notify
        self.pool = pool or api.KEPT
        self._held = _Held()
        self._lock = threading.RLock()
        self._index: dict | None = None            # deck-design's index (role lead), as last answered
        self._single_until = 0.0                   # time.monotonic() until which the single routes are used
        self._last_request = 0.0                   # time.monotonic() of the last request sent (keep_warm)
        self._closed = False
        self.requests = 0                          # requests sent (tests, doctor)

    # --- one request ---------------------------------------------------------------------------------------------------

    def _refused(self, error: api.ApiError, what: str) -> Status:
        """A refusal of the service as the status the assistant acts on (commands._api's rules); every held body goes."""
        self.forget(refused=error.code in ("installation_revoked", "unauthenticated", "account_suspended", "entitlement_inactive",
                                           "device_proof_invalid"))
        if error.code in ("installation_revoked", "unauthenticated"):
            credentials.delete()
            account.delete_token()
            return account.login_start("this device was signed out; it needs to sign in again.")
        if error.http_status in (401, 403) and error.code != "auto_reports_disabled":
            account.delete_token()
        return api.status_from(error, what)

    def _send(self, method: str, url: str, what: str, *, body=None, headers: dict | None = None, limit: int | None = None,
              passing: tuple[str, ...] = (), statuses: tuple[int, ...] = (), again: bool | None = None, redirects: bool = True):
        """One authenticated request on the pool. A network failure is today's OFFLINE status; a refusal its status
        (after which nothing held is served); an error whose code is in `passing`, or whose HTTP status is in
        `statuses`, is raised as ApiError for the caller."""
        self.requests += 1
        self._last_request = time.monotonic()
        try:
            return self.pool.request(method, url, bearer=self._credential(), body=body, headers=headers, limit=limit,
                                     again=again, redirects=redirects, timeout=_timeout())
        except api.NetworkError as error:
            raise api.offline(error, what) from None
        except api.ApiError as error:
            if error.code in passing or error.http_status in statuses:
                raise
            raise self._refused(error, what) from None

    def fetch(self, items: list[Item], what: str) -> list[Got]:
        """Every item in ONE request (or, from a service without the batch route, one request per item, concurrently),
        conditional where a body is held. Answers in the order asked."""
        if not items:
            return []
        if len(items) > BATCH_MAX_ITEMS or sum(item.kind == "sheet" for item in items) > BATCH_MAX_SHEETS:
            raise core.usage(f"at most {BATCH_MAX_ITEMS} texts and {BATCH_MAX_SHEETS} sheets in one call.")
        held = {item: self._held.get(item.key) for item in items}
        if time.monotonic() >= self._single_until:
            try:
                got = self._batch(items, held, what)
            except api.ApiError as error:           # a service without the route: its 404 or 405, never an item's
                core.log(f"texts: the service has no batch route ({error.http_status}); single routes for {SINGLE_ROUTES_SECONDS:g} s")
                self._single_until = time.monotonic() + SINGLE_ROUTES_SECONDS
            else:
                return self._settled(items, got, what)
        return self._settled(items, self._single(items, held, what), what)

    def _settled(self, items: list[Item], got: list[Got], what: str) -> list[Got]:
        """After a successful request: refused items raise as the single routes' refusals would; nothing else."""
        if core.refused():
            raise api.sign_in_required()
        for item, answer in zip(items, got):
            if answer.status == 403:
                error = api.ApiError(403, answer.code or "entitlement_inactive", f"{item.skill} is not in this account's plan")
                api._signed_out(403, error.code, api.url_for("guide_batch"), True)
                raise self._refused(error, f"guide {item.skill}")
        return got

    def _batch(self, items: list[Item], held: dict, what: str) -> list[Got]:
        body_items = []
        for number, item in enumerate(items):
            entry: dict = {"id": f"i{number}", "type": item.kind, "skill": item.skill}
            if item.kind == "section":
                entry.update(section=item.name, role=item.role, bindings=item.bindings)
            elif item.kind == "sheet":
                entry["file"] = item.name
            else:
                entry["role"] = item.role
            if held[item] is not None and held[item][0].startswith('"b1-'):
                entry["etag"] = held[item][0]
            body_items.append(entry)
        _status, _headers, raw = self._send("POST", api.url_for("guide_batch"), what, body={"v": 1, "items": body_items},
                                            headers={"Accept": "application/zip"}, limit=BATCH_ANSWER_MAX,
                                            statuses=(404, 405), again=True)
        return self._read_batch(raw, items, held, what)

    def _read_batch(self, raw: bytes, items: list[Item], held: dict, what: str) -> list[Got]:
        damaged = Status("RETRY", f"{what}: the Theodore service's answer did not arrive whole; nothing of it was used.",
                         "run the same command (or call the same tool) again once. If it fails again, tell the user.",
                         "Theodore's service sent an answer that did not arrive whole. I am trying once more.")
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
            members = archive.infolist()
            if not members or members[0].filename != "index.json" or members[0].file_size > 1 << 20:
                raise damaged
            index = json.loads(archive.read("index.json").decode("utf-8"))
            answers = {entry["id"]: entry for entry in index["items"]} if index.get("v") == 1 else None
            if answers is None:
                raise damaged
            version = str(index.get("version") or "")
            got: list[Got] = []
            keep: list = []                     # what memory takes, once the whole answer has been read and checked
            for number, item in enumerate(items):
                entry = answers.get(f"i{number}")
                if not isinstance(entry, dict):
                    raise damaged
                status, etag = entry.get("status"), str(entry.get("etag") or "")
                if status == 304:
                    kept = held[item]
                    if kept is None or kept[0] != etag:
                        raise damaged               # a 304 for something this call did not hold
                    keep.append((item.key, kept[0], kept[1], kept[2]))
                    got.append(Got(200, kept[1], kept[0], kept[2] or version))
                elif status == 200:
                    member = str(entry.get("member") or "")
                    info = archive.getinfo(member)
                    if item.kind == "sheet" and info.file_size > specimens.MAX_BYTES:
                        raise core.internal(f"specimen {item.name}: the answer is larger than 1 MiB; it was refused.")
                    if info.file_size > TEXT_MAX_BYTES * 4:
                        raise damaged
                    if item.kind == "sheet" and not str(entry.get("content_type") or "").lower().startswith("image/jpeg"):
                        raise core.internal(f"specimen {item.name}: the answer is not a JPEG image; it was refused.")
                    data = archive.read(member)
                    if hashlib.sha256(data).hexdigest() != str(entry.get("sha256") or ""):
                        if item.kind != "sheet":
                            raise damaged
                        raise Status("RETRY", f"specimen {item.name}: the sheet did not arrive as the service sent it (sha256 differs); "
                                     f"nothing of the answer was used.",
                                     "call the same tool (or run the same command) again once. If it fails again, continue without the sheet.",
                                     "A design specimen arrived damaged. I am trying once more.")
                    if item.kind == "sheet" and data[:2] != b"\xff\xd8":
                        raise core.internal(f"specimen {item.name}: the answer is not a JPEG image; it was refused.")
                    keep.append((item.key, etag, data, version))
                    got.append(Got(200, data, etag, version, fresh=True))
                elif status in (403, 404):
                    keep.append((item.key, None, None, None))
                    got.append(Got(status, code=str(entry.get("code") or "")))
                else:
                    raise damaged
            for key, etag, data, version in keep:
                if data is None:
                    self._held.drop(key)
                else:
                    self._held.put(key, etag, data, version)
            return got
        except (zipfile.BadZipFile, KeyError, ValueError, TypeError, AttributeError, OSError):
            raise damaged from None

    # --- the single routes (a service from before the batch route) -------------------------------------------------------

    def _single(self, items: list[Item], held: dict, what: str) -> list[Got]:
        if len(items) == 1:
            return [self._one(items[0], held[items[0]], what)]
        results: list = [None] * len(items)

        def run(number: int, item: Item) -> None:
            try:
                results[number] = self._one(item, held[item], what)
            except BaseException as exc:        # noqa: BLE001  raised in the caller's thread below
                results[number] = exc
        threads = [threading.Thread(target=run, args=(number, item), daemon=True) for number, item in enumerate(items)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    def _one(self, item: Item, kept, what: str) -> Got:
        if item.kind == "sheet":
            return self._one_sheet(item, kept, what)
        conditional = {"If-None-Match": kept[0]} if kept is not None and not kept[0].startswith('"b1-') else {}
        if item.kind == "index":
            query = {"form": "index", **({"role": item.role} if item.role == "slide" else {})}
            try:
                status, headers, raw = self._send("GET", api.url_for("guide", query, skill=item.skill), what, headers=conditional,
                                                  passing=("not_found",))
            except api.ApiError:
                return Got(404)
            if status == 304 and kept is not None:
                return Got(200, kept[1], kept[0], kept[2])
            etag = str(headers.get("ETag") or "")
            if parse_index(raw, etag, str(headers.get("Content-Type") or "")) is None:
                return Got(404)                     # an older service answers the entry text: no index
            self._held.put(item.key, etag, raw, "")
            return Got(200, raw, etag, fresh=True)
        query = {"form": "whole", **({"role": item.role} if item.role == "slide" else {}),
                 **({"bindings": "tool"} if item.bindings == "tool" else {})}
        route, params = ("guide_section", {"section": item.name}) if item.name else ("guide", {})
        try:
            status, headers, raw = self._send("GET", api.url_for(route, query, skill=item.skill, **params), what,
                                              headers=conditional, passing=("not_found",))
        except api.ApiError:
            self._held.drop(item.key)
            return Got(404)
        version = str(headers.get(HEADER_GUIDE_VERSION) or "")
        if status == 304 and kept is not None:
            return Got(200, kept[1], kept[0], kept[2] or version)
        chunks, total = [raw], max(1, int(headers.get(HEADER_GUIDE_PARTS) or 1))
        later = {key: value for key, value in query.items() if key != "form"}
        for part in range(2, total + 1):        # an older service that still pages: every part, in order, now
            try:
                _s, _h, more = self._send("GET", api.url_for(route, {**later, "part": part}, skill=item.skill, **params), what)
            except Status as status:
                missing = f"part {part} of {total}" + (f" (and the {total - part} after it)" if part < total else "")
                failed = Status(status.name, f"{what}{' ' + item.name if item.name else ''}: {missing} could not be fetched "
                                f"({status.summary}). Nothing of the text was printed: it is one text and was not read whole.",
                                status.next_step, status.tell_user)
                failed.cause = status.cause
                raise failed from None
            chunks.append(more)
        body, etag = b"".join(chunks), str(headers.get("ETag") or "").strip()
        if total == 1 and etag:
            self._held.put(item.key, etag, body, version)
        else:
            self._held.drop(item.key)
        return Got(200, body, etag, version, fresh=True)

    def _one_sheet(self, item: Item, kept, what: str) -> Got:
        asked = {"Accept": "image/jpeg, application/json;q=0.9"}
        if kept is not None:
            asked["If-None-Match"] = f'"{specimens.content_sha256(kept[1])}"'
        try:
            status, headers, raw = self._send("GET", api.url_for("guide_specimen", {"skill": "deck-design"}, file=item.name), what,
                                              headers=asked, limit=specimens.MAX_BYTES, passing=("not_found",), redirects=False)
        except api.TooLarge:
            raise core.internal(f"specimen {item.name}: the answer is larger than 1 MiB; it was refused.") from None
        except api.ApiError:
            self._held.drop(item.key)
            return Got(404)
        if status == 304 and kept is not None:
            return Got(200, kept[1], kept[0], kept[2])
        if not str(headers.get("Content-Type") or "").lower().startswith("image/jpeg"):
            raise core.internal(f"specimen {item.name}: the answer is not a JPEG image; it was refused.")
        if str(headers.get("X-Theodore-Sha256") or "").strip().lower() != hashlib.sha256(raw).hexdigest() \
                or not specimens.content_matches(raw, headers.get("ETag")):
            raise Status("RETRY", f"specimen {item.name}: the sheet did not arrive as the service sent it (sha256 differs or is missing).",
                         "call the same tool again once. If it fails again, continue without the sheet.",
                         "A design specimen arrived damaged. I am trying once more.")
        etag = f'"{specimens.content_sha256(raw)}"'
        self._held.put(item.key, etag, raw, "")
        return Got(200, raw, etag, fresh=True)

    # --- what is held ----------------------------------------------------------------------------------------------------

    def forget(self, refused: bool = False) -> None:
        """Drop everything held: after a refusal nothing held is served again. After a refusal of the device or the
        account (`refused`), every sheet on this computer goes too (the shell route's files and the specimen folders)."""
        self._held.clear()
        with self._lock:
            self._index = None
        if refused:
            specimens.sweep(everything=True)
            from .. import chatbuild
            chatbuild.drop_licence_copy()
            self._remove_sheet_files(None)

    def close(self) -> None:
        """At the process's exit: nothing held survives it, and no sheet file either."""
        self._closed = True
        self.forget()
        self._remove_sheet_files(None)

    # --- the index --------------------------------------------------------------------------------------------------------

    def index(self, skill: str = "deck-design", role: str | None = None, *, held_only: bool = False) -> dict | None:
        """The skill's published index for the role: one request (conditional), or with `held_only` the one last
        answered (for a tool list, which is no call). None when there is none (signed out, offline, an older service)."""
        if skill != "deck-design":
            return None
        role = commands._role(role) or "lead"
        item = Item("index", skill, role=role)
        if held_only:
            kept = self._held.get(item.key)
            return parse_index(kept[1], kept[0]) if kept else None
        got = self.fetch([item], f"list {skill}")[0]
        if got.status != 200:
            return None
        fresh = parse_index(got.body or b"", got.etag)
        if role == "lead":
            self._adopt(fresh)
        return fresh

    def _adopt(self, fresh: dict | None) -> None:
        """The lead's deck-design index as last answered; the relays are told when its names changed."""
        if fresh is None:
            return
        with self._lock:
            current, self._index = self._index, fresh
        names = lambda index: {k: v for k, v in (index or {}).items() if k not in ("etag", "version")}
        if (current is None or names(current) != names(fresh)) and self._notify is not None:     # first names, or new ones
            with contextlib.suppress(Exception):
                self._notify()

    def _quiet_index(self) -> None:
        with contextlib.suppress(Exception):
            self.index("deck-design")

    # --- guide -------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _bindings_sent(bindings: str) -> str:
        return "launcher" if bindings == "launcher" else "tool"

    def guide(self, skill: str, sections: list[str] | None, role: str | None, *, session: str | None = None,
              bindings: str = "tool") -> list[str]:
        """Whole texts in the order asked, in one request: `sections` None (or empty) is the skill's entry text; for pptx
        that is the manual (as `guide pptx` prints it), which a section named like the manual also gives."""
        del session
        if role is not None and not isinstance(role, str):
            raise core.usage("role is a string.")
        role = commands._role(role)
        if skill not in SKILLS:
            raise core.usage(f"unknown skill {skill!r}. The skills are: {', '.join(SKILLS)}.")
        if bindings not in BINDINGS:
            raise ValueError(f"bindings is one of {BINDINGS}")
        self._require(skill)
        wanted: list[str | None] = list(sections) if sections else [None]
        plan: list[tuple[str, object]] = []          # ("manual", None) or ("section", name)
        items: list[Item] = []
        reader = role or "lead"
        for section in wanted:
            if section is not None and not isinstance(section, str):
                raise core.usage("section is a string.")
            if skill == "pptx" and (not section or commands._section_name(section).lower() in commands.MANUAL_NAMES):
                plan.append(("manual", None))
                continue
            name = commands._section_name(section) if section else ""
            if section and not commands.SECTION_NAME.fullmatch(name):
                raise commands._no_such_section(skill, name, role, self)
            plan.append(("section", name))
            item = Item("section", skill, name, reader, self._bindings_sent(bindings))
            if item not in items:
                items.append(item)
        manual = None
        if any(kind == "manual" for kind, _n in plan):
            manual = commands.PptxManual.prepare(role)       # the engine, its manual on disk or not, the items it needs
            items += [item for item in manual.items() if item not in items]
        if manual is not None and len(items) == len(manual.items()):
            # The manual alone: offline, an engine's own manual is still printed, with the line that says what is missing.
            manual_got = manual.fetch(self)
            got = manual_got or {}
        else:
            got = dict(zip(items, self.fetch(items, f"guide {skill}"))) if items else {}
            manual_got = got
        out = []
        for kind, name in plan:
            if kind == "manual":
                out.append(manual.document(manual_got, opening=False, notices=bindings != "chat", pointers=bindings != "chat",
                                           bindings="tool" if bindings == "tool" else "launcher"))
                continue
            answer = got[Item("section", skill, name, reader, self._bindings_sent(bindings))]
            if answer.status == 404:
                on_disk = commands._pptx_disk_section(name) if skill == "pptx" and name else None
                if on_disk is None:                      # neither served nor among the engine's own files
                    raise commands._no_such_section(skill, name, role, self)
                _headers, raw = commands._pptx_from_disk(on_disk)
                out.append(raw.decode("utf-8", errors="replace"))
                continue
            if skill == "deck-design" and answer.fresh and not name:
                self._seen_version(answer.version)
            out.append(answer.text())
        return out

    def _seen_version(self, version: str) -> None:
        """A new entry named a guide version that is not the held index's: the index is asked again in the background."""
        with self._lock:
            index = self._index
        if version and index is not None and index.get("version") and version != index["version"]:
            threading.Thread(target=self._quiet_index, name="texts-index", daemon=True).start()

    # --- specimens -------------------------------------------------------------------------------------------------------

    def specimens(self, names: list[str], *, session: str | None = None) -> list[tuple[str, bytes | None, str | None]]:
        """For each file name asked (already checked by the caller), in order: (name, the JPEG, None), or (name, None, a
        note naming the nearest real names) for a sheet the service does not have. One request; any refusal raises."""
        del session
        self._require("deck-design")
        wanted = list(dict.fromkeys(names))
        got = dict(zip(wanted, self.fetch([Item("sheet", "deck-design", name) for name in wanted], "specimen")))
        missing = [name for name in wanted if got[name].status != 200]
        listed = None
        if missing:
            # The nearest names for the note: the index this process holds (names only, confirmed at session start),
            # else one request more for it, only because a name was not found.
            index = self.index("deck-design", held_only=True)
            if index is None:
                with contextlib.suppress(Status):
                    index = self.index("deck-design")
            listed = (index or {}).get("specimens") or None
        return [(name, got[name].body if got[name].status == 200 else None,
                 None if got[name].status == 200 else specimens.missing_note(name, listed)) for name in names]

    # --- the shell route's sheet files ------------------------------------------------------------------------------------

    @staticmethod
    def _sheets_root() -> Path:
        return specimens.sheets_root()

    @classmethod
    def _session_folder(cls, session: str | None) -> Path:
        label = re.sub(r"[^A-Za-z0-9._-]", "_", session or "shell")[:64].strip(".") or "shell"
        return cls._sheets_root() / label

    def sheet_files(self, names: list[str], *, session: str | None = None) -> list[tuple[str, Path | None, str | None]]:
        """The sheets as files for a shell: (name, path, None) or (name, None, note), from one request. The folder is
        the session's, 0700, each file 0600, named `<ETag>-<name>`: a later call of the session that is answered 304
        prints the same file again (so a process of its own, which holds nothing in memory, still costs no download),
        and a changed sheet is a new file. Files go when the session ends or 15 minutes after their last use."""
        folder = self._session_folder(session)
        self._adopt_files(folder, names)
        results = self.specimens(names, session=session)
        out: list[tuple[str, Path | None, str | None]] = []
        for name, raw, note in results:
            if raw is None:
                out.append((name, None, note))
                continue
            kept = self._held.get(("sheet", name))
            tag = _etag_hex(kept[0]) if kept is not None and kept[1] == raw else hashlib.sha256(raw).hexdigest()[:32]
            path = self._private_folder(session) / f"{tag}-{Path(name).name}"
            try:
                out.append((name, self._write_sheet(path, raw), None))
            except FileNotFoundError:   # the folder went while the sheets were written (an uninstall, a sign-out elsewhere)
                raise Status("RETRY", f"specimen {name}: this session's folder for sheets was removed while they were written; "
                             f"nothing more was kept.", "run the same command again once. If it fails again, continue without the sheet.",
                             "A design specimen could not be opened just now; I am trying once more.") from None
        return out

    @staticmethod
    def _write_sheet(path: Path, raw: bytes) -> Path:
        """A new file holding `raw`, or the file of that name when it holds exactly these bytes (another call of the
        session wrote it); its 15 minutes start again."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
                handle.write(raw)
            return path
        except FileExistsError:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and info.st_size == len(raw) and path.read_bytes() == raw:
                os.utime(path)
                return path
            raise core.internal(f"a sheet file of this session ({path.name}) holds other bytes; nothing was written.") from None

    def _adopt_files(self, folder: Path, names: list[str]) -> None:
        """This session's files of the sheets asked for, written within 15 minutes, become held copies, so the call's
        request is conditional (a process of its own holds nothing in memory). Only regular files in a folder of this
        user's; the request still decides whether one is served."""
        try:
            info = os.lstat(folder)
            if not stat.S_ISDIR(info.st_mode) or (not core.IS_WINDOWS and info.st_uid != os.getuid()):
                return
            entries = list(os.scandir(folder))
        except OSError:
            return
        now = core.now()
        for name in dict.fromkeys(names):
            if self._held.get(("sheet", name)) is not None:
                continue
            best = None
            for entry in entries:
                match = _SHEET_FILE.fullmatch(entry.name)
                if not match or match.group(2) != name:
                    continue
                with contextlib.suppress(OSError):
                    found = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(found.st_mode) and 0 < found.st_size <= specimens.MAX_BYTES \
                            and 0 <= now - found.st_mtime <= SHEET_FILES_KEEP_S and (best is None or found.st_mtime > best[1]):
                        best = (entry.path, found.st_mtime, match.group(1))
            if best is not None:
                with contextlib.suppress(OSError):
                    data = Path(best[0]).read_bytes()
                    self._held.put(("sheet", name), _etag_of(best[2]), data, "")

    def _private_folder(self, session: str | None) -> Path:
        """This session's folder for sheet files: made here 0700 (on Windows, this user's alone) or, when it exists, one
        that is a real folder of this user's, never a link."""
        root = self._sheets_root()
        root.mkdir(parents=True, exist_ok=True)
        folder = self._session_folder(session)
        try:
            os.mkdir(folder, 0o700)
            made = True
        except FileExistsError:
            made = False
        info = os.lstat(folder)
        if not stat.S_ISDIR(info.st_mode) or (not core.IS_WINDOWS and info.st_uid != os.getuid()):
            raise core.internal(f"the folder for this session's sheets ({folder}) is not Theodore's own; nothing was written.")
        if made:
            try:
                specimens._make_private(folder)         # POSIX 0700; Windows: this user's access list alone
                if not core.IS_WINDOWS:
                    os.chmod(root, 0o700)
            except (OSError, subprocess.SubprocessError) as exc:
                core.remove_tree(folder)
                raise core.internal(f"the folder for this session's sheets could not be made private ({exc}); nothing was written.") from None
        return folder

    def _remove_sheet_files(self, session: str | None) -> None:
        target = self._sheets_root() if session is None else self._session_folder(session)
        with contextlib.suppress(OSError):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)

    def sweep(self) -> None:
        """The shell route's sheet files older than 15 minutes go, and the session folders left empty (the service's
        timer, and every shell call)."""
        specimens.sweep_sheet_files()

    # --- sessions and the connection ------------------------------------------------------------------------------------

    def session_open(self, session_id: str | None, host: str | None) -> None:
        """A session began: one connection is opened and one cheap authorised request (the index, conditional) is sent
        in the background, so the session's first call finds a connection ready. Never blocks, never raises."""
        del session_id, host
        if self._closed:
            return
        threading.Thread(target=self._quiet_index, name="texts-session-open", daemon=True).start()

    def keep_warm(self) -> None:
        """While a relay is attached (the service's timer, every 30 to 60 s): a connection idle for WARM_IDLE_S gets one
        request, so a proxy or the edge does not close it. The request is the public key list (nothing counted, nothing
        of the account), unless the index is due anyway."""
        if self._closed or time.monotonic() - self._last_request < WARM_IDLE_S:
            return
        def run() -> None:
            with contextlib.suppress(Exception):
                self._last_request = time.monotonic()
                self.pool.request("GET", api.url_for("keys"), timeout=10.0)
        threading.Thread(target=run, name="texts-keep-warm", daemon=True).start()

    def session_closed(self, session_id: str | None) -> None:
        """A session ended: its shell sheet files go."""
        if session_id:
            self._remove_sheet_files(session_id)

    # --- skill bodies ----------------------------------------------------------------------------------------------------

    def skill_body(self, name: str, *, session: str | None = None, host: str | None = None, check: bool = False,
                   bindings: str | None = None) -> str:
        """A skill's instructions as a skill file's body, with the update notices first (what `theodore skill` prints),
        from one request. `check`: the one-shot command's update check and heartbeat (the service has timers).
        `bindings` "tool": the session's relay said hello, so its reader has the plugin's tools (the pptx manual's
        header and deck-design's links then name them); None decides by the host."""
        del session
        if name not in SKILLS:
            raise core.usage(f"unknown skill '{name}'. The skills are: {', '.join(SKILLS)}.")
        body, _version = commands.skill_text(name, self, host=host, check=check, bindings=bindings)
        notices = [line for line in (install.take_announcement(), install.plugin_notice(daily=True)) if line]
        if notices:
            body = "\n".join(notices) + "\n" + body
        return body if body.endswith("\n") else body + "\n"


# --- the process's Texts ------------------------------------------------------------------------------------------------

_CURRENT: Texts | None = None
_CURRENT_LOCK = threading.Lock()


def use(texts: Texts | None) -> None:
    """The service installs its own Texts (with its credential and licence holder); None goes back to the default."""
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = texts


def current() -> Texts:
    """This process's Texts: the service's, else a default one made on first use (a one-shot command)."""
    global _CURRENT
    with _CURRENT_LOCK:
        if _CURRENT is None:
            _CURRENT = Texts()
        return _CURRENT
