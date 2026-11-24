"""Building a deck from a chat: Theodore's engine on this computer, run by the local server's one tool (`mcp --for
claude-desktop`, actions run, manual, read, write, edit, list, reveal).

A chat's own commands run in a cloud or a virtual machine, never on this computer, so the chat builds through this
server: it runs ONE Theodore engine command at a time (an argument list, never a shell string, only the engine's own
verbs, `pptx <verb> ...`) and reads and writes the deck's working files. What is confined, and how:

- Commands: the first argument is one of the engine's verbs (VERBS); `python`, a shell, or any other program cannot be
  named. Options that drive another application (`doctor --powerpoint`, `--oracle`) are refused.
- Folders: every path in the arguments, and every path a file action names, is resolved (symbolic links followed) and
  must lie inside the deck's folder (the one the chat named, or the default: ~/Theodore on macOS,
  ~/Documents/Theodore elsewhere) or Theodore's render cache;
  reads may also open the engine's own manual and references. Writes are text files only (WRITABLE). The deck's folder
  itself is never the filesystem's root, the home folder, a hidden or system folder, or one that holds Theodore.
- The deck script (deck.js, model-written JavaScript the engine runs with Node): every Node process the engine starts
  for a chat runs under Node's permission model (`--permission`): it reads only the engine, the runtime, the asset
  packs, the deck's folder, the render cache and a private state and temporary folder, and writes only the last three
  and the deck's folder; no child process, worker, inspector, WASI or addon outside the runtime. node_guard.js removes
  the network (Node 24's permission model does not cover it). The engine reads its licence from a copy in that private
  state folder, so the script never sees Theodore's own state.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import account, core, install, usage
from .core import Status

# The engine's verbs (`pptx --help`). Nothing else can be run.
VERBS = ("start", "apply", "finish", "render", "slide", "comments", "assets", "list", "doctor", "split", "rebase", "task",
         "project", "cache")
# Options that would make the engine drive another application (PowerPoint through AppleScript or COM).
REFUSED_OPTIONS = ("--powerpoint", "--oracle")
# Option values that are text, never paths: a brief with a slash in it is not a path.
TEXT_OPTIONS = ("--brief", "--objective", "--name", "--text", "--author", "--initials", "--slides", "--translate", "--reply",
                "--resolve", "--status", "--keep", "--by", "--tasks", "--cols", "--after", "--promote-current-as", "--deck",
                "--query", "--kind", "--tag", "--pack", "--id")
PATH_SUFFIXES = (".pptx", ".potx", ".pptm", ".ppsx", ".js", ".md", ".json", ".jpg", ".jpeg", ".png", ".svg", ".csv",
                 ".xlsx", ".txt", ".pdf", ".gif", ".webp")
WRITABLE = (".js", ".md", ".json", ".txt", ".csv")
TEXT_READABLE = (".js", ".md", ".json", ".txt", ".csv", ".xml", ".log", ".svg")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
IMAGES_MAX = 12                     # images one answer carries
IMAGE_BYTES_MAX = 3 << 20           # one image
IMAGES_BYTES_MAX = 12 << 20         # all images of one answer
READ_CHARS_MAX = 200_000
WRITE_BYTES_MAX = 2 << 20
OUTPUT_CHARS_MAX = 60_000           # a command's output, kept whole up to this (head and tail beyond)
RUN_TIMEOUT_S = 900
PROGRESS_EVERY_S = 5.0
DEFAULT_FOLDER = ("Documents", "Theodore")
# On macOS the Documents folder is protected: the first time the server's Python touches it, macOS asks the person
# whether "python3.x" may (a prompt naming a program they never heard of; Don't Allow fails every build). The home
# folder is not protected, so a Mac's default is ~/Theodore. Decided once, from Theodore's own state (never by looking
# inside Documents, which may itself raise the prompt), and recorded in state/chat-folder.json so it never moves:
# a Mac whose chat route ran before this record existed (state/chat, the engine's private home, is there) keeps
# Documents/Theodore, where its earlier decks are.
HOME_FOLDER = ("Theodore",)
FOLDER_STATE = "chat-folder.json"
# Said once to the user when the default folder could not be written and the deck goes to ~/Theodore instead.
NOTICE: list[str] = []

# The one tool's name in Claude Desktop's chat: `theodore`. THEODORE_CHAT_TOOL_NAME overrides it, for testing which
# name a chat picks for a plain request (letters, digits, _ and -, at most 64); every answer follows the name in use.
_NAME = str(os.environ.get("THEODORE_CHAT_TOOL_NAME") or "").strip()
TOOL = _NAME if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", _NAME) else "theodore"

# Folders this server process started a deck in (or was given): later calls may name paths inside them without
# naming the folder again. In memory only; one Claude app process serves every chat, so it never widens beyond the
# folders a chat named.
FOLDERS: list[Path] = []
# Workspaces this server process started (or continued) and that are not finished: the only ones a command may change.
OPEN: set[str] = set()
# Engine verbs that change a workspace or its delivered deck; `slide list` and `slide show` only read.
CHANGING = ("apply", "finish", "slide", "comments", "split", "rebase", "task", "project")
READING_SLIDE_OPS = ("list", "show")

# Set by the server for each call, in the call's own thread (the server answers calls side by side): the request's
# _meta (Codex sends its thread id there) and the sender of progress notifications for the call (None outside a call).
_CALL = threading.local()


def set_call(progress, meta: dict | None) -> None:
    _CALL.progress, _CALL.meta = progress, (meta if isinstance(meta, dict) else {})


def call_meta() -> dict:
    meta = getattr(_CALL, "meta", None)
    return meta if isinstance(meta, dict) else {}


def call_progress():
    return getattr(_CALL, "progress", None)

# The Claude app's chat sends a plain "make a presentation" to its own slide tool until the person has said once, in
# their own words, that Theodore is to be used; the chat then keeps that in its memory (measured 3 of 3). The chat
# changes a person's memory only on the person's word, so no answer here asks the assistant to keep anything: the
# person is only given, through what the assistant tells them, the sentence they can say if they want it. Claude app
# clients only (APP_CLIENT, set by the server from the client's name at initialize); never Claude Code or Codex.
USE_THEODORE_SENTENCE = "From now on, use Theodore for any presentation, deck or slide request. Please remember that."
USE_THEODORE_TIP = f'To have it used for every presentation without naming it, say: "{USE_THEODORE_SENTENCE}"'
# Installed from Claude's Code tab, a person never pastes the set-up sentence into a chat: the set-up tells them, when
# the Claude app's entry is there (commands.hosts register, and doctor while the app still has to be reopened).
INSTALL_TIP = (f'In the Claude app\'s chat, after reopening the app, say once: "{USE_THEODORE_SENTENCE}" and it uses Theodore '
               f'for every presentation.')
APP_CLIENT = False
# The tip also goes with the first decks delivered on this computer through the chat route, then never again.
TIP_DECKS = 2
TIP_STATE = "chat-tip.json"


def is_claude_app(name: str) -> bool:
    """The client is the Claude app: its chat (a name with "claude" in it other than Claude Code's "claude-code", e.g.
    "claude-ai") or its agent mode ("local-agent-mode-..."). Claude Code, Codex and anything unknown are not."""
    lowered = str(name or "").lower()
    if lowered.startswith("local-agent-mode"):
        return True
    return "claude" in lowered and not lowered.startswith("claude-code")


def delivery_tip() -> str:
    """The tip for a deck just delivered: for a Claude app client and the first TIP_DECKS decks on this computer, counted
    in Theodore's state folder; else empty. A missing or unreadable count is zero; the count never fails a delivery."""
    if not APP_CLIENT:
        return ""
    try:
        path = core.home().state / TIP_STATE
        record = core.read_json(path)
        shown = record.get("decks_with_tip") if isinstance(record, dict) else 0
        shown = shown if isinstance(shown, int) and not isinstance(shown, bool) and shown >= 0 else 0
        if shown >= TIP_DECKS:
            return ""
        with contextlib.suppress(Exception):
            core.write_json(path, {"decks_with_tip": shown + 1}, 0o600)
        return USE_THEODORE_TIP
    except Exception:  # noqa: BLE001  never in the way of the delivered file
        return ""


