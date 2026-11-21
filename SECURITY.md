# Security and privacy

This file says what the software in this repository does on your computer and on the network. It is meant to
be checked against the code: everything that talks to the network is in `client/theodore_client/api.py`, the
launcher's two downloads are in `plugins/theodore/skills/pptx/scripts/theodore` (and `theodore.cmd`), and the
installer's are in `install/`.

## What runs, and when

- Theodore runs only when your assistant (or you) calls the launcher, and in Claude Code through the plugin's one hook
  (`hooks/hooks.json`), which runs the launcher's `session-start` when Claude Code compacts or resumes a conversation
  (see "The session note" below). There is no background service and no scheduled task. Background downloads started
  during setup end by themselves. Two helpers of the engine outlive the command that started them and stop by
  themselves: its own helper process (it speeds up `render` and project sync), after up to 30 idle minutes, and, from
  engine 1.0.0-rc.27 on macOS and Linux, the LibreOffice an engine command started, after up to 10 idle minutes. Both
  also stop when Theodore is uninstalled or its cache is cleared. On a Mac the engine sets two of LibreOffice's own preferences
  before it starts it (`ApplePersistenceIgnoreState` and `NSAppSleepDisabled` in `org.libreoffice.script`; README.md
  says what each is for); the uninstall leaves them.
- The engine process, which builds your decks, makes no network calls: it has no image search and no download
  command, and a picture given by a web address is refused. It never contacts Theodore.
- The client is the only part that contacts Theodore.

- In Claude Desktop's chat, Theodore's tool lets the chat run only Theodore's own engine commands. It can read and
  write only the deck's folder (text files) and read Theodore's renders. The deck script the chat writes runs without
  network access, without any other program, and without access to any other file on the computer.

## When the client contacts Theodore

| When | What for | How often |
| --- | --- | --- |
| Sign-in | An activation code or a browser approval, once per computer | once |
| Licence refresh | Renews the signed licence; where the service binds licences to computers, first asks for a single-use nonce and signs it with this installation's key (below) | at most once per 12 hours, when you use Theodore |
| Update check | Asks which engine, client and asset-pack versions are current, and reads the signed `plugin/latest.json` (the current plugin version) | at the first Theodore command of each assistant session that loads the pptx instructions, then at most once per 6 hours |
| Plugin update | When the published plugin is newer than an assistant's copy: Theodore's own copy of the plugin is downloaded (signed document, pinned sha256) and the assistant's own command updates its copy (`claude plugin marketplace update theodore` and `claude plugin update theodore@theodore`; `codex plugin add theodore@theodore`) | once per release and session, in the background |
| Design guidance | Each time the deck-design instructions are loaded; it is never stored on disk | each use |
| Resumed session | When Claude Code resumes a conversation that loaded Theodore's instructions: one request for the signed release manifest, to compare the version the conversation loaded with the current one (nothing about the conversation is sent; two seconds at most) | once per resume of such a conversation |
| Specimen sheets | A deck-design specimen image the assistant asks to look at: fetched on demand into a private folder in Theodore's own cache (the system's temporary folder only when the assistant works in a folder that contains the cache), removed by the first Theodore command run after it is an hour old (nothing runs in the background) and by the uninstall, never shipped | when asked |
| Downloads | Runtime, engine and asset packs | when installing or updating |
| A short "in use" note | Engine, client and plugin versions and the assistant's name | at most once per hour |
| `theodore report` | A bug report or feedback, see below | when filed |
| Usage counts | What Theodore did, as counts and names from a fixed list, see below; only while your account's usage counts are on | at the end of a deck (`pptx finish`), otherwise at most when the oldest waiting count is 15 minutes old and you use Theodore |
| `theodore prefs` | Saving, listing or removing a preference you asked to keep | when used |
| Studio | Reads your account's Studio setting at the end of set-up (to ask about it once), and `theodore studio on` or `off` changes it, as the portal's Settings switch does | at set-up, and when used |

