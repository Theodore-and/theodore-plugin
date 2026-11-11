"""Where the installation credential lives (design section 9).

macOS: the login keychain through /usr/bin/security (no prompt when the Python path changes).
Windows: a file protected with DPAPI for the current user.
Elsewhere, and when the keychain cannot be used (for example over SSH): a 0600 file under state/.

The credential is {"installation_id", "installation_secret"}. It is never printed or logged.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from . import core

SERVICE = "ai.theodoreand.theodore"
SECURITY = "/usr/bin/security"
KEYCHAIN_TIMEOUT_S = 10         # a keychain that shows a system dialog never answers: no call may wait for it
KEYCHAIN_SKIP_S = 86400


def _skip_marker() -> Path:
    return core.home().state / "keychain-unavailable"


def _tombstone() -> Path:
    """The keychain item of this home holds a credential that must not be used again (it was revoked or signed out,
    and deleting the item failed or timed out). While it exists the item is never read, and every load tries to delete
    it again; a successful keychain store replaces the item and removes the tombstone."""
    return core.home().state / "keychain-revoked"


def _holds_marker() -> Path:
    """Written when a credential is stored in the keychain, removed when its deletion is confirmed: whether an item of
    ours may be there, which a keychain that does not answer cannot tell."""
    return core.home().state / "keychain-holds"


def _keychain_skipped() -> bool:
    try:
        return 0 <= core.now() - _skip_marker().stat().st_mtime <= KEYCHAIN_SKIP_S
    except OSError:
        return False


def _security(arguments: list[str], command: str | None = None):
    """One call of /usr/bin/security with a time limit. None when it did not answer: the keychain is then left
    alone for a day (the 0600 file is used instead) and one line on standard error says so."""
    if _keychain_skipped():
        return None
    try:
        return subprocess.run([SECURITY, *arguments], input=command, text=True, capture_output=True, timeout=KEYCHAIN_TIMEOUT_S,
                              **({} if command is not None else {"stdin": subprocess.DEVNULL}))
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            core.write_atomic(_skip_marker(), str(int(core.now())), 0o600)
        print(f"theodore: the macOS keychain did not answer within {KEYCHAIN_TIMEOUT_S} seconds (a system dialog may be waiting); "
              "the sign-in is kept in a file only this user can read instead.", file=sys.stderr, flush=True)
        return None
    except OSError:
        return None


def _account() -> str:
    # One item per data directory, so a second data directory (a test home) never collides with the real one.
    return "installation-" + hashlib.sha256(str(core.home().root).encode("utf-8")).hexdigest()[:12]


def _file() -> Path:
    return core.home().state / "credentials"


def _keychain_args() -> list[str]:
    # Test seam: a keychain file of the test's own, honoured only when the API base is a loopback address.
    path = os.environ.get("THEODORE_TEST_KEYCHAIN")
    return [path] if path and core.is_loopback_api() else []


def backend() -> str:
    choice = os.environ.get("THEODORE_CREDENTIAL_STORE", "").lower()
    if choice in ("file", "keychain", "dpapi"):
        return choice
    if sys.platform == "darwin" and os.path.exists(SECURITY):
        return "keychain"
    if core.IS_WINDOWS:
        return "dpapi"
    return "file"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    source = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not func(ctypes.byref(source), None, None, None, None, 0x01, ctypes.byref(target)):  # CRYPTPROTECT_UI_FORBIDDEN
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _keychain_store(secret: str) -> bool:
    # The command goes through stdin (`security -i`), so the secret never appears in a process argument list.
    # The value is base64url text, so it needs no quoting inside the command line.
    encoded = base64.urlsafe_b64encode(secret.encode("utf-8")).decode("ascii")
    command = f'add-generic-password -a "{_account()}" -s "{SERVICE}" -l "Theodore" -w "{encoded}" -U'
    for extra in _keychain_args():
        command += f' "{extra}"'
    done = _security(["-i"], command + "\n")
    return done is not None and done.returncode == 0 and "error" not in (done.stderr or "").lower()


def _keychain_load() -> str | None:
    done = _security(["find-generic-password", "-a", _account(), "-s", SERVICE, "-w", *_keychain_args()])
    if done is None or done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        return base64.urlsafe_b64decode(done.stdout.strip()).decode("utf-8")
    except ValueError:
        return None


def _keychain_delete() -> bool:
    """True when no item is left: deleted, or there was none (security exits 44 for "not found"). A keychain left
    alone after a timeout cannot confirm it: True only when no credential was ever stored there (no holds marker).
    A deletion that is not confirmed leaves the tombstone, so the old item is never used again."""
    if _keychain_skipped():         # nothing can be confirmed: an item known or suspected to be there stays tombstoned
        gone = not _holds_marker().exists() and not _tombstone().exists()
    else:
        done = _security(["delete-generic-password", "-a", _account(), "-s", SERVICE, *_keychain_args()])
        gone = done is not None and done.returncode in (0, 44)
    if gone:
        core.remove_tree(_tombstone())
        core.remove_tree(_holds_marker())
    else:
        with contextlib.suppress(OSError):
            core.write_atomic(_tombstone(), str(int(core.now())), 0o600)
    return gone


def store(installation_id: str, installation_secret: str) -> str:
    """Save the credential. Returns the backend that holds it."""
    payload = json.dumps({"installation_id": installation_id, "installation_secret": installation_secret}, separators=(",", ":"))
    kind = backend()
    if kind == "keychain":
        if _keychain_store(payload):
            core.write_atomic(_holds_marker(), str(int(core.now())), 0o600)
            core.remove_tree(_tombstone())          # the item now holds this credential, not the revoked one
            core.remove_tree(_file())
            return "keychain"
        kind = "file"  # locked or unavailable keychain (for example an SSH session): fall back to the 0600 file
        _keychain_delete()  # an older item must not win later; when the keychain refuses this too, load() prefers the file
    if kind == "dpapi":
        core.write_atomic(_file(), b"DPAPI1" + _dpapi(payload.encode("utf-8"), True), 0o600)
        return "dpapi"
    core.write_atomic(_file(), payload, 0o600)
    return "file"


def load() -> dict | None:
    # The file wins when it exists: a successful keychain store deletes it, so a file beside a keychain item is
    # always the newer credential (saved while the keychain was locked).
    raw: str | None = None
    if backend() == "keychain" and _tombstone().exists():
        _keychain_delete()          # retried on every run until it is confirmed; the item is never read meanwhile
    elif backend() == "keychain" and not _file().exists():
        raw = _keychain_load()
        if raw and not _holds_marker().exists():          # stored by a client from before the marker
            with contextlib.suppress(OSError):
                core.write_atomic(_holds_marker(), str(int(core.now())), 0o600)
    if raw is None and _file().exists():
        try:
            data = _file().read_bytes()
            raw = _dpapi(data[6:], False).decode("utf-8") if data.startswith(b"DPAPI1") else data.decode("utf-8")
        except (OSError, ValueError):
            raw = None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if isinstance(value, dict) and value.get("installation_id") and value.get("installation_secret"):
        core.register_secret(value["installation_secret"])
        return value
    return None


def delete() -> bool:
    """Remove the credential. False when the keychain item could not be deleted (it is then still there)."""
    gone = _keychain_delete() if backend() == "keychain" else True
    core.remove_tree(_file())
    return gone and not _file().exists()


def describe() -> str:
    kind = backend()
    if kind == "keychain" and _file().exists():
        return "file (0600; the keychain was not available)"
    return {"keychain": "macOS keychain", "dpapi": "DPAPI-protected file", "file": "file (0600)"}[kind]
