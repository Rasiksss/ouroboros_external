"""Enforce the security config SSOT: the legacy server lists are read only where sanctioned."""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

LEGACY_KEYS = frozenset({"MCP_SERVERS", "A2A_REMOTE_AGENTS"})

# Where the fields are defined, validated and serialized. The other sanctioned reader is
# ``security_config/``, which owns the runtime lists and is outside the scanned roots.
ALLOWLIST_FILES = frozenset({"ouroboros/storage/settings/schema.py"})


def _collect_violations(path: pathlib.Path) -> list[str]:
    rel = path.relative_to(REPO).as_posix()
    if rel in ALLOWLIST_FILES:
        return []

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        # Attribute reads (``settings.MCP_SERVERS``) and the string form every dict-like
        # accessor takes (``settings.get("MCP_SERVERS")``, payload keys, env names).
        if isinstance(node, ast.Attribute) and node.attr in LEGACY_KEYS:
            violations.append(f"{rel}:{node.lineno} .{node.attr}")
        if isinstance(node, ast.Name) and node.id in LEGACY_KEYS:
            violations.append(f"{rel}:{node.lineno} {node.id}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in LEGACY_KEYS:
            violations.append(f"{rel}:{node.lineno} {node.value!r}")
    return violations


def test_legacy_server_lists_are_read_only_in_security_config() -> None:
    violations: list[str] = []
    for root in (REPO / "ouroboros", REPO / "supervisor", REPO / "new_core"):
        for path in root.rglob("*.py"):
            violations.extend(_collect_violations(path))
    violations.extend(_collect_violations(REPO / "server.py"))
    assert not violations, "MCP_SERVERS/A2A_REMOTE_AGENTS belong to security_config/ and schema.py:\n" + "\n".join(
        sorted(violations)
    )
