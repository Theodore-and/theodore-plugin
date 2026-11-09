#!/usr/bin/env python3
"""Smoke-test a runtime bundle built by build_bundle.py.

    python3 runtime/build/smoke_test.py --bundle <out dir> --platform darwin-arm64 [--engine <payload dir>]
                                        [--extra-path <dir>]... [--keep]

Standard library only. The bundle is verified against its manifest, unpacked into a fresh directory whose path
contains a space, exercised, then moved to a second path and exercised again. One line per check:
PASS|FAIL|SKIP  <check>: <detail>. The exit code is non-zero when any check failed.

--engine takes an unpacked engine payload (the tree that holds skills/pptx/scripts/pptx.py). The engine is private:
this option is for local runs only and is never used in CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

LOCKS = Path(__file__).resolve().parent.parent / "locks"
WINDOWS = os.name == "nt"
PY_MODULES = ["pptx", "lxml.etree", "PIL.Image", "numpy", "fontTools", "requests", "defusedxml", "xlsxwriter",
              "pypdfium2", "truststore"]
STDLIB_MODULES = ["ssl", "lzma", "sqlite3", "ctypes", "venv"]
# Optional npm dependencies whose native binary comes from an install script. The recipe runs no install scripts,
# so these do not load; the engine treats them as absent.
NO_BINARY = {"skia-canvas"}
# Packages whose published `main` does not load (mathjax-full's needs a component build that npm does not ship);
# the entry point its users require instead.
ENTRY = {"mathjax-full": "mathjax-full/js/mathjax.js"}

UNPACK = "import sys, tarfile\nwith tarfile.open(sys.argv[1]) as t:\n    t.extractall(sys.argv[2], filter='data')\n"

CHECK_PY = r"""
import importlib, json, sys
out = {"prefix": sys.prefix, "base_prefix": sys.base_prefix, "version": sys.version.split()[0], "modules": {}}
for name in sys.argv[1].split(","):
    try:
        module = importlib.import_module(name)
        top = sys.modules[name.split(".")[0]]
        version = getattr(module, "__version__", None) or getattr(top, "__version__", None)
        if version is None:
            try:
                from importlib.metadata import version as dist_version
                version = dist_version({"pptx": "python-pptx", "PIL": "pillow", "fontTools": "fonttools"}.get(
                    name.split(".")[0], name.split(".")[0]))
            except Exception:
                version = "stdlib"
        if name == "ssl":
            module.create_default_context()
            version = module.OPENSSL_VERSION
        elif name == "sqlite3":
            module.connect(":memory:").execute("select 1").fetchone()
            version = "SQLite " + module.sqlite_version
        elif name == "lzma":
            assert module.decompress(module.compress(b"theodore")) == b"theodore"
        elif name == "ctypes":
            assert module.sizeof(module.c_void_p) in (4, 8)
        elif name == "truststore":
            import ssl
            module.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        out["modules"][name] = {"version": str(version), "file": getattr(module, "__file__", "") or ""}
    except BaseException as exc:
        out["modules"][name] = {"error": f"{type(exc).__name__}: {exc}"}
