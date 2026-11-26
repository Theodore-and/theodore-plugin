"""The service, and every process it starts, never wait on the system's crash reporter (the engine's
``pptx_runtime/crash_ports.py`` does the same for itself; lane P, 25 Nov 2026).

macOS keeps a crashed process in exit (state ``UE``, deaf to SIGKILL) until the crash reporter has taken its report; on
25 Nov the user's crash reporter spent 23 hours on another app's crash loop and every crashed process of ours stayed
until the computer restarted. Exception ports are inherited, so the service sets its crash ports (``EXC_CRASH``,
``EXC_CORPSE_NOTIFY``) to none at start and a worker, or an office a worker starts, that crashes is gone at once.
Windows: the error mode ``SEM_NOGPFAULTERRORBOX`` keeps Windows Error Reporting (which freezes a crashed process
while it writes a dump, and may show a dialog) from being invoked; child processes inherit it.
"""
from __future__ import annotations

import os
import sys

CRASH_MASK = (1 << 10) | (1 << 13)  # EXC_CRASH, EXC_CORPSE_NOTIFY
WINDOWS_ERROR_MODE = 0x0001 | 0x0002 | 0x8000  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX


def quiet() -> bool:
    """True when set; never raises."""
    try:
        import ctypes

        if sys.platform == "darwin":
            import platform

            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            task = ctypes.c_uint32.in_dll(lib, "mach_task_self_").value
            lib.task_set_exception_ports.argtypes = [ctypes.c_uint32] * 3 + [ctypes.c_int] * 2
            flavor = 5 if platform.machine() == "arm64" else 13  # THREAD_STATE_NONE
            return lib.task_set_exception_ports(task, CRASH_MASK, 0, 0x80000001, flavor) == 0
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetErrorMode.restype = ctypes.c_uint
            kernel32.SetErrorMode.argtypes = [ctypes.c_uint]
            kernel32.SetErrorMode(kernel32.GetErrorMode() | WINDOWS_ERROR_MODE)
            return True
    except Exception:  # noqa: BLE001 - never stops the service
        return False
    return False