# --- folders and paths --------------------------------------------------------------------------------------------------

def _is_dir(value) -> bool:
    """A directory, never an error: an argument may be a long brief (ENAMETOOLONG) or any other text."""
    try:
        return Path(value).is_dir()
    except (OSError, ValueError):
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def _real(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


def _inside(path: Path, root: Path) -> bool:
    a, b = (str(path), str(root))
    if core.IS_WINDOWS:
        a, b = a.lower(), b.lower()
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def cache_root() -> Path:
    """The engine's per-user cache (renders, icons), as the engine computes it (workspace_paths.user_cache_root)."""
    override = str(os.environ.get("THEODORE_CACHE_DIR") or "").strip()
    home = Path.home()
    if override:
        path = Path(override).expanduser()
        return _real(path if path.is_absolute() else home / path)
    if sys.platform == "darwin":
        return _real(home / "Library" / "Caches" / "Theodore")
    if core.IS_WINDOWS:
        local = str(os.environ.get("LOCALAPPDATA") or "").strip()
        return _real((Path(local) if local else home / "AppData" / "Local") / "Theodore" / "Cache")
    xdg = str(os.environ.get("XDG_CACHE_HOME") or "").strip()
    return _real((Path(xdg) if xdg else home / ".cache") / "theodore")


def sandbox_home() -> Path:
    """The THEODORE_HOME the engine sees for a chat: a copy of the licence and its own clock mark, nothing else."""
    return core.home().state / "chat"


def default_folder() -> Path:
    """The deck's folder when the chat names none (see HOME_FOLDER)."""
    home = Path.home()
    if sys.platform != "darwin":
        return home.joinpath(*DEFAULT_FOLDER)
    path = core.home().state / FOLDER_STATE
    recorded = core.read_json(path)
    recorded = recorded.get("default") if isinstance(recorded, dict) else None
    if recorded in ("documents", "home"):
        return home.joinpath(*(DEFAULT_FOLDER if recorded == "documents" else HOME_FOLDER))
    earlier = _is_dir(sandbox_home())           # the chat route ran here before the record: its default was Documents
    with contextlib.suppress(Exception):
        core.write_json(path, {"default": "documents" if earlier else "home"}, 0o600)
    return home.joinpath(*(DEFAULT_FOLDER if earlier else HOME_FOLDER))


def default_label() -> str:
    """The default folder as the texts name it."""
    return "Theodore in the user's home folder" if sys.platform == "darwin" and default_folder().parent == Path.home() \
        else "Documents/Theodore"


def _writable_folder(folder: Path) -> bool:
    """The folder exists (made when missing) and a file can be created in it: a protected or read-only folder is not."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handle, probe = tempfile.mkstemp(prefix=".theodore-write-check-", dir=folder)
        os.close(handle)
        os.unlink(probe)
        return True
    except OSError:
        return False


def host_folder() -> str | None:
    """The folder the host's conversation works in, when the host says so without the model: Codex sends its thread
    id in _meta (threadId, or x-codex-turn-metadata.thread_id), and the thread's session file names its cwd (the first
    line, type session_meta, payload.id equal to the thread id). None otherwise; the folder still passes check_folder."""
    meta = call_meta()
    turn = meta.get("x-codex-turn-metadata") if isinstance(meta.get("x-codex-turn-metadata"), dict) else {}
    thread = meta.get("threadId") or turn.get("thread_id")
    if not isinstance(thread, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", thread):
        return None
    sessions = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    try:
        found = sorted(sessions.glob(f"*/*/*/rollout-*-{thread}.jsonl"), reverse=True)
    except OSError:
        return None
    for path in found[:3]:
        try:
            with open(path, encoding="utf-8") as handle:
                first = json.loads(handle.readline(1 << 20))
        except (OSError, ValueError):
            continue
        payload = first.get("payload") if isinstance(first, dict) else None
        if first.get("type") == "session_meta" and isinstance(payload, dict) and payload.get("id") == thread \
                and isinstance(payload.get("cwd"), str) and payload["cwd"]:
            return payload["cwd"]
    return None


def _refuse(summary: str, next_step: str) -> Status:
    return Status("USAGE", summary, next_step, "Theodore could not use that file or folder; I am choosing another way.")


def folder_allowed(folder: Path) -> bool:
    """A resolved folder a deck may be built in: inside the home folder (not the home folder itself, not under a hidden
    folder, ~/Library or AppData), or on another volume (/Volumes/<name>/..., /mnt, /media, a drive other than the
    system's); never Theodore's own folders or its cache."""
    home, theodore = _real(Path.home()), _real(core.home().root)
    if _inside(theodore, folder) or _inside(folder, theodore) or _inside(folder, cache_root()):
        return False
    if _inside(folder, home):
        if folder == home:
            return False
        parts = folder.relative_to(home).parts
        if any(part.startswith(".") for part in parts):
            return False
        if parts[0] in (("Library",) if sys.platform == "darwin" else ("AppData",) if core.IS_WINDOWS else ()):
            return False
        return True
    if core.IS_WINDOWS:
        system_drive = str(os.environ.get("SystemDrive") or "C:").rstrip("\\").lower()
        drive = folder.drive.lower()
        return bool(drive) and drive != system_drive and len(folder.parts) >= 2
    for volume in ("/Volumes/", "/mnt/", "/media/"):
        if str(folder).startswith(volume) and len(folder.parts) >= 4:      # /Volumes/<disk>/<folder>
            return True
    return False


def check_folder(raw) -> Path:
    """The deck's folder: the chat's (an absolute path on this computer) or the default, created when it is the
    default. A default that cannot be written gives way to ~/Theodore (said once to the user, NOTICE); a named folder
    that cannot be written is refused with a status that names it. Refused: not absolute, not on this computer, the root, the home folder, a hidden or system folder, or a
    folder that holds Theodore's own (or lies inside it)."""
    if raw in (None, ""):
        hinted = host_folder()          # used only when it is a folder a deck may be built in; else the default
        with contextlib.suppress(OSError, ValueError):
            if hinted and Path(hinted).is_absolute() and _is_dir(hinted) and folder_allowed(_real(Path(hinted))):
                raw = hinted
    if raw in (None, ""):
        folder = default_folder()
        if _real(folder) not in FOLDERS and not _writable_folder(folder):
            fallback = Path.home().joinpath(*HOME_FOLDER)
            if fallback == folder or not _writable_folder(fallback):
                raise Status("SETUP_FAILED", f"Theodore cannot write its default folder {folder} or {fallback} on this computer.",
                             "ask the user for a folder on this computer that Theodore may write, and pass its full path as folder.",
                             "Theodore could not save in its usual folder on this computer. Which folder should I save your deck in?")
            NOTICE.append(f"Theodore could not save in {folder} on this computer (the system did not allow it), so this deck "
                          f"is going to {fallback} instead.")
            folder = fallback
    else:
        if not isinstance(raw, str):
            raise core.usage("folder is a path, as text.")
        folder = Path(raw).expanduser()
        if not folder.is_absolute():
            raise _refuse(f"the folder {raw} is not an absolute path on this computer.",
                          f"give the folder's full path on this computer (for example /Users/<name>/Documents/<folder>), or leave folder out to use {default_label()}.")
        if not folder.is_dir():
            raise _refuse(f"there is no folder {raw} on this computer (a chat's own files can live elsewhere, in its sandbox).",
                          f"ask the user for the folder's path on this computer, or leave folder out to use {default_label()}.")
        if _real(folder) not in FOLDERS and folder_allowed(_real(folder)) and not _writable_folder(folder):
            raise Status("USAGE", f"the folder {raw} cannot be written by Theodore on this computer (the system does not allow "
                                  f"it, or it is read-only).",
                         f"ask the user for another folder, or leave folder out to use {default_label()}.",
                         f"Theodore cannot save in {raw}. Should I use another folder?")
    folder = _real(folder)
    if not folder_allowed(folder):
        raise _refuse(f"Theodore does not build decks in {folder}: it is the home folder, a system or hidden folder, or Theodore's own.",
                      f"use a folder of the user's documents (or leave folder out: {default_label()}).")
    if folder not in FOLDERS:
        FOLDERS.append(folder)
    return folder


def _roots(folder: Path, *, write: bool) -> list[Path]:
    roots = [folder, *[f for f in FOLDERS if f != folder]]
    return roots if write else roots + [cache_root()]


def _engine_docs() -> Path | None:
    version = install.selected_engine()
    return _real(install.engine_dir(version) / "skills" / "pptx") if version else None


def resolve(raw, folder: Path, *, write: bool = False, docs: bool = False) -> Path:
    """A path a chat named: relative to the folder, resolved, and inside the folder (or the folders started in this
    session, or, to read, the render cache and, with `docs`, the engine's manual)."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise core.usage("path is a file or folder path, as text.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = folder / path
    real = _real(path)
    roots = _roots(folder, write=write)
    if docs and not write and _engine_docs():
        roots.append(_engine_docs())
    if not any(_inside(real, root) for root in roots):
        raise _refuse(f"{raw} is outside the deck's folder ({folder}); Theodore opens only files in that folder, its renders and its own manual.",
                      "use paths inside the deck's folder; to work in another folder, pass it as folder.")
    return real


def _looks_like_path(value: str) -> bool:
    return (value.startswith(("/", "~", "./", "../", "\\")) or "/" in value or "\\" in value or value in (".", "..")
            or bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.lower().endswith(PATH_SUFFIXES))


def check_args(args, folder: Path) -> list[str]:
    """The engine arguments, checked: a verb first, plain strings, no refused option, and every path inside the
    folder (made absolute, so the engine and this client read the same file)."""
    if not isinstance(args, list) or not args or not all(isinstance(a, str) and "\x00" not in a for a in args):
        raise core.usage('args is a list of strings: the engine command without "pptx", for example ["apply", "<workspace>"].')
    rest = [a for a in args if a not in ("--quiet", "-q")]
    if not rest or rest[0] not in VERBS:
        raise _refuse(f"{args[0]!r} is not one of Theodore's engine verbs; only these run here: {', '.join(VERBS)}.",
                      'pass the engine command as the build instructions write it, without "pptx" (for example ["apply", "<workspace>"]).')
    out: list[str] = []
    text_next = False
    for arg in args:
        if text_next:
            out.append(arg)
            text_next = False
            continue
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name in REFUSED_OPTIONS:
                raise _refuse(f"{name} drives another application; it does not run from a chat.", "leave that option out.")
            if eq:
                out.append(arg if name in TEXT_OPTIONS or not _looks_like_path(value) or value == "-"
                           else f"{name}={resolve(value, folder, write=True)}")
                continue
            text_next = name in TEXT_OPTIONS
            out.append(arg)
            continue
        if arg == "-" or arg.startswith("-") or not _looks_like_path(arg):
            out.append(arg)
            continue
        # A path: render may write its images to the cache, every other path is in the deck's folder.
        out.append(str(resolve(arg, folder, write=False)))
        if not any(_inside(Path(out[-1]), root) for root in _roots(folder, write=True)) and rest[0] != "render":
            raise _refuse(f"{arg} is not in the deck's folder ({folder}).", "use paths inside the deck's folder.")
    return out


# --- the restricted engine environment ----------------------------------------------------------------------------------

def _node_option(flag: str, value: str) -> str:
    """One NODE_OPTIONS entry, its value in double quotes (Node's own NODE_OPTIONS quoting)."""
    return f'{flag}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def font_dirs() -> list[Path]:
    """The font folders the engine's text measurement looks in first (pptxgenjs_helpers/text.js), those that exist:
    readable, so a font is found without fc-match."""
    home = Path.home()
    if sys.platform == "darwin":
        dirs = [Path(f"/Applications/Microsoft {app}.app/Contents/Resources/DFonts") for app in ("Word", "Excel", "PowerPoint")]
        dirs += [home / "Library" / "Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts")]
    elif core.IS_WINDOWS:
        dirs = [Path(os.environ.get("WINDIR") or "C:\\Windows") / "Fonts", home / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"]
    else:
        dirs = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".fonts", home / ".local" / "share" / "fonts"]
    return [_real(d) for d in dirs if d.is_dir()]


def node_options(reads, writes) -> str:
    """NODE_OPTIONS for a chat's engine processes: the permission model with these folders, and the guard first."""
    guard = Path(__file__).resolve().parent / "node_guard.js"
    # The two flags below print a security warning in every process, which the engine would report as a finding of
    # the deck; the guard is what makes them safe here (addons from the runtime only; Node on the engine's own scripts).
    options = ["--permission", "--allow-addons", "--allow-child-process", "--disable-warning=SecurityWarning"]
    # The guard's own file is readable: Node started with inline code (`node -e`, as the engine resolves an icon named
    # by words) loads a --require file only when it may read it; with a script file as main it loads it either way.
    for path in dict.fromkeys(str(p) for p in [*reads, guard]):
        options.append(_node_option("--allow-fs-read", path))
    for path in dict.fromkeys(str(p) for p in writes):
        options.append(_node_option("--allow-fs-write", path))
    options.append(_node_option("--require", str(guard)))
    return " ".join(options)


def drop_licence_copy() -> None:
    """The licence copy exists only while one engine command runs for a chat: it is made from the main licence just
    before (after account.require accepted that licence, as the launcher's `pptx` does) and removed just after. A
    licence signed out, refused or revoked in the main state therefore stops the chat route at the next command, and no
    copy outlives it."""
    with contextlib.suppress(OSError):
        (sandbox_home() / "state" / "entitlement.json").unlink()


def _prepare_sandbox() -> Path:
    """The engine's THEODORE_HOME for a chat: state/entitlement.json, a copy of the licence, and state/engine-clock.json,
    never behind the main one."""
    with contextlib.suppress(Exception):
        default_folder()            # the default is recorded before this folder exists (it marks an earlier chat route)
    base = sandbox_home()
    state = base / "state"
    (base / "tmp").mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(base, 0o700)
    main = core.home().state
    source = main / "entitlement.json"
    drop_licence_copy()
    if source.is_file():
        shutil.copyfile(source, state / "entitlement.json")
        with contextlib.suppress(OSError):
            os.chmod(state / "entitlement.json", 0o600)
    marks = [core.read_json(p).get("high_water") for p in (main / "engine-clock.json", state / "engine-clock.json")]
    marks = [int(m) for m in marks if isinstance(m, (int, float))]
    if marks:
        core.write_json(state / "engine-clock.json", {"high_water": max(marks)})
    return base


def restricted_env(directory: Path, folder: Path) -> dict:
    """engine_env for a chat: the private THEODORE_HOME and TMPDIR, and NODE_OPTIONS with the permission model and the
    guard (any NODE_OPTIONS inherited is dropped)."""
    env = install.engine_env(directory)
    base = _prepare_sandbox()
    tmp = base / "tmp"
    env["THEODORE_HOME"] = str(base)
    for name in ("TMPDIR", "TMP", "TEMP"):
        env[name] = str(tmp)
    env.pop("THEODORE_ENTITLEMENT", None)
    runtime = _real(directory / "node_modules").parent if (directory / "node_modules").exists() else None
    home = core.home()
    reads = [_real(directory), _real(home.packs), base, folder, cache_root()]
    if runtime:
        reads.append(runtime)
    writes = [base, folder, cache_root()]
    cache_root().mkdir(parents=True, exist_ok=True)
    reads += font_dirs()
    env["NODE_OPTIONS"] = node_options(reads, writes)
    env["THEODORE_NODE_ADDON_ROOTS"] = str(runtime / "node_modules") if runtime else ""
    env["THEODORE_NODE_SCRIPT_ROOT"] = str(_real(directory / "skills"))
    return env


# --- run ---------------------------------------------------------------------------------------------------------------

CHAT_BUILD_FEATURE = "chat-build"       # the plan feature of this route, in the licence's feat claim
FEATURE_RECHECK_S = 600.0
_FEATURE_ASKED = -FEATURE_RECHECK_S


def allowed() -> dict:
    """The licence's claims, when its plan includes building from a chat (the plan feature CHAT_BUILD_FEATURE). An
    admin takes the feature off a plan; the client refuses the build actions from the next licence refresh. The
    service's kill switches apply as to every engine command."""
    global _FEATURE_ASKED
    claims = account.require("pptx", allow_refresh=False)
    account.check_in_background()       # as the launcher's pptx: a revoked or suspended device stops within the hour
    if CHAT_BUILD_FEATURE not in (claims.get("feat") or []) and time.monotonic() - _FEATURE_ASKED > FEATURE_RECHECK_S:
        # A plan given the feature since the last refresh: ask the service once (at most every 10 minutes).
        _FEATURE_ASKED = time.monotonic()
        with contextlib.suppress(Exception):
            account.refresh(timeout=8.0)
            claims = account.require("pptx", allow_refresh=False)
    account.check_in_background()       # as the launcher's pptx: a revoked or suspended device stops within the hour
    if CHAT_BUILD_FEATURE not in (claims.get("feat") or []):
        raise Status("NO_ENTITLEMENT", "this Theodore plan does not include building decks from a chat on this computer.",
                     "do not retry. Tell the user in one sentence, then stop; build the deck with no other tool.",
                     "Your Theodore plan does not include building decks here.")
    return claims


_IMAGE_SUFFIX = re.compile(r"\.(?:jpg|jpeg|png)$", re.IGNORECASE)
_JSON_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _image_candidates(text: str) -> list[str]:
    """Paths the output names, spaces and accents included: every JSON string (rendered_images, png_path, ...) and
    every whole line (a path printed alone) that ends in .jpg, .jpeg or .png."""
    out: list[str] = []
    for match in _JSON_STRING.finditer(text):
        with contextlib.suppress(ValueError):
            value = json.loads(f'"{match.group(1)}"')
            if isinstance(value, str) and _IMAGE_SUFFIX.search(value):
                out.append(value)
    for line in text.splitlines():
        line = line.strip()
        if ":" in line and not line.startswith(("/", "\\")) and not re.match(r"^[A-Za-z]:[\\/]", line):
            line = line.split(":", 1)[1].strip()        # "RENDERS: ..." or "png: ..." style
        if _IMAGE_SUFFIX.search(line) and not line.startswith('"'):
            out.append(line)
    return out


def _images_in(text: str, folder: Path, limit: int) -> list[Path]:
    """Images the output names (render previews), in the order named, inside the cache or the folder, at most
    `limit`."""
    found: list[Path] = []
    roots = _roots(folder, write=False)
    for raw in _image_candidates(text):
        if not Path(raw).is_absolute():
            continue
        path = _real(Path(raw))
        if path not in found and _is_file(path) and any(_inside(path, r) for r in roots):
            found.append(path)
    return found[:limit]


def image_block(path: Path) -> dict | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > IMAGE_BYTES_MAX:
        return None
    return {"type": "image", "data": base64.b64encode(raw).decode("ascii"),
            "mimeType": IMAGE_TYPES.get(path.suffix.lower(), "image/jpeg")}


def _clip(text: str) -> str:
    if len(text) <= OUTPUT_CHARS_MAX:
        return text
    half = OUTPUT_CHARS_MAX // 2
    return text[:half] + f"\n[... {len(text) - OUTPUT_CHARS_MAX} characters not shown ...]\n" + text[-half:]


def run(args_in, stdin_text, folder_in, images_wanted=True) -> dict:
    """One engine command, confined. Returns {"content": [...]} with the output and its images. Nothing of the engine
    is removed while it runs (withdraw.py, lane PROTECT2)."""
    from . import withdraw
    with withdraw.engine_run():
        return _run(args_in, stdin_text, folder_in, images_wanted)


def _run(args_in, stdin_text, folder_in, images_wanted=True) -> dict:
    if folder_in in (None, ""):
        folder_in = _folder_of(args_in)
        if folder_in is None and isinstance(args_in, list) and args_in[:1] and args_in[0] in ("assets", "doctor", "cache") and FOLDERS:
            folder_in = str(FOLDERS[-1])         # a command about Theodore itself: the folder in use, no new one
    folder = check_folder(folder_in)
    args = check_args(args_in, folder)
    if stdin_text is not None and not isinstance(stdin_text, str):
        raise core.usage("stdin is text (the design note, for example).")
    allowed()
    repeat = _repeated_start(args, stdin_text, folder)
    if repeat is not None:
        return repeat
    asked_new = "--new" in args
    if args[0] == "start" and not asked_new:
        # Never the engine's reopening of an earlier deck (start --from a deck it delivered reopens that workspace, and
        # the next apply would replace the delivered file): from a chat, every new start is a deck of its own.
        args.insert(1, "--new")
    _check_open(args)
    install.require_runtime()
    install.wait_for_packs(args)
    install.wait_for_renderer(args)
    install.enforce_restrictions()
    version = install.engine_for(args)
    install.enforce_restrictions(version)
    directory = install.engine_dir(version)
    install.touch_used(version)
    env = restricted_env(directory, folder)
    note = None
    if stdin_text is not None and "--storyline" in args and args[args.index("--storyline") + 1:][:1] == ["-"]:
        # The note on standard input, for every engine (one before rc.11 reads it only from a file): a private copy the
        # engine copies into the workspace, removed after the command.
        note = sandbox_home() / "tmp" / f"note-{os.getpid()}-{int(time.time() * 1000)}.md"
        note.write_text(stdin_text, encoding="utf-8")
        args[args.index("--storyline") + 1] = str(note)
        stdin_text = None
    started = time.monotonic()
    # In the service a warm engine worker confined to this chat's folder runs it (ENGINE_RUNNER, service/chat.py; the
    # licence copy then lives as long as that worker); elsewhere, or for an engine without one, a process of its own.
    warm = ENGINE_RUNNER(version, directory, args, folder, env, stdin_text) if ENGINE_RUNNER is not None else None
    if warm is not None:
        code, output = warm
    else:
        code, output = _engine_process(directory, args, folder, env, stdin_text, started)
    if note is not None:
        with contextlib.suppress(OSError):
            note.unlink()
    install._pin_workspaces(args, version)
    if code in (1, install.LICENCE_EXIT):        # a licence refusal is the engine's last JSON object: no second start to ask
        refusal = install.refusal_in_output(output, code)
        if refusal is not None:
            raise refusal
    if args[0] == "finish":
        usage.send_in_background(force=True)
    if args[0] == "start":
        _remember(output)
        opened = _field(output, "WORKSPACE") or next((a for a in reversed(args[1:]) if not a.startswith("-") and _is_dir(a)), None)
        if code == 0 and opened:
            with contextlib.suppress(OSError, ValueError):
                OPEN.add(str(_real(Path(opened))))
        if code == 0 and not asked_new:      # recorded before anything else can fail: a retried start continues it
            workspace = _field(output, "WORKSPACE") or next((a for a in reversed(args[1:]) if not a.startswith("-") and _is_dir(a)), None)
            STARTS[_start_key(args, None, folder)] = (time.monotonic(), workspace,
                                                      {"content": [{"type": "text", "text": output}]})
    core.log(f"chat run {args[0]}: exit {code} in {time.monotonic() - started:.1f} s")
    seconds = time.monotonic() - started
    head = f"pptx {' '.join(args_in)}  ->  exit {code} in {seconds:.1f} s (folder {folder})\n"
    try:
        following = next_step(args, output, code)
    except Exception as exc:  # noqa: BLE001  the engine ran: its answer is never lost to a client mistake after it
        core.log(f"chat run {args[0]}: next step not computed ({exc.__class__.__name__})")
        following = ""
    content: list[dict] = [{"type": "text", "text": head + _clip(output) + (f"\nNEXT: {following}\n" if following else "")}]
    if images_wanted:
        total = 0
        for path in _images_in(output, folder, IMAGES_MAX):
            block = image_block(path)
            if block is None or total + len(block["data"]) * 3 // 4 > IMAGES_BYTES_MAX:
                continue
            total += len(block["data"]) * 3 // 4
            content += [{"type": "text", "text": path.as_posix()}, block]
    result = {"content": content}
    if code != 0:
        result["isError"] = True
    if code == 0 and args[0] == "finish":       # a finished deck: no later start continues it, no command changes it
        done = set(_workspace_args(args))
        OPEN.difference_update(done)
        for held_key in [k for k, v in STARTS.items() if v[1] and str(_real(Path(v[1]))) in done]:
            STARTS.pop(held_key, None)
    key = _start_key(args, None, folder) if args[0] == "start" else None
    if code == 0 and key in STARTS:                 # the whole answer, for a retried start
        STARTS[key] = (*STARTS[key][:2], result)
    return result


# Set by the service (service/chat.py): runs one engine command for a chat on a warm worker confined as below, and
# returns (exit code, output), or None when it cannot (an engine without a worker): then a process runs it here.
ENGINE_RUNNER = None


def _engine_process(directory: Path, args: list[str], folder: Path, env: dict, stdin_text, started: float) -> tuple[int, str]:
    """One engine process for a chat's command, confined by `env`; the licence copy goes as soon as it ends."""
    command = [str(install.engine_python(directory)), str(directory / "skills" / "pptx" / "scripts" / "pptx.py"), *args]
    try:
        proc = subprocess.Popen(command, cwd=str(folder), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    except OSError as exc:
        drop_licence_copy()
        raise core.internal(f"the engine could not be started: {exc}") from None
    import threading
    chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()), daemon=True)
    reader.start()
    with contextlib.suppress(BrokenPipeError, OSError):
        if stdin_text:
            proc.stdin.write(stdin_text.encode("utf-8"))
        proc.stdin.close()
    while True:
        try:
            code = proc.wait(timeout=PROGRESS_EVERY_S)
            break
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            progress = call_progress()
            if progress:
                with contextlib.suppress(Exception):
                    progress(elapsed, f"pptx {args[0]} running ({int(elapsed)} s)")
            if elapsed > RUN_TIMEOUT_S:
                proc.kill()
                code = proc.wait()
                break
    reader.join(timeout=10)
    drop_licence_copy()
    return code, b"".join(chunks).decode("utf-8", errors="replace")


