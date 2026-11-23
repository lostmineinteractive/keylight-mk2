"""Daemon entrypoint: `python -m bridge` or `python /path/to/bridge/__main__.py`."""

import sys
from pathlib import Path

if not __package__:
    # launchd starts this file by absolute path, which puts bridge/ itself on
    # sys.path. Swap in the parent so the package (and the stdlib) import cleanly.
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from bridge.daemon import main

sys.exit(main())
