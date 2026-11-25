"""Statuses, the data directory, small file helpers, locks, background jobs and the clock."""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, hostenv
from .contract import CLIENT_STATUSES, EXIT

IS_WINDOWS = os.name == "nt"


class LazyModule:
    """A module imported at its first use (any attribute read): an engine command's path then never loads what only the
    network and the archives need (ssl, http, email, tarfile, zipfile: about 100 ms of the client's start on the
    Windows test machine). Attribute writes and deletions go to the module itself, so a test that patches the module
    through this name patches the module."""

    def __init__(self, name: str):
        object.__setattr__(self, "_lazy_name", name)

    def _lazy_module(self):
        import importlib
        return importlib.import_module(object.__getattribute__(self, "_lazy_name"))

    def __getattr__(self, attr):
        return getattr(self._lazy_module(), attr)

    def __setattr__(self, attr, value):
        setattr(self._lazy_module(), attr, value)

    def __delattr__(self, attr):
        delattr(self._lazy_module(), attr)

    def __repr__(self) -> str:
        return f"<lazy module {object.__getattribute__(self, '_lazy_name')}>"


# Used here only off an engine command's path (each loads more modules, some of them DLLs on Windows).
hashlib = LazyModule("hashlib")
locale = LazyModule("locale")
_platform = LazyModule("platform")
shutil = LazyModule("shutil")


def urlparse(url: str):
    from urllib.parse import urlparse as parse
    return parse(url)


def sha256_hex(data: bytes) -> str:
    """A sha256 digest without hashlib, whose first import loads OpenSSL's library (several MB on Windows): CPython's
    own implementation, the same digest."""
    try:
        from _sha2 import sha256        # CPython 3.12 and later
    except ImportError:
        try:
            from _sha256 import sha256  # CPython 3.11 and earlier
        except ImportError:
            from hashlib import sha256
    return sha256(data).hexdigest()


# --- statuses ---------------------------------------------------------------------------------------------------------

class Status(Exception):
    """A three-line message for the assistant and the exit code that goes with it.

    tell_user is None only for automatic problem reports, which are silent by design: the third line
    then says there is nothing to tell the user. Every status is three lines. `cause` is a short code (a SETUP_FAILED
    cause, an API error code, an engine refusal code) for the usage counts; never a message."""

    cause: str | None = None

    def __init__(self, name: str, summary: str, next_step: str, tell_user: str | None):
        super().__init__(summary)
        self.name, self.summary, self.next_step, self.tell_user = name, summary, next_step, tell_user

    @property
    def code(self) -> int:
        return EXIT[self.name] if self.name in EXIT else CLIENT_STATUSES[self.name]

    def render(self) -> str:
        one = lambda text: " ".join(scrub_secrets(str(text)).split())
        lines = f"THEODORE {self.name}: {one(self.summary)}\nNEXT: {one(self.next_step)}"
        return f"{lines}\nTELL THE USER: {NOTHING_TO_TELL if self.tell_user is None else one(self.tell_user)}"


NOTHING_TO_TELL = "nothing; do not mention this to the user."


# Statuses after which no specimen folder is kept on this computer: signed out or revoked, refused by the account or
# its plan, update required (kill switch), blocked by policy, connector only. A status this computer decided about one
# call alone (specimens.local_block: keeps_sheets) is none of those.
SHEET_REFUSALS = ("SIGN_IN_REQUIRED", "SIGN_IN_PENDING", "NO_ENTITLEMENT", "UPDATE_REQUIRED", "BLOCKED_BY_POLICY", "CONNECTOR_REQUIRED")


def emit(status: Status) -> int:
    """Print a status. Once this process has learned that the device was revoked or the account suspended, every
    status printed is that refusal's block instead (account.refusal_status, which cannot raise), once, whatever else
    happened afterwards."""
    global _REFUSAL_EMITTED
    if refused() and not getattr(status, "refusal", False):
        from . import account          # imported here: account imports this module
        status = account.refusal_status()
    elif status.name == "SETUP_IN_PROGRESS":
        from . import withdraw         # after access came back: says once why Theodore downloads again
        status = withdraw.explain_setup(status)
    elif getattr(status, "auto_report", False):
        from . import commands         # imported here: commands imports this module
        status = commands.report_internal(status)      # sent before the status is printed; never raises
    if refused() or (status.name in SHEET_REFUSALS and not getattr(status, "keeps_sheets", False)):
        from . import specimens        # imported here: specimens imports this module
        specimens.sweep(everything=True)        # a device that is refused keeps no specimen sheet (never raises)
    if getattr(status, "refusal", False):
        if (getattr(_SCOPE, "emitted", False) if _SCOPED else _REFUSAL_EMITTED):
            return status.code
        if _SCOPED:                     # the service: once per call, each call learning afresh (begin_call)
            _SCOPE.emitted = True
        else:
            _REFUSAL_EMITTED = True
    print(status.render(), flush=True)
    global _LAST_EMITTED
    if _SCOPED:
        _SCOPE.last = status
    else:
        _LAST_EMITTED = status
    return status.code


_LAST_EMITTED: Status | None = None


def last_emitted() -> Status | None:
    """The last status this process (with per-thread scope: this call) printed (the usage counts record a non-OK one)."""
    return getattr(_SCOPE, "last", None) if _SCOPED else _LAST_EMITTED


# --- refusals: learned by any authenticated call (api._signed_out), answered once, centrally ---------------------------
# ("revoked", code): the device was signed out (401); credential and licence are deleted.
# ("suspended", code): the account is suspended or its plan inactive (403); the licence is deleted, the credential is
# kept (a suspension can be lifted), and no command runs until the service says otherwise.

_REFUSAL: tuple[str, str] | None = None
_REFUSAL_EMITTED = False
# The tool server answers calls side by side (one thread each): there a refusal belongs to the call that learned it,
# and each call learns afresh (a device may sign in again between calls). scope_refusals_per_thread() turns that on;
# begin_call() starts a call's own record.
_SCOPED = False
_SCOPE = threading.local()


def scope_refusals_per_thread() -> None:
    global _SCOPED
    _SCOPED = True


def begin_call() -> None:
    """A tool call starts: nothing refused yet in this thread (with per-thread scope), else in this process."""
    global _REFUSAL, _REFUSAL_EMITTED
    if _SCOPED:
        _SCOPE.refusal, _SCOPE.status, _SCOPE.emitted, _SCOPE.last = None, None, False, None
    else:
        _REFUSAL, _REFUSAL_EMITTED = None, False


def mark_refused(kind: str, code: str) -> None:
    global _REFUSAL
    current = refusal()
    if current is None or (kind == "revoked" and current[0] != "revoked"):
        if _SCOPED:
            _SCOPE.refusal = (kind, code)
        else:
            _REFUSAL = (kind, code)


def refusal() -> tuple[str, str] | None:
    return getattr(_SCOPE, "refusal", None) if _SCOPED else _REFUSAL