# A start that succeeded, by its folder and deck name (and the conversation, when the host names it): a retried start of
# a deck not finished yet continues it instead of making a second one with a "-2" name. A finished deck (finish ran:
# result.json) is never continued: a new start gets a deck of its own. `--new` always starts another.
STARTS: dict[str, tuple[float, str | None, dict]] = {}
REPEAT_WINDOW_S = 3600.0


def _start_key(args, stdin_text, folder: Path) -> str:
    """The deck a start names: its folder and its --name (else its --from, else its --template); not the brief or the
    note, which a second attempt may word differently."""
    def value(option: str) -> str:
        for i, arg in enumerate(args):
            if arg == option and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith(option + "="):
                return arg.split("=", 1)[1]
        return ""
    del stdin_text
    name = value("--name").strip().lower() or value("--from") or value("--template")
    meta = call_meta()
    turn = meta.get("x-codex-turn-metadata") if isinstance(meta.get("x-codex-turn-metadata"), dict) else {}
    thread = meta.get("threadId") or turn.get("thread_id") or ""          # the conversation, when the host names it
    return json.dumps([str(folder), name, "--translate" in args, "--extract" in args, str(thread)], ensure_ascii=False)


def _repeated_start(args: list[str], stdin_text, folder: Path) -> dict | None:
    if not args or args[0] != "start" or "--new" in args:
        return None
    held = STARTS.get(_start_key(args, stdin_text, folder))
    if not held or time.monotonic() - held[0] > REPEAT_WINDOW_S or not held[1] or not _is_dir(held[1]) \
            or _is_file(Path(held[1]) / "result.json"):     # a finished deck is never continued by a new start
        STARTS.pop(_start_key(args, stdin_text, folder), None)
        return None
    core.log("chat run start: the same start again; the deck it started is continued")
    first = held[2]["content"][0]["text"]
    text = ("This deck was already started from this folder under this name; it is continued, not started twice. To "
            "change its note, edit design.md in the workspace. To make a separate deck, add --new. The first start "
            "answered:\n" + first)
    return {"content": [{"type": "text", "text": text}]}


