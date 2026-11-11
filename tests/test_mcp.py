"""`theodore mcp`: the local stdio MCP server (guide and specimen tools). Runs the client directly (macOS and Linux)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import CLIENT_DIR, IS_WINDOWS, RUNTIME_ID, ClientCase, FakeApi, Sandbox, cmd_command, engine_zip, fake_runtime_parts

JPEG = b"\xff\xd8\xff\xe0" + b"sheet bytes " * 50 + b"\xff\xd9"
ENTRY = "# Deck design\n\nENTRY-MARKER é—“quoted”\n" + "".join(f"line {i:05d} " + "x" * 70 + "\n" for i in range(800))


class Server:
    """One `theodore mcp` process: send a message, read its answer line."""

    def __init__(self, case: ClientCase, env: dict | None = None):
        self.proc = subprocess.Popen([sys.executable, "-I", str(CLIENT_DIR), "mcp"], env={**case.box.env, **(env or {})},
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(case.box.tmp))
        self.next_id = 0
        self.notifications: list[dict] = []

    def send(self, method: str, params: dict | None = None, notify: bool = False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()
        if notify:
            return None
        while True:                         # notifications the server sends come before or after an answer
            answer = json.loads(self.proc.stdout.readline())
            if "id" in answer:
                break
            self.notifications.append(answer)
        assert answer["id"] == self.next_id, answer
        return answer

    def call(self, name: str, **arguments) -> dict:
        return self.send("tools/call", {"name": name, "arguments": arguments})["result"]

    def close(self) -> bytes:
        self.proc.stdin.close()
        rest = self.proc.stdout.read()
        self.proc.wait(timeout=30)
        self.proc.stderr.close()
        self.proc.stdout.close()
        return rest


class McpCase(ClientCase):
    def server(self, **kwargs) -> Server:
        server = Server(self, **kwargs)
        self.addCleanup(lambda: server.proc.poll() is None and server.proc.kill())
        server.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        server.send("notifications/initialized", notify=True)
        return server


class Handshake(McpCase):
    def test_initialize_list_and_the_size_annotation(self):
        server = Server(self)
        self.addCleanup(lambda: server.proc.poll() is None and server.proc.kill())
        init = server.send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(init["result"]["serverInfo"]["name"], "theodore")
        self.assertIn("tools", init["result"]["capabilities"])
        server.send("notifications/initialized", notify=True)
        self.assertEqual(server.send("ping")["result"], {})
        tools = {t["name"]: t for t in server.send("tools/list")["result"]["tools"]}
        self.assertEqual(sorted(tools), ["guide", "list", "specimen"])
        self.assertEqual(init["result"]["capabilities"]["tools"], {"listChanged": True})
        self.assertNotIn("enum", tools["guide"]["inputSchema"]["properties"]["section"], "signed out: no index, any name")
        self.assertEqual(tools["guide"]["_meta"], {"anthropic/maxResultSizeChars": 400000})
        self.assertEqual(tools["guide"]["inputSchema"]["required"], ["skill"])
        for tool in tools.values():
            self.assertEqual(tool["description"].count("."), 1, "one neutral sentence")
        self.assertEqual(server.send("nonsense/method")["error"]["code"], -32601)
        self.assertEqual(server.close(), b"", "nothing but answers on standard output")


INDEX = {"version": "g1", "sections": [{"name": "craft"}, {"name": "forms"}, {"name": "../bad"}],
         "specimens": [{"file": "cover-01.jpg"}, {"file": "rules-and-ledgers.jpg"}, {"file": "nope.png"}]}


class Listing(McpCase):
    """The published index makes the files visible as a folder listing would: the `list` tool and the enums."""

    def test_list_and_enums_from_the_index(self):
        self.sign_in()
        self.api.guide_index = INDEX
        server = self.server()
        tools = {t["name"]: t for t in server.send("tools/list")["result"]["tools"]}
        self.assertEqual(tools["guide"]["inputSchema"]["properties"]["section"]["enum"], ["craft", "forms"])
        one, many = tools["specimen"]["inputSchema"]["properties"]["file"]["anyOf"]
        self.assertEqual(one["enum"], ["cover-01.jpg", "rules-and-ledgers.jpg"])
        self.assertEqual((many["type"], many["items"]["enum"], many["minItems"], many["maxItems"]),
                         ("array", ["cover-01.jpg", "rules-and-ledgers.jpg"], 1, 6))
        self.assertEqual([t["description"] for t in tools.values()],
                         ["Lists the files of a Theodore skill.", "Returns one file of a Theodore skill, whole.",
                          "Returns specimen sheets as images: one file, or a list of up to 6 (each image after its file name)."])
        listing = server.call("list", skill="deck-design")["content"][0]["text"]
        self.assertEqual(listing, "SKILL.md\nreferences/craft.md\nreferences/forms.md\nspecimens/cover-01.jpg\nspecimens/rules-and-ledgers.jpg\n")

    def test_a_new_guide_version_refreshes_the_index_and_says_so(self):
        self.sign_in()
        self.api.guide_index = dict(INDEX)
        server = self.server()
        server.send("tools/list")
        server.call("guide", skill="deck-design")
        self.assertEqual(server.notifications, [])
        self.api.guide_version = "g2"
        self.api.guide_index = {"version": "g2", "sections": [{"name": "craft"}, {"name": "critique"}], "specimens": []}
        server.call("guide", skill="deck-design")
        server.send("ping")
        self.assertEqual(server.notifications, [{"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}])
        tools = {t["name"]: t for t in server.send("tools/list")["result"]["tools"]}
        self.assertEqual(tools["guide"]["inputSchema"]["properties"]["section"]["enum"], ["craft", "critique"])

    def test_without_the_index_names_still_work(self):
        self.sign_in()
        server = self.server()
        tools = {t["name"]: t for t in server.send("tools/list")["result"]["tools"]}
        self.assertNotIn("enum", tools["specimen"]["inputSchema"]["properties"]["file"]["anyOf"][0])
        self.assertTrue(server.call("list", skill="deck-design")["content"][0]["text"].startswith("THEODORE RETRY: "))
        self.assertNotIn("isError", server.call("guide", skill="deck-design"))

    def test_pptx_lists_the_engine_skill_folder(self):
        self.ready()
        listing = self.server().call("list", skill="pptx")["content"][0]["text"].splitlines()
        self.assertEqual(listing, ["SKILL.md", "pptxgenjs.md", "scripts/pptx.py"])


class Guide(McpCase):
    def test_signed_out_answers_the_status_as_text(self):
        server = self.server()
        result = server.call("guide", skill="deck-design")
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertTrue(text.startswith("THEODORE SIGN_IN_REQUIRED: "), text)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn("/device?code=", text)

    def test_the_whole_document_byte_exact_with_what_the_service_appends(self):
        self.sign_in()
        self.api.guides["deck-design"][""] = ENTRY
        self.api.guides["deck-design"]["craft"] = "CRAFT " * 9000
        self.api.guide_whole_tail = {"": "\nLINK MECHANICS (tool wording).\nLICENCE LINE.\n", "craft": "\nLICENCE LINE.\n"}
        server = self.server()
        result = server.call("guide", skill="deck-design")
        self.assertNotIn("isError", result)
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["text"], ENTRY + self.api.guide_whole_tail[""])
        self.assertGreater(len(result["content"][0]["text"]), 60000)
        section = server.call("guide", skill="deck-design", section="references/craft.md")
        self.assertEqual(section["content"][0]["text"], "CRAFT " * 9000 + "\nLICENCE LINE.\n")
        self.assertEqual([q.get("form") for q in self.api.guide_queries], ["whole", "whole"])
        self.assertEqual({q.get("bindings") for q in self.api.guide_queries}, {"tool"})
        missing = server.call("guide", skill="deck-design", section="nosuch")
        self.assertTrue(missing["content"][0]["text"].startswith("THEODORE USAGE: "))

    def test_an_older_service_that_pages_is_joined_in_order(self):
        self.sign_in()
        self.api.guide_whole = False
        self.api.guides["deck-design"]["craft"] = ENTRY
        server = self.server()
        text = server.call("guide", skill="deck-design", section="craft")["content"][0]["text"]
        self.assertEqual(text, ENTRY)
        self.assertEqual([q.get("part") for q in self.api.guide_queries], [None, "2", "3"])

    def test_revoked_device_gets_the_status_and_the_server_keeps_running(self):
        self.sign_in()
        server = self.server()
        self.assertNotIn("isError", server.call("guide", skill="deck-design"))
        self.api.revoke_all()
        result = server.call("guide", skill="deck-design")
        self.assertTrue(result["content"][0]["text"].startswith("THEODORE SIGN_IN_REQUIRED: "), result)
        self.assertEqual(server.send("ping")["result"], {})

    def test_pptx_manual_comes_from_the_engine(self):
        self.ready()
        text = self.server().call("guide", skill="pptx")["content"][0]["text"]
        self.assertIn(f"Fake manual for engine {self.ENGINE_VERSION}.", text)
        self.assertIn("=== END theodore pptx SKILL.md", text)


class Specimen(McpCase):
    def test_an_image_block_and_nothing_on_disk(self):
        self.sign_in()
        self.api.specimens = {"cover-01.jpg": JPEG}
        self.api.guides["deck-design"][""] = ENTRY
        temp = Path(self.box.env["TMPDIR"])
        before = sorted(p.name for p in temp.iterdir())
        server = self.server()
        result = server.call("specimen", file="cover-01.jpg")
        self.assertEqual(len(result["content"]), 1)
        block = result["content"][0]
        self.assertEqual((block["type"], block["mimeType"]), ("image", "image/jpeg"))
        self.assertEqual(base64.b64decode(block["data"]), JPEG)
        server.call("guide", skill="deck-design")
        server.close()
        self.assertEqual(sorted(p.name for p in temp.iterdir()), before, "no folder in the temporary root")
        self.assertFalse((self.box.home / "state" / "specimens.txt").exists())
        for root in (self.box.home, temp, self.box.user_home):
            for path in root.rglob("*"):
                if path.is_file() and not path.is_symlink() and "engines" not in path.parts:
                    data = path.read_bytes()
                    self.assertNotIn(b"sheet bytes sheet bytes", data, path)
                    self.assertNotIn(b"ENTRY-MARKER", data, path)

    def test_a_list_of_up_to_six_comes_back_as_name_then_image_in_order(self):
        self.sign_in()
        names = [f"s{i}.jpg" for i in range(6)]
        self.api.specimens = {name: JPEG + name.encode() for name in names}
        server = self.server()
        asked = [names[3], names[0], names[5], names[1], names[4], names[2]]
        before = len([r for r in self.api.requests if r["path"].startswith("/v1/guide/specimens/")])
        content = server.call("specimen", file=asked)["content"]
        self.assertEqual(len(content), 12)
        for i, name in enumerate(asked):
            self.assertEqual(content[2 * i], {"type": "text", "text": name})
            self.assertEqual((content[2 * i + 1]["type"], content[2 * i + 1]["mimeType"]), ("image", "image/jpeg"))
            self.assertEqual(base64.b64decode(content[2 * i + 1]["data"]), JPEG + name.encode())
        fetched = [r for r in self.api.requests if r["path"].startswith("/v1/guide/specimens/")][before:]
        self.assertEqual(len(fetched), 6, "each sheet is one fetch")
        # one file, as before: the image alone
        self.assertEqual([b["type"] for b in server.call("specimen", file=names[0])["content"]], ["image"])
        # a list of one is the list form
        self.assertEqual([b["type"] for b in server.call("specimen", file=[names[0]])["content"]], ["text", "image"])
        # more than six, or none: refused, naming the limit, nothing fetched
        before = len(self.api.requests)
        for bad in (names + ["s6.jpg"], []):
            text = server.call("specimen", file=bad)["content"][0]["text"]
            self.assertTrue(text.startswith("THEODORE USAGE: "), text)
            self.assertIn("1 to 6 files", text)
        self.assertEqual(len(self.api.requests), before)
        # one bad name in a list: refused before any fetch
        self.assertTrue(server.call("specimen", file=[names[0], "../x"])["content"][0]["text"].startswith("THEODORE USAGE: "))
        self.assertEqual(len(self.api.requests), before)

    def test_refusals_are_short_texts(self):
        self.sign_in()
        server = self.server()
        self.assertTrue(server.call("specimen", file="../etc/passwd")["content"][0]["text"].startswith("THEODORE USAGE: "))
        self.api.specimens = {"cover-01.jpg": JPEG}
        self.api.fail_with = (429, "rate_limited")
        text = server.call("specimen", file="cover-01.jpg")["content"][0]["text"]
        self.assertTrue(text.startswith("THEODORE RETRY: "), text)


class SkillBody(ClientCase):
    """`theodore skill <name>`: the instructions as a skill file's body, injected by the host; always exit 0."""

    def body(self, name: str, env: dict | None = None):
        result = self.client("skill", name, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_the_reporting_line_names_this_launcher(self):
        """The host ran `theodore skill`, not the agent: "the launcher you used" (or the service's tool-form wording)
        becomes this launcher's path, the one the reporting command runs."""
        self.sign_in()
        for line in ("Run the Theodore launcher you used to get these instructions with:",
                     "Run the Theodore launcher (scripts/theodore in the folder of either Theodore skill; scripts\\theodore.cmd on Windows) with:"):
            self.api.guides["deck-design"][""] = f"# Deck design\n\nBody.\n\n{line}\n\nreport --kind bug\n"
            for env in ({"CLAUDECODE": "1"}, {}):
                out = self.body("deck-design", env=env)
                self.assertNotIn("launcher you used", out)
                self.assertNotIn("either Theodore skill", out)
                self.assertRegex(out, r"\nRun .*theodore\"? with:\n")

    def test_signed_out_and_unknown_are_statuses_with_exit_0(self):
        out = self.body("deck-design")
        self.assertTrue(out.startswith("THEODORE SIGN_IN_REQUIRED: "), out)
        self.assertEqual(len(out.splitlines()), 3)
        self.assertTrue(self.body("xlsx").startswith("THEODORE USAGE: "))

    def test_deck_design_is_the_whole_entry_without_front_matter_or_framing(self):
        self.sign_in()
        text = "# Deck design\n\nENTRY \u2014 body\n" + "line\n" * 2000
        self.api.guides["deck-design"][""] = "---\nname: deck-design\ndescription: x\n---\n\n" + text
        self.api.guide_whole_tail = {"": "\nLINK MECHANICS.\nLICENCE LINE.\n"}
        out = self.body("deck-design", env={"CLAUDECODE": "1"})
        self.assertEqual(out, text + "\nLINK MECHANICS.\nLICENCE LINE.\n")
        self.assertNotIn("=== ", out)
        self.assertEqual(self.api.guide_queries[-1], {"form": "whole", "bindings": "tool"}, "Claude Code has the tool: its wording")
        self.body("deck-design")
        self.assertEqual(self.api.guide_queries[-1], {"form": "whole"}, "elsewhere the launcher's wording")

    def test_an_entry_past_the_limit_is_cut_on_a_line_with_a_pointer(self):
        self.sign_in()
        self.api.guides["deck-design"][""] = "".join(f"line {i:05d} " + "y" * 60 + "\n" for i in range(600))
        out = self.body("deck-design", env={"CLAUDECODE": "1"})
        self.assertLessEqual(len(out), 28000)
        self.assertTrue(out.endswith('[The rest of this document: call the Theodore guide tool with skill "deck-design"]\n'), out[-200:])
        self.assertTrue(out.split("\n[The rest")[0].endswith("y\n"), "cut at a line end")

    def test_pptx_is_the_engine_manual_with_the_bindings(self):
        self.ready()
        out = self.body("pptx")
        launcher = f'"{self.box.home / "bin" / "theodore"}"'
        self.assertTrue(out.startswith(f"Bindings: pptx <verb>  ->  {launcher} pptx <verb>\n"), out[:200])
        self.assertIn(f"# PPTX Skill\n\nFake manual for engine {self.ENGINE_VERSION}.\n", out)
        self.assertNotIn("name: pptx", out, "the engine file's front matter is not part of the body")
        self.assertNotIn("=== ", out)


@unittest.skipUnless(os.environ.get("THEODORE_LIVE_HOME"), "set THEODORE_LIVE_HOME to a signed-in Theodore folder to check the published entry")
class PublishedEntrySize(unittest.TestCase):
    def test_the_published_entry_stays_well_under_the_injection_limit(self):
        home = Path(os.environ["THEODORE_LIVE_HOME"])
        client = home / "client" / (home / "client" / "current.txt").read_text().strip()
        python = home / "runtime" / (home / "runtime" / "current.txt").read_text().strip() / "python" / "bin" / "python3"
        done = subprocess.run([str(python), "-I", str(client), "skill", "deck-design"], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "THEODORE_HOME": str(home), "CLAUDECODE": "1"})
        self.assertFalse(done.stdout.startswith("THEODORE "), done.stdout[:300])
        self.assertLess(len(done.stdout), 26000, "the deck-design entry nears the ~30,000-character limit of injected skill content")