def refused() -> bool:
    return refusal() is not None


# --- secrets never reach a log or a printed line ------------------------------------------------------------------------

_SECRETS: set[str] = set()
_SECRET_PATTERNS = [
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"),           # a JWS (the licence token)
    re.compile(r"\bsec-[A-Za-z0-9_-]{16,}"),                                              # an installation secret
    re.compile(r"(?i)\b(installation_secret|entitlement_token|access_token|refresh_token|device_code|activation_code|"
               r"secret|token|password|authorization)(\\?[\"']?\s*[=:]\s*\\?[\"']?)[^\s\"',;&}]+"),
]
# An activation code is caught by its value (login_code registers it); the device user code in a sign-in link is meant
# to be shown and is left alone.


def register_secret(value: str | None) -> None:
    """A secret this process holds (installation secret, licence token, activation code): never printed or logged."""
    if value and len(value) >= 6:
        _SECRETS.add(value)


def scrub_secrets(text: str) -> str:
    """Every line printed (Status.render) and logged (log) passes here: the secrets this process holds and anything
    shaped like one (a bearer, a licence token, an installation secret, name=value credentials) become [redacted]."""
    for value in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    for pattern in _SECRET_PATTERNS:          # the name and its separator stay, the value goes
        text = pattern.sub(lambda m: "".join(m.groups()) + "[redacted]", text)
    return text


def usage(summary: str, next_step: str = "") -> Status:
    return Status("USAGE", summary, next_step or f"run {quoted_launcher()} help for the commands.",
                  "I called Theodore with a wrong command; I am correcting it.")


def internal(summary: str) -> Status:
    """An internal error. Printed (emit, and the MCP server's answer), it is first reported to Theodore by the client
    itself (commands.report_internal), and its NEXT and TELL THE USER lines say so: nobody is asked to send a report."""
    status = Status("INTERNAL", summary, f"run {quoted_launcher()} doctor to check this installation, then run the same command again later.",
                    "Theodore hit an internal error.")
    status.auto_report = True
    return status


# --- platform ------------------------------------------------------------------------------------------------------------

def platform_arch() -> tuple[str, str]:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(_platform.system(), _platform.system().lower())
    machine = _platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine, machine)
    return system, arch


def windows_on_arm() -> bool:
    """True when this (x64) process runs under emulation on Windows on ARM. IsWow64Process2 names the computer's own
    processor even for an emulated x64 process. Test seam: THEODORE_TEST_NATIVE_MACHINE (for example "arm64") is
    honoured only when the API base is loopback, and only to say "Windows on ARM"."""
    seam = os.environ.get("THEODORE_TEST_NATIVE_MACHINE")
    if seam and is_loopback_api():
        return seam.lower() == "arm64"
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        process, native = ctypes.c_ushort(), ctypes.c_ushort()
        if not kernel32.IsWow64Process2(ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.byref(process), ctypes.byref(native)):
            return False
        return native.value == 0xAA64                           # IMAGE_FILE_MACHINE_ARM64
    except (AttributeError, OSError):                           # Windows before 10 1709 has no IsWow64Process2
        return False


# Which assistant and session: hostenv.py, which the short command's hot path reads without loading this module.
_flag, _claude_cloud, _chatgpt_cloud = hostenv.flag, hostenv.claude_cloud, hostenv.chatgpt_cloud
host_name, session_id = hostenv.host_name, hostenv.session_id


def codex_sandboxed() -> bool:
    """This command runs inside Codex's local sandbox (Codex sets CODEX_SANDBOX for the commands it sandboxes)."""
    return bool(os.environ.get("CODEX_SANDBOX")) and not _chatgpt_cloud()


