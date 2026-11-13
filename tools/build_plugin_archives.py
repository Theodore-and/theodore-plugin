#!/usr/bin/env python3
"""Build what the download host serves for the plugin itself, reproducibly.

  theodore-<version>.zip             plugins/theodore at the archive root: the `archive` source of the hosted
                                     marketplace.json (Claude Code)
  theodore-plugin-<version>.tar.gz   the marketplace tree (.claude-plugin/, .agents/, plugins/theodore/): what the
                                     installer unpacks into <home>/plugin and Codex adds as a local marketplace
  latest.manifest.json               the plugin release document to sign: canonical JSON of {schema
                                     "theodore-plugin-latest@1", version, archive, sha256, size} of the tar.gz. The
                                     publish step signs these exact bytes with the manifest key and uploads
                                     plugin/latest.json = {"manifest": <that text>, "signature", "kid"}; the
                                     installer trusts nothing else (an unsigned latest.txt is no longer made or read)
  plugin-build.json                  version, file names, sizes and sha256 (what the release step registers)

Usage: python3 tools/build_plugin_archives.py --out <dir> [--root <marketplace tree>] [--release]

--release refuses a tree whose launcher.json still carries the repository's placeholder manifest key (kid
manifest-unset or an all-zero key): such a plugin would refuse to start for every user. Stamp the keys first
(tools/sync_launchers.py --env ... --keys ...). The release step always passes --release.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EPOCH = (2024, 1, 1, 0, 0, 0)
EXECUTABLE = {"theodore"}


def tree_files(root: Path) -> list[Path]:
    keep = [root / ".claude-plugin" / "marketplace.json", root / ".agents" / "plugins" / "marketplace.json"]
    keep += sorted(p for p in (root / "plugins" / "theodore").rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store"
             and not p.name.startswith("._"))      # never macOS metadata (AppleDouble)
    return keep


def placeholder_keys(root: Path) -> list[str]:
    """The launcher.json files of the tree that carry no real manifest key (the client's own rule)."""
    import sys
    sys.path.insert(0, str(ROOT / "client"))
    from theodore_client.core import placeholder_key
    bad = []
    for path in sorted((root / "plugins" / "theodore" / "skills").glob("*/scripts/launcher.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        real = [k for k in data if k.startswith("manifest_key_") and k.endswith("_kid")
                and not placeholder_key(str(data[k]), str(data.get(k[:-4] + "_x") or ""))]
        if not real:
            bad.append(str(path.relative_to(root)))
    return bad


def build(out: Path, root: Path = ROOT, release: bool = False) -> dict:
    if release and placeholder_keys(root):
        raise SystemExit("not a release: the placeholder manifest key is still in " + ", ".join(placeholder_keys(root)) +
                         "; stamp the keys with tools/sync_launchers.py --env <env> --keys <jwks.json>")
    plugin = root / "plugins" / "theodore"
    version = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    out.mkdir(parents=True, exist_ok=True)
    files = tree_files(root)

    zip_path = out / f"theodore-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            if plugin not in path.parents:
                continue
            info = zipfile.ZipInfo(path.relative_to(plugin).as_posix(), EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.name in EXECUTABLE else 0o644) << 16
            bundle.writestr(info, path.read_bytes())

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in files:
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size, info.mtime, info.mode = len(data), 0, 0o755 if path.name in EXECUTABLE else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    tar_path = out / f"theodore-plugin-{version}.tar.gz"
    with open(tar_path, "wb") as handle, gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0, compresslevel=9) as zipped:
        zipped.write(raw.getvalue())

    def describe(path: Path) -> dict:
        return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}

    record = {"version": version, "zip": describe(zip_path), "tar": describe(tar_path), "files": len(files)}
    latest = {"schema": "theodore-plugin-latest@1", "version": version, "archive": record["tar"]["file"],
              "sha256": record["tar"]["sha256"], "size": record["tar"]["size"]}
    (out / "latest.manifest.json").write_bytes(json.dumps(latest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    (out / "plugin-build.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(Path(args.out), Path(args.root), args.release), indent=2))
