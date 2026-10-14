"""CLI wrapper for copied Finengine E2E quality tests."""
from __future__ import annotations

from pathlib import Path
import sys


def main() -> int:
    """Load the copied benchmark only for an explicit CLI execution.

    Pytest discovers this historical ``*_test.py`` wrapper.  Importing the
    vendor runtime at module import time used to prepend its copied ``src`` to
    the process and shadow the repository's canonical ``finengine`` package.
    """
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    from test_benchmark.finengine_e2e_tests.quality import main as quality_main

    return quality_main()


if __name__ == "__main__":
    raise SystemExit(main())
