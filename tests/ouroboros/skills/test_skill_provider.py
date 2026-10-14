from __future__ import annotations

import pathlib

import pytest

from new_core.skills.catalog import SkillCatalog
from new_core.skills.models import SkillId
from new_core.skills.providers.distribution import DistributionSkillProvider
from ouroboros.skills.loader import DistributionSkillLoader
from ouroboros.storage.settings.schema import AppSettings


def _write_skill(root: pathlib.Path, name: str, *, manifest: str | None = None) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        manifest or (f"---\nname: {name}\ndescription: {name} description\ntype: instruction\n---\n# {name}\n"),
        encoding="utf-8",
    )


def _provider(tmp_path: pathlib.Path, root: pathlib.Path) -> DistributionSkillProvider:
    return DistributionSkillProvider(
        distribution_root=root,
        loader=DistributionSkillLoader(AppSettings()),
    )


def test_distribution_provider_requires_keyword_configuration(tmp_path: pathlib.Path) -> None:
    with pytest.raises(TypeError):
        DistributionSkillProvider(tmp_path, dict)  # type: ignore[misc]


def test_distribution_providers_with_different_roots_are_isolated(tmp_path: pathlib.Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_skill(root_a, "alpha")
    _write_skill(root_b, "beta")
    provider_a = _provider(tmp_path, root_a)
    provider_b = _provider(tmp_path, root_b)

    provider_a.refresh()
    provider_b.refresh()

    assert provider_a.source_id == "distribution"
    assert [skill.name for skill in provider_a.skills()] == ["alpha"]
    assert [skill.name for skill in provider_b.skills()] == ["beta"]
    assert [skill.skill_id.local_id for skill in provider_a.skills()] == ["alpha"]
    assert provider_a.skills()[0].skill_id == SkillId("distribution", "alpha")
    assert isinstance(provider_a.skills(), tuple)


def test_distribution_provider_refresh_replaces_one_atomic_snapshot(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha")
    provider = _provider(tmp_path, root)
    provider.refresh()
    initial = provider.skills()
    _write_skill(root, "beta")

    provider.refresh()
    replacement = provider.skills()

    assert replacement is not initial
    assert [skill.name for skill in replacement] == ["alpha", "beta"]

    def fail_discovery(*_args, **_kwargs):
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(
        "ouroboros.skills.loader.discover_distribution_skills",
        fail_discovery,
    )
    with pytest.raises(RuntimeError, match="discovery failed"):
        provider.refresh()

    assert provider.skills() is replacement
    assert provider.skills() == tuple(replacement)


def test_distribution_provider_keeps_individually_broken_skill_visible(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "broken", manifest="---\nname: broken\n")
    provider = _provider(tmp_path, root)

    provider.refresh()

    skill = provider.skills()[0]
    assert skill is not None
    assert skill.load_error
    assert skill.skill_id == SkillId("distribution", "broken")


def test_distribution_skill_id_uses_the_normalized_directory_name(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "directory-name",
        manifest=(
            "---\n"
            "name: manifest-name\n"
            "description: manifest name remains the current runtime alias\n"
            "type: instruction\n"
            "---\n"
        ),
    )
    provider = _provider(tmp_path, root)

    provider.refresh()

    assert provider.skills()[0].name == "manifest-name"
    assert provider.skills()[0].skill_id == SkillId("distribution", "directory-name")


def test_distribution_display_name_refresh_preserves_identity_and_runtime_key(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "directory-name",
        manifest="---\nname: first-name\ndescription: first\ntype: instruction\n---\n",
    )
    provider = _provider(tmp_path, root)
    catalog = SkillCatalog()
    catalog.initialize([provider])
    skill_id = SkillId("distribution", "directory-name")
    before = catalog.snapshot().get(skill_id)
    assert before is not None

    (root / "directory-name" / "SKILL.md").write_text(
        "---\nname: second-name\ndescription: second\ntype: instruction\n---\n",
        encoding="utf-8",
    )
    catalog.initialize([provider])
    after = catalog.snapshot().get(skill_id)

    assert after is not None
    assert (before.name, after.name) == ("first-name", "second-name")
    assert after.skill_id == before.skill_id == SkillId("distribution", "directory-name")
    assert after.skill_id.runtime_key == before.skill_id.runtime_key == "directory-name"


def test_distribution_display_alias_collision_does_not_change_package_selection(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    for directory in ("first-dir", "second-dir"):
        _write_skill(
            root,
            directory,
            manifest=f"---\nname: shared-alias\ndescription: {directory}\ntype: instruction\n---\n",
        )
    provider = _provider(tmp_path, root)
    catalog = SkillCatalog()
    catalog.initialize([provider])

    by_id = {str(skill.skill_id): skill for skill in provider.skills()}

    assert set(by_id) == {"distribution:first-dir", "distribution:second-dir"}
    assert all(skill.selected and skill.eligible for skill in by_id.values())
    assert catalog.snapshot().get(SkillId("distribution", "first-dir")) == by_id["distribution:first-dir"]
    assert catalog.snapshot().get(SkillId("distribution", "second-dir")) == by_id["distribution:second-dir"]


def test_distribution_nested_packages_use_their_source_relative_directory_identity(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "skills"
    _write_skill(
        root,
        "ops/postgres",
        manifest="---\nname: postgres-ops\ndescription: ops\ntype: instruction\n---\n",
    )
    _write_skill(
        root,
        "data/postgres",
        manifest="---\nname: postgres-data\ndescription: data\ntype: instruction\n---\n",
    )
    provider = _provider(tmp_path, root)
    catalog = SkillCatalog()
    catalog.initialize([provider])

    by_id = {str(skill.skill_id): skill for skill in provider.skills()}

    assert set(by_id) == {"distribution:ops/postgres", "distribution:data/postgres"}
    assert all(skill.selected and skill.eligible for skill in by_id.values())
    assert catalog.snapshot().get(SkillId("distribution", "ops/postgres")) == by_id["distribution:ops/postgres"]
    assert catalog.snapshot().get(SkillId("distribution", "data/postgres")) == by_id["distribution:data/postgres"]
    assert by_id["distribution:ops/postgres"].runtime_key != by_id["distribution:data/postgres"].runtime_key
