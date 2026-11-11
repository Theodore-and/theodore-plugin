# Theodore

Build, edit and translate PowerPoint decks in the assistant you already use, with a consultant's design
method. Skills: `theodore:pptx` and `theodore:deck-design`.

This plugin is a loader. It holds two stub skills and a launcher; on first use the launcher sets up
Theodore's runtime in your user folder (`~/.theodore`, or `%LOCALAPPDATA%\Theodore` on Windows; about 100 to
130 MB to download before the first deck, about 560 to 670 MB on disk with the asset packs, no admin rights), signs this computer in to your Theodore account, and prints the skill's
instructions. It has no hooks and installs nothing system-wide.

- Accounts and the one-step installer: https://theodoreand.ai
- What it sends: sign-in, licence refresh, update checks, design-guidance fetches and the reports and preferences you
  or your assistant file; never deck content, file names or folder paths.
- How to remove it: `~/.theodore/bin/theodore uninstall` (Windows: `"%LOCALAPPDATA%\Theodore\bin\theodore.cmd" uninstall`)
  removes its folders, its sign-in and its registrations, and takes back what it changed in your assistants' settings.
- The connector for cloud assistants (`.mcp.json`, `mcp.json`) is not switched on yet; both files are empty
  placeholders.
