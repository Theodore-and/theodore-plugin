"""The one module that talks HTTP. Every route comes from contract.ROUTES; every failure becomes a Status."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import core
from .contract import DELETE_CREDENTIAL_ON, DELETE_TOKEN_ON, API_ERRORS, ROUTES
from .core import Status, quoted_launcher


class ApiError(Exception):
    """The server answered with an error body: {"error": {"code", "message", ...}} or the RFC 6749 device form."""

    def __init__(self, http_status: int, code: str, message: str = "", retry_after: str | None = None):
        super().__init__(f"{http_status} {code}")
        self.http_status, self.code, self.message, self.retry_after = http_status, code, message, retry_after


class TooLarge(Exception):
    """An answer longer than the caller accepts; it was not read past that length."""

    def __init__(self, limit: int):
        super().__init__(f"the answer is longer than {limit} bytes")
        self.limit = limit


class NetworkError(Exception):
    """No answer from the server: DNS, connection, timeout, TLS or proxy."""

    def __init__(self, detail: str, cause: str = "NETWORK"):
        super().__init__(detail)
        self.detail, self.cause = detail, cause


def _ssl_context() -> ssl.SSLContext:
    try:  # the operating system's certificate store, so TLS-inspecting proxies with an installed root work
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


def may_carry_bearer(url: str) -> bool:
    """The installation bearer goes only to the configured API origin (scheme, host and port), and only over HTTPS;
    the tests' loopback API is the one plain-HTTP exception."""
    api_origin = _origin(str(core.config().get("api_base") or ""))
    if _origin(url) != api_origin or not api_origin[1]:
        return False
    return api_origin[0] == "https" or (api_origin[0] == "http" and core.is_loopback_api())


class RedirectRefused(urllib.error.URLError):
    pass


class _Redirects(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only on the origin it started from (engine and pack downloads may answer 302 to a
    short-lived URL on the same host). One to another host, another port or from HTTPS to HTTP is refused, so
    neither a credential nor the download itself can be sent elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urllib.parse.urljoin(req.full_url, newurl)
        if _origin(newurl) != _origin(req.full_url):
            raise RedirectRefused(f"a redirect from {_origin(req.full_url)[1]} to another origin ({urllib.parse.urlsplit(newurl).scheme}://"
                                  f"{urllib.parse.urlsplit(newurl).netloc}) was refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """For the installer's plugin document and archive: no redirect at all, not even on the same origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectRefused(f"the server answered with a redirect (HTTP {code}), which is not followed for this download")


_OPENERS: dict = {}


def _opener(redirects: bool = True):
    if redirects not in _OPENERS:
        _OPENERS[redirects] = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()),
                                                          _Redirects() if redirects else _NoRedirects())
    return _OPENERS[redirects]


def _network_error(exc: Exception) -> NetworkError:
    reason = getattr(exc, "reason", exc)
    text = str(reason) or reason.__class__.__name__
    if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in text or "certificate" in text.lower():
        return NetworkError(text, "TLS_INTERCEPT")
    if "407" in text or "Proxy Authentication" in text:
        return NetworkError(text, "PROXY_AUTH")
    return NetworkError(text, "NETWORK")


DEFAULT_LIMIT = 8 << 20          # an API answer (manifest, guide part, list) is far smaller
ERROR_LIMIT = 64 << 10           # an error answer is a short JSON object
DOWNLOAD_LIMIT = 2 << 30         # a download without a published size (none is larger than LibreOffice)


def _read_bounded(response, limit: int) -> bytes:
    """The whole body, or TooLarge when it is (or says it is) longer than `limit`; never more than limit + 1 bytes read."""
    declared = str(response.headers.get("Content-Length") or "") if response.headers is not None else ""
    if declared.isdigit() and int(declared) > limit:
        raise TooLarge(limit)
    raw = response.read(limit + 1) or b""
    if len(raw) > limit:
        raise TooLarge(limit)
    return raw


