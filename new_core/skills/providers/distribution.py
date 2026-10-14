"""The trusted read-only skill distribution shipped with the runtime."""

from __future__ import annotations

import pathlib
from collections.abc import Sequence

from new_core.skills.interfaces import ISkillLoader, ISkillProvider
from new_core.skills.models import LoadedSkill

__all__ = ["DistributionSkillProvider"]


class DistributionSkillProvider(ISkillProvider):
    """An atomic snapshot over the configured distribution roots."""

    def __init__(
        self,
        *,
        distribution_root: pathlib.Path,
        loader: ISkillLoader,
    ) -> None:
        self._distribution_root = pathlib.Path(distribution_root)
        self._loader = loader
        self._skills: tuple[LoadedSkill, ...] = ()

    @property
    def source_id(self) -> str:
        return "distribution"

    def refresh(self) -> None:
        self._skills = tuple(self._loader.load(self._distribution_root))

    def skills(self) -> Sequence[LoadedSkill]:
        return self._skills
