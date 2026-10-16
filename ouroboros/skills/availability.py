"""Translate application settings into the core skill-availability value."""

from __future__ import annotations

from new_core.skills.surface import SkillAvailability
from ouroboros.storage.settings.schema import AppSettings

__all__ = ["build_skill_availability"]


def build_skill_availability(settings: AppSettings) -> SkillAvailability:
    return SkillAvailability.from_values(
        defaults=settings.get("agents.defaults.skills"),
        agents=settings.get("agents.list", []),
        executable_skills=not bool(settings.OUROBOROS_MULTIUSER),
        trusted_executable_skills=frozenset(settings.OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS),
    )
