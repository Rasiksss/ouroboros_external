from __future__ import annotations

import queue
from unittest.mock import MagicMock, patch

from new_core.llm.types import LLMMessage, ToolCall, ToolCallFunction
from new_core.tools.schema import ToolSchema
from tests.helpers.factories import make_loop_ctx, make_mock_llm
from tests.helpers.loop_harness import drive_loop


def test_language_guard_detects_unexpected_foreign_script():
    from ouroboros.agent.response_language_guard import find_language_violation

    violation = find_language_violation(
        "Грань有时候 тонкая.",
        [{"role": "user", "content": "Объясни кратко"}],
    )

    assert violation is not None
    assert violation.spans == ["有时候"]
    assert violation.scripts == ["CJK"]


def test_language_guard_allows_script_from_user_or_tool_evidence():
    from ouroboros.agent.response_language_guard import find_language_violation

    messages = [
        {"role": "user", "content": "Что означает 有时候?"},
        {"role": "tool", "content": "Перевод: иногда"},
    ]

    assert find_language_violation("有时候 означает иногда.", messages) is None


def test_language_guard_allows_explicit_language_request_without_sample_text():
    from ouroboros.agent.response_language_guard import find_language_violation

    messages = [{"role": "user", "content": "Напиши слово привет по-китайски"}]

    assert find_language_violation("你好", messages) is None


def test_language_repair_prompt_preserves_original_response():
    from ouroboros.agent.response_language_guard import build_language_repair_messages

    messages = build_language_repair_messages("Грань有时候 тонкая.")

    assert messages[-1]["role"] == "user"
    assert "Грань有时候 тонкая." in messages[-1]["content"]
    assert "не меняй смысл" in messages[-1]["content"].lower()


def _loop_context(messages):
    return make_loop_ctx(messages)


def _fake_ctx(messages):
    return make_loop_ctx(messages)


def test_run_loop_repairs_dirty_final_answer_once_without_tools(tmp_path):
    import ouroboros.agent.loop_adapters as loop_adapters_mod
    import ouroboros.agent.task_graph as task_graph_mod

    messages = [{"role": "user", "content": "Ответь кратко"}]
    tools = MagicMock()
    tools._ctx = _loop_context(messages)
    llm = make_mock_llm()
    calls = MagicMock(
        side_effect=[
            LLMMessage(content="Ответ有时候 содержит сбой.", tool_calls=None),
            LLMMessage(content="Ответ содержит сбой.", tool_calls=None),
        ]
    )

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", calls),
        patch.object(loop_adapters_mod.OwnerInboxAdapter, "drain", return_value=False),
        patch.object(loop_adapters_mod.EventSinkAdapter, "maybe_checkpoint", return_value=False),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "seal", return_value=None),
        patch.object(task_graph_mod, "bind_tool_surface", return_value=([], set())),
    ):
        text, _usage, _trace = drive_loop(
            messages=messages,
            tool_ctx=tools._ctx,
            llm=llm,
            drive_logs=tmp_path / "logs",
            emit_progress=lambda _text: None,
            incoming_messages=queue.Queue(),
            task_id="task-1",
        )

    assert text == "Ответ содержит сбой."
    assert calls.call_count == 2
    assert calls.call_args_list[1].args[3] is None


def test_run_loop_keeps_original_when_repair_fails_validation(tmp_path):
    import ouroboros.agent.loop_adapters as loop_adapters_mod
    import ouroboros.agent.task_graph as task_graph_mod

    messages = [{"role": "user", "content": "Ответь кратко"}]
    tools = MagicMock()
    tools._ctx = _loop_context(messages)
    llm = make_mock_llm()
    calls = MagicMock(
        side_effect=[
            LLMMessage(content="Ответ有时候 содержит сбой.", tool_calls=None),
            LLMMessage(content="Другой有时候 сбой.", tool_calls=None),
        ]
    )

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", calls),
        patch.object(loop_adapters_mod.OwnerInboxAdapter, "drain", return_value=False),
        patch.object(loop_adapters_mod.EventSinkAdapter, "maybe_checkpoint", return_value=False),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "seal", return_value=None),
        patch.object(task_graph_mod, "bind_tool_surface", return_value=([], set())),
    ):
        text, _usage, _trace = drive_loop(
            messages=messages,
            tool_ctx=tools._ctx,
            llm=llm,
            drive_logs=tmp_path / "logs",
            emit_progress=lambda _text: None,
            incoming_messages=queue.Queue(),
            task_id="task-1",
        )

    assert text == "Ответ有时候 содержит сбой."


