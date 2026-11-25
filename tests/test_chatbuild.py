"""Building from a chat (`mcp --for claude-desktop`, actions manual, run, read, write, edit, list): what runs, where it
runs, and the confinement of the deck script (client/theodore_client/chatbuild.py and node_guard.js)."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import CLIENT_DIR, IS_WINDOWS  # noqa: E402
from test_mcp import JPEG, McpCase, Server  # noqa: E402

from theodore_client import chatbuild, core  # noqa: E402

GUARD = CLIENT_DIR / "theodore_client" / "node_guard.js"
DEFAULT = ("Theodore",) if sys.platform == "darwin" else ("Documents", "Theodore")     # a fresh install's default folder


class Paths(unittest.TestCase):
    """The checks themselves, in this process, against a scratch home."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Path(os.path.realpath(self.tmp)) / "home"
        (self.home / "Documents" / "decks").mkdir(parents=True)
        (self.home / ".ssh").mkdir()
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "THEODORE_HOME", "THEODORE_CACHE_DIR")}
        os.environ.update({"HOME": str(self.home), "USERPROFILE": str(self.home), "THEODORE_HOME": str(self.home / ".theodore"),
                           "THEODORE_CACHE_DIR": str(self.home / "cache")})
        core._HOME = None
        chatbuild.FOLDERS.clear()

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            core._HOME = None
            chatbuild.FOLDERS.clear()
        self.addCleanup(restore)
        self.folder = self.home / "Documents" / "decks"

    def test_folders_a_deck_may_be_built_in(self):
        allowed = chatbuild.folder_allowed
        self.assertTrue(allowed(self.folder))
        self.assertFalse(allowed(self.home), "the home folder itself")
        self.assertFalse(allowed(self.home / ".ssh"), "a hidden folder")
        self.assertFalse(allowed(self.home / ".theodore"), "Theodore's own folder")
        self.assertFalse(allowed(self.home / "cache" / "x"), "the render cache")
        if not IS_WINDOWS:
            self.assertFalse(allowed(Path("/etc")))
            self.assertFalse(allowed(Path("/")))
            self.assertFalse(allowed(Path("/tmp")))
            self.assertTrue(allowed(Path("/Volumes/Disk/Decks")) or sys.platform != "darwin")
        with self.assertRaises(core.Status):
            chatbuild.check_folder("relative/path")
        with self.assertRaises(core.Status):
            chatbuild.check_folder(str(self.home / "Documents" / "missing"))
        self.assertEqual(chatbuild.check_folder(str(self.folder)), self.folder)

    def test_only_engine_verbs_and_only_paths_inside_the_folder(self):
        check = lambda *args: chatbuild.check_args(list(args), self.folder)
        for bad in (["python", "-c", "1"], ["apply; open -a Calculator"], ["sh"], ["--quiet"], []):
            with self.assertRaises(core.Status, msg=bad):
                check(*bad)
        with self.assertRaises(core.Status):
            check("doctor", "--powerpoint")
        with self.assertRaises(core.Status):
            check("start", "--from", str(self.home / ".ssh" / "id.pptx"), "--brief", "x")
        with self.assertRaises(core.Status):
            check("apply", "../../.theodore/state")
        with self.assertRaises(core.Status):
            check("start", "--blank", "--output=/etc/x.pptx", "--brief", "x")
        out = check("start", "--from", "deck.pptx", "--brief", "Q1/Q2 revenue, see ./notes", "--name", "a/b", "--storyline", "-")
        self.assertEqual(out[2], str(self.folder / "deck.pptx"), "a relative path is made absolute in the folder")
        self.assertEqual(out[4], "Q1/Q2 revenue, see ./notes", "text options are never paths")
        self.assertEqual(out[6], "a/b")
        self.assertEqual(out[-1], "-")
        self.assertEqual(check("assets", "search", "icons", "electric bus"), ["assets", "search", "icons", "electric bus"])
        # `pptx check` (engine rc.34): the deck's checks without finishing it; its workspace is a path in the folder.
        ws = self.folder / ".theodore" / "workspaces" / "d"
        self.assertEqual(check("check", ".theodore/workspaces/d"), ["check", str(ws)])
        self.assertNotIn("check", chatbuild.CHANGING, "it reads the workspace: no started workspace needed")

    def test_every_step_names_the_next_one_through_to_the_file(self):
        ws = self.folder / ".theodore" / "workspaces" / "d"
        (ws / ".pptx-state").mkdir(parents=True)
        start = chatbuild.next_step(["start", "--blank"], f"WORKSPACE: {ws}\nOUTPUT: {self.folder / 'd.pptx'}\n", 0)
        self.assertIn(f"{ws}/deck.js", start)
        self.assertIn(chatbuild._run_call("apply", str(ws)), start)        # JSON: backslashes of a Windows path doubled
        self.assertIn(chatbuild._run_call("finish", str(ws)), chatbuild.next_step(["apply", str(ws)], "{}", 0))
        self.assertIn("apply again", chatbuild.next_step(["apply", str(ws)], "{}", 1))
        done = chatbuild.next_step(["finish", str(ws)], '{"delivered": {"output": "/x/d.pptx"}}', 0)
        self.assertIn("/x/d.pptx", done)
        self.assertIn("only if the user asks", done)

    @unittest.skipIf(IS_WINDOWS, "symbolic links need privileges on Windows")
    def test_a_link_out_of_the_folder_is_refused(self):
        (self.folder / "out").symlink_to(self.home / ".ssh")
        with self.assertRaises(core.Status):
            chatbuild.resolve("out/key.json", self.folder)
        with self.assertRaises(core.Status):
            chatbuild.write("out/notes.md", "x", str(self.folder))

    def test_files_written_are_text_files_in_the_folder(self):
        folder = str(self.folder)
        with self.assertRaises(core.Status):
            chatbuild._writable("run.sh", self.folder)
        with self.assertRaises(core.Status):
            chatbuild._writable("deck.pptx", self.folder)
        with self.assertRaises(core.Status):
            chatbuild._writable(str(self.home / "Documents" / "other.js"), self.folder)
        path = chatbuild._writable(".theodore/workspaces/d/deck.js", self.folder)
        self.assertEqual(path, self.folder / ".theodore" / "workspaces" / "d" / "deck.js")
        del folder


