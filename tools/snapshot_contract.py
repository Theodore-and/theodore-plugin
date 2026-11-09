#!/usr/bin/env python3
"""Cut the part of the API contract this repository depends on out of the backend's openapi.json.

The contract (zod schemas and the generated openapi.json) lives in the private backend repository. This tool
writes tests/fixtures/contract-v1.json: the /v1 routes, the error codes and their HTTP statuses, the device
errors, the launcher exit codes, the entitlement constants, the limits and the guide headers. tests/test_repo.py
checks client/theodore_client/contract.py against that file, and checks the file against openapi.json when the
backend repository is checked out beside this one (or THEODORE_OPENAPI names the file), so a drift shows up.

Usage: python3 tools/snapshot_contract.py <path to openapi.json> [--check]
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests" / "fixtures" / "contract-v1.json"


def snapshot(openapi_path: Path) -> dict:
    raw = openapi_path.read_bytes()
    doc = json.loads(raw)
    x = doc["x-theodore"]
    routes = {}
    for path, methods in sorted(doc["paths"].items()):
        if not path.startswith("/v1/"):
            continue
        for method, op in sorted(methods.items()):
            routes[op["operationId"]] = {"method": method.upper(), "path": path, "auth": op.get("x-theodore-auth"),
                                         "responses": sorted(op.get("responses", {}))}
    guide = doc["paths"]["/v1/guide/{skill}"]["get"]["responses"]["200"].get("headers", {})
    return {
        "contract_version": x["contract_version"],
        "openapi_sha256": hashlib.sha256(raw).hexdigest(),
        "origins": x["origins"],
        "routes": routes,
        "error_codes": {code: info["status"] for code, info in x["error_codes"].items()},
        "device_errors": {"status": x["device_errors"]["status"], "codes": sorted(x["device_errors"]["codes"])},
        "codes": x["codes"],
        "entitlement": {k: v for k, v in x["entitlement"].items() if not isinstance(v, dict)},
        "manifests": {"channel": x["manifests"]["channel"]["schema_id"], "runtime": x["manifests"]["runtime"]["schema_id"]},
        "launcher": x["launcher"],
        "known": x["known"],
        "limits": x["limits"],
        "guide_headers": sorted(h.lower() for h in guide),
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__)
        return 2
    wanted = json.dumps(snapshot(Path(args[0])), indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != wanted:
            print(f"stale: {TARGET.relative_to(ROOT)} differs from {args[0]}")
            return 1
        return 0
    TARGET.write_text(wanted, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