def host_executable(name: str) -> str | None:
    """An assistant's command (claude or codex), also when it is not on PATH: PATH first, then the path recorded when
    Theodore registered it (state/host-settings.json, <name>_path), then where the assistant's own installers put it.
    Claude Code's Windows installer puts claude.exe in %USERPROFILE%\\.local\\bin without adding that folder to PATH,
    and npm, nvm, Volta and Homebrew folders are often only on PATH in an interactive shell. None when it is nowhere.
    Test seam: against a loopback API the folders outside the user's home are THEODORE_TEST_HOST_DIRS (none when unset)."""
    found = shutil.which(name)
    if found:
        return found
    candidates: list[Path] = []
    recorded = read_json(home().state / "host-settings.json")
    if isinstance(recorded, dict) and isinstance(recorded.get(f"{name}_path"), str) and recorded[f"{name}_path"]:
        candidates.append(Path(recorded[f"{name}_path"]))
    user = Path.home()
    if IS_WINDOWS:
        appdata, local = os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")
        candidates += [user / ".local" / "bin" / f"{name}.exe"]
        if appdata:
            candidates += [Path(appdata) / "npm" / f"{name}.cmd"]
        if local:
            candidates += [Path(local) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe", Path(local) / "Volta" / "bin" / f"{name}.exe"]
        candidates += [user / "scoop" / "shims" / f"{name}.exe", user / ".bun" / "bin" / f"{name}.exe"]
        system: list[Path] = []
    else:
        candidates += [user / ".local" / "bin" / name, user / ".claude" / "local" / name, user / ".npm-global" / "bin" / name,
                       user / ".volta" / "bin" / name, user / ".bun" / "bin" / name, user / ".linuxbrew" / "bin" / name]
        prefix = os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("npm_config_prefix")
        if prefix:
            candidates.append(Path(prefix) / "bin" / name)
        with contextlib.suppress(OSError):
            nvm = sorted((user / ".nvm" / "versions" / "node").glob(f"*/bin/{name}"), key=lambda p: p.stat().st_mtime, reverse=True)
            candidates += nvm
        system = [Path(d) / name for d in ("/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin")]
    try:
        loopback = is_loopback_api()
    except Status:              # no launcher.json: not a test
        loopback = False
    if loopback:
        system = [Path(d) / (f"{name}.exe" if IS_WINDOWS else name) for d in (os.environ.get("THEODORE_TEST_HOST_DIRS") or "").split(os.pathsep) if d]
    for candidate in candidates + system:
        try:
            if candidate.is_file() and (IS_WINDOWS or os.access(candidate, os.X_OK)):
                return str(candidate)
        except OSError:
            continue
    return None


# Hosts that run Theodore on a machine of the assistant's own (a cloud sandbox), never on the user's computer.
CLOUD_HOSTS = ("claude-web", "chatgpt")


def cloud_sandbox() -> bool:
    """This process runs in an assistant's cloud sandbox (also ChatGPT's ordinary chat, whose sandbox has no network,
    and any environment that says so with THEODORE_EPHEMERAL). The same test as the launcher's."""
    return bool(os.environ.get("THEODORE_EPHEMERAL")) or _claude_cloud() or _chatgpt_cloud() \
        or any(Path(p).is_dir() for p in ("/mnt/skills", "/mnt/user-data", "/home/oai"))


# A cloud sandbox that cannot reach Theodore's sites: the engine cannot run there, the Theodore connector can still
# design the deck (CONNECTOR_REQUIRED; the launcher says the same).
SANDBOX_NETWORK_NEXT = ("call the Theodore connector's theodore_begin tool now and follow what it returns: it gives the design "
                        "guidance and the specimen sheets, and you build the deck with your own tools. If no Theodore connector "
                        "is available to you, say so and give the user this link, which adds the connector to their Claude account "
                        "in one step (they confirm it, choose Connect and log in): "
                        "https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=Theodore&connectorUrl="
                        "https%3A%2F%2Fmcp.theodoreand.ai%2Fmcp Then they ask again, naming Theodore.")


def sandbox_network_tell() -> str:
    try:
        host = (urlparse(site_base()).hostname or "").lower()
    except Status:
        host = ""
    root = ".".join(host.split(".")[-2:]) or host
    sites = f"{host} and *.{root}" if host else "Theodore's website and its subdomains"
    return (f"Theodore's engine cannot reach its sites from this workspace, so I am using the Theodore connector for the design. "
            f"For the full engine, add {sites} under Settings > Capabilities > Allowed websites (in ChatGPT, use Work mode), "
            "or use Theodore in an assistant on your computer, such as Claude Code.")


# --- data directory ------------------------------------------------------------------------------------------------------

# The only things Theodore creates, manages or removes inside its data directory. The directory itself may exist
# before Theodore and may hold other folders: those are never read, changed or deleted.
OWNED = ("bin", "client", "runtime", "engines", "packs", "tools", "state", "logs", "staging", "plugin", "plugin.partial")


# Theodore's folder on Windows, one rule wherever it is found (both launchers, install.cmd, the short command and this
# client, each with literally the same logic): THEODORE_HOME when set; else %USERPROFILE%\.theodore when it holds an
# installation (bin\launcher.json); else %LOCALAPPDATA%\Theodore when that holds one (made by client 0.4.14 or earlier: it
# keeps working, and updating, where it is); else %USERPROFILE%\.theodore (a new installation). Inside an MSIX-packaged
# app (the Claude desktop app) Windows redirects %LOCALAPPDATA% into the app's own folder, unseen by every other program,
# but not %USERPROFILE%\.theodore: an installation made there is the same folder for everyone.
def windows_homes() -> tuple[Path, Path]:
    """(the home, %USERPROFILE%\\.theodore; the earlier home, %LOCALAPPDATA%\\Theodore)."""
    profile = os.environ.get("USERPROFILE")
    local = os.environ.get("LOCALAPPDATA")
    return (Path(profile) if profile else Path.home()) / ".theodore", \
        (Path(local) if local else Path.home() / "AppData" / "Local") / "Theodore"


def windows_default_home() -> Path:
    new, old = windows_homes()
    if (new / "bin" / "launcher.json").is_file():
        return new
    if (old / "bin" / "launcher.json").is_file():
        return old
    return new


class Home:
    def __init__(self, root: Path | None = None):
        if root is None:
            env = os.environ.get("THEODORE_HOME")
            if env:
                root = Path(env)
            elif IS_WINDOWS:
                root = windows_default_home()
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
        # The folder holds the licensed engine, its manual and the asset packs: this user's alone (lane EXPOSURE). Only
        # the group's and others' permissions are taken away, and only on a folder this user owns; the owner's are kept.
        if not IS_WINDOWS:
            with contextlib.suppress(OSError):
                info = os.stat(self.root)
                if info.st_uid == os.getuid() and info.st_mode & 0o077:
                    os.chmod(self.root, stat.S_IMODE(info.st_mode) & 0o700)

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


LAUNCHER_FILES = ("theodore", "theodore.cmd", "launcher.json")      # the order the launcher copies them in (step 2)


def launcher_generation(folder: Path) -> int:
    """launcher_generation of the launcher.json in `folder`; -1 when there is none or it cannot be read."""
    try:
        return int(read_json(folder / "launcher.json").get("launcher_generation"))
    except (TypeError, ValueError):
        return -1


def refresh_stable_launcher(sources: list[Path]) -> str | None:
    """Bring the stable launcher in <home>/bin (its three files) up to the newest launcher generation among `sources`,
    scripts folders of the plugin (Theodore's own copy, an assistant's plugin folder), when that is newer than its own:
    what the launcher does for itself when a newer one runs (its step 2), done by an update, so a launcher fix reaches an
    updated installation whose host only ever runs the stable launcher. Only a launcher stamped for this folder's
    service. Each file is written whole beside its place and renamed into it, launcher.json last (a launcher that reads
    the new generation finds the new scripts): a launcher running from bin keeps reading the file it opened (POSIX), and
    the Windows launcher runs the client from one line, read whole, so it may be replaced while the client runs.
    Returns a line saying what changed, or None when nothing did. Never raises."""
    try:
        bin_dir = home().bin
        have = launcher_generation(bin_dir)
        if have < 0:
            return None                     # no stable launcher to refresh (the launcher makes the first one)
        own = home_environment() or str(read_json(bin_dir / "launcher.json").get("api_base") or "").rstrip("/")
        best, best_gen = None, have
        for folder in sources:
            folder = Path(folder)
            gen = launcher_generation(folder)
            api_base = str(read_json(folder / "launcher.json").get("api_base") or "").rstrip("/")
            if gen > best_gen and (not own or api_base == own) and all((folder / name).is_file() for name in LAUNCHER_FILES):
                best, best_gen = folder, gen
        if best is None:
            return None
        for name in LAUNCHER_FILES:
            write_atomic(bin_dir / name, (best / name).read_bytes(), 0o644 if name == "launcher.json" else 0o755)
        log(f"stable launcher: generation {have} -> {best_gen}, from {best}")
        return f"stable launcher: generation {have} -> {best_gen}"
    except Exception as exc:  # noqa: BLE001  an update never fails over it: the launcher still refreshes itself when run
        log(f"stable launcher: not refreshed ({exc.__class__.__name__}: {exc})")
        return None


_HOME: Home | None = None
# A process that serves a home that exists (the service) never brings it back once it is removed under it (an
# uninstall, a user deleting the folder): set by keep_removed_home(), every folder made below the home goes through
# make_parents(), which refuses while the home itself is gone. Without it a write that was already on its way (a
# sign-in record, a lock, a log line) made the folder again, and the service kept serving a home that had gone.
_KEEP_REMOVED = False


def keep_removed_home() -> None:
    global _KEEP_REMOVED
    _KEEP_REMOVED = True


def make_parents(folder: Path) -> None:
    """folder.mkdir(parents=True, exist_ok=True), except below a removed home in a process that keeps it removed."""
    if _KEEP_REMOVED:
        root = home().root
        try:
            inside = os.path.commonpath([os.path.abspath(folder), os.path.abspath(root)]) == os.path.abspath(root)
        except ValueError:                  # another drive
            inside = False
        if inside and not os.path.isdir(root):
            raise FileNotFoundError(f"Theodore's folder {root} is gone; nothing is written in it")
    folder.mkdir(parents=True, exist_ok=True)


def home() -> Home:
    global _HOME
    if _HOME is None:
        _HOME = Home()
    return _HOME


def full_launcher() -> str:
    """The stable launcher by its absolute path, quoted as every shell the assistants use reads it."""
    return f'"{home().launcher()}"'


def quoted_launcher() -> str:
    """How texts for the assistant name Theodore's command: `theodore` when Theodore's own short command is what this
    process's PATH finds for that name (the shell tool that ran us finds it the same way), else the stable launcher by
    its quoted absolute path. A plain word is what the model writes as it is, so a permission rule for it matches."""
    return SHIM_NAME if shim_active() else full_launcher()


# --- the short command (lane PROMPTS) ---------------------------------------------------------------------------------
# The model wraps the long, spaced launcher path in a variable (`T="…/theodore"; "$T" …`, `$t=…; & $t …`), which no
# permission rule can match. `theodore`, a two-line script in a folder already on the PATH of the shells Claude Code
# and Codex run, is a word every rule matches however it is combined. It only runs the stable launcher; it is written
# by the host settings (hosts settings --apply) and taken back with them and by the uninstall.
SHIM_NAME = "theodore"
SHIM_MARK = "Theodore's own short command"


def _path_entries() -> list[str]:
    return [os.path.normcase(os.path.abspath(p)) for p in (os.environ.get("PATH") or "").split(os.pathsep) if p.strip()]


def shim_dir_candidates() -> list[Path]:
    """Per-user folders a shim may go in, in order: the folder Claude Code's own installer uses (~/.local/bin, on Windows
    %USERPROFILE%\\.local\\bin, which that installer asks the person to put on PATH), ~/bin, and on Windows the per-user
    WindowsApps folder, which a stock Windows puts on every user's PATH."""
    found = [Path.home() / ".local" / "bin"]
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            found.append(Path(local) / "Microsoft" / "WindowsApps")
    else:
        found.append(Path.home() / "bin")
    return found


def shim_dir() -> Path | None:
    """The first candidate folder that is on this process's PATH (the installer runs in the host's shell tool, so it is
    that tool's PATH). None when none is."""
    entries = _path_entries()
    for folder in shim_dir_candidates():
        if os.path.normcase(os.path.abspath(folder)) in entries:
            return folder
    return None


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


# The run line's end in the batch launcher (theodore.cmd, :run_client) and in the batch short command: an error level
# of 131 or more, or below 0, is Windows refusing to start the interpreter (a policy such as AppLocker), never the
# client's (its codes end at 130); `call exit /b` gives cmd's own exit code the client's. The client tells an argument of
# several lines by this text (cli.LAUNCHER_TAIL), which cmd then carries into the last argument.
_BLOCKED = ("echo THEODORE BLOCKED_BY_POLICY: Theodore's Python could not be started: a security policy on this computer may "
            "block programs in the user folder.& echo NEXT: do not retry. Tell the user; their IT team can allow programs in "
            "Theodore's folder, or they can use Theodore through its connector.& echo TELL THE USER: This computer's security "
            "policy blocked Theodore's runtime. Your IT team can allow it, or you can use Theodore through its connector.& exit /b 73")
RUN_LINE_TAIL = f" & if not errorlevel 0 ({_BLOCKED}) else if errorlevel 131 ({_BLOCKED}) else call exit /b %%ERRORLEVEL%%"


def _msys_path(path: str) -> str | None:
    """C:\\Users\\x as Git Bash names it (/c/Users/x); None for a path it names otherwise (a share, a relative path)."""
    if len(path) < 3 or not path[0].isascii() or not path[0].isalpha() or path[1] != ":" or path[2] not in "\\/":
        return None
    return "/" + path[0].lower() + path[2:].replace("\\", "/")


def shim_texts() -> dict[str, str] | None:
    """{file name: content} of the short command for this installation, or None when it cannot be written safely. On
    Windows only for a default folder (the one windows_default_home finds): the batch file finds it by the same rule,
    from %USERPROFILE% and %LOCALAPPDATA% (no path baked in, so no code page question), and the Git Bash script by the
    folder's paths baked in, with that rule's own lines when they no longer hold.

    Since 0.4.34 each starts Theodore's client straight from its Python, with no other program (the launcher before it
    started about 20 in Git Bash: 1.14 s on the Windows test machine, 4.5 to 7 s on a laptop): the runtime and client
    are read from the folder's records (runtime/current.txt, client/current.txt) by the shell itself, so an update needs
    no new short command. When anything is missing or not a plain name, it runs the full launcher, as before."""
    head = f"# {SHIM_MARK}, written by Theodore's set-up; `theodore hosts settings --remove` and its uninstall remove it.\n"
    # The records hold plain names; a carriage return (a record written on Windows) is dropped, anything else is refused.
    read = ('r= c= ok=1\n'
            '{ IFS= read -r r || [ -n "$r" ]; } 2>/dev/null <"$h/runtime/current.txt"\n'
            '{ IFS= read -r c || [ -n "$c" ]; } 2>/dev/null <"$h/client/current.txt"\n'
            'case $r in *[!A-Za-z0-9._+-]) r=${r%?} ;; esac\n'
            'case $c in *[!A-Za-z0-9._+-]) c=${c%?} ;; esac\n'
            'case $r in ""|*[!A-Za-z0-9._+-]*) ok= ;; esac\n'
            'case $c in ""|*[!A-Za-z0-9._+-]*) ok= ;; esac\n')
    if IS_WINDOWS:
        new, old = windows_homes()
        root = os.path.normcase(os.path.abspath(home().root))
        same = lambda path: root == os.path.normcase(os.path.abspath(path))     # noqa: E731
        if not (same(new) or (same(old) and same(windows_default_home()))):
            return None
        windows = str(home().root)
        msys, msys_new = _msys_path(windows), _msys_path(str(new))
        # The rule of windows_default_home, as the batch file and the Git Bash script read it.
        batch = (f"@echo off\r\nrem {SHIM_MARK}, written by Theodore's set-up; its uninstall removes it.\r\n"
                 "setlocal EnableExtensions DisableDelayedExpansion\r\n"
                 'set "THEODORE_SHIM_HOME=%USERPROFILE%\\.theodore"\r\n'
                 'if not exist "%USERPROFILE%\\.theodore\\bin\\launcher.json" if exist "%LOCALAPPDATA%\\Theodore\\bin\\launcher.json" '
                 'set "THEODORE_SHIM_HOME=%LOCALAPPDATA%\\Theodore"\r\n'
                 'if defined THEODORE_HOME set "THEODORE_SHIM_HOME=%THEODORE_HOME%"\r\n'
                 "rem Theodore's client straight from its Python, by cmd's own reads of the folder's records; else the launcher.\r\n"
                 'set "THEODORE_SHIM_RT="\r\n'
                 'set "THEODORE_SHIM_CV="\r\n'
                 'if exist "%THEODORE_SHIM_HOME%\\runtime\\current.txt" set /p THEODORE_SHIM_RT=<"%THEODORE_SHIM_HOME%\\runtime\\current.txt"\r\n'
                 'if exist "%THEODORE_SHIM_HOME%\\client\\current.txt" set /p THEODORE_SHIM_CV=<"%THEODORE_SHIM_HOME%\\client\\current.txt"\r\n'
                 "if not defined THEODORE_SHIM_RT goto :launcher\r\n"
                 "if not defined THEODORE_SHIM_CV goto :launcher\r\n"
                 'if not exist "%THEODORE_SHIM_HOME%\\runtime\\%THEODORE_SHIM_RT%\\python\\python.exe" goto :launcher\r\n'
                 'if not exist "%THEODORE_SHIM_HOME%\\client\\%THEODORE_SHIM_CV%\\__main__.py" goto :launcher\r\n'
                 'if not exist "%THEODORE_SHIM_HOME%\\bin\\theodore.cmd" goto :launcher\r\n'
                 'set "THEODORE_HOME=%THEODORE_SHIM_HOME%"\r\n'
                 'set "THEODORE_LAUNCHER=%THEODORE_SHIM_HOME%\\bin\\theodore.cmd"\r\n'
                 # One line, then the batch ends: this file may be replaced while the client runs (an update rewrites it).
                 '"%THEODORE_SHIM_HOME%\\runtime\\%THEODORE_SHIM_RT%\\python\\python.exe" -I "%THEODORE_SHIM_HOME%\\client\\%THEODORE_SHIM_CV%" %*'
                 + RUN_LINE_TAIL + "\r\n"
                 ":launcher\r\n"
                 '"%THEODORE_SHIM_HOME%\\bin\\theodore.cmd" %*\r\n')
        hot = ""
        if msys and msys_new:
            # Only this folder, found by the rule: THEODORE_HOME unset (or naming it), and for a folder of 0.4.14 or earlier,
            # none in %USERPROFILE%\.theodore. The client gets the Windows forms; MSYS converts each argument as before.
            claim = ('[ -z "${THEODORE_HOME:-}" ] || [ "$THEODORE_HOME" = "$w" ] || [ "$THEODORE_HOME" = "$h" ]' if same(new) else
                     f'{{ [ -z "${{THEODORE_HOME:-}}" ] && [ ! -f {_sh_quote(msys_new + "/bin/launcher.json")} ]; }} || '
                     '[ "$THEODORE_HOME" = "$w" ] || [ "$THEODORE_HOME" = "$h" ]')
            hot = (f"w={_sh_quote(windows)} h={_sh_quote(msys)}\n"
                   f"if {claim}; then\n"
                   + "".join("  " + line + "\n" for line in read.splitlines()) +
                   '  if [ -n "$ok" ] && [ -f "$h/runtime/$r/python/python.exe" ] && [ -f "$h/client/$c/__main__.py" ] && [ -f "$h/bin/theodore.cmd" ]; then\n'
                   '    THEODORE_HOME=$w THEODORE_LAUNCHER="$w\\\\bin\\\\theodore.cmd"\n'
                   '    export THEODORE_HOME THEODORE_LAUNCHER\n'
                   # exec: no copy of this shell waits beside the client (in MSYS each one is a process started);
                   # execfail: a start the system refuses (a policy) returns here, and the launcher says so.
                   '    shopt -s execfail 2>/dev/null\n'
                   '    exec "$h/runtime/$r/python/python.exe" -I "$w\\\\client\\\\$c" "$@"\n'
                   '  fi\n'
                   'fi\n')
        return {
            "theodore.cmd": batch,
            "theodore": "#!/bin/sh\n" + head + hot
                        + 'h=$(cygpath -u "$USERPROFILE" 2>/dev/null)/.theodore\n'
                        + 'o=$(cygpath -u "$LOCALAPPDATA" 2>/dev/null)/Theodore\n'
                        + '[ ! -f "$h/bin/launcher.json" ] && [ -f "$o/bin/launcher.json" ] && h=$o\n'
                        + '[ -n "${THEODORE_HOME:-}" ] && h=$(cygpath -u "$THEODORE_HOME" 2>/dev/null)\n'
                        + 'b=$h/bin\n'
                        + '[ -f "$b/theodore" ] && exec sh "$b/theodore" "$@"\n'
                        + 'exec "$b/theodore.cmd" "$@"\n',
        }
    root = str(home().root)
    default = os.path.abspath(root) == os.path.abspath(Path.home() / ".theodore")
    lines = "#!/bin/sh\n" + head + f"h={_sh_quote(root)}\n"
    # A folder elsewhere is this short command's own (THEODORE_HOME set always); the default one yields to another.
    body = (read + 'p=$h/runtime/$r/python/bin/python3\n'
            'if [ -n "$ok" ] && [ -f "$p" ] && [ -x "$p" ] && [ -f "$h/client/$c/__main__.py" ] && [ -f "$h/bin/theodore" ]; then\n'
            '  THEODORE_HOME=$h THEODORE_LAUNCHER=$h/bin/theodore\n'
            '  export THEODORE_HOME THEODORE_LAUNCHER\n'
            '  exec "$p" -I "$h/client/$c" "$@"\n'
            'fi\n')
    if default:
        lines += 'if [ "${THEODORE_HOME:-$h}" = "$h" ]; then\n' + "".join("  " + line + "\n" for line in body.splitlines()) + "fi\n"
    else:
        lines += body
    if not default:                 # the launchers set THEODORE_HOME always
        lines += "THEODORE_HOME=$h\nexport THEODORE_HOME\n"
    return {"theodore": lines + f'exec {_sh_quote(str(home().launcher()))} "$@"\n'}


def shim_files() -> dict[str, bytes] | None:
    """{file name: content} of the short command as it is written now: the native front once it is placed (front.py:
    on Windows its own copy, theodore.exe; elsewhere a script that runs it), else the text shim_texts gives."""
    from . import front
    placed = front.shim_bytes()
    if placed:
        return placed
    texts = shim_texts()
    return None if texts is None else {name: text.encode("utf-8") for name, text in texts.items()}


_SHIM_ACTIVE: list[bool] = []


def shim_active() -> bool:
    """Whether `theodore` on this process's PATH is this installation's own short command, as written now."""
    if _SHIM_ACTIVE:
        return _SHIM_ACTIVE[0]
    active = False
    with contextlib.suppress(OSError, ValueError):
        found = shutil.which(SHIM_NAME)
        files = shim_files()
        if found and files:
            path = Path(found)
            # Windows names it as PATHEXT spells the extension (theodore.CMD): the file name is matched without case.
            name = next((n for n in files if n.lower() == path.name.lower()), None) if IS_WINDOWS else path.name
            active = name in files and shim_matches(path, files[name])
    _SHIM_ACTIVE[:] = [active]
    return active


def shim_matches(path: Path, text: str | bytes) -> bool:
    """The file holds exactly this text (or these bytes), byte for byte (read as bytes: a text read turns the batch
    file's CRLF into LF, and the Windows short command then never matched; WINDOWS's recount)."""
    try:
        return path.read_bytes() == (text if isinstance(text, bytes) else text.encode("utf-8"))
    except OSError:
        return False


def shim_reset() -> None:
    _SHIM_ACTIVE.clear()


# --- configuration (launcher.json beside the stable launcher) ---------------------------------------------------------

_CONFIG: dict | None = None


_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def plugin_version() -> str:
    """The version of the installed Theodore plugin, as the API wants it (a semantic version, never empty).

    Read from the plugin manifest of the skill folder that called us (THEODORE_SKILL_DIR is <plugin>/skills/<skill>),
    then from the installer's unpacked copy in <home>/plugin (what Codex runs), then from launcher.json, else 0.0.0."""
    roots = []
    if os.environ.get("THEODORE_SKILL_DIR"):
        roots.append(Path(os.environ["THEODORE_SKILL_DIR"]).parent.parent)
    roots.append(home().root / "plugin" / "plugins" / "theodore")
    for root in roots:
        for manifest in (root / ".claude-plugin" / "plugin.json", root / "plugin.json"):
            try:
                version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "")
            except (OSError, ValueError, AttributeError):
                continue
            if _SEMVER.match(version):
                return version
    version = str(config().get("shim_version") or "")
    return version if _SEMVER.match(version) else "0.0.0"


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


