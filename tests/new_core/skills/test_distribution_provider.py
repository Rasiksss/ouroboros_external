from __future__ import annotations

import pathlib
from collections.abc import Sequence

import pytest

from new_core.skills.interfaces import ISkillLoader
from new_core.skills.manifest import SkillManifest
from new_core.skills.models import LoadedSkill, SkillId
from new_core.skills.providers.distribution import DistributionSkillProvider


def _skill(name: str) -> LoadedSkill:
    return LoadedSkill(
        skill_id=SkillId("distribution", name),
        name=name,
        skill_dir=pathlib.Path("/skills") / name,
        manifest=SkillManifest(name=name, description=name, version="1", type="instruction"),
        content_hash=name,
    )


class _Loader(ISkillLoader):
    def __init__(self, values: Sequence[LoadedSkill]) -> None:
        self.values = tuple(values)
        self.error: Exception | None = None
        self.roots: list[pathlib.Path] = []

    def load(self, root: pathlib.Path) -> Sequence[LoadedSkill]:
        self.roots.append(root)
        if self.error is not None:
            raise self.error
        return self.values


def test_distribution_provider_loads_through_its_injected_source() -> None:
    loader = _Loader([_skill("alpha")])
    root = pathlib.Path("/distribution")
    provider = DistributionSkillProvider(distribution_root=root, loader=loader)

    provider.refresh()

    assert loader.roots == [root]
    assert provider.skills()[0].skill_id == SkillId("distribution", "alpha")


def test_failed_refresh_preserves_the_published_snapshot() -> None:
    loader = _Loader([_skill("alpha")])
    provider = DistributionSkillProvider(distribution_root=pathlib.Path("/distribution"), loader=loader)
    provider.refresh()
    published = provider.skills()
    loader.error = RuntimeError("source unavailable")

    with pytest.raises(RuntimeError, match="source unavailable"):
        provider.refresh()

    assert provider.skills() is published
