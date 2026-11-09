#!/usr/bin/env python3
"""Build the Theodore runtime bundle for one platform.

    python3 runtime/build/build_bundle.py --platform darwin-arm64 --out <dir> [--work <dir>] [--cache <dir>]

Standard library only (Python 3.11 or newer). The build runs natively on the target platform: the locked wheels
are installed with the downloaded Python and `npm ci` runs with the downloaded Node. Inputs are the five files in
runtime/locks/. Outputs are four archives and a manifest in --out. See README.md in this directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import lzma
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

HERE = Path(__file__).resolve().parent
LOCKS = HERE.parent / "locks"
# The runtime id is the sha256 over the bytes of these files, in this order.
ID_FILES = ("requirements.txt", "package-lock.json", "package.json", "runtime-extra.txt", "versions.json")
SCHEMA = "theodore-runtime-bundle-v1"
# mtime of every archive entry: 2024-01-01T00:00:00Z, the date the upstream Python tarball uses. Not 0, because
# ZIP writers refuse timestamps before 1980 and the engine may copy these files into a deck.
EPOCH = 1704067200
PLATFORMS = ("darwin-arm64", "darwin-x64", "win32-x64", "linux-x64", "linux-arm64")
PBS_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/{release}/{file}"
PBS_RAW_URL = "https://raw.githubusercontent.com/astral-sh/python-build-standalone/{release}/{file}"
NODE_URL = "https://nodejs.org/dist/v{version}/{file}"

LICENCE_NAME = re.compile(r"licen[cs]e|copying|notice|copyright|authors|patents|third.?party", re.I)
LICENCE_TEXT = re.compile(r"licen[cs]|copyright|permission is hereby granted", re.I)
PRUNE_DIRS = {"test", "tests", "__tests__", "docs", "example", "examples", ".github"}

T0 = time.monotonic()


def log(message: str) -> None:
    print(f"[{time.monotonic() - T0:7.1f}s] {message}", flush=True)


def die(message: str) -> NoReturn:
    raise SystemExit(f"build_bundle: {message}")


def run(cmd: list, **kwargs) -> None:
    log("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run([str(c) for c in cmd], **kwargs)
    if proc.returncode != 0:
        die(f"command failed with exit code {proc.returncode}: {cmd[0]}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_id() -> str:
    digest = hashlib.sha256()
    for name in ID_FILES:
        digest.update((LOCKS / name).read_bytes())
    return digest.hexdigest()[:16]


def lock_sha256() -> str:
    """The engine release command's name for "these two lockfiles": it finds the bundles of a release by this value."""
    digest = hashlib.sha256()
    for name in ("requirements.txt", "package-lock.json"):
        digest.update(name.encode("ascii") + b"\0" + (LOCKS / name).read_bytes() + b"\0")
    return digest.hexdigest()


def host_platform() -> str:
    osname = {"darwin": "darwin", "win32": "win32", "linux": "linux"}.get(sys.platform, sys.platform)
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())
    return f"{osname}-{arch}"


def wheel_platform_tags(platform_key: str, floor: dict) -> list[str]:
    """The pip --platform tags for the oldest system the bundle supports, best first.

    Without them pip picks wheels for the build host's own OS version (numpy publishes a macOS 14 wheel beside its
    macOS 11 one), and the bundle would then fail on any older system."""
    osname, arch = platform_key.split("-")
    if osname == "win32":
        return ["win_amd64"]
    if osname == "darwin":
        major, minor = floor["macos"].split(".")
        return [f"macosx_{major}_{minor}_{'arm64' if arch == 'arm64' else 'x86_64'}"]
    machine = "aarch64" if arch == "arm64" else "x86_64"
    legacy = {17: "manylinux2014", 12: "manylinux2010", 5: "manylinux1"}
    tags = []
    for minor in range(int(floor["glibc"].split(".")[1]), 4, -1):
        tags.append(f"manylinux_2_{minor}_{machine}")
        if minor in legacy:
            tags.append(f"{legacy[minor]}_{machine}")
    return tags


