"""The ordered process-wide catalog of skill sources."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from new_core.skills.interfaces import ISkillProvider
from new_core.skills.models import LoadedSkill, SkillId

__all__ = ["SkillCatalog", "SkillCatalogSnapshot"]


@dataclass(frozen=True, slots=True)
class SkillCatalogSnapshot:
    """One immutable generation of the ordered package catalogue."""

    _skills: tuple[LoadedSkill, ...]
    _by_id: Mapping[SkillId, LoadedSkill]

    def skills(self) -> Sequence[LoadedSkill]:
        return tuple(deepcopy(skill) for skill in self._skills)

    def records(self) -> tuple[LoadedSkill, ...]:
        """Shared records for read-only admission checks."""
        return self._skills

    def get(self, skill_id: SkillId) -> LoadedSkill | None:
        loaded = self._by_id.get(skill_id)
        return deepcopy(loaded) if loaded is not None else None


class SkillCatalog:
    """Combine provider snapshots while preserving provider order."""

    def __init__(self) -> None:
        self._snapshot: SkillCatalogSnapshot | None = None

    def initialize(
        self,
        providers: Sequence[ISkillProvider],
    ) -> None:
        candidate = tuple(providers)
        source_ids = [provider.source_id for provider in candidate]
        duplicates = sorted({source_id for source_id in source_ids if source_ids.count(source_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate skill provider source_id: {', '.join(duplicates)}")
        snapshot = self._build_snapshot(candidate)
        self._snapshot = snapshot

    def _require_initialized(self) -> SkillCatalogSnapshot:
        snapshot = self._snapshot
        if snapshot is None:
            raise RuntimeError("skill catalog is not initialized")
        return snapshot

    def snapshot(self) -> SkillCatalogSnapshot:
        return self._require_initialized()

    @staticmethod
    def _build_snapshot(providers: tuple[ISkillProvider, ...]) -> SkillCatalogSnapshot:
        for provider in providers:
            provider.refresh()
        skills: list[LoadedSkill] = []
        for provider in providers:
            for skill in provider.skills():
                if skill.skill_id.source_id != provider.source_id:
                    raise ValueError(
                        "skill provider returned an id from another source: "
                        f"provider={provider.source_id!r}, skill_id={skill.skill_id!s}"
                    )
                skills.append(deepcopy(skill))
        by_id: dict[SkillId, LoadedSkill] = {}
        for skill in skills:
            current = by_id.get(skill.skill_id)
            if current is None or (skill.selected and not current.selected):
                by_id[skill.skill_id] = skill
        return SkillCatalogSnapshot(
            _skills=tuple(skills),
            _by_id=MappingProxyType(by_id),
        )
