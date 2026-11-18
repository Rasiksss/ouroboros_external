"""Spawn-safe package entrypoint for the single-process server."""

from __future__ import annotations

import sys


def main() -> int:
    from server import main as server_main

    return int(server_main())


if __name__ == "__main__":
    sys.exit(main())
