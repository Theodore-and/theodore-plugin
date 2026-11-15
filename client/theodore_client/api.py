"""The one module that talks HTTP. Every route comes from contract.ROUTES; every failure becomes a Status."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import re
import os
import queue
import socket
import sys
import threading
import time
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


class _BoundedBody:
    """The body of a redirect answer, which urllib reads in full before following it: read here up to ERROR_LIMIT,
    and refused (TooLarge) when longer."""

    def __init__(self, fp):
        self._fp = fp

    def read(self, n=-1):
        if n is None or n < 0:
            data = self._fp.read(ERROR_LIMIT + 1) or b""
            if len(data) > ERROR_LIMIT:
                raise TooLarge(ERROR_LIMIT)
            return data
        return self._fp.read(min(n, ERROR_LIMIT + 1))

    def __getattr__(self, name):
        return getattr(self._fp, name)


class _Redirects(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only on the origin it started from (engine and pack downloads may answer 302 to a
    short-lived URL on the same host). One to another host, another port or from HTTPS to HTTP is refused, so
    neither a credential nor the download itself can be sent elsewhere. The redirect's own body is bounded too."""

    def http_error_302(self, req, fp, code, msg, headers):
        return super().http_error_302(req, _BoundedBody(fp), code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

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


# --- every address of Theodore's own hosts ------------------------------------------------------------------------------
# Theodore's hosts resolve to several addresses. From some networks one of them is unreachable, or completes the TCP
# connection but never the TLS handshake (or resets it), while another works; http.client tries only the connection,
# and a handshake that hangs or is reset on the first address failed every request. So, for Theodore's own hosts and
# without a proxy (which chooses the address itself), the addresses are raced: the first is tried at once, the next
# STAGGER seconds later (at once when the one before failed), each with CONNECT_EACH seconds to connect and finish the
# handshake, within the request's own timeout; the first handshake that completes wins and the others are closed.
# The address that worked is tried first, in this process and in the next ones: it is kept in state/addresses.json
# for ADDRESS_TTL seconds and only while the name still resolves to it; when it stops working the race moves on after
# STAGGER and the new winner replaces it. A request on a network where one address hangs costs STAGGER the first time
# and nothing after. The certificate is verified for the host name (SNI and verification on the name, never the
# address).
CONNECT_EACH = 3.0
STAGGER = 0.25
ADDRESS_FILE = "addresses.json"
ADDRESS_TTL = 7 * 86400
ADDRESS_REFRESH = 86400          # a remembered address that keeps working is written again once a day (its expiry moves)
_GOOD_ADDRESS: dict[tuple[str, int], tuple] = {}
_ADDRESS_LOCK = threading.Lock()
OWN_BASES: list[str] = []      # bases known before launcher.json exists (the installer's --dl-base)


def _domain(host: str) -> str:
    """A name's last two labels (its registrable domain for Theodore's hosts); an address stays itself."""
    host = (host or "").lower().rstrip(".")
    if not host or host.replace(".", "").isdigit() or ":" in host:
        return host
    return ".".join(host.split(".")[-2:])


def _own_host(host: str) -> bool:
    """Theodore's site, API and download hosts and the other names of their domain (the staging forms): the domain of
    the bases this installation was set up for (launcher.json) or given (OWN_BASES). A third party's host
    (LibreOffice's mirror) is reached as before."""
    bases = list(OWN_BASES)
    try:
        cfg = core.config()
        bases += [str(cfg.get(key) or "") for key in ("api_base", "dl_base", "site_base")]
    except Exception:       # noqa: BLE001  the installer's plugin step runs before any launcher.json exists
        pass
    host = (host or "").lower().rstrip(".")
    for base in bases:
        own = (urllib.parse.urlsplit(base).hostname or "").lower().rstrip(".")
        if own and (host == own or (_domain(own) != own and _domain(host) == _domain(own))):
            return True
    return False


def _address_file() -> Path | None:
    """state/addresses.json in Theodore's folder; None while there is no folder yet (an install's first steps) or it
    is being removed (uninstall), so nothing is created there."""
    try:
        root = core.home().root
        if not root.is_dir() or (root / ".theodore-uninstalling").exists():
            return None
        return root / "state" / ADDRESS_FILE
    except Exception:       # noqa: BLE001
        return None


def _read_addresses() -> dict:
    path = _address_file()
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _remembered(host: str, port: int) -> tuple | None:
    """The address that worked last for host:port: this process's, else the one kept on disk while it is fresh."""
    good = _GOOD_ADDRESS.get((host, port))
    if good:
        return good
    entry = _read_addresses().get(f"{host}:{port}")
    try:
        if isinstance(entry, dict) and 0 <= time.time() - float(entry.get("at") or 0) < ADDRESS_TTL:
            address, at_port = entry.get("address"), entry.get("port", port)
            if isinstance(address, str) and address and isinstance(at_port, int):
                return (address, at_port)
    except (TypeError, ValueError):
        pass
    return None


def _keep_address(host: str, port: int, address: tuple, *, drop: bool = False) -> None:
    """Remember (or, with drop, forget) the address on disk for the next processes. Written only when it changed or
    once a day; one attempt, never an error, never a wait (a reader on Windows may hold the file a moment)."""
    path = _address_file()
    if path is None:
        return
    with _ADDRESS_LOCK, contextlib.suppress(Exception):
        table = _read_addresses()
        key = f"{host}:{port}"
        entry = table.get(key) if isinstance(table.get(key), dict) else {}
        same = entry.get("address") == address[0] and entry.get("port", port) == address[1]
        if drop:
            if not same:
                return
            table.pop(key, None)
        else:
            if same and 0 <= time.time() - float(entry.get("at") or 0) < ADDRESS_REFRESH:
                return
            table[key] = {"address": address[0], "port": address[1], "at": int(time.time())}
        table = {k: v for k, v in table.items()
                 if isinstance(v, dict) and 0 <= time.time() - float(v.get("at") or 0) < ADDRESS_TTL}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()


def _connect_any(host: str, port: int, timeout, source_address, context: ssl.SSLContext):
    """A TLS socket to `host`, from the first of its addresses that connects and completes the handshake, the
    addresses raced as described above. When every address only timed out (a slow link rather than a dead address),
    they are tried once more, one after another, with what is left of the request's own time, as one connection was
    before."""
    budget = timeout if isinstance(timeout, (int, float)) else None
    found, order = set(), []
    for family, kind, proto, _canon, address in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        if (family, address[:2]) not in found:
            found.add((family, address[:2]))
            order.append((family, kind, proto, address))
    remembered = _remembered(host, port)
    order.sort(key=lambda entry: entry[3][:2] != remembered)    # the address that worked last time first (stable)
    began = time.monotonic()

    def left():
        return None if budget is None else budget - (time.monotonic() - began)

    def attempt(entry, each):
        family, kind, proto, address = entry
        deadline = time.monotonic() + each
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(each)
            if source_address:
                sock.bind(source_address)
            sock.connect(address)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            sock.settimeout(max(deadline - time.monotonic(), 0.05))
            return context.wrap_socket(sock, server_hostname=host)
        except BaseException:
            sock.close()
            raise

    def won(entry, tls):
        tls.settimeout(timeout if budget is not None else socket.getdefaulttimeout())
        address = entry[3][:2]
        _GOOD_ADDRESS[(host, port)] = address
        _keep_address(host, port, address)
        return tls

    results: queue.Queue = queue.Queue()
    decided = threading.Event()

    def racer(entry, each):
        try:
            tls = attempt(entry, each)
        except BaseException as exc:            # noqa: BLE001  reported to the waiting thread
            results.put((entry, None, exc))
            return
        if decided.is_set():                    # another address won meanwhile: this one is not used
            with contextlib.suppress(Exception):
                tls.close()
            return
        results.put((entry, tls, None))

    failures, slow = [], []
    started = running = 0
    while True:
        remaining = left()
        if started < len(order) and (started == 0 or remaining is None or remaining > 0):
            each = CONNECT_EACH if remaining is None else max(min(CONNECT_EACH, remaining), 0.05)
            threading.Thread(target=racer, args=(order[started], each), name="theodore-connect", daemon=True).start()
            started += 1
            running += 1
        if running == 0:
            break
        more = started < len(order) and (remaining is None or remaining > 0)
        wait = STAGGER if more else (None if remaining is None else max(remaining, 0) + 0.25)
        try:
            entry, tls, exc = results.get(timeout=wait)
        except queue.Empty:
            if more:
                continue                        # the next address starts beside the ones still trying
            break                               # the request's own time is spent
        running -= 1
        if tls is not None:
            decided.set()
            while True:                         # another handshake that finished before this point is not used
                try:
                    _entry, other, _exc = results.get_nowait()
                except queue.Empty:
                    break
                if other is not None:
                    with contextlib.suppress(Exception):
                        other.close()
            if remembered and entry[3][:2] != remembered and remembered in [e[3][:2] for e in order]:
                core.log(f"network: {host} answered on another address than the one remembered")
            return won(entry, tls)
        if isinstance(exc, ssl.SSLCertVerificationError):
            decided.set()
            raise exc                           # the same on every address: a TLS-inspecting network
        failures.append(f"{entry[3][0]}: {str(exc) or exc.__class__.__name__}")
        if isinstance(exc, TimeoutError) or "timed out" in str(exc):
            slow.append(entry)
    decided.set()
    tried = len(failures)
    if slow and len(slow) == tried == started:
        for entry in slow:
            remaining = left()
            if remaining is not None and remaining <= CONNECT_EACH / 10:
                break
            try:
                return won(entry, attempt(entry, remaining if remaining is not None else 3 * CONNECT_EACH))
            except ssl.SSLCertVerificationError:
                raise
            except OSError:
                continue
    if remembered and tried == started == len(order):
        _keep_address(host, port, remembered, drop=True)    # nothing answered: the next process starts afresh
    untried = len(order) - started
    pending = started - tried
    raise ConnectionError(f"all {len(order)} addresses of {host} failed ({'; '.join(failures) or 'no answer'}"
                          + (f"; {pending} still trying when the request's {budget:g} s were spent" if pending else "")
                          + (f"; {untried} not tried: the request's {budget:g} s were spent" if untried else "") + ")")


class _AnyAddressHTTPS(http.client.HTTPSConnection):
    def connect(self):
        if self._tunnel_host or not _own_host(self.host):       # through a proxy, or a third party's host: as before
            return super().connect()
        sys.audit("http.client.connect", self, self.host, self.port)
        self.sock = _connect_any(self.host, self.port, self.timeout, self.source_address, self._context)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_AnyAddressHTTPS, req, context=self._context)