def _workspace_args(args: list[str]) -> list[str]:
    return [str(_real(Path(a))) for a in args[1:] if not a.startswith("-") and _is_dir(a) and _is_dir(Path(a) / ".pptx-state")]


def _check_open(args: list[str]) -> None:
    """A command that changes a deck runs only on a workspace this server started (or continued) and that is not
    finished. A finished deck, or one another session started, is never changed, so its delivered file is never
    replaced: the chat starts a new deck from it (start --from <its file>), which gets a name of its own."""
    if args[0] not in CHANGING or (args[0] == "slide" and args[1:2] and args[1] in READING_SLIDE_OPS):
        return
    for workspace in _workspace_args(args):
        if workspace in OPEN and not _is_file(Path(workspace) / "result.json"):
            continue
        delivered = core.read_json(Path(workspace) / "result.json").get("output") if _is_file(Path(workspace) / "result.json") else None
        raise Status("USAGE", f"this deck is {'finished' if delivered else 'not one this conversation started'}; Theodore does not "
                              f"change it from here, so {'its file ' + str(delivered) if delivered else 'its file'} stays as it is.",
                     f"to change it, start a new deck from its file: {_run_call('start', '--from', delivered or '<its .pptx>', '--brief', '<what changes>')}; "
                     "the new deck gets a name of its own.",
                     "That deck is already delivered, so I am making the changes in a new copy beside it.")


