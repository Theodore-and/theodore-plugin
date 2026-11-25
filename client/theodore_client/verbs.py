"""An engine command's path through the client (`theodore pptx <verb>`, `theodore python …`): what runs before and
after the engine, and nothing else. Imported without the command table (commands.py) and without the network or
archive modules (0.4.34: about 100 ms of the client's start on the Windows test machine went to modules no engine
command uses); each of those loads at its first use (core.LazyModule)."""

from __future__ import annotations

from . import account, install, sessions, usage


def run_pptx(args: list[str]) -> int:
    account.require("pptx", allow_refresh=False)
    account.check_in_background()      # at most one detached licence refresh an hour; never waited for
    install.require_runtime()
    kept = install.kept_help(args)     # `pptx --help`: the engine's own text, kept beside it after its first time
    if kept is not None:
        return kept
    install.wait_for_packs(args)
    sessions.engine_notice(args[0] if args else "")
    code = install.run_engine(args)
    if args[:1] == ["finish"]:
        # The usage counts and the reports kept for later go at the end of a deck, in a detached send (never on the
        # command's own time; never raises, never prints).
        usage.send_in_background(force=True, reports=True)
    return code


def run_python(args: list[str]) -> int:
    account.require("pptx", allow_refresh=False)
    account.check_in_background()
    install.require_runtime()
    return install.run_engine(args, python_passthrough=True)
