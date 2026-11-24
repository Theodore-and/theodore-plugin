"""Where the service listens and how a relay or a front reaches it (contract C1, "Endpoint and discovery").

- macOS and Linux: a Unix socket `<home>/state/run/s.sock`, mode 0600 in a 0700 folder; the peer's uid is checked
  where the system tells it.
- Windows: a named pipe `\\\\.\\pipe\\theodore-<id16>` (byte mode, a DACL for the user's SID only,
  PIPE_REJECT_REMOTE_CLIENTS, the first instance taken with FILE_FLAG_FIRST_PIPE_INSTANCE so no other user can squat
  the name); a client checks that the pipe's server is the pid service.json names.
- Fallback (THEODORE_SERVICE_ENDPOINT=tcp, or a pipe that cannot be made): TCP on 127.0.0.1, an ephemeral port; the
  secret must come within 2 s of connecting.

`service.json` (0600) names the endpoint, the per-start secret and the versions; the service writes it once it listens
and removes it when it stops taking calls. The start rule (`ensure`) is the client side: read service.json, connect,
or take the start lock, check again, start the service detached and wait for its service.json.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
SOCKET_NAME = "s.sock"
SERVICE_JSON = "service.json"
START_LOCK = "start.lock"
INSTANCE_LOCK = "service.lock"
HELLO_TIMEOUT_S = float(os.environ.get("THEODORE_SERVICE_HELLO_S") or 5.0)
TCP_HELLO_TIMEOUT_S = min(2.0, HELLO_TIMEOUT_S)
START_POLL_S = 0.02
START_WAIT_S = float(os.environ.get("THEODORE_SERVICE_START_WAIT") or 5.0)


class Unreachable(Exception):
    """No service answers at the endpoint service.json names (or there is no service.json)."""


# --- identity and places ------------------------------------------------------------------------------------------------

def user_id() -> str:
    """The user's SID on Windows, the numeric uid elsewhere."""
    if not IS_WINDOWS:
        return str(os.getuid())
    return _windows_user_sid() or os.environ.get("USERNAME", "user")


def id16(home: Path) -> str:
    """First 16 hex of sha256(user id + LF + realpath of the Theodore home)."""
    try:
        real = os.path.realpath(str(home))
    except OSError:
        real = str(home)
    return hashlib.sha256(f"{user_id()}\n{real}".encode("utf-8")).hexdigest()[:16]


def run_dir(home: Path) -> Path:
    folder = Path(home) / "state" / "run"
    folder.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        with contextlib.suppress(OSError):
            os.chmod(folder.parent, 0o700)
            os.chmod(folder, 0o700)
    return folder


def service_json_path(home: Path) -> Path:
    return Path(home) / "state" / "run" / SERVICE_JSON


def read_service_json(home: Path) -> dict | None:
    try:
        data = json.loads(service_json_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("endpoint"), dict):
        return None
    return data


def write_service_json(home: Path, record: dict) -> None:
    path = service_json_path(home)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(record, indent=1, sort_keys=True).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    for attempt in range(40):           # Windows: a reader holding the file for a moment refuses the replace
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == 39:
                raise
            time.sleep(0.025)


def remove_service_json(home: Path, pid: int) -> None:
    """Remove service.json when it still names `pid` (a successor's is left alone)."""
    record = read_service_json(home)
    if record is not None and record.get("pid") == pid:
        with contextlib.suppress(OSError):
            service_json_path(home).unlink()


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        finally:
            kernel32.CloseHandle(handle)
        return code.value == 259                                  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- locks --------------------------------------------------------------------------------------------------------------

