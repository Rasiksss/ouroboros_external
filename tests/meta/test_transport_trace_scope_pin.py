"""No transport ingress leaves its operation identifier to a default argument.

The defect this change fixed was `trace_scope()` with no argument at an ingress: valid code that
does something reasonable, invisible in review, and arriving by copying the transport next door.
A count of instrumented integrations would stay green while a fourth transport quietly started
its own chains, so the pin is on the shape of the call instead.
"""

from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TRANSPORT_DIR = _REPO_ROOT / "transport"


def _trace_scope_calls() -> list[tuple[str, int, ast.Call]]:
    found: list[tuple[str, int, ast.Call]] = []
    for path in sorted(_TRANSPORT_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "trace_scope":
                found.append((str(path.relative_to(_REPO_ROOT)), node.lineno, node))
    return found


def test_every_transport_scope_is_given_an_identifier_explicitly():
    bare = [f"{rel}:{lineno}" for rel, lineno, call in _trace_scope_calls() if not call.args and not call.keywords]

    assert not bare, f"a transport ingress opened a scope with no identifier: {bare}"


def test_the_pin_is_looking_at_something():
    """A walker that silently matched nothing would pass forever."""
    assert _trace_scope_calls(), "no trace_scope call found under transport/ — the pin is blind"
