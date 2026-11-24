"""Texts and sheets for the service's tools (guide, specimen, list) and the skill bodies: held in memory only.

One `Texts` per service process, shared by every session and thread. This is the logic of the 0.4.34 MCP server
(lane L1's mcp.py: every call online and authorised, conditional when its body is held, side by side, over the
kept-alive connections), moved behind the methods agreed with lane D; lane D owns this file and replaces it whole
(contract C6: the batch route, one request per call). Nothing here prints and nothing of a text or a sheet is written to
disk, except the Bash route's transient sheet files (`sheet_files`), which go with their session.

Every method may raise `core.Status`. A refusal drops everything held.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from .. import account, api, commands, core, credentials, install, specimens
from ..contract import HEADER_GUIDE_PARTS, HEADER_GUIDE_VERSION, SKILLS
from ..core import Status
from . import capture

SPECIMEN_MAX_PER_CALL = 6
# What one process holds at most: the sheets and the references of one guide version. A version holds at most 64
# sheets of at most 1 MiB each (the service's limits); references are whole sections of at most 400,000 characters.
HELD_SHEETS_MAX = 64
HELD_SHEET_BYTES_MAX = 64 << 20
HELD_GUIDES_MAX = 64
HELD_GUIDE_CHARS_MAX = 16 << 20
# How a text names its links: "tool" (the MCP tools' wording), "launcher" (the shell route's), "chat" (the tool wording,
# and the pptx manual without update notices or launcher pointers, as the chat's one tool had it).
BINDINGS = ("tool", "launcher", "chat")
SHEET_FILES_KEEP_S = 15 * 60       # the Bash route's transient sheet files

_SHARED: "Texts | None" = None


def use(texts: "Texts | None") -> None:
    """The service's instance: commands that run in the service share it (its memory and connections)."""
    global _SHARED
    _SHARED = texts