@unittest.skipUnless(shutil.which("node"), "Node is not installed here")
class Guard(unittest.TestCase):
    """node_guard.js with the permission model, as a chat's deck script runs (Node 24 or newer)."""

    def setUp(self):
        version = subprocess.run(["node", "-p", "process.versions.node"], capture_output=True, text=True).stdout.strip()
        if int(version.split(".")[0] or 0) < 24:
            self.skipTest(f"Node {version}: the runtime ships Node 24")
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = self.tmp / "work"
        self.engine = self.tmp / "engine"
        self.secret = self.tmp / "secret.txt"
        for folder in (self.work, self.engine):
            folder.mkdir()
        self.secret.write_text("secret")
        (self.engine / "helper.js").write_text("console.log('helper ran', process.permission.has('fs.read', process.argv[2]))")

    def run_script(self, body: str) -> dict:
        script = self.work / "deck.js"
        script.write_text(body + "\nrequire('fs').writeFileSync(__dirname + '/out.json', JSON.stringify(results));\n")
        env = {**os.environ, "NODE_OPTIONS": chatbuild.node_options([self.work, self.engine], [self.work]),
               "THEODORE_NODE_SCRIPT_ROOT": str(self.engine), "THEODORE_NODE_ADDON_ROOTS": str(self.engine)}
        done = subprocess.run(["node", str(script)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("SecurityWarning", done.stderr)
        return json.loads((self.work / "out.json").read_text())

    PROBE = """
const results = {};
const t = (name, f) => { try { results[name] = "ALLOWED " + String(f()); } catch (e) { results[name] = "DENIED " + (e.code || e.message); } };
"""

    def test_the_script_reads_and_writes_only_its_folders_and_runs_nothing_else(self):
        results = self.run_script(self.PROBE + f"""
t("read outside", () => require("fs").readFileSync({json.dumps(str(self.secret))}, "utf8"));
t("write outside", () => require("fs").writeFileSync({json.dumps(str(self.tmp / "x.txt"))}, "x"));
t("read engine", () => require("fs").readFileSync({json.dumps(str(self.engine / "helper.js"))}, "utf8").length > 0);
t("shell", () => require("child_process").execFileSync("/bin/sh", ["-c", "id"]));
t("node -e", () => require("child_process").execFileSync("node", ["-e", "1"]));
t("node on a script it wrote", () => {{ require("fs").writeFileSync(__dirname + "/evil.js", "1"); return require("child_process").execFileSync("node", [__dirname + "/evil.js"]); }});
t("node on the engine's script", () => require("child_process").execFileSync("node", [{json.dumps(str(self.engine / "helper.js"))}, {json.dumps(str(self.secret))}], {{ encoding: "utf8" }}).trim());
t("spawnSync of a missing program", () => require("child_process").spawnSync("fc-match", ["x"], {{ encoding: "utf8" }}).status);
t("spawn", () => require("child_process").spawn("/bin/sh"));
t("ChildProcess", () => new (require("child_process").ChildProcess)());
t("net", () => require("net").connect(80, "example.com"));
t("node:https", () => require("node:https").get("https://example.com"));
t("getBuiltinModule", () => process.getBuiltinModule("net").connect(80, "example.com"));
t("fetch", () => fetch("https://example.com"));
t("worker", () => new (require("worker_threads").Worker)("1", {{ eval: true }}));
t("addon", () => process.dlopen({{ exports: {{}} }}, __dirname + "/x.node"));
t("replace Module._load", () => {{ require("module")._load = () => "mine"; if (require("module")._load("net") === "mine") return "replaced"; throw new Error("no effect"); }});
t("binding", () => process.binding("spawn_sync"));
""")
        allowed = {"read engine", "node on the engine's script", "spawnSync of a missing program"}
        for name, outcome in results.items():
            if name in allowed:
                self.assertTrue(outcome.startswith("ALLOWED"), f"{name}: {outcome}")
            else:
                self.assertTrue(outcome.startswith("DENIED"), f"{name}: {outcome}")
        self.assertEqual(results["node on the engine's script"], "ALLOWED helper ran false", "the child runs under the same restriction")
        self.assertEqual(results["spawnSync of a missing program"], "ALLOWED null", "fc-match fails as a missing program would")
        self.assertFalse((self.tmp / "x.txt").exists())

    def test_node_started_with_inline_code_loads_the_guard(self):
        """The engine resolves an icon named by words with `node -e`. Under the options of a chat's engine processes
        that Node starts (it reads the guard's file) and the guard holds in it (TR-X7W78H: it stopped with
        ERR_ACCESS_DENIED before any code ran, so `assets show icons <words>` failed from a chat)."""
        env = {**os.environ, "NODE_OPTIONS": chatbuild.node_options([self.work, self.engine], [self.work]),
               "THEODORE_NODE_SCRIPT_ROOT": str(self.engine), "THEODORE_NODE_ADDON_ROOTS": str(self.engine)}
        code = ("require(process.argv[1]); const out = ['helper loaded'];"
                "try { require('https').get('https://example.com'); out.push('network open') } catch (e) { out.push(e.code) }"
                "try { require('fs').readFileSync(process.argv[2]); out.push('read outside') } catch (e) { out.push(e.code) }"
                "console.log(out.join(' | '))")
        done = subprocess.run(["node", "-e", code, str(self.engine / "helper.js"), str(self.secret)], env=env, cwd=str(self.work),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip().splitlines()[-1], "helper loaded | ERR_THEODORE_SANDBOX | ERR_ACCESS_DENIED")

    def test_import_reaches_only_the_stand_ins(self):
        results = self.run_script(self.PROBE + """
(async () => {
  for (const name of ["node:child_process", "node:net", "node:https", "node:http2", "node:dns"]) {
    try { const m = await import(name); results[name] = typeof m.default; m.default.connect ? m.default.connect() : m.default.execSync("id"); results[name] = "ALLOWED"; }
    catch (e) { results[name] = "DENIED " + (e.code || e.message); }
  }
  require('fs').writeFileSync(__dirname + '/out.json', JSON.stringify(results));
})();
""")
        for name in ("node:child_process", "node:net", "node:https", "node:http2", "node:dns"):
            self.assertTrue(results[name].startswith("DENIED"), f"{name}: {results[name]}")

    def test_a_library_that_patches_https_at_load_keeps_working(self):
        results = self.run_script(self.PROBE + """
const https = require("https");
https.get = () => "patched"; https.request = https.get;
Object.defineProperty(https, Symbol.for("x"), { value: true });
t("patched", () => https.get());
t("marker", () => https[Symbol.for("x")]);
""")
        self.assertEqual(results, {"patched": "ALLOWED patched", "marker": "ALLOWED true"})


class Desktop(McpCase):
    """Through the server, with the stand-in engine."""

    def setUp(self):
        super().setUp()
        self.api.features = [*self.api.features, chatbuild.CHAT_BUILD_FEATURE]      # the beta plan's feature of this route

    def test_run_executes_the_engine_in_the_folder_with_the_restriction(self):
        self.ready()
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        answer = server.call("theodore", action="run", folder=str(folder), args=["list", "--json"], stdin="# Design — x\n")
        self.assertFalse(answer.get("isError"), answer)
        text = answer["content"][0]["text"]
        seen = json.loads(text.split("\n", 1)[1])
        self.assertEqual(Path(seen["cwd"]).resolve(), folder.resolve())
        self.assertEqual(seen["stdin"], "# Design — x\n")
        self.assertEqual(Path(seen["home"]), self.box.home / "state" / "chat", "the engine sees a private home, not Theodore's")
        self.assertTrue(seen["licence_copy"], "the engine reads a copy of the licence while it runs")
        self.assertFalse((self.box.home / "state" / "chat" / "state" / "entitlement.json").exists(), "and none is left after")
        self.assertIn("--permission", seen["node_options"])
        self.assertIn(str(GUARD.resolve()), seen["node_options"])
        self.assertIn(str(folder.resolve()), seen["node_options"])
        self.assertNotIn(f'--allow-fs-read="{self.box.home}"', seen["node_options"])
        self.assertEqual(Path(seen["tmp"]), self.box.home / "state" / "chat" / "tmp")
        refused = server.call("theodore", action="run", folder=str(folder), args=["python", "-c", "1"])
        self.assertTrue(refused.get("isError"))
        self.assertIn("not one of Theodore's engine verbs", refused["content"][0]["text"])
        outside = server.call("theodore", action="read", folder=str(folder), path=str(self.box.home / "state" / "entitlement.json"))
        self.assertTrue(outside.get("isError"))
        self.assertEqual(server.close(), b"")

    def test_a_licence_refusal_is_read_from_the_run_and_the_engine_starts_once(self):
        """0.4.34: the chat route reads the refusal from the engine's own output (exit 1, or 77 from engine rc.34)."""
        self.ready()
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        starts = self.box.tmp / "engine-starts.jsonl"
        engine = self.box.home / "engines" / self.ENGINE_VERSION
        (engine / "REFUSE").write_text("entitlement_clock_rollback")
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp), "THEODORE_TEST_ENGINE_STARTS": str(starts)})
        for exit_code in ("1", "77"):
            (engine / "REFUSE_EXIT").write_text(exit_code)
            answer = server.call("theodore", action="run", folder=str(folder), args=["list", "--json"])
            self.assertTrue(answer.get("isError"), answer)
            self.assertIn("THEODORE NO_ENTITLEMENT: ", answer["content"][0]["text"], exit_code)
        self.assertEqual(len(starts.read_text().splitlines()), 2, "one engine start per run, never a second with --help")
        server.close()

    def test_starting_the_same_deck_twice_continues_the_first(self):
        """ONESTEP saw a chat call start twice: the user got name-2.pptx beside an orphan. The second start of the same
        name in the same folder answers with the first workspace; --new still makes another."""
        self.ready()
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        ws = folder / ".theodore" / "workspaces" / "board"
        brief = ("One-slide deck for a general reader: " + "why hospitals adopt ambient scribes, " * 20)[:600]     # longer than a file name may be
        first = server.call("theodore", action="run", folder=str(folder), args=["start", "--blank", "--name", "Board", "--brief", brief, str(ws)])
        self.assertNotIn("too long", first["content"][0]["text"], "ONESTEP: a long brief once ended the answer of a start that had run")
        self.assertFalse(first.get("isError"), first)
        again = server.call("theodore", action="run", folder=str(folder), args=["start", "--blank", "--name", "Board", "--brief", "b", str(ws)])
        self.assertIn("already started", again["content"][0]["text"])
        separate = server.call("theodore", action="run", folder=str(folder), args=["start", "--blank", "--new", "--name", "Board", "--brief", "c", str(ws)])
        self.assertNotIn("already started", separate["content"][0]["text"])
        # ONESTEP: a later chat on the same topic must never continue (and overwrite) a FINISHED deck.
        (ws / "result.json").write_text("{}")
        later = server.call("theodore", action="run", folder=str(folder), args=["start", "--blank", "--name", "Board", "--brief", "d", str(ws)])
        self.assertNotIn("already started", later["content"][0]["text"])
        self.assertEqual(server.close(), b"")

    def test_the_chat_manual_says_nothing_about_versions_and_leaves_the_notice_for_the_coding_hosts(self):
        """An update notice says what changed since the last skill load, at any earlier time: in a chat, told mid-build,
        it read "Theodore updated itself while building" when nothing had (an install's plugin update minutes before)."""
        self.ready()
        update = self.box.home / "state" / "update.json"
        state = json.loads(update.read_text()) if update.exists() else {}
        state["announce"] = {"plugin": {"version": "9.9.9"}}
        update.write_text(json.dumps(state))
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        manual = server.call("theodore", action="manual")["content"][0]["text"]
        self.assertNotIn("updated itself", manual)
        self.assertNotIn("THEODORE NOTICE", manual)
        server.close()
        self.assertEqual(json.loads(update.read_text())["announce"], {"plugin": {"version": "9.9.9"}}, "still there for Claude Code")
        plugin = self.server()
        guide = plugin.call("guide", skill="pptx")["content"][0]["text"]
        self.assertIn("THEODORE NOTICE: Theodore updated itself: plugin files 9.9.9", guide)
        plugin.close()

    def test_the_chat_manual_and_a_read_of_it_are_composed_for_the_lead(self):
        self.ready()
        skill = self.box.home / "engines" / self.ENGINE_VERSION / "skills" / "pptx" / "SKILL.md"
        skill.write_text("# Manual\n<!-- theodore:if lead -->\nSend the reviewer message.\n<!-- theodore:end -->\nFinish.\n")
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        manual = server.call("theodore", action="manual")["content"][0]["text"]
        self.assertIn("# Manual\nSend the reviewer message.\nFinish.\n", manual)
        read = server.call("theodore", action="read", folder=str(folder), path=str(skill))["content"][0]["text"]
        self.assertIn("\n# Manual\nSend the reviewer message.\nFinish.\n", read)
        self.assertNotIn("theodore:", manual + read)
        server.close()

    def test_the_tool_name_and_description_can_be_set_for_testing(self):
        """THEODORE_CHAT_TOOL_NAME / _DESCRIPTION (testing which name a chat picks): every answer names the tool in use."""
        self.ready()
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp), "THEODORE_CHAT_TOOL_NAME": "create_presentation",
                                                                   "THEODORE_CHAT_TOOL_DESCRIPTION": "Makes PowerPoint presentations."})
        tools = server.send("tools/list")["result"]["tools"]
        self.assertEqual([(t["name"], t["description"]) for t in tools], [("create_presentation", "Makes PowerPoint presentations.")])
        manual = server.call("create_presentation", action="manual")["content"][0]["text"]
        self.assertIn('create_presentation with action "run"', manual)
        self.assertNotIn("theodore with action", manual)
        ran = server.call("create_presentation", action="run", folder=str(folder), args=["render", "x"])
        self.assertIn("unknown tool", server.call("theodore", action="begin")["content"][0]["text"])
        del ran
        self.assertEqual(server.close(), b"")
        bad = self.server(args=("--for", "claude-desktop"), env={"THEODORE_CHAT_TOOL_NAME": "no spaces; allowed"})
        self.assertEqual([t["name"] for t in bad.send("tools/list")["result"]["tools"]], ["theodore"])
        bad.close()

    def test_codex_thread_folder_is_the_default(self):
        """Codex sends its thread id in _meta; the thread's session file names the folder it works in."""
        self.ready()
        folder = self.box.tmp / "Documents" / "Project"
        folder.mkdir(parents=True)
        codex = self.box.tmp / "codex-home"
        thread = "019a1b2c-3d4e-7f60-8a9b-0c1d2e3f4a5b"
        session = codex / "sessions" / "2026" / "11" / "15" / f"rollout-2026-11-15T14-00-00-{thread}.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": str(folder)}}) + "\n{}\n")
        server = self.server(args=("--for", "codex"), env={"HOME": str(self.box.tmp), "CODEX_HOME": str(codex)})
        self.assertEqual([t["name"] for t in server.send("tools/list")["result"]["tools"]], ["theodore"], "Codex: the same one tool")
        answer = server.send("tools/call", {"name": "theodore", "arguments": {"action": "run", "args": ["list"]},
                                            "_meta": {"threadId": thread}})["result"]
        seen = json.loads(answer["content"][0]["text"].split("\n", 1)[1])
        self.assertEqual(Path(seen["cwd"]).resolve(), folder.resolve())
        other = server.send("tools/call", {"name": "theodore", "arguments": {"action": "run", "args": ["list"]},
                                           "_meta": {"x-codex-turn-metadata": {"thread_id": "019a0000-0000-0000-0000-000000000000"}}})["result"]
        seen = json.loads(other["content"][0]["text"].split("\n", 1)[1])
        self.assertEqual(Path(seen["cwd"]).resolve(), self.box.tmp.joinpath(*DEFAULT).resolve(), "an unknown thread: the default")
        session.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": str(self.box.tmp)}}) + "\n")
        home = server.send("tools/call", {"name": "theodore", "arguments": {"action": "run", "args": ["list"]},
                                          "_meta": {"threadId": thread}})["result"]
        seen = json.loads(home["content"][0]["text"].split("\n", 1)[1])
        self.assertEqual(Path(seen["cwd"]).resolve(), self.box.tmp.joinpath(*DEFAULT).resolve(), "a thread in the home folder: the default")
        self.assertEqual(server.close(), b"")

    def test_a_finished_deck_or_one_from_another_session_is_never_changed(self):
        """Overwriting a delivered deck must be impossible, not unlikely: every start from a chat is a new deck (--new,
        never the engine's reopening of an earlier one), and a command that changes a deck runs only on a workspace this
        server started and that is not finished."""
        self.ready()
        folder = self.box.tmp / "Documents" / "Board"
        foreign = folder / ".theodore" / "workspaces" / "earlier"           # started by another session
        (foreign / ".pptx-state").mkdir(parents=True)
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        refused = server.call("theodore", action="run", folder=str(folder), args=["apply", str(foreign)])
        self.assertTrue(refused.get("isError"))
        self.assertIn("not one this conversation started", refused["content"][0]["text"])
        self.assertIn('"start", "--from"', refused["content"][0]["text"])
        ws = folder / ".theodore" / "workspaces" / "board"
        started = server.call("theodore", action="run", folder=str(folder), args=["start", "--blank", "--name", "Board", "--brief", "a", str(ws)])
        self.assertIn('"--new"', started["content"][0]["text"], "the engine never reopens an earlier deck for a chat")
        self.assertFalse(server.call("theodore", action="run", folder=str(folder), args=["apply", str(ws)]).get("isError"))
        (ws / "result.json").write_text(json.dumps({"output": str(folder / "board.pptx")}))      # finished
        after = server.call("theodore", action="run", folder=str(folder), args=["apply", str(ws)])
        self.assertTrue(after.get("isError"))
        self.assertIn("finished", after["content"][0]["text"])
        self.assertIn(str(folder / "board.pptx"), after["content"][0]["text"])
        for verb in (["finish", str(ws)], ["slide", "set", str(ws), "s1", "x.js"], ["comments", str(ws), "--reply", "c1"]):
            self.assertTrue(server.call("theodore", action="run", folder=str(folder), args=verb).get("isError"), verb)
        shown = server.call("theodore", action="run", folder=str(folder), args=["slide", "show", str(ws), "s1"])
        self.assertFalse(shown.get("isError"), "reading a finished deck is fine")
        self.assertEqual(server.close(), b"")

    def test_without_a_folder_the_deck_goes_to_the_default_folder(self):
        self.ready()
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        answer = server.call("theodore", action="run", args=["list"])
        seen = json.loads(answer["content"][0]["text"].split("\n", 1)[1])
        self.assertEqual(Path(seen["cwd"]).resolve(), self.box.tmp.joinpath(*DEFAULT).resolve())
        self.assertEqual(server.close(), b"")

    def test_images_the_output_names_come_back_and_files_round_trip(self):
        self.ready()
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        (folder / "slide-1.jpg").write_bytes(JPEG)
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        rendered = server.call("theodore", action="run", folder=str(folder), args=["render", "slide-1.jpg"])
        images = [b for b in rendered["content"] if b["type"] == "image"]
        self.assertEqual([base64.b64decode(b["data"]) for b in images], [JPEG])
        self.assertIn("NEXT: look at the images", rendered["content"][0]["text"])
        spaced = folder / "Théo Tester decks"            # WINDOWS: a user folder with a space and an accent
        spaced.mkdir()
        (spaced / "slide 2.jpg").write_bytes(JPEG)
        found = server.call("theodore", action="run", folder=str(folder), args=["render", str(spaced / "slide 2.jpg")])
        self.assertEqual([base64.b64decode(b["data"]) for b in found["content"] if b["type"] == "image"], [JPEG])
        quiet = server.call("theodore", action="run", folder=str(folder), args=["render", "slide-1.jpg"], images=False)
        self.assertFalse([b for b in quiet["content"] if b["type"] == "image"])
        wrote = server.call("theodore", action="write", folder=str(folder), path="notes/design.md", content="# Design\nline\n")
        self.assertFalse(wrote.get("isError"), wrote)
        edited = server.call("theodore", action="edit", path=str(folder / "notes" / "design.md"), old="line", new="row")
        self.assertFalse(edited.get("isError"), edited)
        self.assertEqual((folder / "notes" / "design.md").read_text(), "# Design\nrow\n")
        read = server.call("theodore", action="read", path=str(folder / "notes" / "design.md"))
        self.assertIn("# Design\nrow", read["content"][0]["text"])
        listed = server.call("theodore", action="list", path=str(folder))
        self.assertIn("slide-1.jpg", listed["content"][0]["text"])
        script = server.call("theodore", action="write", folder=str(folder), path="go.command", content="open -a Calculator")
        self.assertTrue(script.get("isError"))
        self.assertFalse((folder / "go.command").exists())
        self.assertEqual(server.close(), b"")

    def test_the_licence_copy_lives_only_while_a_command_runs_and_follows_the_main_one(self):
        """The engine reads a copy of the licence in the chat's private home: made from the main licence just before a
        command (after the client accepted the main one) and removed just after; signed out or refused in the main
        state, the next command is refused and no copy is left."""
        self.ready()
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        copy = self.box.home / "state" / "chat" / "state" / "entitlement.json"
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        ok = server.call("theodore", action="run", folder=str(folder), args=["list"])
        self.assertFalse(ok.get("isError"), ok)
        self.assertFalse(copy.exists(), "no copy after the command")
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text("left over from a killed command")
        self.assertStatus(self.client("logout"), "OK", 0)
        self.assertFalse(copy.exists(), "signing out removes a left-over copy with the main licence")
        refused = server.call("theodore", action="run", folder=str(folder), args=["list"])
        self.assertTrue(refused.get("isError"))
        self.assertIn("SIGN_IN_REQUIRED", refused["content"][0]["text"])
        self.assertFalse(copy.exists())
        self.assertEqual(server.close(), b"")

    def test_a_revoked_device_is_refused_on_the_chat_route_too(self):
        self.ready()
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        self.api.revoke_all()
        (self.box.home / "state" / "entitlement.json").unlink()     # what a refusal does to the main licence
        refused = server.call("theodore", action="run", folder=str(folder), args=["list"])
        self.assertTrue(refused.get("isError"))
        self.assertFalse((self.box.home / "state" / "chat" / "state" / "entitlement.json").exists())
        self.assertEqual(server.close(), b"")

    def test_a_plan_without_the_feature_cannot_build(self):
        """The route is a plan feature (chat-build) in the licence: taken off the plan, run and write are refused from
        the next licence; reading the guidance is not affected."""
        self.api.features = [f for f in self.api.features if f != chatbuild.CHAT_BUILD_FEATURE]
        self.ready()
        folder = self.box.tmp / "Documents" / "Board"
        folder.mkdir(parents=True)
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        for arguments in ({"action": "run", "args": ["list"]}, {"action": "write", "path": "deck.js", "content": "x"}):
            answer = server.call("theodore", folder=str(folder), **arguments)
            self.assertTrue(answer.get("isError"), answer)
            self.assertIn("NO_ENTITLEMENT", answer["content"][0]["text"])
        self.assertFalse((folder / "deck.js").exists())
        self.api.features.append(chatbuild.CHAT_BUILD_FEATURE)          # an admin gives the plan the feature
        server2 = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        granted = server2.call("theodore", folder=str(folder), action="run", args=["list"])
        self.assertFalse(granted.get("isError"), "a new server asks the service once and builds")
        self.assertEqual(server2.close(), b"")
        self.assertEqual(server.close(), b"")