Hosts: `api.theodoreand.ai` (API), `dl.theodoreand.ai` (downloads), `theodoreand.ai` (installer and sign-in
pages). Each of these names resolves to several addresses; the client races them (the next one starts a quarter of a
second after the one before) and keeps the one that answered in `state/addresses.json` (the address and when, nothing
else) for up to 7 days, so that a network where one address does not answer costs that delay once, not at every
command. LibreOffice, when Theodore fetches it, comes unmodified from The Document Foundation
(`download.documentfoundation.org`) and is checked against a pinned SHA-256. On Windows, when Claude Code finds no Git
Bash, the installer fetches Git for Windows' PortableGit, unmodified, from `dl.theodoreand.ai` and otherwise from the
project's own release on `github.com`, checked against a pinned SHA-256; no credential goes with either.

What the client changes on this computer by itself: its own folder (client, runtime, engines, packs, its copy of
the plugin), and, through the assistants' own plugin commands, the Theodore plugin in Claude Code and Codex when a
newer one is published (it takes effect at their next session). Only Theodore's set-up changes anything else in an
assistant's settings or instructions. A plugin added from a
marketplace alone, without the installer, changes nothing there by itself (from 0.4.18): the assistant asks before
each Theodore command, as it does for any plugin, and uses Theodore when you name it; `theodore hosts settings
--apply` and `theodore hosts register` add by hand what the installer adds. The installer adds, when Claude Desktop is on this computer, one entry named
`theodore` in its `claude_desktop_config.json` that starts Theodore's local server for Desktop chat (the uninstall
removes it), and writes one line between
`<!-- theodore:begin -->` and `<!-- theodore:end -->` in `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`
(`CLAUDE_CONFIG_DIR` and `CODEX_HOME` when set), for each of the two assistants found, because the sentence that
starts it asks for that ("Set up Theodore and use it for all my presentations"): "The user chose Theodore for every
presentation, deck or set of slides, in any format: use the Theodore skills (theodore:deck-design, theodore:pptx),
unless the user names another tool for it; to uninstall Theodore, run theodore uninstall." It prints the line it
wrote, changes nothing else in the file, and the uninstall takes back exactly that block; a line you deleted is written
again only if the installer runs again. On Windows, when it fetched Git Bash for Claude Code (above), it names it in Claude Code's user settings
(`env.CLAUDE_CODE_GIT_BASH_PATH`; a value you set is never replaced). Setting Theodore up also applies its host
settings, without a separate question (the person asked their assistant to set Theodore up): in Claude Code (CLI, the desktop app's Code tab and the VS Code extension
share `~/.claude/settings.json`), plugin auto-update for the Theodore marketplace and permission rules that allow only
Theodore's own commands, by their exact installed paths (the stable launcher, the plugin's skill launchers and the
plugin's own tool server; on Windows also for Claude Code's PowerShell tool), so they run without a prompt; the
short command `theodore`, a two-line script that only runs the stable launcher, in a per-user folder already on the
shell's PATH (`~/.local/bin` or `~/bin`; on Windows `%USERPROFILE%\.local\bin` or `%LOCALAPPDATA%\Microsoft\WindowsApps`),
never over another file and never where another `theodore` comes first, with the rule `Bash(theodore *)` (on Windows
also `PowerShell(theodore *)` and `PowerShell(& theodore *)`) only while it is there; reading Claude Code's own copy of
Theodore's plugin (`~/.claude/plugins/cache/theodore`); and one file rule,
`Edit(//**/.theodore/workspaces/**)`: Claude Code's file tools may create and edit, without asking, files inside a
`.theodore/workspaces` folder, wherever it is on this computer, which is where Theodore's engine keeps each deck's
working files (`deck.js`, notes). That deck script is code: Theodore's engine runs it when the assistant applies the
deck, as it ran before; the rule takes away the question before the assistant writes it, not any check. It covers no
other file, and Theodore's own folder has no such subfolder. In Codex, `~/.codex/rules/theodore.rules`, which allows
Theodore's launcher by its exact paths (and `theodore`). With the Codex rules, Theodore's commands, including the slide scripts it runs
for your decks, run outside Codex's sandbox without asking each time. To leave them out, install with
`--no-settings`; to take them back later, run `theodore hosts settings --remove` (the uninstall also removes exactly
what was added). An organisation's managed Claude Code policy always wins: when it allows only its own permission
rules, or does not allow Theodore's marketplace, Theodore does not add that setting, and `theodore doctor` says so in
one line. A claude.ai account's plugin is never changed from a computer: the skill says
once per release how to update it there.

## What is sent

