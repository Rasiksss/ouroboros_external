#!/usr/bin/env python3
"""Outlive the manifest timeout so the kill path stays assertable."""

from __future__ import annotations

import sys
import time

# Manifest timeout_sec is 10; overshoot it clearly without stalling a suite for long.
_SLEEP_SEC = 30


def main() -> int:
    print("OUROBOROS_TEST_SCRIPT_SLOW: sleeping", flush=True)
    time.sleep(_SLEEP_SEC)
    print("OUROBOROS_TEST_SCRIPT_SLOW: not killed — timeout path regressed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
