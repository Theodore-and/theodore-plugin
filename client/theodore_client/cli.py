"""Command line of the Theodore client. Every outcome is an engine exit code (0 to 63) or a three-line status."""

from __future__ import annotations

import argparse
import contextlib
import sys

from . import account, api, commands, core, specimens, usage, withdraw
from .core import Status

HELP = """Theodore client commands (run through the launcher):
  guide <pptx|deck-design> [section] [--part N] [--license] [--role lead|slide]   print the skill instructions
  specimen <file.jpg|CODE> [...] fetch deck-design specimen sheets (at most 8) and print where they are
  skill <pptx|deck-design>       the skill's instructions as a skill file's body (always exits 0)
  mcp                            serve the guide and specimen tools to the assistant (MCP over standard input and output)
  pptx <verb...>                 run the engine          python <args...>   run the engine's Python
  login [--start | --open | --finish --wait S | --code XXXX-XXXX]    logout    whoami
                                 (--open: also open the sign-in page in the browser; nothing else ever opens one)
  status [--json] [--wait S]     doctor [--json]         version
  update [--check | --apply | --rollback | --channel C | --pin V | --unpin | --reset-state]
  report --kind bug|feedback --title T --details D|--details-file F [--verb V --code C --workspace DIR] [--origin user_requested] [--dry-run]
                                 (details of more than one line: put them in a UTF-8 file and pass --details-file; --details - reads standard input)
  prefs add "<text>" | list | remove <id>
  packs install <pack>|--all [--background]    packs status     tools install libreoffice|git [--background]
  hosts register | unregister | settings [--apply | --remove]          activate --grant - | --grant-file F          uninstall
  usage [--show]                 where the usage counts stand; --show prints the next batch exactly as it would be sent
  studio [on|off]                whether Studio is on for the account; on or off changes it (only when the user asks)
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would print its own text and exit 2
        raise core.usage(f"{self.prog}: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="theodore", add_help=False)
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)
    g = sub.add_parser("guide", add_help=False)
    g.add_argument("skill")
    g.add_argument("section", nargs="?")
    g.add_argument("--part", type=int, default=None)
    g.add_argument("--license", action="store_true")
    g.add_argument("--role")
    sp = sub.add_parser("specimen", add_help=False)
    sp.add_argument("files", nargs="*")
    lg = sub.add_parser("login", add_help=False)
    lg.add_argument("--start", action="store_true")
    lg.add_argument("--open", action="store_true")
    lg.add_argument("--finish", action="store_true")
    lg.add_argument("--wait", type=float, default=0.0)
    lg.add_argument("--code")
    lg.add_argument("--refused", help=argparse.SUPPRESS)      # the installers' saved refusal of their code (W-17)
    sub.add_parser("mcp", add_help=False)
    sk = sub.add_parser("skill", add_help=False)
    sk.add_argument("name")
    sub.add_parser("logout", add_help=False)
    sub.add_parser("whoami", add_help=False)
    st = sub.add_parser("status", add_help=False)
    st.add_argument("--json", action="store_true")
    st.add_argument("--wait", type=float, default=0.0)
    dr = sub.add_parser("doctor", add_help=False)
    dr.add_argument("--json", action="store_true")
    sub.add_parser("version", add_help=False)
    sub.add_parser("help", add_help=False)
    up = sub.add_parser("update", add_help=False)
    mode = up.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    mode.add_argument("--channel")
    mode.add_argument("--pin")
    mode.add_argument("--unpin", action="store_true")
    mode.add_argument("--reset-state", action="store_true")
    mode.add_argument("--engine-job", action="store_true")
    mode.add_argument("--plugin-job", action="store_true")
    mode.add_argument("--client-job", nargs="+")         # version sha256 runtime-id [issued]
    rp = sub.add_parser("report", add_help=False)
    rp.add_argument("--kind", choices=("bug", "feedback"), required=True)
    rp.add_argument("--title", required=True)
    details = rp.add_mutually_exclusive_group(required=True)
    details.add_argument("--details", help="'-': read the details from standard input")
    details.add_argument("--details-file")
    rp.add_argument("--verb")
    rp.add_argument("--code")
    rp.add_argument("--workspace")
    rp.add_argument("--origin", choices=("automatic", "user_requested"), default="automatic")
    rp.add_argument("--dry-run", action="store_true")
    pf = sub.add_parser("prefs", add_help=False)
    pf.add_argument("action", choices=("add", "list", "remove"))
    pf.add_argument("value", nargs="?")
    pk = sub.add_parser("packs", add_help=False)
    pk.add_argument("action", choices=("install", "status"))
    pk.add_argument("name", nargs="?")
    pk.add_argument("--all", action="store_true")
    pk.add_argument("--background", action="store_true")
    pk.add_argument("--job", action="store_true")
    tl = sub.add_parser("tools", add_help=False)
    tl.add_argument("action", choices=("install",))
    tl.add_argument("name")
    tl.add_argument("--background", action="store_true")
    tl.add_argument("--if-needed", action="store_true")
    tl.add_argument("--job", action="store_true")
    su = sub.add_parser("setup", add_help=False)
    su.add_argument("--job", action="store_true")
    su.add_argument("--wait", type=float, default=0.0)
    hs = sub.add_parser("hosts", add_help=False)
    hs.add_argument("action", choices=("register", "unregister", "settings"))
    hs.add_argument("--apply", action="store_true")
    hs.add_argument("--remove", action="store_true")
    ac = sub.add_parser("activate", add_help=False)
    grant = ac.add_mutually_exclusive_group(required=True)
    grant.add_argument("--grant", help="'-': read the grant from standard input")
    grant.add_argument("--grant-file")
    ip = sub.add_parser("install-plugin", add_help=False)       # the installer's step: not for assistants
    ip.add_argument("--dl-base", required=True)
    ip.add_argument("--keys", required=True)
    un = sub.add_parser("uninstall", add_help=False)
    un.add_argument("--keep-hosts", action="store_true")
    sub.add_parser("licence-check", add_help=False)        # the detached refresh an engine command starts: not for assistants
    us = sub.add_parser("usage", add_help=False)
    us.add_argument("--show", action="store_true")
    so = sub.add_parser("studio", add_help=False)
    so.add_argument("action", nargs="?", choices=("on", "off"))
    we = sub.add_parser("welcome", add_help=False)              # the installers' last status: not for assistants
    we.add_argument("--downloading", action="store_true")
    return parser


def _dispatch(argv: list[str]) -> int:
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(HELP, end="")
        return 0
    if argv[0] == "pptx":
        return commands.run_pptx(argv[1:])
    if argv[0] == "python":
        return commands.run_python(argv[1:])
    args = _parser().parse_args(argv)
    cmd = args.command
    if cmd == "guide":
        return commands.guide(args.skill, args.section, args.part, args.license, args.role)
    if cmd == "skill":
        return commands.skill(args.name)
    if cmd == "specimen":
        return commands.specimen(args.files)
    if cmd == "login":
        if args.refused:
            return core.emit(account.activation_refused(args.refused))
        if args.code:
            return core.emit(account.login_code(args.code))
        if args.finish:
            return core.emit(account.login_finish(args.wait))
        return core.emit(account.login_start(open_browser=args.open))      # only `login --open` opens a browser
    if cmd == "logout":
        return core.emit(account.logout())
    if cmd == "whoami":
        return core.emit(account.whoami())
    if cmd == "status":
        return commands.status(args.json, args.wait)
    if cmd == "doctor":
        return commands.doctor(args.json)
    if cmd == "version":
        return commands.version()
    if cmd == "update":
        if args.engine_job:
            return commands.engine_update_job()
        if args.client_job:
            return commands.client_update_job(*args.client_job)
        if args.plugin_job:
            return commands.plugin_update_job()
        action = "check" if args.check else "rollback" if args.rollback else "pin" if args.pin else "unpin" if args.unpin \
            else "reset" if args.reset_state \
            else "channel" if args.channel else "apply"
        return commands.update(action, args.pin or args.channel)
    if cmd == "report":
        return commands.report(args.kind, args.title, _details(args.details, args.details_file), args.verb, args.code, args.workspace,
                               args.origin, args.dry_run)
    if cmd == "prefs":
        return commands.prefs(args.action, args.value)
    if cmd == "packs":
        return commands.packs(args.action, args.name, args.all, args.background, args.job)
    if cmd == "tools":
        return commands.tools(args.name, args.background, args.job, args.if_needed)
    if cmd == "setup":
        return commands.setup_job() if args.job else commands.setup(args.wait)
    if cmd == "hosts":
        return commands.hosts(args.action, args.apply, args.remove)
    if cmd == "activate":
        return core.emit(account.activate_grant(account.read_grant(args.grant, args.grant_file)))
    if cmd == "uninstall":
        return commands.uninstall(args.keep_hosts)
    if cmd == "install-plugin":
        return commands.install_plugin(args.dl_base, args.keys)
    if cmd == "usage":
        return usage.show(args.show)
    if cmd == "licence-check":
        return account.licence_check_job()
    if cmd == "studio":
        from . import welcome
        return welcome.studio(args.action)
    if cmd == "welcome":
        from . import welcome
        return welcome.welcome(args.downloading)
    raise core.usage(f"unknown command '{argv[0]}'.")


def _details(details: str | None, path: str | None) -> str:
    """A report's details: the argument, standard input for "-", or a UTF-8 file read whole (text of several lines cannot
    pass as an argument through cmd, which ends a command line at its first line feed)."""
    try:
        if path is not None:
            with open(path, "rb") as handle:
                return handle.read().decode("utf-8-sig")
        if details == "-":
            if sys.stdin is None:
                raise core.usage("--details - reads standard input, and this command has none.")
            return sys.stdin.buffer.read().decode("utf-8-sig")
    except OSError as exc:
        raise core.usage(f"--details-file: the file could not be read ({exc.strerror or exc.__class__.__name__}).") from None
    except UnicodeDecodeError:
        raise core.usage(f"the details ({'--details-file' if path is not None else 'standard input'}) are not UTF-8 text.") from None
    return details or ""


# The text that follows the arguments on the Windows launcher's run line (theodore.cmd, :run_client). cmd ends a command
# line at its first line feed, so an argument of several lines leaves a quote open and the rest of that line arrives
# inside the last argument; the command is then not run (test_launcher ties this text to the launcher's line).
LAUNCHER_TAIL = "& if not errorlevel 0 (echo THEODORE BLOCKED_BY_POLICY"


def cut_command_line(argv: list[str]) -> Status | None:
    if not any(LAUNCHER_TAIL in arg for arg in argv):
        return None
    return core.usage("the command line was cut at its first line break: on Windows, cmd ends a command at a line feed, so an "
                      "argument of more than one line does not arrive whole. Nothing was run.",
                      "pass text of more than one line in a UTF-8 file (report: --details-file <path>) or keep each argument on "
                      "one line, then run the command again.")


def _utf8_streams() -> None:
    """Text goes out as UTF-8 whatever the system's code page: on Windows a pipe or a file otherwise gets the ANSI code
    page (cp1252), and a guidance character outside it would end the command with UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    cut = cut_command_line(argv)
    if cut is not None:
        return core.emit(cut)
    if argv[:1] == ["mcp"]:         # standard output carries only the protocol: statuses become tool answers
        from . import mcp
        blocked = None
        try:
            core.check_environment("mcp")
        except Status as status:
            blocked = status
        with contextlib.suppress(Exception):
            account.register_disk_secrets()
        specimens.sweep()
        withdraw.resume()
        rest = argv[1:]
        for_host = rest[rest.index("--for") + 1] if "--for" in rest[:-1] else None
        return mcp.serve(blocked=blocked, for_host=for_host)
    if argv[:1] == ["session-start"]:   # the plugin's hook: reads Theodore's session note only; silent, exit 0, no network unless resumed
        from . import sessions
        return sessions.session_start()
    if argv[:1] == ["skill"]:       # injected into a skill by the host: whatever happens, exit 0 with something to act on
        code = _main(argv)
        return 0 if code != 130 else code
    return _main(argv)


