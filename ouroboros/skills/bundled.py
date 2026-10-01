"""Skills that ship with the code, and the condition each one is offered under.

Located relative to *this module* — the code that is actually running — so a deployment whose
``OUROBOROS_REPO_DIR`` points elsewhere still gets a skill that belongs to a feature.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

#: ``ouroboros/skills/bundled.py`` -> the repo root that ships this code.
BUNDLED_SKILLS_ROOT = pathlib.Path(__file__).resolve().parents[2] / "skills"


def setting_flag(settings: Any, key: str) -> bool:
    """Read one boolean setting from either shape discovery is handed — an ``AppSettings``
    or the plain dict some callers still pass."""
    if isinstance(settings, Mapping):
        return bool(settings.get(key, False))
    return bool(getattr(settings, key, False))


@dataclass(frozen=True)
class BundledSkill:
    """One skill that ships with the code, and when it is offered."""

    name: str
    is_active: Callable[[Any], bool]


BUNDLED_SKILLS: tuple[BundledSkill, ...] = (
    BundledSkill("agent-delegation", lambda settings: setting_flag(settings, "A2A_EGRESS_ENABLED")),
)

#: What the shipped root contributes when it is not also the configured root: the registered
#: packs and nothing else, so this repository's other skills do not follow the code around.
BUNDLED_SKILL_NAMES = frozenset(skill.name for skill in BUNDLED_SKILLS)

#: Names the root itself in the discovery root list.
BUNDLED_ROOT_KIND = "bundled"


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
    "BUNDLED_ROOT_KIND",
    "BUNDLED_SKILLS",
    "BUNDLED_SKILLS_ROOT",
    "BUNDLED_SKILL_NAMES",
    "BundledSkill",
    "bundled_skills_root",
    "inactive_bundled_skill_names",
]
