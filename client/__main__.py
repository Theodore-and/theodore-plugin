"""Entry point when the launcher runs this directory: python -I <client dir> <command...>."""
import sys

from theodore_client.cli import main

sys.exit(main())
