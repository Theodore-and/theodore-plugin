"""A local stand-in for the Theodore API and download host, for tests. Standard library only.

It implements the routes of the frozen design (backend design section 5.4): device and activation flow, licence
refresh, guide, signed release and runtime manifests, engine and pack downloads, reports, preferences,
heartbeat and keys, plus plain file serving under /dl/ for the runtime parts, the client and the plugin.
State lives on the FakeApi object, so a test changes the server by assigning to it.

Run it by hand:  python3 tests/fake_api.py --port 8787   (prints its base URL and test public key)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
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


class FakeApi:
    MANIFEST_KID = "manifest-test-1"
    ENTITLEMENT_KID = "ent-test-1"

    def __init__(self, port: int = 0):
        self.manifest_seed = hashlib.sha256(b"theodore test manifest key").digest()
        self.entitlement_seed = hashlib.sha256(b"theodore test entitlement key").digest()
        self.lock = threading.RLock()
        self.clock_offset = 0.0
        self.down = False                 # True: every request gets a dropped connection
        self.fail_with = None             # (http status, error code) for every authenticated /v1 call
        self.requests: list[dict] = []    # {method, path, auth} of every request, for assertions
        self.files: dict[str, bytes | Path] = {}      # /dl/<key> -> content
        self.dl_status: dict[str, int] = {}           # /dl/<key> -> forced HTTP status (for example 407)
        self.activation_codes: dict[str, dict] = {}   # code -> {used: bool}
        self.devices: dict[str, dict] = {}            # device_code -> {user_code, status}
        self.installations: dict[str, dict] = {}      # id -> {secret, revoked, gen}
        self.plan, self.features, self.channel_name = "beta", ["pptx", "deck-design"], "beta"
        self.grace_seconds = 7 * 86400
        self.account_state = "active"     # or "suspended" / "inactive"
        self.auto_reports = True
        self.engine: dict | None = None   # {version, sha256, size, runtime_id, min_client, revoked: [], kind}
        self.engine_files: dict[str, bytes] = {}
        self.engine_redirect = False      # True: answer the engine route with a 302 to /dl/private/...
        self.client = {"version": "0.1.0", "sha256": ""}
        self.shim_min = "0.1.0"
        self.min_version = None
        self.kill, self.kill_message = False, ""
        self.tools: dict = {}
        self.runtime: dict | None = None  # {runtime_id, parts: [{part, file, sha256, size}]} (urls are filled in)
        self.packs: list[dict] = []       # {id, version, sha256, size_bytes}
        self.pack_files: dict[tuple[str, str], bytes] = {}
        self.pack_delay = 0.0             # seconds to stall each pack download (to test waiting)
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
        self.activation_codes[code] = {"used": False}
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
            listed.append({"part": part, "url": f"{self.base}/dl/{key}", "sha256": raw_sha, "size": size})
        self.runtime = {"runtime_id": runtime_id, "parts": listed}

    def mint_entitlement(self, installation_id: str, **overrides) -> str:
        now = int(self.now())
        claims = {"iss": "https://theodoreand.ai", "aud": "theodore-engine", "sub": "user-test-1", "iid": installation_id,
                  "gen": self.installations.get(installation_id, {}).get("gen", 1), "plan": self.plan, "feat": list(self.features),
                  "chan": self.channel_name, "min_ver": self.min_version or "0.0.0", "iat": now, "nbf": now - 60,
                  "rfa": now + 12 * 3600, "exp": now + self.grace_seconds, "jti": uuid.uuid4().hex}
        claims.update(overrides)
        header = {"alg": "EdDSA", "typ": "theodore-ent+jwt", "kid": self.ENTITLEMENT_KID}
        signing_input = b64u(json.dumps(header, separators=(",", ":")).encode()) + "." + b64u(json.dumps(claims, separators=(",", ":")).encode())
        return signing_input + "." + b64u(sign(self.entitlement_seed, signing_input.encode("ascii")))

    def signed(self, manifest: dict) -> dict:
        text = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
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
        installation_id = "inst-" + uuid.uuid4().hex[:12]
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

    def _channel_manifest(self) -> dict:
        return {"channel": self.channel_name, "engine": self.engine, "client": self.client, "shim_min": self.shim_min,
                "guides": {"deck-design": self.guide_version}, "packs": [{k: p[k] for k in ("id", "version", "sha256")} | {"size": p["size_bytes"]} for p in self.packs],
                "kill": self.kill, "kill_message": self.kill_message, "min_version": self.min_version, "tools": self.tools,
                "server_time": int(self.now())}

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
            if key in self.dl_status:
                return self._send(h, self.dl_status[key], b"refused", content_type="text/plain")
            if content is None:
                return self._send(h, 404, b"not found", content_type="text/plain")
            data = content if isinstance(content, bytes) else Path(content).read_bytes()
            return self._send(h, 200, data, content_type="application/octet-stream")
        if seg[:1] != ["v1"]:
            return self._error(h, 404, "not_found")
        route = seg[1:]
        if route == ["keys"]:
            return self._send(h, 200, {"keys": [
                {"kty": "OKP", "crv": "Ed25519", "kid": self.ENTITLEMENT_KID, "use": "entitlement", "x": b64u(public_key(self.entitlement_seed))},
                {"kty": "OKP", "crv": "Ed25519", "kid": self.MANIFEST_KID, "use": "manifest", "x": self.manifest_public_key()}]})
        if route == ["runtime", "manifest"] and method == "GET":
            if not self.runtime:
                return self._error(h, 503, "service_disabled")
            return self._send(h, 200, self.signed({**self.runtime, "platform": query.get("platform"), "arch": query.get("arch"),
                                                   "server_time": int(self.now())}))
        if route == ["device", "code"] and method == "POST":
            device_code, user_code = "dc-" + uuid.uuid4().hex, "WXYZ-" + uuid.uuid4().hex[:4].upper()
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
            entry = self.activation_codes.get(body.get("code"))
            if not entry or entry["used"]:
                return self._device_error(h, "invalid_grant")
            entry["used"] = True
            return self._send(h, 200, self._bundle(body))
        # everything below needs the installation bearer
        installation_id = self._installation(h)
        if installation_id is None:
            return None
        if route == ["licence", "refresh"] and method == "POST":
            self.refreshes += 1
            latest = (self.engine or {}).get("version")
            return self._send(h, 200, {"entitlement_token": self.mint_entitlement(installation_id), "server_time": int(self.now()),
                                       "release": {"channel": self.channel_name, "latest_version": latest, "min_version": self.min_version,
                                                   "update_required": False, "kill": self.kill}})
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
            return self._send(h, 200, parts[index - 1].encode("utf-8"), {"ETag": etag, "X-Theodore-Parts": str(len(parts)),
                                                                          "X-Theodore-Guide-Version": self.guide_version},
                              content_type="text/markdown; charset=utf-8")
        if route == ["reports"] and method == "POST":
            if body.get("origin") == "automatic" and not self.auto_reports:
                return self._error(h, 403, "auto_reports_disabled")
            for existing in self.reports:
                if existing["client_report_id"] == body.get("client_report_id"):
                    return self._send(h, 201, {"id": existing["id"], "ref": existing["ref"], "status": "new"})
            record = {**body, "id": uuid.uuid4().hex, "ref": f"R-{len(self.reports) + 1:04d}", "installation_id": installation_id}
            self.reports.append(record)
            return self._send(h, 201, {"id": record["id"], "ref": record["ref"], "status": "new"})
        if route == ["preferences"] and method == "GET":
            return self._send(h, 200, {"preferences": self.preferences})
        if route == ["preferences"] and method == "POST":
            text = str(body.get("text") or "")
            if not text or len(text) > 280 or "\n" in text:
                return self._error(h, 422, "validation_failed", "a preference is one line of at most 280 characters")
            if len(self.preferences) >= 30:
                return self._error(h, 409, "conflict", "at most 30 preferences")
            record = {"id": "p" + uuid.uuid4().hex[:8], "text": text, "source": body.get("source", "agent"), "created_at": int(self.now() * 1000)}
            self.preferences.append(record)
            return self._send(h, 201, record)
        if route[:1] == ["preferences"] and method == "DELETE" and len(route) == 2:
            before = len(self.preferences)
            self.preferences = [p for p in self.preferences if p["id"] != route[1]]
            return self._error(h, 404, "not_found") if len(self.preferences) == before else self._send(h, 204)
        if route == ["releases", "manifest"] and method == "GET":
            if not self.engine:
                return self._error(h, 503, "service_disabled")
            return self._send(h, 200, self.signed(self._channel_manifest()))
        if route[:1] == ["releases"] and len(route) == 3 and route[2] == "engine" and method == "GET":
            data = self.engine_files.get(route[1])
            if data is None:
                return self._error(h, 404, "not_found")
            if self.engine_redirect:
                key = f"private/engine-{route[1]}-{uuid.uuid4().hex}.zip"
                self.files[key] = data
                return self._send(h, 302, None, {"Location": f"{self.base.replace('127.0.0.1', 'localhost')}/dl/{key}"})
            return self._send(h, 200, data, {"ETag": '"' + sha256_hex(data)[:16] + '"'}, content_type="application/zip")
        if route == ["packs", "manifest"] and method == "GET":
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
