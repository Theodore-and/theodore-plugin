# Theodore plugin

Theodore builds, edits and translates PowerPoint decks inside the AI assistant you already use (Claude Code,
Codex and others), with a consultant's design method. This repository is the small public part: two stub
skills, a launcher, an installer and the client that keeps Theodore's runtime and engine current on your
computer. It holds no engine code and no design instructions; those are delivered to signed-in accounts.

Theodore is in beta. Accounts: https://theodoreand.ai

## Install

The simplest way is the sentence on your Get started page at theodoreand.ai, pasted into your assistant.
By hand:

| Assistant | Command |
| --- | --- |
| Any, macOS or Linux | `( d=$(mktemp -d) && curl -q --proto =https -fsS -o "$d/install.sh" https://theodoreand.ai/install.sh && sh "$d/install.sh"; rc=$?; rm -rf "$d"; exit $rc )` |
| Any, Windows (PowerShell) | `$d = Join-Path $env:TEMP ("theodore-" + [guid]::NewGuid()); $null = New-Item -ItemType Directory -Path $d -ErrorAction Stop; curl.exe -q --proto =https -fsS -o "$d\install.cmd" https://theodoreand.ai/install.cmd; if ($LASTEXITCODE -eq 0) { & "$d\install.cmd" }; Remove-Item -Recurse -Force $d` |
| Any, Windows (cmd) | `cmd /d /v:on /c "set D=%TEMP%\theodore-!RANDOM!!RANDOM!!RANDOM!&& mkdir "!D!" && curl.exe -q --proto =https -fsS -o "!D!\install.cmd" https://theodoreand.ai/install.cmd && call "!D!\install.cmd" & rmdir /s /q "!D!""` |
| Claude Code only | `claude plugin install theodore --marketplace https://theodoreand.ai/marketplace.json` |
| Codex only | Run the installer above: it registers Codex from its own copy of the plugin. By hand afterwards: `codex plugin marketplace add ~/.theodore/plugin` (Windows: `codex plugin marketplace add "%LOCALAPPDATA%\Theodore\plugin"`) then `codex plugin add theodore@theodore`. Once the plugin repository is public: `codex plugin marketplace add Theodore-and/theodore-plugin` then the same second command |

No admin rights, no Homebrew, winget, pip or npm. Supported: macOS 13.5 or later (Apple silicon and Intel),
Windows 10 version 1803 or later (x64), Linux x64 and arm64 with glibc 2.28 or later.

## What gets installed, and where

Everything Theodore owns is in one folder per user: `~/.theodore` on macOS and Linux,
`%LOCALAPPDATA%\Theodore` on Windows (`THEODORE_HOME` overrides it). All assistants on the computer share it.
Theodore creates and manages only the subfolders listed here. If the folder already exists and holds anything
else, Theodore never reads, changes or deletes it.

| Path | What it is | Size |
| --- | --- | --- |
| `bin/theodore`, `bin/theodore.cmd`, `bin/launcher.json` | The launcher, at one stable path | under 100 KB |
| `client/<version>/` | The client (Python source) | under 1 MB |
| `runtime/<id>/` | Python 3.12, the engine's Python packages, Node 24 and its modules | about 358 to 466 MB (84 to 111 MB to download; table below) |
| `engines/<version>/` | The PowerPoint engine (compiled for your platform when available); up to four versions are kept | 40 to 45 MB each |
| `packs/` | Icon, flag, map and background libraries | about 145 MB (about 95 MB to download) |
| `tools/libreoffice/` | LibreOffice, only on a Mac or Windows PC that has none (it renders slide previews) | about 800 MB (about 300 MB to download) |
| `plugin/` | A copy of this repository's plugin, which Codex adds as a local marketplace | under 100 KB |
| `state/` | Sign-in state (see below), update state, locks | a few KB |
| `logs/`, `staging/` | Client log; downloads in progress | small |

In all, about 90 MB is downloaded before the first deck (the runtime and the engine, about 2.3 MB) and about
95 MB of asset packs follow in the background; a full install takes about 550 MB on disk (runtime, packs, one
engine), plus LibreOffice where it is fetched. The runtime by platform (runtime `b77f7d4ca2d43955`, built by `ci/run.sh`):

| Platform | Download | On disk |
| --- | --- | --- |
| macOS, Apple silicon | 88.3 MB (84.2 MiB) | 402.3 MB (383.6 MiB) |
| macOS, Intel | 93.2 MB (88.9 MiB) | 407.0 MB (388.2 MiB) |
| Windows x64 | 83.8 MB (79.9 MiB) | 358.1 MB (341.5 MiB) |
| Linux x64 | 111.2 MB (106.0 MiB) | 465.5 MB (444.0 MiB) |
| Linux arm64 | 100.7 MB (96.0 MiB) | 445.0 MB (424.4 MiB) |

Outside that folder:

- **Your assistants' own plugin folders.** Claude Code and Codex keep their copy of the plugin (text files)
  where they keep every plugin.
- **The sign-in credential.** macOS: one item in your login keychain (service `ai.theodoreand.theodore`).
  Windows: a file in `state/` protected with DPAPI for your user. Linux: a file in `state/` readable only by
  you.
- **The engine's render cache**: `~/Library/Caches/pptx-runtime` (macOS), `%LOCALAPPDATA%\pptx-runtime`
  (Windows), `~/.cache/pptx-runtime` (Linux).
- **Your deck workspaces** get one small file, `.pptx-state/theodore-engine.json`, naming the engine version
  that started the workspace, so an update never changes a deck halfway through.