_OPENERS: dict = {}


def _opener(redirects: bool = True):
    if redirects not in _OPENERS:
        _OPENERS[redirects] = urllib.request.build_opener(_HTTPSHandler(context=_ssl_context()),
                                                          _Redirects() if redirects else _NoRedirects())
    return _OPENERS[redirects]


def _network_error(exc: Exception) -> NetworkError:
    reason = getattr(exc, "reason", exc)
    text = str(reason) or reason.__class__.__name__
    if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in text or "certificate" in text.lower():
        return NetworkError(text, "TLS_INTERCEPT")
    # 407 as a status code, never the digits of a port in a URL (127.0.0.1:64071)
    if re.search(r"(?<![\d:])407(?!\d)", text) or "Proxy Authentication" in text:
        return NetworkError(text, "PROXY_AUTH")
    return NetworkError(text, "NETWORK")


# Through a proxy, a new connection can take far longer than a direct one: ChatGPT Work's proxy spends 7 to 10 s on
# every TLS set-up (curl alike). The short waits meant for a direct line (a heartbeat's 2 s, doctor's 5 s) are raised
# to this when a proxy carries the request.
PROXIED_MIN_TIMEOUT = 25.0


def _proxied(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        proxies = urllib.request.getproxies()
        return bool(proxies.get(parts.scheme.lower())) and not urllib.request.proxy_bypass(parts.hostname or "")
    except Exception:       # noqa: BLE001  a malformed proxy setting: urllib reports it when the request is made
        return False


def _timeout(url: str, timeout: float) -> float:
    return max(timeout, PROXIED_MIN_TIMEOUT) if timeout < PROXIED_MIN_TIMEOUT and _proxied(url) else timeout


# A cloud sandbox's proxy refusing our hosts (claude.ai answers CONNECT with 403) or a sandbox without DNS (ChatGPT's
# ordinary chat): the user can change the first, and should move to Work mode for the second.
_SANDBOX_BLOCKED = re.compile(r"Tunnel connection failed: 403|\b403 Forbidden|name resolution|Name or service not known|"
                              r"nodename nor servname|getaddrinfo failed|No address associated|Connection refused", re.I)


def sandbox_blocked(detail: str) -> bool:
    """True in a cloud sandbox whose network refused or could not resolve our hosts."""
    return core.cloud_sandbox() and bool(_SANDBOX_BLOCKED.search(detail or ""))


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
            timeout: float = 20.0, redirects: bool = True, limit: int | None = None, exact_timeout: bool = False,
            learn_refusals: bool = True):
    """One HTTP exchange. Returns (status, headers, bytes) for 2xx and 304; raises ApiError or NetworkError.
    Every body is bounded: a success body longer than `limit` (DEFAULT_LIMIT when not given), or an error body longer
    than ERROR_LIMIT, raises TooLarge and is not read further. A `body` given as bytes is sent as it is (JSON).
    `exact_timeout`: the wait is not raised for a proxy. `learn_refusals=False`: a 401 or 403 changes nothing on this
    computer (the usage counts, which must never change what a command does)."""
    data = None
    hdrs = {"User-Agent": core.user_agent(), "Accept": "application/json, text/markdown;q=0.9, */*;q=0.1"}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if bearer and may_carry_bearer(url):
        hdrs["Authorization"] = f"Bearer {bearer}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _opener(redirects).open(req, timeout=timeout if exact_timeout else _timeout(url, timeout)) as resp:
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
        if "Authorization" in hdrs and learn_refusals:
            _signed_out(exc.code, code)
        raise ApiError(exc.code, code, message, exc.headers.get("Retry-After")) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as exc:
        raise _network_error(exc) from None


