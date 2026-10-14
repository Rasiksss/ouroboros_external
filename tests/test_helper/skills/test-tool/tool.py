"""In-process tools for the ``type: tool`` fixture, exported via ``get_tools()``.

``SkillToolProvider`` imports this module by path and re-exports each entry under
a ``test_tool.<name>`` alias, so handler signatures follow the same
``fn(ctx, **args) -> str`` contract as builtin tools.
"""

from __future__ import annotations

import json
from typing import Any

from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema

MARKER = "OUROBOROS_TEST_TOOL_OK"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _ping(ctx: Any) -> str:
    """Fixed marker plus the context fields a skill tool is expected to see."""
    return _json(
        {
            "marker": MARKER,
            "tool": "ping",
            "task_id": str(getattr(ctx, "task_id", "") or ""),
            "has_drive_root": bool(getattr(ctx, "drive_root", "")),
            "has_repo_dir": bool(getattr(ctx, "repo_dir", "")),
        }
    )


def _echo(ctx: Any, *, text: str = "", count: int = 1) -> str:
    """Echo arguments back so kwarg plumbing is assertable."""
    del ctx
    repeats = max(1, min(int(count), 10))
    return _json(
        {
            "marker": MARKER,
            "tool": "echo",
            "text": text,
            "count": repeats,
            "result": [text] * repeats,
        }
    )


def _boom(ctx: Any, *, message: str = "deliberate failure") -> str:
    """Raise so the provider's exception wrapping is assertable."""
    del ctx
    raise RuntimeError(f"OUROBOROS_TEST_TOOL_BOOM: {message}")


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="ping",
            schema=ToolSchema(
                name="ping",
                description="Return a fixed marker proving the test-tool skill is loaded and routable.",
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            handler=_ping,
            timeout_sec=10,
        ),
        ToolEntry(
            name="echo",
            schema=ToolSchema(
                name="echo",
                description="Echo the given text back, optionally repeated, as JSON.",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text to echo back."},
                        "count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "description": "How many copies to return.",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            ),
            handler=_echo,
            timeout_sec=10,
        ),
        ToolEntry(
            name="boom",
            schema=ToolSchema(
                name="boom",
                description="Raise a RuntimeError so error wrapping in the tool path can be tested.",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            handler=_boom,
            timeout_sec=10,
        ),
    ]


__all__ = ["MARKER", "get_tools"]
