"""Permission decisions over a published skill surface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from new_core.skills.models import SkillId
from new_core.skills.surface import SkillSurface

__all__ = ["CatalogSkillPermissionChecker", "ISkillPermissionChecker"]


class ISkillPermissionChecker(ABC):
    @abstractmethod
    def refusal(self, skill_id: SkillId, permission: str) -> str | None:
        """Return the refusal text, or None when the capability is trusted."""


class CatalogSkillPermissionChecker(ISkillPermissionChecker):
    def __init__(self, *, skill_surface: SkillSurface) -> None:
        self._skill_surface = skill_surface

    def refusal(self, skill_id: SkillId, permission: str) -> str | None:
        loaded = self._skill_surface.deployment_get(skill_id)
        if loaded is None:
            return f"skill {str(skill_id)!r} is not installed"
        if loaded.load_error:
            return f"skill {loaded.name!r} failed to load: {loaded.load_error}"
        if not loaded.selected or not loaded.eligible:
            reason = loaded.blocked_reasons[0] if loaded.blocked_reasons else "skill is not selected"
            return f"skill {loaded.name!r} is not eligible: {reason}"
        if permission not in loaded.trusted_permissions:
            return f"skill {loaded.name!r} does not declare trusted capability {permission!r}"
        return None
