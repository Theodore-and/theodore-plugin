"""Round 10 (0.2.2): one guide print per command, the reading record, and the build gate.

The client suites run the client directly (macOS and Linux); `ThroughTheLauncher` runs the real launcher, also on
Windows (cmd, PowerShell), where the runtime job sets THEODORE_TEST_BUNDLE."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import CLIENT_DIR, IS_WINDOWS, RUNTIME_ID, ClientCase, FakeApi, Sandbox, engine_zip, fake_runtime_parts, pack_zip, status_lines

sys.path.insert(0, str(CLIENT_DIR))
from theodore_client import reading  # noqa: E402

PLAN = ("craft,forms,worked-notes", "critique")
SECTIONS = {"": "# Deck design\n\nENTRY-MARKER\n", "craft": "CRAFT-MARKER\n" + "c" * 30000, "forms": "FORMS-MARKER\n",
            "worked-notes": "NOTES-MARKER\n", "critique": "CRITIQUE-MARKER\n", "evidence": "EVIDENCE-MARKER\n"}
HOURS_12 = 12 * 3600
CHAIN = {"THEODORE_TEST_CHAIN_WINDOW": str(reading.CHAIN_WINDOW_S)}       # the real window (the sandbox turns it off)


class ReadingCase(ClientCase):
    def setUp(self):
        super().setUp()
        self.api.guides["deck-design"].update(SECTIONS)
        self.api.guide_plan["deck-design"] = PLAN
        self.ws = self.box.tmp / "deck one"
        self.ws.mkdir()

    def record(self) -> dict:
        return self.state("state/reading.json")

    def guide(self, *args, env=None):
        result = self.client("guide", "deck-design", *args, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def read_all_required(self):
        self.guide()
        self.guide("craft")
        self.guide("craft", "--part", "2")
        self.guide("forms")
        self.guide("worked-notes")


class OneAtATime(ReadingCase):
    def _shell(self, line: str, env=None):
        return subprocess.run(["/bin/sh", "-c", line], env={**self.box.env, **CHAIN, **(env or {})}, capture_output=True, text=True,
                              timeout=180, stdin=subprocess.DEVNULL, cwd=str(self.box.tmp))

    def _client_line(self, *args: str) -> str:
        return " ".join(shlex.quote(a) for a in (sys.executable, "-I", str(CLIENT_DIR), *args))

    def test_three_guides_in_one_shell_command_print_one(self):
        self.sign_in()
        line = "; ".join([self._client_line("guide", "deck-design"), self._client_line("guide", "deck-design", "craft"),
                          self._client_line("guide", "deck-design", "forms")])
        done = self._shell(line)
        self.assertEqual(done.stdout.count("ENTRY-MARKER"), 1, done.stdout)
        self.assertNotIn("CRAFT-MARKER", done.stdout)
        self.assertNotIn("FORMS-MARKER", done.stdout)
        self.assertEqual(done.stdout.count("THEODORE ONE_AT_A_TIME: "), 2, done.stdout)
        self.assertEqual(done.returncode, 75, "the last command's code: ONE_AT_A_TIME shares RETRY's")
        first, nxt, tell = status_lines(done.stdout)
        self.assertIn("guide deck-design forms by itself as its own tool call", nxt)
        self.assertEqual(sorted(self.record()["read"]["deck-design"]), ["entry"], "only what was printed counts")
        self.assertEqual(self.api.guide_fetches, 1, "a refused guide does not even fetch")

    def test_chained_with_and_and_a_loop_are_refused_too(self):
        self.sign_in()
        done = self._shell(self._client_line("guide", "deck-design") + " && " + self._client_line("guide", "deck-design", "craft"))
        self.assertNotIn("CRAFT-MARKER", done.stdout)
        self.assertIn("THEODORE ONE_AT_A_TIME: ", done.stdout)
        loop = f"for s in forms worked-notes; do {self._client_line('guide', 'deck-design')} $s; done"
        time.sleep(reading.CHAIN_WINDOW_S + 0.2)
        done = self._shell(loop)
        self.assertIn("FORMS-MARKER", done.stdout)
        self.assertNotIn("NOTES-MARKER", done.stdout)

    def test_separate_tool_calls_are_allowed_after_the_window(self):
        self.sign_in()
        self.assertEqual(self._shell(self._client_line("guide", "deck-design")).returncode, 0)
        time.sleep(reading.CHAIN_WINDOW_S + 0.2)          # a host with one persistent shell: only the time tells them apart
        done = self._shell(self._client_line("guide", "deck-design", "forms"))
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("FORMS-MARKER", done.stdout)

    def test_parallel_tool_calls_overlap_and_are_allowed(self):
        self.sign_in()
        self.api.guide_delay = 0.8
        env = {**self.box.env, **CHAIN}
        runs = [subprocess.Popen([sys.executable, "-I", str(CLIENT_DIR), "guide", "deck-design", s], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL) for s in ("forms", "worked-notes", "critique")]
        outs = [r.communicate(timeout=120)[0] for r in runs]
        self.assertEqual([r.returncode for r in runs], [0, 0, 0], outs)
        for marker, out in zip(("FORMS-MARKER", "NOTES-MARKER", "CRITIQUE-MARKER"), outs):
            self.assertIn(marker, out)

    def test_another_caller_is_allowed(self):
        self.sign_in()
        env = {**self.box.env, **CHAIN}
        for section, caller in (("forms", "4242"), ("worked-notes", "4343")):     # another shell, another process group
            done = subprocess.run([sys.executable, "-I", str(CLIENT_DIR), "guide", "deck-design", section], env={**env, "THEODORE_CALLER_PID": caller},
                                  capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, start_new_session=True)
            self.assertEqual(done.returncode, 0, done.stdout)

    def test_redirect_to_a_file_or_the_null_device_prints_no_guidance(self):
        self.sign_in()
        out = self.box.tmp / "guide.txt"
        done = self._shell(self._client_line("guide", "deck-design", "forms") + f" > {shlex.quote(str(out))}", env={"THEODORE_TEST_CHAIN_WINDOW": "0"})
        self.assertEqual(done.returncode, 64)
        text = out.read_text()
        self.assertNotIn("FORMS-MARKER", text)
        self.assertTrue(text.startswith("THEODORE OUTPUT_REDIRECTED: "), text)
        self.assertIn("without > or any redirection", text)
        done = self._shell(self._client_line("guide", "deck-design", "forms") + " > /dev/null", env={"THEODORE_TEST_CHAIN_WINDOW": "0"})
        self.assertEqual(done.returncode, 64)
        self.assertNotIn("forms", self.record().get("read", {}).get("deck-design", {}))
        self.assertEqual(self.api.guide_fetches, 0)
        piped = self._shell(self._client_line("guide", "deck-design", "forms") + " | cat", env={"THEODORE_TEST_CHAIN_WINDOW": "0"})
        self.assertIn("FORMS-MARKER", piped.stdout, "a pipe is how hosts capture output: allowed")


class Record(ReadingCase):
    def test_parts_sections_specimens_and_no_text(self):
        self.sign_in()
        self.guide()
        self.guide("craft")                       # part 1 of 2 only
        record = self.record()
        craft = record["read"]["deck-design"]["craft"]
        self.assertEqual((craft["total"], sorted(craft["parts"])), (2, ["1"]))
        self.assertEqual(record["plan"]["deck-design"]["reading"], ["craft", "forms", "worked-notes"])
        self.assertEqual(record["plan"]["deck-design"]["review"], ["critique"])
        result = self.client("reading")
        _first, nxt, tell = self.assertStatus(result, "READING_REQUIRED", 64)
        self.assertIn("guide deck-design craft --part 2", nxt)
        self.assertIn("12 hours", tell)
        self.assertNotIn("CRAFT-MARKER", result.stdout)
        self.api.specimens = {"one.jpg": b"\xff\xd8\xff\xe0 sheet \xff\xd9", "two.jpg": b"\xff\xd8\xff\xe0 sheet 2 \xff\xd9"}
        fetched = self.client("specimen", "one.jpg", "two.jpg", cwd=self.ws)
        self.assertEqual(fetched.returncode, 0, fetched.stdout)
        self.assertEqual([n for _t, n in self.record()["specimens"]], [2])
        raw = (self.box.home / "state" / "reading.json").read_text()
        for marker in ("ENTRY-MARKER", "CRAFT-MARKER", "ccccc"):
            self.assertNotIn(marker, raw, "the record holds keys and times, never guidance")
        self.guide("craft", "--part", "2")
        self.guide("forms")
        self.guide("worked-notes")
        _f, nxt, _t = self.assertStatus(self.client("reading"), "OK", 0)
        self.assertIn("guide deck-design critique", nxt)
        self.guide("critique")
        first, _n, _t = self.assertStatus(self.client("reading"), "OK", 0)
        self.assertIn("Specimen sheets fetched: 2", first)

    def test_licence_is_not_reading_and_a_failed_print_does_not_count(self):
        self.sign_in()
        self.assertEqual(self.client("guide", "deck-design", "--license").returncode, 0)
        self.assertEqual(self.record().get("read", {}), {})
        self.api.down = True
        self.assertStatus(self.client("guide", "deck-design"), "OFFLINE", 71)
        self.assertEqual(self.record().get("read", {}), {})


class Gate(ReadingCase):
    def setUp(self):
        super().setUp()
        self.ready()

    def assertEngineRan(self, result, verb):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("THEODORE ", result.stdout)
        if verb == "start":
            self.assertTrue((self.ws / ".pptx-state").is_dir())

    def test_enforced_when_the_design_method_is_engaged(self):
        self.guide()
        for verb in ("start", "apply", "finish"):
            result = self.client("pptx", verb, str(self.ws))
            _first, nxt, tell = self.assertStatus(result, "READING_REQUIRED", 64)
            self.assertIn(f'"{self.box.home / "bin" / "theodore"}" guide deck-design craft by itself', nxt)
            self.assertIn("12 hours", tell)
        self.assertFalse((self.ws / ".pptx-state").exists(), "the engine did not run")
        self.guide("craft")
        _f, nxt, _t = self.assertStatus(self.client("pptx", "start", str(self.ws)), "READING_REQUIRED", 64)
        self.assertIn("guide deck-design craft --part 2", nxt)
        self.guide("craft", "--part", "2")
        self.guide("forms")
        _f, nxt, _t = self.assertStatus(self.client("pptx", "--quiet", "start", str(self.ws)), "READING_REQUIRED", 64)
        self.assertIn("guide deck-design worked-notes", nxt)
        self.guide("worked-notes")
        # A sub-agent or a new session within the window: same installation, nothing to read again.
        sub = subprocess.run([sys.executable, "-I", str(CLIENT_DIR), "pptx", "start", str(self.ws)], env=self.box.env, capture_output=True,
                             text=True, timeout=120, stdin=subprocess.DEVNULL, start_new_session=True)
        self.assertEngineRan(sub, "start")
        self.assertEngineRan(self.client("pptx", "apply", str(self.ws)), "apply")
        result = self.client("pptx", "finish", str(self.ws))
        _f, nxt, _t = self.assertStatus(result, "READING_REQUIRED", 64)
        self.assertIn("guide deck-design critique by itself", nxt)
        self.assertIn("revise the deck against it", nxt)
        self.assertStatus(self.client("reading"), "OK", 0)
        self.guide("critique")
        self.assertEngineRan(self.client("pptx", "finish", str(self.ws)), "finish")
        self.assertEngineRan(self.client("pptx", "render", str(self.ws)), "render")

    def test_engine_python_entry_is_gated_too(self):
        self.guide()
        script = self.box.home / "engines" / self.ENGINE_VERSION / "skills" / "pptx" / "scripts" / "pptx.py"
        self.assertStatus(self.client("python", str(script), "apply", str(self.ws)), "READING_REQUIRED", 64)
        self.assertEqual(self.client("python", "-c", "print(1)").returncode, 0)

    def test_not_enforced_without_the_design_method(self):
        self.assertEqual(self.client("guide", "pptx").returncode, 0)        # a typo fix, a translation: pptx only
        self.assertEngineRan(self.client("pptx", "start", str(self.ws)), "start")
        self.assertEngineRan(self.client("pptx", "finish", str(self.ws)), "finish")
        self.assertStatus(self.client("reading"), "OK", 0)

    def test_window_expiry(self):
        self.guide()
        self.assertStatus(self.client("pptx", "start", str(self.ws)), "READING_REQUIRED", 64)
        later = {"THEODORE_TEST_CLOCK_OFFSET": str(HOURS_12 + 60)}
        self.assertEngineRan(self.client("pptx", "start", str(self.ws), env=later), "start")     # the entry is 12 hours old
        self.read_all_required()
        self.guide("critique")
        self.assertEngineRan(self.client("pptx", "finish", str(self.ws)), "finish")
        self.guide(env=later)                                            # engaged again the next day: read again
        _f, nxt, _t = self.assertStatus(self.client("pptx", "apply", str(self.ws), env=later), "READING_REQUIRED", 64)
        self.assertIn("guide deck-design craft", nxt)

    def test_an_older_server_without_the_headers_enforces_nothing(self):
        self.api.guide_plan.clear()
        self.guide()
        self.assertEngineRan(self.client("pptx", "start", str(self.ws)), "start")
        self.assertEngineRan(self.client("pptx", "finish", str(self.ws)), "finish")
        first, _n, _t = self.assertStatus(self.client("reading"), "OK", 0)
        self.assertIn("named no required sections", first)


class Units(unittest.TestCase):
    def test_verbs(self):
        self.assertEqual(reading.verb_of(["--quiet", "apply", "x"]), "apply")
        self.assertEqual(reading.verb_of(["-q"]), "")
        self.assertEqual(reading.python_verb(["/a b/skills/pptx/scripts/pptx.py", "-q", "finish"]), "finish")
        self.assertEqual(reading.python_verb(["C:\\x\\pptx.py", "start"]), "start")
        self.assertEqual(reading.python_verb(["-c", "print(1)"]), "")

    def test_codes_stay_in_the_frozen_table(self):
        from theodore_client.core import Status
        self.assertEqual([Status(n, "a", "b", "c").code for n in ("ONE_AT_A_TIME", "OUTPUT_REDIRECTED", "READING_REQUIRED")], [75, 64, 64])

    def test_plan_header_parsing(self):
        self.assertEqual(reading._plan_keys(" craft, forms ,,entry,bad key,craft"), ["craft", "forms"])
        self.assertIsNone(reading._plan_keys(None))
        self.assertEqual(reading._plan_keys(""), [])


class ThroughTheLauncher(unittest.TestCase):
    """The real launcher (sh, or theodore.cmd from cmd and PowerShell): chains, redirection and the gate."""

    def setUp(self):
        self.api = FakeApi().start()
        self.addCleanup(self.api.stop)
        self.api.publish_runtime(RUNTIME_ID, fake_runtime_parts())
        self.api.publish_engine("1.0.0-rc.3", engine_zip("1.0.0-rc.3"))
        for pack in ("icons", "flags", "backgrounds", "maps"):
            self.api.publish_pack(pack, "1.0.0", pack_zip(pack, "1.0.0"))
        self.api.guides["deck-design"].update(SECTIONS)
        self.api.guide_plan["deck-design"] = PLAN
        self.box = Sandbox(self.api)
        self.addCleanup(self.box.close)
        self.api.issue_activation_code("ABCD-1234")
        login = self.box.run_launcher("login", "--code", "ABCD-1234", timeout=600)
        self.assertEqual(login.returncode, 0, login.stdout + login.stderr)
        self.box.wait_jobs(600)
        self.launcher = str(self.box.launcher)

    def _run(self, command, env=None):
        return subprocess.run(command, env={**self.box.env, **(env or {})}, capture_output=True, text=True, timeout=600,
                              stdin=subprocess.DEVNULL, cwd=str(self.box.tmp))

    def _chain(self, sections: list[list[str]], shell: str = "default", redirect: str | None = None):
        if not IS_WINDOWS:
            parts = [" ".join(shlex.quote(a) for a in ["/bin/sh", self.launcher, "guide", "deck-design", *s]) for s in sections]
            line = "; ".join(parts) + (f" > {shlex.quote(redirect)}" if redirect else "")
            return self._run(["/bin/sh", "-c", line], env=CHAIN if not redirect else None)
        if shell == "powershell":
            parts = [f"& '{self.launcher}' guide deck-design {' '.join(s)}" for s in sections]
            return self._run(["powershell.exe", "-NoProfile", "-Command", "; ".join(parts) + "; exit $LASTEXITCODE"], env=CHAIN)
        parts = [f'call "{self.launcher}" guide deck-design {" ".join(s)}'.rstrip() for s in sections]
        line = " & ".join(parts) + (f' > "{redirect}"' if redirect else "")
        return self._run(f'cmd /d /s /c "{line}"', env=CHAIN if not redirect else None)

    def test_three_guides_in_one_command_print_one(self):
        shells = ["default", "powershell"] if IS_WINDOWS else ["default"]
        for shell in shells:
            with self.subTest(shell=shell):
                time.sleep(reading.CHAIN_WINDOW_S + 0.5)
                done = self._chain([[], ["craft"], ["forms"]], shell)
                self.assertEqual(done.stdout.count("ENTRY-MARKER"), 1, done.stdout + done.stderr)
                self.assertNotIn("CRAFT-MARKER", done.stdout)
                self.assertNotIn("FORMS-MARKER", done.stdout)
                self.assertEqual(done.stdout.count("THEODORE ONE_AT_A_TIME: "), 2, done.stdout)

    def test_redirect_and_gate(self):
        out = self.box.tmp / "out file.txt"
        done = self._chain([["forms"]], redirect=str(out))
        self.assertEqual(done.returncode, 64, done.stdout + done.stderr)
        text = out.read_text()
        self.assertTrue(text.startswith("THEODORE OUTPUT_REDIRECTED: "), text)
        self.assertNotIn("FORMS-MARKER", text)
        engine = self.box.run_launcher("guide", "pptx", timeout=600)
        self.assertEqual(engine.returncode, 0, engine.stdout + engine.stderr)
        self.assertEqual(self.box.run_launcher("guide", "deck-design").returncode, 0)
        ws = self.box.tmp / "deck one"
        ws.mkdir()
        refused = self.box.run_launcher("pptx", "start", str(ws))
        self.assertEqual(refused.returncode, 64, refused.stdout + refused.stderr)
        first, nxt, _tell = status_lines(refused.stdout)
        self.assertTrue(first.startswith("THEODORE READING_REQUIRED: "), first)
        self.assertIn("guide deck-design craft", nxt)
        self.assertFalse((ws / ".pptx-state").exists())
        for args in ([], ["--part", "2"]):
            self.assertEqual(self.box.run_launcher("guide", "deck-design", "craft", *args).returncode, 0)
        for section in ("forms", "worked-notes"):
            self.assertEqual(self.box.run_launcher("guide", "deck-design", section).returncode, 0)
        started = self.box.run_launcher("pptx", "start", str(ws))
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertTrue((ws / ".pptx-state").is_dir())


if __name__ == "__main__":
    unittest.main()
