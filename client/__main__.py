"""Entry point when the launcher runs this directory: python -I <client dir> <command...>.

A command for the running service (`pptx`, `guide`, `specimen`, `skill`) goes first to the hot path (hot.py), which
loads only what reaching the service needs; when it cannot answer, the full client runs the command as before."""
import sys

code = None
if len(sys.argv) > 1 and sys.argv[1] in ("pptx", "guide", "specimen", "skill"):
    from theodore_client import hot
    code = hot.run(sys.argv[1:])
if code is None:
    from theodore_client.cli import main
    code = main()
sys.exit(code)
