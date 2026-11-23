"""Daemon entrypoint: `python -m keylight_mk2` or `python /path/to/keylight_mk2/__main__.py`."""

import sys
from pathlib import Path

if not __package__:
    # launchd starts this file by absolute path, which puts keylight_mk2/ itself on
    # sys.path. Swap in the parent so the package (and the stdlib) import cleanly.
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from keylight_mk2.daemon import main

sys.exit(main())