def running_stamp() -> dict:
    """launcher.json of the build that is running: beside the skill folder that called us (the launcher names it),
    else <home>/bin's. {} when there is none."""
    skill = os.environ.get("THEODORE_SKILL_DIR")
    if skill:
        try:
            stamp = json.loads((Path(skill) / "scripts" / "launcher.json").read_text(encoding="utf-8"))
            if isinstance(stamp, dict):
                return stamp
        except (OSError, ValueError):
            pass
    try:
        return config()
    except Status:
        return {}


def environment_path() -> Path:
    """state/environment: the API base of the service this Theodore folder was set up for, one line (the launchers
    and installers read it too)."""
    return home().state / "environment"


def home_environment() -> str:
    lines = read_text(environment_path()).splitlines()
    recorded = lines[0].strip() if lines else ""
    if not recorded:            # a folder from before the record: its stable launcher.json says
        recorded = str(read_json(home().bin / "launcher.json").get("api_base") or "").strip()
    return recorded.rstrip("/")


def check_environment(command: str | None) -> None:
    """A Theodore folder belongs to one service. A build stamped for another refuses it, naming both and the ways
    out; `uninstall` (one of those ways) is allowed. A folder without a record gets this build's."""
    own = str(running_stamp().get("api_base") or "").strip().rstrip("/")
    if not own:
        return
    if not environment_path().exists() and not (home().bin / "launcher.json").exists() and home().state.is_dir():
        _claim_environment(own)
    theirs = home_environment()
    if theirs and theirs != own:
        if command == "uninstall":
            return
        raise Status("USAGE", f"this Theodore folder ({home().root}) belongs to the Theodore service at {theirs}, but this build is "
                              f"stamped for {own}; a folder is never shared between two services.",
                     f"do not retry. Tell the user: to use this build, set THEODORE_HOME to a separate folder for it and run the same "
                     f"command again, or first remove the other installation with: \"{home().bin / ('theodore.cmd' if IS_WINDOWS else 'theodore')}\" uninstall",
                     f"Theodore found an installation for another Theodore service ({theirs}) in {home().root}. To use this one, give it "
                     f"its own folder (THEODORE_HOME) or uninstall the other first.")
    if not environment_path().exists() and home().state.is_dir():          # a folder from before the record, ours
        _claim_environment(own)


