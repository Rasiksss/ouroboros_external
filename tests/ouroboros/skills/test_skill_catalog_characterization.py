"""Current distribution-skill behavior preserved by the catalog refactor."""

from __future__ import annotations

import pathlib

from ouroboros.skills.loader import summarize_skills
from tests.helpers.shared import (
    discover_test_skills as discover_skills,
    find_test_skill as find_skill,
    make_skill_surface,
)


def _write_skill(
    root: pathlib.Path,
    name: str,
    *,
    skill_type: str = "instruction",
) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    extra = ""
    if skill_type == "script":
        extra = "runtime: python3\nscripts:\n  - name: scripts/run.py\n"
        script = skill_dir / "scripts" / "run.py"
        script.parent.mkdir()
        script.write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        (f"---\nname: {name}\ndescription: {name} description\ntype: {skill_type}\n{extra}---\n# {name}\n"),
        encoding="utf-8",
    )


def test_agent_allowlist_projection_does_not_mutate_another_discovery(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    """An agent-specific block is local to that discovery result."""
    workspace = tmp_path / "repo"
    drive = tmp_path / "drive"
    _write_skill(workspace, "guide")
    settings = {
        "agents.defaults.skills": ["distribution:guide"],
        "agents.list": [{"name": "restricted", "skills": []}],
    }
    monkeypatch.setattr("tests.helpers.shared.skill_test_settings", lambda: settings)

    default_skill = discover_skills(drive, workspace_root=workspace)[0]
    restricted_skill = discover_skills(drive, workspace_root=workspace, agent_name="restricted")[0]
    unknown_agent_skill = discover_skills(drive, workspace_root=workspace, agent_name="unknown")[0]

    assert default_skill.eligible is True
    assert default_skill.blocked_reasons == []
    assert restricted_skill.eligible is False
    assert restricted_skill.blocked_reasons == ["agent allowlist blocks this skill"]
    assert unknown_agent_skill.eligible is True
    assert default_skill.eligible is True
    assert default_skill.blocked_reasons == []


def test_blocked_skill_still_resolves_by_name_for_existing_consumers(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    """Lookup returns the blocked record so each consumer keeps its own error."""
    workspace = tmp_path / "repo"
    drive = tmp_path / "drive"
    _write_skill(workspace, "guide")
    monkeypatch.setattr(
        "tests.helpers.shared.skill_test_settings",
        lambda: {
            "agents.defaults.skills": [],
            "agents.list": [],
        },
    )

    loaded = find_skill(drive, "guide", workspace_root=workspace)
    surface = make_skill_surface(
        repo_dir=workspace,
        drive_root=drive,
        settings={
            "agents.defaults.skills": [],
            "agents.list": [],
        },
    )
    summary = summarize_skills(surface.skills())

    assert loaded is not None
    assert loaded.selected is True
    assert loaded.eligible is False
    assert loaded.blocked_reasons == ["agent allowlist blocks this skill"]
    assert summary["count"] == 1
    assert summary["skills"][0]["blocked_reason"] == "agent allowlist blocks this skill"


def test_multiuser_withholds_executable_skills_from_every_discovery_read(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    """Multi-user lookup and summary cannot recover an executable package."""
    workspace = tmp_path / "repo"
    drive = tmp_path / "drive"
    _write_skill(workspace, "guide")
    _write_skill(workspace, "runner", skill_type="script")
    monkeypatch.setattr(
        "tests.helpers.shared.skill_test_settings",
        lambda: {
            "OUROBOROS_MULTIUSER": True,
            "agents.defaults.skills": None,
            "agents.list": [],
        },
    )

    discovered = discover_skills(drive, workspace_root=workspace)
    surface = make_skill_surface(
        repo_dir=workspace,
        drive_root=drive,
        settings={
            "OUROBOROS_MULTIUSER": True,
            "agents.defaults.skills": None,
            "agents.list": [],
        },
    )
    summary = summarize_skills(surface.skills())

    assert [skill.name for skill in discovered] == ["guide"]
    assert find_skill(drive, "runner", workspace_root=workspace) is None
    assert [row["name"] for row in summary["skills"]] == ["guide"]
