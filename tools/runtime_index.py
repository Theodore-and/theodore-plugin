#!/usr/bin/env python3
"""Combine the per-platform runtime bundles into runtime-<id>.json, the index the release step registers.

Usage: python3 tools/runtime_index.py <bundles dir> [--out <dir>] [--platforms darwin-arm64,...]

<bundles dir> holds one folder per platform, each with the bundle's <...>.manifest.json and its part files (what
build_bundle.py writes; ci/run.sh puts them in ci/out/<platform>/bundle). Every part is checked for size and
sha256. The platforms must agree on runtime id, recipe, Python, Node and lock_sha256; the index carries
lock_sha256 at the top and in every platform, because the backend's registration stores it beside each bundle row
and the engine publisher finds runtimes by it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ALL = ("darwin-arm64", "darwin-x64", "win32-x64", "linux-x64", "linux-arm64")


def build_index(bundles: Path, expected: tuple[str, ...] = ALL) -> dict:
    index, common = {}, set()
    for path in sorted(bundles.glob("*/*.manifest.json")) + sorted(bundles.glob("*/*/*.manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for part in manifest["parts"]:
            data = (path.parent / part["file"]).read_bytes()
            if len(data) != part["size"] or hashlib.sha256(data).hexdigest() != part["sha256"]:
                raise SystemExit(f"{path.parent / part['file']}: size or sha256 does not match the manifest")
        key = f"{manifest['platform']}-{manifest['arch']}"
        if key in index:
            raise SystemExit(f"two bundles for {key}")
        index[key] = {"built_at": manifest["built_at"], "lock_sha256": manifest["lock_sha256"], "parts": manifest["parts"]}
        common.add((manifest["schema"], manifest["runtime_id"], manifest["recipe_version"], manifest["python_version"],
                    manifest["node_version"], manifest["lock_sha256"]))
    if set(index) != set(expected):
        raise SystemExit(f"platforms built: {sorted(index)}; expected {sorted(expected)}")
    if len(common) != 1:
        raise SystemExit(f"the platforms disagree: {sorted(common)}")
    _, runtime_id, recipe_version, python_version, node_version, lock = common.pop()
    return {"schema": "theodore-runtime-index-v1", "runtime_id": runtime_id, "lock_sha256": lock, "recipe_version": recipe_version,
            "python_version": python_version, "node_version": node_version, "platforms": dict(sorted(index.items()))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundles")
    parser.add_argument("--out", default=".")
    parser.add_argument("--platforms", default=",".join(ALL))
    args = parser.parse_args()
    combined = build_index(Path(args.bundles), tuple(p for p in args.platforms.split(",") if p))
    out = Path(args.out) / f"runtime-{combined['runtime_id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
