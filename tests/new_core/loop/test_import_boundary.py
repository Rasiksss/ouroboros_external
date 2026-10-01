"""Task 7.1: the loop layer must import nothing from ouroboros (stdlib + new_core only)."""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import new_core.loop as loop_pkg

_PKG_DIR = pathlib.Path(loop_pkg.__file__).parent
_REPO_ROOT = _PKG_DIR.parents[1]


def _module_files() -> list[pathlib.Path]:
    return sorted(_PKG_DIR.glob("*.py"))


def test_no_static_ouroboros_import() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "ouroboros":
                        offenders.append((path.name, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == "ouroboros":
                    offenders.append((path.name, node.module or ""))
    assert not offenders, f"loop layer must not import ouroboros: {offenders}"


def test_importing_loop_does_not_load_ouroboros() -> None:
    # Fresh interpreter: the shared pytest process has ouroboros loaded by other
    # test modules, so only a clean import proves the boundary.
    code = (
        "import sys, new_core.loop;"
        "leaked=[m for m in sys.modules if m == 'ouroboros' or m.startswith('ouroboros.')];"
        "assert not leaked, leaked"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