- **Only if you say yes** when the installer offers it: two entries in Claude Code's `settings.json`
  (automatic plugin updates for the Theodore marketplace; permission to run `~/.theodore/bin/theodore`
  without a prompt).

Nothing is installed system-wide and nothing runs in the background after setup has finished. There are no
hooks: Theodore runs only when your assistant calls it.

## What Theodore sends and does not send

See [SECURITY.md](SECURITY.md). In one line: sign-in, licence refresh, update checks, design-guidance fetches
and the reports and preferences you or your assistant file; never deck content, file names or folder paths.

## Remove it

```
~/.theodore/bin/theodore uninstall                    macOS, Linux
"%LOCALAPPDATA%\Theodore\bin\theodore.cmd" uninstall  Windows
```

This removes the plugin from Claude Code and Codex, takes back the two optional settings entries, and deletes
the keychain item, the engine's render cache and every Theodore subfolder listed above (and the folder itself
when nothing else is in it). What is left afterwards:

- Anything in the folder that Theodore did not put there.
- `.pptx-state/theodore-engine.json` inside your deck workspaces (harmless; delete it if you like).
- Claude Code's cached copy of the plugin under `~/.claude/plugins/cache/theodore/` (about 60 KB of text).
  Claude Code marks it as orphaned and deletes it itself after 14 days.
- Nothing of the installer: the install commands above download it into a new private folder and delete that
  folder when it ends.
- In the system's temporary folder (`$TMPDIR`, `%TEMP%`): the engine's LibreOffice profile (`pptx-soffice-profile/`,
  `pptx-soffice-profile.warm-seed/`, `pptx-soffice-profile.lock`) and its empty lock files
  (`theodore-pptx-locks/*.lock`). The system clears that folder itself; they can be deleted at any time. Specimen
  folders (`theodore-specimens-*`) are removed by the uninstall.
- In Codex's folder (`~/.codex`): nothing of Theodore's. The uninstall removes Codex's empty plugin cache folder
  for Theodore and a configuration file of zero bytes; in Claude Code's `settings.json` it removes the keys that
  are empty once Theodore's entries are gone (`enabledPlugins`, `extraKnownMarketplaces`, `permissions.allow`).
- Your account at theodoreand.ai. Remove this device, or the account, in the portal.

If the launcher is gone, deleting those subfolders and the keychain item by hand does the same.

## How it works

1. A stub skill (`plugins/theodore/skills/pptx/SKILL.md`, `skills/deck-design/SKILL.md`) triggers as usual
   and tells the assistant to run the launcher beside it.
2. The launcher (`theodore`, POSIX sh; `theodore.cmd`, batch) finds `~/.theodore`. On first use it downloads
   Python and the client (stage 1), each checked against a SHA-256 pinned in `launcher.json`, then hands over.
3. The client downloads the rest of the runtime against a signed manifest, signs the computer in (activation
   code or browser approval), installs the engine and prints the skill's instructions.
   The deck-design text is fetched at each use and never stored. Its specimen images are not shipped either:
   `theodore specimen <file>` fetches one into a private temporary folder that is removed within the hour.
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
| 70 | NO_ENTITLEMENT | The account's plan does not include the skill |
| 71 | OFFLINE | The service cannot be reached and the offline period has ended, or the command needs the network |
| 72 | UPDATE_REQUIRED | The engine or the plugin must update first |
| 73 | BLOCKED_BY_POLICY | Paused by the publisher, blocked by the computer's policy, or automatic reports are off |
| 74 | CONNECTOR_REQUIRED | A cloud sandbox that cannot reach Theodore: use the Theodore connector |
| 75 | RETRY | Busy or rate-limited; try again shortly |
| 78 | INTERNAL | A bug; `theodore doctor` and `theodore report` help us fix it |

Commands: `theodore help`.

## Repository layout

```
.claude-plugin/marketplace.json     marketplace for Claude Code
.agents/plugins/marketplace.json    marketplace for Codex
plugins/theodore/                   the plugin: manifests, two stub skills, the launcher in each skill folder
install/                            install.sh, install.cmd, install.md (the page an assistant reads)
client/                             the client (theodore_client) and the vendored truststore module
runtime/locks/                      the open-source dependency locks and the Python and Node pins
runtime/build/                      how the runtime bundle is built, and its smoke test
tools/                              sync_launchers.py (pins and generated copies), build_plugin_archives.py
tests/                              a local fake of the Theodore API and the test suites
```

## Development

Checks run on our own machines, not on a hosted CI: `ci/run.sh` is the source of truth, and the workflow files
under `.github/workflows/` are an optional mirror of it.

```
ci/run.sh all --host <user@windows-host> --key <ssh key>   every platform, the corporate-network job, the secret scan, the runtime index
ci/run.sh macos-arm64 | macos-x64 | linux-x64 | linux-arm64 | windows-x64 | network | secrets | index | summary
```

macOS x64 runs under Rosetta, Linux in ubuntu 22.04 containers pinned by digest (Docker), Windows over SSH on a
stock Windows that `ci/windows-prepare.ps1` prepares (it adds Git for Windows, for the Git Bash tests). Results,
logs and the runtime bundles to publish are in `ci/out/`.

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

## Licence

See [LICENSE](LICENSE) and [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The deck-design skill is a
derivative of Impeccable by Paul Bakaus (Apache-2.0); see `plugins/theodore/skills/deck-design/NOTICE`.
