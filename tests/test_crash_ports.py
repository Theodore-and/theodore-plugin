"""The service never waits on the system's crash reporter, nor do the processes it starts (lane P, 25 Nov 2026).

macOS keeps a crashed process in exit (state UE, deaf to SIGKILL) until the crash reporter has taken its report; the
service sets its crash ports to none at start, and a child inherits them, so a crashed worker or office is gone at once.
Windows: the error mode keeps Windows Error Reporting from being invoked, and children inherit it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import CLIENT_DIR  # noqa: E402

PROBE = textwrap.dedent("""
    import ctypes, json, os, subprocess, sys, time
    sys.path.insert(0, sys.argv[1])
    from theodore_client import crash_ports
    assert crash_ports.quiet()
    if os.name == "nt":
        mode = ctypes.windll.kernel32.GetErrorMode()
        out = {"mode": mode}
    else:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        task = ctypes.c_uint32.in_dll(lib, "mach_task_self_").value
        n = 32
        masks, ports = (ctypes.c_uint32 * n)(), (ctypes.c_uint32 * n)()
        behaviors, flavors, count = (ctypes.c_int * n)(), (ctypes.c_int * n)(), ctypes.c_uint32(n)
        assert lib.task_get_exception_ports(task, crash_ports.CRASH_MASK, masks, ctypes.byref(count), ports,
                                            behaviors, flavors) == 0
        out = {"ports": [ports[i] for i in range(count.value)]}
    started = time.monotonic()
    crash = "import ctypes; ctypes.string_at(0)" if os.name == "nt" else "import os; os.abort()"
    child = subprocess.run([sys.executable, "-I", "-S", "-c", crash], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    out["child_code"], out["child_seconds"] = child.returncode, round(time.monotonic() - started, 3)
    print(json.dumps(out))
""")


@unittest.skipUnless(sys.platform == "darwin" or os.name == "nt", "macOS and Windows hold crashed processes")
class CrashPortsCase(unittest.TestCase):
    def test_set_and_inherited(self):
        run = subprocess.run([sys.executable, "-I", "-c", PROBE, str(CLIENT_DIR)], capture_output=True, text=True,
                             timeout=120)
        found = json.loads(run.stdout.strip().splitlines()[-1])
        if os.name == "nt":
            self.assertTrue(found["mode"] & 0x0002, found)          # SEM_NOGPFAULTERRORBOX
        else:
            self.assertEqual(set(found["ports"]), {0}, found)
        self.assertNotEqual(found["child_code"], 0, found)          # the child crashed ...
        self.assertLess(found["child_seconds"], 10, found)          # ... and was gone at once


if __name__ == "__main__":
    unittest.main()