class FileLock:
    """An exclusive lock on a file (flock; msvcrt.locking on Windows): it works across sessions and across the Claude
    app's package boundary, since both see the same file. `acquire(timeout)` returns True when held."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self, timeout: float = 0.0) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if IS_WINDOWS:
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.02)

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            if IS_WINDOWS:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class StartLock:
    """C1's start lock: the named mutex `Local\\theodore-start-<id16>` on Windows (what the native front takes), and the
    file lock `<home>/state/run/start.lock` everywhere (what crosses the app's package boundary)."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self._file = FileLock(run_dir(home) / START_LOCK)
        self._mutex = None

    def __enter__(self):
        deadline = time.monotonic() + 15.0
        if IS_WINDOWS:
            self._mutex = _windows_mutex(f"Local\\theodore-start-{id16(self.home)}", 15.0)
        if not self._file.acquire(max(0.0, deadline - time.monotonic())):
            self.__exit__(None, None, None)
            raise Unreachable("another process holds the start lock")
        return self

    def __exit__(self, *_exc):
        self._file.release()
        if self._mutex is not None:
            _windows_release_mutex(self._mutex)
            self._mutex = None
        return False


# --- the server side ---------------------------------------------------------------------------------------------------

def endpoint_kind() -> str:
    wanted = (os.environ.get("THEODORE_SERVICE_ENDPOINT") or "").strip().lower()
    if wanted in ("tcp", "pipe", "unix"):
        if wanted == "pipe" and not IS_WINDOWS or wanted == "unix" and IS_WINDOWS:
            return "pipe" if IS_WINDOWS else "unix"
        return wanted
    return "pipe" if IS_WINDOWS else "unix"


class UnixListener:
    kind = "unix"
    hello_timeout = HELLO_TIMEOUT_S

    def __init__(self, home: Path):
        self.path = socket_path(home)
        self.address = str(self.path)
        self.port = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()          # a dead service's socket (the instance lock says no live one uses it)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            self._sock.bind(self.address)
        finally:
            os.umask(old)
        os.chmod(self.address, 0o600)
        self._sock.listen(128)
        self._sock.settimeout(0.5)

    def accept(self):
        """A connection, or None after the poll interval (so the caller can check whether to stop)."""
        try:
            conn, _ = self._sock.accept()
        except socket.timeout:
            return None
        conn.settimeout(None)
        if not _same_user(conn):
            conn.close()
            return None
        return conn

    def close(self, unlink: bool = True) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()
        if unlink:
            with contextlib.suppress(OSError):
                self.path.unlink()


SUN_PATH_MAX = 100                  # sockaddr_un holds 104 bytes on macOS, 108 on Linux, the terminating NUL included


def socket_path(home: Path) -> Path:
    """<home>/state/run/s.sock; when that path is too long for a Unix socket (a deep THEODORE_HOME), a socket of this
    home in a folder of this user's own in the runtime folder (XDG_RUNTIME_DIR) or /tmp, mode 0700 and checked to be
    ours. service.json names whichever it is."""
    path = run_dir(home) / SOCKET_NAME
    if len(os.fsencode(str(path))) <= SUN_PATH_MAX:
        return path
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime and os.path.isdir(runtime) else Path("/tmp")
    folder = base / f"theodore-{os.getuid()}"
    with contextlib.suppress(FileExistsError):
        os.mkdir(folder, 0o700)
    info = os.lstat(folder)
    import stat as stat_mod
    if not stat_mod.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(f"{folder} is not a private folder of this user")
    return folder / f"{id16(home)}.sock"


def _same_user(conn: socket.socket) -> bool:
    """The peer runs as this user (where the system says; the socket's mode and folder already allow no one else)."""
    try:
        if sys.platform.startswith("linux"):
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            return uid == os.getuid()
        if sys.platform == "darwin":
            creds = conn.getsockopt(0, 0x001, 76)       # SOL_LOCAL, LOCAL_PEERCRED: struct xucred
            _version, uid = struct.unpack_from("<Ii", creds)
            return uid == os.getuid()
    except (OSError, struct.error):
        return True
    return True


class TcpListener:
    kind = "tcp"
    hello_timeout = TCP_HELLO_TIMEOUT_S

    def __init__(self, home: Path):
        del home
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(128)
        self._sock.settimeout(0.5)
        self.address = "127.0.0.1"
        self.port = self._sock.getsockname()[1]

    def accept(self):
        try:
            conn, peer = self._sock.accept()
        except socket.timeout:
            return None
        if peer[0] != "127.0.0.1":
            conn.close()
            return None
        conn.settimeout(None)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return conn

    def close(self, unlink: bool = True) -> None:
        del unlink
        with contextlib.suppress(OSError):
            self._sock.close()


def listen(home: Path, kind: str | None = None):
    """The listener for this platform (or `kind`); a Windows pipe that cannot be made falls back to TCP."""
    kind = kind or endpoint_kind()
    if kind == "tcp":
        return TcpListener(home)
    if kind == "pipe":
        try:
            return PipeListener(home)
        except OSError:
            return TcpListener(home)
    try:
        return UnixListener(home)
    except OSError:
        return TcpListener(home)


# --- the client side ---------------------------------------------------------------------------------------------------

def connect(record: dict, timeout: float = 2.0):
    """A stream to the service service.json describes, or Unreachable."""
    endpoint = record.get("endpoint") or {}
    kind, address = endpoint.get("kind"), endpoint.get("address")
    try:
        if kind == "unix" and not IS_WINDOWS:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(address))
            sock.settimeout(None)
            return sock
        if kind == "tcp":
            sock = socket.create_connection(("127.0.0.1", int(endpoint.get("port") or 0)), timeout=timeout)
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock
        if kind == "pipe" and IS_WINDOWS:
            return PipeConn.open(str(address), timeout, expected_pid=record.get("pid"))
    except (OSError, ValueError) as exc:
        raise Unreachable(f"{kind} {address}: {exc.__class__.__name__}: {exc}") from None
    raise Unreachable(f"no endpoint of a kind this system can reach ({kind})")


