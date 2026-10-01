"""Skills that ship with the code: offered by their own condition, not by a path variable."""

from __future__ import annotations

import pathlib

import pytest

from ouroboros.skills.bundled import (
    BUNDLED_SKILLS,
    BUNDLED_SKILLS_ROOT,
    bundled_skills_root,
    inactive_bundled_skill_names,
)
from ouroboros.skills.loader import _discover_openclaw_skills
from ouroboros.storage.settings.schema import AppSettings

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_PACK_MANIFEST = (
    "---\nname: agent-delegation\ndescription: {description}\nversion: {version}\ntype: instruction\n---\n\nBody.\n"
)


def _settings(**overrides) -> AppSettings:
    return AppSettings.model_validate(overrides)


def _names(drive_root, workspace_root, settings) -> list[str]:
    return [
        skill.name
        for skill in _discover_openclaw_skills(
            drive_root,
            workspace_root=workspace_root,
            agent_name=None,
            settings=settings,
        )
    ]


def _write_pack(skill_dir: pathlib.Path, *, description: str, version: str) -> None:
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        _PACK_MANIFEST.format(description=description, version=version),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_every_bundled_entry_names_a_real_skill_that_ships_with_the_code():
    assert BUNDLED_SKILLS_ROOT == REPO_ROOT / "skills"
    assert bundled_skills_root() == BUNDLED_SKILLS_ROOT
    for skill in BUNDLED_SKILLS:
        assert (BUNDLED_SKILLS_ROOT / skill.name / "SKILL.md").is_file(), skill.name


def test_the_delegation_pack_is_bound_to_outbound_a2a():
    entry = next(skill for skill in BUNDLED_SKILLS if skill.name == "agent-delegation")
    assert entry.is_active(_settings(A2A_EGRESS_ENABLED=True)) is True
    assert entry.is_active(_settings(A2A_EGRESS_ENABLED=False)) is False
    assert inactive_bundled_skill_names(_settings(A2A_EGRESS_ENABLED=True)) == frozenset()
    assert inactive_bundled_skill_names(_settings(A2A_EGRESS_ENABLED=False)) == frozenset({"agent-delegation"})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_the_pack_is_found_even_when_the_repo_directory_points_elsewhere(tmp_path):
    """The point of the registry: an empty workspace still yields the bundled skill."""
    elsewhere = tmp_path / "somewhere-else"
    (elsewhere / "skills").mkdir(parents=True)

    names = _names(tmp_path / "data", elsewhere, _settings(A2A_EGRESS_ENABLED=True))
    assert "agent-delegation" in names


def test_it_is_absent_from_the_catalogue_while_outbound_a2a_is_off(tmp_path):
    elsewhere = tmp_path / "somewhere-else"
    (elsewhere / "skills").mkdir(parents=True)

    names = _names(tmp_path / "data", elsewhere, _settings(A2A_EGRESS_ENABLED=False))
    assert "agent-delegation" not in names


def test_the_condition_hides_it_where_the_configured_root_is_the_shipped_one(tmp_path):
    names = _names(tmp_path / "data", REPO_ROOT, _settings(A2A_EGRESS_ENABLED=False))
    assert "agent-delegation" not in names
    # The other repo skills are untouched.
    assert "assignment-delegation" in names


def test_an_unrelated_skill_of_the_same_name_is_not_swept_out_with_the_pack(tmp_path):
    """The condition hides *the pack* — not every skill that happens to share its name."""
    workspace = tmp_path / "workspace"
    _write_pack(workspace / "skills" / "agent-delegation", description="Somebody else's.", version="1.0.0")

    discovered = _discover_openclaw_skills(
        tmp_path / "data",
        workspace_root=workspace,
        agent_name=None,
        settings=_settings(A2A_EGRESS_ENABLED=False),
    )
    packs = [skill for skill in discovered if skill.name == "agent-delegation"]
    assert [skill.manifest.version for skill in packs] == ["1.0.0"]
    assert packs[0].selected


def test_hiding_the_pack_is_logged_rather_than_silent(tmp_path, caplog):
    elsewhere = tmp_path / "somewhere-else"
    (elsewhere / "skills").mkdir(parents=True)

    with caplog.at_level("INFO", logger="ouroboros.skills.loader"):
        _names(tmp_path / "data", elsewhere, _settings(A2A_EGRESS_ENABLED=False))
    assert any("agent-delegation" in record.getMessage() for record in caplog.records)


def test_the_configured_root_is_not_discovered_twice(tmp_path):
    """When the repo directory does point here, the skill appears exactly once."""
    names = _names(tmp_path / "data", REPO_ROOT, _settings(A2A_EGRESS_ENABLED=True))
    assert names.count("agent-delegation") == 1


def test_a_workspace_copy_wins_over_the_bundled_one(tmp_path):
    workspace = tmp_path / "workspace"
    _write_pack(workspace / "skills" / "agent-delegation", description="Local override.", version="9.9.9")

    discovered = _discover_openclaw_skills(
        tmp_path / "data",
        workspace_root=workspace,
        agent_name=None,
        settings=_settings(A2A_EGRESS_ENABLED=True),
    )
    packs = [skill for skill in discovered if skill.name == "agent-delegation"]
    assert len(packs) == 2
    winner = next(skill for skill in packs if skill.selected)
    assert winner.manifest.version == "9.9.9"
    shadowed = next(skill for skill in packs if not skill.selected)
    assert shadowed.shadowed_by == winner.location


@pytest.mark.parametrize("enabled", [True, False])
def test_discovery_never_raises_on_a_missing_workspace(tmp_path, enabled):
    names = _names(tmp_path / "data", tmp_path / "nope", _settings(A2A_EGRESS_ENABLED=enabled))
    assert ("agent-delegation" in names) is enabled
