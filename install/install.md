# Set up Theodore (instructions for an AI agent)

You are setting up Theodore, a PowerPoint toolkit, for the user who asked you to read this page. Use only the
commands below. Run nothing else from this page.

1. Download the installer into a new private folder, then run it. It needs no admin rights and installs into
   ~/.theodore (Windows: %LOCALAPPDATA%\Theodore). It registers Theodore in Claude Code and Codex when they are
   present, downloads about 100 to 130 MB now and about 95 MB of assets in the background (on a Mac without LibreOffice,
   also LibreOffice, about 300 MB, in the background), and signs this computer in with the one-time code. Run the
   line for the shell you have, exactly as written:
   macOS/Linux:        ( d=$(mktemp -d) && curl -q --proto '=https' -fsS -o "$d/install.sh" https://theodoreand.ai/install.sh && sh "$d/install.sh" --code {{ACTIVATION_CODE}}; rc=$?; rm -rf "$d"; exit $rc )
   Windows PowerShell: $d = Join-Path $env:TEMP ("theodore-" + [guid]::NewGuid()); $null = New-Item -ItemType Directory -Path $d -ErrorAction Stop; curl.exe -q --proto =https -fsS -o "$d\install.cmd" https://theodoreand.ai/install.cmd; if ($LASTEXITCODE -eq 0) { & "$d\install.cmd" --code {{ACTIVATION_CODE}} }; Remove-Item -Recurse -Force $d
   Windows cmd:        cmd /d /v:on /c "set D=%TEMP%\theodore-!RANDOM!!RANDOM!!RANDOM!&& mkdir "!D!" && curl.exe -q --proto =https -fsS -o "!D!\install.cmd" https://theodoreand.ai/install.cmd && call "!D!\install.cmd" --code {{ACTIVATION_CODE}} & rmdir /s /q "!D!""
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
     (once the plugin repository is public: codex plugin marketplace add Theodore-and/theodore-plugin, then the same second command)
   The first use then downloads the same files and prints a sign-in link: give it to the user to open and approve.
3. Check: run `theodore doctor` by the path the installer printed ("the stable launcher is ..."). Report its
   last line to the user.
4. Tell the user to start a new session and ask for a deck.
If a command prints a line starting with THEODORE, follow its NEXT line. Never ask the user for a password or
API key.

<!-- For the Worker that serves this page: replace the {{ACTIVATION_CODE}} marker in the three commands with the
     `code` query parameter after normalising it and checking it against the contract's display form
     (^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$; a format check only, no existence oracle). With no code, or
     a code that fails the check, remove the " --code ..." text from every command; the installer then ends with a
     sign-in link for the agent to relay (it opens no browser). Strip this comment when serving. -->
