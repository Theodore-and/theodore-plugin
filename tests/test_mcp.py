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
from support import CLIENT_DIR, IS_WINDOWS, ClientCase, FakeApi, Sandbox

JPEG = b"\xff\xd8\xff\xe0" + b"sheet bytes " * 50 + b"\xff\xd9"
ENTRY = "# Deck design\n\nENTRY-MARKER é—“quoted”\n" + "".join(f"line {i:05d} " + "x" * 70 + "\n" for i in range(800))


class Server:
    """One `theodore mcp` process: send a message, read its answer line."""

    def __init__(self, case: ClientCase, env: dict | None = None):
        self.proc = subprocess.Popen([sys.executable, "-I", str(CLIENT_DIR), "mcp"], env={**case.box.env, **(env or {})},
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(case.box.tmp))
        self.next_id = 0

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
        answer = json.loads(self.proc.stdout.readline())
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
        self.assertEqual(sorted(tools), ["guide", "specimen"])
        self.assertEqual(tools["guide"]["_meta"], {"anthropic/maxResultSizeChars": 400000})
        self.assertEqual(tools["guide"]["inputSchema"]["required"], ["skill"])
        for tool in tools.values():
            self.assertEqual(tool["description"].count("."), 1, "one neutral sentence")
        self.assertEqual(server.send("nonsense/method")["error"]["code"], -32601)
        self.assertEqual(server.close(), b"", "nothing but answers on standard output")


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

    def test_refusals_are_short_texts(self):
        self.sign_in()
        server = self.server()
        self.assertTrue(server.call("specimen", file="../etc/passwd")["content"][0]["text"].startswith("THEODORE USAGE: "))
        self.api.specimens = {"cover-01.jpg": JPEG}
        self.api.fail_with = (429, "rate_limited")
        text = server.call("specimen", file="cover-01.jpg")["content"][0]["text"]
        self.assertTrue(text.startswith("THEODORE RETRY: "), text)


@unittest.skipIf(IS_WINDOWS, "POSIX launcher")
class ThroughTheLauncher(unittest.TestCase):
    def test_before_setup_standard_output_stays_clean(self):
        api = FakeApi().start()
        self.addCleanup(api.stop)
        box = Sandbox(api)
        self.addCleanup(box.close)
        done = subprocess.run(["/bin/sh", str(box.launcher), "mcp"], env={**box.env, "THEODORE_BOOTSTRAP_WAIT": "1"},
                              input=b"", capture_output=True, timeout=120)
        self.assertEqual(done.stdout, b"", "the protocol stream gets no status")
        self.assertIn(b"THEODORE ", done.stderr)


if __name__ == "__main__":
    unittest.main()