def _claim_environment(own: str) -> None:
    """Record the folder's service in one exclusive step: written whole beside its place, then linked (POSIX) or
    renamed (Windows, which refuses to replace) into it, which fails when a record is already there. The caller reads
    the record afterwards, so of two builds racing on an empty folder one wins and the other refuses."""
    final = environment_path()
    tmp = final.with_name(f".environment.{os.getpid()}.{threading.get_ident()}")
    try:
        tmp.write_text(own + "\n", encoding="utf-8")
        if IS_WINDOWS:
            os.rename(tmp, final)
        else:
            try:
                os.link(tmp, final)
            except FileExistsError:
                pass
            except OSError:         # no hard links on this file system: an exclusive claim directory, then a rename
                claim = final.with_name(".environment.claim")
                try:
                    os.mkdir(claim)
                except FileExistsError:
                    deadline = time.monotonic() + 10
                    while claim.is_dir() and not final.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                else:
                    try:
                        if not final.exists():
                            os.rename(tmp, final)
                    finally:
                        os.rmdir(claim)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def site_base() -> str:
    """The site this build belongs to (sign-in pages, the portal, the install page): the running build's
    launcher.json site_base, or for a launcher.json from before that key the origin of its marketplace address.
    Never a host written here: a staging build must not send anyone to production. Empty when it names neither."""
    cfg = running_stamp()
    base = str(cfg.get("site_base") or "").strip().rstrip("/")
    if not base:
        parsed = urlparse(str(cfg.get("marketplace_url") or ""))
        base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    return base


