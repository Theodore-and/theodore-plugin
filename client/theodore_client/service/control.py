"""`theodore stop`, and the stop an uninstall makes first: the service ends with its workers, at once.

Through the endpoint when the service answers (a `theodore stop` call: it answers, then stops); otherwise, when
service.json names a live process that is this home's service, by a signal (SIGTERM; TerminateProcess on Windows).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import threading
import time

from .. import core
from . import client as service_client, endpoint


def _ask(home) -> bool:
    """Send `theodore stop` through the endpoint; True when the service answered it."""
    answered = threading.Event()

    def on_frame(frame: dict) -> None:
        if frame.get("t") in ("done", "error"):
            answered.set()

    client = service_client.ServiceClient(home, role="probe", name="theodore-probe", session_id=f"stop-{os.getpid()}",
                                          host="shell", on_frame=on_frame)
    try:
        client.connect(start=False)
        client.send_call({"t": "call", "id": f"stop-{secrets.token_hex(4)}", "tool": "theodore", "args": ["stop"],
                          "cwd": str(home)}, start=False)
    except endpoint.Unreachable:
        return False
    ok = answered.wait(5.0)
    client.close()
    return ok


def _terminate(pid: int) -> None:
    if core.IS_WINDOWS:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, int(pid))        # PROCESS_TERMINATE
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def stop_service(wait: float = 10.0) -> str:
    """Stop this home's service. Returns what happened: "stopped", "none" or "left" (still running)."""
    home = core.home().root
    record = endpoint.read_service_json(home)
    if record is None or not endpoint.pid_alive(record.get("pid")):
        return "none"
    pid = int(record["pid"])
    if not _ask(home):
        program, _started = core.process_facts(pid)
        if program and ("service" in program or "python" in program.lower()):
            _terminate(pid)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and endpoint.pid_alive(pid):
        time.sleep(0.05)
    if endpoint.pid_alive(pid):
        _terminate(pid)
        time.sleep(0.5)
    return "left" if endpoint.pid_alive(pid) else "stopped"


def stop() -> int:
    """`theodore stop`."""
    outcome = stop_service()
    if outcome == "left":
        return core.emit(core.Status("RETRY", "Theodore's service did not stop within 10 seconds.",
                                     "run the same command again.", "Theodore is finishing a step; I will stop it again."))
    said = "Theodore's service was stopped" if outcome == "stopped" else "no Theodore service was running"
    return core.emit(core.Status("OK", f"{said}; the next Theodore command starts it again.", "nothing.", None))
