# Changes

## 0.3.7

- Usage counts (lane ANALYTICS): while the licence refresh says `usage_enabled: true`, the client gives the engine a
  ledger path (`THEODORE_USAGE_LEDGER`, the file `state/usage-ledger.jsonl` in Theodore's folder) and records its own
  non-OK statuses there as codes. It sends what waits to `/v1/usage` at the end of `pptx finish`, otherwise at the
  start of a command once the oldest line is 15 minutes old: one attempt, two seconds at most, nothing printed, the
  command's outcome unchanged. Every line is checked against the contract's strict shapes first; the ledger keeps at
  most 2,000 lines, 1 MB and 30 days. A `usage_off` answer deletes the ledger; so does a refresh that does not say
  true, and logging out. `theodore usage --show` prints the next batch exactly as it would be sent. SECURITY.md names
  the file, its fields and when it is sent.
- `ci/run.sh selected` starts its Windows job before it waits for the shared lock.

## 0.3.6

- Uninstall stops Theodore's own running processes first: every background job, the launcher's bootstrap and each
  tool server, each found by the file Theodore wrote for it (never by name), with the processes they started. On
  Windows a cleanup removes the folder in one run, retrying for up to two minutes while Windows releases the last
  files, outside the console's job so a closed console or SSH session does not stop it; the status is OK and never
  asks to run the launcher again (it was already deleted). Before, the background LibreOffice download's python.exe
  kept the runtime, the folder stayed, and the status asked to run a command that no longer existed.
- Uninstall removes the engine's LibreOffice profile, its warm copy and lock, and its lock files from the temporary
  folder (only by the engine's names).
- "Theodore updated itself" is said once per update, also when two sessions start together, and one plugin update
  runs per release.
- The routing block (version 2) adds one clause naming Theodore's own uninstall command with the launcher's quoted
  path: asked to uninstall Theodore, Claude Code on macOS and Windows used to remove the plugin by hand and leave
  Theodore's folder, sign-in and block. Existing blocks are updated in place.
- An install right after an uninstall stays signed in on Windows: the first uninstall's cleanup works only while its
  marker is in the folder, and the installer and the launcher remove that marker (and wait for a pass in progress)
  before they write anything. Before, the cleanup, still running, removed the new installation's sign-in as it was
  written: "signed in (the one-time code was accepted)", then SIGN_IN_REQUIRED.
- A download that arrives damaged (a flaky network) is fetched afresh, three times at most, by the client (the
  runtime, the engine, packs, LibreOffice, PortableGit) and by both installers (Python and the client) before the
  install stops; a connection that drops on the way is retried the same way.
- Launcher generation 10.
- `guide pptx <section>`: a section the service serves to this account, held in memory like deck-design's sections;
  a section it does not serve is a clean USAGE (lane STUDIO, PR #7).
- The runner's per-target record copies whatever logs the target wrote (PR #12).

## 0.3.5

- Uninstall takes Theodore out of Claude Code and Codex also when `claude` or `codex` is not on PATH (Claude Code's
  Windows installer leaves `%USERPROFILE%\.local\bin` off PATH; npm, nvm, Volta and Homebrew folders are often only on
  an interactive shell's PATH). Before, the Claude Code step was skipped without a word, its plugin stayed enabled, and
  its next session ran the plugin's launcher, which downloaded the runtime again. The command is looked for where
  each assistant's installers put it and at the path recorded at registration (now recorded); when it is nowhere, the
  entries Theodore's registration added are taken out of the files directly, each only while it holds what the
  registration wrote (Claude Code: `settings.json` `enabledPlugins."theodore@theodore"` and
  `extraKnownMarketplaces.theodore`, `plugins/installed_plugins.json`, `plugins/known_marketplaces.json`, and its copy
  of the plugin; Codex: the `[marketplaces.theodore]` and `[plugins."theodore@theodore"]` tables, by their lines
  only), and the uninstall says which files it edited (JSON is written again: same content, spacing may differ).
  Whatever it cannot take back is named in its list with the file and the keys. A record from an earlier client is
  taken back the same way. The launchers' own uninstall (without Theodore's Python) looks in the same places and names
  a registration it cannot reach.
- `hosts register`, `doctor`, the routing block and the Windows Git Bash check find the assistants off PATH too. A
  registration run inside Claude Code (or Codex) that cannot find its command says FAILED instead of a success with no
  skills.
- Every uninstall path (the client, both launchers, `install.sh --uninstall`, `install.cmd --uninstall`) removes the
  engine's caches from before 1.0.0-rc.7: `~/.cache/pptx-slide-raster` (images of the user's slides),
  `~/.cache/pptx-icons`, `~/.cache/pptx-flags`, the icons' fallback in the temporary folder, and `pptx-runtime` under
  `$XDG_CACHE_HOME` when set; only files of the names the engine writes, a folder only when nothing else is in it, and
  says what it removed or left.
- The served pptx entry is never dropped silently: three attempts in a 20-second budget, then a status line first in
  the skill body, `guide pptx` and the tool server (lane STUDIO's part of PR #7; `guide pptx <section>` waits).
- The installers remove their own download folder when they end (only a `tmp.*` or `theodore-*` folder holding
  nothing but the installer), so an install line an agent split in two and whose cleanup it dropped (Codex) leaves
  nothing behind. The Windows lines tolerate the folder being gone already.
- Downloads in the background that stopped (sleep, sign-out, a killed process) start again at the next guide, skill
  or doctor: LibreOffice and PortableGit now, as the packs since 0.3.3 and the runtime and first engine through the
  setup job; a failure that passes by itself is retried a minute later at the earliest, one that needs the user is
  reported with the command that tries again. `doctor` has a "still missing" line naming each missing part and what
  will fetch it.
- Updates need no user action. The first Theodore command of each assistant session checks the channel (one request;
  the six-hour rule stays the floor within a session), so a release reaches a user at their next session; nothing is
  applied in the middle of a build. When a newer plugin is published, a background job updates the plugin's files
  through each assistant's own command (Claude Code: `claude plugin marketplace update theodore`, then
  `claude plugin update theodore@theodore`; Codex: Theodore's own copy refreshed, then `codex plugin add`), used
  from the next session. What updated itself (client, engine, asset packs, plugin files, with the release's note when
  it has one) is said once, in one line, at the next skill load or guide. Where an assistant cannot be updated from
  outside (claude.ai's account-level plugin) or its command failed, one line with that assistant's action, once per
  release; `doctor` says what is pending and why. install.md, README and SECURITY say so.
- Claude Code starts the plugin's tool server by the launcher's path instead of `sh <path>`: on Windows there is no
  `sh` on PATH, so every Windows user saw Theodore's server as failed; the path resolves to `theodore.cmd` there and
  the server connects (Windows Server 2022, Claude Code; macOS too).
- `doctor` and `tools install libreoffice` name the platform's real way to get LibreOffice: on Linux the package
  manager's command (apt, dnf, yum, zypper, apk, pacman) or plain words, never a command that is not supported there.
- `update --apply` says what was updated (client, engine, asset packs) and what was already current, instead of
  "nothing to update" while the client moved.
- install.md's first line has the agent read it into a new private folder, never straight into /tmp, and the
  installers remove any copy of it they can identify by its first line (their own folder, the temporary folder, a
  private folder there): it holds the one-time code.
- The archive builders never take macOS metadata files (AppleDouble `._*`, `.DS_Store`); a test checks every archive.
- Launcher generation 9.

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
- The pptx skill's description is the engine's new one (it opens by saying the skill is this session's slides skill,
  installed by the user), copied byte for byte.
- README and SECURITY name the portal's pages as they are now: the Theodore home page (the sentence to send) and
  Settings, Computers and assistants (where a computer is removed).
- Routing: `hosts register` (the installer runs it) writes one line between `<!-- theodore:begin -->` and
  `<!-- theodore:end -->` in `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` (CLAUDE_CONFIG_DIR / CODEX_HOME when set),
  naming Theodore for every presentation, deck or set of slides in any format. Without it, Claude Code's own Slides
  artifact (VS Code, desktop, web) takes "make me a deck" before any skill loads. A file is created only when absent,
  nothing outside the markers is touched, a second run changes nothing, and a new wording replaces the old in place.
  An install from a marketplace alone gets it at its first `guide`, `skill` or `doctor`; a block the user deleted is
  not written again except by `hosts register`. It is recorded in `state/host-settings.json`; `hosts unregister` and
  `uninstall` (also `--keep-hosts`) remove exactly the block, and the file only when Theodore created it and nothing
  else is in it. `doctor` lists the files that hold it.

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