@unittest.skipIf(IS_WINDOWS, "POSIX launcher")
class ThroughTheLauncher(unittest.TestCase):
    def test_skill_before_setup_is_a_status_with_exit_0(self):
        api = FakeApi().start()
        self.addCleanup(api.stop)
        box = Sandbox(api)
        self.addCleanup(box.close)
        done = subprocess.run(["/bin/sh", str(box.launcher), "skill", "deck-design"], env={**box.env, "THEODORE_BOOTSTRAP_WAIT": "1"},
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.startswith("THEODORE "), done.stdout)
        self.assertEqual(done.stderr, "")
        box.write_launcher_json(manifest_key_1_kid="manifest-unset")              # even a refusal to start exits 0
        done = subprocess.run(["/bin/sh", str(box.launcher), "skill", "pptx"], env=box.env, capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout[:18]), (0, "THEODORE INTERNAL:"))

    def test_before_setup_standard_output_stays_clean(self):
        api = FakeApi().start()
        self.addCleanup(api.stop)
        box = Sandbox(api)
        self.addCleanup(box.close)
        done = subprocess.run(["/bin/sh", str(box.launcher), "mcp"], env={**box.env, "THEODORE_BOOTSTRAP_WAIT": "1"},
                              input=b"", capture_output=True, timeout=120)
        self.assertEqual(done.stdout, b"", "the protocol stream gets no status")
        self.assertIn(b"THEODORE ", done.stderr)


