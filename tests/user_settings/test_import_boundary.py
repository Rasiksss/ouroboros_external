from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import config_registry.user_settings as user_settings_pkg

_PKG_DIR = pathlib.Path(user_settings_pkg.__file__).parent
_REPO_ROOT = _PKG_DIR.parent
_FORBIDDEN = ("transport", "supervisor", "multiuser_config", "ouroboros.agent", "ouroboros.egress.a2a")


def _module_files() -> list[pathlib.Path]:
    return sorted(p for p in _PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _forbids(module: str) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in _FORBIDDEN)


def test_every_module_is_covered() -> None:
    assert {p.name for p in _module_files()} >= {
        "interfaces.py",
        "reader.py",
        "repo.py",
        "endpoint.py",
        "cache.py",
    }


def test_no_static_import_of_a_transport_or_of_what_executes_work() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    (str(path.relative_to(_PKG_DIR)), alias.name) for alias in node.names if _forbids(alias.name)
                )
            elif isinstance(node, ast.ImportFrom) and _forbids(node.module or ""):
                offenders.append((str(path.relative_to(_PKG_DIR)), node.module or ""))
    assert not offenders, f"user_settings must not import {_FORBIDDEN}: {offenders}"


def test_importing_the_package_loads_none_of_them() -> None:
    forbidden = ", ".join(repr(name) for name in _FORBIDDEN)
    code = (
        "import importlib, pkgutil, sys, user_settings\n"
        f"forbidden = ({forbidden},)\n"
        "for sub in pkgutil.walk_packages(user_settings.__path__, 'user_settings.'):\n"
        "    importlib.import_module(sub.name)\n"
        "leaked=[m for m in sys.modules if any(m == f or m.startswith(f + '.') for f in forbidden)]\n"
        "assert not leaked, leaked\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(_REPO_ROOT))
    assert proc.returncode == 0, proc.stderr


def test_the_package_does_not_define_the_instance_identity() -> None:
    """``SETTINGS_OWNER_ID`` belongs to the contract that uses it, not to this table's module."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in _module_files())
    assert "SETTINGS_OWNER_ID" not in source
    assert "ouroboros.contracts.owner_scope" not in source
