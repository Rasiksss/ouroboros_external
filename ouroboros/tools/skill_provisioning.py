"""Owner-controlled skill validation and enablement tools."""

from __future__ import annotations

import json
import pathlib
import py_compile
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _find(ctx: ToolContext, name: str):
    from ouroboros.skills.loader import find_skill

    return find_skill(pathlib.Path(ctx.drive_root), name, workspace_root=pathlib.Path(ctx.repo_dir))


def _tool_entry_path(skill: Any) -> pathlib.Path:
    entry = str(getattr(skill.manifest, "entry", "") or "tool.py").strip() or "tool.py"
    return pathlib.Path(skill.skill_dir) / entry


def _validate_loaded_skill(skill: Any) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if getattr(skill, "load_error", ""):
        findings.append({"check": "manifest", "ok": False, "message": str(skill.load_error)})
    else:
        findings.append({"check": "manifest", "ok": True})

    if not skill.manifest.is_tool():
        findings.append({"check": "type", "ok": False, "message": "skill type must be tool"})
    else:
        findings.append({"check": "type", "ok": True})

    entry = _tool_entry_path(skill)
    if not entry.is_file():
        findings.append(
            {"check": "tool_entry", "ok": False, "message": f"{skill.manifest.entry or 'tool.py'} not found"}
        )
    else:
        findings.append({"check": "tool_entry", "ok": True})
        try:
            py_compile.compile(str(entry), doraise=True)
            findings.append({"check": "tool_syntax", "ok": True})
        except py_compile.PyCompileError as exc:
            findings.append({"check": "tool_syntax", "ok": False, "message": str(exc)})
    ok = all(bool(item.get("ok")) for item in findings)
    return {"status": "ok" if ok else "failed", "findings": findings}


def _handle_validate_skill(ctx: ToolContext, name: str) -> str:
    skill = _find(ctx, name)
    if skill is None:
        return _json({"status": "failed", "message": f"skill {name!r} not found", "findings": []})
    payload = _validate_loaded_skill(skill)
    payload["name"] = skill.name
    payload["content_hash"] = skill.content_hash
    return _json(payload)


def _handle_reload_skills(ctx: ToolContext) -> str:
    from ouroboros.skills.loader import discover_skills

    skills = discover_skills(pathlib.Path(ctx.drive_root), workspace_root=pathlib.Path(ctx.repo_dir))
    return _json({"status": "ok", "count": len(skills)})


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "validate_skill",
            ToolSchema(
                name="validate_skill",
                description="Validate a trusted distribution tool skill.",
                parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            ),
            _handle_validate_skill,
        ),
        ToolEntry(
            "reload_skills",
            ToolSchema(
                name="reload_skills",
                description="Reload local skill discovery metadata.",
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            _handle_reload_skills,
        ),
    ]


__all__ = [
    "_handle_validate_skill",
    "get_tools",
]