def site_url(path: str = "") -> str:
    base = site_base()
    return base + path if base else "the Theodore site"


def site_name(path: str = "") -> str:
    """The same without the scheme, for a sentence said to the user."""
    return site_url(path).split("://", 1)[-1]


PLACEHOLDER_KID = "manifest-unset"


def placeholder_key(kid: str, x: str) -> bool:
    """The repository's unstamped key (kid manifest-unset, or an all-zero or otherwise unusable public key)."""
    from . import ed25519
    if kid == PLACEHOLDER_KID or not x:
        return True
    try:
        raw = ed25519.b64url_decode(x)
    except ValueError:
        return True
    return len(raw) != 32 or not any(raw) or ed25519.small_order(raw)


def manifest_keys() -> dict[str, str]:
    """kid -> base64url Ed25519 public key, from the flat launcher.json keys manifest_key_<n>_kid / _x. A placeholder
    key is never returned: a plugin built without its release keys trusts nothing."""
    cfg, out = config(), {}
    for key, value in cfg.items():
        if key.startswith("manifest_key_") and key.endswith("_kid"):
            x = str(cfg.get(key[:-4] + "_x") or "")
            if not placeholder_key(str(value), x):
                out[str(value)] = x
    return out


def no_release_keys() -> Status:
    return Status("INTERNAL", "this copy of the Theodore plugin carries no release signing key (launcher.json has only the "
                              "placeholder key), so it cannot trust any download.",
                  f"do not retry. Tell the user to install Theodore from {site_url('/install')}; this copy is an unstamped development build.",
                  f"This copy of Theodore was built without its security keys and cannot be used. Please install it again from {site_name('/install')}.")


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

