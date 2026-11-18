from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def _safe_json_arguments(raw: str | None) -> str:
    """Best-effort: always return a JSON-parseable string for wire/history use."""
    if not isinstance(raw, str) or not raw.strip():
        return "{}"
    try:
        json.loads(raw)
    except (TypeError, ValueError):
        return "{}"
    return raw


@dataclass
class ToolCallFunction:
    name: str
    arguments: str  # JSON string


@dataclass
class ToolCall:
    id: str
    function: ToolCallFunction
    type: str = "function"
    index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Provider/history wire shape — plain dict, never a raw dataclass."""
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": _safe_json_arguments(self.function.arguments),
            },
        }


@dataclass
class LLMMessage:
    role: str = "assistant"
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    reasoning_content: str | None = None
    tools_state_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The inverse of :meth:`from_dict`, arguments verbatim: ``ToolCall.to_dict`` swaps unparsable
        arguments for ``{}`` on the wire, and a call restored that way would run empty instead of failing.
        """
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    "index": tc.index,
                }
                for tc in self.tool_calls or []
            ],
            "finish_reason": self.finish_reason,
            "reasoning_content": self.reasoning_content,
            "tools_state_id": self.tools_state_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LLMMessage:
        tool_calls = None
        raw_tool_calls = d.get("tool_calls")
        if raw_tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    type=tc.get("type", "function"),
                    function=ToolCallFunction(
                        name=tc["function"]["name"],
                        arguments=tc["function"].get("arguments", "{}"),
                    ),
                    index=tc.get("index"),
                )
                for tc in raw_tool_calls
            ]
        return cls(
            role=d.get("role", "assistant"),
            content=str(d.get("content") or ""),
            tool_calls=tool_calls or None,
            finish_reason=d.get("finish_reason") or d.get("stop_reason"),
            reasoning_content=d.get("reasoning_content"),
            tools_state_id=d.get("tools_state_id"),
        )


@dataclass(frozen=True)
class LLMFailure:
    detail: str
    model_unavailable: bool = False
    user_message: str = ""
    context_overflow: bool = False


__all__ = ["LLMFailure", "LLMMessage", "ToolCall", "ToolCallFunction"]