def current() -> "Texts":
    """The shared instance, or one of this process's own (a one-shot client, a relay answering by itself)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = Texts()
    return _SHARED


def _default_credential() -> str:
    """The installation secret (kept in memory by credentials.keep_in_memory in the service)."""
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


def parse_index(headers, raw: bytes) -> dict | None:
    """{version, sections, specimens, etag} from GET /v1/guide/deck-design?form=index, or None."""
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


class Texts:
    """See the module's text. `credential()` returns the installation secret and raises SIGN_IN_REQUIRED when there is
    none; `require(skill)` checks the licence claims for a skill; `notify()` is called when the index first arrives or
    its names change (the service then sends tools/list_changed to every relay)."""

    def __init__(self, credential=None, require=None, notify=None, pool=None):
        self._credential = credential or _default_credential
        self._require = require or account.require
        self._notify = notify
        self._pool = pool
        self._lock = threading.RLock()
        self._index_fetch = threading.Lock()       # one index request at a time
        self._index: dict | None = None            # deck-design's published index, when known
        self._sheets: dict[str, tuple[str, bytes]] = {}                  # name -> (sha256 published, bytes)
        self._guides: dict[tuple[str, str, str], tuple[str, str]] = {}   # (skill, section, role or "") -> (ETag, text)
        self._closed = False

    # --- requests ---------------------------------------------------------------------------------------------------

    def _api(self, route: str, what: str, *, passing: tuple[str, ...] = (), keep=None, **kwargs):
        """commands._api with the secret from `credential()`: one authenticated call; every refusal becomes its status,
        except the error codes in `passing` and the errors `keep(error)` accepts."""
        try:
            return api.call(route, bearer=self._credential(), **({"pool": self._pool} if self._pool else {}), **kwargs)
        except api.NetworkError as error:
            if keep is not None and keep(error):
                raise
            raise api.offline(error, what) from None
        except api.ApiError as error:
            if error.code in passing or (keep is not None and keep(error)):
                raise
            if error.code in ("installation_revoked", "unauthenticated"):
                credentials.delete()
                account.delete_token()
                raise account.login_start("this device was signed out; it needs to sign in again.") from None
            if error.http_status in (401, 403) and error.code != "auto_reports_disabled":
                account.delete_token()
            raise api.status_from(error, what) from None

    # --- what is held ------------------------------------------------------------------------------------------------

    @staticmethod
    def _hold(store: dict, key, value, size, max_entries: int, max_size: int) -> None:
        """Keep `value` under `key`, newest last; evict the oldest entries beyond the count or the total size."""
        store.pop(key, None)
        store[key] = value
        while len(store) > max_entries or (len(store) > 1 and sum(size(v) for v in store.values()) > max_size):
            store.pop(next(iter(store)))
        if sum(size(v) for v in store.values()) > max_size:      # one item larger than the whole bound: not held
            store.pop(key, None)

    def _hold_sheet(self, name: str, value: tuple[str, bytes]) -> None:
        with self._lock:
            self._hold(self._sheets, name, value, lambda v: len(v[1]), HELD_SHEETS_MAX, HELD_SHEET_BYTES_MAX)

    def _hold_guide(self, key: tuple[str, str, str], value: tuple[str, str]) -> None:
        with self._lock:
            self._hold(self._guides, key, value, lambda v: len(v[1]), HELD_GUIDES_MAX, HELD_GUIDE_CHARS_MAX)

    def forget(self, refused: bool = False) -> None:
        """Drop everything held: after a refusal nothing held is served again. After a refusal of the device or the
        account (`refused`), the specimen folders on this computer go too: it keeps no sheet anywhere."""
        with self._lock:
            self._sheets.clear()
            self._guides.clear()
            self._index = None              # fetched again before its names are used
        if refused:
            specimens.sweep(everything=True)
            from .. import chatbuild
            chatbuild.drop_licence_copy()
            self._remove_sheet_files(None)

    def close(self) -> None:
        """At the service's exit: nothing held survives it."""
        self._closed = True
        self.forget()
        self._remove_sheet_files(None)

    # --- the index -----------------------------------------------------------------------------------------------------

    def _adopt(self, fresh: dict | None) -> None:
        """Take a newly fetched index: another version keeps nothing of the old one; the relays are told when the
        names first arrive or change."""
        if fresh is None:
            return
        with self._lock:
            current = self._index
            if current is not None and fresh.get("etag") != current.get("etag"):
                self._sheets.clear()
                self._guides.clear()
            changed = current is None or {k: v for k, v in fresh.items() if k != "etag"} != \
                {k: v for k, v in current.items() if k != "etag"}
            self._index = fresh
        if changed and self._notify is not None:
            with contextlib.suppress(Exception):
                self._notify()

    def index(self, skill: str = "deck-design", role: str | None = None, *, held_only: bool = False) -> dict | None:
        """deck-design's published index: with `held_only`, what is held (no request); else asked for in this call,
        conditionally when one is held (304 keeps it). A refusal raises its status and drops everything held; a service
        without the index gives None."""
        del role
        if skill != "deck-design":
            return None
        if held_only:
            with self._lock:
                return self._index
        with self._index_fetch:
            with self._lock:
                held = self._index
            etag = (held or {}).get("etag")
            try:
                status, headers, raw = self._api("guide", "list deck-design", skill="deck-design", query={"form": "index"},
                                                 headers={"If-None-Match": etag} if etag else None)
            except Status as refusal:
                if refusal.name not in ("OFFLINE", "RETRY"):
                    self.forget()
                raise
            if core.refused():
                raise api.sign_in_required()
            if status == 304 and held is not None:
                return held
            fresh = parse_index(headers, raw)
            if fresh is not None:
                self._adopt(fresh)
            return fresh

    def _quiet_index(self) -> None:
        """The index for the names, asked once when none is held; failures are silent (the tools take any name)."""
        with contextlib.suppress(Exception):
            with self._lock:
                if self._index is not None:
                    return
            self.index("deck-design")

    def _seen_version(self, version: str | None) -> None:
        """A guide answer named its guide version: when it is not the index's, ask for the index again."""
        with self._lock:
            index = self._index
        if not version or index is None or not index.get("version") or version == index["version"]:
            return
        with contextlib.suppress(Exception):
            self.index("deck-design")

    # --- guide ---------------------------------------------------------------------------------------------------------

    def _fetch_whole(self, skill: str, section: str | None, role: str | None, bindings: str) -> str:
        """The whole document as the service sends it, asked for in this call (conditional when held)."""
        self._require(skill)
        query = {"form": "whole", **({"bindings": "tool"} if bindings in ("tool", "chat") else {}), **({"role": role} if role else {})}
        what = f"guide {skill}"
        name = ""
        if section:
            name = commands._section_name(section)
            if not commands.SECTION_NAME.fullmatch(name):
                raise commands._no_such_section(skill, name, role)
        key = (skill, name if section else "", role or "")
        with self._lock:
            held = self._guides.get(key)
        conditional = {"If-None-Match": held[0]} if held else None
        if section:
            try:
                status, headers, raw = self._api("guide_section", what, passing=("not_found",), skill=skill, section=name,
                                                 query=query, headers=conditional)
            except api.ApiError:
                with self._lock:
                    self._guides.pop(key, None)
                on_disk = commands._pptx_disk_section(name) if skill == "pptx" else None
                if on_disk is None:
                    raise commands._no_such_section(skill, name, role) from None
                _headers, raw = commands._pptx_from_disk(on_disk)
                if core.refused():
                    raise api.sign_in_required() from None
                return raw.decode("utf-8", errors="replace")
            route, extra = "guide_section", {"section": name}
        else:
            status, headers, raw = self._api("guide", what, skill=skill, query=query, headers=conditional)
            route, extra = "guide", {}
        if core.refused():
            raise api.sign_in_required()
        if status == 304 and held:
            return held[1]
        if skill == "deck-design":
            self._seen_version(headers.get(HEADER_GUIDE_VERSION))
        chunks = [raw]
        total = max(1, int(headers.get(HEADER_GUIDE_PARTS) or 1))
        for part in range(2, total + 1):
            _s, _h, more = self._api(route, what, skill=skill, query={**query, "part": part}, **extra)
            chunks.append(more)
        if core.refused():
            raise api.sign_in_required()
        text = b"".join(chunks).decode("utf-8", errors="replace")
        etag = str(headers.get("ETag") or "").strip()
        if total == 1 and etag:
            self._hold_guide(key, (etag, text))
        else:
            with self._lock:
                self._guides.pop(key, None)
        return text

    @staticmethod
    def _pptx_manual(role: str | None, bindings: str) -> str:
        chat = bindings == "chat"
        return capture.printed(lambda: commands._guide_pptx(1, whole=True, opening=False, notices=not chat, role=role,
                                                            pointers=not chat))

    def guide(self, skill: str, sections: list[str] | None, role: str | None, *, session: str | None = None,
              bindings: str = "tool") -> list[str]:
        """Whole texts in the order asked: `sections` None (or empty) is the skill's entry text (for pptx, the manual,
        which a section named like the manual also gives). `role` is checked here ("lead", "slide" or None)."""
        del session
        if role is not None and not isinstance(role, str):
            raise core.usage("role is a string.")
        role = commands._role(role)
        if skill not in SKILLS:
            raise core.usage(f"unknown skill {skill!r}. The skills are: {', '.join(SKILLS)}.")
        if bindings not in BINDINGS:
            raise ValueError(f"bindings is one of {BINDINGS}")
        texts = []
        for section in (list(sections) if sections else [None]):
            if section is not None and not isinstance(section, str):
                raise core.usage("section is a string.")
            if skill == "pptx" and (not section or commands._section_name(section).lower() in commands.MANUAL_NAMES):
                texts.append(self._pptx_manual(role, bindings))
            else:
                texts.append(self._fetch_whole(skill, section or None, role, bindings))
        return texts

    # --- specimens -----------------------------------------------------------------------------------------------------

    def _sheet(self, name: str) -> bytes:
        """One sheet's bytes, checked (JPEG, at most 1 MiB, the SHA-256 the service sent), asked for in this call:
        held, one conditional request (304: from memory); not held, one fetch."""
        with self._lock:
            held = self._sheets.get(name)
        what = f"specimen {name}"
        asked = {"Accept": "image/jpeg, application/json;q=0.9"}
        if held:
            asked["If-None-Match"] = f'"{held[0]}"'
        try:
            status, headers, raw = self._api("guide_specimen", what, passing=("not_found",), file=name, query={"skill": "deck-design"},
                                             headers=asked, redirects=False, limit=specimens.MAX_BYTES)
        except api.TooLarge:
            raise core.internal(f"{what}: the answer is larger than 1 MiB; it was refused.") from None
        except api.ApiError as error:
            with self._lock:
                self._sheets.pop(name, None)
            if error.code != "not_found":
                raise api.status_from(error, what) from None
            raise specimens.NoSuchSheet(name) from None
        if core.refused():
            raise api.sign_in_required()
        if status == 304 and held:
            return held[1]
        with self._lock:
            self._sheets.pop(name, None)
        if not str(headers.get("Content-Type") or "").lower().startswith("image/jpeg"):
            raise core.internal(f"{what}: the answer is not a JPEG image; it was refused.")
        if str(headers.get("X-Theodore-Sha256") or "").strip().lower() != hashlib.sha256(raw).hexdigest() \
                or not specimens.content_matches(raw, headers.get("ETag")):
            raise Status("RETRY", f"{what}: the sheet did not arrive as the service sent it (sha256 differs or is missing).",
                         "call the same tool again once. If it fails again, continue without the sheet.",
                         "A design specimen arrived damaged. I am trying once more.")
        self._hold_sheet(name, (specimens.content_sha256(raw), raw))
        return raw

    def _sheet_or_missing(self, name: str) -> bytes | None:
        try:
            return self._sheet(name)
        except specimens.NoSuchSheet:
            return None

    @staticmethod
    def _side_by_side(work, items: list) -> list:
        """work(item) for each item in threads of their own, results in order; a refusal learned in a helper thread
        holds for the whole call; the first failure (in the order asked) is raised once every request has ended."""
        if len(items) <= 1:
            return [work(item) for item in items]
        learned: list = []
        scoped = getattr(core, "_SCOPED", False)

        def one(item):
            if scoped:
                core.begin_call()
            try:
                return work(item)
            finally:
                found = core.refusal() if scoped else None
                if found is not None:
                    learned.append(found)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(items), SPECIMEN_MAX_PER_CALL),
                                                   thread_name_prefix="theodore-sheet") as pool:
            futures = [pool.submit(one, item) for item in items]
            concurrent.futures.wait(futures)
        for kind, code in learned:
            core.mark_refused(kind, code)
        return [future.result() for future in futures]

    def specimens(self, names: list[str], *, session: str | None = None) -> list[tuple[str, bytes | None, str | None]]:
        """For each file name asked (already checked by the caller), in order: (name, the JPEG, None), or (name, None, a
        note naming the nearest real names) for a sheet the service does not have. Any other refusal raises."""
        del session
        self._require("deck-design")
        with self._lock:
            want_index = self._index is None
        if want_index:
            self._quiet_index()
        distinct = list(dict.fromkeys(names))
        found = dict(zip(distinct, self._side_by_side(self._sheet_or_missing, distinct)))
        if core.refused():
            raise api.sign_in_required()
        with self._lock:
            listed = (self._index or {}).get("specimens") or None
        return [(name, found.get(name), None if found.get(name) is not None else specimens.missing_note(name, listed))
                for name in names]

    # --- the Bash route's sheet files ----------------------------------------------------------------------------------

    @staticmethod
    def _sheets_root() -> Path:
        work = os.environ.get("THEODORE_WORK_DIR")
        return (Path(work) if work else core.home().root / "work") / "scratch" / "sheets"

    @classmethod
    def _session_folder(cls, session: str | None) -> Path:
        label = re.sub(r"[^A-Za-z0-9._-]", "_", session or "shell")[:64] or "shell"
        return cls._sheets_root() / label

    def sheet_files(self, names: list[str], *, session: str | None = None) -> list[tuple[str, Path | None, str | None]]:
        results = self.specimens(names, session=session)
        folder = self._session_folder(session)
        out: list[tuple[str, Path | None, str | None]] = []
        if any(raw is not None for _n, raw, _note in results):
            folder.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(folder.parent, 0o700)
                os.chmod(folder, 0o700)
        for name, raw, note in results:
            if raw is None:
                out.append((name, None, note))
                continue
            path = folder / Path(name).name
            core.write_atomic(path, raw, 0o600)
            out.append((name, path, None))
        return out

    def _remove_sheet_files(self, session: str | None) -> None:
        target = self._sheets_root() if session is None else self._session_folder(session)
        with contextlib.suppress(OSError):
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

    def sweep(self) -> None:
        root = self._sheets_root()
        try:
            folders = list(root.iterdir()) if root.is_dir() else []
        except OSError:
            return
        for folder in folders:
            with contextlib.suppress(OSError):
                if time.time() - folder.stat().st_mtime > SHEET_FILES_KEEP_S:
                    shutil.rmtree(folder, ignore_errors=True)

    # --- sessions --------------------------------------------------------------------------------------------------------

    def session_open(self, session_id: str | None, host: str | None) -> None:
        """A session began: the index is asked for in the background when none is held (one authorised request on a
        kept-alive connection, which also fills the tools' names). Never blocks, never raises."""
        del session_id, host
        with self._lock:
            if self._index is not None or self._closed:
                return
        threading.Thread(target=self._quiet_index, name="texts-session-open", daemon=True).start()

    def session_closed(self, session_id: str | None) -> None:
        if session_id:
            self._remove_sheet_files(session_id)

    def keep_warm(self) -> None:
        """Lane D's: keeps the kept-alive connection open; nothing here (its pool keeps itself)."""

    # --- skill bodies ----------------------------------------------------------------------------------------------------

    def skill_body(self, name: str, *, session: str | None = None, host: str | None = None, check: bool = False) -> str:
        del session, host, check
        if name not in SKILLS:
            raise core.usage(f"unknown skill '{name}'. The skills are: {', '.join(SKILLS)}.")
        body, _version = commands._deck_design_body() if name == "deck-design" else commands._pptx_body()
        notices = [line for line in (install.take_announcement(), install.plugin_notice(daily=True)) if line]
        if notices:
            body = "\n".join(notices) + "\n" + body
        return body if body.endswith("\n") else body + "\n"
