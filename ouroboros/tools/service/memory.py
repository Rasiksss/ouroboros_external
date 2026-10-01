"""Agent memory service tools."""

from __future__ import annotations

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.storage.chat_store import CHAT_HISTORY_MAX_COUNT
from ouroboros.storage.core_memory.scratchpad import get_scratchpad


def _chat_history(
    ctx: ToolContext,
    count: int = 100,
    offset: int = 0,
    date_from: str = "",
    date_to: str = "",
) -> str:
    from ouroboros.storage.chat_store import render_chat_history

    return render_chat_history(
        ctx.drive_root,
        count=count,
        offset=offset,
        date_from=date_from,
        date_to=date_to,
    )


def _update_scratchpad(ctx: ToolContext, content: str) -> str:
    if not content or not isinstance(content, str) or len(content.strip()) < 10:
        return (
            "⚠️ REJECTED: content is empty or too short "
            f"(got {type(content).__name__}, len={len(content) if isinstance(content, str) else 'N/A'}). "
            "Scratchpad must have meaningful content (10+ chars). "
            "This likely means the tool call was malformed - check your arguments."
        )
    block = get_scratchpad().append_block(content, source="task")
    return f"OK: scratchpad block appended ({len(content)} chars, ts={block.get('ts', '?')[:16]})"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "chat_history",
            ToolSchema(
                name="chat_history",
                description="Retrieve recent owner-visible chat context. Use date_from/date_to "
                "to constrain messages by creation time.",
                parameters={
                    "type": "object",
                    "properties": {
                        "count": {
                            "type": "integer",
                            "default": 100,
                            "minimum": 1,
                            "maximum": CHAT_HISTORY_MAX_COUNT,
                            "description": "Maximum messages to return from latest",
                        },
                        "offset": {"type": "integer", "default": 0, "description": "Skip N from end for pagination"},
                        "date_from": {
                            "type": "string",
                            "default": "",
                            "description": "Inclusive ISO date/datetime lower bound",
                        },
                        "date_to": {
                            "type": "string",
                            "default": "",
                            "description": "Exclusive ISO date/datetime upper bound",
                        },
                    },
                    "required": [],
                },
            ),
            _chat_history,
        ),
        ToolEntry(
            "update_scratchpad",
            ToolSchema(
                name="update_scratchpad",
                description="Append a timestamped block to working memory. Use for active "
                "tasks, decisions, and observations that should persist across sessions.",
                parameters={
                    "type": "object",
                    "properties": {"content": {"type": "string", "description": "Content for this scratchpad block"}},
                    "required": ["content"],
                },
            ),
            _update_scratchpad,
        ),
    ]