def launch_record(home: Path) -> dict:
    """<home>/bin/launch.json (C1), or {} when there is none."""
    try:
        data = json.loads((Path(home) / "bin" / "launch.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _runtime_python(home: Path) -> str | None:
    """The base interpreter of the runtime the home names as current."""
    try:
        rid = (Path(home) / "runtime" / "current.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    base = Path(home) / "runtime" / rid / "python"
    python = base / ("python.exe" if IS_WINDOWS else "bin/python3")
    return str(python) if rid and python.is_file() else None


def installed_client(home: Path) -> tuple[str, str] | None:
    """(version, folder) of the client the home names as current (client/current.txt), when it is complete."""
    try:
        version = (Path(home) / "client" / "current.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    folder = Path(home) / "client" / version
    return (version, str(folder)) if version and (folder / "__main__.py").is_file() else None


def service_command(home: Path) -> tuple[list[str], str | None]:
    """(how to start the service, the client version it runs). The newest client the home names: launch.json's
    runtime and client when they agree with client/current.txt; else the current client on the current runtime; else
    this process's own interpreter and client (a source checkout, a relay or front that runs before anything was
    installed)."""
    from .. import __version__
    launch = launch_record(home)
    current = installed_client(home)
    python, client, version = launch.get("runtime_python"), launch.get("client_dir"), launch.get("client_version")
    usable = bool(python and client and Path(str(python)).is_file() and (Path(str(client)) / "__main__.py").is_file())
    if not usable or (current is not None and version != current[0]):
        python, client, version = None, None, None
        runtime = _runtime_python(home)
        if current is not None and runtime is not None:
            python, client, version = runtime, current[1], current[0]
    if python is None or os.environ.get("THEODORE_SERVICE_OWN_CLIENT"):
        python, client, version = sys.executable, str(Path(__file__).resolve().parents[2]), __version__
    return [str(python), "-I", str(client), "service", "--home", str(home)], version


def spawn_detached(command: list[str], home: Path, env: dict | None = None) -> int:
    """Start the service outside this process's session, process group and (Windows) job: a host that ends the relay
    or the shell command never ends the service mid-write. Returns its pid."""
    environment = dict(env if env is not None else os.environ)
    log_dir = Path(home) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stderr = open(log_dir / "service-start.log", "ab")
    try:
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": stderr, "env": environment,
                  "cwd": str(home), "close_fds": True}
        if IS_WINDOWS:
            flags = 0x00000008 | 0x00000200 | 0x08000000      # DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW
            try:
                child = subprocess.Popen(command, creationflags=flags | 0x01000000, **kwargs)    # + CREATE_BREAKAWAY_FROM_JOB
            except OSError:              # a job that forbids breaking away: detached all the same
                child = subprocess.Popen(command, creationflags=flags, **kwargs)
            return child.pid
        child = subprocess.Popen(command, start_new_session=True, **kwargs)
        threading.Thread(target=child.wait, name="service-reaper", daemon=True).start()
        return child.pid
    finally:
        stderr.close()


START_FAILED = "start-failed"
START_FAILED_S = 600.0


def _start_failed_recently(home: Path) -> bool:
    """A start that gave no service is remembered for 10 minutes (state/run/start-failed, as the native front does):
    commands go one-shot meanwhile instead of each paying a start that does not come."""
    try:
        return time.time() - (Path(home) / "state" / "run" / START_FAILED).stat().st_mtime < START_FAILED_S
    except OSError:
        return False


def ensure(home: Path, *, start: bool = True, wait: float | None = None) -> tuple[dict, object]:
    """(service.json, a connected stream): the running service, or one started under the start lock (C1 "Start").
    Raises Unreachable when none answers and none could be started within the wait."""
    home = Path(home)
    record = read_service_json(home)
    if record is not None and pid_alive(record.get("pid")):
        try:
            return record, connect(record)
        except Unreachable:
            pass
    if not start:
        raise Unreachable("no service is running")
    if _start_failed_recently(home):
        raise Unreachable("a start of the service failed less than 10 minutes ago")
    with StartLock(home):
        record = read_service_json(home)
        if record is not None and pid_alive(record.get("pid")):
            try:
                return record, connect(record)
            except Unreachable:
                pass
        stale = record
        spawn_detached(service_command(home)[0], home)
        deadline = time.monotonic() + (START_WAIT_S if wait is None else wait)
        while time.monotonic() < deadline:
            record = read_service_json(home)
            if record is not None and record != stale and pid_alive(record.get("pid")):
                with contextlib.suppress(Unreachable):
                    found = record, connect(record)
                    with contextlib.suppress(OSError):
                        (home / "state" / "run" / START_FAILED).unlink()
                    return found
            time.sleep(START_POLL_S)
        with contextlib.suppress(OSError):
            (run_dir(home) / START_FAILED).write_text(str(int(time.time())))
    raise Unreachable("the service did not start in time")


# --- Windows: SID, mutex, named pipes ----------------------------------------------------------------------------------

def _windows_user_sid() -> str | None:
    try:
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        token = wintypes.HANDLE()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):     # TOKEN_QUERY
            return None
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))      # TokenUser
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]          # TOKEN_USER.User.Sid
            text = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
                return None
            try:
                return text.value
            finally:
                kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            kernel32.CloseHandle(token)
    except Exception:       # noqa: BLE001  the user name stands in
        return None


