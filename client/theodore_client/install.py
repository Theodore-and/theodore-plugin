"""What the client keeps on disk: the runtime, the engines, the asset packs and the optional renderer."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, account, api, core, credentials
from .contract import (CHANNEL_MANIFEST_SCHEMA, ENGINE_KEEP_MAX, ENGINE_KEEP_PINNED_DAYS, ENGINE_KEEP_USED_DAYS, ENGINE_REFUSALS,
                       PACK_ORDER, RUNTIME_MANIFEST_SCHEMA, RUNTIME_PARTS, UPDATE_CHECK_INTERVAL_S, UPDATE_CHECK_TIMEOUT_S)
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


def fetch_runtime_manifest(runtime_id: str | None = None, timeout: float = 20.0) -> dict:
    """The signed runtime manifest for this platform: of `runtime_id`, or of the current release when None."""
    system, arch = core.platform_arch()
    try:
        envelope = api.call_json("runtime_manifest", query={"platform": system, "arch": arch, "runtime_id": runtime_id}, timeout=timeout)
    except api.ApiError as error:
        raise api.status_from(error, "runtime manifest") from None
    except api.NetworkError as error:
        raise api.setup_failed(error.cause, f"the runtime list could not be fetched ({error.detail})") from None
    manifest = account.verify_signed(envelope, "runtime manifest", RUNTIME_MANIFEST_SCHEMA)
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", str(manifest.get("runtime_id") or "")):
        raise core.internal("the runtime manifest names a runtime id that cannot be a folder name.")
    return manifest


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def _same_runtime_installed(signature: list, skip: Path) -> Path | None:
    """A complete runtime folder holding exactly these four parts (a runtime id that differs only in its client)."""
    root = core.home().runtime
    for other in root.iterdir() if root.exists() else []:
        if other == skip or _is_link(other) or not (other / ".ready").exists():
            continue
        if [core.read_text(other / ".parts" / n) for n in RUNTIME_PARTS] == signature:
            return other
    return None


def install_runtime(job: Job | None = None, timeout: float = 20.0, runtime_id: str | None = None) -> str:
    """Fetch the signed runtime manifest and install every missing part. Returns the runtime id.

    The id is the server's name for a set of parts. Two ids with the same Python, wheels, Node and node_modules
    (they differ only in the client) share one folder on disk: the second is a link to the first, and nothing is
    downloaded twice. A stage-1 Python already on disk is copied, not downloaded again.
    """
    home = core.home()
    home.ensure()
    home.runtime.mkdir(parents=True, exist_ok=True)
    manifest = fetch_runtime_manifest(runtime_id, timeout)
    rid = str(manifest["runtime_id"])
    base = home.runtime / rid
    with core.lock("runtime"):
        if not (base / ".ready").exists():
            parts = {p["part"]: p for p in manifest.get("parts", [])}
            missing = [n for n in RUNTIME_PARTS if n not in parts]
            if missing:
                raise core.internal(f"the runtime manifest lacks parts: {', '.join(missing)}.")
            twin = _same_runtime_installed([parts[n]["sha256"] for n in RUNTIME_PARTS], base)
            running_here = base.exists() and base.resolve() in Path(sys.executable).resolve().parents
            if twin is not None and not running_here:
                core.remove_tree(base)
                core.link_dir(base, twin)
            total = sum(int(parts[n].get("size") or 0) for n in RUNTIME_PARTS) or 1
            done = 0
            for name in RUNTIME_PARTS if not (base / ".ready").exists() else ():
                part = parts[name]
                marker = base / ".parts" / name
                if name == "python" and core.read_text(marker) != part["sha256"]:
                    for donor in home.runtime.iterdir():        # a clean Python from stage 1 under another id
                        if donor != base and not _is_link(donor) and core.read_text(donor / ".parts" / "python") == part["sha256"] \
                                and not (donor / ".parts" / "wheels").exists() and (donor / "python").is_dir():
                            core.remove_tree(base / "python")
                            shutil.copytree(donor / "python", base / "python", symlinks=True)
                            core.write_atomic(marker, part["sha256"] + "\n", 0o644)
                            break
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
            if not (base / ".ready").exists():
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


def engine_dirs() -> dict:
    """engines/dirs.json: version -> folder name, for a version whose build was replaced (the compiled build of a
    version first installed from source). Without an entry the folder is engines/<version>."""
    return core.read_json(core.home().engines / "dirs.json")


def engine_dir(version: str) -> Path:
    return core.home().engines / str(engine_dirs().get(version) or version)


def installed_versions() -> list[str]:
    root = core.home().engines
    found = {core.read_json(c / ".theodore-engine.json").get("version") for c in (root.iterdir() if root.exists() else []) if c.is_dir()}
    return sorted(v for v in found if v and engine_installed(v))


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
    query = {"platform": system, "arch": arch}      # the channel is the account's, decided by the server
    envelope = api.call_json("releases_manifest", bearer=cred["installation_secret"], query=query, timeout=timeout)
    return account.verify_signed(envelope, "release manifest", CHANNEL_MANIFEST_SCHEMA)


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


ENGINE_KINDS = ("engine-native", "engine-source")


def engine_artifact(manifest: dict) -> dict:
    """The one place that decides which engine artifact this computer takes from a channel manifest.

    The manifest is asked for with this platform and architecture, and the server puts exactly one artifact in it:
    the compiled build (`engine-native`) for this platform when it has passed parity, otherwise the source build
    (`engine-source`, one archive for every platform) when source builds are allowed. Both unpack to the same
    layout and are started the same way, as a child process. Anything else is refused.
    """
    entry = manifest.get("engine") or {}
    system, arch = core.platform_arch()
    if entry.get("kind") not in ENGINE_KINDS or not entry.get("version") or not entry.get("sha256"):
        raise Status("UPDATE_REQUIRED", f"the release manifest offers no engine this client can install for {system}-{arch} "
                                        f"(kind {entry.get('kind')!r}).",
                     f"run {quoted_launcher()} update --apply once; if it repeats, tell the user Theodore has no build for this computer yet.",
                     "Theodore has no engine build for this computer in the current release yet.")
    if entry["kind"] == "engine-native" and (manifest.get("platform"), manifest.get("arch")) != (system, arch):
        raise api.setup_failed("HASH_MISMATCH", "the release manifest offers a compiled engine for another platform")
    return entry


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
            got = install_runtime(job, runtime_id=runtime_id)
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
        partial = home.engines / f"{version}@{str(entry['sha256'])[:12]}.partial"
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
                                                              "kind": entry.get("kind"), "installed_at": int(core.now())})
        if target.exists():
            # The same version in another build (source, then compiled): installed beside it under a new name and the
            # pointer moves. The old folder may be in use by a running build; it is collected once unused for a day.
            final = home.engines / f"{version}@{str(entry['sha256'])[:12]}"
            core.remove_tree(final)
            os.rename(partial, final)
            with core.lock("current"):
                dirs = engine_dirs()
                dirs[version] = final.name
                core.write_json(home.engines / "dirs.json", dirs)
        else:
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


SUPERSEDED_KEEP_S = DAY


def collect_garbage() -> list[str]:
    """Keep current, previous, engines used within 14 days or pinned within 30, at most four. A folder replaced by
    another build of its version goes once unused for a day. Returns what was removed."""
    home, state, now = core.home(), engines_state(), core.now()
    keep_always = {v for v in (state.get("version"), state.get("previous"), pins().get("engine")) if v}
    pinned = {}
    for record in (pins().get("workspaces") or {}).values():
        pinned[record.get("version")] = max(pinned.get(record.get("version"), 0), float(record.get("at") or 0))
    candidates, removed = [], []
    for child in home.engines.iterdir() if home.engines.exists() else []:
        if not child.is_dir() or child.name.endswith(".partial") or not (child / ".theodore-engine.json").exists():
            continue
        version = str(core.read_json(child / ".theodore-engine.json").get("version") or child.name)
        used = (child / ".last-used").stat().st_mtime if (child / ".last-used").exists() else core.read_json(child / ".theodore-engine.json").get("installed_at", 0)
        if engine_dir(version) != child:              # superseded by another build of the same version
            if now - float(used) > SUPERSEDED_KEEP_S:
                core.remove_tree(child)
                removed.append(child.name)
            continue
        recent = now - float(used) < ENGINE_KEEP_USED_DAYS * DAY or now - pinned.get(version, 0) < ENGINE_KEEP_PINNED_DAYS * DAY
        candidates.append((version in keep_always, recent, float(used), child))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    for index, (always, recent, _used, child) in enumerate(candidates):
        if always or (recent and index < ENGINE_KEEP_MAX):
            continue
        core.remove_tree(child)
        removed.append(child.name)
    survivors = [c for c in home.engines.iterdir() if c.is_dir() and (c / ".theodore-engine.json").exists()] if home.engines.exists() else []
    referenced = {core.read_json(c / ".theodore-engine.json").get("runtime_id") for c in survivors}
    referenced.add(core.read_text(home.runtime / "current.txt"))
    referenced.discard(None)
    in_use = {(home.runtime / name).resolve() for name in referenced if name}      # a linked id keeps its target alive
    running = Path(sys.executable).resolve()
    children = sorted(home.runtime.iterdir(), key=lambda c: not _is_link(c)) if home.runtime.exists() else []
    for child in children:                                                          # links first, then real folders
        if not child.is_dir() or child.name in referenced:
            continue
        if _is_link(child):
            if IS_WINDOWS:
                os.rmdir(child)
            else:
                child.unlink()
            removed.append(f"runtime {child.name}")
        elif child.resolve() not in in_use and child.resolve() not in running.parents:
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


def _manifest_time(value) -> float:
    try:
        return time.mktime(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (ValueError, OverflowError):
        return 0.0


def save_restrictions(manifest: dict) -> None:
    """Keep what a verified channel manifest forbids (kill switch, plugin minimum, engine minimum, withdrawn
    versions) until a newer verified manifest says otherwise. An older manifest, replayed, changes nothing."""
    stored = update_state().get("restrictions") or {}
    when = _manifest_time(manifest.get("server_time"))
    if stored and when < float(stored.get("server_time") or 0):
        return
    engine = manifest.get("engine") or {}
    save_update_state(restrictions={"server_time": when, "kill": bool(manifest.get("kill")), "kill_message": manifest.get("kill_message"),
                                    "shim_min": manifest.get("shim_min"), "min_version": manifest.get("min_version"),
                                    "revoked": list(engine.get("revoked") or [])})


def plugin_too_old(shim_min) -> Status:
    shim = str(core.config().get("shim_version") or "0")
    return Status("UPDATE_REQUIRED", f"the Theodore plugin {shim} is older than the minimum {shim_min}.",
                  "update the plugin in the assistant (Claude Code: claude plugin update theodore@theodore; Codex: codex plugin marketplace upgrade theodore), start a new session, then run the same command again.",
                  "The Theodore plugin needs an update. I am updating it; a new session may be needed.")


def enforce_restrictions(engine_version: str | None = None) -> None:
    """Refuse what the last verified manifest forbids, on every call: BLOCKED_BY_POLICY for the kill switch,
    UPDATE_REQUIRED for a plugin below its minimum or for running an engine that is withdrawn or below the minimum."""
    rules = update_state().get("restrictions") or {}
    if rules.get("kill"):
        message = rules.get("kill_message") or "no reason given"
        raise Status("BLOCKED_BY_POLICY", f"Theodore has paused this channel: {message}",
                     "do not retry today. Tell the user; status is at https://theodoreand.ai/support.",
                     f"Theodore is paused by its publisher: {rules.get('kill_message') or 'please check theodoreand.ai for news'}.")
    shim_min = rules.get("shim_min")
    if shim_min and core.version_key(str(core.config().get("shim_version") or "0")) < core.version_key(str(shim_min)):
        raise plugin_too_old(shim_min)
    if engine_version and (engine_version in (rules.get("revoked") or []) or
                           (rules.get("min_version") and core.version_key(engine_version) < core.version_key(str(rules["min_version"])))):
        raise Status("UPDATE_REQUIRED", f"engine {engine_version} was withdrawn or is below the channel's minimum ({rules.get('min_version') or 'withdrawn'}).",
                     f"run {quoted_launcher()} update --apply, then run the same command again.",
                     "Theodore needs an update before it can continue. I am installing it.")


def check_updates(*, force: bool = False, wait: float = 15.0, timeout: float = UPDATE_CHECK_TIMEOUT_S) -> dict | None:
    """The update check that `guide` runs: at most every six hours (a failed attempt counts), short timeout,
    silent on network failure.

    Raises only what must stop the assistant: BLOCKED_BY_POLICY (kill switch) and UPDATE_REQUIRED (a revoked or
    below-minimum engine, or a plugin too old) when the fix could not be applied. Those restrictions are kept from
    the last verified manifest and enforced between checks too.
    """
    state = update_state()
    pending = state.get("pending") or {}
    if pending.get("version") and not Job("engine-update").alive():
        # a download that outlasted an earlier guide: it applies now, at this guide, never in the middle of a build
        if core.read_json(engine_dir(pending["version"]) / ".theodore-engine.json").get("sha256") == pending.get("sha256"):
            activate_engine(pending["version"])
        save_update_state(pending=None)
    last = max(float(state.get("last_check") or 0), float(state.get("last_attempt") or 0))
    if not force and core.now() - last < UPDATE_CHECK_INTERVAL_S:
        enforce_restrictions(current_engine())
        return None
    save_update_state(last_attempt=int(core.now()))
    try:
        manifest = fetch_channel_manifest(timeout=timeout)
    except api.NetworkError:
        enforce_restrictions(current_engine())
        return None
    except api.ApiError as error:
        if error.code in ("update_required", "installation_revoked", "unauthenticated", "account_suspended", "entitlement_inactive"):
            raise api.status_from(error, "update check") from None
        enforce_restrictions(current_engine())
        return None
    save_update_state(last_check=int(core.now()), latest=manifest.get("engine", {}).get("version"),
                      guides=manifest.get("guides") or {}, tools=manifest.get("tools") or {})
    save_restrictions(manifest)
    enforce_restrictions()          # kill switch and plugin minimum; the engine's own case is handled below
    engine = engine_artifact(manifest)
    client = manifest.get("client") or {}
    if client.get("version") and client.get("sha256") and client["version"] != __version__:
        if not Job("client-update").alive():
            Job("client-update").spawn(["update", "--client-job", client["version"], client["sha256"], str(engine.get("runtime_id") or "")])
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
    have_exact = engine_installed(target) and core.read_json(engine_dir(target) / ".theodore-engine.json").get("sha256") == engine.get("sha256")
    if target and (target != current or not have_exact) and not skip and runtime_ready():
        if have_exact:
            activate_engine(target)
        else:
            job = Job("engine-update")
            if not job.alive():
                core.write_json(core.home().state / "engine-update.json", engine)
                job.spawn(["update", "--engine-job"])
            save_update_state(pending={"version": target, "sha256": engine.get("sha256")})
            if job.wait(wait if current and not must else max(wait, 90.0)) and engine_installed(target):
                activate_engine(target)
                save_update_state(pending=None)
        if held and held != target:
            save_update_state(held=None)
    if must and current_engine() == current:
        raise Status("UPDATE_REQUIRED", f"engine {current} was withdrawn and the replacement {target or ''} is not installed yet.",
                     f"run {quoted_launcher()} update --apply, then run the same command again.",
                     "Theodore needs an update before it can continue. I am installing it.")
    enforce_restrictions(current_engine())
    collect_garbage()
    return manifest


def update_client(version: str, sha256: str, runtime_id: str | None = None) -> None:
    """Install the client the channel manifest names: the `client` part of the engine's runtime. Verify, unpack
    beside the running one, switch. It takes effect the next time the launcher starts the client."""
    home = core.home()
    target = home.client / version
    if not (target / "__main__.py").exists():
        url = f"{core.config()['dl_base'].rstrip('/')}/client/client-{version}.tar"
        try:
            for part in fetch_runtime_manifest(runtime_id).get("parts", []):
                if part.get("part") == "client" and part.get("version") == version and part.get("sha256") == sha256:
                    url = part["url"]
        except Status:
            pass
        archive = api.download(url, home.staging / f"client-{version}.archive", sha256=sha256)
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


def selected_engine() -> str | None:
    """The engine a call without a workspace uses: the global pin (update --pin) when installed, else current."""
    forced = pins().get("engine")
    if forced and engine_installed(forced):
        return forced
    return current_engine()


def _restore_engine(workspace: Path, record: dict) -> str:
    """A workspace pinned to an engine that is no longer on disk gets that engine back (same version, same sha256,
    from the release route), or is refused. It is never run on another engine without being asked."""
    version, sha = str(record.get("version") or ""), str(record.get("sha256") or "")
    reason = "its pin does not name the build's checksum"
    if sha:
        try:
            return stage_engine({"version": version, "sha256": sha, "kind": record.get("kind"), "runtime_id": record.get("runtime_id")})
        except EngineError as exc:
            reason = str(exc)
        except Status as status:
            reason = status.summary
    other = selected_engine() or "the current version"
    raise Status("UPDATE_REQUIRED", f"this workspace was started with engine {version}, which is no longer on this computer and could not be "
                                    f"fetched again ({reason}).",
                 f"ask the user. To continue this deck with engine {other} (it may lay the deck out differently), delete "
                 f"{workspace / '.pptx-state' / 'theodore-engine.json'} and run the same command again.",
                 f"This deck was started with Theodore {version}, which is no longer installed and could not be downloaded again. "
                 f"I can continue it with version {other}, which may change its layout. Shall I?")


def engine_for(args: list[str]) -> str:
    """Workspace pin when an argument is a directory holding .pptx-state/, otherwise the global pin or current."""
    for workspace in _workspaces(args):
        record = core.read_json(workspace / ".pptx-state" / "theodore-engine.json")
        pinned = record.get("version")
        if pinned:
            return pinned if engine_installed(pinned) else _restore_engine(workspace, record)
    current = selected_engine()
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
    build = core.read_json(engine_dir(version) / ".theodore-engine.json")
    for workspace in _workspaces(args):
        pin_file = workspace / ".pptx-state" / "theodore-engine.json"
        if not pin_file.exists():
            try:       # the build's checksum too, so a collected engine can be fetched back exactly
                core.write_json(pin_file, {"version": version, "pinned_at": int(core.now()), "sha256": build.get("sha256"),
                                           "kind": build.get("kind"), "runtime_id": build.get("runtime_id")})
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
    enforce_restrictions(version)
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
    if code == 1:
        refusal = _engine_refusal(directory)
        if refusal is not None:
            raise refusal
    return code


def _engine_refusal(directory: Path) -> Status | None:
    """After an exit code 1: ask the engine's own licence check why (it answers one JSON object, offline) and turn
    a refusal into a status the assistant can act on. Returns None when the engine is not refusing."""
    script = directory / "skills" / "pptx" / "scripts" / "pptx.py"
    try:
        probe = subprocess.run([str(engine_python(directory)), str(script), "--help"], env=engine_env(directory),
                               capture_output=True, text=True, timeout=60)
        answer = json.loads(probe.stdout.strip().splitlines()[-1]) if probe.returncode == 1 and probe.stdout.strip() else {}
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
        return None
    code = answer.get("code") if isinstance(answer, dict) else None
    name = ENGINE_REFUSALS.get(code)
    if not name:
        return None
    launcher = quoted_launcher()
    if code == "entitlement_missing":
        return account.login_start()
    if code == "entitlement_expired":
        try:
            account.refresh()
        except api.NetworkError as error:
            return Status("OFFLINE", f"the licence on this computer has expired and the Theodore service could not be reached ({error.detail}).",
                          "ask the user to connect to the internet, then run the same command again.",
                          "Theodore has been offline for more than 7 days and needs a connection to confirm your licence.")
        except api.ApiError as error:
            return api.status_from(error, "licence check")
        return Status("RETRY", "the licence on this computer had expired and was renewed just now.", "run the same command again now.",
                      "Theodore renewed its licence; I am continuing.")
    if code in ("entitlement_not_yet_valid", "entitlement_clock_rollback"):
        return Status("NO_ENTITLEMENT", f"the engine refused the licence because this computer's clock looks wrong ({code}).",
                      "ask the user to check the date and time of this computer, then run the same command again.",
                      "Theodore could not confirm your licence because this computer's date or time looks wrong. Please check it.")
    if code == "entitlement_state_unavailable":
        return Status("NO_ENTITLEMENT", f"the engine could not use Theodore's state folder {core.home().state} ({code}).",
                      f"run {launcher} logout, then {launcher} login --start, and follow what it prints (signing in again recreates the folder). "
                      f"If it repeats, make {core.home().state} writable.",
                      "Theodore needs to sign in again on this computer to repair its licence files. I am starting it.")
    return Status("NO_ENTITLEMENT", f"the engine refused the licence on this computer ({code}).",
                  f"run {launcher} logout, then {launcher} login --start, and follow what it prints. If it repeats, run {launcher} update --apply.",
                  "Theodore's licence on this computer needs to be renewed. I am signing in again.")


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


PACK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PACK_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")


def checked_packs(entries) -> list[dict]:
    """The pack entries of a verified channel manifest, each one checked before it names a file or a folder: an id
    and a version that are one plain path segment, and a sha256. Anything else stops the whole list."""
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict) or not PACK_ID.match(str(entry.get("id") or "")) \
                or not PACK_VERSION.match(str(entry.get("version") or "")) \
                or not re.match(r"^[0-9a-f]{64}$", str(entry.get("sha256") or "").lower()):
            raise api.setup_failed("HASH_MISMATCH", f"the release manifest names an asset pack this client refuses ({str(entry)[:80]})")
        out.append(entry)
    order = {name: i for i, name in enumerate(PACK_ORDER)}
    return sorted(out, key=lambda p: order.get(p["id"], len(order)))


def fetch_packs_manifest(timeout: float = 20.0) -> list[dict]:
    """The packs of the signed channel manifest. The unsigned GET /v1/packs/manifest is not used: a pack's hash and
    address come only from a document the manifest key signed."""
    return checked_packs(fetch_channel_manifest(timeout=timeout).get("packs"))


def _pack_folder(pack_id: str) -> Path:
    """packs/<id>, resolved, and refused unless it is a direct child of the packs folder inside the install folder."""
    packs = core.home().packs
    packs.mkdir(parents=True, exist_ok=True)
    root = packs.resolve()
    target = (packs / pack_id).resolve()
    if not PACK_ID.match(pack_id) or target.parent != root or core.home().root.resolve() not in root.parents:
        raise api.setup_failed("HASH_MISMATCH", f"the asset pack {pack_id!r} would install outside {packs}")
    return target


def install_pack(entry: dict) -> None:
    home = core.home()
    entry = checked_packs([entry])[0]
    pack_id, version = str(entry["id"]), str(entry["version"])
    folder = _pack_folder(pack_id)
    state = packs_state().get(pack_id, {})
    if state.get("status") == "ready" and state.get("version") == version and folder.is_dir():
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
        api.download(api.url_for("pack", id=pack_id, version=version), archive, sha256=entry["sha256"],
                     bearer=cred["installation_secret"], size=int(entry.get("size") or entry.get("size_bytes") or 0) or None, progress=progress)
        partial = folder.with_name(f"{pack_id}.partial")
        core.remove_tree(partial)
        if zipfile.is_zipfile(archive):
            extract_zip(archive, partial)
        else:
            extract_tar(archive, partial)
        _hoist(partial, "pack.json")
        core.remove_tree(folder)
        os.rename(partial, folder)
        core.remove_tree(archive)
    except Exception as exc:
        core.remove_tree(archive)
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


# Which asset packs a build reaches for. The engine's documented helpers are named exactly; any other call whose
# name carries the pack's word counts too, so a helper added later is still caught. Waiting once too often costs
# seconds; a build that fails on a missing asset costs the user's trust, so every doubt resolves to "wait".
PACK_HELPERS = {
    "icons": ("safeIcon", "iconToBase64Png", "getIconMeta", "resolveIconId", "getCatalog"),
    "flags": ("flagToBase64Png", "getFlagMeta", "getFlagCatalog"),
    "maps": ("addMap",),
    "backgrounds": ("backgroundToBase64", "getBackgroundMeta", "listBackgrounds"),
}
_PACK_LOOSE = {"icons": r"\w*[iI]con\w*\(", "flags": r"\w*[fF]lag\w*\(", "maps": r"\b[a-z]\w*Map\w*\(", "backgrounds": r"\w*[bB]ackground\w*\("}


def packs_in_script(text: str) -> set[str]:
    """The packs a deck script calls into, judged from the helper names in its text."""
    found = set()
    for pack, names in PACK_HELPERS.items():
        if re.search(r"\b(?:" + "|".join(names) + r")\s*\(", text) or re.search(_PACK_LOOSE[pack], text):
            found.add(pack)
    return found


def packs_needed(args: list[str]) -> set[str] | None:
    """The packs this engine call needs, or None when it is no build at all.

    A build is a call that names an existing workspace. Its scripts are read for helper names: every script at the
    top of the workspace, and every deck.js up to three folders down (a deck built in sections keeps one per
    section). When no deck.js can be read the client cannot tell, and answers with every pack.
    """
    needed: set[str] = set()
    if args and args[0] in PACK_HELPERS:        # a pack named as the verb, for example a catalogue search
        needed.add(args[0])
    spaces = _workspaces(args)
    if not spaces:
        return needed or None
    for workspace in spaces:
        try:
            scripts = {p for p in workspace.iterdir() if p.is_file() and p.suffix in (".js", ".mjs", ".cjs")}
            for pattern in ("deck.js", "*/deck.js", "*/*/deck.js", "*/*/*/deck.js"):
                scripts |= {p for p in workspace.glob(pattern)
                            if p.is_file() and not any(part.startswith(".") or part == "node_modules" for part in p.relative_to(workspace).parts)}
            if not any(p.name == "deck.js" for p in scripts):
                return set(PACK_ORDER)
            for script in scripts:
                needed |= packs_in_script(script.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            return set(PACK_ORDER)
    return needed


def wait_for_packs(args: list[str], wait: float = 25.0) -> None:
    """If this build reaches for a pack that is still arriving, wait for it; past `wait` seconds say so (67)."""
    wanted = packs_needed(args)
    if not wanted:
        return
    needed = [p for p in PACK_ORDER if p in wanted and not pack_ready(p)]
    if not needed:
        return
    if core.is_loopback_api() and os.environ.get("THEODORE_TEST_PACK_WAIT"):
        wait = float(os.environ["THEODORE_TEST_PACK_WAIT"])
    start_packs_job()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        needed = [p for p in needed if not pack_ready(p)]
        if not needed or not Job("packs").alive():
            break
        time.sleep(0.5)
    needed = [p for p in needed if not pack_ready(p)]
    if needed:
        pack = needed[0]
        percent = int(packs_state().get(pack, {}).get("percent") or 0)
        noun = {"icons": "icon", "flags": "flag", "maps": "map", "backgrounds": "background"}.get(pack, pack)
        raise Status("SETUP_IN_PROGRESS", f"the {noun} pack is still downloading ({percent}%).",
                     "run the same command again in 20 seconds.",
                     f"Theodore is still downloading its {noun} library ({percent}%). I will continue as soon as it is here.")


# --- LibreOffice (optional renderer) ---------------------------------------------------------------------------------------

# LibreOffice, unmodified, as The Document Foundation publishes it. It is tried first from Theodore's download
# host (<dl_base>/tools/<file name>, a mirror with the MPL notice) and then from The Document Foundation; one
# sha256 covers both. A release manifest may carry newer pins under tools.libreoffice.<platform>-<arch>.
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
    name = Path(urlparse(pin["url"]).path).name
    archive = home.staging / name
    sources = [f"{core.config()['dl_base'].rstrip('/')}/tools/{name}", pin["url"]]
    failure: Status | None = None
    for source in dict.fromkeys(sources):
        try:
            api.download(source, archive, sha256=pin["sha256"], size=int(pin.get("size") or 0) or None,
                         progress=(lambda pct: job.progress("downloading LibreOffice", pct)) if job else None, timeout=120)
            failure = None
            break
        except api.NetworkError as error:
            failure = api.setup_failed(error.cause, f"LibreOffice could not be downloaded ({error.detail})")
        except api.ApiError as error:
            failure = api.setup_failed("NETWORK", f"LibreOffice could not be downloaded (HTTP {error.http_status})")
        except Status as status:      # a checksum mismatch at one source: the other may still be right
            failure = status
    if failure is not None:
        raise failure
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
