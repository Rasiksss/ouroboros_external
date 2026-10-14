from __future__ import annotations

import pathlib
from collections.abc import Sequence

import pytest

from new_core.skills.catalog import SkillCatalog
from new_core.skills.interfaces import ISkillLoader, ISkillProvider
from new_core.skills.manifest import SkillManifest
from new_core.skills.models import LoadedSkill, SkillBlockReason, SkillId
from new_core.skills.providers.distribution import DistributionSkillProvider


def _skill(source: str, local_id: str, name: str) -> LoadedSkill:
    return LoadedSkill(
        skill_id=SkillId(source, local_id),
        name=name,
        skill_dir=pathlib.Path("/skills") / source / local_id,
        manifest=SkillManifest(name=name, description=name, version="1", type="instruction"),
        content_hash=f"hash-{source}-{local_id}",
    )


class _Provider(ISkillProvider):
    def __init__(self, source_id: str, values: Sequence[LoadedSkill]) -> None:
        self._source_id = source_id
        self._skills = tuple(values)
        self.refreshes = 0

    @property
    def source_id(self) -> str:
        return self._source_id

    def refresh(self) -> None:
        self.refreshes += 1

    def skills(self) -> Sequence[LoadedSkill]:
        return self._skills

    def get(self, local_id: str) -> LoadedSkill | None:
        return next((skill for skill in self._skills if skill.skill_id.local_id == local_id), None)


class _FailingProvider(_Provider):
    def __init__(self, source_id: str, values: Sequence[LoadedSkill]) -> None:
        super().__init__(source_id, values)
        self.failure: Exception | None = None

    def refresh(self) -> None:
        super().refresh()
        if self.failure is not None:
            raise self.failure


class _Loader(ISkillLoader):
    def __init__(self, values: Sequence[LoadedSkill]) -> None:
        self._values = tuple(values)

    def load(self, root: pathlib.Path) -> Sequence[LoadedSkill]:
        del root
        return self._values


def test_skill_id_has_stable_round_trip_and_validates_parts() -> None:
    skill_id = SkillId("skillsmp", "community/pdf-tools")

    assert str(skill_id) == "skillsmp:community/pdf-tools"
    assert SkillId.parse(str(skill_id)) == skill_id
    assert SkillId("skillsmp", "community/pdf-tools") == skill_id
    with pytest.raises(ValueError):
        SkillId("", "skill")
    with pytest.raises(ValueError):
        SkillId("source", "")
    with pytest.raises(ValueError):
        SkillId.parse("unqualified")


def test_runtime_key_cannot_be_forged_by_a_distribution_local_id() -> None:
    external = SkillId("openclaw", "calendar")
    forged_distribution = SkillId("distribution", external.runtime_key)

    assert forged_distribution.runtime_key != external.runtime_key


def test_runtime_keys_keep_the_digest_for_long_source_ids() -> None:
    first = SkillId("s" * 64, "alpha")
    second = SkillId("s" * 64, "beta")

    assert first.runtime_key != second.runtime_key


def test_catalog_refuses_reads_before_initialize() -> None:
    catalog = SkillCatalog()

    with pytest.raises(RuntimeError, match="not initialized"):
        catalog.snapshot()


def test_catalog_rejects_duplicate_sources_and_reinitialize_replaces_providers() -> None:
    first = _Provider("one", [_skill("one", "alpha", "alpha")])
    replacement = _Provider("two", [_skill("two", "beta", "beta")])
    catalog = SkillCatalog()

    with pytest.raises(ValueError, match="duplicate.*one"):
        catalog.initialize([first, _Provider("one", [])])

    catalog.initialize([first])
    catalog.initialize([replacement])

    assert catalog.snapshot().get(SkillId("one", "alpha")) is None
    assert catalog.snapshot().get(SkillId("two", "beta")) == replacement.skills()[0]


def test_catalog_rejects_a_skill_id_owned_by_another_provider() -> None:
    catalog = SkillCatalog()

    with pytest.raises(ValueError, match="another source"):
        catalog.initialize([_Provider("openclaw", [_skill("distribution", "calendar", "calendar")])])