The installation credential, the installation id, the computer's name (shown to you in the portal's Settings,
Computers and assistants), operating system and processor type, the client, plugin and engine versions, the update channel, the
names of the instructions and section requested, and the name of the assistant when it can be told (for example
"claude-code"; "claude-web" or "chatgpt" for a Claude or ChatGPT cloud workspace, whose device entry ends by itself
after 7 days without use). The assistant is told from the variables it sets for the programs it starts; no other
part of your environment is sent.

## What is never sent

Deck content, slide text, images, file names, folder paths, renders, or anything else from your workspaces.

## Usage counts

While usage counts are on for your account (the licence refresh says so; they are off when it does not say so), the
engine and the client write one line per command to a file in Theodore's folder:

- macOS and Linux: `~/.theodore/state/usage-ledger.jsonl`
- Windows: `%USERPROFILE%\.theodore\state\usage-ledger.jsonl`

Next to it, `state/usage.json` holds whether the counts are on, how many lines were dropped and the batch being sent.

A line holds only these fields, each a name from a fixed list, a number, a yes or no, a short code or a random id:

- an engine command: `type` ("command"), `event_id` (random), `ts` (when it ended), `verb` (start, apply, finish, ...),
  `deck_id` (128 random bits made when a deck is started; never taken from a name or a path), `workspace` (task or
  project), `kind` (blank, template, existing, translation or comments), `reopen`, `outcome` (ok, failed, refused or
  interrupted), `code` (an error name such as `entitlement_expired`, never a message), `retryable`, `duration_ms`,
  `slides_total`, `slides_added`, `slides_changed`;
