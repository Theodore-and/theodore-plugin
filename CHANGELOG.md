# Changes

## 0.4.25

- A settings file of the Claude app that starts with a byte-order mark is read all the same (Windows PowerShell and some editors write one). 0.4.24 (staging only) refused such a file as not valid JSON, so Theodore's entry for the app's chat was not written, and an installation that was following the app to its new folder gave up there.
- When Theodore's entry leaves the folder the Claude app no longer reads, a settings file there that held that entry and nothing else is removed, whichever of its records names it. 0.4.24 could leave it behind with an empty list.

## 0.4.24

Staging only; everything in it is in 0.4.25.


- `theodore uninstall` and `theodore logout` tell the Theodore service that the computer is leaving your account, so it no longer counts as one of your account's computers. Before, only the sign-in on the computer went: the account kept listing the computer as logged in, and after ten installs, each of them uninstalled, a new install was refused with "this account already has 10 computers logged in". The call takes a few seconds at most and never stops an uninstall: without a connection the computer stays listed until you remove it in Settings, under Computers and assistants.
- Specimen sheets open for an assistant that was started in your own user folder, where a terminal opens. On Windows that folder contains both places a sheet may be kept (the temporary folder and Theodore's cache), and the rule that a sheet is never put inside the working folder refused every one: the design went on without them. From your user folder itself the sheets now go to Theodore's own cache folder, the one they go to from a project folder, private to you as before. Everywhere else nothing changes: a sheet is never put inside a project folder, and a folder above your user folder is still refused.
- `theodore specimen` asked for a text of the guidance (a name ending in `.md`) answers with the command that reads that text.
- Windows: Theodore follows the Claude app to where it now keeps its settings. Since its update of November 2026 the app keeps its data in `%LOCALAPPDATA%\Claude-Data` and no longer in the folder inside its package. A set-up made after that update wrote Theodore's entry for the app's chat into the old folder, which the app no longer reads, and said the Claude app was registered. The entry is now written where the app reads it; an installation whose entry is in the old folder puts it right at its next command; and the uninstall removes the entry from the file the app carried to its new folder.

## 0.4.23

- A text read in parts comes in the fewest parts that hold it, about even in size, cut where a `## ` heading begins when one lies where a cut may fall and otherwise at a line end. In 0.4.22 (staging only) the build API came as three parts on a Mac, the first one its opening paragraph alone, because its first section was just over one part; an assistant that reads a first part of a few lines may not read on. It is two parts now, on every system.

## 0.4.22

- Every text of the engine's manual is read through Theodore's launcher, in parts that fit one answer of the assistant: `theodore guide pptx` for the main text and `theodore guide pptx <name>` for another, such as the build API (`pptxgenjs`). Before, the manual pointed at files in the engine's folder. An assistant that read the build API in one command was shown only its first 2 KB by Claude Code, because the file is longer than Claude Code shows of one answer, and then built slides without it; asking the launcher for it by name answered "no such section". Now the name works, the text comes in two parts that end at its headings, and the manual's pointers are the commands that print each text. A name the service does not serve is read from the installed engine's own file, also with no connection; a name that is no text lists the ones there are.
- From engine 1.0.0-rc.29 the service serves the manual and the engine no longer brings it as files. The commands are the same.
- Set-up no longer ends with `RETRY` when another assistant on the computer cannot take the plugin (an old Codex, for example) while the assistant you are using took it. It ends ready, says in one sentence which assistant was left out, and so shows its welcome and asks about Studio as every set-up does. A failure in the assistant you are using is still a failure.
- A long text printed in parts is cut where a `## ` heading begins whenever it has such headings, so a part holds whole sections.

## 0.4.21

- A plugin added with the Claude Code or Codex command alone sets itself up at its first use again, as it did up to 0.4.17: Theodore's own commands run without a prompt, and a request for a deck goes to Theodore without naming it. Versions 0.4.18 to 0.4.20 left this to the installer, so a person who added only the plugin was asked before every Theodore command and had to name Theodore. Every way of installing now ends the same. `theodore hosts settings --remove` still takes the settings back and keeps them off, and a line you delete from `CLAUDE.md` or `AGENTS.md` is still not written again.
- The documents say so: the plugin's README, `README.md` and `SECURITY.md` list what the plugin sets up at its first use and how to undo it.

## 0.4.20

- Documents only, for engine 1.0.0-rc.28. On Windows, LibreOffice now also stays running for up to 10 idle minutes after a command, as on macOS and Linux; the documents said Windows kept none. They also describe the PowerPoint check on a Mac (`pptx doctor --powerpoint`), which no document mentioned: it opens a copy of the deck in your own PowerPoint, puts that copy in PowerPoint's temporary folder and deletes it afterwards, answers a repair prompt with Cancel, and quits PowerPoint only when it started it and no presentation is open.

## 0.4.19

- Documents only, for engine 1.0.0-rc.27. `README.md`, `SECURITY.md` and the plugin's README now say that two helpers of the engine outlive the command that started them and stop by themselves (its own helper process, up to 30 idle minutes; from rc.27 on macOS and Linux the LibreOffice it started, up to 10 idle minutes), and that on a Mac the engine sets two of LibreOffice's own preferences, which an uninstall leaves behind, with the two commands that remove them. The first of those settings has been written since engine 1.0.0-rc.18 and was not mentioned anywhere.

## 0.4.18

- A plugin added with the Claude Code or Codex command alone, without Theodore's installer, no longer changes anything in an assistant's settings or instructions by itself. Before, its first use added the permission rules for Theodore's commands, the short command `theodore`, the Codex rules file and the line in `CLAUDE.md` and `AGENTS.md` that names Theodore for every presentation, although nobody had asked for them. They now come only with the set-up a person asks for (the sentence on their Theodore page, or the installer), which works as before. By hand, `theodore hosts settings --apply` adds the host settings and `theodore hosts register` the rest. An install that already has them keeps them.
- The plugin's own README now lists everything the plugin runs, fetches, sends and changes. It was out of date: it said the plugin had no hook (it has one, for Claude Code) and that its tool-server files were empty.
- `README.md` and `SECURITY.md` now describe two things the installer does that they did not mention: the line it writes into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, and, on Windows, the Git Bash it fetches for Claude Code (from Theodore's download host, otherwise from Git for Windows' release on github.com) with the Claude Code setting that names it. `SECURITY.md` no longer mentions an image search: the engine has none and makes no network calls.

## 0.4.17

- Windows: removing Theodore no longer leaves an empty `Theodore` folder under `%LOCALAPPDATA%`. The engine keeps its cache there; since Theodore's own folder moved to the user's profile, the cache's folder stayed behind after an uninstall.

## 0.4.16

- A report's text is cleaned on the computer by the same rules as on Theodore's service: long hexadecimal values and base64 values are now removed before the report leaves, as the service already did on arrival.
- Windows: Theodore's files are unpacked by several writers at once. On a computer that checks every new file, as Windows does, unpacking the runtime took a third less time in our test (28 seconds to 19); the files written are the same, one for one.
- When Theodore's service has taken as many requests of one kind as it allows for an account in an hour, the answer says so and says when the next one can go. Before, it said the service was busy and to try again in a moment.

## 0.4.15

- Windows: Theodore now lives in your user folder (`%USERPROFILE%\.theodore`), so an install made from the Claude desktop app is found by every assistant on the computer. Before, the app kept it in a private folder of its own. An install made earlier keeps working where it is; setting Theodore up again moves it and removes the old copy.
- An update also brings Theodore's launcher up to date, so a launcher fix reaches a computer with the update that carries it.
- The installer no longer ends with an error code when a download is only still running, and when the engine could not be set up it says so, with the reason, instead of "still being set up".
- A deck built slide by slide stays on one engine: a slide's folder follows its deck. An engine that a deck used in the last 30 days is never removed. `theodore repin <deck folder>` moves a deck and all its slides to the current engine in one step.
- A report the service cannot take at that moment is kept and sent later by Theodore itself; nobody is asked to try again.

## 0.4.14

- Windows, installed from inside the Claude desktop app: the engine installs. The app keeps what its commands write in a folder of its own, and Theodore's link from the engine to its runtime pointed outside it, so the set-up ended with "lacks the Node module acorn".
- Windows: a command whose text holds a line break no longer runs again and again. `report` takes long details from a file (`--details-file`) or from standard input (`--details -`).
- An internal error is reported to Theodore by Theodore itself, once, with technical content only. The assistant is no longer told to send a report or to ask about one.
- The set-up page an assistant reads is shorter and offers no choice to make.
- When an engine cannot be linked to its runtime, the message says so, with both folders.

## 0.4.13

- On Windows with PowerPoint and no LibreOffice, doctor and the installer's answer say PowerPoint turns decks into slide images, and no longer say LibreOffice is needed or offer to install it.

## 0.4.12

- The Studio agent says where it applies, so an account with Studio off is not handed it.

## 0.4.11

- Theodore's Studio agent and its instructions no longer ask for an effort level: effort is the session's own
  setting, as before 0.4.10. The Studio agent stays.

## 0.4.10

- Theodore brings its own Studio agent and asks for high effort when its instructions load, so Studio's design pass
  runs whatever effort the session is set to.

## 0.4.9

- After a set-up that ended with a sign-in link, finishing that sign-in now also starts the download of the
  PowerPoint engine when it is not installed yet, in the background, as the installer does. The welcome then comes
  with the first `status` that says Theodore is ready (in Codex, at once, with its line about the download), so the
  first deck no longer waits for the engine. If the download cannot be started, the sign-in still succeeds and the
  welcome is given as in 0.4.8. A computer that already had its welcome is not changed.

## 0.4.8

- When set-up ends with a sign-in link (the one-time code was more than an hour old, or missing), finishing that
  sign-in now gives the same short welcome the installer gives, once per computer: if the download is still going on,
  the welcome comes with the first `status` that says Theodore is ready (in Codex, at once). A computer that already
  had its welcome, signing in again later, is told only that it is signed in, as before.

## 0.4.7

- The welcome is the last thing the assistant is given (a staging run: an assistant finishes its tool work before it
  speaks, and built its own closing from the last answer). While the download is still going on, the installer only
  sends it to `status --wait 100` and marks the welcome owed (`state/welcome.json`); the first `status` that says OK
  gives the welcome, once. When nothing is downloading, and in Codex, the installer gives it as before. Its NEXT says
  the welcome is the whole closing message. Exit codes unchanged.
- 0.4.6 reached staging only; 0.4.7 is the first of the two to reach production.

## 0.4.6

A short welcome after set-up, and Studio from the assistant.

- The installers end with the client's `welcome` (not for assistants): a status whose TELL THE USER is at most five
  short lines (a welcome, what Theodore now does, a first thing to ask for, Studio once, and only when it applies a
  new session or a last download), and whose NEXT tells the assistant to give it in the same conversation. Codex's
  "run nothing more in this session" and the still-downloading `status --wait 100` are kept in its NEXT line. When
  not every assistant was registered, or the welcome cannot be given, the ending is as before.
- Studio is mentioned only when the account has it on, in its plan and honoured by its guidance (`GET /v1/studio`,
  four seconds at most; any failure leaves it out), and only once per computer (`state/welcome.json`).
- `theodore studio` says whether Studio is on; `theodore studio off` and `theodore studio on` change it for the
  account (`PUT /v1/studio`, the portal switch's own change). A plan without Studio changes nothing.
- install.md: step 3 says to give the welcome and follow its NEXT; step 4 is gone (the welcome says when a new session
  is needed). Launcher pins re-synced; the launcher scripts are unchanged, so the generation does not rise.
- Studio is described by what it means for the plan, with no claim about time or size: on, each slide gets a deeper
  design pass, which uses more of the AI plan; off, slides get a simpler design pass that uses less of it.
- After a resume, the line that names the versions says each label once when the engine and the design text moved
  together: "as 1.0.0-rc.18; the current version is 1.0.0-rc.19".
- The plugin has an icon (lane BRAND): `plugins/theodore/logo.png` (512 x 512), named by `"icon"` in both plugin.json
  files (`claude plugin validate` accepts the field). tools/sync_launchers.py publishes images as they are.

## 0.4.5

The assistant loads Theodore's instructions again after its conversation was compacted or resumed (lane P045).

- After a compaction, Claude Code keeps the start of each skill's text (its first 5,000 tokens) and none of the
  commands' output. Theodore's skill text is a short stub whose first line brings in 20,000 to 33,000 characters of
  instructions, and the stub's own sentence saying how to load them again came after them, so it was cut. A resumed
  conversation holds the instructions as old as their first load (a real session ran five days on a version nine
  releases old).
- The session note: when `guide <skill>` (the entry text or a section) or `skill <skill>` prints instructions and the
  command carries Claude Code's session id (`CLAUDE_CODE_SESSION_ID`, set for every command Claude Code runs), the
  client notes the skill, the version it served and when, in `state/sessions.json` (written whole under a lock, at most
  200 sessions, none older than 30 days). Without a session id nothing is noted. The tool server's answers are not
  noted.
- One hook, `hooks/hooks.json`: `SessionStart` for `compact` and `resume` runs the launcher's `session-start`. For a
  session the note says loaded Theodore's instructions, it prints one paragraph saying to load them again (naming only
  the skills that session loaded) and to read the deck's design note again: after a compaction always, after a resume
  only when a newer version of what it loaded is current. A resume of such a session asks the service once for the
  current versions (the release manifest, two seconds at most; on any failure the last update check's values stand); no
  other case uses the network. Anything else, a bad input, no home or a missing client prints nothing; it always exits 0.
  The command line is `sh "<plugin>/skills/pptx/scripts/theodore" session-start; exit 0`, so where Claude Code runs it in
  PowerShell (Windows without Git Bash, where Theodore does not load anyway) it ends with exit code 0 and no output.
  Theodore never opens the conversation: the hook decides from the note alone.
- The launchers answer `session-start` before anything else: with Theodore not set up, nothing is created, downloaded
  or printed, and the exit code is 0; once set up, what the client prints when it succeeds. Launcher generation 12.
- `pptx start` and `pptx apply`, in a session that loaded the design instructions, say once per version, in one line on
  standard error, when a newer version of them is current and how to load it; `pptx start` in a session that did not load
  them says once how to load them. Standard output stays the engine's own (one JSON object when piped).
- The stubs: one paragraph before the injected instructions says to load them again with `theodore guide <skill>` when
  they are cut short or gone; the sentence that said so after them is removed. The Windows cmd line of the stubs ends at
  its command again (since 0.4.1 the next sentence was joined to it).

## 0.4.4

Three filed reports, all in the client.

- From a chat, an icon named by words is found again (TR-X7W78H). Every Node process the engine starts for a chat runs
  under Node's permission model with Theodore's guard loaded first. Node started with inline code (`node -e`, as the
  engine resolves an icon named by words) loads a `--require` file only when it may read it, and the guard's own file
  was not among the readable paths: the process stopped with ERR_ACCESS_DENIED before any code ran, so `assets show
  icons <words>` failed while an exact id worked. The guard's file is now readable. Nothing else is: the network stays
  closed and a deck script still cannot start `node -e`.
- From a chat, the build manual's own sections can be read (TR-G78T5N). The manual sends the assistant to its served
  section `slide-parts`; the chat tool's `guide` action answered for the design guidance only, a chat has no launcher,
  and the section is not on disk, so it could not be read at all. The action now looks for a section in the design
  guidance and, when that has none of that name, in the build instructions; a name neither has gets the same answer as
  before. The two sentences of the manual that name a launcher (the links sentence of the service's launcher form,
  and "call the Theodore guide tool with skill ... (or run the launcher: ...)") are said for the chat tool. The tool's
  schema is unchanged apart from the words of the action's description.
- A sheet folder another call is filling is no longer removed (TR-S9N9N8). Every call first removes sheet folders older
  than an hour, with the time it read at its start, and treated any folder dated ahead of that time as not to be
  trusted. A folder another Theodore call made, or put a sheet in, a moment later was therefore removed under that
  call, whose next sheet failed with `INTERNAL FileNotFoundError` naming a sheet of the batch (seen on Windows with
  several agents fetching sheets at once; possible on every system). A folder up to a minute ahead of the clock is now
  kept, as the launchers' own sweep already did; one dated further ahead still goes. A folder that does go while the
  sheets arrive (an uninstall, a sign-out in another window) is answered as a retry, not as an internal error.
- The check runs its platform jobs in four lanes and its suites in four processes by default on a machine with 8 or
  more cores (the whole check in about 16 minutes against about 60), the Linux jobs on the standing build machines when
  they exist, and a standing machine's cleanup is bounded.

## 0.4.3

- Windows launcher: the "is the home writable" probe can no longer collide between launchers (lane P043). cmd has no
  process id and seeds %RANDOM% from the clock in seconds, so launchers started in the same second drew the same probe
  file name; one deleted the file another had just written, and that one answered `SETUP_FAILED cause DISK ... not
  writable` (exit 68) on a writable home (seen with 8 launchers at once on an empty home). The probe is now a folder
  made with mkdir, which is atomic and fails when the name exists: only the launcher whose mkdir made it writes in it
  and removes it, and one that drew a taken name tries the next (the attempt number is part of the name, 32 at most).
  The sh launcher's probe is named by its process id (`$$`), unique among running processes: unchanged.
- Launcher generation 11.

## 0.4.2

- The reader's role (lane P042b, the combined release's F2): `theodore guide <skill> [section] --role slide` sends
  `role=slide` on every request for that text (whole, or each part), so the service composes it for an agent given one
  slide; the output is printed as before (whole under Claude Code, parts elsewhere). Every command the client writes
  for a further section or part of that text carries `--role slide`: the links it turns into commands, the next-part
  line, the opening's tool arguments and the "no such section" status. `--role lead` or no flag sends nothing new; any
  other value is a usage status. The plugin's local tool server takes the same optional `role` in its `guide` tool and
  in the chat tool's `guide` action, holds each text per role, and passes it to the service.
- The engine's pptx manual (its SKILL.md on disk, never served) is composed for the role by the client: marker lines
  `<!-- theodore:if lead -->` / `<!-- theodore:end -->` (whole lines, outside fenced blocks) are removed; with
  `--role slide` (or the tool's role "slide") the lead blocks go too. A file without markers prints as before; markers
  that do not compose (unclosed, an end alone, nested, or any other theodore marker such as Studio's) lose no text: the
  marker lines go, nothing else, and the client log says so. The same composition on every route that hands out the
  manual: `guide pptx`, the skill body, the plugin tool's pptx guide, and the chat tool's `manual` and a `read` of it.
- The chat tool's rewrite of the service's link sentence matches both of the service's wordings: the lead's and the
  slide's (`... with section "<name>" and role "slide", ...`), which becomes the chat tool's `action "guide" and
  section "<name>" and role "slide"`.
- Close-ups by bare code (F1): `theodore specimen HC3` and the tool server's specimen action take a code matching
  `^[A-Z]{2}[0-9]{1,3}$` as the file `HC3.jpg`; file names work as before (at most 8 a call). A code and its file name
  are one held sheet, with the same ETag revalidation. The tool's `file` schema accepts the code pattern beside the
  index's file names.

## 0.4.1

- Theodore's own short command (lane PROMPTS): with the host settings, the set-up writes `theodore`, a two-line script
  that only runs the stable launcher, into the first per-user folder on the shell's PATH: `~/.local/bin` (where
  Claude Code's own installer puts `claude`) or `~/bin`; on Windows `theodore.cmd` plus a `theodore` script for Git
  Bash in `%USERPROFILE%\.local\bin` or else `%LOCALAPPDATA%\Microsoft\WindowsApps` (on every user's PATH on a stock
  Windows). Never over a file that is not Theodore's, never where another `theodore` comes first on PATH, not written
  again once the person deletes it; `hosts settings --remove`, `hosts unregister` and the uninstall delete it.
- While that command is on PATH, every text the client writes for the assistant (statuses, the Bindings lines, the
  guide's section links and specimen line, "the launcher you used") names `theodore …` instead of the quoted long path,
  which the model kept in a variable (`T="…"; "$T" …`, `$t=…; & $t …`) that no permission rule can match. The routing
  line in CLAUDE.md and AGENTS.md, which the model reads first, names it too (`run theodore uninstall`), and is brought
  back to the full path when the short command goes. The Bindings block gains one line: Theodore's other commands
  (guide, specimen, status, report, ...) -> `theodore <command>`.
- Rules (Claude Code 2.1.292, measured headless in default mode): `Bash(theodore *)` matches `theodore …` alone, after
  `cd`, in pipes, `for` loops, here-documents and `T=theodore; "$T"`; written only while the short command is there and
  taken out when it goes. On Windows, for the PowerShell tool: `PowerShell(theodore *)` and `PowerShell(& theodore *)`
  (both measured), and for the launcher and each skill launcher by path `PowerShell(<path> *)` and
  `PowerShell(& "<path>" *)` (the `&` form of a path matched no rule in the macOS probe; lane WINDOWS checks it on
  Windows). Codex: `prefix_rule(["theodore"])`. `Read(//<Claude Code's plugin cache>/theodore/**)`: Theodore's plugin
  files, so `ls <skill folder>/scripts` outside the working folder does not ask.
- `Edit(//**/.theodore/workspaces/**)`: the assistant writes and edits a deck's working files (`deck.js`, notes) in the
  workspace folders Theodore's engine makes inside the user's folder without a file prompt. Nothing else; Theodore's
  own folder has no `workspaces` subfolder. Anchored at the root (`//**`): a relative pattern is read against Claude
  Code's current folder, which moves when the assistant `cd`s into the workspace (measured: the Edit then asked). Said in the installer's last lines, install.md, README and SECURITY.md.
- Codex on Windows starts Theodore's tool server: Codex reads one `mcp.json` on every system and the published one
  starts it with `sh`, which Windows lacks; on Windows Theodore's own copy of the plugin (Codex's marketplace) now
  starts `./skills/pptx/scripts/theodore.cmd`, rewritten at every unpack so plugin updates keep it.
- install.md: when the host holds the installer back (an automatic safety check), the assistant changes nothing and
  tries no other route; it tells the user their assistant held Theodore's installer for their approval, where to give
  it (Claude Code: /permissions, Recently blocked; the app's notice) or to reply "Yes, download and run Theodore's
  installer", then runs the same line again. Measured with the line held 6 times: 6 stopped with no workaround, and
  the reply let the same line run 6 of 6. The line itself is unchanged.
- W-9: `doctor` on Windows says Theodore's own PortableGit is downloading (and how far), with nothing to install,
  instead of telling the person to install Git for Windows meanwhile.
- W-12: the Windows uninstall's cleanup leaves its own folder before removing it (after `(goto)`, on the same line,
  since the script's `setlocal` puts the saved folder back when the batch ends), so no empty
  `%TEMP%\theodore-uninstall-<random>` is left.
- W-16: a sign-in the service refuses for its per-account (or per-network) hourly limit (HTTP 429 `rate_limited`) is no
  longer reported as a bad code. install.sh, install.cmd and `login --code` say there have been too many log-ins on
  this account in the last hour, that the code was not used, and to try again in N minutes (from the answer); the
  installers install everything else and end with that status (RETRY, 75).
- Skill stubs: "Theodore's command is `theodore`: write every Theodore command as `theodore …` in every shell", and the
  skill folder's launcher by absolute path only "if the shell answers that `theodore` is not found" (lane WINDOWS's
  recount: a model given "where that command is on PATH, else …" took the path).
- `doctor` says whether `theodore` is on this shell's PATH, and plainly when it was written but is not found there.
- W-17: a sign-in the service refuses because the account already has its maximum number of computers logged in
  (HTTP 409 `conflict`, `details.reason` `computer_limit`) is no longer reported as an expired or used code followed by
  a sign-in link that fails the same way. `login --code`, `login --finish` and both installers (install.sh,
  install.cmd) end with `THEODORE COMPUTER_LIMIT` (exit 70, the code of NO_ENTITLEMENT; the backend's frozen list does
  not name it yet) carrying the service's own message: no link, nothing to retry; the person removes a computer in
  Settings, under Computers and assistants (`/app/settings#connected`), then pastes the same set-up sentence again (the
  code was not used and works until its hour is up). The installers also no longer call a code "not accepted" when the
  service could not be reached or answered 5xx. `login --code`'s wrong-code line says codes last an hour (it said 10
  minutes).
- The Claude app's chat (lane CHATASK): for a Claude app client only (its chat, `claude-ai`, or its agent mode,
  `local-agent-mode-…`; never Claude Code, Codex or an unknown client), the `check` answer's TELL THE USER line adds
  the sentence the person can say so the chat uses Theodore for every presentation without naming it ("From now on,
  use Theodore for any presentation, deck or slide request. Please remember that."), and so does what the finish
  answer tells the user, for the first two decks delivered on this computer, and only if the user had to name
  Theodore to get the deck (`~/.theodore/state/chat-tip.json`; a missing or unreadable count is zero and never fails
  a delivery). Installed from the Code tab, the person learns it at install: `hosts register`, when Theodore's entry
  in the Claude app was written or already there, and `doctor`, while the app still has to be reopened, add "In the
  Claude app's chat, after reopening the app, say once: "…" and it uses Theodore for every presentation." No answer asks the assistant to save or remember
  anything, or to ask the user (the chat changes its memory only on the person's own word); a test reads every string
  of the local server's answer code for it. The "is the Claude app" rule now includes `local-agent-mode-…`, so the
  plugin's own server stays silent there too beside Theodore's entry.
- The chat route's default folder (lane CHATFOLDER): on macOS a deck with no folder named goes to `~/Theodore`, not
  `~/Documents/Theodore`: Documents is protected, and the first touch by Theodore's Python raised a system prompt naming
  "python3.x" (Don't Allow then failed every start with exit 1). Decided once from Theodore's own state, never by looking
  in Documents, and kept in `~/.theodore/state/chat-folder.json`: a Mac whose chat route ran before (0.4.0's
  `state/chat` is there) keeps `~/Documents/Theodore`, where its decks are. Windows and Linux keep `Documents/Theodore`.
  A default that cannot be written gives way to `~/Theodore` (same confinement checks), and the answer tells the user
  in one sentence where the deck is going and why; a folder the chat named that cannot be written is a USAGE status
  that names it, with NEXT: ask the user for another folder, or leave folder out.
- The chat route's build manual carries no update notice ("THEODORE NOTICE: Theodore updated itself: ... Tell the user
  once"): it said what changed since the last skill load at any earlier time (in the acceptance run, the install's
  plugin update three minutes before), and the chat told the person "Theodore updated itself to a newer version while
  building" when nothing had. Nothing is applied during a build, so a chat says nothing about versions; the notice
  stays pending for Claude Code's next skill load. The Claude app entry's command is pinned as Theodore's stable
  launcher (`~/.theodore/bin/theodore mcp --for claude-desktop`).
- W-15: one LibreOffice install at a time: a second one, while one runs, reports the first one's progress instead of
  racing it over the same file and ending INTERNAL.

## 0.4.0

- A text is read whole (lane WHOLETEXT): under Claude Code (CLAUDECODE=1), `theodore guide <skill> [section]`
  without `--part` prints the whole text as one document (one `form=whole` request; an older paging service is asked
  for every part in order, and a part that cannot be had prints nothing of the text and names the missing part), so
  the host keeps it whole inline or in its own saved-output file; Theodore still writes no guidance text to disk.
  Every output begins with short status lines (its length; read a saved output in full, searching is not reading;
  the plugin's guide tool returns it whole). Elsewhere, or with `--part`, each part says at its start and its end
  that it is part N of M and that nothing in it may be acted on before all are read; the last part and a whole text
  end by saying the text is complete. The local server's pptx manual is one document.
- Tests and `ci/run.sh` no longer inherit the launching host's identity (lane WHOLETEXT2): `CLAUDECODE`, `CLAUDE_PID`,
  `THEODORE_HOST`, `CLAUDE_CODE_*`, `CLAUDE_PLUGIN_*` and `CODEX_*` are removed before any target or suite runs, so a
  check started from a Claude Code or Codex session gives the same results; a test of a host sets the variable itself.
  Three tests read the line after the guide's opening lines.
- Claude Desktop's chat builds the deck with Theodore's engine on this computer, set up by the same install, with no
  connector. The local server's one tool (`theodore`, so "Always allow" is still clicked once) gains the actions:
  - `manual`: the build instructions;
  - `run`: one engine command, `pptx <verb> ...` as an argument list, with the design note on standard input, in
    the deck's folder; slide renders come back as images;
  - `read`, `write`, `edit`, `list`: the deck's working files;
  - `reveal`: shows the file in Finder or Explorer, only when the user asks.

  The deck goes to the folder the chat names (its path on this computer), or else `~/Documents/Theodore`. Every answer
  names the next step, through to the line that gives the user the file's path. Starting the same deck twice in the
  same folder continues the first, with no `-2` copy.
- Confinement:
  - Only the engine's own verbs run, with no shell and no other program.
  - Every path must be inside the deck's folder; reads may also open Theodore's renders and its manual. Only text
    files are written.
  - The deck script runs under Node's permission model plus a guard loaded first. It can read only the engine, its
    runtime and packs, the fonts, the deck's folder and the render cache. It can write only the folder and the
    cache. It has no network, and no program but the engine's own Node scripts, which run under the same restriction.
  - The engine reads a copy of the licence that exists only while one command runs and goes with the main licence.
- The route is the plan feature `chat-build`. Without it, `run`, `write` and `edit` answer NO_ENTITLEMENT; a plan
  given the feature is picked up within minutes.
- A delivered deck is never replaced from a chat: every start from a chat is a deck of its own (the client passes
  `--new`, so the engine never reopens an earlier deck), and a command that changes a deck (apply, finish, slide and
  comment edits) runs only on a workspace this session started and that is not finished; a finished deck is changed by
  starting a new deck from its file, which gets a name of its own. The engine itself never writes a `--from` source and
  never replaces a file it did not deliver (an existing name gets `-2`; `--output` onto such a file is refused). A
  retried start of the same unfinished deck continues it. Proven on the real engine (tests/e2e/chat_build_proof.py).
- One Theodore in a chat: when a Claude account also has Theodore's plugin on, the plugin's own server, started by the
  Claude app (any client named as the app; Claude Code and Codex are unchanged), serves no tool while Theodore's entry
  is in the app's configuration, and otherwise the chat's one tool, so a person meets one tool and one approval. The
  skills' fallback wording names "the Theodore tool", whichever server provides it.
- Codex, with no folder named: the deck goes to the thread's folder (from `_meta.threadId` and the thread's session
  file), else `~/Documents/Theodore`. Render images named by a path with a space or an accent (a Windows user folder)
  come back to the chat. The chat route starts the background licence check as every engine command does.
- Each chat action is logged by name, verb and exit code only. For testing which name a chat picks,
  THEODORE_CHAT_TOOL_NAME and THEODORE_CHAT_TOOL_DESCRIPTION override the tool's name and description.
- Windows, a user name with a letter outside the console's code page (C:\Users\Théo Tester) (lane WINFIX, W-3):
  uninstall left Theodore's folder behind and still said OK. The cleanup script that deletes the folder after the
  command ends had the paths written into it in UTF-8, and cmd.exe reads a batch file in the OEM code page, so it
  looked for another folder and stopped. The script now holds only ASCII and gets every path through environment
  variables, expanded inside quotes with delayed expansion off (a space, &, %, !, ^ or a parenthesis in a name is part
  of the path; the WMI way for a job that forbids breaking away sets them from an encoded PowerShell command). The
  same script is first asked, synchronously, whether it sees the folder; when it does not, nothing is started and the
  uninstall answers RETRY naming the folder, never OK. Directory junctions take their paths the same way.
- Windows: the engine's self-test errors, and the assistants' CLI output the client reads, are read as UTF-8 (they
  were read in the ANSI code page: "ThÃ©o"); the engine's text streams are UTF-8 on every platform
  (PYTHONIOENCODING=utf-8), as the client's already were.
- Windows batch files (lane WINFIX audit): the installer and the launcher call Theodore's launcher, the setup and
  themselves as `call "%%NAME%%"`, so the path is expanded once, after CALL (CALL doubles every caret, quoted or not,
  and expands % a second time: C:\Users\a^b or 50%off was another path); the lines that name Theodore's folder or
  its stable launcher quote it, so a name with & or a parenthesis stays one path (install.sh quotes it the same way).
- Specimen sheets go in a private folder under Theodore's own cache (macOS `~/Library/Caches/Theodore/specimens`,
  Linux `$XDG_CACHE_HOME/theodore/specimens`, Windows `%LOCALAPPDATA%\Theodore\Cache\specimens`), owner-only and named
  `theodore-specimens-<random>`, which the host settings' Read rule covers, instead of the system's temporary folder
  (shared, swept and indexed by other tools); the temporary folder only when the working folder contains the cache.
  Their life is unchanged: removed after an hour by the next Theodore call, all of them at uninstall.
- Windows Codex install (as install.sh, lane CHATGPT): under `CODEX_THREAD_ID` install.cmd waits up to 540 s for the
  download, with a line a minute while it waits, and, still downloading with every assistant registered, ends with
  THEODORE OK and "run nothing more now, neither doctor nor this installer" instead of an in-progress status to poll;
  ready, it ends with "Theodore is ready (the check above)" and sends the deck to a new session (Codex loads Theodore's
  tool and permissions only when a session starts), both as install.sh.
- Windows, a user name with & or a parenthesis: Claude Desktop's entry, when the launcher path has a character cmd
  treats specially, carries it in the entry's `env` (`THEODORE_DESKTOP_LAUNCHER`, quoted) and runs
  `cmd /d /v:on /c !THEODORE_DESKTOP_LAUNCHER! mcp --for claude-desktop` (delayed expansion comes after cmd has parsed
  the line); other paths keep the entry as it was. The launcher's uninstall of a partial installation reports what is
  left with delayed expansion instead of CALL ECHO, so the message is no longer cut at the &.
- Windows LibreOffice (WINDOWS W-6, a release blocker for every profile folder with a space): `msiexec /a` was run
  from a list, so Python quoted the whole `"TARGETDIR=C:\Users\First Last\..."`; msiexec rejects that form, shows its
  usage window where nobody sees it and waited for ever (31 minutes at "100% unpacking", every rendering call answering
  SETUP_IN_PROGRESS). Theodore's own unpack (msi.dll's tables in process, expand.exe for the cabinet) now goes first:
  no Windows Installer service (which refuses network and batch logons), nothing to quote, progress per file; msiexec
  is the second way, as one command string with `TARGETDIR="..."`, as proven on the VM. Every program the client runs to
  unpack or copy a tool has a hard time limit (expand, msiexec, hdiutil, cp, the engine's venv, junctions), ending in the
  other way or a clear SETUP_FAILED; a rendering call names the phase ("unpacking LibreOffice, 40%"), and a LibreOffice
  job whose record has not moved for 20 minutes is reported as stuck, not as still being installed.
- Windows PowerShell (W-4): the install page's PowerShell line sets the console's output encoding to UTF-8 first, so
  PowerShell 5.1 reads the installer's UTF-8 as UTF-8 (it read it in the console's code page: "Th├⌐o"). The client's
  output stays UTF-8: an assistant reading PowerShell's own output (Codex), Claude Code's Bash and PowerShell tools
  and cmd get it right as it is.
- Windows, Claude Desktop (WINDOWS W-7): "quit and reopen the Claude app" was said when only Claude Code's CLI was
  running (it is claude.exe too). The app counts as running only for its own program files: the per-user build under
  `%LOCALAPPDATA%\AnthropicClaude\app-<version>\` or its package under `WindowsApps\Claude_<...>\`.
- Uninstall's record (WINDOWS W-8): the uninstall's last words name the record it keeps beside Theodore's folder
  (`~/.theodore-uninstall.log`, `%LOCALAPPDATA%\Theodore-uninstall.log`; it can be deleted), and on Windows a new log
  starts with a UTF-8 byte-order mark, so Notepad and PowerShell 5.1 read C:\Users\Théo as written.
- Windows install lines (WINDOWS W-13): Microsoft Defender once flagged install.md's own Windows line as
  Trojan:Win32/ClickFix.EEI!MTB (a one-line PowerShell download into %TEMP% followed by use of the file). It did not
  reproduce in nine later runs, but the shape is what ClickFix rules key on. Every documented Windows line
  (install.md's first line and step 1, the README) is now two separate commands into `%LOCALAPPDATA%\Theodore-install`:
  the folder made first (`New-Item -Force` / `mkdir`: curl's `--create-dirs` fails for a profile named with é), then
  `curl.exe ... --retry 2 --retry-delay 2 -o`; the run, or the read of install.md, is a second command. They were clean
  with Defender on in each way tested (Codex's `powershell -Command`, Claude Code's tool, `-EncodedCommand`, `-File`,
  cmd, Win+R). The installer removes the folder when it ends, as before; the pins and checks are unchanged.
- Tests: the every-address timing bounds (installer and client) are what the code promises (4 s per address in
  install.sh; 0.25 s stagger and 3 s per address in the client) plus half the way to a regression's cost, plus twice a
  fast run of the same script or request measured in the test, so a loaded machine no longer fails them while an
  address tried twice or a doubled timeout still does.
- Host settings (lane WINFIX): Claude Code's rules now also allow Theodore's two skills by their plugin names,
  `Skill(theodore:pptx)`, `Skill(theodore:deck-design)` and each with ` *` (any arguments), so loading them no longer
  asks; and they allow reading the engine's render caches (rendered slides) by their own absolute paths, besides
  Theodore's home and the specimen cache: macOS `~/Library/Caches/Theodore` (and the older engines'
  `pptx-runtime`), Linux `~/.cache/theodore`, Windows `%LOCALAPPDATA%\Theodore` (its `Cache` holds the renders) and
  `%LOCALAPPDATA%\pptx-runtime`. Windows gets Read rules for the first time, in the form Claude Code matches there
  (`Read(//c/Users/.../Theodore/**)`); a folder on a network share gets none. All are recorded like the other rules:
  an installation whose settings are on gets them at its next command, and `hosts settings --remove` and uninstall
  take back exactly what was added.
- While the download goes on after the installer has ended, its NEXT is `theodore status --wait 100`, which waits
  for the runtime and the engine and returns as soon as they are there, instead of doctor every 20 seconds: one
  waiting command in place of a sleep and repeated checks. `status --wait` now also waits for the engine while the
  setup job is fetching it. The install page's check step reports the installer's last line (doctor's) instead of
  running doctor again.

## 0.3.10

- Licence (lane EXPOSURE): an engine command (`pptx …`, `python …`) whose stored licence is more than an hour old
  starts one detached licence refresh, at most one an hour, never waited for and silent. A device revoked, or an
  account suspended, deleted or without a plan, while the computer stays online and runs only engine commands now
  stops within about an hour instead of when its licence expires (up to 7 days). Offline nothing changes.
- Theodore's folder (`~/.theodore`) is this user's alone on macOS and Linux: the group's and others' permissions are
  removed (the owner's are kept), so other accounts on the computer cannot read the engine, its manual or the packs.
- A suspended account (403 `account_suspended`, which the service now also answers for its revoked computers while the
  suspension lasts) is told so with Theodore's address and no sign-in link: a sign-in cannot help it. A plan refusal
  says the plan is not active. After a suspension is lifted, the computer is told it needs one new sign-in.
- `whoami` with a sign-in but no stored licence asks the service once: a refused account gets the refusal's status
  (never "plan unknown … 1970-01-01"); offline it says no licence is stored yet.
- Claude Desktop: when the app is on this computer, `hosts register` (the installer runs it) adds Theodore's local
  server to the app's own `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude`, Windows
  `%APPDATA%\Claude` and the Microsoft Store build's folder): the entry `theodore` runs this installation's stable
  launcher, `theodore mcp --for claude-desktop`. Desktop chat then has one tool, `theodore` (the app asks for an
  approval per tool of a local server, so "Always allow" is clicked once, ever): its `action` is `begin`, `guide`
  (a section), `specimen` (sheets), `report`, `preference_list`, `preference_add`, `preference_remove` or `check`
  (deck-design guidance and specimen sheets through this computer's sign-in, held in memory only; every answer and
  next step names `theodore` and the action, never the remote connector's tool names, which are unchanged), with no connector an organisation Owner must add: local servers are on for Team and Enterprise members
  unless the organisation turns them off. The app loads it when it is quit and opened again. Only that entry is
  written; a second run changes nothing; an entry named `theodore` that Theodore did not write is kept; uninstall
  takes back exactly the entry (and the `mcpServers` object when Theodore created it and it is empty).
- Host settings on by default (lane FRICTION): the installer applies `hosts settings --apply` itself, since the
  person asked their assistant to set Theodore up. Claude Code (CLI, desktop Code tab, VS Code extension): plugin
  auto-update and permission rules for Theodore's own commands only, by their exact installed paths: the stable
  launcher bare and quoted, each skill launcher behind `sh` as the skills run it (Claude Code's plugin cache, one
  folder per installed version, and Theodore's copy), and the plugin's tool server (`mcp__plugin_theodore_theodore`).
  Codex: `~/.codex/rules/theodore.rules`, rules that allow Theodore's launcher by its exact installed paths; with it,
  Theodore's commands run outside Codex's sandbox without asking each time. `--no-settings` leaves them out
  (`--accept-settings` is still accepted and changes nothing); `theodore hosts settings --remove` takes back exactly
  what they added and keeps them off; uninstall takes back exactly what was added. A managed Claude Code policy wins:
  with `allowManagedPermissionRulesOnly`, or a marketplace policy that does not allow Theodore's, that setting is not
  added and doctor says so in one line. An installation from before gets the settings once, at its first Theodore
  command after updating to this version (unless turned off); later, rules are added only for launchers that are new
  (a new plugin version's folder), never again one the user took out. The installer's last lines and doctor say in
  one clause that Theodore's own commands run without asking, and how to turn that off.
- A network where one of the service's addresses does not answer (it takes the connection and never the TLS
  handshake) no longer slows or breaks any client call. The client races the addresses (the next one starts 0.25 s
  after the one before, or at once when it failed; 3 s each to connect and finish the handshake) and keeps the one
  that answered in `state/addresses.json` for 7 days, so the next commands, in other processes too, go straight to
  it. A kept address that stops answering is replaced after the same 0.25 s; one the name no longer resolves to is
  not used. The usage send at the end of a deck (2 s in all) gets through on such a network; a batch that gets no
  answer is still kept and sent later under the same id (never lost, never counted twice).
- The launchers' own downloads (the first runtime and client) try every address the way the installers do (the same
  code, word for word).
- install.md and the one line a summarising fetch gets: each download is tried up to three times, 10 s each to
  connect, since a new attempt usually reaches another address.
- The usage switch comes with the log-in answer (servers from 2026-11-15): a new installation needs no licence
  refresh to learn it. Against an older server the first licensed command still asks once.
- The client never switches below the version its launcher pins, unless the release manifest marks that client as a
  rollback (`client.rollback: true`).
- Device binding, client side (off until the service turns it on): each installation makes a P-256 key at sign-in
  (an installation from before 0.3.9 at its next licence refresh), kept inside the credential; registers its public
  half; and signs a single-use nonce at each licence refresh, with an HMAC of this computer's identifier. A service
  that does not know these routes is left alone for a day. An engine that refuses a licence made for another
  computer (`entitlement_wrong_machine`, engines from rc.10) asks for a log in on this one.
- `uninstall` writes what it did beside Theodore's folder (`~/.theodore-uninstall.log`), not into the folder it removes.
- `ci/run.sh all` starts the Windows job before it waits for the shared lock.
- A cloud sandbox that cannot reach Theodore's sites answers `CONNECTOR_REQUIRED`: the assistant calls the Theodore
  connector (`theodore_begin`) for the design, or says how to add it; an ordinary computer offline keeps its message.
- The log-in link is to be opened in the user's own web browser, not a browser inside the assistant's app.

## 0.3.8

- Theodore now tries every address of its service before reporting a connection failure, so a network that cannot
  reach one of them still works. The client tries each address of Theodore's hosts in turn (3 s each to connect and
  finish the TLS handshake, within the request's own time) and starts with the one that worked; install.sh and
  install.cmd do the same with curl (4 s each), since curl moves on only when the connection itself fails, not when
  the handshake hangs or is reset. With a proxy nothing changes; the certificate is still checked for the host name.
- Usage counts start with a new installation: the first licensed command after a log in (the served entry the skill
  reads first) makes one licence refresh to learn whether the account records usage, instead of waiting up to 12
  hours for the first scheduled one. At most one try an hour while it fails; the command's outcome never depends on it.

## 0.3.7

- Usage counts (lane ANALYTICS): while the licence refresh says `usage_enabled: true`, the client gives the engine a
  ledger path (`THEODORE_USAGE_LEDGER`, the file `state/usage-ledger.jsonl` in Theodore's folder) and records its own
  non-OK statuses there as codes. It sends what waits to `/v1/usage` at the end of `pptx finish`, otherwise at the
  start of a command once the oldest line is 15 minutes old: one attempt, two seconds at most, nothing printed, the
  command's outcome unchanged. Every line is checked against the contract's strict shapes first; the ledger keeps at
  most 2,000 lines, 1 MB and 30 days. A `usage_off` answer deletes the ledger; so does a refresh that does not say
  true, and logging out. `theodore usage --show` prints the next batch exactly as it would be sent. SECURITY.md names
  the file, its fields and when it is sent.
- `ci/run.sh selected` starts its Windows job before it waits for the shared lock.
- Install on a slow connection: the long downloads run as a background job that outlives the installer's command; the
  installer waits for it at most three minutes, registers the plugin meanwhile and ends with `SETUP_IN_PROGRESS` and
  the doctor command to poll, so a host's time limit on a command no longer cuts the install.
- Running the install line again with a code already used keeps the login the earlier run saved.
- `doctor` checks that the plugin is registered with each assistant it finds and says what to run when it is not; the
  next Theodore command finishes a registration an interrupted install left undone.
- An engine install that finds another Theodore process holding the update lock answers `SETUP_IN_PROGRESS`, not
  `INTERNAL`.
- The runtime build refuses a node_modules that lacks a package the lockfile lists for the platform (npm skips an
  optional dependency it could not fetch without an error; a bundle without mathjax-full was made that way).

## 0.3.6

- Uninstall stops Theodore's own running processes first: every background job, the launcher's bootstrap and each
  tool server, each found by the file Theodore wrote for it (never by name), with the processes they started. On
  Windows a cleanup removes the folder in one run, retrying for up to two minutes while Windows releases the last
  files, outside the console's job so a closed console or SSH session does not stop it; the status is OK and never
  asks to run the launcher again (it was already deleted). Before, the background LibreOffice download's python.exe
  kept the runtime, the folder stayed, and the status asked to run a command that no longer existed.
- Uninstall removes the engine's LibreOffice profile, its warm copy and lock, and its lock files from the temporary
  folder (only by the engine's names).
- "Theodore updated itself" is said once per update, also when two sessions start together, and one plugin update
  runs per release.
- The routing block (version 2) adds one clause naming Theodore's own uninstall command with the launcher's quoted
  path: asked to uninstall Theodore, Claude Code on macOS and Windows used to remove the plugin by hand and leave
  Theodore's folder, sign-in and block. Existing blocks are updated in place.
- An install right after an uninstall stays signed in on Windows: the first uninstall's cleanup works only while its
  marker is in the folder, and the installer and the launcher remove that marker (and wait for a pass in progress)
  before they write anything. Before, the cleanup, still running, removed the new installation's sign-in as it was
  written: "signed in (the one-time code was accepted)", then SIGN_IN_REQUIRED.
- A download that arrives damaged (a flaky network) is fetched afresh, three times at most, by the client (the
  runtime, the engine, packs, LibreOffice, PortableGit) and by both installers (Python and the client) before the
  install stops; a connection that drops on the way is retried the same way.
- Launcher generation 10.
- `guide pptx <section>`: a section the service serves to this account, held in memory like deck-design's sections;
  a section it does not serve is a clean USAGE (lane STUDIO, PR #7).
- The runner's per-target record copies whatever logs the target wrote (PR #12).

## 0.3.5

- Uninstall takes Theodore out of Claude Code and Codex also when `claude` or `codex` is not on PATH (Claude Code's
  Windows installer leaves `%USERPROFILE%\.local\bin` off PATH; npm, nvm, Volta and Homebrew folders are often only on
  an interactive shell's PATH). Before, the Claude Code step was skipped without a word, its plugin stayed enabled, and
  its next session ran the plugin's launcher, which downloaded the runtime again. The command is looked for where
  each assistant's installers put it and at the path recorded at registration (now recorded); when it is nowhere, the
  entries Theodore's registration added are taken out of the files directly, each only while it holds what the
  registration wrote (Claude Code: `settings.json` `enabledPlugins."theodore@theodore"` and
  `extraKnownMarketplaces.theodore`, `plugins/installed_plugins.json`, `plugins/known_marketplaces.json`, and its copy
  of the plugin; Codex: the `[marketplaces.theodore]` and `[plugins."theodore@theodore"]` tables, by their lines
  only), and the uninstall says which files it edited (JSON is written again: same content, spacing may differ).
  Whatever it cannot take back is named in its list with the file and the keys. A record from an earlier client is
  taken back the same way. The launchers' own uninstall (without Theodore's Python) looks in the same places and names
  a registration it cannot reach.
- `hosts register`, `doctor`, the routing block and the Windows Git Bash check find the assistants off PATH too. A
  registration run inside Claude Code (or Codex) that cannot find its command says FAILED instead of a success with no
  skills.
- Every uninstall path (the client, both launchers, `install.sh --uninstall`, `install.cmd --uninstall`) removes the
  engine's caches from before 1.0.0-rc.7: `~/.cache/pptx-slide-raster` (images of the user's slides),
  `~/.cache/pptx-icons`, `~/.cache/pptx-flags`, the icons' fallback in the temporary folder, and `pptx-runtime` under
  `$XDG_CACHE_HOME` when set; only files of the names the engine writes, a folder only when nothing else is in it, and
  says what it removed or left.
- The served pptx entry is never dropped silently: three attempts in a 20-second budget, then a status line first in
  the skill body, `guide pptx` and the tool server (lane STUDIO's part of PR #7; `guide pptx <section>` waits).
- The installers remove their own download folder when they end (only a `tmp.*` or `theodore-*` folder holding
  nothing but the installer), so an install line an agent split in two and whose cleanup it dropped (Codex) leaves
  nothing behind. The Windows lines tolerate the folder being gone already.
- Downloads in the background that stopped (sleep, sign-out, a killed process) start again at the next guide, skill
  or doctor: LibreOffice and PortableGit now, as the packs since 0.3.3 and the runtime and first engine through the
  setup job; a failure that passes by itself is retried a minute later at the earliest, one that needs the user is
  reported with the command that tries again. `doctor` has a "still missing" line naming each missing part and what
  will fetch it.
- Updates need no user action. The first Theodore command of each assistant session checks the channel (one request;
  the six-hour rule stays the floor within a session), so a release reaches a user at their next session; nothing is
  applied in the middle of a build. When a newer plugin is published, a background job updates the plugin's files
  through each assistant's own command (Claude Code: `claude plugin marketplace update theodore`, then
  `claude plugin update theodore@theodore`; Codex: Theodore's own copy refreshed, then `codex plugin add`), used
  from the next session. What updated itself (client, engine, asset packs, plugin files, with the release's note when
  it has one) is said once, in one line, at the next skill load or guide. Where an assistant cannot be updated from
  outside (claude.ai's account-level plugin) or its command failed, one line with that assistant's action, once per
  release; `doctor` says what is pending and why. install.md, README and SECURITY say so.
- Claude Code starts the plugin's tool server by the launcher's path instead of `sh <path>`: on Windows there is no
  `sh` on PATH, so every Windows user saw Theodore's server as failed; the path resolves to `theodore.cmd` there and
  the server connects (Windows Server 2022, Claude Code; macOS too).
- `doctor` and `tools install libreoffice` name the platform's real way to get LibreOffice: on Linux the package
  manager's command (apt, dnf, yum, zypper, apk, pacman) or plain words, never a command that is not supported there.
- `update --apply` says what was updated (client, engine, asset packs) and what was already current, instead of
  "nothing to update" while the client moved.
- install.md's first line has the agent read it into a new private folder, never straight into /tmp, and the
  installers remove any copy of it they can identify by its first line (their own folder, the temporary folder, a
  private folder there): it holds the one-time code.
- The archive builders never take macOS metadata files (AppleDouble `._*`, `.DS_Store`); a test checks every archive.
- Launcher generation 9.

## 0.3.4

- The plugin's tool server keeps each specimen sheet and each reference it fetched in memory for the session (never
  on disk), so a lead and its sub-agents opening the same sheets cost one fetch per sheet per guide version. Held
  sheets are confirmed with the service by one conditional request at most every 60 seconds; held references are
  asked for conditionally on every call. A revoked device is refused on its next reference call at once and on its
  next sheet call within 60 seconds, and nothing held is served after a refusal.
- `theodore specimen` reuses a sheet it already holds in a recent specimen folder (at most 45 minutes old) after a
  conditional check with the service: nothing new is written or downloaded. A refusal that signs the device out or
  refuses the account removes every specimen folder at once.
- Every refusal (also the channel's kill switch and a policy block) empties what the tool server holds, and the
  60-second confirmation is checked for every sheet served, also within one call. The tool server holds at most the
  sheets and references of one guide version (64 sheets and 64 MiB; 64 references and 16 MiB of text), oldest first
  out; a new version drops everything of the old one.
- Any command that learns the device is signed out, revoked, suspended, refused or blocked removes every specimen
  folder, and any command (the tool server on each call) removes folders older than an hour.
- The pptx skill's description is the engine's new one (it opens by saying the skill is this session's slides skill,
  installed by the user), copied byte for byte.
- README and SECURITY name the portal's pages as they are now: the Theodore home page (the sentence to send) and
  Settings, Computers and assistants (where a computer is removed).
- Routing: `hosts register` (the installer runs it) writes one line between `<!-- theodore:begin -->` and
  `<!-- theodore:end -->` in `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` (CLAUDE_CONFIG_DIR / CODEX_HOME when set),
  naming Theodore for every presentation, deck or set of slides in any format. Without it, Claude Code's own Slides
  artifact (VS Code, desktop, web) takes "make me a deck" before any skill loads. A file is created only when absent,
  nothing outside the markers is touched, a second run changes nothing, and a new wording replaces the old in place.
  An install from a marketplace alone gets it at its first `guide`, `skill` or `doctor`; a block the user deleted is
  not written again except by `hosts register`. It is recorded in `state/host-settings.json`; `hosts unregister` and
  `uninstall` (also `--keep-hosts`) remove exactly the block, and the file only when Theodore created it and nothing
  else is in it. `doctor` lists the files that hold it.

## 0.3.3

- The assistant is reported as itself: Claude Code (also from the plugin's MCP server and a skill's load command,
  through `CLAUDE_PLUGIN_ROOT`), `claude-web` for a Claude cloud session (claude.ai chat, Cowork, Claude Code on the
  web), Codex (any install, through the variables Codex sets), `chatgpt` for ChatGPT Work or a Codex cloud task. The
  installer sends the same at activation instead of `installer`.
- Through a proxy the client waits at least 25 s for an answer: ChatGPT Work's proxy spends 7 to 10 s on each
  connection, so `doctor` (5 s), the heartbeat (2 s) and the update check (3 s) timed out there although the
  service was reachable.
- In a cloud sandbox whose network refuses or cannot resolve Theodore's sites, the installer, the launcher and the
  client name the setting to change in Claude (Settings > Capabilities > Allowed websites: theodoreand.ai and
  *.theodoreand.ai) and say that ChatGPT's ordinary chat has no internet access while Work mode does.
- The device sign-in in a cloud workspace says why each chat's workspace is approved once, with the one-click link.
- install.md: its first line says to read it as downloaded with curl (quoted address), then run the line for the
  shell exactly; one line says it may run in a cloud sandbox session.
- Windows (lane WIN): asset packs come back for a catalogue call; LibreOffice is fetched when nothing can render
  (about 350 MB, in the background) and the deck waits for it; doctor says when Claude Code has no Git Bash.
- Windows (lane WIN): Git Bash for Claude Code on Windows without Git (PortableGit, pinned, into Theodore's folder;
  CLAUDE_CODE_GIT_BASH_PATH set and taken back on uninstall); LibreOffice unpacks without Windows Installer when the
  service refuses (1601); only x64 runtime DLLs are copied beside soffice.
- The deck-design body loaded by the skill names this launcher's path in its reporting line; the service names the
  launcher by where it is when its text reaches the agent through a tool.
- Launcher generation 8.

## 0.3.2

- The repository is the production build; public copy; a Windows write-test race.
