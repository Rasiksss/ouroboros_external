"""Read-only tools for OpenClaw-style skill discovery and validation."""

from __future__ import annotations

import json
import pathlib

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.contracts.skill_payload_policy import (
    SKILL_OWNER_STATE_FILENAMES,
    SKILL_PAYLOAD_CONTROL_DIRNAMES,
    SKILL_PAYLOAD_CONTROL_FILENAMES,
)
from ouroboros.skills.loader import check_skills, find_skill, summarize_skills
from ouroboros.tools.catalog import SKILL_READ_RESULT_LIMIT
from ouroboros.tools.core import coerce_line_window, render_line_slice

_DISCOVERY_PAGE_LIMIT = 15


def _compact_discovery_field(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = "".join(character if character.isprintable() else " " for character in text)
    return " ".join(text.split())[:limit]


def _list_skills(
    ctx: ToolContext,
    agent_name: str = "",
    view: str = "full",
    cursor: int = 0,
    limit: int = _DISCOVERY_PAGE_LIMIT,
) -> str:
    payload = summarize_skills(
        pathlib.Path(ctx.drive_root),
        workspace_root=pathlib.Path(ctx.repo_dir),
        agent_name=str(agent_name or "").strip() or None,
    )
    if str(view or "").strip().lower() != "discovery":
        return json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        start = max(0, int(cursor))
    except (TypeError, ValueError):
        start = 0
    try:
        page_limit = min(_DISCOVERY_PAGE_LIMIT, max(1, int(limit)))
    except (TypeError, ValueError):
        page_limit = _DISCOVERY_PAGE_LIMIT
    discoverable = [
        skill
        for skill in payload.get("skills") or []
        if isinstance(skill, dict) and skill.get("selected") and skill.get("eligible")
    ]
    page = discoverable[start : start + page_limit]
    next_cursor = start + len(page)
    compact = {
        "view": "discovery",
        "cursor": start,
        "total": len(discoverable),
        "skills": [
            {
                "name": _compact_discovery_field(skill.get("name"), 80),
                "description": _compact_discovery_field(skill.get("description"), 260),
            }
            for skill in page
        ],
        "next_cursor": next_cursor if next_cursor < len(discoverable) else None,
    }
    return json.dumps(compact, ensure_ascii=False, indent=2)


def _check_skills(ctx: ToolContext, agent_name: str = "") -> str:
    payload = check_skills(
        pathlib.Path(ctx.drive_root),
        workspace_root=pathlib.Path(ctx.repo_dir),
        agent_name=str(agent_name or "").strip() or None,
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _skill_document_path(path: str) -> pathlib.PurePosixPath | None:
    raw = str(path or "")
    if "\x00" in raw or "\\" in raw:
        return None
    relative = pathlib.PurePosixPath(raw or "SKILL.md")
    if relative.is_absolute() or ".." in relative.parts:
        return None
    if relative == pathlib.PurePosixPath("SKILL.md"):
        return relative
    if len(relative.parts) < 2 or relative.parts[0] != "references":
        return None
    blocked_names = SKILL_PAYLOAD_CONTROL_DIRNAMES | SKILL_PAYLOAD_CONTROL_FILENAMES | SKILL_OWNER_STATE_FILENAMES
    if any(part.lower() in blocked_names for part in relative.parts[1:]):
        return None
    return relative


def _skill_read_error(code: str, detail: str) -> str:
    return f"⚠️ {code}: {detail}"


def _skill_read(
    ctx: ToolContext,
    skill: str,
    path: str = "",
    start_line: int = 1,
    max_lines: int = 2000,
    start_char: int | None = None,
) -> str:
    loaded = find_skill(
        pathlib.Path(ctx.drive_root),
        str(skill or ""),
        workspace_root=pathlib.Path(ctx.repo_dir),
    )
    if loaded is None:
        return _skill_read_error(
            "SKILL_READ_NOT_FOUND",
            "no skill matches the requested logical name.",
        )
    if not loaded.selected or not loaded.eligible or loaded.skill_path is None:
        return _skill_read_error(
            "SKILL_READ_NOT_ELIGIBLE",
            f"skill {loaded.name!r} is not both selected and eligible.",
        )

    relative = _skill_document_path(path)
    if relative is None:
        return _skill_read_error(
            "SKILL_READ_BLOCKED",
            "only SKILL.md and files below references/ are readable.",
        )

    try:
        root = loaded.skill_dir.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return _skill_read_error(
            "SKILL_READ_ERROR",
            f"skill {loaded.name!r} package root is unavailable.",
        )

    if relative == pathlib.PurePosixPath("SKILL.md"):
        candidate = loaded.skill_path
        if candidate.is_symlink():
            return _skill_read_error(
                "SKILL_READ_BLOCKED",
                "SKILL.md must be a regular manifest file, not a symlink.",
            )
    else:
        candidate = loaded.skill_dir.joinpath(*relative.parts)
    try:
        target = candidate.resolve(strict=True)
    except FileNotFoundError:
        return _skill_read_error(
            "SKILL_READ_FILE_NOT_FOUND",
            f"document {relative.as_posix()!r} does not exist.",
        )
    except (OSError, RuntimeError):
        return _skill_read_error(
            "SKILL_READ_BLOCKED",
            f"document {relative.as_posix()!r} cannot be resolved safely.",
        )

    readable_root = root if relative == pathlib.PurePosixPath("SKILL.md") else root / "references"
    try:
        target.relative_to(readable_root)
    except ValueError:
        return _skill_read_error(
            "SKILL_READ_BLOCKED",
            f"document {relative.as_posix()!r} escapes its readable documentation root.",
        )
    if not target.is_file():
        return _skill_read_error(
            "SKILL_READ_BLOCKED",
            f"document {relative.as_posix()!r} is not a regular file.",
        )

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _skill_read_error(
            "SKILL_READ_NOT_TEXT",
            f"document {relative.as_posix()!r} is not valid UTF-8 text.",
        )
    except OSError:
        return _skill_read_error(
            "SKILL_READ_ERROR",
            f"document {relative.as_posix()!r} could not be read.",
        )

    start, limit = coerce_line_window(start_line, max_lines)
    start = max(1, start)
    limit = min(2000, max(1, limit))
    if start_char is not None:
        try:
            start_char = max(1, int(start_char))
        except (TypeError, ValueError):
            start_char = 1
    logical_path = f"skill:{loaded.name}/{relative.as_posix()}"
    return render_line_slice(
        logical_path,
        content,
        start_line=start,
        max_lines=limit,
        start_char=start_char,
        max_chars=SKILL_READ_RESULT_LIMIT,
    )


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "list_skills",
            ToolSchema(
                name="list_skills",
                description=(
                    "List discovered SKILL.md instruction packs, including location, precedence, "
                    "eligibility, and shadowing. Use view=discovery for compact cursor-paginated "
                    "logical names and descriptions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": "Optional agent name for agent-specific skill allowlists.",
                        },
                        "view": {
                            "type": "string",
                            "enum": ["full", "discovery"],
                            "default": "full",
                            "description": ("Full administrative status or compact model discovery page."),
                        },
                        "cursor": {
                            "type": "integer",
                            "default": 0,
                            "minimum": 0,
                            "description": "Zero-based discovery cursor from next_cursor.",
                        },
                        "limit": {
                            "type": "integer",
                            "default": _DISCOVERY_PAGE_LIMIT,
                            "minimum": 1,
                            "maximum": _DISCOVERY_PAGE_LIMIT,
                            "description": "Maximum skills returned in one discovery page.",
                        },
                    },
                },
            ),
            _list_skills,
        ),
        ToolEntry(
            "check_skills",
            ToolSchema(
                name="check_skills",
                description=(
                    "Validate discovered SKILL.md instruction packs and report parse, gating, "
                    "oversize, path, and shadowing problems."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": "Optional agent name for agent-specific skill allowlists.",
                        }
                    },
                },
            ),
            _check_skills,
        ),
        ToolEntry(
            "skill_read",
            ToolSchema(
                name="skill_read",
                description=(
                    "Read the selected eligible skill's SKILL.md or a UTF-8 document "
                    "below its references/ directory. Follow every continuation cursor "
                    "until the document is complete. Other package files are blocked."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "skill": {
                            "type": "string",
                            "description": "Logical skill name from the available-skills index.",
                        },
                        "path": {
                            "type": "string",
                            "default": "",
                            "description": ("Empty or SKILL.md for the manifest; otherwise a path below references/."),
                        },
                        "start_line": {
                            "type": "integer",
                            "default": 1,
                            "minimum": 1,
                        },
                        "max_lines": {
                            "type": "integer",
                            "default": 2000,
                            "minimum": 1,
                            "maximum": 2000,
                        },
                        "start_char": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Optional 1-based absolute character offset returned "
                                "by a previous character page; takes precedence over start_line."
                            ),
                        },
                    },
                    "required": ["skill"],
                },
            ),
            _skill_read,
        ),
    ]
