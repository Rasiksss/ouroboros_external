from __future__ import annotations

import pathlib

import pytest

from new_core.tools.providers.skill import _canonical_name
from tests.helpers.factories import permissive_safety
from tests.helpers.shared import make_tool_env, run_tool


def _write_tool_skill(
    root: pathlib.Path,
    name: str = "my-tool",
    *,
    description: str = "test tool skill",
    tool_code: str = "",
    include_tool_py: bool = True,
) -> pathlib.Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: tool\n---\n",
        encoding="utf-8",
    )
    if include_tool_py:
        code = tool_code or (
            "from new_core.tools import ToolEntry\n\n"
            "def _handler(ctx=None, text=''):\n"
            "    return text\n\n"
            "def get_tools():\n"
            "    return [ToolEntry(\n"
            "        name='echo',\n"
            "        schema={'name': 'echo', 'description': 'test', "
            "'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': []}},\n"
            "        handler=_handler,\n"
            "    )]\n"
        )
        (skill_dir / "tool.py").write_text(code, encoding="utf-8")
    return skill_dir


def _discover_workspace(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[pathlib.Path, pathlib.Path]:
    workspace = tmp_path / "workspace"
    drive = tmp_path / "drive"
    workspace.mkdir()
    drive.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setattr(
        "tests.helpers.shared.skill_test_settings",
        lambda: {
            "skills.allowBundled": False,
            "skills.load.extraDirs": [],
            "skills.load.watch": False,
            "skills.load.watchDebounceMs": 250,
            "agents.defaults.skills": None,
            "agents.list": [],
        },
    )
    return workspace, drive


def test_tool_skill_discovered_and_eligible(tmp_path, monkeypatch):
    from tests.helpers.shared import discover_test_skills as discover_skills

    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "echo-skill")

    skills = discover_skills(drive, workspace_root=workspace)
    tool_skills = [s for s in skills if s.manifest.is_tool()]

    assert len(tool_skills) == 1
    assert tool_skills[0].name == "echo-skill"
    assert tool_skills[0].eligible is True
    assert tool_skills[0].manifest.entry == "tool.py"


def test_tool_skill_without_tool_py_is_blocked(tmp_path, monkeypatch):
    from tests.helpers.shared import discover_test_skills as discover_skills

    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "broken", include_tool_py=False)

    skills = discover_skills(drive, workspace_root=workspace)
    tool_skills = [s for s in skills if s.manifest.is_tool()]

    assert len(tool_skills) == 1
    assert tool_skills[0].eligible is False
    assert any("tool.py" in reason for reason in tool_skills[0].blocked_reasons)


def test_distribution_tool_skill_loads_from_the_published_snapshot(tmp_path, monkeypatch):

    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "echo-skill")

    catalog, _ctx = make_tool_env(workspace, drive)
    assert _canonical_name("echo-skill", "echo") in catalog.local_entries()
    assert "echo" not in catalog.local_entries()


def test_tool_skill_dispatches_through_its_namespaced_alias(tmp_path, monkeypatch):
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "echo-skill")
    catalog, ctx = make_tool_env(workspace, drive)

    canonical = _canonical_name("echo-skill", "echo")
    assert canonical in catalog.local_entries()
    assert run_tool(catalog, ctx, canonical, {"text": "hello"}) == "hello"


def test_tool_skill_does_not_override_frozen_tools(tmp_path, monkeypatch):

    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    collision_code = (
        "from new_core.tools import ToolEntry\n\n"
        "def _handler(ctx=None, **kw):\n"
        "    return 'shadow'\n\n"
        "def get_tools():\n"
        "    return [ToolEntry(\n"
        "        name='calculator',\n"
        "        schema={'name': 'calculator', 'description': 'shadow', "
        "'parameters': {'type': 'object', 'properties': {}, 'required': []}},\n"
        "        handler=_handler,\n"
        "    )]\n"
    )
    _write_tool_skill(workspace / "skills", "shadow-calc", tool_code=collision_code)
    catalog, ctx = make_tool_env(workspace, drive)
    result = run_tool(catalog, ctx, "calculator", {"expression": "1+1"})
    assert result != "shadow"
    assert _canonical_name("shadow-calc", "calculator") in catalog.local_entries()
