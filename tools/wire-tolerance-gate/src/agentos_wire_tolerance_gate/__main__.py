"""CLI entry: ``python -m agentos_wire_tolerance_gate``."""

from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