def _main(argv: list[str]) -> int:
    try:
        core.check_environment(argv[0] if argv else None)      # before anything touches the folder
    except Status as status:
        return core.emit(status)
    try:
        account.register_disk_secrets()     # before any server diagnostic can be printed
    except Exception:       # noqa: BLE001  a scrubber that knows less is no reason not to run
        pass
    specimens.sweep()       # specimen sheets older than an hour go before anything else is done
    withdraw.resume()       # Theodore's files, when the service ended this computer's access and they are still here
    if argv[:1] and argv[0] not in _NO_USAGE_FLUSH:
        usage.flush()           # only when the oldest waiting usage event is older than 15 minutes; never raises
    code = _run(argv)
    withdraw.resume()       # learned by this command: removed now that it is done (never while an engine runs)
    last = core.last_emitted()
    if last is not None and last.name != "OK" and code == last.code:
        usage.record_status(argv[0] if argv else None, last.name, last.cause)     # only while usage is on; never raises
    return code


# Commands that never send the usage counts at their start: the one that shows them, and those that end the sign-in or
# the installation (logout and uninstall remove the ledger).
_NO_USAGE_FLUSH = ("usage", "logout", "uninstall", "install-plugin", "licence-check", "help", "-h", "--help")


def _run(argv: list[str]) -> int:
    try:
        code = _dispatch(argv)
        if core.refused():          # learned on the way and not answered yet (a job, a swallowed refusal)
            return core.emit(account.refusal_status())
        return code
    except Status as status:
        return core.emit(status)
    except api.TooLarge as exc:
        return core.emit(core.internal(f"an answer from the Theodore service was longer than {exc.limit} bytes; it was refused and not read further."))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # never show a traceback to the assistant: it would try to "fix" it
        core.log(f"internal error in {argv[:1]}: {exc.__class__.__name__}: {exc}")
        return core.emit(core.internal(f"{exc.__class__.__name__}: {exc}"))
