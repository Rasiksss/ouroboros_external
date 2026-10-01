#!/usr/bin/env python3
"""Exit non-zero with stderr output so the failure path stays assertable."""

from __future__ import annotations

import sys


def main() -> int:
    print("stdout before failure", flush=True)
    print("OUROBOROS_TEST_SCRIPT_FAIL: deliberate non-zero exit", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
