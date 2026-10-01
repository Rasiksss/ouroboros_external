from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_pulse_skill_is_distributed_as_mcp_router(tmp_path: pathlib.Path) -> None:
    from ouroboros.skills.loader import find_skill

    skill = find_skill(tmp_path, "pulse-people", workspace_root=REPO_ROOT)

    assert skill is not None
    assert skill.selected is True
    assert skill.eligible is True
    assert skill.skill_path is not None
    manifest = skill.skill_path.read_text(encoding="utf-8")
    assert "list_available_tools" in manifest
    assert "enable_tools" in manifest
    assert "available tool descriptions" in manifest
    assert "Do not rely on fixed tool names, server ids, or namespaces" in manifest
    assert "untrusted data" in manifest
    assert "do not fall back to memory" in manifest
    assert "pulse_people_search" not in manifest


def test_pulse_tool_is_absent_from_runtime_surface(tmp_path: pathlib.Path) -> None:
    from new_core.tools.providers.builtin import FROZEN_TOOL_MODULES
    from ouroboros.agent.task_graph import bind_tool_surface
    from ouroboros.tools.catalog import TOOL_SPECS
    from tests._shared import make_tool_env

    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    schemas, _active = bind_tool_surface(catalog, ctx, [])
    discoverable = {desc.name for desc in catalog.descriptors() if desc.surface == "discoverable"}

    assert "pulse_people_search" not in TOOL_SPECS
    assert "pulse_people" not in FROZEN_TOOL_MODULES
    assert "pulse_people_search" not in catalog.local_tool_names()
    assert "pulse_people_search" not in {schema.name for schema in schemas}
    assert "pulse_people_search" not in discoverable


def test_static_context_does_not_reference_removed_pulse_tool() -> None:
    for relative_path in ("prompts/SYSTEM.md", "docs/ARCHITECTURE.md"):
        content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "pulse_people_search" not in content
