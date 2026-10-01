from __future__ import annotations

import json
import pathlib

import pytest

from tests._shared import make_tool_env, run_tool


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
        "ouroboros.skills.loader.load_settings",
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
    from ouroboros.skills.loader import discover_skills

    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "echo-skill")

    skills = discover_skills(drive, workspace_root=workspace)
    tool_skills = [s for s in skills if s.manifest.is_tool()]

    assert len(tool_skills) == 1
    assert tool_skills[0].name == "echo-skill"
    assert tool_skills[0].eligible is True
    assert tool_skills[0].manifest.entry == "tool.py"


def test_tool_skill_without_tool_py_is_blocked(tmp_path, monkeypatch):
    from ouroboros.skills.loader import discover_skills

    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "broken", include_tool_py=False)

    skills = discover_skills(drive, workspace_root=workspace)
    tool_skills = [s for s in skills if s.manifest.is_tool()]

    assert len(tool_skills) == 1
    assert tool_skills[0].eligible is False
    assert any("tool.py" in reason for reason in tool_skills[0].blocked_reasons)


def test_distribution_tool_skill_loads_without_manual_enablement(tmp_path, monkeypatch):

    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "echo-skill")

    catalog, _ctx = make_tool_env(workspace, drive)
    assert "echo_skill.echo" in catalog.local_entries()
    assert "echo" not in catalog.local_entries()


def test_distribution_tool_skill_loads_without_review_grant(tmp_path, monkeypatch):

    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "echo-skill")
    catalog, _ctx = make_tool_env(workspace, drive)

    assert "echo_skill.echo" in catalog.local_entries()


def test_tool_skill_loads_namespaced_alias_after_review_grant(tmp_path, monkeypatch):
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (True, ""))
    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(workspace / "skills", "echo-skill")
    catalog, ctx = make_tool_env(workspace, drive)

    assert "echo_skill.echo" in catalog.local_entries()
    assert run_tool(catalog, ctx, "echo_skill.echo", {"text": "hello"}) == "hello"


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
    assert "shadow_calc.calculator" in catalog.local_entries()


def test_validate_skill_checks_tool_syntax(tmp_path, monkeypatch):
    from new_core.tools.context import ToolContext
    from ouroboros.tools.skill_provisioning import _handle_validate_skill

    workspace, drive = _discover_workspace(tmp_path, monkeypatch)
    _write_tool_skill(
        workspace / "skills",
        "broken-syntax",
        tool_code="def get_tools(\n    this is broken\n",
    )

    ctx = ToolContext(repo_dir=workspace, drive_root=drive)
    result = json.loads(_handle_validate_skill(ctx, name="broken-syntax"))

    assert result["status"] == "failed"
    assert any(finding.get("check") == "tool_syntax" and not finding.get("ok") for finding in result["findings"])


def test_manual_enable_and_disable_tools_are_not_registered() -> None:
    from ouroboros.tools.skill_provisioning import get_tools

    names = {tool.name for tool in get_tools()}

    assert "enable_skill" not in names
    assert "disable_skill" not in names
