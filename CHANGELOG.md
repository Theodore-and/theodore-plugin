# Changes

## 0.3.4

- The plugin's tool server keeps each specimen sheet and each reference it fetched in memory for the session (never
  on disk), so a lead and its sub-agents opening the same sheets cost one fetch per sheet per guide version. Held
  sheets are confirmed with the service by one conditional request at most every 60 seconds; held references are
  asked for conditionally on every call. A revoked device is refused on its next reference call at once and on its
  next sheet call within 60 seconds, and nothing held is served after a refusal.
- `theodore specimen` reuses a sheet it already holds in a recent specimen folder (at most 45 minutes old) after a
  conditional check with the service: nothing new is written or downloaded. A refusal that signs the device out or
  refuses the account removes every specimen folder at once.
- Every refusal (also the channel's kill switch and a policy block) empties what the tool server holds, and the
  60-second confirmation is checked for every sheet served, also within one call. The tool server holds at most the
  sheets and references of one guide version (64 sheets and 64 MiB; 64 references and 16 MiB of text), oldest first
  out; a new version drops everything of the old one.
- Any command that learns the device is signed out, revoked, suspended, refused or blocked removes every specimen
  folder, and any command (the tool server on each call) removes folders older than an hour.

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
- Windows (lane WIN): asset packs come back for a catalogue call; LibreOffice is fetched when nothing can render
  (about 350 MB, in the background) and the deck waits for it; doctor says when Claude Code has no Git Bash.
- Windows (lane WIN): Git Bash for Claude Code on Windows without Git (PortableGit, pinned, into Theodore's folder;
  CLAUDE_CODE_GIT_BASH_PATH set and taken back on uninstall); LibreOffice unpacks without Windows Installer when the
  service refuses (1601); only x64 runtime DLLs are copied beside soffice.
- The deck-design body loaded by the skill names this launcher's path in its reporting line; the service names the
  launcher by where it is when its text reaches the agent through a tool.
- Launcher generation 8.

## 0.3.2

- The repository is the production build; public copy; a Windows write-test race.
