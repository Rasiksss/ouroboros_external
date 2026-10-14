from collections.abc import Mapping

from pydantic import BaseModel

from new_core.skills.models import LoadedSkill, SkillId
from new_core.skills.surface import SkillSurface
from ouroboros.skills.presentation import SkillPresentation


class SkillPayload(BaseModel):
    id: str
    name: str
    display_name: str
    description: str
    short_description: str
    version: str | None
    selected: bool
    available: bool
    blocked_reasons: list[str]


class SkillsPayload(BaseModel):
    skills: list[SkillPayload]


def _blocked_reasons(skill: LoadedSkill) -> list[str]:
    labels = {
        "agent_allowlist": "agent allowlist blocks this skill",
        "unsupported_os": "unsupported operating system",
        "missing_executable": "required executable is unavailable",
        "missing_configuration": "required configuration is unavailable",
        "package_too_large": "skill package exceeds size limits",
    }
    return list(dict.fromkeys(labels.get(reason.code, "skill package unavailable") for reason in skill.blockers))


def skills_payload(
    surface: SkillSurface,
    selected_ids: list[str],
    agent_id: str,
    presentations: Mapping[SkillId, SkillPresentation],
) -> SkillsPayload:
    selected = frozenset(selected_ids)
    rows = []
    for skill in surface.skills(agent_name=agent_id):
        if skill.skill_id.source_id != "distribution":
            continue
        presentation = presentations[skill.skill_id]
        rows.append(
            SkillPayload(
                id=str(skill.skill_id),
                name=skill.name,
                display_name=presentation.display_name,
                description=skill.description,
                short_description=presentation.short_description,
                version=skill.manifest.version or None,
                selected=str(skill.skill_id) in selected,
                available=skill.eligible,
                blocked_reasons=_blocked_reasons(skill),
            )
        )
    return SkillsPayload(skills=rows)
