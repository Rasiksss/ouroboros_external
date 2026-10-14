"""Read-only tools for skill discovery and validation."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from hashlib import sha256

from new_core.skills.manifest import SkillManifest
from new_core.skills.models import SkillId
from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.interfaces import ToolResult
from new_core.tools.schema import ToolSchema
from ouroboros.contracts.skill_payload_policy import (
    SKILL_PAYLOAD_CONTROL_FILENAMES,
    SKILL_PAYLOAD_SKIPPED_NAMES,
    canonical_declared_script_rel,
    is_skill_presentation_file,
)
from ouroboros.skills.loader import check_skills, summarize_skills
from ouroboros.skills.repo_pack import is_sensitive_path
from ouroboros.tools.catalog import SKILL_READ_RESULT_LIMIT
from ouroboros.tools.core import ReadPage, coerce_line_window, read_page

_DISCOVERY_PAGE_LIMIT = 15
_MANIFEST_PATH = pathlib.PurePosixPath("SKILL.md")
_UNREADABLE_NAMES = frozenset(name.lower() for name in (SKILL_PAYLOAD_SKIPPED_NAMES | SKILL_PAYLOAD_CONTROL_FILENAMES))
_BLOCKED_DOCUMENT = (
    "only SKILL.md and the package's own documents are readable, by a path relative to the "
    "package root; whatever the manifest declares as code to run, and control files, are not."
)
_SKILLS_UNAVAILABLE = "⚠️ SKILLS_UNAVAILABLE: skill surface is not configured"


@dataclass(frozen=True)
class SkillReadResult:
    page: ReadPage
    identity: str
    version: str
    name: str


def _compact_discovery_field(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = "".join(character if character.isprintable() else " " for character in text)
    return " ".join(text.split())[:limit]


def _list_skills(
    ctx: ToolContext,
    view: str = "full",
    cursor: int = 0,
    limit: int = _DISCOVERY_PAGE_LIMIT,
) -> str:
    surface = ctx.skill_surface
    if surface is None:
        return _SKILLS_UNAVAILABLE
    payload = summarize_skills(surface.skills(), extension_runtime=ctx.extension_runtime)
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
                "skill_id": str(skill.get("skill_id") or ""),
                "name": _compact_discovery_field(skill.get("name"), 80),
                "description": _compact_discovery_field(skill.get("description"), 260),
            }
            for skill in page
        ],
        "next_cursor": next_cursor if next_cursor < len(discoverable) else None,
    }
    return json.dumps(compact, ensure_ascii=False, indent=2)


def _check_skills(ctx: ToolContext) -> str:
    surface = ctx.skill_surface
    if surface is None:
        return _SKILLS_UNAVAILABLE
    payload = check_skills(surface.skills(), extension_runtime=ctx.extension_runtime)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _payload_key(relative: pathlib.PurePath) -> str:
    return relative.as_posix().lower()


def _dispatched_paths(manifest: SkillManifest, root: pathlib.Path) -> frozenset[str]:
    declared: list[pathlib.PurePosixPath] = []
    for script in manifest.scripts:
        rel = canonical_declared_script_rel(script.get("name", ""))
        if rel is not None:
            declared.append(rel)
    entry = manifest.entry.strip()
    if entry:
        declared.append(pathlib.PurePosixPath(entry))
    for companion in manifest.companion_processes:
        for argument in list(companion.get("command") or [])[1:]:
            text = str(argument).strip()
            if text:
                declared.append(pathlib.PurePosixPath(text))

    paths: set[str] = set()
    for rel in declared:
        paths.add(_payload_key(rel))
        try:
            landed = root.joinpath(*rel.parts).resolve(strict=False).relative_to(root)
        except (ValueError, OSError, RuntimeError):
            continue
        paths.add(_payload_key(landed))
    return frozenset(paths)


def _skill_document_path(path: str) -> pathlib.PurePosixPath | None:
    raw = str(path or "")
    if "\x00" in raw or "\\" in raw:
        return None
    relative = pathlib.PurePosixPath(raw or _MANIFEST_PATH.as_posix())
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return None
    if relative == _MANIFEST_PATH:
        return relative
    if is_skill_presentation_file(relative.as_posix()):
        return None
    if any(part.lower() in _UNREADABLE_NAMES or is_sensitive_path(part) for part in relative.parts):
        return None
    if relative.parts[0].lower() == "scripts":
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
) -> str | ToolResult:
    surface = ctx.skill_surface
    if surface is None:
        return _SKILLS_UNAVAILABLE
    try:
        skill_id = SkillId.parse(str(skill or ""))
    except ValueError:
        return _skill_read_error("SKILL_READ_NOT_FOUND", "the requested skill id is not canonical.")
    loaded = surface.get(skill_id)
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
        return _skill_read_error("SKILL_READ_BLOCKED", _BLOCKED_DOCUMENT)

    try:
        root = loaded.skill_dir.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return _skill_read_error(
            "SKILL_READ_ERROR",
            f"skill {loaded.name!r} package root is unavailable.",
        )

    reads_manifest = relative == _MANIFEST_PATH
    dispatched: frozenset[str] = frozenset()
    if reads_manifest:
        candidate = loaded.skill_path
        if candidate.is_symlink():
            return _skill_read_error(
                "SKILL_READ_BLOCKED",
                "SKILL.md must be a regular manifest file, not a symlink.",
            )
    else:
        dispatched = _dispatched_paths(loaded.manifest, root)
        if _payload_key(relative) in dispatched:
            return _skill_read_error("SKILL_READ_BLOCKED", _BLOCKED_DOCUMENT)
        candidate = root.joinpath(*relative.parts)
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

    try:
        resolved_relative = target.relative_to(root)
    except ValueError:
        return _skill_read_error(
            "SKILL_READ_BLOCKED",
            f"document {relative.as_posix()!r} escapes the package.",
        )
    if not reads_manifest and (
        _skill_document_path(resolved_relative.as_posix()) is None or _payload_key(resolved_relative) in dispatched
    ):
        return _skill_read_error(
            "SKILL_READ_BLOCKED",
            f"document {relative.as_posix()!r} resolves to a file that is not readable.",
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
    logical_path = f"skill:{loaded.runtime_key}/{relative.as_posix()}"
    page = read_page(
        logical_path,
        content,
        start_line=start,
        max_lines=limit,
        start_char=start_char,
        max_chars=SKILL_READ_RESULT_LIMIT,
    )
    if reads_manifest:
        return ToolResult(
            page.render(),
            SkillReadResult(
                page=page,
                identity=loaded.runtime_key,
                version=sha256(content.encode()).hexdigest(),
                name=logical_path,
            ),
        )
    return page.render()


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "list_skills",
            ToolSchema(
                name="list_skills",
                description=(
                    "List discovered SKILL.md instruction packs, including location, precedence, "
                    "eligibility, and shadowing. Use view=discovery for compact cursor-paginated "
                    "canonical ids, logical names, and descriptions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
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
                    "properties": {},
                },
            ),
            _check_skills,
        ),
        ToolEntry(
            "skill_read",
            ToolSchema(
                name="skill_read",
                description=(
                    "Read the selected eligible skill's SKILL.md or any of its own UTF-8 "
                    "documents, at any depth, by the path SKILL.md names. Follow every "
                    "continuation cursor until the document is complete. Whatever the manifest "
                    "declares as code to run, and lifecycle files, are blocked."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "skill": {
                            "type": "string",
                            "description": "Canonical source-qualified skill id from the available-skills index.",
                        },
                        "path": {
                            "type": "string",
                            "default": "",
                            "description": (
                                "Empty or SKILL.md for the manifest; otherwise a document path "
                                "relative to the package root."
                            ),
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
