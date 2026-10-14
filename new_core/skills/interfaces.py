"""Source interface for locally available, verified skill packages."""

from __future__ import annotations

import pathlib
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from new_core.skills.models import LoadedSkill, SkillId

if TYPE_CHECKING:
    from new_core.skills.surface import SkillSurface

__all__ = ["IExtensionRuntime", "ISkillLoader", "ISkillProvider"]


class IExtensionRuntime(ABC):
    """Live extension records consumed by the tool provider."""

    @abstractmethod
    def registered_tools(self) -> Sequence[dict[str, Any]]:
        """Return the currently registered extension tools."""

    @abstractmethod
    def get_tool(self, name: str) -> dict[str, Any] | None:
        """Resolve a registered extension tool name."""

    @abstractmethod
    def parse_surface_name(self, name: str) -> tuple[str, str] | None:
        """Parse a provider-safe extension tool name."""

    @abstractmethod
    def is_live(self, skill_id: SkillId, skill_surface: SkillSurface) -> bool:
        """Return whether the extension remains dispatchable."""

    @abstractmethod
    def runtime_state(self, skill: LoadedSkill) -> dict[str, Any]:
        """Return live-loading state for one published extension record."""

    @abstractmethod
    def unload(self, skill_name: str) -> None:
        """Unload one extension and its registered surfaces."""


class ISkillLoader(ABC):
    """Load validated package records from one configured source root."""

    @abstractmethod
    def load(self, root: pathlib.Path) -> Sequence[LoadedSkill]:
        """Return the source records found under root."""


class ISkillProvider(ABC):
    """One source's process-wide skill snapshot."""

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Stable identifier of this source."""

    @abstractmethod
    def refresh(self) -> None:
        """Rebuild the provider snapshot."""

    @abstractmethod
    def skills(self) -> Sequence[LoadedSkill]:
        """Return the current provider snapshot."""
