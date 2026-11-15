"""Building from a chat (`mcp --for claude-desktop`, actions manual, run, read, write, edit, list): what runs, where it
runs, and the confinement of the deck script (client/theodore_client/chatbuild.py and node_guard.js)."""

from __future__ import annotations

import base64
import json
import os
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
        self.assertEqual(Path(seen["cwd"]).resolve(), (self.box.tmp / "Documents" / "Theodore").resolve(), "an unknown thread: the default")
        session.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": str(self.box.tmp)}}) + "\n")
        home = server.send("tools/call", {"name": "theodore", "arguments": {"action": "run", "args": ["list"]},
                                          "_meta": {"threadId": thread}})["result"]
        seen = json.loads(home["content"][0]["text"].split("\n", 1)[1])
        self.assertEqual(Path(seen["cwd"]).resolve(), (self.box.tmp / "Documents" / "Theodore").resolve(), "a thread in the home folder: the default")
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

    def test_without_a_folder_the_deck_goes_to_documents_theodore(self):
        self.ready()
        server = self.server(args=("--for", "claude-desktop"), env={"HOME": str(self.box.tmp)})
        answer = server.call("theodore", action="run", args=["list"])
        seen = json.loads(answer["content"][0]["text"].split("\n", 1)[1])
        self.assertEqual(Path(seen["cwd"]).resolve(), (self.box.tmp / "Documents" / "Theodore").resolve())
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
        for server in (with_entry, without, code):
            server.close()

