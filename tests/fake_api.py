"""A local stand-in for the Theodore API and download host, for tests. Standard library only.

It implements the /v1 routes of the frozen contract (contract version 1.0.0; a snapshot of what the client
depends on is in tests/fixtures/contract-v1.json): device and activation flow, licence refresh, guide, signed
channel and runtime manifests, engine and pack downloads, reports, preferences, heartbeat and keys, plus plain
file serving under /dl/ for the runtime parts, the client and the plugin. Request bodies are checked the way the
contract's schemas check them (and unknown members are refused, which is stricter), so a client that drifts
from the contract fails here first. State lives on the FakeApi object: a test changes the server by assigning.

Run it by hand:  python3 tests/fake_api.py --port 8787   (prints its base URL and test public key)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from theodore_client import ed25519  # noqa: E402


# --- Ed25519 signing (the client only verifies; tests need to sign) ------------------------------------------------

def _clamp(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    a, _ = _clamp(seed)
    return ed25519.point_compress(ed25519.point_mul(a, ed25519.G))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _clamp(seed)
    pub = ed25519.point_compress(ed25519.point_mul(a, ed25519.G))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % ed25519.L
    big_r = ed25519.point_compress(ed25519.point_mul(r, ed25519.G))
    h = int.from_bytes(hashlib.sha512(big_r + pub + message).digest(), "little") % ed25519.L
    return big_r + int.to_bytes((r + h * a) % ed25519.L, 32, "little")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value) -> str:
    """The contract's canonical JSON: keys sorted at every depth, no whitespace, JSON.stringify scalars."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def iso(seconds: float) -> str:
    """Milliseconds, as the Worker writes server_time (Date.toISOString)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{int(seconds * 1000) % 1000:03d}Z"


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def normalize_code(raw: str) -> str | None:
    """One-time codes as typed: any case, optional hyphen or spaces; I and L read as 1, O as 0."""
    compact = "".join(ch for ch in str(raw).upper() if ch not in "- ").translate(str.maketrans("ILO", "110"))
    if len(compact) != 8 or any(ch not in CODE_ALPHABET for ch in compact):
        return None
    return compact[:4] + "-" + compact[4:]


def _check(body: dict, required: dict, optional: dict | None = None) -> str | None:
    """None when `body` fits: every required member present and valid, optional ones valid, nothing else."""
    optional = optional or {}
    if not isinstance(body, dict):
        return "the body is not an object"
    for key in body:
        if key not in required and key not in optional:
            return f"unknown member {key}"
    for key, rule in required.items():
        if key not in body or not rule(body[key]):
            return f"member {key} is missing or not valid"
    for key, rule in optional.items():
        if key in body and not rule(body[key]):
            return f"member {key} is not valid"
    return None


_semver = lambda v: isinstance(v, str) and len(v) <= 64 and bool(SEMVER.match(v))
_slug = lambda v: isinstance(v, str) and bool(SLUG.match(v))
_text = lambda low, high: (lambda v: isinstance(v, str) and low <= len(v.strip()) <= high)
DEVICE_METADATA = {"device_name": _text(1, 80), "platform": lambda v: v in ("darwin", "win32", "linux"), "arch": lambda v: v in ("arm64", "x64"),
                   "host": _slug, "client_version": _semver}


class FakeApi:
    MANIFEST_KID = "manifest-test-1"
    ENTITLEMENT_KID = "ent-test-1"

    def __init__(self, port: int = 0, tls: tuple[str, str] | None = None):
        """`tls` = (certificate file, key file) serves HTTPS, to stand in for a network that inspects TLS."""
        self.manifest_seed = hashlib.sha256(b"theodore test manifest key").digest()
        self.entitlement_seed = hashlib.sha256(b"theodore test entitlement key").digest()
        self.lock = threading.RLock()
        self.clock_offset = 0.0
        self.down = False                 # True: every request gets a dropped connection
        self.fail_with = None             # (http status, error code) for every authenticated /v1 call
        self.requests: list[dict] = []    # {method, path, auth} of every request, for assertions
        self.files: dict[str, bytes | Path] = {}      # /dl/<key> -> content
        self.dl_status: dict[str, int] = {}           # /dl/<key> -> forced HTTP status (for example 407)
        self.dl_redirect: dict[str, str] = {}         # /dl/<key> -> Location of a 302
        self.dl_delay = 0.0                           # seconds to stall every /dl/ download (a slow connection)
        self.activation_codes: dict[str, dict] = {}   # code -> {used: bool}
        self.devices: dict[str, dict] = {}            # device_code -> {user_code, status}
        self.installations: dict[str, dict] = {}      # id -> {secret, revoked, gen}
        self.plan, self.features, self.channel_name = "beta", ["pptx", "deck-design", "agent-reports"], "beta"
        self.grace_seconds = 7 * 86400
        self.account_state = "active"     # or "suspended" / "inactive"
        self.auto_reports = True
        self.refresh_carries_report_switch = True    # the refresh answer names the user's switch (lane G2 adds it to the contract)
        self.engine: dict | None = None   # {version, sha256, size, runtime_id, min_client, revoked: [], kind}
        self.engine_files: dict[str, bytes] = {}
        self.engine_redirect = None       # "same": 302 to /dl/private/... on this origin; "other": the same on localhost
        self.client = {"version": "0.1.0", "sha256": ""}
        self.shim_min: str | None = "0.1.0"
        self.min_version = None
        self.kill, self.kill_message = False, ""
        self.runtime: dict | None = None  # the current release's runtime: {runtime_id, parts: [{part, version, url, sha256, size}]}
        self.runtimes: dict[str, dict] = {}   # every published runtime by id
        self.prefs_rev = 0
        self.packs: list[dict] = []       # {id, version, sha256, size_bytes}
        self.pack_files: dict[tuple[str, str], bytes] = {}
        self.pack_delay = 0.0             # seconds to stall each pack download (to test waiting)
        self.packs_manifest_override = None   # a list: what the unsigned GET /v1/packs/manifest answers instead
        self.guides = {"deck-design": {"": "# Deck design\n\nFake design guidance.\n", "license": "Apache License 2.0 (fake)\n"},
                       "pptx": {"": ""}}
        self.guide_version = "g1"
        self.guide_fetches = 0
        self.preferences: list[dict] = []
        self.reports: list[dict] = []
        self.heartbeats: list[dict] = []
        self.refreshes = 0
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                api._handle(self, "GET")

            def do_POST(self):
                api._handle(self, "POST")

            def do_DELETE(self):
                api._handle(self, "DELETE")

            def do_HEAD(self):
                api._handle(self, "HEAD")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if tls:
            import ssl
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            self.base = f"https://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # --- lifecycle -----------------------------------------------------------------------------------------------
    def start(self) -> "FakeApi":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def now(self) -> float:
        return time.time() + self.clock_offset

    # --- test controls -----------------------------------------------------------------------------------------------
    def issue_activation_code(self, code: str = "ABCD-1234") -> str:
        self.activation_codes[normalize_code(code)] = {"used": False}
        return code

    def approve_device(self, deny: bool = False) -> None:
        for device in self.devices.values():
            if device["status"] == "pending":
                device["status"] = "denied" if deny else "approved"

    def revoke_all(self) -> None:
        for inst in self.installations.values():
            inst["revoked"] = True

    def publish_engine(self, version: str, data: bytes, runtime_id: str | None = None, **extra) -> None:
        self.engine_files[version] = data
        self.engine = {"version": version, "kind": "engine-source", "sha256": sha256_hex(data), "size": len(data),
                       "runtime_id": runtime_id or (self.runtime or {}).get("runtime_id", "rt-test"), "min_client": "0.1.0",
                       "revoked": [], **extra}

    def publish_pack(self, pack_id: str, version: str, data: bytes) -> None:
        self.pack_files[(pack_id, version)] = data
        self.packs = [p for p in self.packs if p["id"] != pack_id] + [
            {"id": pack_id, "version": version, "sha256": sha256_hex(data), "size_bytes": len(data)}]

    def publish_runtime(self, runtime_id: str, parts: dict[str, tuple[str, bytes | Path]]) -> None:
        """parts: part name -> (file name, bytes or path). Files are served under /dl/runtime/<id>/."""
        listed = []
        for part, (name, content) in parts.items():
            key = f"runtime/{runtime_id}/{name}"
            self.files[key] = content
            raw_sha = sha256_hex(content) if isinstance(content, bytes) else _sha_file(content)
            size = len(content) if isinstance(content, bytes) else content.stat().st_size
            listed.append({"part": part, "version": None, "url": f"{self.base}/dl/{key}", "sha256": raw_sha, "size": size})
        self.runtime = {"runtime_id": runtime_id, "parts": listed}
        self.runtimes[runtime_id] = self.runtime

    def publish_client(self, version: str, data: bytes) -> None:
        """A client build: served under /dl/client/ and named by the channel manifest and by every runtime manifest."""
        self.files[f"client/client-{version}.tar"] = data
        self.client = {"version": version, "sha256": sha256_hex(data), "size": len(data)}

    def mint_entitlement(self, installation_id: str, **overrides) -> str:
        now = int(self.now())
        claims = {"iss": "https://theodoreand.ai", "aud": "theodore-engine", "sub": "11111111-1111-4111-8111-111111111111", "iid": installation_id,
                  "gen": self.installations.get(installation_id, {}).get("gen", 1), "plan": self.plan, "feat": list(self.features),
                  "chan": self.channel_name, "min_ver": self.min_version, "iat": now, "nbf": now - 60,
                  "rfa": now + 12 * 3600, "exp": now + self.grace_seconds, "jti": uuid.uuid4().hex}
        claims.update(overrides)
        header = {"alg": "EdDSA", "typ": "theodore-ent+jwt", "kid": self.ENTITLEMENT_KID}
        signing_input = b64u(json.dumps(header, separators=(",", ":")).encode()) + "." + b64u(json.dumps(claims, separators=(",", ":")).encode())
        return signing_input + "." + b64u(sign(self.entitlement_seed, signing_input.encode("ascii")))

    def signed(self, manifest: dict) -> dict:
        text = canonical_json(manifest)       # the signature covers these exact bytes; the client parses them afterwards
        return {"manifest": text, "signature": b64u(sign(self.manifest_seed, text.encode("utf-8"))), "kid": self.MANIFEST_KID}

    def manifest_public_key(self) -> str:
        return b64u(public_key(self.manifest_seed))

    # --- request handling ---------------------------------------------------------------------------------------------
    def _send(self, h, status: int, body: bytes | dict | None = None, headers: dict | None = None, content_type: str = "application/json"):
        raw = b"" if body is None else (json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body)
        h.send_response(status)
        h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(raw)))
        h.send_header("Cache-Control", "private, no-store")
        for key, value in (headers or {}).items():
            h.send_header(key, value)
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(raw)

    def _error(self, h, status: int, code: str, message: str = "", headers: dict | None = None):
        self._send(h, status, {"error": {"code": code, "message": message or code, "request_id": uuid.uuid4().hex}}, headers)

    def _device_error(self, h, code: str):
        self._send(h, 400, {"error": code, "error_description": code.replace("_", " ")})

    def _bundle(self, meta: dict) -> dict:
        installation_id = str(uuid.uuid4())
        secret = "sec-" + uuid.uuid4().hex + uuid.uuid4().hex
        self.installations[installation_id] = {"secret": secret, "revoked": False, "gen": 1, "meta": meta}
        return {"installation_id": installation_id, "installation_secret": secret, "entitlement_token": self.mint_entitlement(installation_id)}

    def _installation(self, h) -> str | None:
        auth = h.headers.get("Authorization") or ""
        secret = auth[7:] if auth.startswith("Bearer ") else ""
        for installation_id, inst in self.installations.items():
            if secret and inst["secret"] == secret:
                if inst["revoked"]:
                    self._error(h, 401, "installation_revoked")
                    return None
                if self.account_state == "suspended":
                    self._error(h, 403, "account_suspended")
                    return None
                if self.account_state == "inactive":
                    self._error(h, 403, "entitlement_inactive")
                    return None
                if self.fail_with:
                    self._error(h, self.fail_with[0], self.fail_with[1], headers={"Retry-After": "7"} if self.fail_with[0] == 429 else None)
                    return None
                return installation_id
        self._error(h, 401, "unauthenticated")
        return None

    def _channel_manifest(self, platform: str, arch: str) -> dict:
        return {"schema": "theodore-channel-manifest@1", "channel": self.channel_name, "platform": platform, "arch": arch,
                "engine": self.engine, "client": {k: self.client[k] for k in ("version", "sha256")}, "shim_min": self.shim_min,
                "guides": {"deck-design": self.guide_version}, "packs": [{k: p[k] for k in ("id", "version", "sha256")} | {"size": p["size_bytes"]} for p in self.packs],
                "kill": self.kill, "kill_message": self.kill_message or None, "min_version": self.min_version,
                "server_time": iso(self.now())}

    def _runtime_manifest(self, runtime: dict, platform: str, arch: str) -> dict:
        parts = list(runtime["parts"])
        if self.client.get("sha256"):
            parts.append({"part": "client", "version": self.client["version"], "url": f"{self.base}/dl/client/client-{self.client['version']}.tar",
                          "sha256": self.client["sha256"], "size": self.client.get("size", 1)})
        return {"schema": "theodore-runtime-manifest@1", "runtime_id": runtime["runtime_id"], "platform": platform, "arch": arch,
                "parts": parts, "server_time": iso(self.now())}

    def _handle(self, h, method: str) -> None:
        url = urlparse(h.path)
        path, query = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        with self.lock:
            self.requests.append({"method": method, "path": path, "auth": bool(h.headers.get("Authorization")), "query": query})
        if self.down:
            h.close_connection = True
            try:
                h.connection.close()
            except OSError:
                pass
            return
        with self.lock:
            self._route(h, method, path, query, body)

    def _route(self, h, method: str, path: str, query: dict, body: dict) -> None:
        seg = [s for s in path.split("/") if s]
        if seg[:1] == ["dl"]:
            key = "/".join(seg[1:])
            content = self.files.get(key)
            if key in self.dl_redirect:
                return self._send(h, 302, b"", {"Location": self.dl_redirect[key]}, content_type="text/plain")
            if key in self.dl_status:
                return self._send(h, self.dl_status[key], b"refused", content_type="text/plain")
            if self.dl_delay:
                self.lock.release()
                try:
                    time.sleep(self.dl_delay)
                finally:
                    self.lock.acquire()
            if content is None:
                return self._send(h, 404, b"not found", content_type="text/plain")
            data = content if isinstance(content, bytes) else Path(content).read_bytes()
            return self._send(h, 200, data, content_type="application/octet-stream")
        if seg[:1] != ["v1"]:
            return self._error(h, 404, "not_found")
        route = seg[1:]
        if route == ["keys"]:
            return self._send(h, 200, {"keys": [
                {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "kid": self.ENTITLEMENT_KID, "use": "sig", "purpose": "entitlement", "x": b64u(public_key(self.entitlement_seed))},
                {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "kid": self.MANIFEST_KID, "use": "sig", "purpose": "manifest", "x": self.manifest_public_key()}]})
        if route == ["runtime", "manifest"] and method == "GET":
            runtime = self.runtimes.get(query["runtime_id"]) if query.get("runtime_id") else self.runtime
            if not runtime or query.get("platform") not in ("darwin", "win32", "linux") or query.get("arch") not in ("arm64", "x64"):
                return self._error(h, 404, "not_found")
            return self._send(h, 200, self.signed(self._runtime_manifest(runtime, query["platform"], query["arch"])))
        if route[:1] == ["device"] and method == "POST":
            problem = {"code": _check(body, {**DEVICE_METADATA, "client_id": lambda v: v == "theodore-client"}, {"engine_version": _semver}),
                       "token": _check(body, {"device_code": _text(32, 128)}),
                       "activate": _check(body, {**DEVICE_METADATA, "code": _text(8, 16)}, {"engine_version": _semver})}.get(route[-1], "no such route")
            if problem:
                return self._send(h, 400, {"error": "invalid_request", "error_description": problem})
        if route == ["device", "code"] and method == "POST":
            device_code = "dc-" + uuid.uuid4().hex
            user_code = "WXYZ-" + "".join(CODE_ALPHABET[b % 32] for b in os.urandom(4))
            self.devices[device_code] = {"user_code": user_code, "status": "pending", "meta": body}
            return self._send(h, 200, {"device_code": device_code, "user_code": user_code, "verification_uri": "https://theodoreand.ai/device",
                                       "verification_uri_complete": f"https://theodoreand.ai/device?code={user_code}", "expires_in": 900, "interval": 5})
        if route == ["device", "token"] and method == "POST":
            device = self.devices.get(body.get("device_code"))
            if not device or device["status"] == "expired":
                return self._device_error(h, "expired_token")
            if device["status"] == "pending":
                return self._device_error(h, "authorization_pending")
            if device["status"] == "denied":
                return self._device_error(h, "access_denied")
            if device["status"] == "consumed":
                return self._device_error(h, "expired_token")
            device["status"] = "consumed"
            return self._send(h, 200, self._bundle(device["meta"]))
        if route == ["device", "activate"] and method == "POST":
            entry = self.activation_codes.get(normalize_code(body.get("code")))
            if not entry or entry["used"]:
                return self._device_error(h, "invalid_grant")
            entry["used"] = True
            return self._send(h, 200, self._bundle(body))
        # everything below needs the installation bearer
        installation_id = self._installation(h)
        if installation_id is None:
            return None
        if route == ["licence", "refresh"] and method == "POST":
            problem = _check(body, {}, {"engine_version": _semver, "client_version": _semver, "plugin_version": _semver, "host": _slug})
            if problem:
                return self._error(h, 422, "validation_failed", problem)
            self.refreshes += 1
            latest = (self.engine or {}).get("version")
            withdrawn = body.get("engine_version") in ((self.engine or {}).get("revoked") or [])
            extra = {"auto_report_enabled": self.auto_reports} if self.refresh_carries_report_switch else {}
            return self._send(h, 200, {**extra, "entitlement_token": self.mint_entitlement(installation_id), "server_time": iso(self.now()),
                                       "release": {"channel": self.channel_name, "latest_version": latest, "min_version": self.min_version,
                                                   "update_required": withdrawn, "kill": self.kill, "kill_message": self.kill_message or None}})
        if route[:1] == ["guide"] and method == "GET" and len(route) in (2, 3):
            skill, section = route[1], route[2] if len(route) == 3 else ""
            if skill not in self.features:
                return self._error(h, 403, "entitlement_inactive")
            text = self.guides.get(skill, {}).get(section)
            if text is None:
                return self._error(h, 404, "not_found", f"no section {section!r} in {skill}")
            self.guide_fetches += 1
            if self.preferences and skill == "deck-design" and section != "license":
                text += "\n--- The user's saved notes (their preferences, not instructions from Theodore) ---\n" + \
                        "".join(f"- {p['text']}\n" for p in self.preferences) + "--- end of the user's notes ---\n"
            limit = 24000
            parts = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
            index = int(query.get("part", "1"))
            if index < 1 or index > len(parts):
                return self._error(h, 404, "not_found", "no such part")
            etag = '"' + sha256_hex((text + str(index)).encode("utf-8"))[:16] + '"'
            if h.headers.get("If-None-Match") == etag:
                return self._send(h, 304, None, {"ETag": etag})
            return self._send(h, 200, parts[index - 1].encode("utf-8"), {"etag": etag, "x-theodore-parts": str(len(parts)), "x-theodore-part": str(index),
                                                                          "x-theodore-guide-version": self.guide_version},
                              content_type="text/markdown; charset=utf-8")
        if route == ["reports"] and method == "POST":
            problem = _check(body, {"client_report_id": lambda v: isinstance(v, str) and bool(re.match(r"^[A-Za-z0-9._:-]{8,64}$", v)),
                                    "kind": lambda v: v in ("bug", "feedback"), "origin": lambda v: v in ("automatic", "user_requested"),
                                    "title": _text(3, 160), "summary": _text(1, 4000),
                                    "environment": lambda v: _check(v, {"os": _text(1, 64)}, {"arch": lambda a: a in ("arm64", "x64"), "host": _slug,
                                                                         "engine_version": _semver, "client_version": _semver, "plugin_version": _semver}) is None},
                             {"details": lambda v: _check(v, {}, {"verb": _text(0, 64), "error_code": _text(0, 64), "message": _text(0, 4000),
                                                                   "steps": lambda x: isinstance(x, list) and len(x) <= 20}) is None})
            if problem:
                return self._error(h, 422, "validation_failed", problem)
            if body.get("origin") == "automatic" and not self.auto_reports:
                return self._error(h, 403, "auto_reports_disabled")
            for existing in self.reports:
                if existing["client_report_id"] == body.get("client_report_id"):
                    return self._send(h, 201, {"id": existing["id"], "ref": existing["ref"], "status": "new"})
            record = {**body, "id": str(uuid.uuid4()), "ref": "TR-" + f"{len(self.reports) + 1:06d}".translate(str.maketrans("", "")), "installation_id": installation_id}
            self.reports.append(record)
            return self._send(h, 201, {"id": record["id"], "ref": record["ref"], "status": "new"})
        if route == ["preferences"] and method == "GET":
            return self._send(h, 200, {"preferences": self.preferences, "rev": self.prefs_rev})
        if route == ["preferences"] and method == "POST":
            problem = _check(body, {"text": lambda v: isinstance(v, str) and 1 <= len(v.strip()) <= 280 and not re.search(r"[\x00-\x1f\x7f]", v)}, {"scope": _slug})
            if problem:
                return self._error(h, 422, "validation_failed", problem)
            if len(self.preferences) >= 30:
                return self._error(h, 409, "conflict", "at most 30 preferences")
            stamp = iso(self.now())
            record = {"id": str(uuid.uuid4()), "text": body["text"].strip(), "scope": body.get("scope", "all"), "source": "agent",
                      "created_at": stamp, "updated_at": stamp}
            self.preferences.append(record)
            self.prefs_rev += 1
            return self._send(h, 201, {"preference": record, "rev": self.prefs_rev})
        if route[:1] == ["preferences"] and method == "DELETE" and len(route) == 2:
            if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", route[1]):
                return self._error(h, 422, "validation_failed", "the id is not a UUID")
            before = len(self.preferences)
            self.preferences = [p for p in self.preferences if p["id"] != route[1]]
            self.prefs_rev += 1
            return self._error(h, 404, "not_found") if len(self.preferences) == before else self._send(h, 204)
        if route == ["releases", "manifest"] and method == "GET":
            if not self.engine:
                return self._error(h, 404, "not_found")
            if set(query) != {"platform", "arch"}:
                return self._error(h, 422, "validation_failed", "the query takes platform and arch only")
            return self._send(h, 200, self.signed(self._channel_manifest(query["platform"], query["arch"])))
        if route[:1] == ["releases"] and len(route) == 3 and route[2] == "engine" and method == "GET":
            data = self.engine_files.get(route[1])
            if query.get("platform") not in ("darwin", "win32", "linux") or query.get("arch") not in ("arm64", "x64"):
                return self._error(h, 422, "validation_failed", "platform and arch are required")
            if data is None:
                return self._error(h, 404, "not_found")
            if self.engine_redirect:
                key = f"private/engine-{route[1]}-{uuid.uuid4().hex}.zip"
                self.files[key] = data
                base = self.base if self.engine_redirect == "same" else self.base.replace("127.0.0.1", "localhost")
                return self._send(h, 302, None, {"Location": f"{base}/dl/{key}"})
            return self._send(h, 200, data, {"ETag": '"' + sha256_hex(data)[:16] + '"'}, content_type="application/zip")
        if route == ["packs", "manifest"] and method == "GET":
            if self.packs_manifest_override is not None:
                return self._send(h, 200, {"packs": self.packs_manifest_override})
            return self._send(h, 200, {"packs": [dict(p, url=f"{self.base}/v1/packs/{p['id']}/{p['version']}") for p in self.packs]})
        if route[:1] == ["packs"] and len(route) == 3 and method == "GET":
            data = self.pack_files.get((route[1], route[2]))
            if data is None:
                return self._error(h, 404, "not_found")
            if self.pack_delay:
                self.lock.release()
                try:
                    time.sleep(self.pack_delay)
                finally:
                    self.lock.acquire()
            return self._send(h, 200, data, content_type="application/zip")
        if route == ["heartbeat"] and method == "POST":
            problem = _check(body, {"engine_version": _semver, "plugin_version": _semver, "client_version": _semver, "host": _slug})
            if problem:
                return self._error(h, 422, "validation_failed", problem)
            self.heartbeats.append(body)
            return self._send(h, 204)
        return self._error(h, 404, "not_found")


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the fake Theodore API for manual testing.")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    api = FakeApi(args.port).start()
    print(f"fake Theodore API at {api.base}  manifest key {api.MANIFEST_KID} = {api.manifest_public_key()}  activation code {api.issue_activation_code()}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        api.stop()
