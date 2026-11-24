"""Texts and sheets for the service's tools (guide, specimen, list) and the skill bodies: held in memory only.

One `Texts` per service process, shared by every session and thread. This is today's logic, moved from mcp.py
unchanged in what it asks the Theodore service and when (lane A); lane D owns this file from here and replaces its
internals (the kept-alive connections, the batch route, prefetch, transient sheet files: contract C6) behind the same
methods. Nothing here prints and nothing of a text or a sheet is written to disk, except the Bash route's transient
sheet files (`sheet_files`), which live in `<home>/work/scratch/sheets/<session>/` and go with the session.

Every method may raise `core.Status`. Held items follow the rules mcp.py had: a held guide text is asked for again with
If-None-Match on every call; held sheets belong to the published guide version the index names and are confirmed by one
conditional index request at most REVALIDATE_SECONDS after the last; a refusal drops everything held.
"""

from __future__ import annotations

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

REVALIDATE_SECONDS = 60.0          # held sheets are served for at most this long after the service last confirmed them
SPECIMEN_MAX_PER_CALL = 6
# What one process holds at most: the sheets and the references of one guide version. A version holds at most 64
# sheets of at most 1 MiB each (the service's limits); references are whole sections of at most 400,000 characters.
HELD_SHEETS_MAX = 64
HELD_SHEET_BYTES_MAX = 64 << 20
HELD_GUIDES_MAX = 64
HELD_GUIDE_CHARS_MAX = 16 << 20
# How a text names its links: "tool" (the MCP tools' wording), "launcher" (the shell route's), "chat" (the tool wording,
# and the pptx manual without update notices or launcher pointers, as the chat's one tool had it: mcp FOR == DESKTOP).
BINDINGS = ("tool", "launcher", "chat")
SHEET_FILES_KEEP_S = 15 * 60       # the Bash route's transient sheet files


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
    """See the module's text. `credential()` returns the installation secret (from memory in the service) and raises
    SIGN_IN_REQUIRED when there is none; `require(skill)` checks the licence claims for a skill; `notify()` is called
    when the index's names changed (the service then sends tools/list_changed to every relay)."""

    def __init__(self, credential=None, require=None, notify=None):
        self._credential = credential or _default_credential
        self._require = require or account.require
        self._notify = notify
        self._lock = threading.RLock()
        self._index_fetch = threading.Lock()       # one index fetch at a time
        self._index: dict | None = None            # deck-design's published index, when known
        self._index_tried = False                  # a specimen call asks for a missing index once, not on every call
        self._sheets: dict[str, tuple[str, bytes, str | None]] = {}      # name -> (sha256, bytes, index ETag fetched under)
        self._guides: dict[tuple[str, str, str], tuple[str, str]] = {}   # (skill, section, role or "") -> (ETag, text)
        self._validated_at: float | None = None    # time.monotonic() of the last confirmation of the index
        self._closed = False

    # --- requests ---------------------------------------------------------------------------------------------------

    def _api(self, route: str, what: str, *, passing: tuple[str, ...] = (), keep=None, **kwargs):
        """commands._api with the secret from `credential()`: one authenticated call; every refusal becomes its status,
        except the error codes in `passing` and the errors `keep(error)` accepts."""
        try:
            return api.call(route, bearer=self._credential(), **kwargs)
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

    def _revalidate_seconds(self) -> float:
        """REVALIDATE_SECONDS. Test seam: THEODORE_TEST_REVALIDATE_SECONDS, honoured only when the API base is loopback."""
        raw = os.environ.get("THEODORE_TEST_REVALIDATE_SECONDS")
        if raw and core.is_loopback_api():
            with contextlib.suppress(ValueError):
                return float(raw)
        return REVALIDATE_SECONDS

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

    def _hold_sheet(self, name: str, value: tuple[str, bytes, str | None]) -> None:
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
            self._validated_at = None
            self._index = None              # fetched again (and confirmed) before anything is held again
            self._index_tried = False
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

    def _fetch_index(self) -> dict | None:
        """GET /v1/guide/deck-design?form=index, or None when signed out, offline, or from a service without the index
        (then the tools take any name, as before)."""
        try:
            secret = self._credential()
            _s, headers, raw = api.call("guide", bearer=secret, skill="deck-design", query={"form": "index"}, timeout=5.0)
        except Exception:       # noqa: BLE001  no index: the tools still work by name
            return None
        return parse_index(headers, raw)

    def _adopt(self, fresh: dict | None) -> None:
        """Take a newly fetched index: held sheets of another version are no longer served unchecked, and the relays
        are told when the names changed."""
        if fresh is None:
            return
        with self._lock:
            current = self._index
            if current is not None and fresh.get("etag") != current.get("etag"):     # another version: nothing of the old one is kept
                self._sheets.clear()
                self._guides.clear()
            changed = current is not None and {k: v for k, v in fresh.items() if k != "etag"} != \
                {k: v for k, v in current.items() if k != "etag"}
            self._index = fresh
            self._validated_at = time.monotonic() if fresh.get("etag") else None
        if changed and self._notify is not None:
            with contextlib.suppress(Exception):
                self._notify()

    def index(self, skill: str = "deck-design", role: str | None = None) -> dict | None:
        """The published index of `skill` (deck-design only today; `role` is not sent yet), fetched once and held.
        One fetch at a time: a caller that comes while it is on its way waits for it (the service asks at session open
        and for the relay's names at once)."""
        del role
        if skill != "deck-design":
            return None
        with self._index_fetch:
            with self._lock:
                if self._index is not None:
                    return self._index
                self._index_tried = True
            fresh = self._fetch_index()
            with self._lock:
                if self._index is None:
                    self._index = fresh
                    self._validated_at = time.monotonic() if fresh and fresh.get("etag") else None
                return self._index

    def _seen_version(self, version: str | None) -> None:
        """A guide answer named its guide version: when it is not the index's, fetch the index again and say so."""
        with self._lock:
            index = self._index
        if not version or index is None or not index.get("version") or version == index["version"]:
            return
        fresh = self._fetch_index()
        if fresh and fresh != index:
            self._adopt(fresh)

    def _validate(self) -> str | None:
        """The index ETag under which held sheets may be served now, or None (no index: each held sheet is checked on its
        own). Past the interval, one conditional index request: 304 confirms; a new version demotes the held sheets; a
        refusal or a failure raises its status (and a refusal drops everything held)."""
        with self._lock:
            etag = (self._index or {}).get("etag")
            fresh_enough = self._validated_at is not None and 0 <= time.monotonic() - self._validated_at < self._revalidate_seconds()
        if not etag:
            return None
        if fresh_enough:
            return etag
        try:
            status, headers, raw = self._api("guide", "specimen", skill="deck-design", query={"form": "index"},
                                             headers={"If-None-Match": etag})
        except Status as status:
            if status.name not in ("OFFLINE", "RETRY"):         # refused, or no published version: nothing held is kept
                self.forget()
            raise
        if core.refused():
            raise api.sign_in_required()
        if status == 304:
            with self._lock:
                self._validated_at = time.monotonic()
            return etag
        fresh = parse_index(headers, raw)
        if fresh is None:               # the service no longer serves the index: check sheets one by one
            with self._lock:
                self._index = {k: v for k, v in (self._index or {}).items() if k != "etag"}
            return None
        self._adopt(fresh)
        return fresh.get("etag")

    # --- guide ---------------------------------------------------------------------------------------------------------

    def _fetch_whole(self, skill: str, section: str | None, role: str | None, bindings: str) -> str:
        """The whole document as the service sends it: one answer with form=whole; an older service that still pages
        is asked for every part, joined in order. `role` "slide": the text as composed for an agent given one slide."""
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
            status, headers, raw = self._api("guide", what, skill=skill, query=query, headers=conditional)
            route, extra = "guide", {}
        if core.refused():
            raise api.sign_in_required()
        if status == 304 and held:
            return held[1]
        if skill == "deck-design":         # the index names deck-design's version; a served pptx section has its own
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
        """The pptx manual as the launcher prints it, whole, in one document (it comes with the engine); a tool result
        needs none of the opening lines a long command output gets. On the chat route it carries no update notice and
        no launcher pointers (a chat has no launcher)."""
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
        wanted = list(sections) if sections else [None]
        texts = []
        for section in wanted:
            if section is not None and not isinstance(section, str):
                raise core.usage("section is a string.")
            if skill == "pptx" and (not section or commands._section_name(section).lower() in commands.MANUAL_NAMES):
                texts.append(self._pptx_manual(role, bindings))     # the main text, also by the name an engine message gives it
            else:
                texts.append(self._fetch_whole(skill, section or None, role, bindings))
        return texts

    # --- specimens -----------------------------------------------------------------------------------------------------

    def _sheet(self, name: str, current: str | None) -> bytes:
        """One sheet's bytes, checked (JPEG, at most 1 MiB, the SHA-256 the service sent). Held under the index ETag
        `current` (just confirmed): served from memory. Held otherwise: one conditional request (304: from memory).
        Not held: one fetch."""
        with self._lock:
            held = self._sheets.get(name)
        if held and current and held[2] == current:
            return held[1]
        what = f"specimen {name}"
        asked = {"Accept": "image/jpeg, application/json;q=0.9"}
        if held:
            asked["If-None-Match"] = f'"{held[0]}"'
        try:
            status, headers, raw = self._api("guide_specimen", what, passing=("not_found",), file=name, query={"skill": "deck-design"},
                                             headers=asked, redirects=False, limit=specimens.MAX_BYTES)
        except api.TooLarge:
            raise core.internal(f"{what}: the answer is larger than 1 MiB; it was refused.") from None
        except api.ApiError as error:       # passed through only for not_found: said beside the sheets that came
            with self._lock:
                self._sheets.pop(name, None)
            if error.code != "not_found":
                raise api.status_from(error, what) from None
            raise specimens.NoSuchSheet(name) from None
        if core.refused():
            raise api.sign_in_required()
        if status == 304 and held:
            self._hold_sheet(name, (held[0], held[1], current))
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
        self._hold_sheet(name, (specimens.content_sha256(raw), raw, current))     # the published sheet's hash: If-None-Match
        return raw

    def specimens(self, names: list[str], *, session: str | None = None) -> list[tuple[str, bytes | None, str | None]]:
        """For each file name asked (already checked by the caller), in order: (name, the JPEG, None), or (name, None, a
        note naming the nearest real names) for a sheet the service does not have. Any other refusal raises."""
        del session
        self._require("deck-design")
        with self._lock:
            want_index = self._index is None
        if want_index:                  # asked for once, or waited for while the session's own request is on its way
            if not self._index_tried or self._index_fetch.locked():
                self.index("deck-design")
        sheets: dict[str, bytes] = {}
        missing: list[str] = []
        for name in names:
            if name not in sheets and name not in missing:
                # The confirmation's age is checked for every sheet served, so no batch serves a held sheet more than
                # REVALIDATE_SECONDS after the service last confirmed it.
                try:
                    sheets[name] = self._sheet(name, self._validate())
                except specimens.NoSuchSheet:
                    missing.append(name)
        if core.refused():
            raise api.sign_in_required()
        with self._lock:
            listed = (self._index or {}).get("specimens") or None
        notes = {name: specimens.missing_note(name, listed) for name in missing}
        return [(name, sheets.get(name), None if name in sheets else notes.get(name)) for name in names]

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
        """The sheets as files for a shell (the Bash route): (name, path, None) or (name, None, note). The folder is
        this session's, 0700, each file 0600; it goes when the session ends or after SHEET_FILES_KEEP_S."""
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
        root = self._sheets_root()
        target = root if session is None else self._session_folder(session)
        with contextlib.suppress(OSError):
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

    def sweep(self) -> None:
        """Transient sheet folders older than SHEET_FILES_KEEP_S go (the service's prune timer calls this)."""
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
        """A session began: the index is fetched in the background when it is not held (one cheap authorised request,
        which also fills the tools' names). Never blocks, never raises."""
        del session_id, host
        with self._lock:
            if self._index is not None or self._closed:
                return
        threading.Thread(target=self._quiet_index, name="texts-session-open", daemon=True).start()

    def _quiet_index(self) -> None:
        with contextlib.suppress(Exception):
            self.index("deck-design")

    def session_closed(self, session_id: str | None) -> None:
        """A session ended: its transient sheet files go."""
        if session_id:
            self._remove_sheet_files(session_id)

    def prefetch(self, kind: str, names: list[str], *, session: str | None = None, role: str | None = None) -> None:
        """Warm what a step will read, in the background: kind "sections" (deck-design sections) or "sheets". Best
        effort: nothing is raised or printed."""
        def run() -> None:
            with contextlib.suppress(Exception):
                if kind == "sections":
                    self.guide("deck-design", list(names), role, session=session)
                elif kind == "sheets":
                    self.specimens(list(names)[:SPECIMEN_MAX_PER_CALL], session=session)
        if names:
            threading.Thread(target=run, name=f"texts-prefetch-{kind}", daemon=True).start()

    # --- skill bodies ----------------------------------------------------------------------------------------------------

    def skill_body(self, name: str, *, session: str | None = None, host: str | None = None) -> str:
        """A skill's instructions as a skill file's body, with the update notices first (what `theodore skill` prints)."""
        del session, host
        if name not in SKILLS:
            raise core.usage(f"unknown skill '{name}'. The skills are: {', '.join(SKILLS)}.")
        body, _version = commands._deck_design_body() if name == "deck-design" else commands._pptx_body()
        notices = [line for line in (install.take_announcement(), install.plugin_notice(daily=True)) if line]
        if notices:
            body = "\n".join(notices) + "\n" + body
        return body if body.endswith("\n") else body + "\n"
