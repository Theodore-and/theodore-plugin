# Changes

## 0.3.3

- The assistant is reported as itself: Claude Code (also from the plugin's MCP server and a skill's load command,
  through `CLAUDE_PLUGIN_ROOT`), `claude-web` for a Claude cloud session (claude.ai chat, Cowork, Claude Code on the
  web), Codex (any install, through the variables Codex sets), `chatgpt` for ChatGPT Work or a Codex cloud task. The
  installer sends the same at activation instead of `installer`.
- Through a proxy the client waits at least 25 s for an answer: ChatGPT Work's proxy spends 7 to 10 s on each
  connection, so `doctor` (5 s), the heartbeat (2 s) and the update check (3 s) timed out there although the
  service was reachable.
- In a cloud sandbox whose network refuses or cannot resolve Theodore's sites, the installer, the launcher and the
  client name the setting to change in Claude (Settings > Capabilities > Allowed websites: theodoreand.ai and
  *.theodoreand.ai) and say that ChatGPT's ordinary chat has no internet access while Work mode does.
- The device sign-in in a cloud workspace says why each chat's workspace is approved once, with the one-click link.
- install.md: its first line says to read it as downloaded with curl (quoted address), then run the line for the
  shell exactly; one line says it may run in a cloud sandbox session.
- Launcher generation 8.

## 0.3.2

- The repository is the production build; public copy; a Windows write-test race.
