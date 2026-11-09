"""Statuses, the data directory, small file helpers, locks, background jobs and the clock."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform as _platform
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .contract import EXIT

IS_WINDOWS = os.name == "nt"


# --- statuses ---------------------------------------------------------------------------------------------------------

class Status(Exception):
    """A three-line message for the assistant and the exit code that goes with it."""

    def __init__(self, name: str, summary: str, next_step: str, tell_user: str):
        super().__init__(summary)
        self.name, self.summary, self.next_step, self.tell_user = name, summary, next_step, tell_user

    @property
    def code(self) -> int:
        return EXIT[self.name]

    def render(self) -> str:
        one = lambda text: " ".join(str(text).split())
        return f"THEODORE {self.name}: {one(self.summary)}\nNEXT: {one(self.next_step)}\nTELL THE USER: {one(self.tell_user)}"


def emit(status: Status) -> int:
    print(status.render(), flush=True)
    return status.code


def usage(summary: str, next_step: str = "") -> Status:
    return Status("USAGE", summary, next_step or f"run {quoted_launcher()} help for the commands.",
                  "I called Theodore with a wrong command; I am correcting it.")


def internal(summary: str) -> Status:
    return Status("INTERNAL", summary,
                  f"run {quoted_launcher()} doctor and report its output with: {quoted_launcher()} report --kind bug",
                  "Theodore hit an internal error. I am collecting the details.")


# --- platform ------------------------------------------------------------------------------------------------------------

def platform_arch() -> tuple[str, str]:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(_platform.system(), _platform.system().lower())
    machine = _platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine, machine)
    return system, arch


def host_name() -> str:
    """The assistant that called us, as far as the environment says (sent as `host`)."""
    import re
    explicit = re.sub(r"[^a-z0-9-]", "", (os.environ.get("THEODORE_HOST") or "").lower())[:32].lstrip("-")
    if explicit:
        return explicit
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_MANAGED_BY_NPM"):
        return "codex"
    return "other"


# --- data directory ------------------------------------------------------------------------------------------------------

# The only things Theodore creates, manages or removes inside its data directory. The directory itself may exist
# before Theodore and may hold other folders: those are never read, changed or deleted.
OWNED = ("bin", "client", "runtime", "engines", "packs", "tools", "state", "logs", "staging", "plugin")


class Home:
    def __init__(self, root: Path | None = None):
        if root is None:
            env = os.environ.get("THEODORE_HOME")
            if env:
                root = Path(env)
            elif IS_WINDOWS:
                root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Theodore"
            else:
                root = Path.home() / ".theodore"
        self.root = Path(root).absolute()

    def __getattr__(self, name: str) -> Path:
        table = {
            "bin": "bin", "client": "client", "runtime": "runtime", "engines": "engines", "packs": "packs",
            "tools": "tools", "state": "state", "logs": "logs", "staging": "staging", "plugin": "plugin",
            "jobs": "state/jobs", "locks": "state/lock",
        }
        if name in table:
            return self.root / table[name]
        raise AttributeError(name)

    def ensure(self) -> None:
        for sub in ("state", "state/jobs", "state/lock", "logs", "staging", "engines", "packs"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root / "state", 0o700)

    def launcher(self) -> Path:
        env = os.environ.get("THEODORE_LAUNCHER")
        if env:
            return Path(env)
        return self.bin / ("theodore.cmd" if IS_WINDOWS else "theodore")

    def runtime_dir(self, runtime_id: str | None = None) -> Path | None:
        rid = runtime_id or read_text(self.runtime / "current.txt")
        return self.runtime / rid if rid else None

    def runtime_python(self, runtime_id: str | None = None) -> Path | None:
        base = self.runtime_dir(runtime_id)
        if base is None:
            return None
        return base / "python" / ("python.exe" if IS_WINDOWS else "bin/python3")


_HOME: Home | None = None


def home() -> Home:
    global _HOME
    if _HOME is None:
        _HOME = Home()
    return _HOME


def quoted_launcher() -> str:
    return f'"{home().launcher()}"'


# --- configuration (launcher.json beside the stable launcher) ---------------------------------------------------------

_CONFIG: dict | None = None


def config() -> dict:
    """launcher.json: API and download bases, stage-1 pins, manifest keys. Read from <home>/bin, else beside us."""
    global _CONFIG
    if _CONFIG is None:
        candidates = [home().bin / "launcher.json"]
        if os.environ.get("THEODORE_SKILL_DIR"):
            candidates.append(Path(os.environ["THEODORE_SKILL_DIR"]) / "scripts" / "launcher.json")
        for path in candidates:
            try:
                _CONFIG = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
        else:
            raise internal(f"launcher.json was not found in {home().bin}.")
    return _CONFIG


def manifest_keys() -> dict[str, str]:
    """kid -> base64url Ed25519 public key, from the flat launcher.json keys manifest_key_<n>_kid / _x."""
    cfg, out = config(), {}
    for key, value in cfg.items():
        if key.startswith("manifest_key_") and key.endswith("_kid"):
            x = cfg.get(key[:-4] + "_x")
            if x:
                out[str(value)] = str(x)
    return out


def is_loopback_api() -> bool:
    host = urlparse(config().get("api_base", "")).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def now() -> float:
    """Wall clock. Test seam: THEODORE_TEST_CLOCK_OFFSET (seconds) is honoured only when the API base is loopback."""
    offset = os.environ.get("THEODORE_TEST_CLOCK_OFFSET")
    if offset:
        with contextlib.suppress(Exception):
            if is_loopback_api():
                return time.time() + float(offset)
    return time.time()


# --- files ---------------------------------------------------------------------------------------------------------------

def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def write_atomic(path: Path, data: str | bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600 if mode is None else mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data.encode("utf-8") if isinstance(data, str) else data)
    if mode is not None and not IS_WINDOWS:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_json(path: Path, value, mode: int | None = 0o644) -> None:
    write_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n", mode)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def remove_tree(path: Path) -> None:
    import shutil

    def onerror(func, target, _exc):
        with contextlib.suppress(OSError):
            os.chmod(target, 0o700)
            func(target)

    if path.is_symlink() or path.is_file():
        with contextlib.suppress(OSError):
            path.unlink()
    elif path.exists():
        shutil.rmtree(path, onerror=onerror)


def link_dir(link: Path, target: Path, relative: str | None = None) -> None:
    """A directory link: symlink on POSIX, a junction (no admin rights needed) on Windows."""
    if link.is_symlink() or link.exists():
        remove_tree(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.symlink(relative or str(target), link, target_is_directory=True)


def version_key(version: str) -> tuple:
    """Order versions such as 1.0.0-rc.2 < 1.0.0-rc.10 < 1.0.0 < 1.0.1."""
    main, _, pre = version.strip().lstrip("v").partition("-")
    nums = tuple(int(p) if p.isdigit() else 0 for p in main.split("."))
    if not pre:
        return (nums, (1,))
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.replace("-", ".").split("."))
    return (nums, (0,) + parts)


# --- locks -----------------------------------------------------------------------------------------------------------------

@contextlib.contextmanager
def lock(name: str, timeout: float = 600.0):
    """An exclusive lock file under state/lock, shared by every assistant session on the machine."""
    path = home().locks / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if IS_WINDOWS:
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise Status("RETRY", f"another Theodore process holds the {name} lock.",
                                 "run the same command again in 30 seconds.",
                                 "Theodore is busy finishing another step; I will retry shortly.")
                time.sleep(0.2)
        yield
    finally:
        with contextlib.suppress(OSError):
            if IS_WINDOWS:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


# --- background jobs -------------------------------------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Job:
    """state/jobs/<name>.json: {pid, phase, percent, done, failed:{cause, detail}} for one detached task."""

    def __init__(self, name: str):
        self.name = name
        self.path = home().jobs / f"{name}.json"

    def read(self) -> dict:
        return read_json(self.path)

    def alive(self) -> bool:
        state = self.read()
        return bool(state) and not state.get("done") and not state.get("failed") and pid_alive(int(state.get("pid") or 0))

    def update(self, **fields) -> None:
        state = self.read()
        state.update(fields)
        state["updated"] = int(time.time())
        write_json(self.path, state)

    def begin(self, phase: str = "starting") -> None:
        write_json(self.path, {"pid": os.getpid(), "phase": phase, "percent": 0, "started": int(time.time())})

    def progress(self, phase: str, percent: float | None = None) -> None:
        fields = {"phase": phase}
        if percent is not None:
            fields["percent"] = max(0, min(100, int(percent)))
        self.update(**fields)

    def finish(self) -> None:
        self.update(done=True, percent=100, phase="done")

    def fail(self, cause: str, detail: str) -> None:
        self.update(failed={"cause": cause, "detail": detail[:300]})

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()

    def spawn(self, argv: list[str]) -> None:
        """Start `theodore <argv>` detached from this process (it outlives the assistant's tool call)."""
        home().ensure()
        write_json(self.path, {"pid": 0, "phase": "starting", "percent": 0, "started": int(time.time())})
        client_dir = Path(__file__).resolve().parent.parent
        command = [sys.executable, "-I", str(client_dir), *argv]
        log = open(home().logs / "client.log", "ab")
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "env": dict(os.environ), "cwd": str(home().root)}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # detached, new group, no window
        else:
            kwargs["start_new_session"] = True
        child = subprocess.Popen(command, **kwargs)
        log.close()
        self.update(pid=child.pid)

    def wait(self, seconds: float) -> bool:
        """Wait until the job is no longer running. True when it finished (done or failed)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.alive():
                return True
            time.sleep(0.25)
        return not self.alive()


def log(message: str) -> None:
    with contextlib.suppress(OSError):
        home().logs.mkdir(parents=True, exist_ok=True)
        with open(home().logs / "client.log", "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{os.getpid()}] {message}\n")


def user_agent() -> str:
    system, arch = platform_arch()
    return f"theodore-client/{__version__} ({system}-{arch})"
