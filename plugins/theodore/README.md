# Theodore

Build, edit and translate PowerPoint decks in the assistant you already use, with a consultant's design
method. Skills: `theodore:pptx` and `theodore:deck-design`.

This plugin is a loader. It holds two stub skills and a launcher; on first use the launcher sets up
Theodore's runtime in your user folder (`~/.theodore`, or `%LOCALAPPDATA%\Theodore` on Windows; about 80 MB to
download, no admin rights), signs this computer in to your Theodore account, and prints the skill's
instructions. It has no hooks and installs nothing system-wide.

- Accounts and the one-step installer: https://theodoreand.ai
- What is installed where, what is sent, how to remove it: README.md and SECURITY.md of
  https://github.com/Theodore-and/theodore-plugin
- The connector for cloud assistants (`.mcp.json`, `mcp.json`) is not switched on yet; both files are empty
  placeholders.