class PluginServerInTheApp(McpCase):
    """A Claude account with Theodore's plugin on: the plugin's own server also runs in the app's chat. There it is
    never a second Theodore: no tool when the app runs Theodore's entry, else the chat's one tool. Claude Code keeps
    the plugin's tools."""

    def start(self, client: str, entry: bool):
        home = self.box.tmp / f"home-{client}-{entry}"
        folder = home / "Library" / "Application Support" / "Claude" if sys.platform == "darwin" else home / "AppData" / "Roaming" / "Claude"
        folder.mkdir(parents=True)
        if entry:
            (folder / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {"theodore": {"command": "x", "args": ["mcp", "--for", "claude-desktop"]}}}))
        server = Server(self, env={"HOME": str(home), "APPDATA": str(home / "AppData" / "Roaming")})
        self.addCleanup(lambda: server.proc.poll() is None and server.proc.kill())
        server.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": client, "version": "1"}})
        return server

    @unittest.skipUnless(sys.platform == "darwin", "the app's configuration folder as on macOS")
    def test_one_server_and_one_tool_whichever_way_theodore_reaches_the_chat(self):
        self.sign_in()
        names = lambda server: [t["name"] for t in server.send("tools/list")["result"]["tools"]]
        with_entry = self.start("claude-ai", True)
        self.assertEqual(names(with_entry), [], "the app's own Theodore entry serves the tool: the plugin's server is silent")
        self.assertIn("unknown tool", with_entry.call("specimen", file="cover-01.jpg")["content"][0]["text"])
        without = self.start("Claude Desktop", False)
        self.assertEqual(names(without), ["theodore"], "no entry: the plugin's server is the chat's one tool")
        code = self.start("claude-code", True)
        self.assertEqual(sorted(names(code)), ["guide", "list", "specimen"], "Claude Code keeps the plugin's tools")
        agent = self.start("local-agent-mode-plugin:theodore:theodore", True)      # as the app names the plugin's server
        self.assertEqual(names(agent), [], "the app's agent mode is the app too: silent beside Theodore's entry")
        agent_alone = self.start("local-agent-mode-theodore", False)
        self.assertEqual(names(agent_alone), ["theodore"])
        codex = self.start("codex-mcp-client", True)
        self.assertEqual(sorted(names(codex)), ["guide", "list", "specimen"], "Codex keeps the plugin's tools")
        for server in (with_entry, without, code, agent, agent_alone, codex):
            server.close()
        log = (self.box.home / "logs" / "client.log").read_text(encoding="utf-8")
        self.assertIn("mcp initialize: client local-agent-mode-plugin:theodore:theodore (for plugin)", log)


