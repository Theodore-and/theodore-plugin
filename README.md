# Theodore plugin

Theodore builds, edits and translates PowerPoint decks inside the AI assistant you already use (Claude Code,
Codex and others), with a consultant's design method. This repository is the small public part: two small
loaders, a launcher, an installer and the client that keeps Theodore's runtime and engine current on your
computer. It holds no engine code and no design instructions; those are delivered to signed-in accounts.

Theodore is in beta. Accounts: https://theodoreand.ai

## Install

The simplest way is the sentence on your Theodore home page at theodoreand.ai (after you log in), sent to your assistant.
On Windows, Claude Code needs Git for Windows (it provides Git Bash) to load Theodore: install it from
git-scm.com first. Without it Claude Code runs commands in PowerShell, and Theodore cannot load.
By hand:

| Assistant | Command |
| --- | --- |
| Any, macOS or Linux | `( d=$(mktemp -d) && for i in 1 2 3; do curl -q --proto '=https' -fsS --connect-timeout 10 -o "$d/install.sh" https://theodoreand.ai/install.sh && break; rm -f "$d/install.sh"; sleep 2; done; [ -s "$d/install.sh" ] && sh "$d/install.sh"; rc=$?; rm -rf "$d"; exit $rc )` |
| Any, Windows (PowerShell) | `$f = "$env:LOCALAPPDATA\Theodore-install"; $null = New-Item -ItemType Directory -Force -Path $f; curl.exe -q --proto =https -fsS --connect-timeout 10 --retry 2 --retry-delay 2 -o "$f\install.cmd" https://theodoreand.ai/install.cmd` then, as a second command, `try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}; & "$env:LOCALAPPDATA\Theodore-install\install.cmd"` |
| Any, Windows (cmd) | `(if not exist "%LOCALAPPDATA%\Theodore-install" mkdir "%LOCALAPPDATA%\Theodore-install") & curl.exe -q --proto =https -fsS --connect-timeout 10 --retry 2 --retry-delay 2 -o "%LOCALAPPDATA%\Theodore-install\install.cmd" https://theodoreand.ai/install.cmd` then, as a second command, `"%LOCALAPPDATA%\Theodore-install\install.cmd"` |
| Claude Code only | `claude plugin install theodore --marketplace https://theodoreand.ai/marketplace.json` |
| Codex only | `codex plugin marketplace add Theodore-and/theodore-plugin` then `codex plugin add theodore@theodore` (the installer above registers Codex from its own copy, the same as `codex plugin marketplace add ~/.theodore/plugin`) |

No admin rights, no Homebrew, winget, pip or npm. Supported: macOS 13.5 or later (Apple silicon and Intel),
Windows 10 version 1803 or later (x64), Linux x64 and arm64 with glibc 2.28 or later.

## What gets installed, and where

Everything Theodore owns is in one folder per user: `~/.theodore` on macOS and Linux,
`%USERPROFILE%\.theodore` on Windows (`THEODORE_HOME` overrides it). All assistants on the computer share it, and so
does the Claude desktop app on Windows: an installation made from inside the app is in the same folder. An installation
by an earlier version in `%LOCALAPPDATA%\Theodore` keeps working, and updating, where it is; running the installer
again removes it and installs into `%USERPROFILE%\.theodore`.
Theodore creates and manages only the subfolders listed here. If the folder already exists and holds anything
else, Theodore never reads, changes or deletes it. The folder belongs to the Theodore service it was set up for
(`state/environment` names it): a build for another service refuses it and says so; give that build its own folder
with `THEODORE_HOME`, or uninstall the other first.

| Path | What it is | Size |
| --- | --- | --- |
| `bin/theodore`, `bin/theodore.cmd`, `bin/launcher.json` | The launcher, at one stable path | under 100 KB |
| `client/<version>/` | The client (Python source) | under 1 MB |
| `runtime/<id>/` | Python 3.12, the engine's Python packages, Node 24 and its modules | about 358 to 466 MB (84 to 111 MB to download; table below) |
| `engines/<version>/` | The PowerPoint engine (compiled for your platform when available); the current and previous versions and every version a deck used in the last 30 days are kept, others up to four versions in all | 43 to 58 MB each (15 to 21 MB to download) |
| `packs/` | Icon, flag, map and background libraries | about 145 MB (about 95 MB to download) |
| `tools/libreoffice/` | LibreOffice, only on a Mac or Windows PC that has none (it renders slide previews) | about 800 MB (about 300 MB to download) |
| `plugin/` | A copy of this repository's plugin, which Codex adds as a local marketplace | under 100 KB |
| `state/` | Sign-in state (see below), update state, locks, the session note (`sessions.json`, below) | a few KB |
| `logs/`, `staging/` | Client log; downloads in progress | small |

