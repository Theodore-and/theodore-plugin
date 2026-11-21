# Theodore

Build, edit and translate PowerPoint decks in the assistant you already use, with a consultant's design
method: `theodore:pptx` for PowerPoint files and `theodore:deck-design` for deck design. Theodore is in beta and
needs a Theodore account: https://theodoreand.ai

This plugin is a loader. It holds no engine and no design method: two small loaders (`skills/`), a launcher beside
each (`scripts/theodore`, and `theodore.cmd` for Windows), one agent definition (`agents/studio.md`), one hook and
the definition of a local tool server. This page lists everything it runs, fetches, sends and changes. The same, in
full and meant to be checked against the code, is in `SECURITY.md` and `README.md` of the plugin's repository.

## What it runs

- **The launcher**, when one of Theodore's two loaders is used or your assistant runs a Theodore command. On first
  use it sets up Theodore's runtime in your user folder (`~/.theodore`, or `%USERPROFILE%\.theodore` on Windows; no
  admin rights, nothing system-wide), asks for one login in the browser to sign this computer in to your Theodore
  account, and prints the instructions.
- **Theodore's engine**, on your computer, when a deck is built, edited, rendered, translated or checked. The engine
  makes no network calls.
- **One hook** (`hooks/hooks.json`, Claude Code only): after a conversation is compacted or resumed, it runs the
  launcher's `session-start`, which reads Theodore's own record of the instructions it served to that session and
  prints one paragraph saying to load them again. It reads nothing of the conversation and sends nothing about it.
- **A local tool server** (`.mcp.json` for Claude Code, `mcp.json` for Codex): the launcher's `mcp` command, started
  by the assistant, speaking over standard input and output only. It gives the assistant the same instructions and
  specimen images as the launcher does, where a tool result is shown whole and a command's output is not. It opens no
  network port.
- No service and no scheduled task. Three things outlive the command that started them, and each ends by itself:
  downloads started during set-up; the engine's own helper process, which stays ready for up to 30 idle minutes; and
  the LibreOffice the engine started, which stays ready for up to 10 idle minutes.
- On a Mac, and only when your assistant or you ask for it (`pptx doctor --powerpoint`): your own PowerPoint, to open
  a copy of a deck and see whether PowerPoint would ask to repair it. The copy is deleted afterwards and a repair
  prompt is answered with Cancel.

## What it fetches

| From | What | How it is checked |
| --- | --- | --- |
| `dl.theodoreand.ai` | Theodore's Python (an unmodified python-build-standalone archive) and its client; then the rest of the runtime, the engine, the asset packs, and later versions of all of these and of this plugin. About 100 to 130 MB before the first deck, about 560 to 670 MB on disk with the asset packs | the first two against SHA-256 values pinned in `launcher.json`; everything else against manifests signed with Ed25519 keys pinned in the same file |
| `api.theodoreand.ai` | the design instructions, each time they are loaded (never stored on disk), and the specimen images the assistant asks to look at (on disk for an hour at most) | sent only to a signed-in computer of an account with access |
| `download.documentfoundation.org` | LibreOffice, unmodified, only when the computer has none: it renders the slides Theodore checks | pinned SHA-256 |
| `dl.theodoreand.ai`, otherwise `github.com` | Git for Windows' PortableGit, unmodified, only on Windows when Claude Code finds no Git Bash and Theodore's set-up or you ask for it (`theodore tools install git`) | pinned SHA-256 |

Nothing that was downloaded runs before it has been checked.

## What it sends

Everything goes to `api.theodoreand.ai` over HTTPS, with this installation's credential:

- sign-in (once per computer) and the licence refresh (at most once every 12 hours, when you use Theodore);
- update checks, and a short "in use" note at most once an hour (the versions and the assistant's name);
- each request for instructions: the names of the instructions and of the section asked for;
- usage counts, while they are on for your account: what Theodore did, as counts and names from a fixed list.
  `theodore usage --show` prints the next batch exactly as it would be sent;
- bug reports: the command, the error code, the versions and the operating system, with a title and a description
  the assistant writes; when Theodore itself hits an internal error, the client files one on its own (at most five a
  day). Reports are cleaned on the computer and again on the service, and `theodore report ... --dry-run` prints what
  would be sent. To stop automatic reports, write to support@theodoreand.ai;
- the preferences you ask it to keep, and your account's Studio setting when you change it.

With each request: the installation id, the computer's name (shown to you in the portal), the operating system and
processor type, the versions, the update channel and the assistant's name.

Never sent: deck content, slide text, images, file names, folder paths, renders, or anything else from your work.

## What it changes on your computer

In Theodore's own places:

- its folder in your user folder, and the engine's render cache (`~/Library/Caches/Theodore` on macOS,
  `%LOCALAPPDATA%\Theodore\Cache` on Windows, `~/.cache/theodore` on Linux);
- the sign-in credential: one item in the macOS keychain, a DPAPI-protected file on Windows, a file readable only by
  you on Linux;
- one small file in each deck's working folder, naming the engine version that started it;
- its own copy of this plugin in Claude Code and Codex, through their own plugin commands, when a newer version is
  published;
- on a Mac, two of LibreOffice's own preferences (`ApplePersistenceIgnoreState` and `NSAppSleepDisabled` in
  `org.libreoffice.script`), which a LibreOffice without a window needs. They also hold for a LibreOffice you open
  yourself, and they stay after an uninstall.

At its first use it also **sets Theodore up in your assistant**, so that it works without further steps:

- Theodore's own commands, and the assistant's edits of a deck's working files, run without a prompt: permission
  rules for exactly those in Claude Code's `settings.json`, a rules file in Codex with which Theodore's commands run
  outside Codex's sandbox, and the short command `theodore` in a per-user folder on your PATH;
- automatic updates are turned on for Theodore's marketplace;
- one marked line is written into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` saying that you chose Theodore for
  presentations, so that a request for a deck goes to Theodore.

`theodore hosts settings --remove` takes the rules back and keeps them off, a line you delete is not written again,
and an organisation's managed policy always wins.

The sentence on your Theodore page ("Set up Theodore and use it for all my presentations"), or its installer, does the
same and prints what it wrote. It also registers the plugin in every assistant it finds, adds one entry to Claude
Desktop's configuration, and on Windows fetches Git Bash for Claude Code when it has none and names it in Claude
Code's settings. Its `--no-settings` leaves the rules out.

## What it needs, and how to remove it

- A Theodore account (beta): https://theodoreand.ai
- On Windows, Claude Code needs Git Bash to load Theodore: install Git for Windows from git-scm.com first, or use the
  sentence on your Theodore page, which brings its own.
- To remove everything: `~/.theodore/bin/theodore uninstall` (Windows:
  `"%USERPROFILE%\.theodore\bin\theodore.cmd" uninstall`). It removes Theodore's folders, its sign-in and its
  registrations, takes back exactly what was changed in your assistants' settings and instructions, and names anything
  it could not remove. Removing the plugin in the assistant alone leaves Theodore's folder in place.
- Questions and security reports: support@theodoreand.ai
