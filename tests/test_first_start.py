"""What only a first start pays is paid at install, not by the first command ever (lane firstrun, 26 Nov 2026).

The runtime's Python files come as source, and the interpreter compiles each at its first import: the first worker
after an install on rig 9 (2 processors) was ready after 10.8 s, later ones after 2.9 to 5.5 s. When an engine is
installed the client now starts it once as the service starts its workers, with nobody to serve (install.first_start),
and compiles its own files (install.compile_client). These tests: the start is the service's own (isolated, the
runtime's interpreter, low priority), what it compiled is kept with the installed engine, a second start compiles
nothing new, it never fails or outlasts an install, and an engine with no worker is left alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ENGINE_DEPENDENCY, FAKE_PPTX, IS_WINDOWS, ClientCase, sandbox_processes, zip_bytes  # noqa: E402

from theodore_client import core, install  # noqa: E402

# A stand-in worker (C2: frames on its standard output, a 4-byte length then JSON). It records how it was started,
# loads a module that sits beside it, says what FIRST_START_SAYS names, and ends when its input closes.
FAKE_WORKER = r'''
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))     # as the engine's entry does: -I names no folder
import first_start_probe                    # compiled by this start, kept beside its source
record = os.environ.get("FIRST_START_RECORD")
if record:
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "argv": sys.argv[1:], "isolated": bool(sys.flags.isolated), "no_bytecode": bool(sys.flags.dont_write_bytecode),
            "nice": os.getpriority(os.PRIO_PROCESS, 0) if hasattr(os, "getpriority") else None,
            "resident": os.environ.get("THEODORE_RESIDENT"), "started_by": os.environ.get("THEODORE_TEST_STARTED_BY"),
            "cwd": os.getcwd(), "input_closed": sys.stdin.buffer.read(1) == b"", "pid": os.getpid()}) + "\n")
says = os.environ.get("FIRST_START_SAYS") or "ready"
if says == "hang":
    import time
    time.sleep(600)
if says != "nothing":
    frame = {"t": "ready", "v": 1, "protocol": 1, "warm_ms": 7, "pid": os.getpid()} if says == "ready" else \
        {"t": "fatal", "code": "LICENCE", "message": "a stand-in refusal", "exit": 77}
    body = json.dumps(frame).encode("utf-8")
    sys.stdout.buffer.write(len(body).to_bytes(4, "little") + body)
    sys.stdout.buffer.flush()
sys.exit(0 if says == "ready" else 77)
'''
PROBE = "VALUE = 1\n"


def worker_engine_zip(version: str) -> bytes:
    return zip_bytes({
        "VERSION": version.encode(),
        "package.json": json.dumps({"name": "fake-engine", "dependencies": {ENGINE_DEPENDENCY: "*"}}).encode(),
        "requirements.txt": b"# none\n",
        "skills/pptx/SKILL.md": f"---\nname: pptx\n---\n\n# PPTX Skill\n\nFake manual for engine {version}.\n".encode(),
        "skills/pptx/pptxgenjs.md": b"# build API (fake)\n",
        "skills/pptx/scripts/pptx.py": FAKE_PPTX.encode("utf-8"),
        "skills/pptx/scripts/engine.py": FAKE_WORKER.encode("utf-8"),
        "skills/pptx/scripts/first_start_probe.py": PROBE.encode("utf-8"),
    })


def compiled_files(folder: Path) -> dict[str, tuple[int, int]]:
    return {str(path.relative_to(folder)): (path.stat().st_mtime_ns, path.stat().st_size) for path in folder.rglob("*.pyc")}


class AtInstall(ClientCase):
    """Through the client's own set-up: download, unpack, self-test, the first start, then the engine in its place."""

    ENGINE_VERSION = "1.0.0-rc.91"

    def test_an_installed_engine_was_started_once_as_a_worker_and_what_that_compiled_is_kept(self):
        self.api.publish_engine(self.ENGINE_VERSION, worker_engine_zip(self.ENGINE_VERSION))
        record = self.box.tmp / "first-start.jsonl"
        # The user's own variable must not reach the worker: the client starts it isolated, as the service does.
        self.box.env.update({"FIRST_START_RECORD": str(record), "PYTHONDONTWRITEBYTECODE": "1"})
        self.ready()
        starts = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(starts), 1, "once an install")
        start = starts[0]
        self.assertEqual(start["argv"], ["serve", "--protocol", "1"])
        self.assertTrue(start["isolated"] and not start["no_bytecode"], start)
        self.assertEqual(start["started_by"], "runtime-python", "the runtime's own interpreter, as the service's workers")
        self.assertEqual(start["resident"], "1")
        self.assertTrue(start["input_closed"], "nobody to serve: its input is closed")
        if start["nice"] is not None:
            self.assertGreaterEqual(start["nice"], 10, "at low priority")
        engine = self.box.home / "engines" / self.ENGINE_VERSION
        self.assertTrue((engine / ".theodore-engine.json").is_file(), "the engine is installed")
        kept = [name for name in compiled_files(engine) if "first_start_probe" in name]
        self.assertEqual(len(kept), 1, "what the start compiled sits beside its source in the installed engine")
        log = (self.box.home / "logs" / "client.log").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(log, rf"engine {self.ENGINE_VERSION}: started once as a worker at its install, \d+ ms: ready "
                              rf"\(its own start 7 ms\)")
        self.assertEqual([line for _pid, line in sandbox_processes(self.box.tmp.name) if "engine.py serve" in line], [],
                         "it ended by itself")

    def test_an_engine_with_no_worker_is_installed_as_before(self):
        self.ready()                    # the usual stand-in: pptx.py only
        log = (self.box.home / "logs" / "client.log").read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("started once as a worker", log)
        self.assertTrue((self.box.home / "engines" / "1.0.0-rc.91" / ".theodore-engine.json").is_file())

    def says(self, what: str, logged: str) -> None:
        self.api.publish_engine(self.ENGINE_VERSION, worker_engine_zip(self.ENGINE_VERSION))
        self.box.env["FIRST_START_SAYS"] = what
        self.ready()
        self.assertTrue((self.box.home / "engines" / self.ENGINE_VERSION / ".theodore-engine.json").is_file(),
                        "the engine is installed all the same")
        log = (self.box.home / "logs" / "client.log").read_text(encoding="utf-8", errors="replace")
        self.assertRegex(log, rf"engine {self.ENGINE_VERSION}: started once as a worker at its install, \d+ ms: {logged}")
        self.assertNotIn("rejected", log)

    def test_an_engine_whose_worker_refuses_is_installed_all_the_same(self):
        self.says("fatal", "fatal LICENCE")

    def test_an_engine_whose_worker_says_nothing_is_installed_all_the_same(self):
        self.says("nothing", "no answer")


