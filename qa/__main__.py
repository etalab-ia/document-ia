"""Module entrypoint for `uv run python -m qa ...`."""

from __future__ import annotations

import sys

from qa.cli import main

if __name__ == "__main__":
    sys.exit(main())