def test_catalog_resolves_only_full_ids() -> None:
    first_skill = _skill("one", "first-local", "shared")
    second_skill = _skill("two", "second-local", "shared")
    catalog = SkillCatalog()

    catalog.initialize([_Provider("one", [first_skill]), _Provider("two", [second_skill])])

    snapshot = catalog.snapshot()
    assert snapshot.skills() == (first_skill, second_skill)
    assert snapshot.get(SkillId("one", "first-local")) == first_skill
    assert first_skill.selected is True
    assert second_skill.selected is True


def test_catalog_reinitialize_keeps_the_previous_generation_when_a_candidate_fails() -> None:
    old = _skill("old", "old", "old")
    catalog = SkillCatalog()
    catalog.initialize([_Provider("old", [old])])
    previous = catalog.snapshot()
    candidate = _FailingProvider("candidate", [_skill("candidate", "new", "new")])
    candidate.failure = RuntimeError("candidate unavailable")

    with pytest.raises(RuntimeError, match="candidate unavailable"):
        catalog.initialize([candidate])

    assert catalog.snapshot() is previous
    assert catalog.snapshot().get(old.skill_id) == old


def test_published_snapshot_records_cannot_be_mutated_through_provider_or_reader() -> None:
    provider_skill = _skill("distribution", "alpha", "alpha")
    provider = _Provider("distribution", [provider_skill])
    catalog = SkillCatalog()
    catalog.initialize([provider])

    provider_skill.blockers.append(SkillBlockReason("invalid_package", "provider mutation"))
    projected = catalog.snapshot().get(provider_skill.skill_id)
    assert projected is not None
    projected.blockers.append(SkillBlockReason("invalid_package", "reader mutation"))

    reread = catalog.snapshot().get(provider_skill.skill_id)
    assert reread is not None
    assert reread.blocked_reasons == []


def test_catalog_does_not_resolve_aliases() -> None:
    skill = _skill("distribution", "foo_bar", "foo_bar")
    catalog = SkillCatalog()
    catalog.initialize([_Provider("distribution", [skill])])

    assert catalog.snapshot().get(SkillId("distribution", "foo_bar")) == skill


def test_catalog_rejects_untyped_references() -> None:
    alias_target = _skill("distribution", "victim", "openclaw_")
    catalog = SkillCatalog()
    catalog.initialize([_Provider("distribution", [alias_target])])

    assert catalog.snapshot().get(SkillId("openclaw", "missing")) is None


def test_catalog_keeps_same_display_names_distinct() -> None:
    catalog = SkillCatalog()

    catalog.initialize(
        [
            _Provider("one", [_skill("one", "first", "foo/bar")]),
            _Provider("two", [_skill("two", "second", "foo_bar")]),
        ]
    )

    assert catalog.snapshot().get(SkillId("one", "first")).skill_id == SkillId("one", "first")


def test_catalog_and_surface_resolve_the_selected_copy_of_one_stable_id() -> None:
    from new_core.skills.surface import SkillSurface
    from tests.helpers.shared import make_skill_availability

    shadowed = _skill("distribution", "calendar", "calendar")
    shadowed.selected = False
    shadowed.eligible = False
    shadowed.blockers = [SkillBlockReason("invalid_package", "shadowed")]
    selected = _skill("distribution", "calendar", "calendar")
    catalog = SkillCatalog()
    catalog.initialize([_Provider("distribution", [shadowed, selected])])
    surface = SkillSurface(snapshot=catalog.snapshot(), availability=make_skill_availability())

    assert catalog.snapshot().get(selected.skill_id) == selected
    assert surface.get(selected.skill_id) == selected
    assert surface.get("calendar") is None


def test_display_aliases_do_not_resolve_a_selected_copy() -> None:
    shadowed = _skill("distribution", "calendar", "old-calendar")
    shadowed.selected = False
    shadowed.eligible = False
    shadowed.blockers = [SkillBlockReason("invalid_package", "shadowed")]
    selected = _skill("distribution", "calendar", "new-calendar")
    catalog = SkillCatalog()
    catalog.initialize([_Provider("distribution", [shadowed, selected])])

    assert catalog.snapshot().get(selected.skill_id) == selected


def test_distribution_provider_requires_exact_local_id(tmp_path: pathlib.Path) -> None:
    skill = _skill("distribution", "foo_bar", "foo_bar")
    provider = DistributionSkillProvider(
        distribution_root=tmp_path / "skills",
        loader=_Loader([skill]),
    )
    provider.refresh()

    assert provider.skills() == (skill,)
