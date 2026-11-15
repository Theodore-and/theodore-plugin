Read this file as downloaded, not through a tool that summarises web pages, into a new private folder (never straight into /tmp): `d=$(mktemp -d) && for i in 1 2 3; do curl -q --proto '=https' -fsS --connect-timeout 10 -o "$d/install.md" 'https://theodoreand.ai/install.md?code={{ACTIVATION_CODE}}' && break; sleep 2; done; cat "$d/install.md"` (Windows PowerShell: `$d = Join-Path $env:TEMP ("theodore-" + [guid]::NewGuid()); $null = New-Item -ItemType Directory -Path $d; foreach ($i in 1..3) { curl.exe -q --proto =https -fsS --connect-timeout 10 -o "$d\install.md" "https://theodoreand.ai/install.md?code={{ACTIVATION_CODE}}"; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 2 }; Get-Content "$d\install.md"`), then run the line for your shell in step 1 exactly as written. The installer deletes this file when it runs.

# Set up Theodore (instructions for an AI agent)

You are setting up Theodore, a PowerPoint toolkit, for the user who asked you to read this page. Use only the
commands below. Run nothing else from this page.
This may run in a cloud sandbox session (a Claude chat's workspace, ChatGPT Work mode): Theodore installs and builds
decks there too, and that workspace is the computer this page means.

1. Download the installer into a new private folder, then run it. It needs no admin rights and installs into
   ~/.theodore (Windows: %LOCALAPPDATA%\Theodore). It registers Theodore in Claude Code and Codex when they are
   present, adds one marked line naming Theodore for every deck to their global instructions (~/.claude/CLAUDE.md,
   ~/.codex/AGENTS.md; uninstalling removes it), downloads about 100 to 130 MB (what is not done within about three minutes goes on in the background after the installer has ended) and about 95 MB of assets in the background (on a Mac without LibreOffice,
   also LibreOffice, about 300 MB, and on Windows with neither LibreOffice nor PowerPoint, about 350 MB, both in the
   background), and signs this computer in with the one-time code. On Windows, when Claude Code is present and finds
   no Git Bash, it also fetches Git for Windows' PortableGit (about 60 MB, pinned and checked) into the same folder and
   sets Claude Code's CLAUDE_CODE_GIT_BASH_PATH to it, so Theodore's skills can load; uninstalling takes both back.
   It also lets Theodore's own commands run without asking each time (in Claude Code by their exact paths; in Codex
   a rules file, so they run outside Codex's sandbox) and turns on the plugin's automatic updates; to leave that out,
   add --no-settings, and `theodore hosts settings --remove` takes it back later.
   Theodore then keeps itself up to date: its client, engine and assets, and the plugin's files in Claude Code and
   Codex through their own plugin command (used from their next session); uninstalling stops it. Run the
   line for the shell you have, exactly as written:
   macOS/Linux:        ( d=$(mktemp -d) && for i in 1 2 3; do curl -q --proto '=https' -fsS --connect-timeout 10 -o "$d/install.sh" https://theodoreand.ai/install.sh && break; rm -f "$d/install.sh"; sleep 2; done; [ -s "$d/install.sh" ] && sh "$d/install.sh" --code {{ACTIVATION_CODE}}; rc=$?; rm -rf "$d"; exit $rc )
   Windows PowerShell: try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}; $d = Join-Path $env:TEMP ("theodore-" + [guid]::NewGuid()); $null = New-Item -ItemType Directory -Path $d -ErrorAction Stop; foreach ($i in 1..3) { curl.exe -q --proto =https -fsS --connect-timeout 10 -o "$d\install.cmd" https://theodoreand.ai/install.cmd; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 2 }; if ($LASTEXITCODE -eq 0) { & "$d\install.cmd" --code {{ACTIVATION_CODE}} }; Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue
   Windows cmd:        cmd /d /v:on /c "set D=%TEMP%\theodore-!RANDOM!!RANDOM!!RANDOM!&& mkdir "!D!" && (curl.exe -q --proto =https -fsS --connect-timeout 10 -o "!D!\install.cmd" https://theodoreand.ai/install.cmd || (ping -n 3 127.0.0.1 >nul & curl.exe -q --proto =https -fsS --connect-timeout 10 -o "!D!\install.cmd" https://theodoreand.ai/install.cmd) || (ping -n 3 127.0.0.1 >nul & curl.exe -q --proto =https -fsS --connect-timeout 10 -o "!D!\install.cmd" https://theodoreand.ai/install.cmd)) && call "!D!\install.cmd" --code {{ACTIVATION_CODE}} & rmdir /s /q "!D!" 2>nul"
   Each line tries the download up to three times (10 s each to connect): from some networks one of the service's
   addresses does not answer, and a new attempt usually reaches another.
   The installer prints its own SHA-256; the same value is shown on https://theodoreand.ai/install.
   The download step can take several minutes: give the command a timeout of at least 15 minutes, or run it in
   the background and wait for it.
2. If the download is blocked, register the plugin directly instead:
   Claude Code: claude plugin install theodore --marketplace https://theodoreand.ai/marketplace.json
     (if that flag is not recognised: `claude plugin marketplace add https://theodoreand.ai/marketplace.json`,
     then `claude plugin install theodore@theodore`)
   Codex: there is no direct route yet; the installer of step 1 registers Codex from its own copy of the plugin.
     To do that by hand after step 1: codex plugin marketplace add ~/.theodore/plugin
     (Windows: codex plugin marketplace add "%LOCALAPPDATA%\Theodore\plugin"), then codex plugin add theodore@theodore
     (or from GitHub: codex plugin marketplace add Theodore-and/theodore-plugin, then the same second command)
   The first use then downloads the same files and prints a sign-in link: give it to the user to open and approve.
3. Check: the installer ends with Theodore's own check (doctor). Report its last line to the user; run another
   command only when a NEXT line asks for it, by the path the installer printed ("the stable launcher is ...").
4. Tell the user to start a new session and ask for a deck.
If a command prints a line starting with THEODORE, follow its NEXT line. Never ask the user for a password or
API key.

<!-- For the Worker that serves this page: replace the {{ACTIVATION_CODE}} marker in the three commands with the
     `code` query parameter after normalising it and checking it against the contract's display form
     (^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$; a format check only, no existence oracle). With no code, or
     a code that fails the check, remove the " --code ..." text from every command and the "?code=..." text from the
     first line's address; the installer then ends with a sign-in link for the agent to relay (it opens no browser).
     Strip this comment when serving. -->