def fetch(url: str, dest: Path, sha256: str, size: int | None = None) -> None:
    """Download into the cache and verify. A mismatch is a hard failure."""
    if dest.exists() and sha256_file(dest) == sha256:
        log(f"cached   {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    log(f"download {url}")
    with urllib.request.urlopen(url, timeout=120) as response, open(part, "wb") as handle:
        shutil.copyfileobj(response, handle, 1 << 20)
    got, got_size = sha256_file(part), part.stat().st_size
    if got != sha256 or (size is not None and got_size != size):
        part.unlink()
        die(f"checksum mismatch for {url}: expected sha256 {sha256} size {size}, got sha256 {got} size {got_size}")
    os.replace(part, dest)


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(dest)
        return
    with tarfile.open(archive) as bundle:
        if hasattr(tarfile, "tar_filter"):
            bundle.extractall(dest, filter="tar")
        else:  # Python 3.11.0 to 3.11.3
            bundle.extractall(dest)


def walk_files(root: Path):
    """Every file and symlink under root (symlinks are not followed)."""
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for name in filenames:
            yield here / name
        for name in dirnames:
            if (here / name).is_symlink():
                yield here / name


def tree_entries(root: Path, prefix: str) -> list[tuple[str, Path]]:
    return [(f"{prefix}/{path.relative_to(root).as_posix()}", path) for path in walk_files(root)]


def stats(entries: list[tuple[str, Path]]) -> tuple[int, int]:
    """(unpacked size in bytes, number of files and symlinks)."""
    size = sum(path.lstat().st_size for _, path in entries if not path.is_symlink())
    return size, len(entries)


# ---------------------------------------------------------------------------------------------------------------
# Packing. Runs under the bundle's own Python (`--pack <spec.json>`), so the tar and xz bytes come from one pinned
# interpreter and one pinned liblzma whatever the host Python is.

def pack(spec_path: Path) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    entries = sorted((arc, src) for arc, src in spec["entries"])
    directories = set()
    for arc, _ in entries:
        parts = arc.split("/")[:-1]
        directories.update("/".join(parts[:n]) for n in range(1, len(parts) + 1))
    with lzma.open(spec["out"], "wb", format=lzma.FORMAT_XZ, preset=9 | lzma.PRESET_EXTREME) as stream:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for arc, src in sorted([(d, None) for d in directories] + entries, key=lambda item: item[0]):
                info = tarfile.TarInfo(arc)
                info.mtime, info.uid, info.gid, info.uname, info.gname = spec["mtime"], 0, 0, "", ""
                if src is None:
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    tar.addfile(info)
                elif os.path.islink(src):
                    info.type, info.mode, info.linkname = tarfile.SYMTYPE, 0o777, os.readlink(src)
                    tar.addfile(info)
                else:
                    status = os.stat(src)
                    info.size = status.st_size
                    info.mode = 0o755 if os.name != "nt" and status.st_mode & 0o111 else 0o644
                    with open(src, "rb") as handle:
                        tar.addfile(info, handle)


def start_pack(python: Path, out_file: Path, entries: list[tuple[str, Path]], spec_dir: Path) -> subprocess.Popen:
    spec = spec_dir / (out_file.name + ".spec.json")
    spec.write_text(json.dumps({"out": str(out_file), "mtime": EPOCH,
                                "entries": [[arc, str(src)] for arc, src in entries]}), encoding="utf-8")
    return subprocess.Popen([str(python), "-I", "-B", str(Path(__file__).resolve()), "--pack", str(spec)])


# ---------------------------------------------------------------------------------------------------------------
# Python and the wheels

def site_packages_dir(python: Path) -> Path:
    out = subprocess.run([str(python), "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                         check=True, capture_output=True, text=True).stdout.strip()
    return Path(out)


def snapshot(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): ("link" if path.is_symlink() else sha256_file(path))
            for path in walk_files(root) if "__pycache__" not in path.parts}


def trim_record(dist_info: Path) -> None:
    """Drop RECORD rows for files outside site-packages (console scripts, man pages). Those files are not shipped,
    and the hash of a console script depends on the build directory through its interpreter line."""
    record = dist_info / "RECORD"
    if not record.is_file():
        return
    with open(record, newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.reader(handle) if row and not row[0].startswith("../")]
    with open(record, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)


def build_wheels(python: Path, runtime_root: Path, wheel_dir: Path, tags: list[str], licence_dir: Path
                 ) -> list[tuple[str, Path]]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    pip = [python, "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input",
           "--no-cache-dir"]
    locks = ["--require-hashes", "--only-binary", ":all:", "--no-deps",
             "-r", LOCKS / "requirements.txt", "-r", LOCKS / "runtime-extra.txt"]
    site = site_packages_dir(python)
    python_root = runtime_root / "python"
    before = snapshot(python_root)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    platform_args = [arg for tag in tags for arg in ("--platform", tag)]
    run([*pip, "download", "--dest", wheel_dir, *platform_args, *locks], env=env)
    run([*pip, "install", "--no-compile", "--no-index", "--find-links", wheel_dir, "--no-warn-script-location",
         *locks], env=env)
    # --no-deps installs exactly the two files; `pip check` fails if any declared dependency is absent or at an
    # incompatible version. Do not loosen: add the missing pin to the lock instead.
    run([*pip, "check"], env=env)

    after = snapshot(python_root)
    changed = sorted(rel for rel in before if after.get(rel) != before[rel])
    if changed:
        die(f"pip changed or removed files of the upstream Python: {changed[:10]}")
    site_rel = site.resolve().relative_to(python_root.resolve()).as_posix()
    added = sorted(rel for rel in after if rel not in before)
    outside = [rel for rel in added if not rel.startswith(site_rel + "/")]
    if outside:
        log(f"not shipped ({len(outside)} files pip put outside site-packages): " + ", ".join(outside))
    licences = {}
    for dist_info in sorted(site.glob("*.dist-info")):
        if f"{site_rel}/{dist_info.name}/METADATA" in before:
            continue  # pip itself: part of the upstream tarball
        trim_record(dist_info)
        dist = dist_info.name[: -len(".dist-info")]
        texts = [path for path in walk_files(dist_info)
                 if path.relative_to(dist_info).parts[0] in ("licenses", "license_files")
                 or (path.parent == dist_info and LICENCE_NAME.search(path.name))]
        if not texts:
            die(f"{dist}: no licence file in its dist-info; add the text by hand before shipping this package")
        for path in texts:
            rel = path.relative_to(dist_info).parts
            rel = rel[1:] if len(rel) > 1 else rel
            licences[f"licenses/python-packages/{dist}/{'/'.join(rel)}"] = path
    entries = list(licences.items())
    entries += [(f"python/{rel}", python_root / rel) for rel in added if rel.startswith(site_rel + "/")]
    entries += [(f"licenses/python-build-standalone/{path.name}", path) for path in sorted(licence_dir.iterdir())]
    return entries


def cpython_licence(python_root: Path) -> str:
    for candidate in sorted(python_root.glob("lib/python3.*/LICENSE.txt")) + [python_root / "LICENSE.txt"]:
        if candidate.is_file():
            return f"python/{candidate.relative_to(python_root).as_posix()}"
    die("the upstream Python tarball carries no LICENSE.txt; add the CPython licence to the wheels part")


# ---------------------------------------------------------------------------------------------------------------
# Node and node_modules

def referenced_by_manifest(package_json: Path, directory: str) -> bool:
    """True when the package's own manifest points into the directory (main, bin, exports and the like)."""
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    found = []

    def collect(value):
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for key in ("main", "module", "browser", "bin", "exports", "imports", "types", "typings"):
        collect(data.get(key))
    return any(text.removeprefix("./").startswith(directory + "/") for text in found)


def is_package_root(directory: Path) -> bool:
    parent = directory.parent
    return (directory / "package.json").is_file() and (
        parent.name == "node_modules" or (parent.name.startswith("@") and parent.parent.name == "node_modules"))


def prune_node_modules(node_modules: Path, platform_key: str) -> dict[str, int]:
    removed = {"other-platform sharp packages": 0, "test/docs/example directories": 0, "*.map": 0, "*.md": 0}
    scope = node_modules / "@img"
    if scope.is_dir():
        for package in sorted(scope.iterdir()):
            match = re.fullmatch(r"sharp-(?:libvips-)?(.+)", package.name)
            if match and match.group(1) != platform_key:
                shutil.rmtree(package)
                removed["other-platform sharp packages"] += 1
    for dirpath, dirnames, filenames in os.walk(node_modules):
        here = Path(dirpath)
        if is_package_root(here):
            for name in sorted(PRUNE_DIRS.intersection(dirnames)):
                target = here / name
                if target.is_symlink() or referenced_by_manifest(here / "package.json", name):
                    continue
                if any(LICENCE_NAME.search(path.name) for path in walk_files(target)):
                    continue
                shutil.rmtree(target)
                dirnames.remove(name)
                removed["test/docs/example directories"] += 1
        for name in filenames:
            if LICENCE_NAME.search(name):
                continue
            kind = "*.map" if name.endswith(".map") else "*.md" if name.lower().endswith(".md") else None
            if kind == "*.md" and LICENCE_TEXT.search((here / name).read_text(encoding="utf-8", errors="replace")):
                continue  # a README is the only licence statement of some packages (sharp's libvips package is one)
            if kind:
                (here / name).unlink()
                removed[kind] += 1
    return removed


def npm_licence_list(node_modules: Path, dest: Path) -> None:
    rows = set()
    for dirpath, _, filenames in os.walk(node_modules):
        here = Path(dirpath)
        if "package.json" in filenames and is_package_root(here):
            data = json.loads((here / "package.json").read_text(encoding="utf-8"))
            licence = data.get("license") or data.get("licenses") or "UNKNOWN"
            if isinstance(licence, dict):
                licence = licence.get("type", "UNKNOWN")
            if isinstance(licence, list):
                licence = " OR ".join(item.get("type", "UNKNOWN") if isinstance(item, dict) else str(item)
                                      for item in licence)
            rows.add((str(data.get("name", here.name)), str(data.get("version", "")), str(licence)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# name\tversion\tlicence (from each package.json in node_modules; the licence texts are inside "
             "each package directory)"]
    lines += ["\t".join(row) for row in sorted(rows)]
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def build_node_modules(node: Path, npm_cli: Path, scratch: Path, cache: Path, platform_key: str) -> Path:
    scratch.mkdir(parents=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(LOCKS / name, scratch / name)
    npmrc = {kind: scratch.parent / f"empty-{kind}-npmrc" for kind in ("user", "global")}
    for path in npmrc.values():  # npm refuses one file for both; empty files keep the developer's own config out
        path.write_text("", encoding="utf-8")
    env = {**os.environ,
           "PATH": str(node.parent) + os.pathsep + os.environ.get("PATH", ""),
           "npm_config_cache": str(cache / "npm"),
           "npm_config_userconfig": str(npmrc["user"]),
           "npm_config_globalconfig": str(npmrc["global"]),
           "npm_config_update_notifier": "false"}
    # --ignore-scripts: nothing from a package runs at build time. skia-canvas (optional) therefore has no native
    # binary; see README.md.
    run([node, npm_cli, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=scratch, env=env)
    node_modules = scratch / "node_modules"
    removed = prune_node_modules(node_modules, platform_key)
    log("pruned node_modules: " + ", ".join(f"{count} {kind}" for kind, count in removed.items()))
    return node_modules


# ---------------------------------------------------------------------------------------------------------------

def tar_gz_stats(path: Path) -> tuple[int, int]:
    with tarfile.open(path) as bundle:
        members = [member for member in bundle if not member.isdir()]
    return sum(member.size for member in members if member.isfile()), len(members)


def build(platform_key: str, out: Path, work: Path, cache: Path) -> None:
    versions = json.loads((LOCKS / "versions.json").read_text(encoding="utf-8"))
    rid = runtime_id()
    windows = platform_key.startswith("win32")
    py_pin, node_pin = versions["python"], versions["node"]
    py_asset, node_asset = py_pin["assets"][platform_key], node_pin["assets"][platform_key]
    log(f"runtime id {rid}  platform {platform_key}  Python {py_pin['version']}  Node {node_pin['version']}")

    out.mkdir(parents=True, exist_ok=True)
    build_dir = work / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    runtime_root = build_dir / "runtime"
    runtime_root.mkdir(parents=True)

    py_archive, node_archive = cache / py_asset["file"], cache / node_asset["file"]
    fetch(PBS_URL.format(release=py_pin["pbs_release"], file=urllib.parse.quote(py_asset["file"])),
          py_archive, py_asset["sha256"], py_asset["size"])
    fetch(NODE_URL.format(version=node_pin["version"], file=node_asset["file"]),
          node_archive, node_asset["sha256"], node_asset["size"])
    licence_dir = cache / f"python-build-standalone-{py_pin['pbs_release']}-licences"
    for name, digest in sorted(py_pin["license_texts"].items()):
        fetch(PBS_RAW_URL.format(release=py_pin["pbs_release"], file=name), licence_dir / name, digest)

    # Python: the upstream tarball is shipped byte for byte; this copy only runs pip and the packer.
    extract(py_archive, runtime_root)
    python_root = runtime_root / "python"
    python = python_root / ("python.exe" if windows else "bin/python3")
    python_stats = stats(tree_entries(python_root, "python"))
    log(f"CPython licence inside the python part: {cpython_licence(python_root)}")
    tags = wheel_platform_tags(platform_key, py_pin["wheel_floor"])
    wheel_entries = build_wheels(python, runtime_root, cache / f"wheels-{rid}-{platform_key}", tags, licence_dir)

    # Node: only the binary and its licence are shipped; npm from the same archive runs `npm ci`.
    node_dist = build_dir / "node-dist"
    extract(node_archive, node_dist)
    node_top = next(node_dist.iterdir())
    node = node_top / ("node.exe" if windows else "bin/node")
    npm_cli = node_top / ("node_modules" if windows else "lib/node_modules") / "npm" / "bin" / "npm-cli.js"
    node_entries = [("node/node.exe" if windows else "node/bin/node", node),
                    ("licenses/node/LICENSE", node_top / "LICENSE")]
    node_modules = build_node_modules(node, npm_cli, build_dir / "npm", cache, platform_key)
    npm_list = build_dir / "licenses" / "npm-packages.txt"
    npm_licence_list(node_modules, npm_list)
    module_entries = tree_entries(node_modules, "node_modules") + [("licenses/npm-packages.txt", npm_list)]

    stem = f"runtime-{rid}-{platform_key}"
    shutil.copyfile(py_archive, out / py_asset["file"])
    created = [("wheels", wheel_entries), ("node", node_entries), ("node_modules", module_entries)]
    log("packing three archives (xz -9e)")
    jobs = [start_pack(python, out / f"{stem}-{part}.tar.xz", entries, build_dir) for part, entries in created]
    if any(job.wait() != 0 for job in jobs):
        die("packing failed")

    def record(part: str, path: Path, unpacked: int, count: int) -> dict:
        return {"part": part, "file": path.name, "sha256": sha256_file(path), "size": path.stat().st_size,
                "unpacked_size": unpacked, "file_count": count}

    parts = [record("python", out / py_asset["file"], *python_stats)]
    parts += [record(part, out / f"{stem}-{part}.tar.xz", *stats(entries)) for part, entries in created]
    build_client = HERE / "build_client.py"
    if build_client.exists():
        printed = subprocess.run([sys.executable, str(build_client), "--out", str(out)], check=True,
                                 capture_output=True, text=True).stdout.strip().splitlines()[-1]
        parts.append(record("client", Path(printed), *tar_gz_stats(Path(printed))))
    manifest = {"schema": SCHEMA, "runtime_id": rid, "lock_sha256": lock_sha256(), "platform": platform_key.split("-")[0],
                "arch": platform_key.split("-")[1], "recipe_version": versions["recipe_version"],
                "python_version": py_pin["version"], "node_version": node_pin["version"], "parts": parts,
                "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    manifest_path = out / f"{stem}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    mb = 1024 * 1024
    print(f"\n{'part':<14}{'download bytes':>16}{'MB':>8}{'unpacked bytes':>16}{'MB':>8}{'files':>8}")
    for part in parts:
        print(f"{part['part']:<14}{part['size']:>16}{part['size'] / mb:>8.1f}{part['unpacked_size']:>16}"
              f"{part['unpacked_size'] / mb:>8.1f}{part['file_count']:>8}")
    total = [sum(part[key] for part in parts) for key in ("size", "unpacked_size", "file_count")]
    print(f"{'total':<14}{total[0]:>16}{total[0] / mb:>8.1f}{total[1]:>16}{total[1] / mb:>8.1f}{total[2]:>8}")
    print(f"manifest: {manifest_path}")
    log("done")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--pack":
        pack(Path(sys.argv[2]))
        return 0
    parser = argparse.ArgumentParser(description="Build the Theodore runtime bundle for one platform.")
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--out", required=True, type=Path, help="directory for the archives and the manifest")
    parser.add_argument("--work", type=Path, help="scratch directory (default: a temporary one, removed at the end)")
    parser.add_argument("--cache", type=Path, help="download cache (default: <work>/cache)")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        die("Python 3.11 or newer is required to run the recipe")
    if args.platform != host_platform():
        die(f"--platform {args.platform} does not match this host ({host_platform()}); the build is native only")
    temporary = None if args.work else tempfile.mkdtemp(prefix="theodore-runtime-")
    work = (args.work or Path(temporary)).resolve()
    try:
        build(args.platform, args.out.resolve(), work, (args.cache or work / "cache").resolve())
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
