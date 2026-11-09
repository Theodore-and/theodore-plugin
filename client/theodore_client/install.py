"""What the client keeps on disk: the runtime, the engines, the asset packs and the optional renderer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, account, api, core, credentials
from .contract import (ENGINE_KEEP_MAX, ENGINE_KEEP_PINNED_DAYS, ENGINE_KEEP_USED_DAYS, PACK_ORDER, RUNTIME_PARTS,
                       UPDATE_CHECK_INTERVAL_S, UPDATE_CHECK_TIMEOUT_S)
from .core import IS_WINDOWS, Job, Status, quoted_launcher

DAY = 86400


# --- archives ------------------------------------------------------------------------------------------------------------

def _safe_target(dest: Path, name: str) -> Path:
    target = (dest / name).resolve()
    if os.path.commonpath([str(dest.resolve()), str(target)]) != str(dest.resolve()):
        raise api.setup_failed("HASH_MISMATCH", f"an archive entry points outside its folder ({name})")
    return target


def extract_tar(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(dest, filter="data")
            else:  # older Python: the same checks by hand
                for member in tar.getmembers():
                    _safe_target(dest, member.name)
                    if member.issym() or member.islnk():
                        _safe_target(dest, os.path.join(os.path.dirname(member.name), member.linkname))
                tar.extractall(dest)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise api.setup_failed("DISK", f"could not unpack {archive.name}: {exc}") from None


def extract_zip(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                target = _safe_target(dest, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                mode = (info.external_attr >> 16) & 0o777
                if mode and not IS_WINDOWS:
                    os.chmod(target, mode | 0o600)
    except (zipfile.BadZipFile, OSError) as exc:
        raise api.setup_failed("DISK", f"could not unpack {archive.name}: {exc}") from None


def _hoist(root: Path, marker: str) -> None:
    """If the archive wrapped everything in one top-level folder, lift its content up so `marker` sits at root."""
    if (root / marker).exists():
        return
    children = [c for c in root.iterdir() if not c.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir() and (children[0] / marker).exists():
        inner = children[0]
        for item in list(inner.iterdir()):
            os.rename(item, root / item.name)
        inner.rmdir()


# --- runtime -------------------------------------------------------------------------------------------------------------

def runtime_ready(runtime_id: str | None = None) -> bool:
    base = core.home().runtime_dir(runtime_id)
    return bool(base) and (base / ".ready").exists()


def _finalize_runtime(base: Path, manifest: dict) -> None:
    bin_dir = base / "bin"
    bin_dir.mkdir(exist_ok=True)
    if IS_WINDOWS:
        source, link = base / "node" / "node.exe", bin_dir / "node.exe"
        if source.exists() and not link.exists():
            try:
                os.link(source, link)
            except OSError:
                shutil.copy2(source, link)
    else:
        link = bin_dir / "node"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink("../node/bin/node", link)
    core.write_json(base / "MANIFEST.json", manifest)
    core.write_json(base / ".ready", {"runtime_id": manifest["runtime_id"], "at": int(time.time())})


def install_runtime(job: Job | None = None, timeout: float = 20.0) -> str:
    """Fetch the signed runtime manifest and install every missing part. Returns the runtime id."""
    home = core.home()
    home.ensure()
    system, arch = core.platform_arch()
    try:
        envelope = api.call_json("runtime_manifest", query={"platform": system, "arch": arch}, timeout=timeout)
    except api.ApiError as error:
        raise api.status_from(error, "runtime manifest") from None
    except api.NetworkError as error:
        raise api.setup_failed(error.cause, f"the runtime list could not be fetched ({error.detail})") from None
    manifest = account.verify_signed(envelope, "runtime manifest")
    rid = str(manifest["runtime_id"])
    base = home.runtime / rid
    with core.lock("runtime"):
        if not (base / ".ready").exists():
            parts = {p["part"]: p for p in manifest.get("parts", [])}
            missing = [n for n in RUNTIME_PARTS if n not in parts]
            if missing:
                raise core.internal(f"the runtime manifest lacks parts: {', '.join(missing)}.")
            total = sum(int(parts[n].get("size") or 0) for n in RUNTIME_PARTS) or 1
            done = 0
            for name in RUNTIME_PARTS:
                part = parts[name]
                marker = base / ".parts" / name
                if core.read_text(marker) != part["sha256"]:
                    if job:
                        job.progress(f"downloading {name}", done * 100.0 / total)
                    archive = home.staging / (Path(urlparse(part["url"]).path).name or f"runtime-{rid}-{name}.tar")
                    size = int(part.get("size") or 0)
                    try:
                        api.download(part["url"], archive, sha256=part["sha256"], size=size or None,
                                     progress=(lambda pct, d=done, s=size: job.progress(f"downloading {name}", (d + s * pct / 100.0) * 100.0 / total)) if job else None)
                    except api.NetworkError as error:
                        raise api.setup_failed(error.cause, f"the runtime part {name} could not be downloaded ({error.detail})") from None
                    except api.ApiError as error:
                        raise api.setup_failed("NETWORK", f"the runtime part {name} could not be downloaded (HTTP {error.http_status})") from None
                    if job:
                        job.progress(f"unpacking {name}")
                    extract_tar(archive, base)
                    core.write_atomic(marker, part["sha256"] + "\n", 0o644)
                    core.remove_tree(archive)
                done += int(part.get("size") or 0)
            _finalize_runtime(base, manifest)
        if core.read_text(home.runtime / "current.txt") != rid:
            core.write_atomic(home.runtime / "current.txt", rid, 0o644)  # no newline: cmd reads it with set /p
    return rid


def setup_status() -> Status:
    """SETUP_IN_PROGRESS (or SETUP_FAILED) describing the background setup job; starts the job if none is running."""
    job = Job("setup")
    state = job.read()
    failed = state.get("failed")
    if failed and not job.alive():
        job.clear()
        if failed.get("cause") in ("NETWORK", "TLS_INTERCEPT", "PROXY_AUTH", "DISK", "HASH_MISMATCH"):
            return api.setup_failed(failed["cause"], failed.get("detail", "setup failed"))
        return core.internal(f"setup failed: {failed.get('detail', 'unknown error')}")
    if not job.alive():
        job.spawn(["setup", "--job"])
        state = job.read()
    percent = int(state.get("percent") or 0)
    return Status("SETUP_IN_PROGRESS",
                  f"Theodore is still setting up its runtime on this computer ({state.get('phase', 'starting')}, {percent}%).",
                  "run the same command again in 20 seconds. Do not use another slide tool meanwhile.",
                  "Theodore is finishing its first-time setup. This takes a few minutes; I will continue when it is ready.")


def require_runtime(wait: float = 0.0) -> str:
    """The id of a complete runtime, or raise SETUP_IN_PROGRESS / SETUP_FAILED."""
    rid = core.read_text(core.home().runtime / "current.txt")
    if rid and runtime_ready(rid):
        return rid
    status = setup_status()
    if status.name == "SETUP_IN_PROGRESS" and wait > 0:
        Job("setup").wait(wait)
        rid = core.read_text(core.home().runtime / "current.txt")
        if rid and runtime_ready(rid):
            return rid
        status = setup_status()
    raise status


# --- engines -------------------------------------------------------------------------------------------------------------

def engines_state() -> dict:
    return core.read_json(core.home().engines / "current.json")


def update_state() -> dict:
    return core.read_json(core.home().state / "update.json")


def save_update_state(**fields) -> None:
    state = update_state()
    state.update(fields)
    core.write_json(core.home().state / "update.json", state)


def pins() -> dict:
    return core.read_json(core.home().state / "pins.json")


def engine_dir(version: str) -> Path:
    return core.home().engines / version


def engine_installed(version: str) -> bool:
    return bool(version) and (engine_dir(version) / ".theodore-engine.json").exists()


def current_engine() -> str | None:
    version = engines_state().get("version")
    return version if version and engine_installed(version) else None


def engine_python(directory: Path) -> Path:
    return directory / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def engine_env(directory: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = str(directory / ".runtime" / "bin") + os.pathsep + env.get("PATH", "")
    env["THEODORE_HOME"] = str(core.home().root)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if not system_libreoffice():
        local = libreoffice_binary()
        if local:
            env["SOFFICE_BIN"] = str(local)
    return env


def fetch_channel_manifest(timeout: float = 20.0) -> dict:
    cred = credentials.load()
    if not cred:
        raise api.sign_in_required()
    system, arch = core.platform_arch()
    query = {"platform": system, "arch": arch, "channel": pins().get("channel")}
    envelope = api.call_json("releases_manifest", bearer=cred["installation_secret"], query=query, timeout=timeout)
    return account.verify_signed(envelope, "release manifest")


def _prepare_engine(directory: Path, runtime_id: str) -> None:
    home = core.home()
    runtime = home.runtime / runtime_id
    python = home.runtime_python(runtime_id)
    done = subprocess.run([str(python), "-I", "-m", "venv", "--system-site-packages", "--without-pip", str(directory / ".venv")],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise EngineError(f"could not create the engine's Python environment: {(done.stderr or done.stdout).strip()[:200]}")
    core.link_dir(directory / "node_modules", runtime / "node_modules")
    core.link_dir(directory / ".runtime" / "bin", runtime / "bin")
    home.packs.mkdir(parents=True, exist_ok=True)
    core.link_dir(directory / "skills" / "pptx-asset-packs", home.packs, relative="../../../packs")


class EngineError(Exception):
    pass


def _self_test(directory: Path, runtime_id: str) -> None:
    script = directory / "skills" / "pptx" / "scripts" / "pptx.py"
    if not script.is_file():
        raise EngineError("the engine archive has no skills/pptx/scripts/pptx.py")
    try:
        done = subprocess.run([str(engine_python(directory)), str(script), "--help"], env=engine_env(directory),
                              capture_output=True, text=True, timeout=180, cwd=str(directory))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineError(f"the engine did not start: {exc}") from None
    if done.returncode != 0:
        raise EngineError(f"the engine's self-test exited {done.returncode}: {(done.stderr or done.stdout).strip()[-200:]}")
    package = core.read_json(directory / "package.json")
    for name in (package.get("dependencies") or {}):
        if not (directory / "node_modules" / name / "package.json").is_file():
            raise EngineError(f"the runtime {runtime_id} lacks the Node module {name}")
    node = core.home().runtime / runtime_id / "bin" / ("node.exe" if IS_WINDOWS else "node")
    try:
        if subprocess.run([str(node), "--version"], capture_output=True, timeout=60).returncode != 0:
            raise EngineError("the runtime's Node did not start")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineError(f"the runtime's Node did not start: {exc}") from None


def stage_engine(entry: dict, job: Job | None = None) -> str:
    """Download, verify, unpack and self-test the engine named by a manifest entry. Does not make it current."""
    version = str(entry["version"])
    home = core.home()
    with core.lock("update"):
        target = engine_dir(version)
        if core.read_json(target / ".theodore-engine.json").get("sha256") == entry["sha256"]:
            return version
        runtime_id = str(entry.get("runtime_id") or core.read_text(home.runtime / "current.txt"))
        if not runtime_ready(runtime_id):
            got = install_runtime(job)
            if got != runtime_id:
                raise Status("RETRY", f"engine {version} needs runtime {runtime_id}, which is not published for this platform yet.",
                             "run the same command again in 10 minutes.", "A Theodore update is still being published. I will keep using the current version.")
        cred = credentials.load()
        if not cred:
            raise api.sign_in_required()
        system, arch = core.platform_arch()
        archive = home.staging / f"engine-{version}.zip"
        if job:
            job.progress(f"downloading engine {version}", 0)
        try:
            api.download(api.url_for("release_engine", {"platform": system, "arch": arch}, version=version), archive,
                         sha256=entry["sha256"], bearer=cred["installation_secret"], size=int(entry.get("size") or 0) or None,
                         progress=(lambda pct: job.progress(f"downloading engine {version}", pct)) if job else None)
        except api.ApiError as error:
            raise api.status_from(error, f"engine {version} download") from None
        except api.NetworkError as error:
            raise api.offline(error, f"engine {version} could not be downloaded") from None
        partial = home.engines / f"{version}.partial"
        core.remove_tree(partial)
        try:
            extract_zip(archive, partial)
            _hoist(partial, "package.json")
            _prepare_engine(partial, runtime_id)
            _self_test(partial, runtime_id)
        except EngineError as exc:
            core.remove_tree(partial)
            core.remove_tree(archive)
            save_update_state(failed={"version": version, "reason": str(exc), "at": int(core.now())})
            core.log(f"engine {version} rejected: {exc}")
            raise
        core.write_json(partial / ".theodore-engine.json", {"version": version, "sha256": entry["sha256"], "runtime_id": runtime_id,
                                                              "installed_at": int(core.now())})
        core.remove_tree(target)
        os.rename(partial, target)
        core.remove_tree(archive)
    return version


def activate_engine(version: str) -> None:
    with core.lock("current"):
        state = engines_state()
        old = state.get("version")
        if old == version:
            return
        core.write_json(core.home().engines / "current.json", {"version": version, "previous": old if old and engine_installed(old) else state.get("previous")})
    collect_garbage()


def touch_used(version: str) -> None:
    marker = engine_dir(version) / ".last-used"
    try:
        if not marker.exists() or time.time() - marker.stat().st_mtime > 3600:
            marker.write_text(str(int(time.time())))
    except OSError:
        pass


def collect_garbage() -> list[str]:
    """Keep current, previous, engines used within 14 days or pinned within 30, at most four. Returns what was removed."""
    home, state, now = core.home(), engines_state(), core.now()
    keep_always = {v for v in (state.get("version"), state.get("previous"), pins().get("engine")) if v}
    pinned = {}
    for record in (pins().get("workspaces") or {}).values():
        pinned[record.get("version")] = max(pinned.get(record.get("version"), 0), float(record.get("at") or 0))
    candidates = []
    for child in home.engines.iterdir() if home.engines.exists() else []:
        if not child.is_dir() or child.name.endswith(".partial") or not (child / ".theodore-engine.json").exists():
            continue
        used = (child / ".last-used").stat().st_mtime if (child / ".last-used").exists() else core.read_json(child / ".theodore-engine.json").get("installed_at", 0)
        recent = now - float(used) < ENGINE_KEEP_USED_DAYS * DAY or now - pinned.get(child.name, 0) < ENGINE_KEEP_PINNED_DAYS * DAY
        candidates.append((child.name in keep_always, recent, float(used), child))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    removed = []
    for index, (always, recent, _used, child) in enumerate(candidates):
        if always or (recent and index < ENGINE_KEEP_MAX):
            continue
        core.remove_tree(child)
        removed.append(child.name)
    referenced = {core.read_json(c[3] / ".theodore-engine.json").get("runtime_id") for c in candidates if c[3].exists()}
    referenced.add(core.read_text(home.runtime / "current.txt"))
    running = Path(sys.executable).resolve()
    for child in home.runtime.iterdir() if home.runtime.exists() else []:
        if child.is_dir() and child.name not in referenced and child.resolve() not in running.parents:
            core.remove_tree(child)
            removed.append(f"runtime {child.name}")
    return removed


def rollback() -> Status:
    with core.lock("current"):
        state = engines_state()
        current, previous = state.get("version"), state.get("previous")
        if not previous or not engine_installed(previous):
            return core.usage("there is no previous engine version to go back to.", "nothing to do; keep using the current version.")
        core.write_json(core.home().engines / "current.json", {"version": previous, "previous": current})
    save_update_state(held=current)
    return Status("OK", f"engine {previous} is current again; {current} is held back until a newer version is published.",
                  f"run {quoted_launcher()} guide pptx again before continuing, because the instructions belong to the engine version.",
                  f"Theodore went back to version {previous}.")


def check_updates(*, force: bool = False, wait: float = 15.0, timeout: float = UPDATE_CHECK_TIMEOUT_S) -> dict | None:
    """The update check that `guide` runs: at most every six hours, short timeout, silent on network failure.

    Raises only what must stop the assistant: BLOCKED_BY_POLICY (kill switch) and UPDATE_REQUIRED (a revoked or
    below-minimum engine, or a plugin too old) when the fix could not be applied.
    """
    state = update_state()
    if not force and core.now() - float(state.get("last_check") or 0) < UPDATE_CHECK_INTERVAL_S:
        return None
    try:
        manifest = fetch_channel_manifest(timeout=timeout)
    except api.NetworkError:
        return None
    except api.ApiError as error:
        if error.code in ("update_required", "installation_revoked", "unauthenticated", "account_suspended", "entitlement_inactive"):
            raise api.status_from(error, "update check") from None
        return None
    save_update_state(last_check=int(core.now()), latest=manifest.get("engine", {}).get("version"),
                      guides=manifest.get("guides") or {}, tools=manifest.get("tools") or {})
    if manifest.get("kill"):
        raise Status("BLOCKED_BY_POLICY", f"Theodore has paused this channel: {manifest.get('kill_message') or 'no reason given'}",
                     "do not retry today. Tell the user; status is at https://theodoreand.ai/support.",
                     f"Theodore is paused by its publisher: {manifest.get('kill_message') or 'please check theodoreand.ai for news'}.")
    shim_min = manifest.get("shim_min")
    shim = str(core.config().get("shim_version") or "0")
    if shim_min and core.version_key(shim) < core.version_key(str(shim_min)):
        raise Status("UPDATE_REQUIRED", f"the Theodore plugin {shim} is older than the minimum {shim_min}.",
                     "update the plugin in the assistant (Claude Code: claude plugin update theodore@theodore; Codex: codex plugin marketplace upgrade theodore), start a new session, then run the same command again.",
                     "The Theodore plugin needs an update. I am updating it; a new session may be needed.")
    engine = manifest.get("engine") or {}
    client = manifest.get("client") or {}
    if client.get("version") and client.get("sha256") and client["version"] != __version__:
        if not Job("client-update").alive():
            Job("client-update").spawn(["update", "--client-job", client["version"], client["sha256"]])
        if engine.get("min_client") and core.version_key(__version__) < core.version_key(str(engine["min_client"])):
            Job("client-update").wait(60)
            raise Status("RETRY", "the Theodore client was updated and must restart.", "run the same command again now.",
                         "Theodore updated itself; I am continuing.")
    start_packs_job(manifest.get("packs") or [])
    current = current_engine()
    target = engine.get("version")
    must = bool(current) and (current in (engine.get("revoked") or []) or
                              (manifest.get("min_version") and core.version_key(current) < core.version_key(str(manifest["min_version"]))))
    pinned = pins().get("engine")
    held = state.get("held")
    failed = state.get("failed") or {}
    skip = (pinned and not must) or (held == target and not must and not force) or \
           (failed.get("version") == target and core.now() - float(failed.get("at") or 0) < UPDATE_CHECK_INTERVAL_S and not force)
    if target and target != current and not skip and runtime_ready():
        if engine_installed(target) and core.read_json(engine_dir(target) / ".theodore-engine.json").get("sha256") == engine.get("sha256"):
            activate_engine(target)
        else:
            job = Job("engine-update")
            if not job.alive():
                core.write_json(core.home().state / "engine-update.json", engine)
                job.spawn(["update", "--engine-job"])
            if job.wait(wait if current and not must else max(wait, 90.0)) and engine_installed(target):
                activate_engine(target)
        if held and held != target:
            save_update_state(held=None)
    if must and current_engine() == current:
        raise Status("UPDATE_REQUIRED", f"engine {current} was withdrawn and the replacement {target or ''} is not installed yet.",
                     f"run {quoted_launcher()} update --apply, then run the same command again.",
                     "Theodore needs an update before it can continue. I am installing it.")
    return manifest


def update_client(version: str, sha256: str) -> None:
    """Fetch client-<version>.tar.gz from the download host, verify, unpack beside the running one, switch."""
    home = core.home()
    target = home.client / version
    if not (target / "__main__.py").exists():
        url = f"{core.config()['dl_base'].rstrip('/')}/client/client-{version}.tar"
        archive = api.download(url, home.staging / f"client-{version}.tar", sha256=sha256)
        partial = home.client / f"{version}.partial"
        core.remove_tree(partial)
        extract_tar(archive, partial)
        _hoist(partial, "__main__.py")
        core.remove_tree(target)
        os.rename(partial, target)
        core.remove_tree(archive)
    core.write_atomic(home.client / "current.txt", version, 0o644)  # no newline: cmd reads it with set /p
    for child in home.client.iterdir():
        if child.is_dir() and child.name not in (version, __version__):
            core.remove_tree(child)


# --- running the engine ----------------------------------------------------------------------------------------------------

def _workspaces(args: list[str]) -> list[Path]:
    found = []
    for arg in args:
        if arg.startswith("-"):
            continue
        try:
            path = Path(arg)
            if path.is_dir() and (path / ".pptx-state").is_dir():
                found.append(path.resolve())
        except OSError:
            continue
    return found


def engine_for(args: list[str]) -> str:
    """Workspace pin when an argument is a directory holding .pptx-state/, otherwise the global pin or current."""
    for workspace in _workspaces(args):
        pinned = core.read_json(workspace / ".pptx-state" / "theodore-engine.json").get("version")
        if pinned and engine_installed(pinned):
            return pinned
    forced = pins().get("engine")
    if forced and engine_installed(forced):
        return forced
    current = current_engine()
    if not current:
        raise Status("SETUP_IN_PROGRESS", "the engine is not installed yet.",
                     f"run {quoted_launcher()} guide pptx first; it installs the engine and prints the instructions.",
                     "Theodore's engine is not installed yet. I am setting it up.")
    return current


def _pin_workspaces(args: list[str], version: str) -> None:
    import hashlib
    record = pins()
    spaces = record.setdefault("workspaces", {})
    changed = False
    for workspace in _workspaces(args):
        pin_file = workspace / ".pptx-state" / "theodore-engine.json"
        if not pin_file.exists():
            try:
                core.write_json(pin_file, {"version": version, "pinned_at": int(core.now())})
            except OSError:
                continue
        pinned = core.read_json(pin_file).get("version") or version
        key = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]  # a hash: no folder name is stored
        if spaces.get(key, {}).get("version") != pinned or core.now() - float(spaces[key].get("at") or 0) > DAY:
            spaces[key] = {"version": pinned, "at": int(core.now())}
            changed = True
    if changed:
        core.write_json(core.home().state / "pins.json", record)


def run_engine(args: list[str], *, python_passthrough: bool = False) -> int:
    version = engine_for(args)
    directory = engine_dir(version)
    touch_used(version)
    python = engine_python(directory)
    command = [str(python), *args] if python_passthrough else [str(python), str(directory / "skills" / "pptx" / "scripts" / "pptx.py"), *args]
    try:
        code = subprocess.call(command, env=engine_env(directory))
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        blocked = isinstance(exc, PermissionError)
        raise Status("BLOCKED_BY_POLICY" if blocked else "INTERNAL", f"the engine could not be started: {exc}",
                     "do not retry. Tell the user; a security policy on this computer may block programs in the user folder." if blocked
                     else f"run {quoted_launcher()} doctor.",
                     "This computer's security policy blocked Theodore's engine. Your IT team can allow it, or you can use Theodore through its connector." if blocked
                     else "Theodore's engine could not start. I am checking why.") from None
    if not python_passthrough:
        _pin_workspaces(args, version)
    return code


# --- asset packs -----------------------------------------------------------------------------------------------------------

def packs_state() -> dict:
    return core.read_json(core.home().packs / "state.json")


def _save_pack(pack_id: str, **fields) -> None:
    with core.lock("packs-state", timeout=30):
        state = packs_state()
        entry = state.setdefault(pack_id, {})
        entry.update(fields)
        entry["updated"] = int(time.time())
        core.write_json(core.home().packs / "state.json", state)


def pack_ready(pack_id: str) -> bool:
    return packs_state().get(pack_id, {}).get("status") == "ready" and (core.home().packs / pack_id).is_dir()


def fetch_packs_manifest(timeout: float = 20.0) -> list[dict]:
    cred = credentials.load()
    if not cred:
        raise api.sign_in_required()
    packs = api.call_json("packs_manifest", bearer=cred["installation_secret"], timeout=timeout).get("packs") or []
    order = {name: i for i, name in enumerate(PACK_ORDER)}
    return sorted(packs, key=lambda p: order.get(p.get("id"), len(order)))


def install_pack(entry: dict) -> None:
    home = core.home()
    pack_id, version = str(entry["id"]), str(entry["version"])
    state = packs_state().get(pack_id, {})
    if state.get("status") == "ready" and state.get("version") == version and (home.packs / pack_id).is_dir():
        return
    cred = credentials.load()
    if not cred:
        raise api.sign_in_required()
    _save_pack(pack_id, status="downloading", percent=0, target=version, pid=os.getpid())
    archive = home.staging / f"pack-{pack_id}-{version}.zip"
    last = [0.0]

    def progress(pct: float) -> None:
        if pct - last[0] >= 2 or pct >= 100:
            last[0] = pct
            _save_pack(pack_id, percent=int(pct))

    try:
        api.download(entry.get("url") or api.url_for("pack", id=pack_id, version=version), archive, sha256=entry["sha256"],
                     bearer=cred["installation_secret"], size=int(entry.get("size_bytes") or entry.get("size") or 0) or None, progress=progress)
        partial = home.packs / f"{pack_id}.partial"
        core.remove_tree(partial)
        if zipfile.is_zipfile(archive):
            extract_zip(archive, partial)
        else:
            extract_tar(archive, partial)
        _hoist(partial, "pack.json")
        core.remove_tree(home.packs / pack_id)
        os.rename(partial, home.packs / pack_id)
        core.remove_tree(archive)
    except Exception as exc:
        _save_pack(pack_id, status="failed", error=str(getattr(exc, "summary", exc))[:200])
        raise
    _save_pack(pack_id, status="ready", version=version, sha256=entry["sha256"], percent=100, error=None)


def start_packs_job(manifest_packs: list[dict] | None = None) -> bool:
    """Start the background pack fetch when a pack is missing or out of date. True when a job is (now) running."""
    job = Job("packs")
    if job.alive():
        return True
    if manifest_packs is not None:
        state = packs_state()
        if all(state.get(p["id"], {}).get("status") == "ready" and state[p["id"]].get("version") == p["version"] for p in manifest_packs):
            return False
    if not credentials.load():
        return False
    job.spawn(["packs", "install", "--all", "--job"])
    return True


# What a deck script must mention for a pack to count as "needed by this build" (a heuristic, on purpose loose).
_PACK_HINTS = {"icons": r"[iI]con\(", "flags": r"[fF]lag\(", "maps": r"[A-Za-z]Map\(", "backgrounds": r"[A-Za-z]Background\(|backgrounds/"}


def wait_for_packs(args: list[str], wait: float = 25.0) -> None:
    """If this build reaches for a pack that is still arriving, wait for it; past `wait` seconds say so (67)."""
    import re
    needed = set()
    if args and args[0] in _PACK_HINTS:
        needed.add(args[0])
    for workspace in _workspaces(args):
        script = workspace / "deck.js"
        try:
            text = script.read_text(encoding="utf-8", errors="ignore") if script.is_file() else ""
        except OSError:
            text = ""
        needed.update(pack for pack, pattern in _PACK_HINTS.items() if re.search(pattern, text))
    needed = {p for p in needed if not pack_ready(p)}
    if not needed:
        return
    if core.is_loopback_api() and os.environ.get("THEODORE_TEST_PACK_WAIT"):
        wait = float(os.environ["THEODORE_TEST_PACK_WAIT"])
    start_packs_job()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        needed = {p for p in needed if not pack_ready(p)}
        if not needed or not Job("packs").alive():
            break
        time.sleep(0.5)
    needed = sorted(p for p in needed if not pack_ready(p))
    if needed:
        pack = needed[0]
        percent = int(packs_state().get(pack, {}).get("percent") or 0)
        noun = {"icons": "icon", "flags": "flag", "maps": "map", "backgrounds": "background"}.get(pack, pack)
        raise Status("SETUP_IN_PROGRESS", f"the {noun} pack is still downloading ({percent}%).",
                     "run the same command again in 20 seconds.",
                     f"Theodore is still downloading its {noun} library ({percent}%). I will continue as soon as it is here.")


# --- LibreOffice (optional renderer) ---------------------------------------------------------------------------------------

# Fetched unmodified from The Document Foundation. The signed release manifest may carry newer pins under
# tools.libreoffice.<platform>-<arch>; these are the built-in defaults.
LIBREOFFICE_PINS = {
    "darwin-arm64": {"version": "26.2.7", "size": 297813809,
                     "url": "https://download.documentfoundation.org/libreoffice/stable/26.2.7/mac/aarch64/LibreOffice_26.2.7_MacOS_aarch64.dmg",
                     "sha256": "58601808ae2b919c82dc7c4d6a26389de0e167f344ca48e5b6c9189218eb95ae"},
    "darwin-x64": {"version": "26.2.7", "size": 308070986,
                   "url": "https://download.documentfoundation.org/libreoffice/stable/26.2.7/mac/x86_64/LibreOffice_26.2.7_MacOS_x86-64.dmg",
                   "sha256": "26a830205c617cc624b2a83783ef46434a4d6c337c3a873f79c43012171f83de"},
    "win32-x64": {"version": "26.2.7", "size": 373121024,
                  "url": "https://download.documentfoundation.org/libreoffice/stable/26.2.7/win/x86_64/LibreOffice_26.2.7_Win_x86-64.msi",
                  "sha256": "480fecf759fc6393bd614fbf43753eb95a8198fc1a065d41b727e3b664cd1e59"},
}


def libreoffice_pin() -> dict | None:
    key = "-".join(core.platform_arch())
    override = (update_state().get("tools") or {}).get("libreoffice", {}).get(key)
    return override or LIBREOFFICE_PINS.get(key)


def libreoffice_binary() -> Path | None:
    base = core.home().tools / "libreoffice"
    for candidate in (base / "LibreOffice.app" / "Contents" / "MacOS" / "soffice", base / "program" / "soffice.exe",
                      base / "LibreOffice" / "program" / "soffice.exe"):
        if candidate.exists():
            return candidate
    return None


def system_libreoffice() -> str | None:
    if os.environ.get("SOFFICE_BIN") and Path(os.environ["SOFFICE_BIN"]).exists():
        return os.environ["SOFFICE_BIN"]
    found = shutil.which("soffice")
    if found:
        return found
    for candidate in ("/Applications/LibreOffice.app/Contents/MacOS/soffice", r"C:\Program Files\LibreOffice\program\soffice.exe"):
        if Path(candidate).exists():
            return candidate
    return None


def install_libreoffice(job: Job | None = None) -> Path:
    pin = libreoffice_pin()
    if not pin:
        raise Status("UNSUPPORTED_PLATFORM", "Theodore does not fetch LibreOffice on this platform.",
                     "ask the user to install LibreOffice with their package manager; renders work once `soffice` is on PATH.",
                     "To preview slides, please install LibreOffice with your system's package manager.")
    home = core.home()
    existing = libreoffice_binary()
    if existing and core.read_text(home.tools / "libreoffice" / ".version") == pin["version"]:
        return existing
    archive = home.staging / Path(pin["url"]).name
    try:
        api.download(pin["url"], archive, sha256=pin["sha256"], size=int(pin.get("size") or 0) or None,
                     progress=(lambda pct: job.progress("downloading LibreOffice", pct)) if job else None, timeout=120)
    except api.NetworkError as error:
        raise api.setup_failed(error.cause, f"LibreOffice could not be downloaded ({error.detail})") from None
    except api.ApiError as error:
        raise api.setup_failed("NETWORK", f"LibreOffice could not be downloaded (HTTP {error.http_status})") from None
    target = home.tools / "libreoffice"
    partial = home.tools / "libreoffice.partial"
    core.remove_tree(partial)
    partial.mkdir(parents=True)
    if job:
        job.progress("unpacking LibreOffice")
    try:
        if sys.platform == "darwin":
            mount = home.staging / "libreoffice-mount"
            core.remove_tree(mount)
            mount.mkdir(parents=True)
            subprocess.run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount), str(archive)],
                           check=True, capture_output=True)
            try:
                apps = [a for a in mount.iterdir() if a.suffix == ".app"]
                if not apps:
                    raise OSError("the disk image holds no application")
                subprocess.run(["/bin/cp", "-R", str(apps[0]), str(partial / "LibreOffice.app")], check=True, capture_output=True)
            finally:
                subprocess.run(["/usr/bin/hdiutil", "detach", str(mount), "-force"], capture_output=True)
                core.remove_tree(mount)
        elif IS_WINDOWS:  # an administrative install only copies files: no elevation, nothing registered
            subprocess.run(["msiexec", "/a", str(archive), "/qn", f"TARGETDIR={partial}"], check=True, capture_output=True)
        else:
            raise OSError("no unpack method for this platform")
    except (OSError, subprocess.CalledProcessError) as exc:
        core.remove_tree(partial)
        raise api.setup_failed("DISK", f"LibreOffice could not be unpacked: {exc}") from None
    core.write_atomic(partial / ".version", pin["version"] + "\n", 0o644)
    core.remove_tree(target)
    os.rename(partial, target)
    core.remove_tree(archive)
    binary = libreoffice_binary()
    if not binary:
        raise api.setup_failed("DISK", "LibreOffice was unpacked but its program was not found")
    return binary
