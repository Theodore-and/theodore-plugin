#!/usr/bin/env python3
"""Pack the client into client-<version>.tar, byte for byte reproducible on every platform.

The archive holds <version>/__main__.py, <version>/theodore_client/ and <version>/truststore/ (vendored, MIT).
It is a plain uncompressed ustar file (about 300 KB): compressed bytes depend on the zlib build, plain tar bytes
do not, and the launcher unpacks it with the system `tar` during stage 1. Its sha256 is pinned in
plugins/theodore/skills/pptx/scripts/launcher.json (tools/sync_launchers.py writes the pin; a test checks it).

Usage: python3 runtime/build/build_client.py --out <dir>      prints the path of the archive
"""

from __future__ import annotations

import argparse
import io
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "client"


def client_version() -> str:
    text = (CLIENT / "theodore_client" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def client_files() -> list[Path]:
    files = [CLIENT / "__main__.py"]
    for folder in ("theodore_client", "truststore"):
        # Never macOS metadata (AppleDouble ._*, .DS_Store) that a copy or an archive tool left beside the sources.
        found = [p for p in (CLIENT / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts
                 and not p.name.startswith("._") and p.name != ".DS_Store"]
        # By name parts, case-sensitive: sorting Path objects ignores case on Windows and gives other bytes there.
        files += sorted(found, key=lambda p: p.relative_to(CLIENT).parts)
    return files


def build(out_dir: Path) -> Path:
    version = client_version()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in client_files():
            data = path.read_bytes().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(f"{version}/{path.relative_to(CLIENT).as_posix()}")
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"client-{version}.tar"
    target.write_bytes(raw.getvalue())
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    print(build(Path(parser.parse_args().out)))
