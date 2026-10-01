"""Tool discovery meta-tools — lets the agent see and enable non-core tools.

Both read the catalog through ``ctx.tool_surface``: no injected handler, no module-global
registry. ``enable_tools`` resolves names here, so an unknown name is reported in the same
call; the resolved ``(name, schema)`` pairs travel on the override channel and the
``apply_overrides`` node puts them on the prompt surface for the next round.
"""

from __future__ import annotations

import logging
import re

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema

log = logging.getLogger(__name__)

_META_TOOL_NAMES = ("list_available_tools", "enable_tools")


def _parse_names(raw: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\s,]+", str(raw or "")):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _list_available_tools(ctx: ToolContext, **kwargs) -> str:
    surface = ctx.tool_surface
    if surface is None:
        return "Tool discovery not available in this context."

    active = surface.active_names()
    candidates = [
        desc
        for desc in surface.catalog()
        if desc.name
        and desc.name not in active
        and desc.name not in _META_TOOL_NAMES
        and desc.surface == "discoverable"
    ]
    if not candidates:
        return "All tools are already in your active set."
    lines = [f"**{len(candidates)} additional tools available** (use `enable_tools` to activate):\n"]
    for desc in candidates:
        description = desc.schema.description or "No description"
        lines.append(f"- **{desc.name}**: {description[:120]}")
    return "\n".join(lines)


def _enable_tools(ctx: ToolContext, tools: str = "", **kwargs) -> str:
    surface = ctx.tool_surface
    if surface is None:
        return "Tool enablement is only available inside the active agent loop."

    enabled: list[str] = []
    already_active: list[str] = []
    not_found: list[str] = []
    for name in _parse_names(tools):
        if name in surface.active_names():
            already_active.append(name)
            continue
        desc = surface.find(name)
        if desc is None or desc.surface == "excluded":
            not_found.append(name)
            continue
        if desc.name in surface.active_names():
            # An alias resolving onto an already-active tool: report it as such rather
            # than claiming an enable that request_enable would silently drop.
            already_active.append(name)
            continue
        surface.request_enable(desc.name, desc.schema)
        enabled.append(desc.name)

    parts = []
    reported = enabled + [f"{name} (already active)" for name in already_active]
    if reported:
        parts.append(f"✅ Enabled: {', '.join(reported)}")
    if not_found:
        parts.append(f"❌ Not found: {', '.join(not_found)}")
    return "\n".join(parts) if parts else "No tools specified."


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="list_available_tools",
            schema=ToolSchema(
                name="list_available_tools",
                description=(
                    "List all additional tools not currently in your active tool set. "
                    "Returns name + description for each. Use this to discover tools "
                    "you might need for specific tasks."
                ),
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            ),
            handler=_list_available_tools,
        ),
        ToolEntry(
            name="enable_tools",
            schema=ToolSchema(
                name="enable_tools",
                description=(
                    "Enable specific additional tools by name (comma-separated). "
                    "Their schemas will be added to your active tool set for the "
                    "remainder of this task. Example: enable_tools(tools='calculator,gigasearch')"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "tools": {
                            "type": "string",
                            "description": "Comma-separated tool names to enable",
                        }
                    },
                    "required": ["tools"],
                },
            ),
            handler=_enable_tools,
        ),
    ]