print(json.dumps(out))
"""

CHECK_JS = r"""
const fs = require("fs"), path = require("path");
const out = {node: process.version, modules: {}};
const entry = JSON.parse(process.argv[2]);
for (const name of JSON.parse(process.argv[1])) {
  try {
    require(entry[name] || name);
    const manifest = JSON.parse(fs.readFileSync(path.join(process.cwd(), "node_modules", name, "package.json")));
    out.modules[name] = {version: manifest.version, file: require.resolve(entry[name] || name)};
  } catch (error) {
    out.modules[name] = {error: String(error && error.message).split("\n")[0]};
  }
}
(async () => {
  try {
    const sharp = require("sharp");
    out.vips = sharp.versions.vips;
    const png = await sharp({create: {width: 1, height: 1, channels: 4, background: {r: 255, g: 0, b: 0, alpha: 1}}})
      .png().toBuffer();
    out.png = {bytes: png.length, signature: png.subarray(0, 8).toString("hex")};
  } catch (error) {
    out.sharpError = String(error && error.message).split("\n")[0];
  }
  console.log(JSON.stringify(out));
})();
"""

RESULTS: list[str] = []


def report(status: str, check: str, detail: str = "") -> bool:
    RESULTS.append(status)
    print(f"{status}  {check}: {detail}", flush=True)
    return status == "PASS"


def expect(condition: bool, check: str, detail: str = "") -> bool:
    return report("PASS" if condition else "FAIL", check, detail)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def timed(cmd: list, **kwargs) -> tuple[subprocess.CompletedProcess, float]:
    start = time.monotonic()
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          **kwargs)
    return proc, time.monotonic() - start


def last_line(proc: subprocess.CompletedProcess) -> str:
    lines = [line for line in (proc.stderr + "\n" + proc.stdout).splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def command_check(proc: subprocess.CompletedProcess, seconds: float, check: str) -> bool:
    ok = expect(proc.returncode == 0, check, f"exit {proc.returncode} in {seconds:.1f} s")
    if not ok:
        for line in (proc.stdout + "\n" + proc.stderr).splitlines()[-25:]:
            print(f"      {line[:300]}")
    return ok


def inside(path: str, root: Path) -> bool:
    return bool(path) and os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


def runtime_python(runtime: Path) -> Path:
    return runtime / "python" / ("python.exe" if WINDOWS else "bin/python3")


def runtime_node(runtime: Path) -> Path:
    return runtime / "node" / ("node.exe" if WINDOWS else "bin/node")


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if WINDOWS else "bin/python")


def system_path() -> list[str]:
    if WINDOWS:
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return [os.path.join(root, "System32"), root]
    return ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def clean_env(scratch: Path, path_dirs: list) -> dict[str, str]:
    """A minimal environment: no developer PATH, home and caches inside the scratch directory."""
    home, tmp = scratch / "user home", scratch / "tmp"
    for directory in (home, tmp):
        directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        keep = ("SystemRoot", "SystemDrive", "ComSpec", "PATHEXT", "windir", "NUMBER_OF_PROCESSORS",
                "PROCESSOR_ARCHITECTURE", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles")
        env = {key: os.environ[key] for key in keep if key in os.environ}
        env.update(USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
                   LOCALAPPDATA=str(home / "AppData" / "Local"), TEMP=str(tmp), TMP=str(tmp))
    else:
        env = {"HOME": str(home), "XDG_CACHE_HOME": str(scratch / "cache"), "TMPDIR": str(tmp)}
    env["PATH"] = os.pathsep.join([str(p) for p in path_dirs] + system_path())
    return env


def link_directory(link: Path, target: Path) -> None:
    """A symlink; on Windows a directory junction, which needs no administrator rights."""
    if WINDOWS:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


# ---------------------------------------------------------------------------------------------------------------

def verify_parts(bundle: Path, manifest: dict) -> bool:
    ok = True
    for part in manifest["parts"]:
        path = bundle / part["file"]
        good = path.is_file() and path.stat().st_size == part["size"] and sha256_file(path) == part["sha256"]
        ok &= expect(good, f"sha256 {part['part']}", f"{part['file']} {part['sha256'][:16]} {part['size']} bytes")
    return ok


def unpack(bundle: Path, manifest: dict, runtime: Path) -> bool:
    """Stage 1 the way the launcher does it (plain tar, before any Python exists), the rest with the Python that
    stage 1 delivered."""
    runtime.mkdir(parents=True)
    total = 0.0
    for part in manifest["parts"]:
        archive = bundle / part["file"]
        if part["part"] == "client":
            report("SKIP", "unpack client", "not part of the runtime directory")
            continue
        if part["part"] == "python":
            tar = os.path.join(system_path()[0], "tar.exe") if WINDOWS else "tar"
            proc, seconds = timed([tar, "-xzf", archive, "-C", runtime])
            how = "plain tar"
        else:
            proc, seconds = timed([runtime_python(runtime), "-I", "-B", "-c", UNPACK, archive, runtime])
            how = "bundled Python, tarfile data filter"
        total += seconds
        if not expect(proc.returncode == 0, f"unpack {part['part']}",
                      f"{seconds:.1f} s ({how})" if proc.returncode == 0 else last_line(proc)):
            return False  # the later parts need the Python that the first one delivers
    return report("PASS", "unpack all parts", f"{total:.1f} s into {runtime}")


def check_python(python: Path, runtime: Path, env: dict, label: str, version: str, in_venv: bool) -> None:
    names = PY_MODULES + STDLIB_MODULES
    # The base interpreter runs isolated (-I); the venv runs the way the engine runs it.
    proc, _ = timed([python, *([] if in_venv else ["-I"]), "-c", CHECK_PY, ",".join(names)], env=env)
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        report("FAIL", f"{label} start", last_line(proc))
        return
    expect(data["version"] == version and inside(data["base_prefix"], runtime)
           and (data["prefix"] != data["base_prefix"]) == in_venv,
           f"{label} interpreter", f"Python {data['version']} prefix {data['prefix']} base {data['base_prefix']}")
    for name in names:
        info = data["modules"][name]
        if "error" in info:
            report("FAIL", f"{label} import {name}", info["error"])
        else:
            expect(inside(info["file"], runtime), f"{label} import {name}", f"{info['version']} ({info['file']})")


def check_node(runtime: Path, env: dict, label: str, version: str) -> None:
    package = json.loads((LOCKS / "package.json").read_text(encoding="utf-8"))
    required = sorted(package.get("dependencies", {}))
    optional = sorted(package.get("optionalDependencies", {}))
    proc, _ = timed([runtime_node(runtime), "-e", CHECK_JS, json.dumps(required + optional), json.dumps(ENTRY)],
                    cwd=runtime, env=env)
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        report("FAIL", f"{label} node start", last_line(proc))
        return
    expect(data["node"] == f"v{version}", f"{label} node", data["node"])
    for name in required + optional:
        info = data["modules"][name]
        kind = "optional" if name in optional else "required"
        if "error" not in info:
            expect(inside(info["file"], runtime), f"{label} require {name} ({kind})", info["version"])
        elif name in NO_BINARY:
            report("SKIP", f"{label} require {name} (optional, no native binary without its install script)",
                   info["error"])
        else:
            report("FAIL", f"{label} require {name} ({kind})", info["error"])
    expect("vips" in data, f"{label} sharp libvips", data.get("vips") or data.get("sharpError", ""))
    png = data.get("png") or {}
    expect(png.get("signature") == "89504e470d0a1a0a", f"{label} sharp one-pixel PNG encode",
           f"{png.get('bytes')} bytes" if png else data.get("sharpError", ""))


def check_runtime(runtime: Path, scratch: Path, label: str, manifest: dict) -> None:
    env = clean_env(scratch, [])
    version = manifest["python_version"]
    check_python(runtime_python(runtime), runtime, env, f"{label} python", version, in_venv=False)
    venv = scratch / f"venv {label}" / ".venv"
    proc, seconds = timed([runtime_python(runtime), "-m", "venv", "--system-site-packages", "--without-pip", venv],
                          env=env)
    if expect(proc.returncode == 0, f"{label} venv create",
              f"{seconds:.2f} s" if proc.returncode == 0 else last_line(proc)):
        check_python(venv_python(venv), runtime, env, f"{label} venv", version, in_venv=True)
    check_node(runtime, env, label, manifest["node_version"])


# ---------------------------------------------------------------------------------------------------------------
# The engine directory, laid out the way the client lays it out.

def prepare_engine(runtime: Path, payload: Path, engine: Path) -> Path:
    """Copy the payload, create the venv over the runtime Python, link node_modules and the runtime bin directory.
    Returns the engine's Python."""
    shutil.copytree(payload, engine, symlinks=True)
    subprocess.run([str(runtime_python(runtime)), "-m", "venv", "--system-site-packages", "--without-pip",
                    str(engine / ".venv")], check=True)
    link_directory(engine / "node_modules", runtime / "node_modules")
    bin_dir = runtime / "bin"
    bin_dir.mkdir(exist_ok=True)
    if WINDOWS and not (bin_dir / "node.exe").exists():
        try:
            os.link(runtime_node(runtime), bin_dir / "node.exe")
        except OSError:
            shutil.copy2(runtime_node(runtime), bin_dir / "node.exe")
    elif not WINDOWS and not (bin_dir / "node").is_symlink():
        os.symlink("../node/bin/node", bin_dir / "node")
    (engine / ".runtime").mkdir()
    link_directory(engine / ".runtime" / "bin", bin_dir)
    return venv_python(engine / ".venv")