def _signed_out(http_status: int, code: str) -> None:
    """The service refused this installation's own credential. Whatever route learned it: a revoked or unknown
    installation loses the credential and the stored licence now, so no later command works from what is on disk; an
    account that is refused (403) loses the stored licence. Automatic reports being off is not a refusal of the account."""
    from . import account, credentials, specimens
    if code in DELETE_CREDENTIAL_ON or code in ("account_suspended", "entitlement_inactive"):
        specimens.sweep(everything=True)        # whatever command learned it: no specimen sheet is kept (never raises)
    if code in DELETE_CREDENTIAL_ON:
        core.mark_refused("revoked", code)      # the command ends with the sign-in block, whatever it does next (core.emit, cli.main)
        credentials.delete()
        account.delete_token()
        core.log(f"signed out by the service ({code}): credential and licence deleted")
    elif code in ("account_suspended", "entitlement_inactive"):
        core.mark_refused("suspended", code)    # the same, central: nothing runs or prints after it; the credential stays
        account.delete_token()
        core.log(f"account refused by the service ({code}): licence deleted, credential kept")
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
         timeout: float = 20.0, redirects: bool = True, limit: int | None = None, exact_timeout: bool = False,
         learn_refusals: bool = True, **params):
    method, _template, needs_auth = ROUTES[route]
    if needs_auth and not bearer:
        raise sign_in_required()
    return request(method, url_for(route, query, **params), bearer=bearer if needs_auth else None, body=body,
                   headers=headers, timeout=timeout, redirects=redirects, limit=limit, exact_timeout=exact_timeout,
                   learn_refusals=learn_refusals)


