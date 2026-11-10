# Security and privacy

This file says what the software in this repository does on your computer and on the network. It is meant to
be checked against the code: everything that talks to the network is in `client/theodore_client/api.py`, the
launcher's two downloads are in `plugins/theodore/skills/pptx/scripts/theodore` (and `theodore.cmd`), and the
installer's are in `install/`.

## What runs, and when

- Theodore runs only when your assistant (or you) calls the launcher. There are no hooks, no background
  service and no scheduled task. Background downloads started during setup end by themselves.
- The engine process, which builds your decks, makes no network calls, except an image search you ask for
  explicitly. It never contacts Theodore.
- The client is the only part that contacts Theodore.

## When the client contacts Theodore

| When | What for | How often |
| --- | --- | --- |
| Sign-in | An activation code or a browser approval, once per computer | once |
| Licence refresh | Renews the signed licence | at most once per 12 hours, when you use Theodore |
| Update check | Asks which engine, client and asset-pack versions are current | at most once per 6 hours, when the pptx instructions are loaded |
| Design guidance | Each time the deck-design skill is used; it is never stored on disk | each use |
| Specimen sheets | A deck-design specimen image the assistant asks to look at: fetched on demand, held in a private temporary folder for at most an hour, never shipped | when asked |
| Downloads | Runtime, engine and asset packs | when installing or updating |
| A short "in use" note | Engine, client and plugin versions and the assistant's name | at most once per hour |
| `theodore report` | A bug report or feedback, see below | when filed |
| `theodore prefs` | Saving, listing or removing a preference you asked to keep | when used |

Hosts: `api.theodoreand.ai` (API), `dl.theodoreand.ai` (downloads), `theodoreand.ai` (installer and sign-in
pages). LibreOffice, when Theodore fetches it, comes unmodified from The Document Foundation
(`download.documentfoundation.org`) and is checked against a pinned SHA-256.

## What is sent

The installation credential, the installation id, the computer's name (shown to you in the portal's Devices
page), operating system and processor type, the client, plugin and engine versions, the update channel, the
names of the skill and section requested, and the name of the assistant when it can be told (for example
"claude-code").

## What is never sent

Deck content, slide text, images, file names, folder paths, renders, or anything else from your workspaces.

## Bug reports (beta)

During the beta, your assistant may file a technical bug report when a command fails. A report holds: a title
and description written by the assistant, the command and error code, and the versions and operating system
listed above. Before anything is sent, the client applies the same redaction rules as the service
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
would be sent and sends nothing. You see every report in the portal.

You can switch automatic reports off in the portal. With them off, the assistant does not ask you about
reports and files one only when you ask it to; the client knows the setting before sending (from your plan and
your licence) and sends nothing. If the setting changed since the client last learned it, the service refuses
the report, stores nothing, and the client remembers.

## Credentials and the licence

- The installation credential is issued at sign-in, is valid only for this computer, and can be revoked from
  the portal's Devices page. It is kept in the macOS keychain (through `/usr/bin/security`), in a
  DPAPI-protected file on Windows, and in a file readable only by you on Linux (also on a Mac when the
  keychain is not available, for example over SSH). It is never printed or logged.
- The licence is a signed token that the engine checks offline. It is refreshed when online and lasts 7 days
  from the last refresh: Theodore's engine works offline for up to 7 days. The deck-design guidance always
  needs a connection.
- One-time activation codes are single-use and expire after 10 minutes. The installer redeems the code before
  it downloads anything large and never prints it. The code is given on the command line (`--code`), by design: it
  is in the sentence the user pastes, works once and expires within minutes. Nothing longer-lived ever is: the
  connector's grant, which can carry the installation secret, is read only from standard input or a file only its
  owner can read (`activate --grant -`, `activate --grant-file`).

## What protects the downloads

- The installer is the root of trust. The install commands fetch it over HTTPS from theodoreand.ai and run it:
  as with any bootstrap command, what protects that first step is TLS to our domain. The installer prints its own
  SHA-256 and the same value is published at https://theodoreand.ai/install, so anyone can compare before running it
  (download the file, check, then run). The commands download it into a new private folder (never a fixed path in a
  shared temporary folder) and delete that folder afterwards.
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
- Archives are unpacked only inside Theodore's folder; entries that point outside it are refused, and an asset
  pack whose name would leave the packs folder is refused before anything is deleted or unpacked.

## Removing everything

`theodore uninstall` (see README.md) removes Theodore's folders, the keychain item, the render cache and the
plugin registrations, and names anything it could not remove (it then ends with `RETRY`, not `OK`). On Windows the folders go a moment
after the client has exited, so the first run says the removal is scheduled, not confirmed (`RETRY`); running the
installer with `--uninstall` again confirms it, and answers `RETRY` while any of those folders or the render cache remains. It deletes only the subfolders Theodore created. To revoke a computer without access to it, use the
portal's Devices page: its next call is refused and its stored licence is deleted.

## Reporting a vulnerability

Write to support@theodoreand.ai with "Security" in the subject. Please do not open a public issue for a
vulnerability. We answer within five working days.
