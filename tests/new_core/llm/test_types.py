"""``LLMMessage`` survives a round trip through the parked run's snapshot."""

from __future__ import annotations

from new_core.llm.types import LLMMessage, ToolCall, ToolCallFunction


def _response() -> LLMMessage:
    return LLMMessage(
        content="about to write",
        tool_calls=[
            ToolCall(
                id="call_1",
                function=ToolCallFunction(name="file_write", arguments='{"path": "a.txt", '),
                index=0,
            )
        ],
        finish_reason="tool_calls",
        reasoning_content="considering",
        tools_state_id="state-1",
    )


def test_to_dict_keeps_unparsable_arguments_verbatim() -> None:
    payload = _response().to_dict()

    assert payload["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt", '


def test_from_dict_reads_back_what_to_dict_wrote() -> None:
    assert LLMMessage.from_dict(_response().to_dict()) == _response()
