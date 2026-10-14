"""Enforce config SSOT: no direct os.environ reads outside bootstrap layer."""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

ALLOWLIST_PREFIXES = (
    "ouroboros/storage/settings/",
    "ouroboros/app/cli.py",
)

ALLOWLIST_FILES = frozenset(
    {
        "ouroboros/tools/skill_exec.py",
        "ouroboros/skills/extensions/companion.py",
        "ouroboros/app/server_control.py",
        "ouroboros/utils.py",
        "server.py",
    }
)

ALLOWED_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "TMPDIR",
        "TMP",
        "TEMP",
        "PYTEST_CURRENT_TEST",
        "OUROBOROS_AGENT_PYTHON",
        "OUROBOROS_SERVER_PROCESS_PID",
        "OUROBOROS_ENV",
        "OUROBOROS_SERVER_HOST",
        "OUROBOROS_SERVER_PORT",
        "OUROBOROS_DATA_DIR",
        "OUROBOROS_SECRETS_DIR",
    }
)


def _is_allowlisted(rel: str) -> bool:
    if rel in ALLOWLIST_FILES:
        return True
    return any(rel.startswith(prefix) for prefix in ALLOWLIST_PREFIXES)


def _env_key_from_node(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _collect_violations(path: pathlib.Path) -> list[str]:
    rel = path.relative_to(REPO).as_posix()
    if _is_allowlisted(rel):
        return []

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Attribute)
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
                and func.value.attr == "environ"
                and func.attr == "get"
                and node.args
            ):
                key = _env_key_from_node(node.args[0])
                if key is None or key not in ALLOWED_ENV_KEYS:
                    violations.append(f"{rel}:{node.lineno} os.environ.get({key!r})")
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr == "getenv"
                and node.args
            ):
                key = _env_key_from_node(node.args[0])
                if key is None or key not in ALLOWED_ENV_KEYS:
                    violations.append(f"{rel}:{node.lineno} os.getenv({key!r})")
        if isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
                and value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value not in ALLOWED_ENV_KEYS
            ):
                violations.append(f"{rel}:{node.lineno} os.environ[{node.slice.value!r}]")
    return violations


def test_no_direct_env_reads_outside_bootstrap_layer() -> None:
    violations: list[str] = []
    for root in (REPO / "ouroboros", REPO / "supervisor"):
        for path in root.rglob("*.py"):
            violations.extend(_collect_violations(path))
    assert not violations, "Direct env access outside bootstrap layer:\n" + "\n".join(sorted(violations))
