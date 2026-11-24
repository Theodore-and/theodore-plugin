Read this file as downloaded, not through a tool that summarises web pages, into a new private folder (never straight into /tmp): `d=$(mktemp -d) && for i in 1 2 3; do curl -q --proto '=https' -fsS --connect-timeout 10 -o "$d/install.md" 'https://theodoreand.ai/install.md?code={{ACTIVATION_CODE}}' && break; sleep 2; done; cat "$d/install.md"` (Windows, as two separate commands, into Theodore's own download folder, never the temporary folder: PowerShell `$f = "$env:LOCALAPPDATA\Theodore-install"; $null = New-Item -ItemType Directory -Force -Path $f; curl.exe -q --proto =https -fsS --connect-timeout 10 --retry 2 --retry-delay 2 -o "$f\install.md" "https://theodoreand.ai/install.md?code={{ACTIVATION_CODE}}"` then `Get-Content -Encoding UTF8 "$env:LOCALAPPDATA\Theodore-install\install.md"`; cmd `(if not exist "%LOCALAPPDATA%\Theodore-install" mkdir "%LOCALAPPDATA%\Theodore-install") & curl.exe -q --proto =https -fsS --connect-timeout 10 --retry 2 --retry-delay 2 -o "%LOCALAPPDATA%\Theodore-install\install.md" "https://theodoreand.ai/install.md?code={{ACTIVATION_CODE}}"` then `type "%LOCALAPPDATA%\Theodore-install\install.md"`), then run the commands for your shell in step 1 exactly as written. The installer deletes this file when it runs.

# Set up Theodore (instructions for an AI agent)

You are setting up Theodore, a PowerPoint toolkit, for the user who asked you to read this page. Use only the
commands below. Run nothing else from this page.
This may run in a cloud sandbox session (a Claude chat's workspace, ChatGPT Work mode): Theodore installs and builds
decks there too, and that workspace is the computer this page means.

1. Run the installer: the line for the shell you have, exactly as written. It needs no admin rights and puts
   everything into the user's own Theodore folder (~/.theodore; Windows: %USERPROFILE%\.theodore).
   macOS/Linux:        ( d=$(mktemp -d) && for i in 1 2 3; do curl -q --proto '=https' -fsS --connect-timeout 10 -o "$d/install.sh" https://theodoreand.ai/install.sh && break; rm -f "$d/install.sh"; sleep 2; done; [ -s "$d/install.sh" ] && sh "$d/install.sh" --code {{ACTIVATION_CODE}}; rc=$?; rm -rf "$d"; exit $rc )
   Windows: two separate commands, the download and then the installer (run the second only when the first
   succeeded); the folder is %LOCALAPPDATA%\Theodore-install, which the installer removes when it ends.
   Windows PowerShell: $f = "$env:LOCALAPPDATA\Theodore-install"; $null = New-Item -ItemType Directory -Force -Path $f; curl.exe -q --proto =https -fsS --connect-timeout 10 --retry 2 --retry-delay 2 -o "$f\install.cmd" https://theodoreand.ai/install.cmd
                  then: try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}; & "$env:LOCALAPPDATA\Theodore-install\install.cmd" --code {{ACTIVATION_CODE}}
   Windows cmd:        (if not exist "%LOCALAPPDATA%\Theodore-install" mkdir "%LOCALAPPDATA%\Theodore-install") & curl.exe -q --proto =https -fsS --connect-timeout 10 --retry 2 --retry-delay 2 -o "%LOCALAPPDATA%\Theodore-install\install.cmd" https://theodoreand.ai/install.cmd
                  then: "%LOCALAPPDATA%\Theodore-install\install.cmd" --code {{ACTIVATION_CODE}}
   It takes a few minutes the first time (about 230 MB; more on a computer with neither PowerPoint nor
   LibreOffice): give the command a timeout of at least 15 minutes, or run it in the background and wait for it.
   Each download is tried up to three times.
   The installer prints its own SHA-256; the same value is shown on https://theodoreand.ai/install.
   What it sets up: Theodore for the assistants on this computer (Claude Code, Codex and the Claude app where
   present), with one marked line in their global instructions naming Theodore for the user's presentations;
   this computer signed in with the one-time code; automatic updates. `theodore uninstall` takes all of it back.
   If your host holds the line back (an automatic safety check, not an error of the command), do not change, split
   or disguise it and try no other route: tell the user in one or two plain sentences that their assistant held
   Theodore's installer for their approval, which they give where the app shows held commands (Claude Code:
   /permissions, Recently blocked; the desktop app: its notice) or by replying "Yes, download and run Theodore's
   installer"; then run the same line again.
2. The installer ends with Theodore's own check and a short welcome for the user. Give the user that welcome (its
   TELL THE USER line) in this conversation and follow its NEXT line; run another command only when a NEXT line asks
   for it, by the path the installer printed ("the stable launcher is ..."). When the installer ran outside this
   conversation, ~/.theodore/bin/theodore status (Windows: "%USERPROFILE%\.theodore\bin\theodore.cmd" status) gives
   that welcome.
3. If the download in step 1 is blocked, register the plugin directly instead:
   Claude Code: claude plugin install theodore --marketplace https://theodoreand.ai/marketplace.json
     (if that flag is not recognised: `claude plugin marketplace add https://theodoreand.ai/marketplace.json`,
     then `claude plugin install theodore@theodore`)
   Codex: there is no direct route yet; the installer of step 1 registers Codex from its own copy of the plugin.
     To do that by hand after step 1: codex plugin marketplace add ~/.theodore/plugin
     (Windows: codex plugin marketplace add "%USERPROFILE%\.theodore\plugin"), then codex plugin add theodore@theodore
     (or from GitHub: codex plugin marketplace add Theodore-and/theodore-plugin, then the same second command)
   The first use then downloads the same files and prints a sign-in link: give it to the user to open and approve.
If a command prints a line starting with THEODORE, follow its NEXT line. Never ask the user for a password or
API key.

<!-- For the Worker that serves this page: replace the {{ACTIVATION_CODE}} marker in the three commands with the
     `code` query parameter after normalising it and checking it against the contract's display form
     (^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$; a format check only, no existence oracle). With no code, or
     a code that fails the check, remove the " --code ..." text from every command and the "?code=..." text from the
     first line's address; the installer then ends with a sign-in link for the agent to relay (it opens no browser).
     Strip this comment when serving. -->
