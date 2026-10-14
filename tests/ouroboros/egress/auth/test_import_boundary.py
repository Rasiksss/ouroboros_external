from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LAYER = _REPO_ROOT / "ouroboros" / "egress" / "auth"
_FORBIDDEN = (
    "ouroboros.mcp",
    "ouroboros.egress.a2a",
    "transport",
    "supervisor",
    "multiuser_config",
    "ouroboros.agent",
    "new_core.tools",
    "mcp",
    "a2a",
)


def _forbidden(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _FORBIDDEN)


def _layer_modules() -> list[str]:
    return sorted(
        ".".join(path.relative_to(_REPO_ROOT).with_suffix("").parts).removesuffix(".__init__")
        for path in _LAYER.rglob("*.py")
    )


def _imported_names(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_egress_auth_imports_no_protocol_client_transport_or_tier() -> None:
    modules = _layer_modules()
    stated = sorted(name for path in _LAYER.rglob("*.py") for name in _imported_names(path) if _forbidden(name))
    code = (
        "import importlib, importlib.util, json, sys\n"
        "import ouroboros.storage.settings.schema\n"
        "schema = sorted(sys.modules)\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "layer = sorted(set(sys.modules) - set(schema))\n"
        "old_layer = importlib.util.find_spec('ouroboros.mcp.auth') is not None\n"
        "print(json.dumps({'schema': schema, 'layer': layer, 'old_layer': old_layer}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=_REPO_ROOT, capture_output=True, text=True, check=False)

    assert {
        "ouroboros.egress.auth",
        "ouroboros.egress.auth.authenticators",
        "ouroboros.egress.auth.client_guard",
        "ouroboros.egress.auth.interfaces",
    } <= set(modules)
    assert stated == []
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert [name for name in report["layer"] if _forbidden(name)] == []
    assert [name for name in report["schema"] if _forbidden(name) and not name.startswith("new_core.tools")] == []
    assert report["old_layer"] is False
