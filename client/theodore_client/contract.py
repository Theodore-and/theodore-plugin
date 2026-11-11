"""The shapes this client shares with the Theodore API and with the launcher.

Everything another party depends on is in this one file: launcher exit codes and status names, the API routes
the client calls, the API error codes and how each maps to a status, the entitlement claim names, and the
limits the client enforces before the server does. `tests/test_contract.py` checks this file against the
backend's `openapi.json` snapshot when that file is available.
"""

from __future__ import annotations

# --- launcher and client statuses (design section 6.2) ----------------------------------------------------------
# Exit codes 0 to 63 belong to the engine and pass through unchanged.
EXIT = {
    "OK": 0,
    "USAGE": 64,
    "SIGN_IN_REQUIRED": 65,
    "SIGN_IN_PENDING": 66,
    "SETUP_IN_PROGRESS": 67,
    "SETUP_FAILED": 68,
    "UNSUPPORTED_PLATFORM": 69,
    "NO_ENTITLEMENT": 70,
    "OFFLINE": 71,
    "UPDATE_REQUIRED": 72,
    "BLOCKED_BY_POLICY": 73,
    "CONNECTOR_REQUIRED": 74,
    "RETRY": 75,
    "INTERNAL": 78,
}
SETUP_FAILED_CAUSES = ("NETWORK", "TLS_INTERCEPT", "PROXY_AUTH", "DISK", "HASH_MISMATCH")

# --- routes on the API host (backend design section 5.4) ---------------------------------------------------------
# name: (method, path template, needs the installation bearer)
ROUTES = {
    "device_code": ("POST", "/v1/device/code", False),
    "device_token": ("POST", "/v1/device/token", False),
    "device_activate": ("POST", "/v1/device/activate", False),
    "licence_refresh": ("POST", "/v1/licence/refresh", True),
    "installation_rotate": ("POST", "/v1/installation/rotate", True),
    "guide": ("GET", "/v1/guide/{skill}", True),
    "guide_section": ("GET", "/v1/guide/{skill}/{section}", True),
    "guide_specimen": ("GET", "/v1/guide/specimens/{file}", True),       # ?skill=deck-design; image/jpeg, X-Theodore-Sha256
    "reports": ("POST", "/v1/reports", True),
    "preferences_list": ("GET", "/v1/preferences", True),
    "preferences_add": ("POST", "/v1/preferences", True),
    "preferences_remove": ("DELETE", "/v1/preferences/{id}", True),
    "releases_manifest": ("GET", "/v1/releases/manifest", True),
    "release_engine": ("GET", "/v1/releases/{version}/engine", True),
    "packs_manifest": ("GET", "/v1/packs/manifest", True),
    "pack": ("GET", "/v1/packs/{id}/{version}", True),
    "heartbeat": ("POST", "/v1/heartbeat", True),
    "runtime_manifest": ("GET", "/v1/runtime/manifest", False),
    "keys": ("GET", "/v1/keys", False),
}

# Routes the backend is adding and its published contract (openapi.json, snapshotted in tests/fixtures) does not list
# yet. The contract test lets these through, and fails as soon as the snapshot has one of them: then remove it here.
PENDING_ROUTES: tuple[str, ...] = ()

# --- API error codes (backend design section 5) and the status each becomes -----------------------------------------
# code: (HTTP status, launcher status name)
API_ERRORS = {
    "unauthenticated": (401, "SIGN_IN_REQUIRED"),
    "installation_revoked": (401, "SIGN_IN_REQUIRED"),
    "forbidden": (403, "NO_ENTITLEMENT"),
    "account_suspended": (403, "NO_ENTITLEMENT"),
    "entitlement_inactive": (403, "NO_ENTITLEMENT"),
    "auto_reports_disabled": (403, "BLOCKED_BY_POLICY"),
    "not_found": (404, "USAGE"),
    "conflict": (409, "USAGE"),
    "payload_too_large": (413, "USAGE"),
    "validation_failed": (422, "USAGE"),
    "update_required": (426, "UPDATE_REQUIRED"),
    "rate_limited": (429, "RETRY"),
    "internal_error": (500, "RETRY"),
    "service_disabled": (503, "RETRY"),
}
# Codes after which the stored entitlement token is deleted (backend section 6: "a 401 or 403 deletes it").
DELETE_TOKEN_ON = ("unauthenticated", "installation_revoked", "forbidden", "account_suspended", "entitlement_inactive")
# Codes after which the stored installation credential is deleted as well (the device must sign in again).
DELETE_CREDENTIAL_ON = ("unauthenticated", "installation_revoked")

