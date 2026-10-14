"""Shared skill-payload path resolution policy."""

from __future__ import annotations

from pathlib import PurePosixPath

SKILL_PRESENTATION_PATH = PurePosixPath("agents/gigaagent.yaml")

SKILL_PAYLOAD_CONTROL_FILENAMES = frozenset(
    {
        ".clawhub.json",
        ".ouroboroshub.json",
        "skill.openclaw.md",
    }
)

SKILL_PAYLOAD_CONTROL_DIRNAMES = frozenset(
    {
        ".ouroboros_env",
        "node_modules",
        "__pycache__",
    }
)

# A name added here is also hidden from the loader's sensitive-file check.
SKILL_PAYLOAD_SKIPPED_NAMES = SKILL_PAYLOAD_CONTROL_DIRNAMES | frozenset(
    {
        ".DS_Store",
        ".git",
        ".hg",
        ".idea",
        ".svn",
        ".tox",
        ".vscode",
    }
)


def is_skill_presentation_file(rel_posix: str) -> bool:
    return rel_posix.lower() == SKILL_PRESENTATION_PATH.as_posix()


def canonical_declared_script_rel(declared_name: str) -> PurePosixPath | None:
    name = declared_name.strip()
    if not name:
        return None
    return PurePosixPath(name if ("/" in name or name.startswith(".")) else f"scripts/{name}")


__all__ = [
    "SKILL_PAYLOAD_CONTROL_DIRNAMES",
    "SKILL_PAYLOAD_CONTROL_FILENAMES",
    "SKILL_PAYLOAD_SKIPPED_NAMES",
    "SKILL_PRESENTATION_PATH",
    "canonical_declared_script_rel",
    "is_skill_presentation_file",
]
