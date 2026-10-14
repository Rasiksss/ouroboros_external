"""Application adapter over the process-local extension loader."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from new_core.skills.interfaces import IExtensionRuntime
from new_core.skills.models import LoadedSkill, SkillId
from new_core.skills.surface import SkillSurface
from ouroboros.skills.extensions import loader

__all__ = ["ExtensionRuntime"]


class ExtensionRuntime(IExtensionRuntime):
    def registered_tools(self) -> Sequence[dict[str, Any]]:
        return loader.registered_tools()

    def get_tool(self, name: str) -> dict[str, Any] | None:
        return loader.get_tool(name)

    def parse_surface_name(self, name: str) -> tuple[str, str] | None:
        return loader.parse_extension_surface_name(name)

    def is_live(self, skill_id: SkillId, skill_surface: SkillSurface) -> bool:
        return loader.is_extension_live(skill_id, skill_surface)

    def runtime_state(self, skill: LoadedSkill) -> dict[str, Any]:
        return loader.runtime_state_for_loaded_skill(skill)

    def unload(self, skill_name: str) -> None:
        loader.unload_extension(skill_name)