def request(method: str, url: str, *, bearer: str | None = None, body=None, headers: dict | None = None,
            timeout: float = 20.0, redirects: bool = True, limit: int | None = None):
    """One HTTP exchange. Returns (status, headers, bytes) for 2xx and 304; raises ApiError or NetworkError.
    Every body is bounded: a success body longer than `limit` (DEFAULT_LIMIT when not given), or an error body longer
    than ERROR_LIMIT, raises TooLarge and is not read further."""
    data = None
    hdrs = {"User-Agent": core.user_agent(), "Accept": "application/json, text/markdown;q=0.9, */*;q=0.1"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if bearer and may_carry_bearer(url):
        hdrs["Authorization"] = f"Bearer {bearer}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _opener(redirects).open(req, timeout=timeout) as resp:
            return resp.status, resp.headers, _read_bounded(resp, DEFAULT_LIMIT if limit is None else limit)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, exc.headers, b""
        if exc.code == 407:
            raise NetworkError("the proxy asks for authentication (HTTP 407)", "PROXY_AUTH") from None
        raw = _read_bounded(exc, ERROR_LIMIT)
        code, message = f"http_{exc.code}", ""
        try:
            parsed = json.loads(raw.decode("utf-8"))
            err = parsed.get("error")
            if isinstance(err, dict):
                code, message = str(err.get("code") or code), str(err.get("message") or "")
            elif isinstance(err, str):
                code, message = err, str(parsed.get("error_description") or "")
        except (ValueError, AttributeError):
            pass
        if "Authorization" in hdrs:
            _signed_out(exc.code, code)
        raise ApiError(exc.code, code, message, exc.headers.get("Retry-After")) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as exc:
        raise _network_error(exc) from None


def _signed_out(http_status: int, code: str) -> None:
    """The service refused this installation's own credential. Whatever route learned it: a revoked or unknown
    installation loses the credential and the stored licence now, so no later command works from what is on disk; an
    account that is refused (403) loses the stored licence. Automatic reports being off is not a refusal of the account."""
    from . import account, credentials
    if code in DELETE_CREDENTIAL_ON:
        core.mark_revoked(code)          # the command ends with the sign-in block, whatever it does next (core.emit, cli.main)
        credentials.delete()
        account.delete_token()
        core.log(f"signed out by the service ({code}): credential and licence deleted")
    elif code in DELETE_TOKEN_ON or (http_status == 403 and code != "auto_reports_disabled"):
        account.delete_token()


def url_for(route: str, query: dict | None = None, **params) -> str:
    _method, template, _auth = ROUTES[route]
    path = template.format(**{k: urllib.parse.quote(str(v), safe="") for k, v in params.items()})
    url = core.config()["api_base"].rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    return url


def call(route: str, *, bearer: str | None = None, body=None, query: dict | None = None, headers: dict | None = None,
         timeout: float = 20.0, redirects: bool = True, limit: int | None = None, **params):
    method, _template, needs_auth = ROUTES[route]
    if needs_auth and not bearer:
        raise sign_in_required()
    return request(method, url_for(route, query, **params), bearer=bearer if needs_auth else None, body=body,
                   headers=headers, timeout=timeout, redirects=redirects, limit=limit)


def call_json(route: str, **kwargs):
    status, headers, raw = call(route, **kwargs)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise core.internal(f"the Theodore API answered {route} with something that is not JSON (HTTP {status}).") from None


def download(url: str, dest: Path, *, sha256: str, bearer: str | None = None, size: int | None = None,
             progress=None, timeout: float = 60.0, redirects: bool = True) -> Path:
    """Stream `url` to `dest` and check its sha256. A mismatch deletes the file and raises SETUP_FAILED."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    hdrs = {"User-Agent": core.user_agent()}
    if bearer and may_carry_bearer(url):        # a URL off the API origin is fetched without the credential
        hdrs["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, headers=hdrs)
    digest, done = hashlib.sha256(), 0
    bound = size or DOWNLOAD_LIMIT
    try:
        with _opener(redirects).open(req, timeout=timeout) as resp, open(part, "wb") as out:
            total = size or int(resp.headers.get("Content-Length") or 0)
            while True:
                block = resp.read(1 << 18)
                if not block:
                    break
                if done + len(block) > bound:         # longer than published: refused, not read further
                    out.close()
                    _unlink(part)
                    raise setup_failed("HASH_MISMATCH", f"{dest.name} is larger than its published size")
                out.write(block)
                digest.update(block)
                done += len(block)
                if progress and total:
                    progress(done * 100.0 / total)
    except urllib.error.HTTPError as exc:
        _unlink(part)
        if exc.code == 407:
            raise NetworkError("the proxy asks for authentication (HTTP 407)", "PROXY_AUTH") from None
        raw = _read_bounded(exc, ERROR_LIMIT)
        code = f"http_{exc.code}"
        try:
            err = json.loads(raw.decode("utf-8")).get("error")
            code = err.get("code", code) if isinstance(err, dict) else (err or code)
        except (ValueError, AttributeError):
            pass
        if "Authorization" in hdrs:
            _signed_out(exc.code, str(code))
        raise ApiError(exc.code, str(code), "", exc.headers.get("Retry-After")) from None
    except OSError as exc:
        _unlink(part)
        if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError)):
            raise _network_error(exc) from None
        raise setup_failed("DISK", f"could not write {dest.name}: {exc}") from None
    if digest.hexdigest() != sha256.lower():
        _unlink(part)
        raise setup_failed("HASH_MISMATCH", f"{dest.name} did not match its published checksum")
    core.replace(part, dest)
    return dest


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# --- failures as statuses ------------------------------------------------------------------------------------------------

def sign_in_required(detail: str = "this computer is not signed in to Theodore.") -> Status:
    return Status("SIGN_IN_REQUIRED", detail,
                  f"run {quoted_launcher()} login --start and follow what it prints.",
                  "Theodore needs a one-time sign-in on this computer. I am starting it.")


def setup_failed(cause: str, detail: str) -> Status:
    nexts = {
        "NETWORK": "check the internet connection, then run the same command again.",
        "TLS_INTERCEPT": "this network inspects HTTPS with a certificate this computer does not trust. Ask the user to have "
                         "their IT team install the company root certificate, or to try another network; then run the same command again.",
        "PROXY_AUTH": "the network proxy asks for a sign-in. Ask the user to sign in to the proxy (opening any website in a "
                      "browser usually does it), then run the same command again.",
        "DISK": f"free disk space or make {core.home().root} writable, then run the same command again.",
        "HASH_MISMATCH": "run the same command again. If it fails twice, stop and tell the user to write to support@theodoreand.ai.",
    }
    tells = {
        "NETWORK": "Theodore could not download its files. Please check the internet connection.",
        "TLS_INTERCEPT": "This network inspects secure connections and blocked Theodore's download. Your IT team can fix it by installing the company certificate.",
        "PROXY_AUTH": "The network proxy needs you to sign in before Theodore can download its files.",
        "DISK": "Theodore could not write its files. The disk may be full or the folder protected.",
        "HASH_MISMATCH": "A Theodore download arrived damaged. I am trying again.",
    }
    return Status("SETUP_FAILED", f"cause {cause}: {detail}", nexts[cause], tells[cause])


def offline(error: NetworkError, what: str) -> Status:
    if error.cause in ("TLS_INTERCEPT", "PROXY_AUTH"):
        failed = setup_failed(error.cause, f"{what}: {error.detail}")
        return Status("OFFLINE", failed.summary, failed.next_step, failed.tell_user)
    return Status("OFFLINE", f"{what}: the Theodore service could not be reached ({error.detail}).",
                  "check the internet connection, then run the same command again.",
                  "Theodore could not reach its service. Please check the internet connection.")


def status_from(error: ApiError, what: str) -> Status:
    """Map an API error code to the status the assistant acts on (contract.API_ERRORS)."""
    name = API_ERRORS.get(error.code, (0, None))[1]
    if name is None:
        name = "RETRY" if error.http_status >= 500 else "INTERNAL"
    launcher = quoted_launcher()
    detail = f"{what}: {error.message or error.code}"
    if name == "SIGN_IN_REQUIRED":
        reason = "this device was signed out from the Theodore portal." if error.code == "installation_revoked" \
            else "this computer's sign-in is no longer valid."
        return sign_in_required(reason)
    if name == "NO_ENTITLEMENT":
        return Status(name, detail, f"do not retry. Tell the user; they can check their plan at {core.site_url('/app/plan')}.",
                      f"Your Theodore account does not currently include this skill. You can check your plan at {core.site_name('/app/plan')}.")
    if error.code == "auto_reports_disabled":
        # Off means off (the lead's ruling): nothing to ask, nothing to tell; only a report the user asked for goes out.
        # The client normally knows before sending (account.automatic_reports); this answer is the server's backstop.
        return Status(name, "automatic reports are off for this account; the Theodore service refused the report and did not store it.",
                      "do not retry and do not ask the user about it; continue with the user's task. Send a report only "
                      "when the user explicitly asks for one, with --origin user_requested.", None)
    if name == "UPDATE_REQUIRED":
        return Status(name, detail, f"run {launcher} update --apply, then run the same command again.",
                      "Theodore needs an update before it can continue. I am installing it.")
    if name == "RETRY":
        wait = error.retry_after or "60"
        return Status(name, detail, f"wait {wait} seconds, then run the same command again (at most three times).",
                      "Theodore's service is busy. I will try again in a moment.")
    if name == "USAGE":
        return core.usage(detail)
    return core.internal(f"{detail} (HTTP {error.http_status}).")
