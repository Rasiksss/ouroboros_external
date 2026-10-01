#!/usr/bin/env python3
"""Present on disk but absent from the manifest: skill_exec must refuse to run it."""

from __future__ import annotations


def main() -> int:
    # Reaching this line means the declared-script allowlist was bypassed.
    print("OUROBOROS_TEST_SCRIPT_UNDECLARED_RAN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
