"""A stand-in for Theodore's service (contract C1) and for the client's one-shot commands, for the front's tests.

The front's tests point `<home>/bin/launch.json` at this interpreter and at a client folder whose `__main__.py` calls
main() here, so the front starts this as it would start the real service:

  service --home H        the C1 service: listens (a Unix socket; on Windows a named pipe, as C1 says; loopback TCP
                          with FAKE_TCP=1), writes
                          state/run/service.json, answers hello and calls; every event goes to state/run/events.jsonl
  mcp [--for X]           a stand-in for the client's own MCP server (the relay's fallback)
  anything else           a one-shot command: prints {"oneshot": argv, ...} and exits 0 (or the code `exitN` asks)

The calls the service knows (tool pptx, args[0]): echo, sleep MS, fail CODE, progress N, image, busy, drop (the
connection ends before the answer the first time), big N. Tool theodore: echo, skill. Tool list with form tools: the
list FAKE_TOOLS names. Invented content only.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import struct
import sys
import threading
import time
from pathlib import Path

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def log(home: Path, event: dict) -> None:
    run = home / "state" / "run"
    run.mkdir(parents=True, exist_ok=True)
    with open(run / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), **event}) + "\n")


def read_frame(sock) -> dict | None:
    head = b""
    while len(head) < 4:
        chunk = sock.recv(4 - len(head))
        if not chunk:
            return None
        head += chunk
    (n,) = struct.unpack("<I", head)
    body = b""
    while len(body) < n:
        chunk = sock.recv(n - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


class WindowsPipe:
    """One connection of a named-pipe server, opened for overlapped I/O (multiprocessing.connection's way): a call's
    thread writes while the connection's thread waits in a read, as the real service's do."""

    def __init__(self, handle):
        self.handle = handle

    def _wait(self, ov, err):
        import _winapi
        try:
            if err == _winapi.ERROR_IO_PENDING:
                _winapi.WaitForMultipleObjects([ov.event], False, _winapi.INFINITE)
        except BaseException:
            ov.cancel()
            raise
        return ov.GetOverlappedResult(True)

    def recv(self, n: int) -> bytes:
        import _winapi
        try:
            ov, err = _winapi.ReadFile(self.handle, n, overlapped=True)
            done, err = self._wait(ov, err)
        except OSError:
            return b""                      # the client went away
        return bytes(ov.getbuffer()[:done]) if err in (0, _winapi.ERROR_MORE_DATA) else b""

    def sendall(self, data: bytes) -> None:
        import _winapi
        ov, err = _winapi.WriteFile(self.handle, data, overlapped=True)
        done, err = self._wait(ov, err)
        if err or done != len(data):
            raise OSError(f"pipe write: {err}")

    def shutdown(self, _how=None) -> None:
        self.close()

    def close(self) -> None:
        import _winapi
        if self.handle is not None:
            handle, self.handle = self.handle, None
            try:
                _winapi.CloseHandle(handle)
            except OSError:
                pass


class WindowsPipeServer:
    PIPE_REJECT_REMOTE_CLIENTS = 0x8

    def __init__(self, name: str):
        self.name, self.first = name, True

    def accept(self, timeout: float):
        import _winapi
        mode = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED | (_winapi.FILE_FLAG_FIRST_PIPE_INSTANCE if self.first else 0)
        self.first = False
        handle = _winapi.CreateNamedPipe(self.name, mode, _winapi.PIPE_WAIT | self.PIPE_REJECT_REMOTE_CLIENTS,
                                         _winapi.PIPE_UNLIMITED_INSTANCES, 65536, 65536, _winapi.NMPWAIT_WAIT_FOREVER, _winapi.NULL)
        ov = _winapi.ConnectNamedPipe(handle, overlapped=True)
        waited = _winapi.WaitForMultipleObjects([ov.event], False, int(timeout * 1000))
        if waited != _winapi.WAIT_OBJECT_0:
            ov.cancel()
            _winapi.CloseHandle(handle)
            raise socket.timeout()
        ov.GetOverlappedResult(True)
        return WindowsPipe(handle)


class Connection:
    def __init__(self, service: "Service", sock):
        self.service, self.sock, self.lock = service, sock, threading.Lock()
        self.cancelled: set[str] = set()

    def send(self, frame: dict) -> None:
        body = json.dumps(frame, ensure_ascii=False).encode("utf-8")
        with self.lock:
            try:
                self.sock.sendall(struct.pack("<I", len(body)) + body)
            except OSError:
                pass

    def serve(self) -> None:
        home = self.service.home
        try:
            hello = read_frame(self.sock)
            if not hello or hello.get("t") != "hello" or hello.get("secret") != self.service.secret:
                log(home, {"e": "bad-hello", "hello": {k: v for k, v in (hello or {}).items() if k != "secret"}})
                self.send({"t": "error", "code": "BAD_SECRET", "message": "wrong secret"})
                return
            log(home, {"e": "hello", "role": hello.get("role"), "client": hello.get("client"), "session": hello.get("session"),
                       "protocol": hello.get("protocol"), "plugin_root": hello.get("plugin_root")})
            if os.environ.get("FAKE_REFUSE_PROTOCOL"):
                self.send({"t": "error", "code": "PROTOCOL", "message": "protocol 1 is not served", "protocol": 2})
                return
            self.send({"t": "welcome", "v": 1, "protocol": 1, "service": {"version": "0.0.0-fake", "pid": os.getpid(),
                       "started": "2026-11-24T00:00:00Z"}, "engines": [], "new_session": True})
            while True:
                frame = read_frame(self.sock)
                if frame is None:
                    return
                kind = frame.get("t")
                if kind == "call":
                    log(home, {"e": "call", "id": frame["id"], "tool": frame.get("tool"), "args": frame.get("args"),
                               "params": frame.get("params"), "cwd": frame.get("cwd"), "env": frame.get("env"),
                               "images": frame.get("images"), "deadline_ms": frame.get("deadline_ms"),
                               "resend": frame.get("resend", False), "stdin": frame.get("stdin")})
                    threading.Thread(target=self.call, args=(frame,), daemon=True).start()
                elif kind == "cancel":
                    log(home, {"e": "cancel", "id": frame.get("id")})
                    self.cancelled.add(frame.get("id"))
                elif kind == "bye":
                    log(home, {"e": "bye"})
                    return
        finally:
            try:
                self.sock.close()
            except OSError:
                pass

    def done(self, cid: str, code: int, started: float, **extra) -> None:
        self.send({"t": "done", "id": cid, "exit": code, "elapsed_ms": int((time.monotonic() - started) * 1000), **extra})

    def call(self, frame: dict) -> None:
        cid, tool, args, params = frame["id"], frame.get("tool"), frame.get("args") or [], frame.get("params") or {}
        started = time.monotonic()
        out = lambda text, stream="stdout": self.send({"t": "out", "id": cid, "stream": stream, "data": text})  # noqa: E731
        verb = args[0] if args else ""
        if tool == "list" and params.get("form") == "tools":
            names = [n for n in os.environ.get("FAKE_TOOLS", "").split(",") if n]
            if not names:
                self.done(cid, 1, started)
                return
            out(json.dumps({"tools": [{"name": n, "description": f"{n} from the service", "inputSchema": {"type": "object"}}
                                      for n in names]}))
            self.done(cid, 0, started)
            return
        if tool == "chat":
            out(json.dumps({"chat": params}))
            self.done(cid, 0, started)
            return
        if tool in ("guide", "specimen", "list"):
            out(json.dumps({"tool": tool, "params": params}))
            self.done(cid, 0, started)
            return
        if tool == "theodore" and verb == "skill":
            out("BODY of the skill\n")
            out("a warning\n", "stderr")
            self.done(cid, 1, started)
            return
        if verb == "echo" or tool == "theodore":
            out(json.dumps({"tool": tool, "args": args, "cwd": frame.get("cwd"), "env": frame.get("env"),
                            "stdin": frame.get("stdin"), "deadline_ms": frame.get("deadline_ms")}, ensure_ascii=False))
            self.done(cid, 0, started)
        elif verb == "sleep":
            end = time.monotonic() + int(args[1]) / 1000
            while time.monotonic() < end:
                if cid in self.cancelled:
                    self.done(cid, 130, started)
                    return
                time.sleep(0.01)
            out(json.dumps({"slept": args[1], "id": cid, "tag": args[2] if len(args) > 2 else None}))
            self.done(cid, 0, started)
        elif verb == "fail":
            out("it failed\n", "stderr")
            out(json.dumps({"ok": False}))
            self.done(cid, int(args[1]), started)
        elif verb == "progress":
            for i in range(int(args[1])):
                self.send({"t": "progress", "id": cid, "elapsed_ms": 1000 * (i + 1), "phase": f"step {i + 1}"})
            out("{}")
            self.done(cid, 0, started)
        elif verb == "image":
            path = self.service.home / "state" / "run" / "picture.png"
            path.write_bytes(PNG)
            self.send({"t": "image", "id": cid, "index": 0, "mime": "image/png", "slide": "s001", "page": 1, "path": str(path),
                       **({"data": base64.b64encode(PNG).decode()} if frame.get("images") else {})})
            self.send({"t": "image", "id": cid, "index": 1, "mime": "image/png", "path": str(path)})
            out("{}")
            self.done(cid, 0, started, pending=[{"kind": "picture", "slide": "s002", "tier": "exact"}])
        elif verb == "busy":
            key = "busy:" + json.dumps(args)
            if key not in self.service.seen:
                self.service.seen.add(key)
                self.send({"t": "error", "id": cid, "code": "BUSY", "message": "busy", "retry_after_ms": 30})
                return
            out("{}")
            self.done(cid, 0, started)
        elif verb == "drop":
            key = "drop:" + json.dumps(args)
            if key not in self.service.seen:
                self.service.seen.add(key)
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
                return
            out(json.dumps({"resent": frame.get("resend", False)}))
            self.done(cid, 0, started)
        elif verb == "big":
            chunk = "x" * 65536
            total = int(args[1])
            while total > 0:
                out(chunk[: min(total, len(chunk))])
                total -= len(chunk)
            self.done(cid, 0, started)
        else:
            out(json.dumps({"unknown": args}))
            self.done(cid, 2, started)


class Service:
    def __init__(self, home: Path):
        self.home, self.secret, self.seen = home, secrets.token_hex(32), set()

    def run(self) -> int:
        run = self.home / "state" / "run"
        run.mkdir(parents=True, exist_ok=True)
        if os.environ.get("FAKE_SERVICE_FAIL"):
            log(self.home, {"e": "service-failed"})
            return 3
        delay = float(os.environ.get("FAKE_SERVICE_DELAY", "0") or 0)
        if delay:
            time.sleep(delay)
        pipe = None
        if os.environ.get("FAKE_TCP"):
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(("127.0.0.1", 0))
            endpoint = {"kind": "tcp", "address": "127.0.0.1", "port": server.getsockname()[1]}
        elif os.name == "nt":
            name = rf"\\.\pipe\theodore-fake-{os.getpid()}-{secrets.token_hex(4)}"
            pipe = WindowsPipeServer(name)
            endpoint = {"kind": "pipe", "address": name}
        else:
            path = run / "s.sock"
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            endpoint = {"kind": "unix", "address": str(path)}
        if pipe is None:
            server.listen(64)
        doc = {"v": 1, "pid": os.getpid(), "started": "2026-11-24T00:00:00Z", "protocol": 1, "version": "0.0.0-fake",
               "endpoint": endpoint, "secret": self.secret, "home": str(self.home)}
        tmp = run / f"service.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, run / "service.json")
        log(self.home, {"e": "service-started", "ppid": os.getppid(), "sid": os.getsid(0) if hasattr(os, "getsid") else None})
        idle = float(os.environ.get("FAKE_SERVICE_IDLE", "20"))
        if pipe is None:
            server.settimeout(idle)
        try:
            while True:
                try:
                    sock = pipe.accept(idle) if pipe is not None else server.accept()[0]
                except socket.timeout:
                    break
                threading.Thread(target=Connection(self, sock).serve, daemon=True).start()
        finally:
            try:
                if json.loads((run / "service.json").read_text())["pid"] == os.getpid():
                    (run / "service.json").unlink()
            except (OSError, ValueError, KeyError):
                pass
        return 0


def fake_mcp(home: Path, argv: list[str]) -> int:
    """The client's own MCP server, as far as the relay's fallback needs it."""
    log(home, {"e": "client-mcp", "argv": argv})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method, mid = message.get("method"), message.get("id")
        log(home, {"e": "client-mcp-message", "method": method, "id": mid})
        if mid is None:
            continue
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-client", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "fake-tool", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "fake client: " + message["params"]["name"]}], "isError": False}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)
    return 0


def main(argv: list[str]) -> int:
    home = Path(os.environ.get("THEODORE_HOME") or ".")
    if argv[:1] == ["service"]:
        if "--home" in argv:
            home = Path(argv[argv.index("--home") + 1])
        return Service(home).run()
    if argv[:1] == ["mcp"]:
        return fake_mcp(home, argv)
    stdin = None
    for i, arg in enumerate(argv):
        if i and argv[i - 1] in ("--from", "--details") and os.path.isfile(arg):
            stdin = Path(arg).read_text(encoding="utf-8")
    log(home, {"e": "oneshot", "argv": argv})
    print(json.dumps({"oneshot": argv, "home": os.environ.get("THEODORE_HOME"), "skill_dir": os.environ.get("THEODORE_SKILL_DIR"),
                      "file_stdin": stdin}))
    code = next((int(a[4:]) for a in argv if a.startswith("exit") and a[4:].isdigit()), 0)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
