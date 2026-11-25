"""`theodore mcp`: the local stdio MCP server (guide and specimen tools). Runs the client directly (macOS and Linux)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import CLIENT_DIR, IS_WINDOWS, RUNTIME_ID, ClientCase, FakeApi, Sandbox, cmd_command, engine_zip, fake_runtime_parts
from theodore_client import api  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"sheet bytes " * 50 + b"\xff\xd9"
ENTRY = "# Deck design\n\nENTRY-MARKER é—“quoted”\n" + "".join(f"line {i:05d} " + "x" * 70 + "\n" for i in range(800))


class Server:
    """One `theodore mcp` process: send a message, read its answer line."""

    def __init__(self, case: ClientCase, env: dict | None = None, args: tuple = ()):
        self.proc = subprocess.Popen([sys.executable, "-I", str(CLIENT_DIR), "mcp", *args], env={**case.box.env, **(env or {})},
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
        self.assertEqual(tools["guide"]["inputSchema"]["properties"]["section"]["anyOf"][0]["enum"], ["craft", "forms"])
        one, many = tools["specimen"]["inputSchema"]["properties"]["file"]["anyOf"]
        code = {"type": "string", "pattern": "^[A-Z]{2}[0-9]{1,3}$"}         # a close-up's bare code (P042b)
        worked = {"type": "string", "pattern": "^(specimens/)?worked-[a-z0-9-]+\\.jpg$"}   # never listed (0.4.32)
        self.assertEqual(one["anyOf"], [{"type": "string", "enum": ["cover-01.jpg", "rules-and-ledgers.jpg"]}, code, worked])
        self.assertEqual((many["type"], many["items"]["anyOf"][0]["enum"], many["items"]["anyOf"][1], many["minItems"], many["maxItems"]),
                         ("array", ["cover-01.jpg", "rules-and-ledgers.jpg"], code, 1, 6))
        self.assertEqual(many["items"]["anyOf"][2], worked)
        self.assertEqual(tools["guide"]["inputSchema"]["properties"]["role"],
                         {"type": "string", "enum": ["lead", "slide"], "description": '"slide" for an agent given one slide.'})
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
        self.assertEqual(tools["guide"]["inputSchema"]["properties"]["section"]["anyOf"][0]["enum"], ["craft", "critique"])

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

    # P042b: the reader's role. "slide" is sent and the text comes as composed for it; held per role.
    def test_role_slide_is_sent_and_held_apart_from_the_lead_text(self):
        self.sign_in()
        self.api.guides["deck-design"]["craft"] = "Intro.\n<!-- theodore:if lead -->\nLEAD ONLY.\n<!-- theodore:end -->\nShared.\n"
        server = self.server()
        lead = server.call("guide", skill="deck-design", section="craft")["content"][0]["text"]
        slide = server.call("guide", skill="deck-design", section="craft", role="slide")["content"][0]["text"]
        self.assertEqual(lead, "Intro.\nLEAD ONLY.\nShared.\n")
        self.assertEqual(slide, "Intro.\nShared.\n")
        self.assertEqual([q.get("role") for q in self.api.guide_queries], [None, "slide"])
        again = server.call("guide", skill="deck-design", section="craft", role="lead")["content"][0]["text"]
        self.assertEqual(again, lead)
        self.assertNotIn("role", self.api.guide_queries[-1], "lead is the default: nothing is sent")
        wrong = server.call("guide", skill="deck-design", section="craft", role="boss")
        self.assertTrue(wrong["content"][0]["text"].startswith("THEODORE USAGE: "), wrong)

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

    def test_pptx_manual_says_when_the_served_entry_could_not_be_reached(self):
        """Lane STUDIO: the tool server's pptx guide prints what `guide pptx` prints, so a served entry that could not
        be reached after every attempt is named first, never dropped silently."""
        self.ready()
        self.api.guides["pptx"][""] = "SERVED-POINTER.\n"
        self.api.guide_delays["pptx"] = [1.2, 1.2, 1.2]
        text = self.server(env={"THEODORE_TEST_DIRECTIONS_TIMEOUTS": "0.5,0.5,0.5"}).call("guide", skill="pptx")["content"][0]["text"]
        self.assertTrue(text.splitlines()[1].startswith("THEODORE OFFLINE: Theodore's online part for pptx could not be reached ("), text[:400])
        self.assertNotIn("SERVED-POINTER", text)
        self.assertIn(f"Fake manual for engine {self.ENGINE_VERSION}.", text)

    def test_pptx_manual_is_composed_for_the_role(self):
        """The engine's manual carries role markers on disk; the tool composes it as `guide pptx [--role slide]` does."""
        self.ready()
        (self.box.home / "engines" / self.ENGINE_VERSION / "skills" / "pptx" / "SKILL.md").write_text(
            "# Manual\n<!-- theodore:if lead -->\nSend the reviewer message.\n<!-- theodore:end -->\nFinish.\n")
        server = self.server()
        lead = server.call("guide", skill="pptx")["content"][0]["text"]
        slide = server.call("guide", skill="pptx", role="slide")["content"][0]["text"]
        self.assertIn("# Manual\nSend the reviewer message.\nFinish.\n", lead)
        self.assertIn("# Manual\nFinish.\n", slide)
        self.assertNotIn("theodore:", lead + slide)

    def test_pptx_manual_comes_from_the_engine(self):
        self.ready()
        text = self.server().call("guide", skill="pptx")["content"][0]["text"]
        self.assertIn(f"Fake manual for engine {self.ENGINE_VERSION}.", text)
        self.assertIn("=== END theodore pptx SKILL.md", text)


    def test_a_served_pptx_section_is_fetched_whole_and_a_missing_one_is_a_clean_usage(self):
        """Lane STUDIO: a pptx section the service serves to this account (the rest of the manual is on disk). Against
        a service that does not serve it (an older one, or an account without it), the same clean answer as any
        missing section."""
        self.sign_in()
        self.api.guides["pptx"]["served-part"] = "SERVED PPTX SECTION, see [charts](references/pptxgenjs-charts.md)\n"
        server = self.server()
        text = server.call("guide", skill="pptx", section="served-part")["content"][0]["text"]
        self.assertEqual(text, "SERVED PPTX SECTION, see [charts](references/pptxgenjs-charts.md)\n")
        self.assertEqual(self.api.guide_queries[-1], {"form": "whole", "bindings": "tool"})
        missing = server.call("guide", skill="pptx", section="not-served")
        self.assertTrue(missing["isError"])
        first = missing["content"][0]["text"].splitlines()[0]
        self.assertEqual(first, "THEODORE USAGE: guide pptx has no section 'not-served'.")
        self.assertEqual(server.send("ping")["result"], {})

    def test_the_guide_tool_accepts_a_section_outside_the_deck_design_listing(self):
        from theodore_client import mcp
        tool = next(t for t in mcp.tools({"sections": ["craft"], "specimens": []}) if t["name"] == "guide")
        section = tool["inputSchema"]["properties"]["section"]
        self.assertEqual(section["anyOf"][0]["enum"], ["craft"])
        self.assertEqual(section["anyOf"][1], {"type": "string"})