- a launcher command that ended with a status other than OK: `type` ("client_status"), `event_id`, `ts`, `command`
  (the launcher's command name), `status` (for example `OFFLINE`) and `cause` (a code such as `NETWORK`, or none).

No text from your work is ever in it: no prompt, slide text, deck, file, folder, template or client name, path, hash
or size of your files. The client checks every line against that list before sending and drops any line that does not
match it exactly. The file keeps at most 2,000 lines or 1 MB and nothing older than 30 days; what is dropped is only
counted.

The client sends what is waiting to `api.theodoreand.ai/v1/usage` with the installation credential, in batches of at
most 200 lines, together with the client, plugin and engine versions, the assistant's name, the operating system's
family and major version (for example `macos-15`), the processor type and the number of lines dropped. It sends at the
end of a deck (`pptx finish`), and otherwise at the start of a Theodore command when the oldest waiting line is 15
minutes old. It waits two seconds at most, tries once, never prints anything about it and never changes the outcome of
your command; nothing runs in the background. Sent lines are deleted once the service confirms them.

`theodore usage --show` prints the next batch exactly as it would be sent, and `theodore usage` says whether the counts
are on and how many lines are waiting. To switch usage counts off, write to support@theodoreand.ai: the service then
refuses the next batch, the client deletes the file and writes nothing more until the switch is on again. Logging out
of Theodore on this computer (`theodore logout`) and the uninstall also delete it.

## The session note

`state/sessions.json` in Theodore's folder records which of Theodore's instructions the client served to each Claude Code
session: the session id Claude Code puts in the environment of the commands it runs (and gives the hook), the skill
(`deck-design` or `pptx`), the version served, when, and which one-line reminders were already given. It holds no text
from the conversation or the work, is readable only by you, keeps at most 200 sessions and nothing older than 30 days,
and is never sent anywhere. Theodore never opens the conversation or any other file of Claude Code's sessions: the hook
decides only from this note. Logging out does not delete it; the uninstall does, with the rest of `state/`.

## Bug reports (beta)

During the beta, your assistant may file a technical bug report when a command fails. A report holds: a title
and description written by the assistant, the command and error code, and the versions and operating system
listed above. When Theodore itself hits an internal error, the client files the report on its own, without asking:
the command's name, the error as the client prints it, and the same versions and operating system; at most once a day
for the same error and five a day in all, only while this computer is signed in, and never while automatic reports
are off for your account. Before anything is sent, the client applies the same redaction rules as the service
(`client/theodore_client/redact.py`, a port of the service's redactor that shares its test cases): absolute
paths are removed whole, including folder names with spaces; a document or media file name is removed together
with the words before it, unless they are generic diagnostic words; credentials in URLs and in name=value pairs,
email addresses and anything that looks like a key are removed; and any quoted passage (text copied from a slide
or a file) is replaced as a whole. The client also removes bare names of scripts and text files (`deck.js`, a `.py`
or `.md` file) by the same rule as document names. The service redacts again.

The limit, plainly: a report's description is prose the assistant writes, and no rule on this computer can prove
that prose free of deck content (an unquoted sentence copied from a slide reads like any other sentence). What
stands between deck content and a report is, in order: the reporting directions, which forbid deck content, file
names and client names; the client's redaction above; and the service's own redaction. `theodore report ... --dry-run` prints exactly what
would be sent and sends nothing. To see the reports sent from your account, write to support@theodoreand.ai.

During the beta, automatic reports are on. To stop them, write to support@theodoreand.ai and they are switched off
for your account. With them off, the assistant does not ask you about reports and files one only when you ask it
to; the client knows the setting before sending (from your plan and your licence) and sends nothing. If the setting
changed since the client last learned it, the service refuses the report, stores nothing, and the client remembers.

## Credentials and the licence

- The installation credential is issued at sign-in, is valid only for this computer, and can be removed from
  Settings in the portal (Computers and assistants). It is kept in the macOS keychain (through `/usr/bin/security`), in a
  DPAPI-protected file on Windows, and in a file readable only by you on Linux (also on a Mac when the
  keychain is not available, for example over SSH). It is never printed or logged.
- From 0.3.9 each installation also has its own key (ECDSA P-256), made on this computer at sign-in and kept inside the
  installation credential, in the same store, so it is protected and removed with it. Only its public half is sent,
  once, to register it with the installation (`state/binding.json` records that it was). Where the service binds
  licences to computers, a licence refresh carries a signature made with this key and a value derived from this
  computer's identifier and the installation id (an HMAC: the identifier itself never leaves the computer, and two
  installations on one computer give different values), so a copied credential cannot refresh elsewhere and a
  copied licence can be refused on another computer. A service that does not bind licences ignores both.
- The licence is a signed token that the engine checks offline. It is refreshed when online and lasts 7 days
  from the last refresh: Theodore's engine works offline for up to 7 days. The deck-design guidance always
  needs a connection.
- One-time activation codes expire after an hour and sign in one computer: the same code works again only for that computer and network, within 30 minutes of its first use (an install that failed half way). The installer redeems the code before
  it downloads anything large and never prints it. The code is given on the command line (`--code`), by design: it
  is in the sentence the user pastes, works once and expires within minutes. Nothing longer-lived ever is: the
  connector's grant, which can carry the installation secret, is read only from standard input or a file only its
  owner can read (`activate --grant -`, `activate --grant-file`).

## What protects the downloads

- The installer is the root of trust. The install commands fetch it over HTTPS from theodoreand.ai and run it:
  as with any bootstrap command, what protects that first step is TLS to our domain. The installer prints its own
  SHA-256 and the same value is published at https://theodoreand.ai/install, so anyone can compare before running it
  (download the file, check, then run). The commands download it into a private folder, never one in a shared
  temporary folder: a new one from `mktemp -d` on macOS and Linux, `%LOCALAPPDATA%\Theodore-install` in the user's
  own profile on Windows, where the download and the run are two separate commands. The folder goes when the
  installer ends.
- The installer carries, stamped in at release, the SHA-256 of Theodore's Python (an unmodified
  python-build-standalone archive) and of the client, and the Ed25519 public keys. It downloads Python and the
  client first and checks each against its SHA-256 with the system's own tool (`shasum`/`sha256sum`,
  `certutil`), and only then runs them. That Python verifies the signed plugin release document (Ed25519, the
  pinned keys; macOS and Windows have no built-in tool for it), and the plugin archive is checked against the
  SHA-256 and size that document gives; neither request follows a redirect. A plugin older than the one already
  installed is refused (a replayed document cannot downgrade it; versions are compared by semantic-version
  precedence). Nothing in a release document can ask for a plugin rollback, so going back to an older plugin takes
  an uninstall. Nothing downloaded runs before it has been checked. A build of the
  installer or the plugin that still carries the placeholder key refuses to run.
- The launcher's first two downloads (the same Python and client) are checked against SHA-256 values pinned in
  `launcher.json`.
