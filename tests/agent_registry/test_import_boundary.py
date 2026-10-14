from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import config_registry.agent_registry as agent_registry_pkg

_PKG_DIR = pathlib.Path(agent_registry_pkg.__file__).parent
_REPO_ROOT = _PKG_DIR.parents[1]
_FORBIDDEN = ("transport", "supervisor", "multiuser_config")


def _module_files() -> list[pathlib.Path]:
    return sorted(p for p in _PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_every_module_is_covered() -> None:
    assert {p.name for p in _module_files()} >= {"domain.py", "interfaces.py", "repo.py", "endpoint.py"}


def test_no_static_import_of_a_transport() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _FORBIDDEN:
                        offenders.append((str(path.relative_to(_PKG_DIR)), alias.name))
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in _FORBIDDEN:
                offenders.append((str(path.relative_to(_PKG_DIR)), node.module or ""))
    assert not offenders, f"agent_registry must not import {_FORBIDDEN}: {offenders}"


def test_importing_the_package_loads_no_transport() -> None:
    forbidden = ", ".join(repr(name) for name in _FORBIDDEN)
    code = f"import importlib, pkgutil, sys, config_registry.agent_registry as pkg\nforbidden = ({forbidden},)\nfor sub in pkgutil.walk_packages(pkg.__path__, 'config_registry.agent_registry.'):\n    importlib.import_module(sub.name)\nleaked=[m for m in sys.modules if m.split('.')[0] in forbidden]\nassert not leaked, leaked\n"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(_REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