def child_text(data: bytes | str | None) -> str:
    """A child process's output as text. Theodore's children (its Python with PYTHONIOENCODING=utf-8, Node, the
    assistants' CLIs) write UTF-8 on every platform; Windows' own tools write the console's code page. UTF-8 first,
    else the system's code page, so C:\\Users\\Théo is never read as "ThÃ©o" (WINDOWS: a self-test error)."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if IS_WINDOWS:
            with contextlib.suppress(LookupError):
                return data.decode("oem", errors="replace")
        return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_json(path: Path, default=None):
    # On Windows a read that meets a writer's replace is refused for a moment (PermissionError); retried, so that a
    # job's state file is never mistaken for a missing one (which reads as "the job died").
    deadline = time.monotonic() + 2.0
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if IS_WINDOWS and time.monotonic() < deadline:
                time.sleep(0.02)
                continue
            return {} if default is None else default
        except (OSError, ValueError):
            return {} if default is None else default


def write_atomic(path: Path, data: str | bytes, mode: int | None = None) -> None:
    make_parents(path.parent)
    # Named by the thread too: the tool server's calls run side by side (0.4.34), and two of them writing one file
    # shared one name, so one moved the other's half-written copy into place or found it gone.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600 if mode is None else mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data.encode("utf-8") if isinstance(data, str) else data)
        if mode is not None and not IS_WINDOWS:
            os.chmod(tmp, mode)
        replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def replace(src: Path, dest: Path) -> None:
    """os.replace, patient on Windows: there a file another process has open (a status reader polling a job's state)
    cannot be replaced, and the call fails with "Access is denied" for as long as that reader holds it, which is
    milliseconds. Retried for up to five seconds before the error is let through."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if not IS_WINDOWS or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


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


class LinkError(OSError):
    """A directory link was made but does not lead to an existing directory from this process."""


def link_dir(link: Path, target: Path, relative: str | None = None) -> None:
    """A directory link: symlink on POSIX, a junction (no admin rights needed) on Windows. On Windows the link is
    checked once made: one that does not lead to a directory raises LinkError, naming both paths."""
    if link.is_symlink() or link.exists():
        remove_tree(link)
    make_parents(link.parent)
    if IS_WINDOWS:
        # The junction stores the target's final real path, not the path as given: inside an MSIX-packaged app (the
        # Claude desktop app) Windows silently redirects this process's files under %LOCALAPPDATA% to
        # %LOCALAPPDATA%\Packages\<package>\LocalCache\Local\..., but follows a junction without that redirect, so a
        # junction to the plain path leads nowhere. realpath names the redirected folder.
        real = os.path.realpath(target)
        # The paths reach cmd only through variables, expanded inside quotes: on its command line a path with &, ^, % or
        # a parenthesis (C:\\Users\\Tom & Jerry) would be cmd syntax, not a name.
        cmd = os.path.join(os.environ.get("SystemRoot") or "C:" + os.sep + "Windows", "System32", "cmd.exe")
        try:
            subprocess.run(f'"{cmd}" /d /v:off /c "mklink /J "%THEODORE_LINK%" "%THEODORE_LINK_TARGET%""', check=True,
                           env={**os.environ, "THEODORE_LINK": str(link), "THEODORE_LINK_TARGET": real},
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            code = getattr(exc, "returncode", None)
            raise LinkError(f"the link {link} to {real} could not be made (mklink {'exit ' + str(code) if code is not None else exc.__class__.__name__})") from None
        if not os.path.isdir(link):
            raise LinkError(f"the link {link} was made, but its target {real} is not reachable from here")
    else:
        os.symlink(relative or str(target), link, target_is_directory=True)


def version_key(version: str) -> tuple:
    """Semantic-version precedence: 1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-beta.2 < 1.0.0-beta.11 < 1.0.0 < 1.0.1.
    Build metadata (+...) is ignored; numeric pre-release identifiers sort below alphanumeric ones and by value; a
    shorter identifier list sorts first when the rest is equal."""
    text = version.strip().lstrip("v").split("+", 1)[0]
    main, _, pre = text.partition("-")
    nums = tuple(int(p) if p.isdigit() else 0 for p in main.split("."))
    nums = (nums + (0, 0, 0))[:3]
    if not pre:
        return (nums, (1,))
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (nums, (0,) + parts)


# --- locks -----------------------------------------------------------------------------------------------------------------

_HELD = threading.local()


LOCK_BUSY = "another Theodore process holds the"


def lock_busy(status) -> bool:
    """True for the RETRY a lock gives when another Theodore process holds it (a status, or its recorded summary)."""
    summary = status if isinstance(status, str) else getattr(status, "summary", "")
    return str(summary).startswith(LOCK_BUSY)


@contextlib.contextmanager
def lock(name: str, timeout: float = 600.0):
    """An exclusive lock file under state/lock, shared by every assistant session on the machine. A thread that
    already holds it enters again (a locked step may call another that takes the same lock); any other thread or
    process waits, and after `timeout` seconds gets RETRY: nothing guarded by the lock is written without it."""
    path = home().locks / f"{name}.lock"
    key = str(path)
    held = getattr(_HELD, "names", None)
    if held is None:
        held = _HELD.names = {}
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    make_parents(path.parent)
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
                    raise Status("RETRY", f"{LOCK_BUSY} {name} lock.",
                                 "run the same command again in 30 seconds.",
                                 "Theodore is busy finishing another step; I will retry shortly.")
                time.sleep(0.05 if timeout < 5 else 0.2)
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)
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


# --- whose process a number is (0.4.32) -------------------------------------------------------------------------------
# A process number read from a file proves nothing by itself: a job's record keeps its number after the job has ended,
# and the system hands freed numbers out again (quickly on Windows). An uninstall stopped whatever held a recorded
# number, so it could stop a stranger's program and, with taskkill /T /F, everything that program had started (seen on
# the Windows test machine on 2026-11-23, with a stand-in given a finished job's number). What a process is now is read
# here; commands._recorded_is_ours decides from it.

def _windows_process_facts(pid: int) -> tuple[str | None, float | None]:
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None, None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        program = buffer.value if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        started = None
        if kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime      # 100 ns since 1601-01-01 UTC
            started = ticks / 1e7 - 11644473600 if ticks else None
        return program, started
    finally:
        kernel32.CloseHandle(handle)


def _ps(pid: int, field: str) -> str | None:
    try:
        out = subprocess.run(["ps", "-ww", "-o", f"{field}=", "-p", str(int(pid))], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.decode("utf-8", "replace").strip()
    return text if out.returncode == 0 and text else None


def _elapsed_seconds(text: str) -> float | None:
    """ps's etime: [[dd-]hh:]mm:ss."""
    days, _, rest = text.strip().rpartition("-")
    try:
        parts = [int(x) for x in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None


def _posix_process_facts(pid: int) -> tuple[str | None, float | None]:
    proc = Path("/proc") / str(int(pid))
    if (proc / "stat").exists():            # Linux: exact, and no program to start
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            boot = next(float(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
            return command, boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError, StopIteration):
            return None, None
    command = _ps(pid, "command")
    elapsed = _elapsed_seconds(_ps(pid, "etime") or "")
    return command, (time.time() - elapsed if elapsed is not None else None)


def process_facts(pid: int) -> tuple[str | None, float | None]:
    """(what the process runs, when it started, seconds since the epoch) for the process holding `pid` now; None for
    what cannot be read. What it runs: on Windows its program file, elsewhere its whole command line."""
    if not pid or pid <= 0:
        return None, None
    try:
        return _windows_process_facts(pid) if IS_WINDOWS else _posix_process_facts(pid)
    except Exception:       # noqa: BLE001  no proof: the caller leaves the process alone
        return None, None


# How much later than its record's last write a process may seem to have started and still be the one that wrote it:
# Windows times are exact; ps gives whole seconds of elapsed time.
START_TOLERANCE = 1.0 if IS_WINDOWS else 2.0


def _parents() -> dict[int, int]:
    """Every process's parent, by number (empty when it cannot be read)."""
    parents: dict[int, int] = {}
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)              # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == wintypes.HANDLE(-1).value:
            return parents
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(Entry)
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return parents
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, timeout=10).stdout.decode("ascii", "replace")
    except (OSError, subprocess.SubprocessError):
        return parents
    for line in out.splitlines():
        with contextlib.suppress(ValueError):
            pid, ppid = (int(x) for x in line.split())
            parents[pid] = ppid
    return parents


def ancestors() -> set[int]:
    """This process and every process above it, to the top: never stopped by Theodore, whatever a record says (a
    parent's number can be another program's now, which is only more left alone)."""
    mine = {os.getpid(), os.getppid()}
    try:
        parents = _parents()
    except Exception:       # noqa: BLE001
        parents = {}
    at = os.getppid()
    while at and at in parents and parents[at] not in mine and parents[at] > 0:
        at = parents[at]
        mine.add(at)
    mine.discard(0)
    return mine


class Job:
    """state/jobs/<name>.json: {pid, phase, percent, done, failed:{cause, detail}} for one detached task."""

    def __init__(self, name: str):
        self.name = name
        self.path = home().jobs / f"{name}.json"

    def read(self) -> dict:
        return read_json(self.path)

    # On Windows a record another process holds (its replace, an antivirus scan of the new file) cannot be read for a
    # moment, sometimes longer than read_json's 2 s on a loaded machine: read_json then gives {}, which read as "the
    # job ended", and the setup's wait ended early and started a second job (release/0.5.0's Windows runtime target,
    # 7 suites side by side, 25 Nov). A record that is there but held is read again; held this long, it is taken for
    # a job still running (an ended job's record is readable).
    UNREADABLE_WAIT_S = 30.0

    def _state(self) -> dict | None:
        """The record; {} when there is none or it is not a JSON object; None while it is there but held (Windows)."""
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except PermissionError:
            return None if IS_WINDOWS else {}
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def alive(self) -> bool:
        deadline = time.monotonic() + self.UNREADABLE_WAIT_S
        state = self._state()
        while state is None:
            if time.monotonic() >= deadline:
                return True
            time.sleep(0.1)
            state = self._state()
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

    # A finished job's record no longer names a process (0.4.32): the number is soon another program's. Records written
    # before keep theirs, so whoever acts on a number still checks whose it is (commands._recorded_is_ours).
    def finish(self) -> None:
        self.update(done=True, percent=100, phase="done", pid=0)

    def fail(self, cause: str, detail: str) -> None:
        self.update(failed={"cause": cause, "detail": scrub_secrets(detail)[:300]}, pid=0)

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
            # Detached, new group, no window; and out of the job object the assistant's command may run in (one that is
            # closed at a time limit takes its processes with it), when that job allows it.
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000
            try:
                child = subprocess.Popen(command, **kwargs)
            except OSError:                  # a job that forbids breaking away: detached all the same
                kwargs["creationflags"] &= ~0x01000000
                child = subprocess.Popen(command, **kwargs)
            log.close()
            self.update(pid=child.pid)
            return
        # Through a shell that starts the job in its own session and exits at once: the job is then nobody's child, so a
        # host that stops the assistant's command with the whole process tree under it (a time limit) does not stop it.
        log.close()
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        relay = ["/bin/sh", "-c", 'log=$1; shift; "$@" </dev/null >>"$log" 2>&1 & echo $!', "sh", str(home().logs / "client.log"), *command]
        try:
            out = subprocess.run(relay, timeout=30, **kwargs).stdout
            pid = int(out.decode("ascii", "replace").strip() or 0)
        except (OSError, subprocess.SubprocessError, ValueError):
            pid = 0
        self.update(pid=pid)

    def wait(self, seconds: float) -> bool:
        """Wait until the job is no longer running. True when it finished (done or failed)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.alive():
                return True
            time.sleep(0.25)
        return not self.alive()


_LOG_FILE: Path | None = None       # set by uninstall: its record goes outside the folder it removes


def uninstall_log() -> Path:
    """Where an uninstall keeps its record: beside Theodore's folder, never inside it (~/.theodore-uninstall.log,
    %USERPROFILE%\\.theodore-uninstall.log), so what it did can still be read once the folder is gone."""
    root = home().root
    return root.parent / f"{root.name}-uninstall.log"


def log_elsewhere(path: Path) -> None:
    """From now on this process logs to `path` (kept under 256 KB: an older record is cut first), not to logs/."""
    global _LOG_FILE
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size > 256 * 1024:
            path.write_text(("\ufeff" if IS_WINDOWS else "") + path.read_text(encoding="utf-8-sig", errors="replace")[-64 * 1024:],
                            encoding="utf-8")
    _LOG_FILE = path


def log(message: str) -> None:
    with contextlib.suppress(OSError):
        if _LOG_FILE is not None:
            target = _LOG_FILE
        else:
            make_parents(home().logs)
            target = home().logs / "client.log"
        # On Windows a new log starts with a UTF-8 byte-order mark: Notepad and PowerShell 5.1 then read it as UTF-8
        # (without one they read the ANSI code page: C:\Users\Théo showed mangled, WINDOWS W-8).
        bom = "\ufeff" if IS_WINDOWS and not os.path.exists(target) else ""
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{bom}{time.strftime('%Y-%m-%dT%H:%M:%S')} [{os.getpid()}] {scrub_secrets(message)}\n")


def user_agent() -> str:
    system, arch = platform_arch()
    return f"theodore-client/{__version__} ({system}-{arch})"