In all, about 100 to 130 MB is downloaded before the first deck (the runtime, 84 to 111 MB, and the engine, 15 to
21 MB) and about 95 MB of asset packs follow in the background; a full install takes about 560 to 670 MB on disk
(runtime, packs, one engine), plus LibreOffice where it is fetched. Engine sizes are those of the compiled
`1.0.0-rc.2` builds (download, then unpacked): macOS Apple silicon 14.6 / 42.7 MB, macOS Intel 16.5 / 45.9 MB,
Windows x64 17.9 / 54.6 MB, Linux x64 21.2 / 58.5 MB, Linux arm64 18.5 / 48.9 MB. The runtime by platform (runtime `b77f7d4ca2d43955`, built by `ci/run.sh`):

| Platform | Download | On disk |
| --- | --- | --- |
| macOS, Apple silicon | 88.3 MB (84.2 MiB) | 402.3 MB (383.6 MiB) |
| macOS, Intel | 93.2 MB (88.9 MiB) | 407.0 MB (388.2 MiB) |
| Windows x64 | 83.8 MB (79.9 MiB) | 358.1 MB (341.5 MiB) |
| Linux x64 | 111.2 MB (106.0 MiB) | 465.5 MB (444.0 MiB) |
| Linux arm64 | 100.7 MB (96.0 MiB) | 445.0 MB (424.4 MiB) |

Outside that folder:

- **Your assistants' own plugin folders.** Claude Code and Codex keep their copy of the plugin (text files)
  where they keep every plugin. Where Codex's settings folder is read-only (ChatGPT's Work mode), Codex cannot
  take the plugin; the installer then links Theodore's two skills into `~/.agents/skills` (`theodore-deck-design`,
  `theodore-pptx`), which Codex reads at the start of each session. Uninstalling removes those two links.
- **The sign-in credential.** macOS: one item in your login keychain (service `ai.theodoreand.theodore`).
  Windows: a file in `state/` protected with DPAPI for your user. Linux: a file in `state/` readable only by
  you.
- **The engine's render cache**: `~/Library/Caches/Theodore` (macOS), `%LOCALAPPDATA%\Theodore\Cache` (Windows),
  `~/.cache/theodore` (Linux); older engines used `pptx-runtime` in the same places (and under `$XDG_CACHE_HOME`
  when set), and engines up to 1.0.0-rc.6 also kept slide images in `~/.cache/pptx-slide-raster` and icon and flag
  images in `~/.cache/pptx-icons`, `~/.cache/pptx-flags` and the temporary folder's `pptx-icons` (on Windows under
  `%USERPROFILE%\.cache` and `%TEMP%`); the uninstall removes all of them (only files of the names the engine writes).
- **Your deck workspaces** get one small file, `.pptx-state/theodore-engine.json`, naming the engine version
  that started the workspace, so an update never changes a deck halfway through (a deck built slide by slide runs
  every slide on its deck's version). `theodore repin <workspace>` moves one deck, with all its slides, to the
  current version on purpose; the previous file stays beside it as `theodore-engine.json.previous`.
- **Claude Desktop**, when the app is on the computer: one entry, `theodore`, in its `claude_desktop_config.json`,
  which starts `~/.theodore/bin/theodore mcp --for claude-desktop` so the app's chat can read Theodore's design
  guidance through one tool, `theodore` (it loads once the app is quit and opened again). Nothing else in that file changes.
- **Theodore's host settings**, applied by the installer (the person asked their assistant to set Theodore up):
  in Claude Code's `settings.json` (shared by the CLI, the desktop app's Code tab and the VS Code extension),
  automatic plugin updates for the Theodore marketplace and permission rules for Theodore's own commands only, by
  their exact installed paths (on Windows for the PowerShell tool too), so they run without a prompt; the short
  command `theodore` and its rule (below); and `Edit(//**/.theodore/workspaces/**)`, which lets the assistant write and
  edit a deck's working files (`deck.js`, its notes) in the `.theodore/workspaces` folder Theodore's engine makes
  inside the folders you work in (wherever they are), and nothing else; in Codex, the file `~/.codex/rules/theodore.rules`,
  which allows Theodore's launcher by its exact paths (and `theodore`). With the Codex rules, Theodore's commands run outside Codex's
  sandbox without asking each time. Leave them out with the installer's `--no-settings`, take them back with
  `theodore hosts settings --remove`; an organisation's managed policy always wins.
