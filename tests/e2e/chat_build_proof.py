"""Proof without the GUI (lane LOCALENGINE): speak MCP over stdio to Theodore's local server as Claude Desktop starts it
(`mcp --for claude-desktop`) and build a small real deck end to end with the engine installed on this Mac, through
the one tool `theodore`; then try what a hostile chat would try and record each refusal.

Usage: <python> prove_chat_build.py <client dir> <deck folder> <evidence dir>

Nothing of the guidance or the specimen sheets is written: for those steps only sizes, counts and SHA-256 are kept.
Render previews of this deck are saved (they are our slides). No code or token is printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CLIENT, FOLDER, EVIDENCE = (Path(a).resolve() for a in sys.argv[1:4])
EVIDENCE.mkdir(parents=True, exist_ok=True)
FOLDER.mkdir(parents=True, exist_ok=True)
PY = sys.executable
WIN = os.name == "nt"
LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
THEODORE_HOME = Path(os.environ.get("THEODORE_HOME") or (LOCAL / "Theodore" if WIN else Path.home() / ".theodore"))
CACHE = LOCAL / "Theodore" / "Cache" if WIN else Path.home() / "Library" / "Caches" / "Theodore" if sys.platform == "darwin" else Path.home() / ".cache" / "theodore"
SECRET = Path.home() / "le-proof-secret.txt"          # a file outside the deck's folder, made and removed by this script
SECRET.write_text("not for decks")
HIDDEN = Path.home() / ".le-proof-hidden"
HIDDEN.mkdir(exist_ok=True)
SHELL = os.environ.get("ComSpec", "C:\\Windows\\System32\\cmd.exe") if WIN else "/bin/sh"
FONTS = (Path(os.environ.get("WINDIR") or "C:\\Windows") / "Fonts") if WIN else Path("/System/Library/Fonts") if sys.platform == "darwin" else Path("/usr/share/fonts")
OUTSIDE_DECK = str(Path(os.environ.get("WINDIR") or "C:\\Windows") / "win.pptx") if WIN else "/etc/hosts.pptx"

proc = subprocess.Popen([PY, "-I", str(CLIENT), "mcp", "--for", "claude-desktop"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=open(EVIDENCE / "server-stderr.txt", "wb"))
ids = iter(range(1, 10_000))
LOG: list[dict] = []


def rpc(method: str, params: dict | None = None, progress: bool = False) -> tuple[dict, list[dict]]:
    ident = next(ids)
    msg = {"jsonrpc": "2.0", "id": ident, "method": method, "params": dict(params or {})}
    if progress:
        msg["params"]["_meta"] = {"progressToken": f"p{ident}"}
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()
    notes = []
    while True:
        line = proc.stdout.readline()
        if not line:
            raise SystemExit("server closed its output")
        item = json.loads(line)
        if item.get("id") == ident:
            return item, notes
        notes.append(item)


def call(step: str, arguments: dict, *, private: bool = False, save_images: str | None = None) -> dict:
    started = time.monotonic()
    answer, notes = rpc("tools/call", {"name": "theodore", "arguments": arguments}, progress=True)
    seconds = time.monotonic() - started
    result = answer.get("result") or {}
    texts = [b["text"] for b in result.get("content", []) if b.get("type") == "text"]
    images = [b for b in result.get("content", []) if b.get("type") == "image"]
    joined = "\n".join(texts)
    record = {"step": step, "action": arguments.get("action"), "seconds": round(seconds, 2), "isError": bool(result.get("isError")),
              "text_chars": len(joined), "images": len(images), "progress_notifications": len(notes)}
    shown = {k: v for k, v in arguments.items() if k not in ("content", "stdin")}
    if "content" in arguments:
        shown["content"] = f"<{len(arguments['content'])} chars>"
    if "stdin" in arguments:
        shown["stdin"] = f"<{len(arguments['stdin'])} chars>"
    record["arguments"] = shown
    nxt = re.findall(r"^NEXT: (.+)$", joined, re.M)
    if nxt:
        record["next"] = nxt[-1][:600]
    record["points_to_manual"] = 'action "manual"' in joined
    if private:     # licensed text: never kept
        record["text_sha256"] = hashlib.sha256(joined.encode()).hexdigest()
    else:
        record["text"] = joined[:4000] + ("..." if len(joined) > 4000 else "")
    if save_images and images:
        for n, block in enumerate(images, 1):
            out = EVIDENCE / f"{save_images}-{n}.{'png' if block['mimeType'].endswith('png') else 'jpg'}"
            out.write_bytes(base64.b64decode(block["data"]))
        record["images_saved"] = [f"{save_images}-{n}" for n in range(1, len(images) + 1)]
    LOG.append(record)
    print(f"[{step}] {record['seconds']} s  error={record['isError']}  text={record['text_chars']}  images={record['images']}  "
          f"progress={record['progress_notifications']}", flush=True)
    return {"texts": texts, "joined": joined, "images": images, "error": record["isError"]}


def _f(result: dict, name: str) -> str:
    match = re.search(rf"^{name}: (.+)$", result["joined"], re.M)
    return match.group(1).strip() if match else ""


total = time.monotonic()
init, _ = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "prove", "version": "1"}})
proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
tools, _ = rpc("tools/list")
names = [t["name"] for t in tools["result"]["tools"]]
actions = tools["result"]["tools"][0]["inputSchema"]["properties"]["action"]["enum"]
LOG.append({"step": "tools/list", "tools": names, "actions": actions})
print("tools:", names, "actions:", actions)

# 1. The method: begin, then the sections it names (only counted).
begin = call("begin", {"action": "begin"}, private=True)
required = []
for m in re.finditer(r'\]\(references/([a-z0-9-]+)\.md\)', begin["joined"]):
    if m.group(1) not in required:
        required.append(m.group(1))
LOG[-1]["sections_named"] = len(required)
LOG[-1]["build_pointer_present"] = 'action "manual"' in begin["joined"]
for name in required[:3]:            # the first few sections, to show the path works; their text is not kept
    call(f"guide ({len(name)} chars)", {"action": "guide", "section": name}, private=True)

# 2. The build instructions for this chat.
manual = call("manual", {"action": "manual"}, private=True)
LOG[-1]["bindings_present"] = "Bindings (this chat)" in manual["joined"]
LOG[-1]["launcher_named"] = str(THEODORE_HOME / "bin") in manual["joined"]

# 3. The design note, on standard input to start (our own short note, in the build skill's format).
NOTE = """# Design — Why Gulf cities are electrifying their bus fleets