APP_TELL = ("TELL THE USER: Theodore is set up and ready for your presentations here. To have it used for every presentation "
            'without naming it, say: "From now on, use Theodore for any presentation, deck or slide request. Please remember that."')
PLAIN_TELL = "TELL THE USER: Theodore is set up and ready for your presentations here."


class UseTheodoreTip(unittest.TestCase):
    """The sentence a person can say so that the Claude app's chat uses Theodore for every presentation: told to the
    person by the Claude app's clients only, never asked of the assistant (chatbuild.USE_THEODORE_SENTENCE)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = os.environ.get("THEODORE_HOME"), chatbuild.APP_CLIENT
        os.environ["THEODORE_HOME"] = str(self.tmp / ".theodore")
        core._HOME = None

        def restore():
            if saved[0] is None:
                os.environ.pop("THEODORE_HOME", None)
            else:
                os.environ["THEODORE_HOME"] = saved[0]
            chatbuild.APP_CLIENT = saved[1]
            core._HOME = None
        self.addCleanup(restore)
        self.counter = self.tmp / ".theodore" / "state" / chatbuild.TIP_STATE

    def finish(self) -> str:
        return chatbuild.next_step(["finish", str(self.tmp)], '{"delivered": {"output": "/x/d.pptx"}}', 0)

    def test_the_claude_app_by_its_client_name(self):
        for name in ("claude-ai", "Claude Desktop", "local-agent-mode-theodore", "local-agent-mode-plugin:theodore:theodore",
                     "local-agent-mode"):
            self.assertTrue(chatbuild.is_claude_app(name), name)
        for name in ("claude-code", "Claude-Code", "codex-mcp-client", "codex", "test", "", None, "my-local-agent-mode"):
            self.assertFalse(chatbuild.is_claude_app(name), name)

    def test_the_first_two_decks_delivered_carry_the_tip_then_never_again(self):
        chatbuild.APP_CLIENT = True
        first, second, third = self.finish(), self.finish(), self.finish()
        line = ('Your deck is saved on this computer at /x/d.pptx. If the user had to name Theodore in their request to get '
                'this deck, add: ' + chatbuild.USE_THEODORE_TIP + ' Show it in Finder')
        self.assertIn(line, first)
        self.assertIn(line, second)
        self.assertNotIn(chatbuild.USE_THEODORE_SENTENCE, third)
        self.assertIn("Your deck is saved on this computer at /x/d.pptx. Show it in Finder", third)
        self.assertEqual(json.loads(self.counter.read_text())["decks_with_tip"], 2)
        self.assertNotIn(chatbuild.USE_THEODORE_SENTENCE, self.finish())

    def test_an_unreadable_counter_counts_as_zero_and_a_broken_one_never_fails_the_delivery(self):
        chatbuild.APP_CLIENT = True
        self.counter.parent.mkdir(parents=True)
        for bad in ("not json", '{"decks_with_tip": "two"}', '{"decks_with_tip": -3}', "[]"):
            self.counter.write_text(bad)
            self.assertIn(chatbuild.USE_THEODORE_TIP, self.finish(), bad)
        self.counter.unlink()
        self.counter.mkdir()                    # cannot be read or written: no tip is lost to it, no error either
        done = self.finish()
        self.assertIn("/x/d.pptx", done)

    def test_other_clients_never_get_the_tip_or_count(self):
        chatbuild.APP_CLIENT = False
        for _ in range(3):
            done = self.finish()
            self.assertIn("/x/d.pptx", done)
            self.assertNotIn(chatbuild.USE_THEODORE_SENTENCE, done)
        self.assertFalse(self.counter.exists())

    def test_no_answer_asks_the_assistant_to_keep_anything(self):
        """Every string literal the local server's answer code holds (not its docstrings), and the answers that carry the
        sentence: nothing tells the assistant to save, remember or store a preference, nothing puts a question to the
        user about preferences, and the sentence appears only as what the user can say, in what is told to the user."""
        import ast
        from theodore_client import mcp
        literals = []
        from theodore_client import account, commands, desktop
        for module in (mcp, chatbuild, commands, desktop, account, core):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                          and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
            literals += [node.value for node in ast.walk(tree)
                         if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]
        keep = re.compile(r"\b(remember|memori[sz]e|memory)\b|\b(save|store|keep)\b[^.]{0,40}\b(preference|sentence|setting)", re.I)
        for text in literals:
            if text == chatbuild.USE_THEODORE_SENTENCE:
                continue
            self.assertIsNone(keep.search(text), text)
            if re.search(r"\bask (the user|them)\b", text, re.I):
                self.assertIsNone(re.search(r"prefer|remember|memory|from now on", text, re.I), text)
        self.assertEqual(sum(chatbuild.USE_THEODORE_SENTENCE in t for t in literals), 1, "one constant for the sentence")
        chatbuild.APP_CLIENT = True
        answers = [self.finish(), mcp._check_status().render(), "TELL THE USER: x. " + chatbuild.INSTALL_TIP]
        for answer in answers:
            for line in answer.splitlines():
                if chatbuild.USE_THEODORE_SENTENCE in line:
                    told = re.match(r"(TELL THE USER: |.*\btell the user, in one line: )", line)
                    self.assertIsNotNone(told, line)
                    self.assertRegex(line[told.end():], rf'say( once)?: "{re.escape(chatbuild.USE_THEODORE_SENTENCE)}"')


class UseTheodoreTipThroughTheServer(McpCase):
    """The check answer of the chat's one tool, by the client that started the server."""

    def check(self, client: str, for_host: str = "claude-desktop") -> str:
        server = Server(self, args=("--for", for_host))
        self.addCleanup(lambda: server.proc.poll() is None and server.proc.kill())
        server.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": client, "version": "1"}})
        text = server.call("theodore", action="check")["content"][0]["text"]
        server.close()
        return text

    def test_the_check_tells_the_claude_app_user_the_sentence_and_no_one_else(self):
        self.sign_in()
        ok = ("THEODORE OK: Theodore is set up on this computer and signed in; its design guidance reaches this chat.\n"
              'NEXT: to make or rework a presentation, call theodore with action "begin".\n')
        for client in ("claude-ai", "local-agent-mode-theodore"):
            self.assertEqual(self.check(client).strip(), ok + APP_TELL, client)
        for client, host in (("claude-code", "claude-desktop"), ("codex-mcp-client", "codex"), ("test", "claude-desktop"),
                             ("", "claude-desktop")):
            self.assertEqual(self.check(client, host).strip(), ok + PLAIN_TELL, client)