- Everything else (the rest of the runtime, the engine, client updates, asset packs) is listed in manifests
  signed with Ed25519; the public keys are pinned in `launcher.json`. A download that does not match is deleted
  and reported as `SETUP_FAILED` with cause `HASH_MISMATCH`. What a verified manifest forbids (the publisher's
  pause, minimum versions, withdrawn versions) holds until a newer verified manifest says otherwise, from the very
  first manifest of a new installation. A signed manifest older than the one already applied on this computer is
  ignored, so a replayed document cannot downgrade the engine or the client; a newer one that rolls back is the
  publisher's decision and is applied. A manifest dated more than a day ahead of this computer's clock is set aside
  and not remembered. `theodore update --reset-state` (or signing out and in again) clears what this computer
  remembers about manifests; the versions installed at that moment stay the floor until a manifest issued after the
  reset says otherwise; a download chosen before the reset is not activated. An engine becomes current only if the
  manifest that chose it is still the newest accepted at that moment (checked and switched under one lock), so two
  checks running at once cannot end on the older engine.
- Downloads use HTTPS only and follow no redirect: not the launchers, not the installers (which also ignore a
  user's `.curlrc` and `.wgetrc`), not the client's runtime, engine, client and asset-pack downloads. The one
  exception is LibreOffice, fetched from The Document Foundation, whose site redirects to mirrors; that file is
  pinned by SHA-256 and no credential goes with it. API calls follow a redirect only on the same origin over HTTPS; a transfer that stalls is abandoned, and none
  lasts more than 30 minutes. The installation credential is sent only to the API host over HTTPS, never to the download host.
- TLS uses the operating system's certificate store, so a company's inspecting proxy works when its root
  certificate is installed on the computer, and fails closed (`TLS_INTERCEPT`) when it is not.
- Theodore's own hosts resolve to several addresses; without a proxy, the client and the installers try each in turn
  when one does not connect or finish the TLS handshake (a few seconds each). The name sent in the handshake and the
  name the certificate is checked against stay the host's name, never the address; a certificate that fails the check
  is not retried on another address. With a proxy, the proxy chooses the address as before.
- Archives are unpacked only inside Theodore's folder; entries that point outside it are refused, and an asset
  pack whose name would leave the packs folder is refused before anything is deleted or unpacked.

## Removing everything

`theodore uninstall` (see README.md) removes Theodore's folders, the keychain item, the render cache and the
plugin registrations, and names anything it could not remove (it then ends with `RETRY`, not `OK`). What it did is
written beside Theodore's folder, never inside it (`~/.theodore-uninstall.log`; Windows:
`%USERPROFILE%\.theodore-uninstall.log`), so it can still be read once the folder is gone; it holds paths and
outcomes, no credential, and can be deleted at any time. It first stops Theodore's own running
processes (background downloads, the tool server), each found by the file Theodore wrote for it. On Windows the folders
go after the client has exited: a cleanup removes them, retrying for up to two minutes while Windows releases the last
files, and writes its result to a file in the temporary folder that the status names; running the installer with
`--uninstall` later confirms it, and answers `RETRY` while any of those folders or the render cache remains. It deletes only the subfolders Theodore created. To revoke a computer without access to it, use the
Settings in the portal (Computers and assistants, Remove). The next command on that computer that reaches the Theodore service, whichever it is (loading
the instructions, a licence refresh, an update check, a report, automatic or not, a preference, a specimen, the setup
job, the hourly "in use" note), is refused: the client deletes the stored sign-in and the stored licence at once, and
that command ends with `SIGN_IN_REQUIRED` and the sign-in link, whatever else it did or failed to do afterwards (it
prints no instructions once it knows; when even a new sign-in cannot be started, it says so and what to run once the
connection is back). From then on no command runs, the engine included. Which call reaches the service: the first
part of the pptx instructions always asks for its directions; later parts (`guide pptx --part 2` and on) contact the
service only when the licence refresh (12 hours), the update check (6 hours) or the "in use" note (an hour) is due,
and when one is, it is made before any text is printed. If the macOS keychain cannot delete the revoked item (it fails
or does not answer), the client leaves a marker in `state/` that makes that item unusable from then on, never reads it
again and retries the deletion at each later run. A computer that stays offline cannot learn of the revoke: by design
it keeps working from its stored licence until that licence expires, at most 7 days after its last refresh.

## Reporting a vulnerability

Write to support@theodoreand.ai with "Security" in the subject. Please do not open a public issue for a
vulnerability. We answer within five working days.
