"""``new_core`` must import nothing from ouroboros, outside a named exemption.

The rule belongs to the package, not to ``graph`` and ``loop`` individually: a future
``new_core/<pkg>`` must not be able to reintroduce the leak by being new. Per-subpackage
tests remain, checking the same rule from the subpackage's own point of view.

``new_core.tools`` is exempt by decision: provider integrations still reach Ouroboros
policy and external clients. The exemption is a hard-coded list rather than a skipped
test, so adding another exempt package is a visible edit here.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import new_core as new_core_pkg

_PKG_DIR = pathlib.Path(new_core_pkg.__file__).parent
_REPO_ROOT = _PKG_DIR.parent

#: Subpackages allowed to import ouroboros. Everything else in ``new_core`` stays pure.
_EXEMPT_SUBPACKAGES = frozenset({"tools"})


def _is_exempt(path: pathlib.Path) -> bool:
    rel = path.relative_to(_PKG_DIR).parts
    return bool(rel) and rel[0] in _EXEMPT_SUBPACKAGES


def _module_files() -> list[pathlib.Path]:
    return sorted(p for p in _PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts and not _is_exempt(p))


def test_the_exemption_list_names_only_real_subpackages() -> None:
    for name in _EXEMPT_SUBPACKAGES:
        assert (_PKG_DIR / name).is_dir(), f"{name!r} is exempt but is not a new_core subpackage"


def test_graph_loop_and_skills_are_never_exempt() -> None:
    assert not _EXEMPT_SUBPACKAGES & {"graph", "loop", "skills"}


def test_no_static_application_import() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"ouroboros", "transport"}:
                        offenders.append((str(path.relative_to(_PKG_DIR)), alias.name))
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in {"ouroboros", "transport"}:
                    offenders.append((str(path.relative_to(_PKG_DIR)), node.module or ""))
    assert not offenders, f"new_core must not import application packages: {offenders}"


def test_importing_every_subpackage_does_not_load_the_application() -> None:
    # Fresh interpreter: the shared pytest process has ouroboros loaded by other test
    # modules, so only a clean import proves the boundary. Every module under new_core is
    # imported, so a leak in a module no subpackage __init__ pulls in is still caught.
    # walk_packages imports a package to descend into it, so exempt subtrees are
    # skipped before the walk rather than filtered out of its results.
    exempt = ", ".join(repr(name) for name in sorted(_EXEMPT_SUBPACKAGES))
    code = (
        "import importlib, pkgutil, sys, new_core\n"
        f"exempt = ({exempt},)\n"
        "for entry in pkgutil.iter_modules(new_core.__path__):\n"
        "    if entry.name in exempt: continue\n"
        "    mod = importlib.import_module('new_core.' + entry.name)\n"
        "    if entry.ispkg:\n"
        "        for sub in pkgutil.walk_packages(mod.__path__, mod.__name__ + '.'):\n"
        "            importlib.import_module(sub.name)\n"
        "leaked=[m for m in sys.modules if m == 'ouroboros' or m.startswith('ouroboros.') "
        "or m == 'transport' or m.startswith('transport.')]\n"
        "assert not leaked, leaked\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
