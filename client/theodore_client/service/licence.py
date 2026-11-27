"""The credential and the licence claims for the service's calls (DESIGN §4.7; C6: texts are authorised online per call).

The client keeps the credential in memory in the service (credentials.keep_in_memory, read again when the files that
change with it change), and an engine command's licence check reads no credential while the stored licence holds
(account.require with allow_refresh False). Engine verbs keep today's model: the signed licence is verified by the
engine, and refreshed online in the background when it is older than an hour (account.check_in_background, run here
in a thread instead of a detached process).
"""

from __future__ import annotations

import threading
import time

from .. import account, core, credentials

REFRESH_AFTER_S = account.BACKGROUND_CHECK_SECONDS


class Licence:
    def __init__(self, service=None):
        self.service = service
        self._lock = threading.Lock()
        self._refreshing = False
        self._refresh_tried = 0.0

    def credential(self) -> str:
        """The installation secret; SIGN_IN_REQUIRED (with the sign-in link) when there is none."""
        cred = credentials.load()
        if not cred:
            raise account.login_start()
        return cred["installation_secret"]

    def require(self, feature: str) -> dict:
        """The claims of a valid licence covering `feature` (account.require, allow_refresh False), and the hourly
        background refresh when it is due."""
        claims = account.require(feature, allow_refresh=False)
        self._refresh_when_due(claims)
        return claims

    def require_texts(self, skill: str) -> dict:
        """The local check before a text request (the request itself is authorised online)."""
        return self.require(skill)

    def _refresh_when_due(self, claims: dict) -> None:
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
            account.refresh(timeout=20.0)       # a channel that moved starts the update now (service/timers._nudged)
        except api.ApiError as error:
            if self.service is not None:
                self.service.log(f"licence check: refused ({error.code})")
        except Exception as exc:        # noqa: BLE001  offline or anything else: the licence stays as it was
            if self.service is not None:
                self.service.log(f"licence check: not made ({exc.__class__.__name__})")
        finally:
            with self._lock:
                self._refreshing = False
