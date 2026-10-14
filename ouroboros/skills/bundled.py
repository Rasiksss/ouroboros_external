"""Application feature gates for skills shipped in the release repository."""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: ``ouroboros/skills/bundled.py`` -> the repo root that ships this code.
BUNDLED_SKILLS_ROOT = pathlib.Path(__file__).resolve().parents[2] / "skills"


def setting_flag(settings: Any, key: str) -> bool:
    """Read one boolean setting through discovery's shared mapping contract."""
    return bool(settings.get(key, False))


@dataclass(frozen=True)
class BundledSkill:
    """One skill that ships with the code, and when it is offered."""

    name: str
    is_active: Callable[[Any], bool]


BUNDLED_SKILLS: tuple[BundledSkill, ...] = (
    BundledSkill("agent-delegation", lambda settings: setting_flag(settings, "A2A_EGRESS_ENABLED")),
)


def bundled_skills_root() -> pathlib.Path | None:
    """The shipped skills tree, or ``None`` when this install does not carry one."""
    return BUNDLED_SKILLS_ROOT if BUNDLED_SKILLS_ROOT.is_dir() else None


def inactive_bundled_skill_names(settings: Any) -> frozenset[str]:
    """Names to hide *from the bundled root*: their condition does not hold right now.

    Scoped to that root rather than applied by name everywhere, so an unrelated skill that
    happens to share a name is never swept out with the pack.
    """
    return frozenset(skill.name for skill in BUNDLED_SKILLS if not skill.is_active(settings))


__all__ = [
    "BUNDLED_SKILLS",
    "BUNDLED_SKILLS_ROOT",
    "BundledSkill",
    "bundled_skills_root",
    "inactive_bundled_skill_names",
]
