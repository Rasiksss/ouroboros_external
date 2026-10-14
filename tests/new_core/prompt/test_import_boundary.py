"""The prompt substrate must import nothing from ouroboros (stdlib only).

Invariant 4 enumerates the packages held to this rule; ``new_core/prompt`` joins
it. The boundary is what keeps section *declarations* — which are about this
product's prompt — from drifting down into the mechanism that assembles one.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import new_core.prompt as prompt_pkg

_PKG_DIR = pathlib.Path(prompt_pkg.__file__).parent
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
    assert not offenders, f"prompt substrate must not import ouroboros: {offenders}"


def test_importing_prompt_does_not_load_ouroboros() -> None:
    # Fresh interpreter: the shared pytest process has ouroboros loaded by other
    # test modules, so only a clean import proves the boundary.
    code = (
        "import sys, new_core.prompt;"
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


def test_the_substrate_names_no_section() -> None:
    """It may describe the shape of a declaration, never the subject of one."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in _module_files())
    for subject in ("Scratchpad", "Recalled", "Recent chat", "Identity", "Knowledge", "Runtime context"):
        assert subject not in source, f"the substrate must not name the {subject!r} section"