def _windows_mutex(name: str, timeout: float):
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
    if result in (0, 0x80):                 # WAIT_OBJECT_0, WAIT_ABANDONED: ours
        return handle
    kernel32.CloseHandle(handle)
    raise Unreachable("another process holds the start mutex")


def _windows_release_mutex(handle) -> None:
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    with contextlib.suppress(Exception):
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


PIPE_PREFIX = "\\\\.\\pipe\\theodore-"
_ERROR_PIPE_BUSY = 231
_ERROR_PIPE_CONNECTED = 535
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_IO_PENDING = 997
_ERROR_ACCESS_DENIED = 5
_ERROR_OPERATION_ABORTED = 995


def _pipe_security():
    """SECURITY_ATTRIBUTES with a protected DACL that grants the current user, and only the current user, everything."""
    import ctypes
    from ctypes import wintypes

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    sddl = f"D:P(A;;GA;;;{user_id()})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError(ctypes.get_last_error(), "ConvertStringSecurityDescriptorToSecurityDescriptorW")
    attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
    return attributes, descriptor


class PipeConn:
    """One end of a byte-mode named pipe opened for overlapped I/O, read on one thread and written on others (a
    pipe opened for synchronous I/O would serialise a blocked read and a write on the same handle)."""

    def __init__(self, handle: int, server: bool):
        self._handle = handle
        self._server = server
        self._closed = False
        self._reading = None

    @classmethod
    def open(cls, name: str, timeout: float, expected_pid=None) -> "PipeConn":
        import _winapi
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle = _winapi.CreateFile(name, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, _winapi.NULL,
                                            _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) != _ERROR_PIPE_BUSY or time.monotonic() >= deadline:
                    raise
                with contextlib.suppress(OSError):
                    _winapi.WaitNamedPipe(name, max(1, int((deadline - time.monotonic()) * 1000)))
        conn = cls(handle, server=False)
        if expected_pid and _pipe_server_pid(handle) not in (None, int(expected_pid)):
            conn.close()
            raise OSError(_ERROR_ACCESS_DENIED, "the pipe's server is not the service service.json names")
        return conn

    def recv(self, size: int) -> bytes:
        import _winapi
        if self._closed:
            return b""
        try:
            overlapped, err = _winapi.ReadFile(self._handle, size, overlapped=True)
            self._reading = overlapped
            try:
                _nread, err = overlapped.GetOverlappedResult(True)
            finally:
                self._reading = None
            if err not in (0,):
                return b""
            return overlapped.getbuffer()
        except OSError as exc:
            if getattr(exc, "winerror", None) in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_OPERATION_ABORTED):
                return b""
            raise

    def sendall(self, data: bytes) -> None:
        import _winapi
        view = memoryview(data)
        while view:
            if self._closed:
                raise BrokenPipeError("pipe closed")
            overlapped, err = _winapi.WriteFile(self._handle, bytes(view[:1 << 20]), overlapped=True)
            written, err = overlapped.GetOverlappedResult(True)
            if err != 0 or written <= 0:
                raise BrokenPipeError("pipe write failed")
            view = view[written:]

    def shutdown(self, _how=None) -> None:
        reading = self._reading
        if reading is not None:
            with contextlib.suppress(Exception):
                reading.cancel()

    def close(self) -> None:
        import _winapi
        if self._closed:
            return
        self._closed = True
        self.shutdown()
        with contextlib.suppress(OSError):
            if self._server:
                import ctypes
                ctypes.windll.kernel32.FlushFileBuffers(self._handle)
                ctypes.windll.kernel32.DisconnectNamedPipe(self._handle)
            _winapi.CloseHandle(self._handle)