## Spine
Governing thought: Gulf cities electrify buses because the running cost and the air case now beat diesel.
Title chain: 1. Electric buses now cost less to run than diesel in Gulf cities

## Slide 1 — Electric buses now cost less to run than diesel in Gulf cities
**Subtitle:** Illustrative running cost per kilometre, 2026
**Message:** The running-cost gap is the reason fleets switch.
**To show it:** 1. Diesel and electric cost per km side by side (illustrative figures).
**Layout:** a bar chart on the left two thirds, a takeaway panel on the right.
**Colour:** one emphasis on electric, neutrals for diesel.
**Regions:**
- Chart: clustered bar, two bars per city.
- Takeaway: headed panel, two short lines.
"""
start = call("start", {"action": "run", "folder": str(FOLDER),
                       "args": ["start", "--blank", "--name", "Gulf e-buses", "--brief", "One slide: why Gulf cities electrify buses", "--storyline", "-"],
                       "stdin": NOTE})
if "Storyline file not found: -" in start["joined"]:
    # This engine (before rc.11) reads the note from a file only: write it with the file action and name it.
    call("write design.md", {"action": "write", "folder": str(FOLDER), "path": "design.md", "content": NOTE})
    start = call("start (note as file)", {"action": "run", "folder": str(FOLDER),
                 "args": ["start", "--blank", "--name", "Gulf e-buses", "--brief", "One slide: why Gulf cities electrify buses", "--storyline", "design.md"]})
workspace = re.search(r"^WORKSPACE: (.+)$", start["joined"], re.M).group(1).strip()
output = re.search(r"^OUTPUT: (.+)$", start["joined"], re.M).group(1).strip()

# 4. Assets through the engine: search, and a preview rendered as an image.
call("assets search", {"action": "run", "args": ["assets", "search", "icons", "bus", "--limit", "3"]})
call("assets show --render", {"action": "run", "args": ["assets", "show", "icons", "iconbt_0696", "--render"]}, save_images="icon")

# 4b. Author deck.js through the file actions.
deck = call("read deck.js", {"action": "read", "path": f"{workspace}/deck.js"})
original = re.search(r'deck\.slide\(\{\}, \(slide, pres\) => \{.*?\n\}\);\n', deck["joined"], re.S)
SLIDE = '''deck.slide({}, (slide, pres) => {
  const TITLE = "Electric buses now cost less to run than diesel in Gulf cities";
  const title = calcTextBox(TYPE.title, { text: TITLE, w: 12.333, fontFace: THEME.fonts.heading, bold: true, margin: 0 });
  slide.addText(TITLE, { x: 0.5, y: 0.5, w: 12.333, h: title.h, margin: 0, fontFace: THEME.fonts.heading,
    fontSize: TYPE.title, bold: true, color: THEME.colors.primary });
  const top = 0.5 + title.h + 0.4;
  slide.addChart(pres.charts.BAR, [
    { name: "Diesel", labels: ["Dubai", "Riyadh", "Doha"], values: [0.62, 0.58, 0.60] },
    { name: "Electric", labels: ["Dubai", "Riyadh", "Doha"], values: [0.41, 0.39, 0.43] },
  ], { x: 0.5, y: top, w: 8.2, h: 5.2 - (top - 1.6), barDir: "col", barGrouping: "clustered",
       chartColors: [THEME.colors.muted, THEME.colors.accent], showLegend: true, legendPos: "b",
       showValue: true, dataLabelFormatCode: "$0.00", valAxisHidden: true, valGridLine: { style: "none" },
       catAxisLabelFontSize: TYPE.chartLabel, dataLabelFontSize: TYPE.chartLabel, legendFontSize: TYPE.chartLegend });
  slide.addShape(pres.shapes.RECTANGLE, { x: 9.1, y: top, w: 3.73, h: 3.0, fill: { color: THEME.colors.soft }, line: { color: THEME.colors.border } });
  slide.addImage({ data: safeIcon("iconbt_0696", "ED7D__TAIL__", 301), x: 9.35, y: top + 2.2, w: 0.6, h: 0.6, objectName: "Bus_Icon" });
  slide.addText("A third cheaper per km", { x: 9.35, y: top + 0.25, w: 3.25, h: 0.5, margin: 0, fontFace: THEME.fonts.heading,
    fontSize: TYPE.header, bold: true, color: THEME.colors.ink });
  slide.addText("Energy and maintenance savings outweigh the higher purchase price over a 12-year bus life.",
    { x: 9.35, y: top + 0.85, w: 3.25, h: 1.9, margin: 0, fontFace: THEME.fonts.body, fontSize: TYPE.body, color: THEME.colors.ink, valign: "top" });
  addFootnote(slide, "Source: illustrative figures for this test deck, not research.");
});
'''
TAIL = f"{int(time.time()) % 256:02X}"         # a colour not rasterised before: the icon goes through the child Node process
SLIDE = SLIDE.replace("__TAIL__", TAIL)
ICONS = CACHE / "icons"
icons_before = len(list(ICONS.glob("*.png"))) if ICONS.is_dir() else 0
if original:
    call("edit deck.js", {"action": "edit", "path": f"{workspace}/deck.js", "old": original.group(0), "new": SLIDE})
else:
    LOG.append({"step": "edit deck.js", "note": "starter slide block not found; wrote the whole file"})

# 5. Apply (build, render, gate): previews come back as images.
call("apply", {"action": "run", "args": ["apply", workspace]}, save_images="apply")
LOG.append({"step": "icon rasterised in this apply (child Node under the same restriction)",
            "icon_cache_files_before": icons_before, "after": len(list(ICONS.glob("*.png"))) if ICONS.is_dir() else 0})
call("render grid", {"action": "run", "args": ["render", workspace, "--grid"]}, save_images="render")
call("finish", {"action": "run", "args": ["finish", workspace]})
LOG.append({"step": "delivered", "output": output, "exists": Path(output).is_file(),
            "bytes": Path(output).stat().st_size if Path(output).is_file() else 0})

# 5b. A delivered or foreign file is never overwritten (the engine's naming and the client's rules, on the real engine).
import hashlib as _h
digest = lambda p: _h.sha256(Path(p).read_bytes()).hexdigest() if Path(p).is_file() else None
own = FOLDER / "own-deck.pptx"                       # the person's own file, with the name a new deck would take
own.write_bytes(Path(output).read_bytes())
before = {"own": digest(own), "delivered": digest(output)}
NAMING = {}
r = call("naming: start over the person's own file name", {"action": "run", "folder": str(FOLDER), "args": ["start", "--blank", "--name", "Own deck", "--brief", "x"]})
ws_own, out_own = _f(r, "WORKSPACE"), _f(r, "OUTPUT")
call("naming: apply it", {"action": "run", "args": ["apply", ws_own], "images": False})
NAMING["own file kept, new name used"] = out_own != str(own) and digest(own) == before["own"]
r = call("naming: start a finished deck's name again", {"action": "run", "folder": str(FOLDER), "args": ["start", "--blank", "--name", "Gulf e-buses", "--brief", "y"]})
ws_again, out_again = _f(r, "WORKSPACE"), _f(r, "OUTPUT")
call("naming: apply it", {"action": "run", "args": ["apply", ws_again], "images": False})
NAMING["finished deck kept, new name used"] = out_again != output and ws_again != workspace and digest(output) == before["delivered"]
r = call("naming: start --from the delivered deck", {"action": "run", "folder": str(FOLDER), "args": ["start", "--from", output, "--brief", "z"]})
ws_from, out_from = _f(r, "WORKSPACE"), _f(r, "OUTPUT")
call("naming: apply it", {"action": "run", "args": ["apply", ws_from], "images": False})
NAMING["--from: source unchanged, output elsewhere"] = out_from != output and digest(output) == before["delivered"]
r = call("naming: apply the finished deck", {"action": "run", "args": ["apply", workspace]})
NAMING["finished workspace refused"] = r["error"] and digest(output) == before["delivered"]
r = call("naming: --output onto the person's file", {"action": "run", "folder": str(FOLDER), "args": ["start", "--blank", "--name", "Third", "--output", str(own), "--brief", "w"]})
NAMING["--output onto a foreign file refused"] = r["error"] and digest(own) == before["own"]
LOG.append({"step": "never overwritten", "results": NAMING, "outputs": [out_own, out_again, out_from]})

# 6. What a hostile chat would try.
HOSTILE = [
    ("refuse: another program", {"action": "run", "args": ["python", "-c", "print(1)"]}),
    ("refuse: shell string", {"action": "run", "args": ["apply; open -a Calculator"]}),
    ("refuse: path outside", {"action": "run", "args": ["start", "--from", OUTSIDE_DECK, "--brief", "x"]}),
    ("refuse: home as folder", {"action": "run", "folder": str(Path.home()), "args": ["list"]}),
    ("refuse: hidden folder", {"action": "list", "folder": str(HIDDEN)}),
    ("refuse: read outside", {"action": "read", "path": str(SECRET)}),
    ("refuse: read via ..", {"action": "read", "folder": str(FOLDER), "path": os.path.relpath(THEODORE_HOME / "state" / "entitlement.json", FOLDER)}),
    ("refuse: write a script", {"action": "write", "folder": str(FOLDER), "path": "run.sh", "content": "echo hi"}),
    ("refuse: drive PowerPoint", {"action": "run", "args": ["doctor", "--powerpoint"]}),
]
for step, arguments in HOSTILE:
    call(step, {"folder": str(FOLDER), **arguments} if "folder" not in arguments else arguments)

ATTACK = ('''
// probe: what a model-written deck script can do under the chat sandbox
const results = {};
const t = (name, f) => { try { results[name] = "ALLOWED " + String(f()).slice(0, 30); } catch (e) { results[name] = "DENIED " + (e.code || e.message).slice(0, 60); } };
t("read a file outside", () => require("fs").readFileSync(__SECRET__, "utf8").length);
t("read Theodore state", () => require("fs").readdirSync(__STATE__).length);
t("write outside", () => { require("fs").writeFileSync(require("path").join(require("os").homedir(), "le-probe.txt"), "x"); return 1; });
t("child_process", () => require("child_process").execSync("id").toString());
t("net", () => require("net").connect(80, "example.com"));
t("https", () => require("https").get("https://example.com"));
t("fetch", () => fetch("https://example.com"));
t("getBuiltinModule net", () => process.getBuiltinModule("node:net").connect(80, "example.com"));
t("worker", () => new (require("worker_threads").Worker)("1", { eval: true }));
t("addon from workspace", () => process.dlopen({ exports: {} }, __dirname + "/x.node"));
t("replace Module._load", () => { require("module")._load = () => "replaced"; if (require("module")._load("net") === "replaced") return "replaced"; throw new Error("assignment had no effect"); });
t("process.binding", () => process.binding("spawn_sync"));
t("execFileSync /bin/sh", () => require("child_process").execFileSync(__SHELL__, []).toString());
t("execFileSync node -e", () => require("child_process").execFileSync("node", ["-e", "console.log(1)"]).toString());
t("node on a written script", () => { require("fs").writeFileSync(__dirname + "/evil.js", "console.log(require('os').userInfo().username)"); return require("child_process").execFileSync("node", [__dirname + "/evil.js"]).toString(); });
t("spawn (async)", () => require("child_process").spawn(__SHELL__, []));
t("ChildProcess class", () => new (require("child_process").ChildProcess)().spawn({ file: __SHELL__, args: [__SHELL__] }));
t("read a font", () => require("fs").readdirSync(__FONTS__).length);
let esm = "pending"; import("node:child_process").then((m) => { try { m.default.execSync("id"); esm = "ALLOWED"; } catch (e) { esm = "DENIED " + (e.code || e.message).slice(0, 60); } }).catch((e) => { esm = "DENIED " + (e.code || e.message).slice(0, 60); });
let esmNet = "pending"; import("node:net").then((m) => { try { m.default.connect(80, "example.com"); esmNet = "ALLOWED"; } catch (e) { esmNet = "DENIED " + (e.code || e.message).slice(0, 60); } }).catch((e) => { esmNet = "DENIED " + (e.code || e.message).slice(0, 60); });
process.on("exit", () => require("fs").writeFileSync(__dirname + "/probe-esm.json", JSON.stringify({ "import(node:child_process)": esm, "import(node:net)": esmNet })));
require("fs").writeFileSync(__dirname + "/probe-results.json", JSON.stringify(results, null, 1));
''').replace("__SECRET__", json.dumps(str(SECRET))).replace("__STATE__", json.dumps(str(THEODORE_HOME / 'state'))).replace("__SHELL__", json.dumps(SHELL)).replace("__FONTS__", json.dumps(str(FONTS)))
probe = call("probe: start", {"action": "run", "folder": str(FOLDER), "args": ["start", "--blank", "--name", "Sandbox probe", "--brief", "probe"]})
probe_ws = re.search(r"^WORKSPACE: (.+)$", probe["joined"], re.M).group(1).strip()
text = call("probe: read", {"action": "read", "path": f"{probe_ws}/deck.js"})["joined"]
call("probe: write deck.js", {"action": "edit", "path": f"{probe_ws}/deck.js", "old": "const deck = openDeck(", "new": ATTACK + "\nconst deck = openDeck("})
res = call("probe: apply", {"action": "run", "args": ["apply", probe_ws], "images": False})
pr = call("probe: read results", {"action": "read", "path": f"{probe_ws}/probe-results.json"})
pe = call("probe: read esm results", {"action": "read", "path": f"{probe_ws}/probe-esm.json"})
found = re.findall(r"SANDBOX-PROBE (\{.*?\})(?:\\n|\n|$)", res["joined"].replace('\\"', '"'))
esm = re.findall(r"SANDBOX-PROBE-ESM (\{.*?\})(?:\\n|\n|$)", res["joined"].replace('\\"', '"'))
def body(r):
    t = r["joined"].split("\n", 1)
    try:
        return json.loads(t[1])
    except Exception:
        return r["joined"][:500]
LOG.append({"step": "sandbox probe results", "results": body(pr), "esm": body(pe),
            "home_file_written": (Path.home() / "le-probe.txt").exists()})

SECRET.unlink()
HIDDEN.rmdir()
LOG.append({"step": "platform", "os": sys.platform, "python": sys.version.split()[0]})
LOG.append({"step": "total", "seconds": round(time.monotonic() - total, 1)})
proc.stdin.close()
proc.wait(timeout=30)
(EVIDENCE / "steps.json").write_text(json.dumps(LOG, indent=1, ensure_ascii=False), encoding="utf-8")
print("evidence:", EVIDENCE / "steps.json")
