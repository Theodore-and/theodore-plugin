"""Entry point when a launcher runs this client: python -I -S <client dir>/__main__.py <command...> (0.5.1 and later),
or python -I <client dir> <command...> (a launcher or a short command written before 0.5.1, and every start of the
service and the relay).

Run as a file, Python loads neither runpy nor, with -S, site (6 of the 38 ms of its start on a 4-core Windows test
machine); -I puts no script folder on sys.path, so this file adds its own. A command for the running service (`pptx`,
`guide`, `specimen`, `skill`) goes first to the hot path (hot.py), which loads only what reaching it needs. When the hot
path does not answer, the full client runs the command as before, with site loaded first when -S left it out (the
runtime's packages are on sys.path again, exactly as in a start without -S)."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if not sys.path or os.path.normcase(os.path.abspath(sys.path[0] or ".")) != os.path.normcase(_HERE):
    sys.path.insert(0, _HERE)

code = None
if len(sys.argv) > 1 and sys.argv[1] in ("pptx", "guide", "specimen", "skill"):
    from theodore_client import hot
    code = hot.run(sys.argv[1:])
if code is None:
    if sys.flags.no_site:
        import site
        site.main()
    from theodore_client.cli import main
    code = main()
sys.exit(code)
