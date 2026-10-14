"""Enforce the security config SSOT: the settings keys behind the runtime lists are read only
where sanctioned."""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

#: The image-owned server lists are confined to security-config.
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


#: Every product root that can compose a runtime. A root left out is a root where the
#: confinement holds by nobody having written the read yet.
SCANNED_ROOTS = ("ouroboros", "supervisor", "new_core", "background_memory", "transport", "multiuser_config")


def test_legacy_server_lists_are_read_only_in_security_config() -> None:
    violations: list[str] = []
    for name in SCANNED_ROOTS:
        for path in (REPO / name).rglob("*.py"):
            violations.extend(_collect_violations(path))
    violations.extend(_collect_violations(REPO / "server.py"))
    assert not violations, "MCP_SERVERS/A2A_REMOTE_AGENTS belong to security_config/ and schema.py:\n" + "\n".join(
        sorted(violations)
    )


def test_a_composition_root_reading_the_legacy_key_is_a_violation(tmp_path, monkeypatch) -> None:
    """The scan is only as wide as its root list; a tier outside it would be unguarded."""
    import sys

    assert "multiuser_config" in SCANNED_ROOTS

    tier = tmp_path / "multiuser_config" / "tier.py"
    tier.parent.mkdir()
    tier.write_text("from x import load_settings\nservers = load_settings().MCP_SERVERS\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)

    assert _collect_violations(tier) == ["multiuser_config/tier.py:2 .MCP_SERVERS"]