- **One line in your assistants' instructions**, written by the installer because the sentence asks for it ("Set up
  Theodore and use it for all my presentations"): between `<!-- theodore:begin -->` and `<!-- theodore:end -->` in
  `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` (`CLAUDE_CONFIG_DIR` and `CODEX_HOME` when set), for each of the two
  assistants found on the computer: "The user chose Theodore for every presentation, deck or set of slides, in any
  format: use the Theodore skills (theodore:deck-design, theodore:pptx), unless the user names another tool for it; to
  uninstall Theodore, run theodore uninstall." Claude Code and Codex read these files at the start of every session;
  without the line, Claude Code's own slide tool takes "make me a deck" before any plugin. Nothing else in the file
  changes, the installer prints the line it wrote, a line you delete is written again only if the installer runs
  again, and the uninstall takes back exactly that block (and the file, when Theodore created it and nothing else was
  added to it).
- **Git Bash for Claude Code on Windows**, only when Claude Code finds none: the installer fetches Git for Windows'
  PortableGit, unmodified, into Theodore's folder (from Theodore's download host, otherwise from the project's own
  release on github.com; pinned SHA-256) and names it in Claude Code's user settings (`env.CLAUDE_CODE_GIT_BASH_PATH`;
  a value you set is never replaced, and the uninstall takes Theodore's back).
