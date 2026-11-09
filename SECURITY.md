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
listed above. Before sending, the client removes paths under your home folder, email addresses, document file
names and long tokens (`scrub` in `client/theodore_client/commands.py`); the service removes them again.
`theodore report ... --dry-run` prints exactly what would be sent and sends nothing. You see every report in
the portal, and you can switch automatic reports off there; the assistant then asks you first.

## Credentials and the licence

- The installation credential is issued at sign-in, is valid only for this computer, and can be revoked from
  the portal's Devices page. It is kept in the macOS keychain (through `/usr/bin/security`), in a
  DPAPI-protected file on Windows, and in a file readable only by you on Linux (also on a Mac when the
  keychain is not available, for example over SSH). It is never printed or logged.
- The licence is a signed token that the engine checks offline. It is refreshed when online and lasts 7 days
  from the last refresh: Theodore's engine works offline for up to 7 days. The deck-design guidance always
  needs a connection.
- One-time activation codes are single-use and expire after 10 minutes. The installer redeems the code before
  it downloads anything large and never prints it.

## What protects the downloads

- The installer prints its own SHA-256; the same value is shown at https://theodoreand.ai/install.
- The plugin archive the installer fetches is checked against the SHA-256 published beside it.
- The launcher's first two downloads (the Python runtime, an unmodified python-build-standalone archive, and
  the client) are checked against SHA-256 values pinned in `launcher.json` in this repository.
- Everything else (the rest of the runtime, the engine, client updates, asset packs) is listed in manifests
  signed with Ed25519; the public keys are pinned in `launcher.json`. A download that does not match is deleted
  and reported as `SETUP_FAILED` with cause `HASH_MISMATCH`.
- TLS uses the operating system's certificate store, so a company's inspecting proxy works when its root
  certificate is installed on the computer, and fails closed (`TLS_INTERCEPT`) when it is not.
- Archives are unpacked only inside Theodore's folder; entries that point outside it are refused.

## Removing everything

`theodore uninstall` (see README.md) removes Theodore's folders, the keychain item, the render cache and the
plugin registrations, and lists what little is left. It deletes only the subfolders Theodore created. To revoke a computer without access to it, use the
portal's Devices page: its next call is refused and its stored licence is deleted.

## Reporting a vulnerability

Write to support@theodoreand.ai with "Security" in the subject. Please do not open a public issue for a
vulnerability. We answer within five working days.