def test_language_repair_cannot_repeat_tool_workflow(tmp_path):
    import ouroboros.agent.loop_adapters as loop_adapters_mod
    import ouroboros.agent.task_graph as task_graph_mod

    messages = [{"role": "user", "content": "Уточни дату"}]
    tools = MagicMock()
    tools._ctx = _loop_context(messages)
    llm = make_mock_llm()
    tool_calls = [ToolCall(id="call-1", function=ToolCallFunction(name="calendar_resolve", arguments="{}"))]
    calls = MagicMock(
        side_effect=[
            LLMMessage(content="", tool_calls=tool_calls),
            LLMMessage(content="Дата уточнена有时候.", tool_calls=None),
            LLMMessage(content="Дата уточнена.", tool_calls=None),
        ]
    )
    handled: list[str] = []

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", calls),
        patch.object(loop_adapters_mod.OwnerInboxAdapter, "drain", return_value=False),
        patch.object(loop_adapters_mod.EventSinkAdapter, "maybe_checkpoint", return_value=False),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "seal", return_value=None),
        patch.object(
            task_graph_mod,
            "bind_tool_surface",
            return_value=(
                [ToolSchema(name="calendar_resolve", description="test", parameters={})],
                {"calendar_resolve"},
            ),
        ),
        patch.object(
            loop_adapters_mod.ToolRunnerAdapter,
            "handle_tool_calls",
            side_effect=lambda *args, **kwargs: handled.append("called") or [],
        ),
    ):
        text, _usage, _trace = drive_loop(
            messages=messages,
            tool_ctx=tools._ctx,
            llm=llm,
            drive_logs=tmp_path / "logs",
            emit_progress=lambda _text: None,
            incoming_messages=queue.Queue(),
            task_id="task-1",
        )

    assert text == "Дата уточнена."
    assert calls.call_count == 3
    assert calls.call_args_list[2].args[3] is None
    assert handled == ["called"]


def test_language_guard_does_not_rewrite_progress_text(tmp_path):
    import ouroboros.agent.loop_adapters as loop_adapters_mod
    import ouroboros.agent.task_graph as task_graph_mod

    messages = [{"role": "user", "content": "Уточни дату"}]
    tools = MagicMock()
    tools._ctx = _loop_context(messages)
    llm = make_mock_llm()
    emitted: list[str] = []
    tool_calls = [ToolCall(id="call-1", function=ToolCallFunction(name="calendar_resolve", arguments="{}"))]

    with (
        patch.object(
            loop_adapters_mod,
            "call_llm_with_retry",
            return_value=LLMMessage(content="Сейчас проверю有时候.", tool_calls=tool_calls),
        ),
        patch.object(loop_adapters_mod.OwnerInboxAdapter, "drain", return_value=False),
        patch.object(loop_adapters_mod.EventSinkAdapter, "maybe_checkpoint", return_value=False),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "seal", return_value=None),
        patch.object(
            task_graph_mod,
            "bind_tool_surface",
            return_value=(
                [ToolSchema(name="calendar_resolve", description="test", parameters={})],
                {"calendar_resolve"},
            ),
        ),
        patch.object(loop_adapters_mod.ToolRunnerAdapter, "handle_tool_calls", side_effect=KeyboardInterrupt),
    ):
        try:
            drive_loop(
                messages=messages,
                tool_ctx=tools._ctx,
                llm=llm,
                drive_logs=tmp_path / "logs",
                emit_progress=emitted.append,
                incoming_messages=queue.Queue(),
                task_id="task-1",
            )
        except KeyboardInterrupt:
            pass

    assert emitted == ["Сейчас проверю有时候."]


def test_language_guard_allows_foreign_text_from_user_evidence() -> None:
    from ouroboros.agent.response_language_guard import find_language_violation

    violation = find_language_violation(
        "Пользователь написал: 有时候.",
        [{"role": "user", "content": "Фраза для проверки: 有时候"}],
    )

    assert violation is None


def test_language_guard_detects_new_cjk_span() -> None:
    from ouroboros.agent.response_language_guard import find_language_violation

    violation = find_language_violation(
        "Не грубый — честный. Но грань有时候 тонкая.",
        [{"role": "user", "content": "а ты грубый"}],
    )

    assert violation is not None
    assert "有时候" in violation.spans