def _field(output: str, name: str) -> str | None:
    match = re.search(rf"^{name}: (.+)$", output, re.M)
    return match.group(1).strip() if match else None


def _run_call(*args: str) -> str:
    return f'{TOOL} with action "run", args ' + json.dumps(list(args), ensure_ascii=False)


def next_step(args: list[str], output: str, code: int) -> str:
    """What the chat does after this command, said in the answer itself: a chat is steered only by what the tool
    returns, so every step names the next one, through to the delivered file."""
    verb = args[0] if args else ""
    workspace = next((a for a in args[1:] if not a.startswith("-") and _is_dir(a) and _is_dir(Path(a) / ".pptx-state")), None)
    if verb == "start" and code == 0:
        workspace = _field(output, "WORKSPACE") or workspace
        return (f'write each slide\'s block in {workspace}/deck.js as the design note and the build instructions say (action "read" '
                f'it first, then "edit" or "write"; read the build API where the manual points to it), then '
                f'{_run_call("apply", workspace or "<workspace>")}. The deck is built here, on this computer, never with another slide tool.')
    if verb == "apply":
        if code == 0:
            return (f"look at every slide image above against the design note and fix what is plainer, misplaced or cut off in "
                    f'deck.js (action "edit"), then apply again. When every slide holds, review the deck as the instructions say, '
                    f"then {_run_call('finish', workspace or '<workspace>')}.")
        return 'fix what the output names in deck.js (action "edit"), then run the same apply again.'
    if verb == "finish":
        if code == 0:
            delivered = None
            with contextlib.suppress(ValueError, AttributeError):
                delivered = json.loads(output[output.index("{"):]).get("delivered", {}).get("output")
            # Not for a person who already has the preference: the tool cannot know, the assistant does (it was named).
            tip = delivery_tip()
            tip = f" If the user had to name Theodore in their request to get this deck, add: {tip}" if tip else ""
            return (f"tell the user, in one line: Your deck is saved on this computer at {delivered or 'the OUTPUT path above'}."
                    f"{tip} "
                    f'Show it in Finder or Explorer (action "reveal") only if the user asks.')
        return "fix what finish names (in deck.js or the note), apply again, then finish again."
    if verb == "render" and code == 0:
        return "look at the images; fix in deck.js, then apply again."
    return ""


