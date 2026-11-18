"""Stable identities shared by skill sources, catalogs, and projections."""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass, field
from typing import Literal

from new_core.skills.manifest import SkillManifest

__all__ = ["LoadedSkill", "SkillBlockReason", "SkillId", "normalize_skill_alias"]

_QUALIFIED_RUNTIME_PREFIX = "sid_"


def normalize_skill_alias(value: str) -> str:
    """Return the legacy safe spelling used for a short skill name."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value or "").strip())
    return cleaned.strip("._")[:64]


@dataclass(frozen=True, slots=True)
class SkillId:
    """A source-qualified stable skill identifier."""

    source_id: str
    local_id: str

    def __post_init__(self) -> None:
        source_id = str(self.source_id or "")
        local_id = str(self.local_id or "")
        if not source_id or source_id != source_id.strip() or ":" in source_id:
            raise ValueError("source_id must be non-empty, trimmed, and contain no colon")
        if not local_id or local_id != local_id.strip():
            raise ValueError("local_id must be non-empty and trimmed")

    def __str__(self) -> str:
        return f"{self.source_id}:{self.local_id}"

    @property
    def runtime_key(self) -> str:
        """Return the collision-free key for runtime-owned skill resources."""
        # The bundled distribution predates source-qualified state paths.  Its source is
        # fixed, so retaining the local key keeps existing private state paths valid.
        if (
            self.source_id == "distribution"
            and normalize_skill_alias(self.local_id) == self.local_id
            and not self.local_id.startswith(_QUALIFIED_RUNTIME_PREFIX)
        ):
            return self.local_id
        digest = hashlib.sha256(str(self).encode()).hexdigest()
        return f"{_QUALIFIED_RUNTIME_PREFIX}{digest}"

    @classmethod
    def parse(cls, value: str) -> SkillId:
        """Parse the canonical ``source_id:local_id`` representation."""
        source_id, separator, local_id = str(value or "").partition(":")
        if not separator:
            raise ValueError("skill id must contain a source separator")
        return cls(source_id=source_id, local_id=local_id)


@dataclass(frozen=True, slots=True)
class SkillBlockReason:
    """A stable validation code with internal diagnostic details."""

    code: Literal[
        "agent_allowlist",
        "unsupported_os",
        "missing_executable",
        "missing_configuration",
        "package_too_large",
        "invalid_package",
    ]
    detail: str


@dataclass
class LoadedSkill:
    """One provider-published skill package record."""

    skill_id: SkillId
    name: str
    skill_dir: pathlib.Path
    manifest: SkillManifest
    content_hash: str
    load_error: str = ""
    source: str = "distribution"
    skill_path: pathlib.Path | None = None
    source_root: pathlib.Path | None = None
    source_kind: str = ""
    distribution_owned: bool = False
    precedence: int = 0
    selected: bool = True
    eligible: bool = True
    blockers: list[SkillBlockReason] = field(default_factory=list)
    shadowed_by: str = ""
    trusted_permissions: tuple[str, ...] = ()

    @property
    def blocked_reasons(self) -> list[str]:
        return [reason.detail for reason in self.blockers]

    @property
    def description(self) -> str:
        return self.manifest.description

    @property
    def location(self) -> str:
        path = self.skill_path
        if path is None:
            for name in ("SKILL.md", "skill.json"):
                candidate = self.skill_dir / name
                if candidate.is_file():
                    path = candidate
                    break
        return str(path or (self.skill_dir / "SKILL.md"))

    @property
    def skill_key(self) -> str:
        metadata = self.manifest.raw_extra.get("metadata")
        openclaw = metadata.get("openclaw") if isinstance(metadata, dict) else None
        key = openclaw.get("skillKey") if isinstance(openclaw, dict) else None
        return str(key or "").strip() or self.name

    @property
    def runtime_key(self) -> str:
        return self.skill_id.runtime_key
