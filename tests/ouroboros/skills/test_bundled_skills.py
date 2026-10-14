"""Skills that ship with the code: offered by their own condition, not by a path variable."""

from __future__ import annotations

import pathlib

import pytest

from config_registry.user_settings.api.http.skills import skills_payload
from new_core.skills.models import SkillId
from new_core.skills.surface import SkillSurface
from ouroboros.skills import bundled
from ouroboros.skills.bundled import (
    BUNDLED_SKILLS,
    BUNDLED_SKILLS_ROOT,
    inactive_bundled_skill_names,
)
from ouroboros.skills.loader import discover_distribution_skills
from ouroboros.skills.presentation import load_skill_presentations, read_skill_presentation
from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import make_skill_availability, make_skill_catalog

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

_PACK_MANIFEST = "---\nname: {name}\ndescription: {description}\nversion: {version}\ntype: instruction\n---\n\nBody.\n"


def _settings(**overrides) -> AppSettings:
    return AppSettings.model_validate(overrides)


def _names(distribution_root, settings) -> list[str]:
    return [skill.name for skill in discover_distribution_skills(distribution_root, settings=settings)]


def _write_pack(
    skill_dir: pathlib.Path,
    *,
    description: str,
    version: str,
    name: str = "agent-delegation",
) -> None:
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        _PACK_MANIFEST.format(name=name, description=description, version=version),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_every_distribution_package_ships_russian_display_text():
    skills = discover_distribution_skills(BUNDLED_SKILLS_ROOT, settings=_settings(A2A_EGRESS_ENABLED=True))
    for skill in skills:
        presentation = read_skill_presentation(skill)
        assert any("А" <= letter <= "я" for letter in presentation.display_name), skill.name
        assert any("А" <= letter <= "я" for letter in presentation.short_description), skill.name


def test_the_delegation_pack_is_bound_to_outbound_a2a():
    entry = next(skill for skill in BUNDLED_SKILLS if skill.name == "agent-delegation")
    assert entry.is_active(_settings(A2A_EGRESS_ENABLED=True)) is True
    assert entry.is_active(_settings(A2A_EGRESS_ENABLED=False)) is False
    assert inactive_bundled_skill_names(_settings(A2A_EGRESS_ENABLED=True)) == frozenset()
    assert inactive_bundled_skill_names(_settings(A2A_EGRESS_ENABLED=False)) == frozenset({"agent-delegation"})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_the_condition_hides_it_where_the_configured_root_is_the_shipped_one(tmp_path):
    included = _names(REPO_ROOT / "skills", _settings(A2A_EGRESS_ENABLED=True))
    names = _names(REPO_ROOT / "skills", _settings(A2A_EGRESS_ENABLED=False))
    assert set(names) == set(included) - {"agent-delegation"}


def test_an_unrelated_skill_of_the_same_name_is_not_swept_out_with_the_pack(tmp_path):
    """The condition hides *the pack* — not every skill that happens to share its name."""
    workspace = tmp_path / "workspace"
    _write_pack(workspace / "skills" / "agent-delegation", description="Somebody else's.", version="1.0.0")

    discovered = discover_distribution_skills(
        workspace / "skills",
        settings=_settings(A2A_EGRESS_ENABLED=False),
    )
    packs = [skill for skill in discovered if skill.name == "agent-delegation"]
    assert [skill.manifest.version for skill in packs] == ["1.0.0"]
    assert packs[0].selected


def test_hiding_the_pack_is_logged_rather_than_silent(tmp_path, monkeypatch, caplog):
    _write_pack(tmp_path / "agent-delegation", description="Delegation", version="1")
    monkeypatch.setattr(bundled, "BUNDLED_SKILLS_ROOT", tmp_path)
    with caplog.at_level("INFO", logger="ouroboros.skills.loader"):
        _names(tmp_path, _settings(A2A_EGRESS_ENABLED=False))
    assert any("agent-delegation" in record.getMessage() for record in caplog.records)


def test_the_configured_root_is_not_discovered_twice(tmp_path):
    """When the repo directory does point here, the skill appears exactly once."""
    names = _names(REPO_ROOT / "skills", _settings(A2A_EGRESS_ENABLED=True))
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(True, True), (False, False)],
)
def test_bundled_feature_gate_uses_directory_identity_after_display_rename(
    tmp_path,
    monkeypatch,
    enabled,
    expected,
):
    bundled_root = tmp_path / "bundled"
    _write_pack(
        bundled_root / "agent-delegation",
        name="renamed-display",
        description="Bundled.",
        version="1.0.0",
    )
    monkeypatch.setattr(bundled, "BUNDLED_SKILLS_ROOT", bundled_root)

    discovered = discover_distribution_skills(
        bundled_root,
        settings=_settings(A2A_EGRESS_ENABLED=enabled),
    )

    assert any(skill.skill_id.local_id == "agent-delegation" for skill in discovered) is expected


@pytest.mark.parametrize("enabled", [True, False])
def test_discovery_never_raises_on_a_missing_workspace(tmp_path, enabled):
    names = _names(tmp_path / "nope", _settings(A2A_EGRESS_ENABLED=enabled))
    assert names == []


@parametrize_from_dict(
    {
        "one package removed": {"remaining": ["kept"]},
        "empty skills directory": {"remaining": []},
        "missing skills directory": {"remaining": None},
    }
)
def test_reduced_distribution_publishes_remaining_skills_despite_stale_selection(tmp_path, remaining):
    settings = _settings(OUROBOROS_MULTIUSER=True)
    selected = ["distribution:kept", "distribution:removed"]
    distribution = tmp_path / "skills"
    if remaining is not None:
        distribution.mkdir()
        for name in remaining:
            _write_pack(distribution / name, name=name, description="Example", version="1")
    snapshot = make_skill_catalog(repo_dir=tmp_path, drive_root=tmp_path, settings=settings).snapshot()
    surface = SkillSurface(
        snapshot=snapshot,
        availability=make_skill_availability(executable_skills=False),
        task_selection=frozenset(map(SkillId.parse, selected)),
    )
    payload = skills_payload(surface, selected, "main", load_skill_presentations(snapshot))

    assert {row.id for row in payload.skills} == {f"distribution:{name}" for name in remaining or []}
    assert all(row.available and row.selected for row in payload.skills)