def _pipe_server_pid(handle: int):
    try:
        import ctypes
        from ctypes import wintypes
        pid = wintypes.ULONG()
        if ctypes.windll.kernel32.GetNamedPipeServerProcessId(wintypes.HANDLE(handle), ctypes.byref(pid)):
            return int(pid.value)
    except Exception:       # noqa: BLE001
        return None
    return None


class PipeListener:
    kind = "pipe"
    hello_timeout = HELLO_TIMEOUT_S

    def __init__(self, home: Path):
        import ctypes
        self.port = None
        base = PIPE_PREFIX + id16(home)
        self._security, self._descriptor = _pipe_security()
        self._first = True
        self._waiting = None
        self._closed = False
        try:
            self.address = base
            self._next = self._create(first=True)
        except OSError as exc:
            if (getattr(exc, "winerror", None) or exc.errno) not in (_ERROR_ACCESS_DENIED, _ERROR_PIPE_BUSY):
                raise
            # The name is held: by a predecessor still draining its calls (hand-over), or by someone else. A name of
            # our own (the pid makes it unique) is never another user's (requests-P A-3).
            self.address = f"{base}-{os.getpid()}"
            self._next = self._create(first=True)
        del ctypes

    def _create(self, first: bool) -> int:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        open_mode = 0x00000003 | 0x40000000 | (0x00080000 if first else 0)     # DUPLEX, OVERLAPPED, FIRST_PIPE_INSTANCE
        pipe_mode = 0x00000000 | 0x00000000 | 0x00000000 | 0x00000008          # BYTE, READMODE_BYTE, WAIT, REJECT_REMOTE_CLIENTS
        handle = kernel32.CreateNamedPipeW(self.address, open_mode, pipe_mode, 255, 1 << 16, 1 << 16, 0,
                                           ctypes.byref(self._security))
        if handle in (None, 0, ctypes.c_void_p(-1).value):
            error = ctypes.get_last_error()
            raise OSError(error, f"CreateNamedPipeW failed ({error})")
        return int(handle)

    def accept(self):
        """A connected pipe instance, or None after the poll interval. The pending connect of the waiting instance
        is kept across calls (a second ConnectNamedPipe on it would fail)."""
        import _winapi
        if self._closed:
            return None
        handle = self._next
        if self._waiting is None:
            try:
                self._waiting = _winapi.ConnectNamedPipe(handle, overlapped=True)   # ERROR_PIPE_CONNECTED sets its event
            except OSError as exc:
                if getattr(exc, "winerror", None) != _ERROR_NO_DATA:
                    raise
                # a client connected, wrote and left before the connect: this instance is spent
                with contextlib.suppress(OSError):
                    _winapi.CloseHandle(handle)
                self._next = self._create(first=False)
                return None
        overlapped = self._waiting
        if _winapi.WaitForMultipleObjects([overlapped.event], False, 500) == 0x102:      # WAIT_TIMEOUT: poll again
            return None
        self._waiting = None
        try:
            overlapped.GetOverlappedResult(True)
        except OSError:
            with contextlib.suppress(OSError):
                _winapi.CloseHandle(handle)
            self._next = self._create(first=False)
            return None
        self._next = self._create(first=False)
        return PipeConn(handle, server=True)

    def close(self, unlink: bool = True) -> None:
        import _winapi
        del unlink
        self._closed = True
        waiting, self._waiting = self._waiting, None
        if waiting is not None:
            with contextlib.suppress(Exception):
                waiting.cancel()
        with contextlib.suppress(OSError):
            _winapi.CloseHandle(self._next)
