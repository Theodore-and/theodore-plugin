"""Device binding, client side (design: the backend's planning/lanes/requests/DEVICE-BINDING.md).

Each installation has its own P-256 key. The private key is kept inside the installation credential, in the same
store (macOS keychain, Windows DPAPI, else a 0600 file: credentials.py), so it goes wherever the credential goes and
is deleted with it; it is never sent. The public key is registered once with POST /v1/installation/bind. A licence
refresh then carries a proof that only this computer can make (a JWS signed with the key over a single-use nonce from
POST /v1/licence/nonce and the hash of the bearer), and `mid`: an HMAC of this computer's identifier keyed with the
installation id, which the engine can compare offline. The raw identifier never leaves the computer.

Everything here is optional for the command that runs: a server that does not know these routes (404), a key that
cannot be made or stored, a network failure on the way: the refresh goes ahead without a proof, as before.

The arithmetic is plain Python (no third-party package in the client); the signature nonce is derived as RFC 6979
says, so no random number is needed when signing.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time

from . import core

# --- P-256 (FIPS 186-4, D.1.2.3) ---------------------------------------------------------------------------------------------
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _double(pt):
    x, y, z = pt
    if not y or not z:
        return (0, 1, 0)
    yy = y * y % P
    s = 4 * x * yy % P
    zz = z * z % P
    m = 3 * (x - zz) * (x + zz) % P          # a = -3
    x3 = (m * m - 2 * s) % P
    return (x3, (m * (s - x3) - 8 * yy * yy) % P, 2 * y * z % P)


def _add(p1, p2):
    if not p1[2]:
        return p2
    if not p2[2]:
        return p1
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    z1z1, z2z2 = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2z2 % P, x2 * z1z1 % P
    s1, s2 = y1 * z2 * z2z2 % P, y2 * z1 * z1z1 % P
    if u1 == u2:
        return _double(p1) if s1 == s2 else (0, 1, 0)
    h, r = (u2 - u1) % P, (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    return (x3, (r * (v - x3) - s1 * hhh) % P, h * z1 * z2 % P)


def _multiply(k: int, point=G):
    result, addend = (0, 1, 0), (point[0], point[1], 1)
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _double(addend)
        k >>= 1
    return result


def _affine(pt):
    if not pt[2]:
        return None
    zi = pow(pt[2], -1, P)
    zi2 = zi * zi % P
    return (pt[0] * zi2 % P, pt[1] * zi2 * zi % P)


def on_curve(x: int, y: int) -> bool:
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def public_point(d: int) -> tuple[int, int]:
    return _affine(_multiply(d))


def _bits2int(data: bytes) -> int:
    return int.from_bytes(data, "big")          # SHA-256 output is exactly the order's length


def _rfc6979_k(d: int, digest: bytes):
    """The deterministic nonces of RFC 6979, section 3.2, HMAC-SHA256: the first is used unless it is out of range."""
    x = d.to_bytes(32, "big")
    h1 = (_bits2int(digest) % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = _bits2int(v)
        if 1 <= candidate < N:
            yield candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(d: int, message: bytes) -> bytes:
    """ECDSA P-256 with SHA-256: the 64-byte r || s a JWS ES256 signature is."""
    digest = hashlib.sha256(message).digest()
    e = _bits2int(digest) % N
    for k in _rfc6979_k(d, digest):
        r = _affine(_multiply(k))[0] % N
        if not r:
            continue
        s = pow(k, -1, N) * (e + r * d) % N
        if s:
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    raise AssertionError("unreachable")


def verify(point: tuple[int, int], message: bytes, signature: bytes) -> bool:
    if len(signature) != 64 or not on_curve(*point):
        return False
    r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    if not (1 <= r < N and 1 <= s < N):
        return False
    e = _bits2int(hashlib.sha256(message).digest()) % N
    w = pow(s, -1, N)
    total = _affine(_add(_multiply(e * w % N), _multiply(r * w % N, point)))
    return total is not None and total[0] % N == r


def new_private_key() -> int:
    return 1 + secrets.randbelow(N - 1)


# --- encodings ---------------------------------------------------------------------------------------------------------------

def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwk(d: int) -> dict:
    x, y = public_point(d)
    return {"kty": "EC", "crv": "P-256", "x": b64u(x.to_bytes(32, "big")), "y": b64u(y.to_bytes(32, "big"))}


def thumbprint(key: dict) -> str:
    """RFC 7638: SHA-256 of the required members in lexicographic order, no spaces."""
    canonical = '{"crv":"%s","kty":"%s","x":"%s","y":"%s"}' % (key["crv"], key["kty"], key["x"], key["y"])
    return b64u(hashlib.sha256(canonical.encode("utf-8")).digest())


def encode_key(d: int) -> str:
    return b64u(d.to_bytes(32, "big"))


def decode_key(text) -> int | None:
    """The private scalar from its stored form (43 base64url characters), or None for anything else."""
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", text):
        return None
    try:
        d = int.from_bytes(b64u_decode(text), "big")
    except ValueError:
        return None
    return d if 1 <= d < N else None


def proof(d: int, *, htu: str, nonce: str, bearer: str, now: float | None = None) -> str:
    """The Theodore-Proof header of a licence refresh: a compact JWS (ES256) over {htm, htu, iat, jti, nonce, ath}."""
    header = {"alg": "ES256", "typ": "theodore-proof+jwt"}
    payload = {"htm": "POST", "htu": htu, "iat": int(now if now is not None else time.time()), "jti": b64u(secrets.token_bytes(16)),
               "nonce": nonce, "ath": b64u(hashlib.sha256(bearer.encode("utf-8")).digest())}
    signing_input = (b64u(json.dumps(header, separators=(",", ":")).encode("utf-8")) + "." +
                     b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8")))
    return signing_input + "." + b64u(sign(d, signing_input.encode("ascii")))


# --- this computer's identifier ------------------------------------------------------------------------------------------------

MID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MACHINE: list = []


def machine_id() -> str | None:
    """A stable identifier of this computer, read where the operating system keeps it; None when it cannot be read.
    Test seam: THEODORE_TEST_MACHINE_ID, honoured only when the API base is a loopback address."""
    if _MACHINE:
        return _MACHINE[0]
    value = None
    with contextlib.suppress(Exception):
        test = os.environ.get("THEODORE_TEST_MACHINE_ID")
        if test is not None and core.is_loopback_api():
            value = test.strip() or None
        elif sys.platform == "darwin":
            out = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                                 timeout=5, stdin=subprocess.DEVNULL).stdout
            found = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            value = found.group(1).strip() if found else None
        elif core.IS_WINDOWS:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip() or None
        else:
            for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                with contextlib.suppress(OSError):
                    text = open(path, encoding="utf-8").read().strip()
                    if text:
                        value = text
                        break
    _MACHINE.append(value)
    return value


def mid(installation_id: str) -> str | None:
    """base64url(HMAC-SHA256(key = "theodore-mid-v1" + installation id, machine id)): the same on this computer for
    this installation, different for two installations on one computer, and nothing that names the computer."""
    raw = machine_id()
    if not raw or not installation_id:
        return None
    value = b64u(hmac.new(("theodore-mid-v1" + installation_id).encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).digest())
    return value if MID_PATTERN.match(value) else None


# --- the key, the binding and the proof at refresh ---------------------------------------------------------------------------

BINDING = "binding.json"            # state/: {jkt, bound_at} of the key the server has; unsupported_until for an older server
UNSUPPORTED_RETRY_S = 86400


def _binding_path():
    return core.home().state / BINDING


def _binding() -> dict:
    value = core.read_json(_binding_path())
    return value if isinstance(value, dict) else {}


def _save_binding(**fields) -> None:
    with contextlib.suppress(OSError):
        state = _binding()
        state.update(fields)
        core.write_json(_binding_path(), state, 0o600)


def forget() -> None:
    core.remove_tree(_binding_path())


def _unsupported() -> bool:
    return core.now() < float(_binding().get("unsupported_until") or 0)


def ensure_key(cred: dict) -> dict:
    """The credential with a device key: one is made and stored with it when it has none (an installation from before
    0.3.9, or a fresh log in). Returns the credential unchanged when the key cannot be stored."""
    if decode_key(cred.get("device_key")) is not None:
        return cred
    from . import credentials
    d = new_private_key()
    if credentials.store(cred["installation_id"], cred["installation_secret"], device_key=encode_key(d)) is None:
        return cred
    stored = credentials.load() or {}
    if decode_key(stored.get("device_key")) == d:
        core.log("device: a key was made for this installation")
        return stored
    return cred


def bind(cred: dict, timeout: float = 8.0) -> bool:
    """Register this installation's public key (once; the same key again is answered as bound). False, quietly, when
    the server does not know the route (it is tried again a day later) or anything else fails."""
    from . import api, credentials
    d = decode_key(cred.get("device_key"))
    if d is None:
        return False
    key = jwk(d)
    jkt = thumbprint(key)
    if _binding().get("jkt") == jkt:
        return True
    body = {"device_key": {"jwk": key, "hw": credentials.hardware()}}
    value = mid(cred["installation_id"])
    if value:
        body["mid"] = value
    try:
        api.call_json("installation_bind", bearer=cred["installation_secret"], body=body, timeout=timeout)
    except api.ApiError as error:
        if error.http_status in (404, 405):
            _save_binding(unsupported_until=int(core.now() + UNSUPPORTED_RETRY_S))
        elif error.code == "device_key_exists":
            core.log("device: the service holds another key for this installation; the refresh goes without a proof")
        else:
            core.log(f"device: not bound ({error.code})")
        return False
    except api.NetworkError as error:
        core.log(f"device: not bound ({error.cause})")
        return False
    _save_binding(jkt=jkt, bound_at=int(core.now()), unsupported_until=0)
    core.log("device: this installation's key is registered")
    return True


def for_refresh(cred: dict, timeout: float = 8.0) -> tuple[dict, dict]:
    """(headers, body fields) for a licence refresh: the proof and `mid` when this installation is bound, else
    nothing. Never raises for a failure of its own; an answer that signs this computer out is the refresh's to report."""
    from . import api
    try:
        if _unsupported():
            return {}, {}
        cred = ensure_key(cred)
        if not bind(cred, timeout):
            return {}, {}
        d = decode_key(cred.get("device_key"))
        try:
            answer = api.call_json("licence_nonce", bearer=cred["installation_secret"], body={}, timeout=timeout)
        except api.ApiError as error:
            if error.http_status in (404, 405):
                _save_binding(unsupported_until=int(core.now() + UNSUPPORTED_RETRY_S))
            else:
                core.log(f"device: no nonce ({error.code})")
            return {}, {}
        except api.NetworkError as error:
            core.log(f"device: no nonce ({error.cause})")
            return {}, {}
        nonce = str(answer.get("nonce") or "")
        if not nonce:
            return {}, {}
        headers = {"Theodore-Proof": proof(d, htu=api.url_for("licence_refresh"), nonce=nonce, bearer=cred["installation_secret"])}
        value = mid(cred["installation_id"])
        return headers, ({"mid": value} if value else {})
    except Exception as exc:        # noqa: BLE001  binding never stands in the way of the licence
        core.log(f"device: refresh without a proof ({exc.__class__.__name__})")
        return {}, {}
