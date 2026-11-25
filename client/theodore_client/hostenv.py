"""Which assistant and which session a command runs in, from the environment alone: standard library `os` and `sys`
only, so the short command's hot path (hot.py) reads them without loading the client (core re-exports them)."""

from __future__ import annotations

import os
import sys


def flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() not in ("", "0", "false", "no")


def claude_cloud() -> bool:
    """A Claude cloud session: a claude.ai chat, Cowork or the desktop app's chat (each a fresh machine), or Claude
    Code on the web."""
    return flag("CLAUDE_CODE_REMOTE") or os.environ.get("CLAUDE_CODE_SESSION_ORIGIN") == "claude_ai_chat" \
        or (os.environ.get("CLAUDE_CODE_ENTRYPOINT") or "").startswith("remote")


def chatgpt_cloud() -> bool:
    """ChatGPT's Work mode or a Codex cloud task: a Codex runtime on a Linux machine of OpenAI's, behind its proxy."""
    return sys.platform.startswith("linux") and flag("CODEX_NETWORK_PROXY_ACTIVE") \
        and (bool(os.environ.get("CODEX_PROXY_CERT")) or os.path.isdir("/opt/codex/runtimes"))


def host_name() -> str:
    """The assistant that called us, as far as the environment says (sent as `host`).

    claude-code: Claude Code on this computer, from its Bash tool, a skill's load command or the plugin's MCP server
    (CLAUDECODE, CLAUDE_CODE_ENTRYPOINT, CLAUDE_PLUGIN_ROOT). claude-web: a Claude cloud session. codex: Codex on this
    computer (the variables Codex sets for the commands it runs). chatgpt: ChatGPT Work or a Codex cloud task."""
    explicit = "".join(c for c in (os.environ.get("THEODORE_HOST") or "").lower()
                       if c in "abcdefghijklmnopqrstuvwxyz0123456789-")[:32].lstrip("-")
    if explicit:
        return explicit
    if claude_cloud():
        return "claude-web"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    if chatgpt_cloud():
        return "chatgpt"
    # Before CLAUDE_PLUGIN_ROOT: Codex sets it for the plugins it runs (a Codex desktop install was shown as Claude Code).
    if any(os.environ.get(name) for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX", "CODEX_HOME",
                                             "CODEX_MANAGED_BY_NPM", "CODEX_VERSION")):
        return "codex"
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "claude-code"
    return "other"


def session_id() -> str:
    """The assistant session this command runs in, when the assistant says (Claude Code, Codex); else empty."""
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value[:128]
    return ""
