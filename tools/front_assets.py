#!/usr/bin/env python3
"""Write the front's static tool lists (front/assets/) from their sources, or check that they are in step.

  front/assets/mcp-tools.json    the plugin's tools: contract C1's C1-mcp-tools.json (the architect's), as copied
  front/assets/chat-tools.json   the chat's one tool (`mcp --for codex|claude-desktop`): the client's own
                                 mcp.desktop_tools(), so the relay shows exactly what the client's server shows

The relay serves these until the service answers with its filled list (C1 "The relay's mapping", tools/list).

Usage: python3 tools/front_assets.py [--check]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "front" / "assets"


def chat_tools() -> str:
    """mcp.desktop_tools(None) from a clean interpreter: the test-only overrides of its name and description unset."""
    env = {k: v for k, v in os.environ.items() if k not in ("THEODORE_CHAT_TOOL_NAME", "THEODORE_CHAT_TOOL_DESCRIPTION")}
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from theodore_client import mcp; "
            "print(json.dumps({'tools': mcp.desktop_tools(None)}, indent=1, ensure_ascii=False))")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "client")], env=env, capture_output=True, text=True,
                         encoding="utf-8", check=True)
    return out.stdout


def main(argv: list[str]) -> int:
    check = "--check" in argv
    want = {"chat-tools.json": chat_tools()}
    stale = []
    for name, text in want.items():
        path = ASSETS / name
        have = path.read_text(encoding="utf-8") if path.exists() else None
        if have != text:
            if check:
                stale.append(name)
            else:
                path.write_text(text, encoding="utf-8")
                print(f"wrote {path.relative_to(ROOT)}")
    if not (ASSETS / "mcp-tools.json").exists():
        stale.append("mcp-tools.json (copy contracts/C1-mcp-tools.json)")
    if stale:
        print("front assets out of step: " + ", ".join(stale) + "; run python3 tools/front_assets.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
