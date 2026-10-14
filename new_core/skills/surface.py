"""Per-run projections over the process-wide skill catalog."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace

from new_core.skills.catalog import SkillCatalogSnapshot
from new_core.skills.models import LoadedSkill, SkillBlockReason, SkillId

__all__ = ["SkillAvailability", "SkillSurface"]


@dataclass(frozen=True, slots=True)
class SkillAvailability:
    defaults: frozenset[str] | None
    agents: tuple[tuple[str, frozenset[str] | None], ...]
    executable_skills: bool = True

    @classmethod
    def from_values(
        cls,
        *,
        defaults: object,
        agents: object,
        executable_skills: bool,
    ) -> SkillAvailability:
        return cls(
            defaults=cls._string_list(defaults),
            agents=tuple(
                (str(item.get("name") or "").strip(), cls._string_list(item.get("skills")))
                for item in cls._json_list(agents)
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ),
            executable_skills=executable_skills,
        )

    def for_agent(self, agent_name: str | None) -> frozenset[str] | None:
        if agent_name:
            for name, skills in self.agents:
                if name == agent_name:
                    return skills
        return self.defaults

    def for_deployment(self) -> frozenset[str] | None:
        """Return the union needed to load code usable by any configured agent."""
        effective_agents: list[frozenset[str] | None] = []
        seen_agents: set[str] = set()
        for name, skills in self.agents:
            if name in seen_agents:
                continue
            seen_agents.add(name)
            effective_agents.append(skills)
        allowlists = (self.defaults, *effective_agents)
        if any(allowlist is None for allowlist in allowlists):
            return None
        return frozenset().union(*(allowlist for allowlist in allowlists if allowlist is not None))

    @staticmethod
    def _json_list(value: object) -> list[object]:
        if isinstance(value, list):
            return value
        if not isinstance(value, str) or not value.strip():
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _string_list(value: object) -> frozenset[str] | None:
        if value is None:
            return None
        if isinstance(value, list):
            return frozenset(str(item).strip() for item in value if str(item or "").strip())
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return frozenset()
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                return frozenset({text})
            if isinstance(parsed, list):
                return frozenset(str(item).strip() for item in parsed if str(item or "").strip())
        return None


class SkillSurface:
    """The skill records visible to one deployment or run."""

    def __init__(
        self,
        *,
        snapshot: SkillCatalogSnapshot,
        availability: SkillAvailability,
        agent_name: str | None = None,
        task_selection: frozenset[SkillId] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._availability = availability
        self._agent_name = str(agent_name or "").strip() or None
        self._task_selection = None if task_selection is None else frozenset(task_selection)

    def skills(self, *, agent_name: str | None = None) -> Sequence[LoadedSkill]:
        allowlist = self._availability.for_agent(self._agent_name or agent_name)
        return tuple(
            self._project(skill, allowlist)
            for skill in self._snapshot.skills()
            if self._is_visible(skill) and self._is_selected(skill)
        )

    def get(self, skill_id: SkillId, *, agent_name: str | None = None) -> LoadedSkill | None:
        requested = self._snapshot.get(skill_id)
        if requested is None or not self._is_visible(requested) or not self._is_selected(requested):
            return None
        return self._project(requested, self._availability.for_agent(self._agent_name or agent_name))

    def admitted_ids(self) -> frozenset[SkillId]:
        if self._task_selection is None:
            return frozenset()
        allowlist = self._availability.for_agent(self._agent_name)
        return frozenset(
            skill.skill_id
            for skill in self._snapshot.records()
            if skill.selected
            and skill.eligible
            and self._is_visible(skill)
            and skill.skill_id in self._task_selection
            and (allowlist is None or str(skill.skill_id) in allowlist)
        )

    def with_task_selection(self, selection: frozenset[SkillId]) -> SkillSurface:
        return SkillSurface(
            snapshot=self._snapshot,
            availability=self._availability,
            agent_name=self._agent_name,
            task_selection=selection,
        )

    def deployment_skills(self) -> Sequence[LoadedSkill]:
        allowlist = self._deployment_allowlist()
        return tuple(self._project(skill, allowlist) for skill in self._snapshot.skills() if self._is_visible(skill))

    def deployment_get(self, skill_id: SkillId) -> LoadedSkill | None:
        requested = self._snapshot.get(skill_id)
        if requested is None or not self._is_visible(requested):
            return None
        return self._project(requested, self._deployment_allowlist())

    def _deployment_allowlist(self) -> frozenset[str] | None:
        if self._agent_name:
            return self._availability.for_agent(self._agent_name)
        return self._availability.for_deployment()

    def _is_visible(self, skill: LoadedSkill) -> bool:
        return self._availability.executable_skills or not skill.manifest.is_executable()

    def _is_selected(self, skill: LoadedSkill) -> bool:
        return self._task_selection is None or skill.skill_id in self._task_selection

    @staticmethod
    def _project(skill: LoadedSkill, allowlist: frozenset[str] | None) -> LoadedSkill:
        if allowlist is None or str(skill.skill_id) in allowlist:
            return skill
        reasons = list(skill.blockers)
        if not any(reason.code == "agent_allowlist" for reason in reasons):
            reasons.append(SkillBlockReason("agent_allowlist", "agent allowlist blocks this skill"))
        return replace(skill, eligible=False, blockers=reasons)