class DefaultFolder(unittest.TestCase):
    """Where a deck goes when the chat names no folder (lane CHATFOLDER): on a Mac ~/Theodore, since the Documents
    folder is protected and its first touch raises a system prompt naming Theodore's Python; a Mac whose chat route
    ran before keeps Documents/Theodore; elsewhere Documents/Theodore. A folder that cannot be written never ends in a
    bare exit 1."""

    def setUp(self):
        from unittest import mock
        self.mock = mock
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: [os.chmod(p, 0o700) for p in self.tmp.rglob("*") if p.is_dir()])
        self.home = Path(os.path.realpath(self.tmp)) / "home"
        self.home.mkdir()
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "THEODORE_HOME", "THEODORE_CACHE_DIR")}
        os.environ.update({"HOME": str(self.home), "USERPROFILE": str(self.home), "THEODORE_HOME": str(self.home / ".theodore"),
                           "THEODORE_CACHE_DIR": str(self.home / "cache")})
        core._HOME = None
        chatbuild.FOLDERS.clear()
        chatbuild.NOTICE.clear()

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            core._HOME = None
            chatbuild.FOLDERS.clear()
            chatbuild.NOTICE.clear()
        self.addCleanup(restore)
        self.record = self.home / ".theodore" / "state" / chatbuild.FOLDER_STATE

    def on(self, platform: str):
        return self.mock.patch.object(chatbuild.sys, "platform", platform)

    def test_a_fresh_mac_builds_in_theodore_in_the_home_folder_and_keeps_it(self):
        with self.on("darwin"):
            self.assertEqual(chatbuild.check_folder(None), self.home / "Theodore")
            self.assertFalse((self.home / "Documents").exists(), "Documents is never touched")
            self.assertEqual(json.loads(self.record.read_text()), {"default": "home"})
            chatbuild._prepare_sandbox()                     # the chat route runs: the recorded choice stays
            self.assertEqual(chatbuild.default_folder(), self.home / "Theodore")
        self.assertEqual(chatbuild.NOTICE, [])

    def test_a_mac_whose_chat_route_ran_before_keeps_documents_theodore(self):
        (self.home / ".theodore" / "state" / "chat" / "state").mkdir(parents=True)    # 0.4.0's chat route ran here
        with self.on("darwin"):
            self.assertEqual(chatbuild.check_folder(None), self.home / "Documents" / "Theodore")
            self.assertEqual(json.loads(self.record.read_text()), {"default": "documents"})

    def test_windows_and_linux_keep_documents_theodore(self):
        for platform in ("win32", "linux"):
            chatbuild.FOLDERS.clear()
            with self.on(platform):
                self.assertEqual(chatbuild.check_folder(None), self.home / "Documents" / "Theodore", platform)
        self.assertFalse(self.record.exists())

    @unittest.skipIf(IS_WINDOWS or os.geteuid() == 0, "folder permissions as on macOS and Linux")
    def test_a_default_that_cannot_be_written_gives_way_to_theodore_in_the_home_folder_with_a_sentence(self):
        (self.home / "Documents" / "Theodore").mkdir(parents=True)
        os.chmod(self.home / "Documents" / "Theodore", 0o500)       # as macOS answers Don't Allow, or a read-only folder
        with self.on("linux"):
            self.assertEqual(chatbuild.check_folder(None), self.home / "Theodore")
        self.assertEqual(chatbuild.NOTICE, [f"Theodore could not save in {self.home / 'Documents' / 'Theodore'} on this computer "
                                            f"(the system did not allow it), so this deck is going to {self.home / 'Theodore'} instead."])
        self.assertTrue(chatbuild.folder_allowed(self.home / "Theodore"), "the fallback passes the same checks")
        (self.home / "Theodore").rmdir()
        chatbuild.FOLDERS.clear()
        os.chmod(self.home, 0o500)                                  # neither can be written: a status, never exit 1
        try:
            with self.on("linux"), self.assertRaises(core.Status) as caught:
                chatbuild.check_folder(None)
        finally:
            os.chmod(self.home, 0o700)
        self.assertEqual(caught.exception.name, "SETUP_FAILED")
        self.assertIn("ask the user for a folder", caught.exception.next_step)

    @unittest.skipIf(IS_WINDOWS or os.geteuid() == 0, "folder permissions as on macOS and Linux")
    def test_a_named_folder_that_cannot_be_written_is_a_status_that_names_it(self):
        named = self.home / "Documents" / "Board"
        named.mkdir(parents=True)
        os.chmod(named, 0o500)
        with self.on("darwin"), self.assertRaises(core.Status) as caught:
            chatbuild.check_folder(str(named))
        status = caught.exception
        self.assertEqual(status.name, "USAGE")
        self.assertIn(f"the folder {named} cannot be written", status.summary)
        self.assertEqual(status.next_step, "ask the user for another folder, or leave folder out to use Theodore in the user's home folder.")
        self.assertEqual(chatbuild.NOTICE, [])