def check_engine(runtime: Path, payload: Path, home: Path, scratch: Path, label: str, extra_path: list) -> None:
    engine = home / "engines" / "test"
    start = time.monotonic()
    python = prepare_engine(runtime, payload, engine)
    report("PASS", f"{label} engine directory", f"prepared in {time.monotonic() - start:.2f} s at {engine}")
    cli = engine / "skills" / "pptx" / "scripts" / "pptx.py"
    # The engine finds node on PATH. With nothing but the runtime's bin directory and the system directories on
    # PATH, node and the interpreter must both be the bundle's.
    strict = clean_env(scratch, [engine / ".runtime" / "bin"])
    probe = "import os, shutil, sys; print(os.path.realpath(shutil.which('node') or '')); print(sys.base_prefix)"
    proc, _ = timed([python, "-c", probe], env=strict, cwd=scratch)
    lines = proc.stdout.splitlines() + ["", ""]
    expect(lines[0] == os.path.realpath(runtime_node(runtime)) and inside(lines[1], runtime),
           f"{label} engine resolves the bundle's node and Python on a bare PATH", f"node={lines[0]} python={lines[1]}")

    # The engine's daemon is switched off for the smoke, as its own install recorder does.
    env = clean_env(scratch, [engine / ".runtime" / "bin", *extra_path])
    env.update(PPTX_RUNTIME_DAEMON="0", PPTX_RUNTIME_DAEMON_NAMESPACE=f"runtime-smoke-{os.getpid()}-{label}")
    workspace = scratch / f"workspace {label}"
    output = workspace / "output" / "smoke.pptx"

    proc, seconds = timed([python, cli, "--help"], env=env, cwd=scratch)
    command_check(proc, seconds, f"{label} engine --help")

    proc, seconds = timed([python, cli, "doctor", "--json"], env=env, cwd=scratch)
    try:
        results = json.loads(proc.stdout)
    except ValueError:
        results = None
    if not isinstance(results, dict):
        report("FAIL", f"{label} engine doctor", f"exit {proc.returncode}: {last_line(proc)}")
    else:
        package = json.loads((LOCKS / "package.json").read_text(encoding="utf-8"))
        # The engine's doctor requires each package by its bare name, so it reports the ENTRY packages as absent
        # on every install; check_node has already loaded them through their real entry point.
        npm = (set(package.get("dependencies", {})) | set(package.get("optionalDependencies", {}))) - NO_BINARY
        npm -= set(ENTRY)
        bundled = {f"python:{name.split('.')[0]}" for name in PY_MODULES} | {f"npm:{name}" for name in npm}
        bundled |= {"python", "node"}
        absent = sorted(name for name, info in results.items()
                        if isinstance(info, dict) and info.get("status") in ("missing", "broken"))
        ours = [name for name in absent if name.split(" ")[0] in bundled]
        expect(not ours, f"{label} engine doctor",
               f"exit {proc.returncode} in {seconds:.1f} s; bundle modules missing: {ours or 'none'}; "
               f"not provided by the bundle: {absent or 'none'}")

    proc, seconds = timed([python, cli, "start", "--blank", "--output", output, "--objective",
                           "runtime bundle smoke", workspace], env=env, cwd=scratch)
    command_check(proc, seconds, f"{label} engine start --blank")
    proc, seconds = timed([python, cli, "apply", workspace], env=env, cwd=scratch)
    command_check(proc, seconds, f"{label} engine apply")
    valid = False
    if output.is_file() and zipfile.is_zipfile(output):
        with zipfile.ZipFile(output) as deck:
            valid = deck.testzip() is None and "ppt/presentation.xml" in deck.namelist()
    expect(valid, f"{label} engine output", f"{output.name} {output.stat().st_size} bytes, valid zip with "
           "ppt/presentation.xml" if valid else f"{output} missing or not a valid deck")