def _folder_of(paths) -> str | None:
    """No folder named: the folder of this session a path in the call lies in (a workspace's later commands name only
    the workspace), else None (the default folder)."""
    for raw in paths if isinstance(paths, list) else [paths]:
        if isinstance(raw, str) and raw and Path(raw).expanduser().is_absolute():
            real = _real(Path(raw).expanduser())
            for folder in sorted(FOLDERS, key=lambda f: len(str(f)), reverse=True):
                if _inside(real, folder):
                    return str(folder)
    return None


def _remember(output: str) -> None:
    """A started workspace's folder is a folder of this session (its later commands name the workspace path only)."""
    for line in output.splitlines():
        if line.startswith("WORKSPACE:"):
            with contextlib.suppress(OSError, ValueError):
                workspace = _real(Path(line.split(":", 1)[1].strip()))
                folder = workspace.parent.parent.parent if workspace.parent.name == "workspaces" else workspace
                if folder not in FOLDERS:
                    FOLDERS.append(folder)


# --- file actions --------------------------------------------------------------------------------------------------------

def read(path_in, folder_in, offset=None, limit=None) -> dict:
    folder = check_folder(folder_in or _folder_of(path_in))
    path = resolve(path_in, folder, docs=True)
    if path.is_dir():
        return list_dir(str(path), str(folder))
    if not path.is_file():
        raise _refuse(f"there is no file {path}.", "check the path (action list shows a folder).")
    suffix = path.suffix.lower()
    if suffix in IMAGE_TYPES:
        block = image_block(path)
        if block is None:
            raise _refuse(f"{path.name} is larger than {IMAGE_BYTES_MAX >> 20} MiB.", "render fewer slides or a smaller image.")
        return {"content": [{"type": "text", "text": path.as_posix()}, block]}
    if suffix not in TEXT_READABLE:
        raise _refuse(f"{path.name} is not a text file; Theodore reads text files and images here.",
                      'to read a deck, run ["start", "--from", "<deck.pptx>", "--brief", "read"] and read its deck.js, or ["slide", "show", ...].')
    text = path.read_text(encoding="utf-8", errors="replace")
    docs = _engine_docs()
    if docs is not None and path == docs / "SKILL.md":     # the manual read as a file: composed as `manual` gives it
        from . import commands
        text = commands.compose_manual(text, None)
    lines = text.splitlines(keepends=True)
    start = max(0, int(offset or 1) - 1) if isinstance(offset, int) else 0
    end = start + int(limit) if isinstance(limit, int) and limit > 0 else len(lines)
    body = "".join(lines[start:end])
    if len(body) > READ_CHARS_MAX:
        body = body[:READ_CHARS_MAX] + f"\n[... cut at {READ_CHARS_MAX} characters; read on with offset ...]\n"
    return {"content": [{"type": "text", "text": f"{path.as_posix()} (lines {start + 1}-{min(end, len(lines))} of {len(lines)})\n{body}"}]}