class FirstStartUnit(unittest.TestCase):
    """install.first_start on a stand-in engine in a home of its own."""

    def setUp(self):
        if IS_WINDOWS:
            raise unittest.SkipTest("the stand-in runtime is a shell wrapper; Windows runs the rig's check (lanes/firstrun-rig.md)")
        self.root = Path(tempfile.mkdtemp(prefix="theodore-first-start-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        python = self.home / "runtime" / "rt-test" / "python" / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        python.chmod(0o755)
        (self.home / "runtime" / "current.txt").write_text("rt-test")
        self.engine = self.home / "engines" / "9.9.9"
        scripts = self.engine / "skills" / "pptx" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "engine.py").write_text(FAKE_WORKER)
        (scripts / "first_start_probe.py").write_text(PROBE)
        self.record = self.root / "starts.jsonl"
        for patch in (mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.home), "FIRST_START_RECORD": str(self.record),
                                                   "PYTHONDONTWRITEBYTECODE": "1"}),
                      mock.patch.object(core, "_HOME", None), mock.patch.object(core, "_LOG_FILE", None)):
            patch.start()
            self.addCleanup(patch.stop)

    def log(self) -> str:
        path = self.home / "logs" / "client.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def test_a_second_start_compiles_nothing_new(self):
        first = install.first_start(self.engine, "rt-test")
        self.assertEqual((first["said"], first["exit"]), ("ready", 0), first)
        compiled = compiled_files(self.engine)
        self.assertEqual(len(compiled), 1, compiled)
        time.sleep(1.1)                 # a file written again would carry a later time
        second = install.first_start(self.engine, "rt-test")
        self.assertEqual(second["said"], "ready")
        self.assertEqual(compiled_files(self.engine), compiled)
        self.assertEqual(len(self.record.read_text().splitlines()), 2)

    def test_a_worker_that_never_ends_is_stopped_and_the_install_goes_on(self):
        real_popen = subprocess.Popen
        running_at: list[float] = []

        def popen_once_it_has_started(*args, **kwargs):
            # The window under test is the hang's, and it opens when the worker is running: on a loaded machine the
            # interpreter's own start takes seconds, and a worker killed before it wrote its record proves nothing.
            process = real_popen(*args, **kwargs)
            deadline = time.monotonic() + 30
            while not self.record.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            running_at.append(time.monotonic())
            return process

        with mock.patch.dict(os.environ, {"FIRST_START_SAYS": "hang"}), mock.patch.object(install, "FIRST_START_WAIT_S", 1.5), \
                mock.patch.object(subprocess, "Popen", popen_once_it_has_started):
            result = install.first_start(self.engine, "rt-test", label="9.9.9")
        self.assertLess(time.monotonic() - running_at[0], 20)  # stopped by its window, not left to its sleep
        self.assertTrue(result.get("stopped"), result)
        self.assertEqual(result["said"], "nothing")
        pid = json.loads(self.record.read_text().splitlines()[0])["pid"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            self.fail("the worker outlived its first start")
        self.assertRegex(self.log(), r"engine 9\.9\.9: started once as a worker at its install, \d+ ms: no answer; stopped after")

    def test_nothing_is_started_and_nothing_logged_without_a_worker_script_or_a_runtime(self):
        (self.engine / "skills" / "pptx" / "scripts" / "engine.py").unlink()
        self.assertEqual(install.first_start(self.engine, "rt-test")["said"], "not started")
        (self.engine / "skills" / "pptx" / "scripts" / "engine.py").write_text(FAKE_WORKER)
        self.assertEqual(install.first_start(self.engine, "rt-absent")["said"], "not started")
        self.assertFalse(self.record.exists())
        self.assertNotIn("started once", self.log())

    def test_a_start_that_cannot_run_is_logged_and_never_raised(self):
        with mock.patch.object(install.subprocess, "Popen", side_effect=OSError("no such interpreter")):
            result = install.first_start(self.engine, "rt-test", label="9.9.9")
        self.assertIn("OSError", result.get("error", ""))
        self.assertIn("engine 9.9.9: started once as a worker at its install", self.log())

    def test_frames_are_read_as_c2_writes_them(self):
        def frame(value: dict) -> bytes:
            body = json.dumps(value).encode("utf-8")
            return len(body).to_bytes(4, "little") + body
        raw = frame({"t": "log", "message": "x"}) + frame({"t": "ready", "warm_ms": 3}) + b"\x09\x00\x00\x00{cut"
        self.assertEqual([f["t"] for f in install._worker_frames(raw)], ["log", "ready"])
        self.assertEqual(install._worker_frames(b""), [])
        self.assertEqual(install._worker_frames(b"\x02\x00\x00\x00[]"), [], "only objects are frames")


class ClientFiles(unittest.TestCase):
    """install.compile_client: the client's own files compiled once, so the first command ever compiles none."""

    def test_each_file_is_compiled_once_and_the_interpreter_takes_it_as_its_own(self):
        root = Path(tempfile.mkdtemp(prefix="theodore-client-files-"))
        self.addCleanup(shutil.rmtree, root, True)
        package = root / "stand_in_client"
        (package / "service").mkdir(parents=True)
        (package / "__init__.py").write_text("NAME = 'stand-in'\n")
        (package / "service" / "__init__.py").write_text("")
        (package / "service" / "pool.py").write_text("SIZE = 2\n")
        (package / "broken.py").write_text("def (:\n")          # never an install's failure
        self.assertEqual(install.compile_client(package), 3)
        compiled = compiled_files(package)
        self.assertEqual(len(compiled), 3)
        self.assertEqual(install.compile_client(package), 0, "a second pass compiles nothing")
        time.sleep(1.1)
        done = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                               "import stand_in_client.service.pool as p; print(p.SIZE)", str(root)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.stdout.strip(), "2", done.stderr)
        self.assertEqual(compiled_files(package), compiled, "the interpreter read them and wrote none")

    def test_the_real_clients_files_are_all_reached(self):
        sources = sorted(Path(install.__file__).resolve().parent.rglob("*.py"))
        self.assertGreater(len(sources), 30)
        self.assertTrue(any(path.parent.name == "service" for path in sources), "the service's modules are among them")
        self.assertEqual(install.compile_client(Path(self.id())), 0, "a folder that is not there: nothing, no failure")


if __name__ == "__main__":
    unittest.main()
