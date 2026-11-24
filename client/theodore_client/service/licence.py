"""The credential and the licence claims, held in memory (DESIGN §4.7; C6: texts are authorised online per call).

Today every command reads the token file and the credential store (on macOS a `security` process, 21 to 62 ms) and
checks the token's dates. The service reads them once and again only when a file that changes with them changes
(the token file, the keychain markers, the credential file, a pending activation); the claims are checked per call in
memory. Engine verbs keep today's model: the signed licence is verified by the engine, and refreshed online in the
background when it is older than an hour (account.check_in_background, run here in a thread instead of a detached
process). An expired licence, a missing feature or no credential answer exactly what account.require answers.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path

from .. import account, core, credentials

REFRESH_AFTER_S = account.BACKGROUND_CHECK_SECONDS
RELOAD_AT_LEAST_S = 300.0          # a store that changes without its marker (a keychain edited by hand) is read again


def _stat(path: Path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns, getattr(info, "st_ino", 0)


class Licence:
    def __init__(self, service=None):
        self.service = service
        self._lock = threading.Lock()
        self._keys = None
        self._loaded_at = 0.0
        self._secret: str | None = None
        self._token: str | None = None
        self._claims: dict = {}
        self._refreshing = False
        self._refresh_tried = 0.0

    def _watched(self) -> list[Path]:
        state = core.home().state
        files = [state / "entitlement.json", state / "keychain-holds", state / "keychain-revoked",
                 state / "keychain-unavailable", state / "activation.pending"]
        with contextlib.suppress(Exception):
            files.append(credentials._file())       # noqa: SLF001  the 0600 or DPAPI file, when that is the store
        return files

    def _load(self, force: bool = False) -> None:
        keys = tuple(_stat(p) for p in self._watched())
        if not force and keys == self._keys and time.monotonic() - self._loaded_at < RELOAD_AT_LEAST_S:
            return
        with contextlib.suppress(Exception):
            account.import_pending_activation()
        cred = credentials.load()
        token, claims = account.stored_token()
        self._secret = cred["installation_secret"] if cred else None
        self._token, self._claims = token, dict(claims or {})
        self._keys = tuple(_stat(p) for p in self._watched())
        self._loaded_at = time.monotonic()

    def forget(self) -> None:
        with self._lock:
            self._keys = None
            self._secret = None
            self._token, self._claims = None, {}

    # --- the credential -----------------------------------------------------------------------------------------------

    def credential(self) -> str:
        """The installation secret, from memory; SIGN_IN_REQUIRED (with the sign-in link) when there is none."""
        with self._lock:
            self._load()
            secret = self._secret
        if not secret:
            raise account.login_start()
        return secret

    # --- the claims ----------------------------------------------------------------------------------------------------

    def require(self, feature: str) -> dict:
        """account.require(feature, allow_refresh=False), from memory: the claims of a valid licence covering
        `feature`. Anything but that answer comes from account.require itself (same statuses, same refresh when
        expired)."""
        if os.environ.get(account.ENTITLEMENT_ENV):
            return account.require(feature, allow_refresh=False)
        with self._lock:
            self._load()
            secret, token, claims = self._secret, self._token, dict(self._claims)
        now = core.now()
        valid = bool(secret and token) and now < float(claims.get("exp") or 0) \
            and claims.get("aud") == account.ENTITLEMENT_AUD and feature in (claims.get("feat") or [])
        if not valid:
            claims = account.require(feature, allow_refresh=False)
            with self._lock:
                self._load(force=True)
            return claims
        self._refresh_when_due(claims)
        return claims

    def require_texts(self, skill: str) -> dict:
        """The local check before a text request (the request itself is authorised online)."""
        return self.require(skill)

    def _refresh_when_due(self, claims: dict) -> None:
        """account.check_in_background, in this process: at most one refresh an hour, never waited for."""
        if core.now() - float(claims.get("iat") or 0) < REFRESH_AFTER_S:
            return
        with self._lock:
            if self._refreshing or time.monotonic() - self._refresh_tried < REFRESH_AFTER_S:
                return
            self._refreshing = True
            self._refresh_tried = time.monotonic()
        threading.Thread(target=self._refresh, name="licence-refresh", daemon=True).start()

    def _refresh(self) -> None:
        from .. import api
        try:
            account.refresh(timeout=20.0, nudge_updates=False)
        except api.ApiError as error:
            if self.service is not None:
                self.service.log(f"licence check: refused ({error.code})")
        except Exception as exc:        # noqa: BLE001  offline or anything else: the licence stays as it was
            if self.service is not None:
                self.service.log(f"licence check: not made ({exc.__class__.__name__})")
        finally:
            with self._lock:
                self._refreshing = False
                self._keys = None           # read again: the refresh replaced (or deleted) the token
