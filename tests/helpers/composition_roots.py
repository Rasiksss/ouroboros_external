from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

START_UP_ROOTS = (
    "server.py",
    "multiuser_config/bootstrap.py",
    "multiuser_config/config_registry/app.py",
    "multiuser_config/transport/browser/app.py",
)


def called_names_in_order(root: str) -> list[str]:
    calls = [
        node
        for node in ast.walk(ast.parse((_REPO_ROOT / root).read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    calls.sort(key=lambda node: (node.lineno, node.col_offset))
    return [node.func.id for node in calls]


__all__ = ["START_UP_ROOTS", "called_names_in_order"]