# Device endpoints answer in RFC 6749 form: {"error": ..., "error_description": ...}.
DEVICE_ERRORS = ("authorization_pending", "slow_down", "access_denied", "expired_token", "invalid_grant", "invalid_request")
DEVICE_CLIENT_ID = "theodore-client"

# --- entitlement token (backend design section 6) --------------------------------------------------------------------
ENTITLEMENT_TYP = "theodore-ent+jwt"
ENTITLEMENT_ALG = "EdDSA"
ENTITLEMENT_AUD = "theodore-engine"
ENTITLEMENT_CLAIMS = ("iss", "aud", "sub", "iid", "gen", "plan", "feat", "chan", "min_ver", "iat", "nbf", "rfa", "exp", "jti")
ENTITLEMENT_ENV = "THEODORE_ENTITLEMENT"
# state/entitlement.json, read by the engine's licence check: {"entitlement_token": "<JWS compact>", ...}
ENTITLEMENT_FILE_KEY = "entitlement_token"

# --- limits the client applies before the server does ------------------------------------------------------------------
GUIDE_PART_CHARS = 24000          # no single printed document goes over this; longer ones are paged
UPDATE_CHECK_INTERVAL_S = 6 * 3600
UPDATE_CHECK_TIMEOUT_S = 3
PREFERENCE_MAX_CHARS = 280
PREFERENCE_MAX_COUNT = 30
REPORT_MAX_BYTES = 64 * 1024
# A one-time code as typed: the display form is XXXX-XXXX from the alphabet 0123456789ABCDEFGHJKMNPQRSTVWXYZ; the
# server accepts any case and an optional hyphen, so the client only checks the rough shape before sending.
ACTIVATION_CODE_PATTERN = r"^[0-9A-Za-z]{4}[- ]?[0-9A-Za-z]{4}$"
PACK_ORDER = ("icons", "flags", "backgrounds", "maps")
ENGINE_KEEP_MAX = 4
ENGINE_KEEP_USED_DAYS = 14
ENGINE_KEEP_PINNED_DAYS = 30

SKILLS = ("pptx", "deck-design")
PLATFORMS = ("darwin-arm64", "darwin-x64", "win32-x64", "linux-x64", "linux-arm64")
RUNTIME_PARTS = ("python", "wheels", "node", "node_modules")

# Response headers of the guide route.
HEADER_GUIDE_PARTS = "x-theodore-parts"
HEADER_GUIDE_PART = "x-theodore-part"
HEADER_GUIDE_VERSION = "x-theodore-guide-version"

# Signed manifests: `manifest` is canonical JSON text; the signature covers its UTF-8 bytes.
CHANNEL_MANIFEST_SCHEMA = "theodore-channel-manifest@1"
RUNTIME_MANIFEST_SCHEMA = "theodore-runtime-manifest@1"
# The plugin release document the installer trusts: <dl>/plugin/latest.json, a signed envelope like the manifests
# ({manifest, signature, kid}, the manifest key), whose manifest is canonical JSON of
# {"schema": PLUGIN_LATEST_SCHEMA, "version", "archive": "theodore-plugin-<version>.tar.gz", "sha256", "size"}.
PLUGIN_LATEST_SCHEMA = "theodore-plugin-latest@1"
KNOWN_HOSTS = ("claude-code", "codex", "claude-desktop", "claude-web", "cowork", "chatgpt", "other")
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
REPORT_TITLE_MAX, REPORT_SUMMARY_MAX, REPORT_MESSAGE_MAX = 160, 4000, 4000

# What the engine prints (one JSON object, exit code 1) when its own licence check refuses, and the status each
# becomes. The engine checks offline; the client turns the refusal into something the assistant can act on.
ENGINE_REFUSALS = {
    "entitlement_missing": "SIGN_IN_REQUIRED",
    "entitlement_malformed": "NO_ENTITLEMENT",
    "entitlement_untrusted": "NO_ENTITLEMENT",
    "entitlement_wrong_audience": "NO_ENTITLEMENT",
    "entitlement_feature_missing": "NO_ENTITLEMENT",
    "entitlement_expired": "OFFLINE",
    "entitlement_not_yet_valid": "NO_ENTITLEMENT",
    "entitlement_clock_rollback": "NO_ENTITLEMENT",
    # State/ is missing, or state/engine-clock.json cannot be read or written (entitlement_state_unavailable): signing in again
    # recreates state/.
    "entitlement_state_unavailable": "NO_ENTITLEMENT",
}
