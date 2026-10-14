from __future__ import annotations

from new_core.skills.surface import SkillSurface
from ouroboros.storage.settings.store import load_settings

_BUILTIN_SKILL_TOOLS = {
    "calendar-orientation": ("calendar_resolve",),
    "image-generation": ("image_generate",),
    "tasks-scheduler": (
        "create_reminder",
        "create_scheduled_task",
        "edit_schedule",
        "confirm_schedule",
        "list_schedules",
        "cancel_schedule",
        "pause_schedule",
        "resume_schedule",
        "move_schedule_here",
    ),
    "workstation": (
        "workstation_connect",
        "code_exec",
        "file_read",
        "file_write",
        "file_edit",
        "apply_patch",
    ),
}
BUILTIN_TOOL_SKILL_ORIGINS = {
    tool: f"skill:{skill_id}" for skill_id, tools in _BUILTIN_SKILL_TOOLS.items() for tool in tools
}


def admitted_skill_origins(surface: SkillSurface) -> frozenset[str] | None:
    if not load_settings().OUROBOROS_MULTIUSER:
        return None
    return frozenset(f"skill:{skill_id.runtime_key}" for skill_id in surface.admitted_ids())


def skill_tool_refusal(name: str, origin: str, skill_origins: frozenset[str] | None) -> str | None:
    if skill_origins is None:
        return None
    source, _, source_id = origin.partition(":")
    required: str | None
    if source in {"skill", "ext"}:
        required = f"skill:{source_id}"
    elif source == "a2a":
        required = "skill:agent-delegation"
    elif origin == "builtin":
        required = BUILTIN_TOOL_SKILL_ORIGINS.get(name)
    else:
        required = None
    if required is not None and required not in skill_origins:
        return f"⚠️ TOOL_UNAVAILABLE ({name}): skill {required.removeprefix('skill:')!r} is not available to this run."
    return None
