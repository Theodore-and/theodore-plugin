"""Sign-in (browser approval or activation code), the entitlement token and its refresh, signed manifests."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time

from . import __version__, api, core, credentials, ed25519
from .contract import (ACTIVATION_CODE_PATTERN, DELETE_CREDENTIAL_ON, DELETE_TOKEN_ON, DEVICE_CLIENT_ID, ENTITLEMENT_AUD,
                       ENTITLEMENT_ENV, ENTITLEMENT_FILE_KEY)
from .core import Status, quoted_launcher


# --- signed documents --------------------------------------------------------------------------------------------------

def verify_signed(envelope: dict, what: str, schema: str | None = None, keys: dict | None = None, platform: bool = True) -> dict:
    """Check {manifest, signature, kid} against the manifest keys pinned in launcher.json; return the manifest.

    The signature covers the bytes of the `manifest` string exactly as received; it is parsed only afterwards.
    `schema` pins the kind of manifest, so one signed for another purpose cannot stand in for it, and `platform`
    pins it to this computer's platform and arch. `keys` (kid -> base64url key) replaces launcher.json's keys; the
    installer passes the keys it carries before any launcher.json exists.
    """
    manifest, signature, kid = envelope.get("manifest"), envelope.get("signature"), envelope.get("kid")
    keys = core.manifest_keys() if keys is None else {k: v for k, v in keys.items() if not core.placeholder_key(k, v)}
    if not keys:
        raise core.no_release_keys()
    if manifest is None or not signature or kid not in keys:
        raise api.setup_failed("HASH_MISMATCH", f"the {what} is not signed with a key this launcher knows")
    if isinstance(manifest, str):
        message, parsed = manifest.encode("utf-8"), None
    else:  # an object: the signature covers its canonical form (sorted keys, no spaces, UTF-8)
        message = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        parsed = manifest
    try:
        ok = ed25519.verify(ed25519.b64url_decode(keys[kid]), message, ed25519.b64url_decode(signature))
    except ValueError:
        ok = False
    if not ok:
        raise api.setup_failed("HASH_MISMATCH", f"the signature of the {what} is not valid")
    if parsed is None:
        try:
            parsed = json.loads(manifest)
        except ValueError:
            raise api.setup_failed("HASH_MISMATCH", f"the {what} is not valid JSON") from None
    if schema:
        system, arch = core.platform_arch()
        if not isinstance(parsed, dict) or parsed.get("schema") != schema or \
                (platform and (parsed.get("platform"), parsed.get("arch")) != (system, arch)):
            raise api.setup_failed("HASH_MISMATCH", f"the {what} is signed but is not the document asked for")
    return parsed


# --- entitlement token -------------------------------------------------------------------------------------------------

def _token_path():
    return core.home().state / "entitlement.json"


def decode_claims(token: str) -> dict:
    """The claims of a JWS compact token. The engine verifies the signature; the client only reads the dates."""
    try:
        _header, payload, _sig = token.split(".")
        claims = json.loads(ed25519.b64url_decode(payload))
        return claims if isinstance(claims, dict) else {}
    except (ValueError, AttributeError):
        return {}


def stored_token() -> tuple[str | None, dict]:
    env = os.environ.get(ENTITLEMENT_ENV)
    token = env or core.read_json(_token_path()).get(ENTITLEMENT_FILE_KEY)
    core.register_secret(token)
    return (token, decode_claims(token)) if token else (None, {})


def save_token(token: str, release: dict | None = None, auto_reports: bool | None = None) -> None:
    """Store the token. What is known about automatic reports is kept with it: the server's answer when it gives
    one, otherwise what was known before."""
    old = core.read_json(_token_path())
    record = {ENTITLEMENT_FILE_KEY: token, "saved_at": int(time.time())}
    if release:
        record["release"] = release
    if auto_reports is not None:
        record["auto_reports"] = {"enabled": bool(auto_reports), "source": "server", "at": int(core.now())}
    elif isinstance(old.get("auto_reports"), dict):
        record["auto_reports"] = old["auto_reports"]
    core.write_json(_token_path(), record, 0o600)


AGENT_REPORTS_FEATURE = "agent-reports"
SWITCH_FRESH_SECONDS = 3600


def automatic_reports() -> tuple[bool, str]:
    """Whether an automatic report may be sent, decided before anything is sent: (allowed, why not).

    The plan feature `agent-reports` is in the entitlement's feat claim. The user's switch is cached with the
    entitlement: from the licence refresh answer (auto_report_enabled), or from a 403 auto_reports_disabled (off).
    A cached value, on or off, is trusted for an hour; older, or dated ahead of the clock, it is "unknown", the
    caller asks the service again, and no automatic report is sent unless the fresh answer says on."""
    token, claims = stored_token()
    if token and AGENT_REPORTS_FEATURE not in (claims.get("feat") or []):
        return False, "plan"
    known = core.read_json(_token_path()).get("auto_reports") or {}
    enabled, source = known.get("enabled"), known.get("source")
    if isinstance(enabled, bool) and (source == "server" or (source == "refused" and enabled is False)):
        if 0 <= core.now() - float(known.get("at") or 0) <= SWITCH_FRESH_SECONDS:
            return (True, "") if enabled else (False, "off")
    return False, "unknown"         # not said, or said too long ago: no automatic report (a report the user asks for still goes)


def remember_automatic_reports_off() -> None:
    path = _token_path()
    record = core.read_json(path)
    if record:
        record["auto_reports"] = {"enabled": False, "source": "refused", "at": int(core.now())}
        core.write_json(path, record, 0o600)


def delete_token() -> None:
    core.remove_tree(_token_path())


def release_hint() -> dict:
    return core.read_json(_token_path()).get("release") or {}


def device_metadata() -> dict:
    system, arch = core.platform_arch()
    name = re.sub(r"[^A-Za-z0-9 ._-]", "", socket.gethostname() or "computer")[:60] or "computer"
    return {"device_name": name, "platform": system, "arch": arch, "host": core.host_name(), "client_version": __version__}


def _adopt(bundle: dict) -> None:
    """Store what both sign-in routes return: {installation_id, installation_secret, entitlement_token}."""
    core.register_secret(bundle.get("installation_secret"))
    core.register_secret(bundle.get("entitlement_token"))
    if not all(bundle.get(k) for k in ("installation_id", "installation_secret", "entitlement_token")):
        raise core.internal("the sign-in answer from the Theodore API is incomplete.")
    core.home().ensure()
    credentials.store(bundle["installation_id"], bundle["installation_secret"])
    save_token(bundle["entitlement_token"])
    if (core.home().state / "update.json").exists():   # a sign-in starts from fresh replay times (installed versions stay the floor)
        _fresh_update_state("signed in")
    core.remove_tree(core.home().state / "device.json")


def import_pending_activation() -> None:
    """The installer redeems the activation code with curl before any large download and leaves the answer in
    state/activation.pending (0600). Move it into the credential store and delete the file."""
    pending = core.home().state / "activation.pending"
    if not pending.exists():
        return
    bundle = core.read_json(pending)
    if bundle.get("installation_secret"):
        _adopt(bundle)          # the file is the only copy of the secret: it goes only once the credential is stored
    core.remove_tree(pending)


def refresh(timeout: float = 20.0) -> dict:
    """POST /v1/licence/refresh. Saves the new token. Raises ApiError / NetworkError to the caller."""
    cred = credentials.load()
    if not cred:
        raise api.sign_in_required()
    engine = core.read_json(core.home().engines / "current.json").get("version")
    body = {"client_version": __version__, "plugin_version": core.plugin_version(), "host": core.host_name()}
    if engine:
        body["engine_version"] = engine
    try:
        answer = api.call_json("licence_refresh", bearer=cred["installation_secret"], body=body, timeout=timeout)
    except api.ApiError as error:
        if error.code in DELETE_TOKEN_ON or error.http_status in (401, 403):
            delete_token()
        if error.code in DELETE_CREDENTIAL_ON:
            credentials.delete()
        raise
    token = answer.get("entitlement_token")
    if not token:
        raise core.internal("the licence refresh answer has no entitlement token.")
    release = answer.get("release") or {}
    switch = answer.get("auto_report_enabled", release.get("auto_report_enabled"))
    save_token(token, release, switch if isinstance(switch, bool) else None)
    if release.get("update_required") or release.get("kill") or (release.get("latest_version") and release["latest_version"] != engine):
        # the channel moved: let the next guide check the manifest now instead of waiting out the six hours
        from . import install          # imported here: install imports this module
        install.save_update_state(last_check=0, last_attempt=0)
    return answer


def require(feature: str, *, allow_refresh: bool = True) -> dict:
    """The claims of a valid entitlement that covers `feature`, refreshing past `rfa` when the network allows.

    No credential: SIGN_IN_REQUIRED (the device flow is started so the message carries the URL).
    Server says no: NO_ENTITLEMENT / SIGN_IN_REQUIRED / UPDATE_REQUIRED. Expired and unreachable: OFFLINE.
    """
    import_pending_activation()
    token, claims = stored_token()
    now = core.now()
    env_only = bool(os.environ.get(ENTITLEMENT_ENV))
    if not env_only:
        if not credentials.load():
            raise login_start()
        expired = not token or now >= float(claims.get("exp") or 0)
        stale = expired or now >= float(claims.get("rfa") or 0)
        if expired or (stale and allow_refresh):
            try:
                refresh(timeout=8.0 if token and now < float(claims.get("exp") or 0) else 20.0)
                token, claims = stored_token()
            except api.ApiError as error:
                if error.code in DELETE_CREDENTIAL_ON:
                    raise login_start("this device was signed out; it needs to sign in again.") from None
                raise api.status_from(error, "licence check") from None
            except api.NetworkError as error:
                if not token or now >= float(claims.get("exp") or 0):
                    days = "the offline period (7 days since the last successful check) has ended"
                    raise Status("OFFLINE", f"{days} and the Theodore service could not be reached ({error.detail}).",
                                 "ask the user to connect to the internet, then run the same command again.",
                                 "Theodore has been offline for more than 7 days and needs a connection to confirm your licence.") from None
    if not token:
        raise login_start()
    if now >= float(claims.get("exp") or 0):
        raise Status("OFFLINE", "the stored licence has expired and could not be refreshed.",
                     "ask the user to connect to the internet, then run the same command again.",
                     "Theodore needs a connection to confirm your licence.")
    if claims.get("aud") != ENTITLEMENT_AUD or feature not in (claims.get("feat") or []):
        raise Status("NO_ENTITLEMENT", f"this account's plan ({claims.get('plan', 'unknown')}) does not include {feature}.",
                     f"do not retry. Tell the user; they can check their plan at {core.site_url('/app/plan')}.",
                     f"Your Theodore plan does not include {feature}. You can check it at {core.site_name('/app/plan')}.")
    return claims


# --- sign-in ---------------------------------------------------------------------------------------------------------------

def _open_browser(url: str) -> bool:
    if os.environ.get("THEODORE_NO_BROWSER"):
        return False
    try:
        seam = os.environ.get("THEODORE_TEST_BROWSER")      # a test's own program, honoured only against a loopback API
        if seam and core.is_loopback_api():
            subprocess.Popen([seam, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif core.IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
        elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            return False
        return True
    except OSError:
        return False


def login_start(reason: str = "this computer is not signed in to Theodore.", open_browser: bool = False) -> Status:
    """Begin browser approval (or reuse the pending one) and return SIGN_IN_REQUIRED carrying the URL. The browser is
    opened only when the user asked for it (`login --open`); everything else, the installers included, prints the link
    for the assistant to relay."""
    pending_path = core.home().state / "device.json"
    pending = core.read_json(pending_path)
    fresh = False
    if not pending.get("device_code") or core.now() >= float(pending.get("expires_at") or 0):
        body = {"client_id": DEVICE_CLIENT_ID, **device_metadata()}
        try:
            answer = api.call_json("device_code", body=body)
        except api.NetworkError as error:
            return api.offline(error, "sign-in could not start")
        except api.ApiError as error:
            return api.status_from(error, "sign-in could not start")
        pending = {"device_code": answer["device_code"], "user_code": answer.get("user_code"),
                   "url": answer.get("verification_uri_complete") or answer.get("verification_uri"),
                   "interval": int(answer.get("interval") or 5),
                   "expires_at": core.now() + int(answer.get("expires_in") or 900)}
        core.home().ensure()
        core.write_json(pending_path, pending, 0o600)
        fresh = True
    opened = _open_browser(pending["url"]) if open_browser else False
    tell = "Theodore needs a one-time sign-in. " + (
        "I opened a browser page; approve it and tell me when done." if opened
        else f"Please open {pending['url']} in your browser, approve, and tell me when done.")
    return Status("SIGN_IN_REQUIRED", reason,
                  f"ask the user to open {pending['url']} and approve, then run: {quoted_launcher()} login --finish --wait 90",
                  tell)


_REFUSAL_STATUS: Status | None = None


def refusal_status() -> Status:
    """The one answer of a command that learned, on any authenticated call, that the device was revoked or the account
    suspended (core.refusal). It cannot raise: whatever fails while it is built, a fixed block is answered."""
    global _REFUSAL_STATUS
    if _REFUSAL_STATUS is None:
        kind, code = core.refusal() or ("revoked", "unauthenticated")
        try:
            built = revoked_status() if kind == "revoked" else suspended_status(code)
        except BaseException:           # noqa: BLE001  the central handler never fails
            built = _static_sign_in(kind)
        built.refusal = True
        _REFUSAL_STATUS = built
    return _REFUSAL_STATUS


_REVOKED_REASON = ("this device was signed out of Theodore (from the portal, or its sign-in is no longer valid); "
                   "its stored sign-in and licence were deleted.")


def _static_sign_in(kind: str) -> Status:
    if kind != "revoked":
        return Status("NO_ENTITLEMENT", "the Theodore account is suspended or its plan is not active; the stored licence was deleted.",
                      "do not retry. Tell the user to check their Theodore account.", "Your Theodore account is not active at the moment.")
    return Status("SIGN_IN_REQUIRED", _REVOKED_REASON, f"run {quoted_launcher()} login --start and follow what it prints.",
                  "Theodore was signed out on this computer and needs a one-time sign-in again.")


def revoked_status() -> Status:
    """SIGN_IN_REQUIRED with the sign-in link, or, when a new sign-in cannot be started, what to do once the connection
    is back. Never opens a browser."""
    try:
        started = login_start(_REVOKED_REASON)
    except Exception as exc:        # noqa: BLE001  an answer too large, a broken one: the fixed block, with why
        started = core.internal(f"{exc.__class__.__name__}")
    if started.name != "SIGN_IN_REQUIRED":
        started = Status("SIGN_IN_REQUIRED", _REVOKED_REASON,
                         f"a new sign-in could not be started ({started.summary}). "
                         f"When the connection is back, run {quoted_launcher()} login --start and follow what it prints.",
                         "Theodore was signed out on this computer and needs a new sign-in; I will start it once the internet connection is back.")
    return started


def suspended_status(code: str) -> Status:
    """The account is suspended or its plan inactive (403): the licence is deleted, the credential kept (a suspension
    can be lifted; a deleted account's installations are revoked, which is the 401 route)."""
    return Status("NO_ENTITLEMENT", f"the Theodore service refused this account ({code}): it is suspended or its plan is not active. "
                                    "The stored licence was deleted; nothing runs until the account is active again.",
                  f"do not retry. Tell the user; they can check their account at {core.site_url('/app/plan')}.",
                  f"Your Theodore account is not active at the moment. You can check it at {core.site_name('/app/plan')}.")


def signed_in_status(claims: dict) -> Status:
    return Status("OK", f"this computer is signed in to Theodore (plan {claims.get('plan', 'unknown')}).",
                  "run the command you were running before the sign-in.",
                  "Theodore is signed in on this computer.")


def login_finish(wait_seconds: float) -> Status:
    pending_path = core.home().state / "device.json"
    pending = core.read_json(pending_path)
    if credentials.load() and not pending.get("device_code"):
        return signed_in_status(stored_token()[1])
    if not pending.get("device_code"):
        return login_start()
    interval = max(1, int(pending.get("interval") or 5))
    if core.is_loopback_api() and os.environ.get("THEODORE_TEST_POLL_INTERVAL"):
        interval = float(os.environ["THEODORE_TEST_POLL_INTERVAL"])
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        try:
            bundle = api.call_json("device_token", body={"device_code": pending["device_code"]})
            _adopt(bundle)
            return signed_in_status(stored_token()[1])
        except api.NetworkError as error:
            return api.offline(error, "sign-in could not finish")
        except api.ApiError as error:
            if error.code == "slow_down":
                interval += 5
            elif error.code in ("access_denied", "expired_token", "invalid_grant"):
                core.remove_tree(pending_path)
                why = "the sign-in was declined in the browser." if error.code == "access_denied" else "the sign-in page expired."
                return login_start(why + " A new sign-in was started.")
            elif error.code != "authorization_pending":
                return api.status_from(error, "sign-in")
        if time.monotonic() + interval > deadline:
            return Status("SIGN_IN_PENDING", "the sign-in has not been approved in the browser yet.",
                          f"ask the user to approve at {pending['url']}, then run: {quoted_launcher()} login --finish --wait 90",
                          "I am still waiting for you to approve the Theodore sign-in in your browser.")
        time.sleep(interval)


def login_code(code: str) -> Status:
    """Redeem a one-time activation code from the portal. The code is never printed or logged."""
    code = code.strip().upper()
    core.register_secret(code)
    if not re.match(ACTIVATION_CODE_PATTERN, code):
        return core.usage("the activation code must look like ABCD-1234.",
                          f"use the code exactly as it appears in the sentence the user pasted, or run {quoted_launcher()} login --start.")
    try:
        bundle = api.call_json("device_activate", body={"code": code, **device_metadata()})
    except api.NetworkError as error:
        return api.offline(error, "the activation code could not be redeemed")
    except api.ApiError as error:
        if error.code == "invalid_grant":
            return Status("SIGN_IN_REQUIRED", "the activation code is not valid any more (codes last 10 minutes and work once).",
                          f"run {quoted_launcher()} login --start and follow what it prints, or ask the user for a fresh sentence from {core.site_url('/app')}.",
                          "The Theodore activation code has expired. I am starting a browser sign-in instead.")
        return api.status_from(error, "activation")
    _adopt(bundle)
    return signed_in_status(stored_token()[1])


def read_grant(flag: str | None, path: str | None) -> str:
    """The connector's grant from standard input (`--grant -`) or from a file only its owner can read
    (`--grant-file`), never from the command line: a credential bundle holds the installation secret, and a
    process's arguments are visible to other users and land in shell histories and logs."""
    if path:
        file = __import__("pathlib").Path(path)
        try:
            if not core.IS_WINDOWS and file.stat().st_mode & 0o077:
                raise core.usage("the grant file must be readable only by its owner (mode 0600).",
                                 "write it with mode 0600 (or pass it on standard input with --grant -) and run the command again.")
            return file.read_text(encoding="utf-8")
        except OSError as exc:
            raise core.usage(f"the grant file could not be read ({exc.__class__.__name__}).") from None
    if flag == "-":
        return sys.stdin.read()
    raise core.usage("a grant is never taken from the command line (it can carry the installation secret).",
                     "pass it on standard input: activate --grant - , or in a 0600 file: activate --grant-file <path>.")


def activate_grant(blob: str) -> Status:
    """Connector route: accept an entitlement token, or a base64url JSON credential bundle, handed over by the connector."""
    blob = blob.strip()
    if blob.count(".") == 2 and decode_claims(blob).get("aud") == ENTITLEMENT_AUD:
        core.home().ensure()
        save_token(blob)
        return signed_in_status(decode_claims(blob))
    try:
        bundle = json.loads(ed25519.b64url_decode(blob))
        _adopt(bundle)
    except (ValueError, TypeError):
        return core.usage("the grant is not a Theodore entitlement or credential bundle.",
                          "call the Theodore connector's begin tool again and pass its grant unchanged.")
    return signed_in_status(stored_token()[1])


def _fresh_update_state(reason: str) -> None:
    from . import install          # imported here: install imports this module
    install.reset_update_state(reason)


def logout() -> Status:
    credentials.delete()
    delete_token()
    _fresh_update_state("signed out")
    core.remove_tree(core.home().state / "device.json")
    return Status("OK", "this computer is signed out of Theodore.",
                  f"to sign in again run {quoted_launcher()} login --start.",
                  "Theodore is signed out on this computer.")


def whoami() -> Status:
    import_pending_activation()
    token, claims = stored_token()
    cred = credentials.load()
    if not cred and not token:
        return login_start()
    exp = time.strftime("%Y-%m-%d", time.gmtime(float(claims.get("exp") or 0)))
    iid = (cred or {}).get("installation_id") or claims.get("iid") or "unknown"
    return Status("OK", f"signed in: installation {iid}, plan {claims.get('plan', 'unknown')}, channel {claims.get('chan', 'unknown')}, "
                        f"skills {', '.join(claims.get('feat') or []) or 'none'}, works offline until {exp}, credential in {credentials.describe()}.",
                  "nothing to do.", "Theodore is signed in on this computer.")