# ---------------------------------------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a Theodore runtime bundle.")
    parser.add_argument("--bundle", required=True, type=Path, help="directory holding the manifest and the archives")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--engine", type=Path, help="unpacked engine payload for a blank deck build (local only)")
    parser.add_argument("--extra-path", action="append", default=[], type=Path,
                        help="directory added to PATH for the engine run (for example where soffice lives)")
    parser.add_argument("--keep", action="store_true", help="keep the unpacked tree and print where it is")
    args = parser.parse_args()

    bundle = args.bundle.resolve()
    manifests = sorted(bundle.glob(f"runtime-*-{args.platform}.manifest.json"))
    if len(manifests) != 1:
        print(f"FAIL  manifest: expected one runtime-*-{args.platform}.manifest.json in {bundle}, "
              f"found {len(manifests)}")
        return 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    expect(manifest.get("schema") == "theodore-runtime-bundle-v1"
           and f"{manifest['platform']}-{manifest['arch']}" == args.platform,
           "manifest", f"{manifests[0].name} runtime {manifest['runtime_id']}")
    scratch = Path(tempfile.mkdtemp(prefix="theodore smoke ")).resolve()
    try:
        if verify_parts(bundle, manifest):
            for label, home in (("unpacked", scratch / "first home"), ("moved", scratch / "second home")):
                runtime = home / "runtime" / manifest["runtime_id"]
                if label == "unpacked":
                    if not unpack(bundle, manifest, runtime):
                        break
                else:
                    # Relocation: the runtime directory moves to a second path; venvs are created again there.
                    runtime.parent.mkdir(parents=True)
                    shutil.move(scratch / "first home" / "runtime" / manifest["runtime_id"], runtime)
                    report("PASS", "move the runtime directory", str(runtime))
                check_runtime(runtime, scratch, label, manifest)
                if args.engine:
                    check_engine(runtime, args.engine.resolve(), home, scratch, label,
                                 [path.resolve() for path in args.extra_path])
            if not args.engine:
                report("SKIP", "engine blank build", "no --engine payload given")
    finally:
        if args.keep:
            print(f"kept: {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    counts = {status: RESULTS.count(status) for status in ("PASS", "FAIL", "SKIP")}
    print(f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