class ClaudeDesktop(McpCase):
    """`mcp --for claude-desktop`: the entry Theodore writes in Claude Desktop's configuration (desktop.py)."""

    # The service's tool-form link sentence as the backend composes it (localLinkBindings, tool form), word for word.
    SERVICE_LINKS = ('Links in this guidance are read with the Theodore tools: a link written references/<name>.md or <name>.md '
                     'is read by calling this same Theodore tool guide with section "<name>"{role}, and a link written '
                     'specimens/<file>.jpg is opened by calling the Theodore tool specimen with file "<file>".')

    def test_the_link_sentence_is_rewritten_for_the_lead_and_for_a_slide(self):
        self.sign_in()
        for role, words in ((None, ""), ("slide", ' and role "slide"')):
            with self.subTest(role=role):
                self.api.guides["deck-design"]["craft"] = "CRAFT TEXT\n" + self.SERVICE_LINKS.format(role=words) + "\n"
                server = self.server(args=("--for", "claude-desktop"))
                text = server.call("theodore", action="guide", section="craft", **({"role": role} if role else {}))["content"][0]["text"]
                server.close()
                self.assertNotIn("Theodore tools:", text, "the service's sentence is replaced")
                self.assertIn('read by calling theodore with action "guide" and section "<name>"' + words + ", and a link", text)
                self.assertIn('opened by calling theodore with action "specimen" and file "<file>"', text)

    def test_guide_reaches_a_section_of_the_build_instructions(self):
        """TR-G78T5N: the build manual sends the assistant to its own served section (slide-parts); a chat has neither a
        launcher nor a skill to name, and the action answered for deck-design only. A name the design guidance does not
        have is looked for in the build instructions; a name neither has still gets the answer that lists the sections."""
        self.sign_in()
        self.api.guides["deck-design"]["craft"] = "CRAFT TEXT\n"
        self.api.guides.setdefault("pptx", {})["slide-parts"] = "SLIDE PARTS TEXT\n"
        server = self.server(args=("--for", "claude-desktop"))
        parts = server.call("theodore", action="guide", section="slide-parts")
        self.assertFalse(parts.get("isError"), parts)
        self.assertIn("SLIDE PARTS TEXT", parts["content"][0]["text"])
        self.assertIn("CRAFT TEXT", server.call("theodore", action="guide", section="craft")["content"][0]["text"])
        neither = server.call("theodore", action="guide", section="no-such-part")
        self.assertTrue(neither.get("isError"))
        self.assertIn("guide deck-design has no section 'no-such-part'", neither["content"][0]["text"])
        server.close()

    def test_the_build_manuals_launcher_sentences_are_said_for_this_tool(self):
        """The build manual comes as the launcher prints it. From a chat, the two sentences that name a launcher or a
        tool with a skill name this tool's guide action instead (the wording is the service's and the engine's)."""
        from theodore_client import mcp
        links = ("Links in this guidance are read through the same launcher: a link written references/<name>.md or <name>.md "
                 "is the section printed by: guide pptx <name>{flag}, and a link written specimens/<file>.jpg is the sheet "
                 "fetched by: specimen <file>.")
        entry = ('Before you build any of these, call the Theodore guide tool with skill "pptx" and section "slide-parts" (or run '
                 "the launcher: `guide pptx slide-parts`), read it whole and work as it says.")
        text = mcp.desktop_text(entry + "\n\n# Manual\n\n" + links.format(flag="") + "\n")
        self.assertIn('call theodore with action "guide" and section "slide-parts", read it whole', text)
        self.assertIn(mcp.DESKTOP_BINDINGS, text)
        self.assertNotIn("launcher", text)
        self.assertNotIn("skill", text)
        self.assertIn(mcp.DESKTOP_BINDINGS_SLIDE, mcp.desktop_text(links.format(flag=" --role slide")))

    def test_one_tool_named_theodore_serves_every_action(self):
        """The app asks for an approval per tool of a local server: exactly one tool, so "Always allow" is clicked once."""
        from theodore_client import mcp
        self.sign_in()
        self.api.guide_index = INDEX
        self.api.guides["deck-design"][""] = ENTRY + mcp.SERVICE_TOOL_BINDINGS + "\n"
        self.api.guides["deck-design"]["craft"] = ("CRAFT TEXT\n" + mcp.SERVICE_LAUNCHER_LINES[0] + " report --kind bug\n"
                                                    "or call the `theodore_preference_add` tool.\n")
        self.api.specimens = {"cover-01.jpg": JPEG}
        server = self.server(args=("--for", "claude-desktop"))
        tools = server.send("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["theodore"])
        tool = tools[0]
        self.assertTrue(tool["title"])
        for phrase in ("call this first", "before any", "before built", "instead of", "built-in", "this computer", "its guidance on"):
            self.assertNotIn(phrase, tool["description"].lower(), "no routing over other tools, nothing on how Theodore works")
        for phrase in ("presentation", "deck", "slide", "set up or check"):
            self.assertIn(phrase, tool["description"].lower())
        props = tool["inputSchema"]["properties"]
        self.assertEqual(tool["inputSchema"]["required"], ["action"])
        self.assertEqual(props["action"]["enum"], list(mcp.DESKTOP_ACTIONS))
        self.assertEqual(props["section"]["anyOf"][0]["enum"], ["craft", "forms"])
        self.assertNotIn("skill", props)
        begin = server.call("theodore", action="begin")["content"][0]["text"]
        self.assertTrue(begin.startswith(ENTRY))
        self.assertIn(mcp.DESKTOP_BINDINGS, begin)
        self.assertNotIn(mcp.SERVICE_TOOL_BINDINGS, begin)
        craft = server.call("theodore", action="guide", section="craft")["content"][0]["text"]
        self.assertIn("CRAFT TEXT", craft)
        self.assertIn(mcp.DESKTOP_LAUNCHER_LINE, craft)
        self.assertIn('the `theodore` tool with action "preference_add"', craft)
        image = server.call("theodore", action="specimen", file="cover-01.jpg")["content"][0]
        self.assertEqual(base64.b64decode(image["data"]), JPEG)
        self.assertIn("tool", {q.get("bindings") for q in self.api.guide_queries})
        added = server.call("theodore", action="preference_add", text="Navy headers")["content"][0]["text"]
        self.assertIn("THEODORE OK: preference saved", added)
        listed = server.call("theodore", action="preference_list")["content"][0]["text"]
        self.assertIn("Navy headers", listed)
        report = server.call("theodore", action="report", kind="feedback", title="Lovely deck", details="It was quick",
                             origin="user_requested")["content"][0]["text"]
        self.assertIn("THEODORE OK: report filed", report)
        self.assertEqual(len(self.api.reports), 1)
        check = server.call("theodore", action="check")["content"][0]["text"]
        self.assertIn("THEODORE OK", check)
        self.assertIn('action "begin"', check)
        wrong = server.call("theodore", action="guide")
        self.assertTrue(wrong.get("isError"))
        for old in ("theodore_begin", "theodore_guide", "theodore_specimen", "guide"):
            self.assertIn("unknown tool", server.call(old, skill="deck-design")["content"][0]["text"])
        everything = begin + craft + added + listed + report + check + wrong["content"][0]["text"]
        for old in ("theodore_begin", "theodore_guide", "theodore_specimen", "theodore_report", "theodore_preference",
                    "Theodore tool guide", "Theodore tool specimen", "Theodore launcher"):
            self.assertNotIn(old, everything)
        self.assertEqual(server.close(), b"")

    def test_the_begin_answer_names_the_tool_also_when_the_service_sent_no_bindings(self):
        from theodore_client import mcp
        self.sign_in()
        self.api.guides["deck-design"][""] = ENTRY
        server = self.server(args=("--for", "claude-desktop"))
        from theodore_client import chatbuild
        begin = server.call("theodore", action="begin")["content"][0]["text"].rstrip()
        self.assertIn(mcp.DESKTOP_BINDINGS, begin)
        self.assertTrue(begin.endswith(chatbuild.BUILD_POINTER), "the build with the engine is the last thing begin says")
        server.close()

    def test_a_pointer_to_the_remote_connector_keeps_its_tool_names(self):
        from theodore_client import mcp, core
        mcp.FOR = mcp.DESKTOP
        try:
            text = mcp.desktop_text(core.SANDBOX_NETWORK_NEXT)
        finally:
            mcp.FOR = None
        self.assertIn("connector's theodore_begin tool", text)

    def test_codex_gets_the_same_one_tool(self):
        """Lane CHATGPT: Codex's plugin server (mcp.json, `--for codex`) is the one tool, marked neither destructive nor
        open-world, which Codex calls without asking."""
        self.sign_in()
        self.api.guides["deck-design"][""] = ENTRY
        server = self.server(args=("--for", "codex"))
        tools = server.send("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["theodore"])
        self.assertIs(tools[0]["annotations"]["destructiveHint"], False)
        self.assertIs(tools[0]["annotations"]["openWorldHint"], False)
        self.assertTrue(server.call("theodore", action="begin")["content"][0]["text"].startswith(ENTRY))
        server.close()

    def test_without_the_flag_the_plugin_server_is_unchanged(self):
        server = self.server(args=("--for", "something-else"))
        self.assertEqual(sorted(t["name"] for t in server.send("tools/list")["result"]["tools"]), ["guide", "list", "specimen"])


class Specimen(McpCase):
    def test_a_bare_code_is_its_close_up_and_shares_the_held_copy(self):
        self.sign_in()
        self.api.specimens = {"HC3.jpg": JPEG + b"HC3", "cover-01.jpg": JPEG}
        server = self.server()
        one = server.call("specimen", file="HC3")["content"]
        self.assertEqual(base64.b64decode(one[0]["data"]), JPEG + b"HC3")
        many = server.call("specimen", file=["HC3.jpg", "cover-01.jpg", "HC3"])["content"]
        self.assertEqual([b["text"] for b in many if b["type"] == "text"], ["HC3.jpg", "cover-01.jpg", "HC3.jpg"])
        self.assertEqual(self.api.served["specimen", "HC3.jpg", 200], 1, "a code and its file name are one held sheet")
        self.assertTrue(server.call("specimen", file="hc3")["content"][0]["text"].startswith("THEODORE USAGE: "))

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


SHEETS = [f"sheet-{i:02d}.jpg" for i in range(14)]


class Reuse(McpCase):
    """One server for a session (Claude Code's sub-agents call the same process). Served online on every call (C6, 24
    November 2026): each held sheet is asked for again in every call that returns it, conditionally, and the service
    answers 304; its bytes stay in memory only; a refusal drops everything."""

    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {name: JPEG + name.encode() for name in SHEETS}
        self.api.guide_index = {"version": "g1", "sections": [{"name": "craft"}], "specimens": [{"file": n} for n in SHEETS]}

    def sheet_answers(self, status: int) -> int:
        return sum(v for (route, _n, code), v in self.api.served.items() if route == "specimen" and code == status)

    def test_several_callers_fetch_each_sheet_once_and_every_call_asks_again(self):
        server = self.server()
        server.send("tools/list")
        calls = [SHEETS[0:6], SHEETS[3:9], SHEETS[6:12], SHEETS[8:14], SHEETS[0:3] + SHEETS[10:13], SHEETS[1:7], [SHEETS[5]]]
        for asked in calls * 3:
            content = server.call("specimen", file=asked)["content"]
            images = [base64.b64decode(b["data"]) for b in content if b["type"] == "image"]
            self.assertEqual(images, [JPEG + name.encode() for name in asked])
        for name in SHEETS:
            self.assertEqual(self.api.served["specimen", name, 200], 1, name)
        asked = sum(len(c) for c in calls * 3)
        self.assertEqual(self.sheet_answers(304), asked - len(SHEETS), "every later call asks again: 304, served from memory")
        self.assertEqual(self.api.served["index", "", 200], 1)

    def test_a_held_sheet_is_asked_for_in_every_call_that_returns_it(self):
        server = self.server()
        server.send("tools/list")
        server.call("specimen", file=SHEETS[0:6])
        server.call("specimen", file=SHEETS[0:6])
        server.call("specimen", file=SHEETS[0:3])
        self.assertEqual(self.sheet_answers(200), 6)
        self.assertEqual(self.sheet_answers(304), 9, "no window in which a held sheet is served without the service")

    def test_a_revoked_device_is_refused_at_its_next_call_and_nothing_held_is_served(self):
        server = self.server()
        server.send("tools/list")
        server.call("specimen", file=SHEETS[0:2])
        self.api.revoke_all()
        result = server.call("specimen", file=SHEETS[0])
        self.assertTrue(result["content"][0]["text"].startswith("THEODORE SIGN_IN_REQUIRED: "), result)
        self.assertNotIn("image", [b["type"] for b in result["content"]])
        for inst in self.api.installations.values():           # signed in again: nothing was kept, fetched afresh
            inst["revoked"] = False
        self.sign_in()
        self.assertEqual([b["type"] for b in server.call("specimen", file=SHEETS[0])["content"]], ["image"])
        self.assertEqual(self.api.served["specimen", SHEETS[0], 200], 2)

    def test_a_changed_sheet_comes_new_at_the_next_call(self):
        server = self.server()
        server.send("tools/list")
        server.call("specimen", file=SHEETS[0:2])
        self.api.specimens[SHEETS[0]] = JPEG + b"v2"
        content = server.call("specimen", file=SHEETS[0:2])["content"]
        self.assertEqual(base64.b64decode(content[1]["data"]), JPEG + b"v2")
        self.assertEqual(self.api.served["specimen", SHEETS[0], 200], 2, "its ETag no longer matches: sent again")
        self.assertEqual(self.api.served["specimen", SHEETS[1], 304], 1, "the unchanged one: 304")

    def test_without_the_index_each_held_sheet_is_checked_on_its_own(self):
        self.api.guide_index = None
        server = self.server()
        server.call("specimen", file=SHEETS[0:3])
        server.call("specimen", file=SHEETS[0:3])
        self.assertEqual(self.sheet_answers(200), 3)
        self.assertEqual(self.sheet_answers(304), 3)

    def test_a_held_guide_text_is_asked_for_conditionally_every_time(self):
        self.api.guides["deck-design"]["craft"] = "CRAFT TEXT\n"
        server = self.server()
        texts = [server.call("guide", skill="deck-design", section="craft")["content"][0]["text"] for _ in range(4)]
        self.assertEqual(set(texts), {"CRAFT TEXT\n"})
        self.assertEqual((self.api.served["guide", "craft", 200], self.api.served["guide", "craft", 304]), (1, 3))
        self.api.revoke_all()
        result = server.call("guide", skill="deck-design", section="craft")
        self.assertTrue(result["content"][0]["text"].startswith("THEODORE SIGN_IN_REQUIRED: "), "refused at once")


class SideBySide(McpCase):
    """0.4.34: every tools/call runs in a thread of its own and answers when it is done, over kept-alive connections; a
    lead and its designers no longer queue behind each other's network waits (five calls sent at once finished 0.84 to
    3.8 s, one after another, on 24 November)."""

    def setUp(self):
        super().setUp()
        self.api.bundle_usage = False           # the log-in answer says the usage switch: no first refresh in a call
        self.sign_in()
        for i in range(5):
            self.api.guides["deck-design"][f"part{i}"] = f"TEXT {i}\n"

    def send_at_once(self, server: Server, calls: list[dict]) -> dict[int, tuple[float, dict]]:
        ids = []
        for arguments in calls:
            server.next_id += 1
            ids.append(server.next_id)
            message = {"jsonrpc": "2.0", "id": server.next_id, "method": "tools/call", "params": {"name": "guide", "arguments": arguments}}
            server.proc.stdin.write(json.dumps(message).encode() + b"\n")
        server.proc.stdin.flush()
        started, done = time.monotonic(), {}
        while len(done) < len(ids):
            answer = json.loads(server.proc.stdout.readline())
            if "id" in answer:
                done[answer["id"]] = (time.monotonic() - started, answer)
        return done

    def test_five_calls_sent_at_once_finish_together(self):
        self.api.latency = 1.0                  # every request waits a second, as a slow network would
        server = self.server()
        done = self.send_at_once(server, [{"skill": "deck-design", "section": f"part{i}"} for i in range(5)])
        times = sorted(at for at, _answer in done.values())
        self.assertLess(times[-1] - times[0], 0.6, f"side by side, not one second apart: {times}")
        self.assertLess(times[-1], 4.0, times)
        texts = {answer["result"]["content"][0]["text"] for _at, answer in done.values()}
        self.assertEqual(texts, {f"TEXT {i}\n" for i in range(5)}, "each answer under its own id")
        self.assertEqual(server.close(), b"", "nothing but answers on standard output")

    def test_a_repeat_call_asks_again_on_a_kept_connection(self):
        accepted = []
        accept = self.api.server.get_request

        def counting():
            connection = accept()
            accepted.append(connection)
            return connection
        self.api.server.get_request = counting
        server = self.server()
        deadline = time.monotonic() + 30
        while len(accepted) < api.KEPT_WARM and time.monotonic() < deadline:     # opened side by side at its start
            time.sleep(0.02)
        self.assertEqual(len(accepted), api.KEPT_WARM)
        for _ in range(6):
            self.assertEqual(server.call("guide", skill="deck-design", section="part0")["content"][0]["text"], "TEXT 0\n")
        self.assertEqual((self.api.served["guide", "part0", 200], self.api.served["guide", "part0", 304]), (1, 5),
                         "every call goes to the service; a repeat is a 304")
        self.assertEqual(len(accepted), api.KEPT_WARM, "the calls go on the connections opened at its start, none of their own")
        server.close()

    def test_answers_written_side_by_side_never_interleave(self):
        self.api.latency = 0.2
        server = self.server()
        big = "y" * 200_000 + "\n"
        self.api.guides["deck-design"]["big"] = big
        done = self.send_at_once(server, [{"skill": "deck-design", "section": "big"}] * 6)
        self.assertEqual({answer["result"]["content"][0]["text"] for _at, answer in done.values()}, {big})
        self.assertEqual(server.close(), b"")


class ReuseBounds(McpCase):
    """Lane SERVE review: every refusal empties what is held, the 60-second bound holds per sheet, and what is held is
    bounded in count and bytes."""

    def setUp(self):
        super().setUp()
        self.sign_in()
        self.api.specimens = {name: JPEG + name.encode() for name in SHEETS}
        self.api.guide_index = {"version": "g1", "sections": [{"name": "craft"}], "specimens": [{"file": n} for n in SHEETS]}

    def test_the_kill_switch_empties_what_is_held(self):
        server = self.server()
        server.send("tools/list")
        server.call("specimen", file=SHEETS[0:2])
        self.api.guide_kill = True
        result = server.call("specimen", file=SHEETS[0])
        self.assertTrue(result["content"][0]["text"].startswith("THEODORE UPDATE_REQUIRED: "), result)
        self.api.guide_kill = False
        self.assertEqual([b["type"] for b in server.call("specimen", file=SHEETS[0])["content"]], ["image"])
        self.assertEqual(self.api.served["specimen", SHEETS[0], 200], 2, "nothing held survived the refusal")

    def test_the_sheets_of_one_call_are_asked_for_side_by_side(self):
        self.api.latency = 0.8               # every request waits 0.8 s, as a slow network's round trip
        server = self.server()
        server.send("tools/list")
        started = time.monotonic()
        content = server.call("specimen", file=SHEETS[0:4])["content"]
        elapsed = time.monotonic() - started
        self.assertEqual(len([b for b in content if b["type"] == "image"]), 4)
        self.assertLess(elapsed, 4 * 0.8, "four requests at once, not one after another")


class HeldBounds(unittest.TestCase):
    """The two stores of `theodore mcp`, in-process: count and byte bounds, oldest evicted first, a version change drops
    everything of the old version."""

    def setUp(self):
        sys.path.insert(0, str(CLIENT_DIR))
        from theodore_client import mcp
        self.mcp = mcp
        mcp.SHEETS.clear()
        mcp.GUIDES.clear()
        self.addCleanup(mcp.SHEETS.clear)
        self.addCleanup(mcp.GUIDES.clear)

    def test_sheets_are_bounded_in_count_oldest_first(self):
        for i in range(self.mcp.HELD_SHEETS_MAX + 5):
            self.mcp._hold_sheet(f"s{i}.jpg", ("h", b"x", None))
        self.assertEqual(len(self.mcp.SHEETS), self.mcp.HELD_SHEETS_MAX)
        self.assertNotIn("s0.jpg", self.mcp.SHEETS)
        self.assertIn(f"s{self.mcp.HELD_SHEETS_MAX + 4}.jpg", self.mcp.SHEETS)

    def test_sheets_are_bounded_in_bytes(self):
        big = b"x" * (self.mcp.HELD_SHEET_BYTES_MAX // 4 + 1)
        for i in range(5):
            self.mcp._hold_sheet(f"b{i}.jpg", ("h", big, None))
        self.assertLessEqual(sum(len(v[1]) for v in self.mcp.SHEETS.values()), self.mcp.HELD_SHEET_BYTES_MAX)
        self.assertEqual(list(self.mcp.SHEETS), ["b2.jpg", "b3.jpg", "b4.jpg"])

    def test_references_are_bounded_in_count_and_characters(self):
        for i in range(self.mcp.HELD_GUIDES_MAX + 3):
            self.mcp._hold_guide(("deck-design", f"r{i}"), ("e", "t"))
        self.assertEqual(len(self.mcp.GUIDES), self.mcp.HELD_GUIDES_MAX)
        huge = "t" * (self.mcp.HELD_GUIDE_CHARS_MAX + 1)
        self.mcp._hold_guide(("deck-design", "huge"), ("e", huge))
        self.assertNotIn(("deck-design", "huge"), self.mcp.GUIDES, "an item larger than the bound is not held")

    def test_a_new_version_drops_everything_of_the_old_one(self):
        self.mcp.INDEX = {"version": "g1", "sections": [], "specimens": ["a.jpg"], "etag": '"v1"'}
        self.mcp._hold_sheet("a.jpg", ("h", b"x", '"v1"'))
        self.mcp._hold_guide(("deck-design", "craft"), ("e", "t"))
        self.mcp._adopt({"version": "g2", "sections": [], "specimens": ["a.jpg"], "etag": '"v2"'})
        self.assertEqual((self.mcp.SHEETS, self.mcp.GUIDES), ({}, {}))
        self.mcp.INDEX, self.mcp.NOTIFY[:] = None, []


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
