"""Command line of the Theodore client. Every outcome is an engine exit code (0 to 63) or a three-line status."""

from __future__ import annotations

import argparse
import sys

from . import account, commands, core
from .core import Status

HELP = """Theodore client commands (run through the launcher):
  guide <pptx|deck-design> [section] [--part N] [--license]   print the skill instructions
  pptx <verb...>                 run the engine          python <args...>   run the engine's Python
  login [--start | --finish --wait S | --code XXXX-XXXX]    logout    whoami
  status [--json] [--wait S]     doctor [--json]         version
  update [--check | --apply | --rollback | --channel C | --pin V | --unpin | --reset-state]
  report --kind bug|feedback --title T --details D [--verb V --code C --workspace DIR] [--origin user_requested] [--dry-run]
  prefs add "<text>" | list | remove <id>
  packs install <pack>|--all [--background]    packs status     tools install libreoffice [--background]
  hosts register | unregister | settings [--apply]          activate --grant - | --grant-file F          uninstall
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
    g.add_argument("--part", type=int, default=1)
    g.add_argument("--license", action="store_true")
    lg = sub.add_parser("login", add_help=False)
    lg.add_argument("--start", action="store_true")
    lg.add_argument("--finish", action="store_true")
    lg.add_argument("--wait", type=float, default=0.0)
    lg.add_argument("--code")
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
    mode.add_argument("--client-job", nargs=3)
    rp = sub.add_parser("report", add_help=False)
    rp.add_argument("--kind", choices=("bug", "feedback"), required=True)
    rp.add_argument("--title", required=True)
    rp.add_argument("--details", required=True)
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
    tl.add_argument("--job", action="store_true")
    su = sub.add_parser("setup", add_help=False)
    su.add_argument("--job", action="store_true")
    su.add_argument("--wait", type=float, default=0.0)
    hs = sub.add_parser("hosts", add_help=False)
    hs.add_argument("action", choices=("register", "unregister", "settings"))
    hs.add_argument("--apply", action="store_true")
    ac = sub.add_parser("activate", add_help=False)
    grant = ac.add_mutually_exclusive_group(required=True)
    grant.add_argument("--grant", help="'-': read the grant from standard input")
    grant.add_argument("--grant-file")
    ip = sub.add_parser("install-plugin", add_help=False)       # the installer's step: not for assistants
    ip.add_argument("--dl-base", required=True)
    ip.add_argument("--keys", required=True)
    un = sub.add_parser("uninstall", add_help=False)
    un.add_argument("--keep-hosts", action="store_true")
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
        return commands.guide(args.skill, args.section, args.part, args.license)
    if cmd == "login":
        if args.code:
            return core.emit(account.login_code(args.code))
        if args.finish:
            return core.emit(account.login_finish(args.wait))
        return core.emit(account.login_start())
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
        action = "check" if args.check else "rollback" if args.rollback else "pin" if args.pin else "unpin" if args.unpin \
            else "reset" if args.reset_state \
            else "channel" if args.channel else "apply"
        return commands.update(action, args.pin or args.channel)
    if cmd == "report":
        return commands.report(args.kind, args.title, args.details, args.verb, args.code, args.workspace, args.origin, args.dry_run)
    if cmd == "prefs":
        return commands.prefs(args.action, args.value)
    if cmd == "packs":
        return commands.packs(args.action, args.name, args.all, args.background, args.job)
    if cmd == "tools":
        return commands.tools(args.name, args.background, args.job)
    if cmd == "setup":
        return commands.setup_job() if args.job else commands.setup(args.wait)
    if cmd == "hosts":
        return commands.hosts(args.action, args.apply)
    if cmd == "activate":
        return core.emit(account.activate_grant(account.read_grant(args.grant, args.grant_file)))
    if cmd == "uninstall":
        return commands.uninstall(args.keep_hosts)
    if cmd == "install-plugin":
        return commands.install_plugin(args.dl_base, args.keys)
    raise core.usage(f"unknown command '{argv[0]}'.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return _dispatch(argv)
    except Status as status:
        return core.emit(status)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # never show a traceback to the assistant: it would try to "fix" it
        core.log(f"internal error in {argv[:1]}: {exc.__class__.__name__}: {exc}")
        return core.emit(core.internal(f"{exc.__class__.__name__}: {exc}"))