def _writable(path_in, folder: Path) -> Path:
    path = resolve(path_in, folder, write=True)
    if path.suffix.lower() not in WRITABLE:
        raise _refuse(f"Theodore writes only {', '.join(WRITABLE)} files from a chat; {path.name} is not one.",
                      "write the deck's script, its note and its parts; the engine writes the .pptx.")
    if path.exists() and not path.is_file():
        raise _refuse(f"{path} is not a file.", "name a file.")
    return path


def write(path_in, content, folder_in) -> dict:
    folder = check_folder(folder_in or _folder_of(path_in))
    if not isinstance(content, str):
        raise core.usage("content is the file's whole text.")
    if len(content.encode("utf-8")) > WRITE_BYTES_MAX:
        raise core.usage(f"content is larger than {WRITE_BYTES_MAX >> 20} MiB.")
    path = _writable(path_in, folder)
    allowed()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.theodore-tmp")
    temp.write_text(content, encoding="utf-8", newline="")
    os.replace(temp, path)
    return {"content": [{"type": "text", "text": f"wrote {path.as_posix()} ({len(content.splitlines())} lines)"}]}


def edit(path_in, old, new, folder_in, replace_all=False) -> dict:
    folder = check_folder(folder_in or _folder_of(path_in))
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise core.usage("edit needs old (text that is in the file) and new (its replacement).")
    path = _writable(path_in, folder)
    if not path.is_file():
        raise _refuse(f"there is no file {path}.", 'create it with action "write".')
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise _refuse(f"old was not found in {path.name}.", 'read the file again (action "read") and copy the text exactly.')
    if count > 1 and not replace_all:
        raise _refuse(f"old occurs {count} times in {path.name}.", "give more surrounding text so it is unique, or set all to true.")
    return write(str(path), text.replace(old, new) if replace_all else text.replace(old, new, 1), str(folder))