def test_build_language_repair_messages_preserve_original_for_rewrite() -> None:
    from ouroboros.agent.response_language_guard import build_language_repair_messages

    repair_messages = build_language_repair_messages(
        "Не грубый, но грань有时候 тонкая.",
    )

    assert repair_messages[-1]["role"] == "user"
    assert "Не грубый, но грань有时候 тонкая." in repair_messages[-1]["content"]
    assert "не меняй смысл" in repair_messages[-1]["content"].lower()


def test_language_guard_keeps_foreign_text_from_evidence() -> None:
    from ouroboros.agent.response_language_guard import find_language_violation

    assert (
        find_language_violation(
            "Пользователь написал: 有时候.",
            [{"role": "user", "content": "Фраза для проверки: 有时候"}],
        )
        is None
    )


def test_run_loop_language_guard_cannot_repeat_tool_workflow(tmp_path) -> None:
    import ouroboros.agent.loop_adapters as loop_adapters_mod
    import ouroboros.agent.task_graph as task_graph_mod

    messages = [{"role": "user", "content": "создай поручение"}]
    fake_tools = MagicMock()
    fake_tools._ctx = _fake_ctx(messages)
    fake_llm = make_mock_llm()
    tool_calls = [
        ToolCall(id="call-1", function=ToolCallFunction(name="send_assignment", arguments="{}")),
    ]
    calls = [
        LLMMessage(content="", tool_calls=tool_calls),
        LLMMessage(content="Поручение отправлено有时候."),
        LLMMessage(content="Поручение отправлено."),
    ]
    handled: list[str] = []

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", side_effect=calls) as call_mock,
        patch.object(loop_adapters_mod.OwnerInboxAdapter, "drain", return_value=False),
        patch.object(loop_adapters_mod.EventSinkAdapter, "maybe_checkpoint", return_value=False),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "seal", return_value=None),
        patch.object(
            task_graph_mod,
            "bind_tool_surface",
            return_value=(
                [ToolSchema(name="send_assignment", description="test", parameters={})],
                {"send_assignment"},
            ),
        ),
        patch.object(
            loop_adapters_mod.ToolRunnerAdapter,
            "handle_tool_calls",
            side_effect=lambda *args, **kwargs: handled.append("called") or [],
        ),
    ):
        text, _usage, _trace = drive_loop(
            messages=messages,
            tool_ctx=fake_tools._ctx,
            llm=fake_llm,
            drive_logs=tmp_path / "logs",
            emit_progress=lambda _text: None,
            incoming_messages=queue.Queue(),
            task_id="task-1",
        )

    assert text == "Поручение отправлено."
    assert call_mock.call_count == 3
    assert call_mock.call_args_list[2].args[3] is None
    assert handled == ["called"]


def test_run_loop_does_not_repair_or_clean_progress_text(tmp_path) -> None:
    import ouroboros.agent.loop_adapters as loop_adapters_mod
    import ouroboros.agent.task_graph as task_graph_mod

    messages = [{"role": "user", "content": "используй инструмент"}]
    fake_tools = MagicMock()
    fake_tools._ctx = _fake_ctx(messages)
    fake_llm = make_mock_llm()
    tool_calls = [
        ToolCall(id="call-1", function=ToolCallFunction(name="calendar_resolve", arguments="{}")),
    ]
    emitted: list[str] = []

    with (
        patch.object(
            loop_adapters_mod,
            "call_llm_with_retry",
            return_value=LLMMessage(content="Сейчас проверю有时候.", tool_calls=tool_calls),
        ),
        patch.object(loop_adapters_mod.OwnerInboxAdapter, "drain", return_value=False),
        patch.object(loop_adapters_mod.EventSinkAdapter, "maybe_checkpoint", return_value=False),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "seal", return_value=None),
        patch.object(
            task_graph_mod,
            "bind_tool_surface",
            return_value=(
                [ToolSchema(name="calendar_resolve", description="test", parameters={})],
                {"calendar_resolve"},
            ),
        ),
        patch.object(loop_adapters_mod.ToolRunnerAdapter, "handle_tool_calls", side_effect=KeyboardInterrupt),
    ):
        try:
            drive_loop(
                messages=messages,
                tool_ctx=fake_tools._ctx,
                llm=fake_llm,
                drive_logs=tmp_path / "logs",
                emit_progress=emitted.append,
                incoming_messages=queue.Queue(),
                task_id="task-1",
            )
        except KeyboardInterrupt:
            pass

    assert emitted == ["Сейчас проверю有时候."]
