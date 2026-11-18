from __future__ import annotations

import logging
from dataclasses import dataclass

import yaml

from new_core.skills.catalog import SkillCatalogSnapshot
from new_core.skills.models import LoadedSkill, SkillId
from ouroboros.contracts.skill_payload_policy import SKILL_PRESENTATION_PATH

log = logging.getLogger(__name__)
_MAX_METADATA_BYTES = 16 * 1024


@dataclass(frozen=True)
class SkillPresentation:
    display_name: str
    short_description: str


def _display_text(value: object, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        try:
            value.encode("utf-8")
            return value.strip()
        except UnicodeEncodeError:
            pass
    return fallback


def read_skill_presentation(skill: LoadedSkill) -> SkillPresentation:
    fallback = SkillPresentation(skill.name, skill.description)
    if not skill.distribution_owned or not skill.selected:
        return fallback
    try:
        root = skill.skill_dir.resolve(strict=True)
        path = (root / SKILL_PRESENTATION_PATH).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > _MAX_METADATA_BYTES:
            return fallback
        with path.open("rb") as stream:
            data = stream.read(_MAX_METADATA_BYTES + 1)
        if len(data) > _MAX_METADATA_BYTES:
            return fallback
        document = yaml.safe_load(data.decode("utf-8"))
    except FileNotFoundError:
        return fallback
    except (OSError, ValueError, RuntimeError, yaml.YAMLError):
        log.warning("event=skill_presentation_invalid skill_id=%s", skill.skill_id)
        return fallback
    interface = document.get("interface") if isinstance(document, dict) else None
    if not isinstance(interface, dict):
        return fallback
    return SkillPresentation(
        _display_text(interface.get("display_name"), skill.name),
        _display_text(interface.get("short_description"), skill.description),
    )


def load_skill_presentations(snapshot: SkillCatalogSnapshot) -> dict[SkillId, SkillPresentation]:
    return {skill.skill_id: read_skill_presentation(skill) for skill in snapshot.skills()}
