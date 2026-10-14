"""Station extraction has no runtime traffic switch to an old pipeline."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from finengine.config import Settings
from finengine.skills import extract_text


def _names_in_entrypoint() -> set[str]:
    tree = ast.parse(inspect.getsource(extract_text.extract_stream_from_text).lstrip())
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def test_two_stage_is_the_only_station_extraction_engine():
    names = _names_in_entrypoint()
    assert "extract_stream_finengine_dev_two_stage" in names
    assert "extract_stream_finengine_dev" not in names


def test_runtime_architecture_switch_is_removed():
    assert "extract_arch" not in Settings.model_fields
    source = inspect.getsource(extract_text.extract_stream_from_text)
    assert "FINENGINE_EXTRACT_ARCH" not in source
    assert "full_context" not in source


def test_no_other_runtime_module_selects_an_extraction_architecture():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in [*(root / "src").rglob("*.py"), *(root / "station-skills").rglob("*.py")]:
        if "extract_arch" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert not offenders