def list_dir(path_in, folder_in) -> dict:
    folder = check_folder(folder_in or _folder_of(path_in))
    path = resolve(path_in or str(folder), folder, docs=True)
    if not path.is_dir():
        raise _refuse(f"{path} is not a folder.", "name a folder.")
    rows = []
    for child in sorted(path.iterdir(), key=lambda p: p.name.lower())[:500]:
        with contextlib.suppress(OSError):
            rows.append(f"{child.name}/" if child.is_dir() else f"{child.name}  {child.stat().st_size} bytes")
    return {"content": [{"type": "text", "text": f"{path.as_posix()}\n" + "\n".join(rows) + "\n"}]}


def reveal(path_in, folder_in) -> dict:
    """Shows the file in Finder (Explorer on Windows); only when the user asks."""
    folder = check_folder(folder_in or _folder_of(path_in))
    path = resolve(path_in, folder, write=True)
    if not path.exists():
        raise _refuse(f"there is no file {path}.", "check the path.")
    if sys.platform == "darwin":
        command = ["/usr/bin/open", "-R", str(path)]
    elif core.IS_WINDOWS:
        command = [str(Path(os.environ.get("SystemRoot") or "C:\\Windows") / "explorer.exe"), f"/select,{path}"]
    else:
        command = ["xdg-open", str(path.parent)]
    with contextlib.suppress(OSError):
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"content": [{"type": "text", "text": f"shown: {path.as_posix()}"}]}


# --- the build instructions for a chat ----------------------------------------------------------------------------------

FILE_ACTIONS = ("run", "read", "write", "edit", "list", "reveal")
# Appended to begin's answer: where "the build skill" is in a chat.
BUILD_POINTER = ('How the deck is built here: the build skill this guidance names is Theodore\'s engine on this computer, run '
                 'through this same tool, and the deck is delivered as a PowerPoint file in the user\'s folder. Never build '
                 'it with another slide, document or artifact tool, and never with code of your own. Steps: read what this '
                 'guidance requires (action "guide"), open its sheets (action "specimen"), write the design note, then call '
                 f'{TOOL} with action "manual" once and follow it: start the deck with the note (action "run"), write deck.js, '
                 'apply, look at the slide images it returns, fix, finish. Pass folder: the full path on this computer of the '
                 'folder the user named or attached; with none, leave folder out (Theodore\'s default folder).')
# Ends every guidance section and specimen answer: the way on to the build.
THEN_BUILD = (f'Then, once the reading is done and the design note is written: {TOOL} with action "manual", and build with '
              'Theodore\'s engine as it says (never with another slide tool).')

def bindings(docs: Path | None) -> str:
    """The adapter: how the build instructions' commands and files are reached through the one tool."""
    where = docs.as_posix() if docs else "<pptx-skill-dir>"
    return "\n".join([
        f"Bindings (this chat): Theodore's engine runs on this computer through the `{TOOL}` tool, not through a shell.",
        f'  pptx <verb> <args...>  ->  {TOOL} with action "run", args ["<verb>", "<args>", ...] (one list item per argument,',
        '                             no quotes, no shell); text the command reads on standard input (the design note for',
        '                             --storyline -) goes in stdin; folder is the deck\'s folder on this computer (the folder',
        "                             the user named, its full path; leave it out for Theodore's default folder). Renders come back as images.",
        (f'  <pptx-skill-dir> = {where}   (read pptxgenjs.md and references/ there with action "read", path)'
         if docs is None or (docs / "pptxgenjs.md").is_file() else       # an engine from 1.0.0-rc.29: the manual is served
         f'  theodore guide pptx <name>  ->  {TOOL} with action "guide", section "<name>" (the build API is section "pptxgenjs")'),
        '  edit <wd>/deck.js, design.md or a part  ->  action "edit" (path, old, new) or "write" (path, content); "read" a file',
        '                             or an image path, "list" a folder. "reveal" shows a file in Finder, only when the user asks.',
        "  python3 ...  ->  not available here; every step the instructions give runs as an engine command.",
        "  When finish has run, tell the user the delivered file's full path (OUTPUT).",
    ])


def manual() -> dict:
    """The engine's build instructions (skills/pptx/SKILL.md, every part) with the chat's bindings in place of the
    launcher's."""
    from . import mcp
    text = mcp._pptx_manual()
    docs = _engine_docs()
    lines = text.split("\n")
    out, skipping = [], False
    for line in lines:
        if line.startswith("Bindings: pptx <verb>"):
            out.append(bindings(docs))
            skipping = True
            continue
        if skipping and (line.startswith("          ") and ("->" in line or "<pptx-skill-dir>" in line)):
            continue
        skipping = False
        out.append(line)
    text = "\n".join(out)
    if "Bindings (this chat)" not in text:
        text = bindings(docs) + "\n\n" + text
    text = text.rstrip("\n") + ("\n\nNEXT: " + _run_call("start", "--blank", "--name", "<deck name>", "--brief", "<one paragraph>",
                                                         "--storyline", "-") + ' with the design note as stdin and folder set '
                                 '(or --template "<template.pptx>" / --from "<deck.pptx>" in place of --blank, as the table above '
                                 'says). Its answer names the workspace and the next step.\n')
    return {"content": [{"type": "text", "text": text}]}