def call_json(route: str, **kwargs):
    status, headers, raw = call(route, **kwargs)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise core.internal(f"the Theodore API answered {route} with something that is not JSON (HTTP {status}).") from None


DOWNLOAD_ATTEMPTS = 3


def download(url: str, dest: Path, *, sha256: str, bearer: str | None = None, size: int | None = None,
             progress=None, timeout: float = 60.0, redirects: bool = True) -> Path:
    """Stream `url` to `dest` and check its sha256, fetching it afresh up to three times when the bytes that arrived do
    not match (a flaky network cuts or damages a download: ACCEPT-LOCAL2) or the connection fails on the way; then the
    last failure is raised. An answer of the service (HTTP 4xx), a file larger than published and a full disk are
    raised at once."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return _download_once(url, dest, sha256=sha256, bearer=bearer, size=size, progress=progress,
                                  timeout=timeout, redirects=redirects)
        except Status as status:
            if attempt >= DOWNLOAD_ATTEMPTS or "did not match its published checksum" not in status.summary:
                raise
            core.log(f"download {dest.name}: checksum mismatch, attempt {attempt} of {DOWNLOAD_ATTEMPTS}; fetching it again")
        except NetworkError as error:
            if attempt >= DOWNLOAD_ATTEMPTS or error.cause not in ("NETWORK",):
                raise
            core.log(f"download {dest.name}: {error.detail}, attempt {attempt} of {DOWNLOAD_ATTEMPTS}; fetching it again")
        try:
            quick = core.is_loopback_api()
        except core.Status:                 # the installer's plugin step runs before any launcher.json exists
            quick = False
        time.sleep(0.1 if quick else 2.0 * attempt)


def _download_once(url: str, dest: Path, *, sha256: str, bearer: str | None = None, size: int | None = None,
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
        with _opener(redirects).open(req, timeout=_timeout(url, timeout)) as resp, open(part, "wb") as out:
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
    if cause == "DISK" and core.codex_sandboxed():
        # Codex's sandbox lets a command write only the workspace: the one approval Theodore needs is for this command
        # to run outside it, once (lane ORGDIRECT, TR-KN8R69).
        nexts["DISK"] = (f"this runs inside Codex's sandbox, which does not let Theodore write {core.home().root}. Run the same command "
                         f"again with permission to run outside the sandbox (one approval). That same command also writes Theodore's "
                         f"Codex rules, so its later commands are not asked about again (the user turns that off with "
                         f"{quoted_launcher()} hosts settings --remove).")
    tells = {
        "NETWORK": "Theodore could not download its files. Please check the internet connection.",
        "TLS_INTERCEPT": "This network inspects secure connections and blocked Theodore's download. Your IT team can fix it by installing the company certificate.",
        "PROXY_AUTH": "The network proxy needs you to sign in before Theodore can download its files.",
        "DISK": "Theodore could not write its files. The disk may be full or the folder protected.",
        "HASH_MISMATCH": "A Theodore download arrived damaged. I am trying again.",
    }
    if cause == "NETWORK" and sandbox_blocked(detail):
        status = Status("CONNECTOR_REQUIRED", f"cause {cause}: {detail}; this cloud workspace cannot reach Theodore's sites, so "
                        "Theodore's engine cannot run here.", core.SANDBOX_NETWORK_NEXT, core.sandbox_network_tell())
    else:
        status = Status("SETUP_FAILED", f"cause {cause}: {detail}", nexts[cause], tells[cause])
    status.cause = cause
    return status


def offline(error: NetworkError, what: str) -> Status:
    status = _offline(error, what)
    status.cause = error.cause
    return status


def _offline(error: NetworkError, what: str) -> Status:
    if error.cause in ("TLS_INTERCEPT", "PROXY_AUTH"):
        failed = setup_failed(error.cause, f"{what}: {error.detail}")
        return Status("OFFLINE", failed.summary, failed.next_step, failed.tell_user)
    if sandbox_blocked(error.detail):
        return Status("CONNECTOR_REQUIRED", f"{what}: the Theodore service could not be reached from this cloud workspace "
                      f"({error.detail}), so Theodore's engine cannot run here.", core.SANDBOX_NETWORK_NEXT, core.sandbox_network_tell())
    return Status("OFFLINE", f"{what}: the Theodore service could not be reached ({error.detail}).",
                  "check the internet connection, then run the same command again.",
                  "Theodore could not reach its service. Please check the internet connection.")


def status_from(error: ApiError, what: str) -> Status:
    """Map an API error code to the status the assistant acts on (contract.API_ERRORS); the code is its cause."""
    status = _status_from(error, what)
    status.cause = error.code
    return status


def _status_from(error: ApiError, what: str) -> Status:
    name = API_ERRORS.get(error.code, (0, None))[1]
    if name is None:
        name = "RETRY" if error.http_status >= 500 else "INTERNAL"
    launcher = quoted_launcher()
    detail = f"{what}: {error.message or error.code}"
    if name == "SIGN_IN_REQUIRED":
        reason = "this device was signed out from the Theodore portal." if error.code == "installation_revoked" \
            else "this copy of Theodore was set up on another computer; this computer needs its own log in." \
            if error.code == "device_proof_invalid" else "this computer's sign-in is no longer valid."
        return sign_in_required(reason)
    if name == "NO_ENTITLEMENT":
        return Status(name, detail, f"do not retry. Tell the user; they can check their plan at {core.site_url('/app/plan')}.",
                      f"Your Theodore account does not currently include this skill. You can check your plan at {core.site_name('/app/plan')}.")
    if error.code == "auto_reports_disabled":
        # Off means off: nothing to ask, nothing to tell; only a report the user asked for goes out.
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