- **The short command `theodore`** (with the host settings, from 0.4.1): a two-line script that only runs the stable
  launcher, written into the first of `~/.local/bin` (the folder Claude Code's own installer uses) or `~/bin` that is
  on the PATH of the shell that ran the installer; on Windows `theodore.cmd` (and a `theodore` script for Git Bash) in
  `%USERPROFILE%\.local\bin` or else `%LOCALAPPDATA%\Microsoft\WindowsApps`, which Windows puts on every user's PATH.
  It is never written over a file that is not Theodore's or where another `theodore` comes first on PATH, and the
  rules `Bash(theodore *)` (on Windows also `PowerShell(theodore *)` and `PowerShell(& theodore *)`) exist only while it
  does. Texts then name `theodore …` instead of the launcher's long path, which the assistant otherwise kept in a
  variable that no permission rule can match. `theodore hosts settings --remove` and the uninstall delete it.

- **Two settings of LibreOffice on a Mac**, in LibreOffice's own preferences (`org.libreoffice.script`), written by
  the engine before it starts LibreOffice: `ApplePersistenceIgnoreState` (after a crash of LibreOffice, macOS asks at
  the next start whether to restore its windows, and a LibreOffice without a window waits for an answer for ever) and,
  from engine 1.0.0-rc.27, `NSAppSleepDisabled` (macOS slows an app that shows no window after about 30 seconds, and
  every conversion then took three times as long). Both also hold for a LibreOffice you open yourself: its windows
  are not restored after a crash, and it is not slowed while it is hidden.

- **The PowerPoint check on a Mac** (`pptx doctor --powerpoint`), only when your assistant or you run it: it opens a
  copy of the deck in your own PowerPoint to see whether PowerPoint would ask to repair it. The copy goes into
  PowerPoint's own temporary folder (`~/Library/Containers/com.microsoft.Powerpoint/Data/tmp`) and is deleted after
  the check. A repair prompt is answered with Cancel, never with Repair. PowerPoint is quit afterwards only when the
  check started it and no presentation is open; a presentation you have open is not touched. macOS may ask you once
  to allow it.

**The plugin alone.** Added with the Claude Code or Codex command only, without the installer, Theodore changes
nothing in an assistant's settings or instructions (from 0.4.18): no permission rule, no short command, no Codex rules
file, no line in `CLAUDE.md` or `AGENTS.md`, no Claude Desktop entry. Nobody asked for them. The assistant then asks
before each Theodore command, as it does for any plugin, and uses Theodore when you name it. The sentence on your
Theodore page adds all of it later; by hand, `theodore hosts settings --apply` adds the host settings and
`theodore hosts register` the rest.

Nothing is installed system-wide. Nothing runs on its own: a Theodore command your assistant runs may start a
download or an update in the background, which ends when it is done. Two helpers of the engine outlive the command
that started them, so that the next command finds them ready, and stop by themselves: the engine's own helper process
(it speeds up `render` and project sync), for up to 30 idle minutes; and the LibreOffice an engine command started,
for up to 10 idle minutes (from engine 1.0.0-rc.27 on macOS and Linux, from 1.0.0-rc.28 on Windows too; 250 to 400 MB
of memory in our runs, almost no CPU). Both also stop when Theodore is uninstalled or its cache is cleared. The plugin has one hook (`hooks/hooks.json`):
Claude Code runs it when it compacts a conversation or resumes one, and only then. It runs the launcher's
`session-start`, which reads Theodore's own small record of the instructions it served to that session
(`state/sessions.json`: the session id Claude Code gives, the skill, its version and when; never the conversation, which
Theodore does not open) and prints one paragraph telling the assistant to load Theodore's instructions again: after a
compaction, for a session that loaded them; after a resume, for a session that loaded them and only when a newer
version of them is current. Otherwise it prints nothing. It contacts the Theodore service only on a resume of a session
that loaded the instructions: one request for the current versions (the update check's signed release manifest),
two seconds at most, which sends nothing about the conversation. Before Theodore is set up it does nothing at all, and
it always ends with exit code 0. In a session that loaded the design instructions, `pptx start` and `pptx apply` also
say once, in one line on standard error, when a newer version of them is current; `pptx start` in a session that did not
load them says once how to load them.

## After set-up

The installer's last words (or, while a download is still finishing, the answer of the `status` check the installer
sends your assistant to) are a short welcome for your assistant to give you in the same conversation: what
Theodore now does for you, a first thing to ask for, and, when it applies, that a new session is needed or a last
download is still going on. When your account has Studio on, the welcome says so once and asks whether you want it
off: with Studio on, each slide gets a deeper design pass, which uses more of your AI plan; off, slides get a simpler
design pass that uses less of it. `theodore studio` says whether it is on; `theodore studio off` and `theodore studio on` change it for your
account (the same setting as the portal's Settings switch). Nothing asks again later.

## Updates

Theodore keeps itself up to date with nothing for you to do. The first Theodore command of each assistant session
checks once; a newer client, engine or asset pack is fetched and used from the next deck (a deck in progress keeps the
engine that started it). When a newer plugin is published, Theodore updates the plugin's files in Claude Code and
Codex through their own plugin command, in the background; the new files are used from the assistant's next session.
The assistant tells you once, in one line, what was updated. A claude.ai account's plugin can only be updated in
claude.ai (Customize > Plugins > Add plugin > Manage marketplaces > theodore-plugin > Check for updates); the skill
says so once per release. If an update cannot be fetched or applied, the current version keeps working and
`theodore doctor` says what is pending and why. `theodore uninstall` stops all of it.

## What Theodore sends and does not send

See [SECURITY.md](SECURITY.md). In one line: sign-in, licence refresh, update checks, design-guidance fetches
and the reports and preferences you or your assistant file; never deck content, file names or folder paths.

## Remove it

```
~/.theodore/bin/theodore uninstall                      macOS, Linux
"%USERPROFILE%\.theodore\bin\theodore.cmd" uninstall  Windows
```

This removes the plugin from Claude Code and Codex, also when their `claude` or `codex` command is not on PATH (it
looks where their installers put it, and where it was at registration; when it is nowhere, it takes out of their
settings files exactly the entries Theodore's registration added and says so), takes back exactly what Theodore added
or changed in their settings (recorded in `state/host-settings.json` when it made the change), and deletes
the keychain item, the engine's render cache and every Theodore subfolder listed above (and the folder itself
when nothing else is in it). What is left afterwards:

- Anything in the folder that Theodore did not put there.
- `.pptx-state/theodore-engine.json` inside your deck workspaces (harmless; delete it if you like).
- On a Mac, the two LibreOffice settings named above. They are LibreOffice's own preferences and harmless;
  `defaults delete org.libreoffice.script ApplePersistenceIgnoreState` and
  `defaults delete org.libreoffice.script NSAppSleepDisabled` remove them.
- Claude Code's cached copy of the plugin under `~/.claude/plugins/cache/theodore/` (about 60 KB of text).
  Claude Code marks it as orphaned and deletes it itself after 14 days.
- Nothing of the installer: the install commands above download it into a new private folder (Windows:
  `%LOCALAPPDATA%\Theodore-install`, never the temporary folder) that the installer deletes when it ends.
- In the system's temporary folder (`$TMPDIR`, `%TEMP%`): the engine's LibreOffice profile (`pptx-soffice-profile/`,
  `pptx-soffice-profile.warm-seed/`, `pptx-soffice-profile.lock`) and its empty lock files
  (`theodore-pptx-locks/*.lock`). The system clears that folder itself; they can be deleted at any time. Specimen
  folders (`theodore-specimens-*`) are not left: each is recorded in `state/specimens.txt` when it is made, is removed
  by the first Theodore command run after it is an hour old (there is no background process, so not before Theodore
  runs again), and the uninstall (the launcher's and the installer's own included) removes every one, wherever `TMPDIR`
  pointed; one it could not remove keeps its record for the next uninstall. When the temporary folder lies inside the working folder they go to `~/Library/Caches/Theodore/specimens`,
  `~/.cache/theodore/specimens` or `%LOCALAPPDATA%\Theodore\Cache\specimens` instead; never under the working folder.
- In the assistants' settings: what was there before. The uninstall removes Theodore's two permission rules, the
  marketplace entry it created (left as it is, and said so, if you changed it since), restores an `autoUpdate` value
  it changed (an explicit `false` included), removes a marketplace only if Theodore's registration added it, and
  removes a key, an empty `config.toml` or a `settings.json` only if Theodore created it and it is empty again.
  Anything you added, empty or not, stays. Codex's empty plugin cache folder for Theodore goes, and so does its
  `theodore.rules` while it is still what Theodore wrote. In Claude Desktop's `claude_desktop_config.json`, only the
  `theodore` entry Theodore wrote goes (and `mcpServers` if Theodore created it and it is empty again).
- Your account at theodoreand.ai. Remove this device, or the account, in the portal.

If the launcher is gone, deleting those subfolders and the keychain item by hand does the same.

## How it works

1. A small loader (`plugins/theodore/skills/pptx/SKILL.md`, `skills/deck-design/SKILL.md`) triggers as usual
   and tells the assistant to run the launcher beside it.
2. The launcher (`theodore`, POSIX sh; `theodore.cmd`, batch) finds `~/.theodore`. On first use it downloads
   Python and the client (stage 1), each checked against a SHA-256 pinned in `launcher.json`, then hands over.
3. The client downloads the rest of the runtime against a signed manifest, signs the computer in (activation
   code or browser approval), installs the engine and prints the instructions.
   The deck-design text is fetched at each use and never stored. Its specimen images are not shipped either:
   `theodore specimen <file>` fetches one into a private folder in Theodore's own cache. Nothing runs in the background, so the
   folder is removed by the first Theodore command run after it is an hour old, and at the latest by the uninstall.
4. Every status is three lines the assistant can act on:

```
THEODORE SETUP_IN_PROGRESS: Theodore is still setting up its runtime on this computer (downloading wheels, 41%).
NEXT: run the same command again in 20 seconds. Do not use another slide tool meanwhile.
TELL THE USER: Theodore is finishing its first-time setup. This takes a few minutes; I will continue when it is ready.
```

| Exit code | Status | Meaning |
| --- | --- | --- |
| 0 to 63 | | The engine's own exit code, passed through |
| 64 | USAGE | Wrong command or argument |
| 65 | SIGN_IN_REQUIRED | This computer is not signed in; the message carries the sign-in link |
| 66 | SIGN_IN_PENDING | The browser approval has not happened yet |
| 67 | SETUP_IN_PROGRESS | A download is still running; run the same command again |
| 68 | SETUP_FAILED | Cause NETWORK, TLS_INTERCEPT, PROXY_AUTH, DISK or HASH_MISMATCH |
| 69 | UNSUPPORTED_PLATFORM | No engine build for this computer |
| 70 | NO_ENTITLEMENT | The account's plan does not include this part of Theodore |
| 70 | COMPUTER_LIMIT | The account already has its maximum number of computers logged in; one is removed in Settings first |
| 71 | OFFLINE | The service cannot be reached and the offline period has ended, or the command needs the network |
| 72 | UPDATE_REQUIRED | The engine or the plugin must update first |
| 73 | BLOCKED_BY_POLICY | Paused by the publisher, blocked by the computer's policy, or automatic reports are off |
| 74 | CONNECTOR_REQUIRED | A cloud sandbox that cannot reach Theodore: the user allows its sites (Claude) or uses the connector |
| 75 | RETRY | Busy or rate-limited; try again shortly |
| 78 | INTERNAL | A bug; the client reports it to Theodore automatically, and `theodore doctor` shows the installation's state |

Commands: `theodore help`.

## Repository layout

```
.claude-plugin/marketplace.json     marketplace for Claude Code
.agents/plugins/marketplace.json    marketplace for Codex
plugins/theodore/                   the plugin: manifests, two small loaders, the launcher beside each, the Studio agent
install/                            install.sh, install.cmd, install.md (the page an assistant reads)
client/                             the client (theodore_client) and the vendored truststore module
runtime/locks/                      the open-source dependency locks and the Python and Node pins
runtime/build/                      how the runtime bundle is built, and its smoke test
tools/                              sync_launchers.py (pins and generated copies), build_plugin_archives.py
tests/                              a local fake of the Theodore API and the test suites
```

## Development

Checks run on our own machines, not on a hosted CI: `ci/run.sh` is the source of truth. The workflow files under
`.github/workflows/` are a manual-only mirror: no push or pull request starts them, and nobody waits for them.

```
ci/run.sh all --azure        every platform, the corporate-network job, the secret scan, the archives, the runtime index,
                             then the table; Windows on a throwaway Azure VM deleted when the run ends
ci/run.sh all --host <user@windows-host> --key <ssh key>   the same with Windows over SSH on a machine of your own
ci/run.sh macos-arm64 | macos-x64 | linux-x64 | linux-arm64 | windows-x64 | network | secrets | archives | index | summary
ci/run.sh require [commit]   exit 0 only when `all` passed at that commit on a clean tree; release steps call it
ci/azure-windows.sh sweep    delete any throwaway Windows VM a stopped run left behind (tag purpose=theodore-plugin-ci)
```

macOS x64 runs under Rosetta, Linux in ubuntu 22.04 containers pinned by digest (Docker; the packages are baked
into a prepared image named after its recipe, built once and reused, so no run waits on an apt mirror), Windows over SSH on a
stock Windows Server 2022 that `ci/windows-prepare.ps1` prepares (it adds Git for Windows, for the Git Bash tests).
Working files and the runtime bundles to publish are in `ci/out/`. `all` ends with `ci/run.sh record`: the table and
logs go to `$THEODORE_CI_LOGS/<date>/plugin-<commit>-<time>/` (default `~/theodore-ci`), and when every row passed on
a clean tree, the stamp `green/plugin/<commit>` ("checks green at <commit>") that `require` looks for.

Guards: one matrix run at a time on this machine (a lock in `$THEODORE_CI_LOGS/locks/`, shared with the other
repositories' runners); each heavy local job waits until the 1- and 5-minute load averages are under `CI_MAX_LOAD`
(default 8) and runs under a hard deadline, killed with its containers on the deadline or when the runner dies. The
Azure VM deallocates itself at `CI_AZURE_DEADLINE_HOURS` (default 4) through Azure's auto-shutdown, and each Azure run
first sweeps groups older than that.

```
python3 -m unittest discover -s tests -v        every suite, in clean temporary homes, against the local fake API
python3 tools/sync_launchers.py                 after changing the client, the launcher or the pins
python3 runtime/build/build_bundle.py --platform darwin-arm64 --out dist/runtime
python3 runtime/build/smoke_test.py --bundle dist/runtime --platform darwin-arm64
python3 tools/build_plugin_archives.py --out dist/plugin
```

The tests need only Python 3.11 or later; they never touch your real home, keychain or assistants.
`plugins/theodore/skills/deck-design/scripts/` is generated: edit the files under `skills/pptx/scripts/` and
run `tools/sync_launchers.py`. `theodore.cmd` and `install.cmd` must keep CRLF line endings, `theodore` LF
(`.gitattributes` enforces both).
The repository is the production build: `tools/sync_launchers.py --keys <keys.json>` stamps the service's public
manifest key into `launcher.json` and the installers, so rotating that key is a commit; staging is stamped in a copy.

## Licence

See [LICENSE](LICENSE) and [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The deck-design guidance is a
derivative of Impeccable by Paul Bakaus (Apache-2.0); see `plugins/theodore/skills/deck-design/NOTICE`.
