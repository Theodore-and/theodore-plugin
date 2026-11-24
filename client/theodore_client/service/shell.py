"""The shell route through the service (C1 role `front`, client `theodore-front-py`): `theodore pptx ...`, `theodore
guide ...`, `theodore specimen ...` and `theodore skill ...` typed in a shell are sent to the running service (started
when none runs), and their output is streamed back as it comes, with the command's exit code. Until lane C's native
front is signed, the client itself is the front (PLAN §3 C).

`run(argv)` returns None when the service cannot be reached or started: the command then runs as it always did, in
this process (the one-shot path). THEODORE_SERVICE=0 always takes that path.
"""

from __future__ import annotations

import os
import sys

from .. import core

# Nothing else at import: with THEODORE_SERVICE=0 or THEODORE_RESIDENT=0 an engine command takes the lean one-shot path
# (verbs.py), which loads no network, hashing or archive module (0.4.34); run() imports what reaching the service needs.

FRONT_COMMANDS = ("pptx", "guide", "specimen", "skill")
# The Bash tool's limit minus 30 s (Claude Code: BASH_MAX_TIMEOUT_MS, 10 minutes), as the native front reckons it.
DEADLINE_MS = int(os.environ.get("THEODORE_DEADLINE_MS") or 0) or \
    max(30_000, int(os.environ.get("BASH_MAX_TIMEOUT_MS") or 600_000) - 30_000)


def resident() -> bool:
    """False when THEODORE_RESIDENT=0: no service at all; the relay answers its calls itself, every engine command runs
    one-shot."""
    return (os.environ.get("THEODORE_RESIDENT") or "").strip() != "0"


def enabled() -> bool:
    """Whether shell commands go through the service: not with THEODORE_RESIDENT=0, nor with THEODORE_SERVICE=0 (the
    shell route alone runs as before, in its own process)."""
    return resident() and (os.environ.get("THEODORE_SERVICE") or "").strip() != "0"


def _same_stream(first, second) -> bool:
    """stdout and stderr are one pipe, file or console (`2>&1`): the same device and file, or on Windows the same
    object (CompareObjectHandles) or both on a console."""
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            one, two = msvcrt.get_osfhandle(first.fileno()), msvcrt.get_osfhandle(second.fileno())
            if first.isatty() and second.isatty():
                return True
            compare = getattr(ctypes.windll.kernelbase, "CompareObjectHandles", None)
            return bool(compare and compare(ctypes.c_void_p(one), ctypes.c_void_p(two)))
        a, b = os.fstat(first.fileno()), os.fstat(second.fileno())
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except (OSError, ValueError, AttributeError):
        return False


def stdio_flags() -> str:
    """THEODORE_STDIO for a call: `stdout-tty`, `stderr-tty`, `shared` (comma-separated, none of them possible)."""
    flags = []
    out, err = sys.__stdout__, sys.__stderr__
    for name, stream in (("stdout-tty", out), ("stderr-tty", err)):
        try:
            if stream is not None and stream.isatty():
                flags.append(name)
        except (OSError, ValueError):
            pass
    if out is not None and err is not None and _same_stream(out, err):
        flags.append("shared")
    return ",".join(flags)


def _stdin(args: list[str]) -> dict | None:
    """What the command reads on standard input ("-" among its arguments), sent with the call."""
    if "-" not in args or sys.stdin is None or sys.stdin.closed:
        return None
    try:
        if sys.stdin.isatty():
            return None
        data = sys.stdin.buffer.read()
    except (OSError, ValueError):
        return None
    try:
        return {"encoding": "utf-8", "data": data.decode("utf-8")}
    except UnicodeDecodeError:
        import base64
        return {"encoding": "base64", "data": base64.b64encode(data).decode("ascii")}


def run(argv: list[str]) -> int | None:
    if not enabled() or not argv or argv[0] not in FRONT_COMMANDS:
        return None
    import secrets
    import threading

    from . import client as service_client, context, endpoint
    done = threading.Event()
    result: dict = {}
    out = getattr(sys.stdout, "buffer", None)
    err = getattr(sys.stderr, "buffer", None)

    skill = argv[0] == "skill"              # a skill's injected command: everything on standard output, as the launcher

    def on_frame(frame: dict) -> None:
        kind = frame.get("t")
        if kind == "out":
            data = str(frame.get("data") or "").encode("utf-8")
            stream = err if frame.get("stream") == "stderr" and not skill else out
            if stream is not None:
                stream.write(data)
                stream.flush()
        elif kind == "done":
            result["exit"] = int(frame.get("exit") or 0)
            done.set()
        elif kind == "error":
            result["error"] = frame
            done.set()

    session = core.session_id() or f"shell-{os.getpid()}"
    client = service_client.ServiceClient(core.home().root, role="front", name="theodore-front-py", session_id=session,
                                          host="shell", on_frame=on_frame, log=core.log)
    try:
        client.connect(start=True)
    except endpoint.Unreachable as exc:
        core.log(f"front: no Theodore service ({exc}); running here")
        return None
    env = {k: v for k, v in os.environ.items() if context.ALLOWED.match(k) and k not in context.FIXED}
    env.setdefault("THEODORE_HOST", core.host_name())
    env["THEODORE_STDIO"] = stdio_flags()       # what the engine would see of its streams in this process (C1, B-2)
    tool = "pptx" if argv[0] == "pptx" else "theodore"
    args = argv[1:] if tool == "pptx" else argv
    frame = {"t": "call", "id": f"f{os.getpid()}-{secrets.token_hex(4)}", "tool": tool, "args": args, "cwd": os.getcwd(),
             "env": env, "images": False, "deadline_ms": max(1000, DEADLINE_MS)}
    stdin = _stdin(args)
    if stdin is not None:
        frame["stdin"] = stdin
    try:
        client.send_call(frame)
    except endpoint.Unreachable:
        return None
    try:
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        client.cancel(frame["id"])
        done.wait(10)
        client.close()
        return 130
    client.close()
    if "error" in result:
        error = result["error"]
        if error.get("code") in ("SHUTTING_DOWN",):
            return None
        sys.stdout.write(f"THEODORE INTERNAL: the Theodore service refused the command ({error.get('code')}: {error.get('message')}).\n"
                         "NEXT: run the same command again once.\nTELL THE USER: nothing; do not mention this to the user.\n")
        sys.stdout.flush()
        return 78
    return result.get("exit", 78)
