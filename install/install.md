# Set up Theodore (instructions for an AI agent)

You are setting up Theodore, a PowerPoint toolkit, for the user who asked you to read this page. Use only the
commands below. Run nothing else from this page.

1. Download the installer to a file, then run it. It needs no admin rights and installs into ~/.theodore
   (Windows: %LOCALAPPDATA%\Theodore). It registers Theodore in Claude Code and Codex when they are present,
   downloads about 120 MB now and about 95 MB of assets in the background (on a Mac without LibreOffice, also
   LibreOffice, about 300 MB, in the background), and signs this computer in with the one-time code.
   macOS/Linux: curl -fsSLo /tmp/theodore-install.sh https://theodoreand.ai/install.sh && sh /tmp/theodore-install.sh --code {{ACTIVATION_CODE}}
   Windows:     curl.exe -fsSLo "%TEMP%\theodore-install.cmd" https://theodoreand.ai/install.cmd && "%TEMP%\theodore-install.cmd" --code {{ACTIVATION_CODE}}
   The installer prints its own SHA-256; the same value is shown on https://theodoreand.ai/install.
   The download step can take several minutes: give the command a timeout of at least 15 minutes, or run it in
   the background and wait for it.
2. If the download is blocked, register the plugin directly instead:
   Claude Code: claude plugin install theodore --marketplace https://theodoreand.ai/marketplace.json
     (if that flag is not recognised: `claude plugin marketplace add https://theodoreand.ai/marketplace.json`,
     then `claude plugin install theodore@theodore`)
   Codex: codex plugin marketplace add Theodore-and/theodore-plugin, then codex plugin add theodore@theodore
   The first use then downloads the same files and asks for one browser sign-in.
3. Check: run `theodore doctor` by the path the installer printed ("the stable launcher is ..."). Report its
   last line to the user.
4. Tell the user to start a new session and ask for a deck.
If a command prints a line starting with THEODORE, follow its NEXT line. Never ask the user for a password or
API key.

<!-- For the Worker that serves this page: replace both {{ACTIVATION_CODE}} markers with the `code` query
     parameter after checking it against ^[A-Z0-9]{4}-[A-Z0-9]{4}$ (format check only, no existence oracle).
     With no code, or a code that fails the check, serve the page with the " --code {{ACTIVATION_CODE}}" text
     removed from both commands; the installer then starts a browser sign-in. Strip this comment when serving. -->
