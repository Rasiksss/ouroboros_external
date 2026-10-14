from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import config_registry.chat_registry as chat_registry_pkg

_PKG_DIR = pathlib.Path(chat_registry_pkg.__file__).parent
_REPO_ROOT = _PKG_DIR.parents[1]
_FORBIDDEN = ("transport", "supervisor", "multiuser_config")
#: The tier that serves the list composes inline precisely because it never calls a model, so a
#: model client named by this package would break that root or quietly give it a provider. Named,
#: not reachable: `ouroboros/storage/settings/schema.py` pulls the provider registry in for its own
#: model settings, so every package that reads a setting reaches it and the transitive form would
#: assert nothing about this one.
_FORBIDDEN_MODULES = ("ouroboros.llm", "openai", "anthropic")


def _module_files() -> list[pathlib.Path]:
    return sorted(p for p in _PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_every_module_is_covered() -> None:
    assert {p.name for p in _module_files()} >= {"domain.py", "interfaces.py", "repo.py", "endpoint.py"}


def _imported(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_static_import_of_a_transport() -> None:
    offenders = [
        (str(path.relative_to(_PKG_DIR)), module)
        for path in _module_files()
        for module in _imported(path)
        if module.split(".")[0] in _FORBIDDEN
    ]
    assert not offenders, f"chat_registry must not import {_FORBIDDEN}: {offenders}"


def test_no_module_names_a_model_client() -> None:
    offenders = [
        (str(path.relative_to(_PKG_DIR)), module)
        for path in _module_files()
        for module in _imported(path)
        if any(module == name or module.startswith(f"{name}.") for name in _FORBIDDEN_MODULES)
    ]
    assert not offenders, f"chat_registry must call no model: {offenders}"


def test_importing_the_package_loads_no_transport() -> None:
    forbidden = ", ".join(repr(name) for name in _FORBIDDEN)
    code = (
        "import importlib, pkgutil, sys, config_registry.chat_registry as pkg\n"
        f"forbidden = ({forbidden},)\n"
        "for sub in pkgutil.walk_packages(pkg.__path__, 'config_registry.chat_registry.'):\n"
        "    importlib.import_module(sub.name)\n"
        "leaked=[m for m in sys.modules if m.split('.')[0] in forbidden]\n"
        "assert not leaked, leaked\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(_REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