@unittest.skipUnless(IS_WINDOWS, "the batch launcher")
class WindowsLauncher(unittest.TestCase):
    """`theodore.cmd mcp` keeps standard output for the protocol and serves once set up (named without its extension,
    which Windows resolves to theodore.cmd beside it)."""

    def setUp(self):
        self.api = FakeApi().start()
        self.addCleanup(self.api.stop)
        self.api.publish_runtime(RUNTIME_ID, fake_runtime_parts())
        self.api.publish_engine("1.0.0-rc.3", engine_zip("1.0.0-rc.3"))
        self.box = Sandbox(self.api)
        self.addCleanup(self.box.close)

    def _mcp(self, env=None):
        command = cmd_command(str(self.box.launcher.with_suffix("")), "mcp")      # no extension, as the host spawns it
        return subprocess.Popen(command, env={**self.box.env, **(env or {})}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=str(self.box.tmp))

    def test_the_skill_body_with_characters_outside_the_code_page(self):
        self.box.run_launcher("version", timeout=600)
        self.box.wait_jobs(600)
        self.api.issue_activation_code("ABCD-1234")
        self.assertEqual(self.box.run_launcher("login", "--code", "ABCD-1234", timeout=600).returncode, 0)
        self.api.guides["deck-design"][""] = "# Deck design\n\nA \u2192 B \u2014 \u201cquoted\u201d\n"
        done = subprocess.run(cmd_command(str(self.box.launcher), "skill", "deck-design"), env=self.box.env, capture_output=True,
                              timeout=600, cwd=str(self.box.tmp))
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(done.stdout.decode("utf-8"), "# Deck design\n\nA \u2192 B \u2014 \u201cquoted\u201d\n".replace("\n", "\r\n")
                         if b"\r\n" in done.stdout else "# Deck design\n\nA \u2192 B \u2014 \u201cquoted\u201d\n")

    def test_before_setup_standard_output_stays_clean_then_the_server_answers(self):
        proc = self._mcp({"THEODORE_BOOTSTRAP_WAIT": "1"})
        out, err = proc.communicate(b"", timeout=600)
        self.assertNotIn(b"THEODORE ", out, "the protocol stream gets no status")
        self.box.wait_jobs(600)
        self.api.issue_activation_code("ABCD-1234")
        self.assertEqual(self.box.run_launcher("login", "--code", "ABCD-1234", timeout=600).returncode, 0)
        proc = self._mcp()
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        out, _err = proc.communicate((json.dumps(message) + "\n" + json.dumps(listing) + "\n").encode(), timeout=300)
        answers = [json.loads(line) for line in out.decode().splitlines() if line.strip()]
        self.assertEqual([a["id"] for a in answers], [1, 2], out)
        self.assertEqual(answers[0]["result"]["serverInfo"]["name"], "theodore")
        self.assertEqual(sorted(t["name"] for t in answers[1]["result"]["tools"]), ["guide", "list", "specimen"])


if __name__ == "__main__":
    unittest.main()
