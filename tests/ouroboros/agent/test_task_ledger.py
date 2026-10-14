import json

import ouroboros.agent.task_graph as task_graph_mod
from new_core.llm.types import LLMMessage, ToolCall, ToolCallFunction
from tests.helpers.factories import permissive_safety
from tests.helpers.loop_harness import drive_loop
from tests.helpers.shared import make_tool_env, static_tool_provider


def test_task_ledger_aggregates_repeated_failures_and_stays_bounded():
    from ouroboros.agent.task_ledger import TaskLedger

    ledger = TaskLedger(max_chars=240)
    for index in range(12):
        ledger.record_tool_result(
            tool_name="skill_exec",
            args_hash="abc123",
            status="error",
            failure_class="no skills repo configured",
            short_outcome=f"failure {index}",
        )

    rendered = ledger.render()

    assert len(rendered) <= 240
    assert "skill_exec" in rendered
    assert "no skills repo configured" in rendered
    assert "x12" in rendered
    assert "failure 11" not in rendered


def test_task_ledger_records_controlled_state_not_raw_outcome():
    from ouroboros.agent.task_ledger import TaskLedger

    ledger = TaskLedger(max_chars=500)
    ledger.record_tool_result(
        tool_name="run_shell",
        args_hash="hash123",
        status="non_zero_exit",
        failure_class="non_zero_exit",
        short_outcome="raw stdout secret_token=sk-abcdef command output with private user text",
    )

    rendered = ledger.render()

    assert "run_shell" in rendered
    assert "non_zero_exit" in rendered
    assert "hash123" in rendered
    assert "secret_token" not in rendered
    assert "private user text" not in rendered


def test_repeat_guard_blocks_third_same_failed_call_and_resets_on_user_input():
    from ouroboros.agent.task_ledger import RepeatToolGuard

    guard = RepeatToolGuard(block_after=3)
    assert guard.record_failure("skill_exec", {"skill": "demo"}, "no skills repo configured") == ""

    guard.record_failure("send_file", {"file_path": "report.txt"}, "SEND_FILE_DELIVERY_UNKNOWN")
    assert "TOOL_REPEAT_BLOCKED" in guard.preflight("send_file", {"file_path": "report.txt"})
    warning = guard.record_failure("skill_exec", {"skill": "demo"}, "no skills repo configured")
    assert "REPEAT_TOOL_WARNING" in warning
    blocked = guard.record_failure("skill_exec", {"skill": "demo"}, "no skills repo configured")
    assert "TOOL_REPEAT_BLOCKED" in blocked

    guard.mark_user_input()

    assert guard.record_failure("skill_exec", {"skill": "demo"}, "no skills repo configured") == ""


def test_repeat_guard_allows_different_args():
    from ouroboros.agent.task_ledger import RepeatToolGuard

    guard = RepeatToolGuard(block_after=3)
    guard.record_failure("skill_exec", {"skill": "demo-a"}, "no skills repo configured")
    guard.record_failure("skill_exec", {"skill": "demo-a"}, "no skills repo configured")

    assert guard.record_failure("skill_exec", {"skill": "demo-b"}, "no skills repo configured") == ""


def test_disabled_tool_repeats_ignore_args_without_losing_trace_fingerprint_or_executing(tmp_path, monkeypatch):
    from ouroboros.agent import loop_tool_execution
    from ouroboros.agent.task_ledger import RepeatToolGuard, safe_args_hash
    from tests.helpers.shared import run_tool_calls

    calls = []
    catalog, ctx = make_tool_env(tmp_path, tmp_path)
    guard = RepeatToolGuard(block_after=3)

    async def _unexpected_execute(*args, **kwargs):
        calls.append(("handler", args, kwargs))
        return "unexpected"

    monkeypatch.setattr(loop_tool_execution, "execute_tool", _unexpected_execute)
    monkeypatch.setattr(
        loop_tool_execution,
        "authorize_tool",
        lambda _name, args, _ctx: calls.append(("policy", args)),
    )
    messages: list[dict] = []
    trace = {"tool_calls": []}
    arguments = [{"expression": f"{index} + {index}"} for index in range(4)]

    for index, args in enumerate(arguments):
        run_tool_calls(
            [
                ToolCall(
                    id=f"call-{index}",
                    function=ToolCallFunction(name="calculator", arguments=json.dumps(args)),
                )
            ],
            catalog,
            tmp_path / "logs",
            "task-1",
            messages,
            trace,
            lambda _text: None,
            ctx=ctx,
            active_tool_names=frozenset(),
            repeat_guard=guard,
        )

    assert calls == []
    assert "REPEAT_TOOL_WARNING" in messages[1]["content"]
    assert "TOOL_REPEAT_BLOCKED" in messages[2]["content"]
    assert "TOOL_REPEAT_BLOCKED" in messages[3]["content"]
    assert [call["args_hash"] for call in trace["tool_calls"]] == [safe_args_hash(args) for args in arguments]


def test_repeat_guard_preflight_blocks_third_failed_tool_before_handler(tmp_path):
    from new_core.tools.entry import ToolEntry
    from ouroboros.agent.task_ledger import RepeatToolGuard
    from tests.helpers.shared import run_tool_calls

    calls = {"count": 0}
    catalog, ctx = make_tool_env(tmp_path, tmp_path)
    guard = RepeatToolGuard(block_after=3)

    def _handler(ctx, value: str):
        del ctx, value
        calls["count"] += 1
        return "⚠️ TOOL_ERROR: failed"

    catalog.add(
        static_tool_provider(
            ToolEntry(
                "repeat_probe",
                {"name": "repeat_probe", "description": "test", "parameters": {"type": "object", "properties": {}}},
                _handler,
            )
        )
    )
    messages: list[dict] = []
    trace = {"tool_calls": []}

    for index in range(4):
        run_tool_calls(
            [
                ToolCall(
                    id=f"call-{index}", function=ToolCallFunction(name="repeat_probe", arguments='{"value":"same"}')
                )
            ],
            catalog,
            tmp_path / "logs",
            "task-1",
            messages,
            trace,
            lambda _: None,
            ctx=ctx,
            active_tool_names=None,
            repeat_guard=guard,
        )

    assert calls["count"] == 2
    assert messages[-2]["tool_call_id"] == "call-2"
    assert "TOOL_REPEAT_BLOCKED" in messages[-2]["content"]
    assert messages[-1]["tool_call_id"] == "call-3"
    assert "TOOL_REPEAT_BLOCKED" in messages[-1]["content"]
    assert "call was not executed" in messages[-1]["content"]


def test_repeat_guard_preflight_resets_after_success(tmp_path):
    from new_core.tools.entry import ToolEntry
    from ouroboros.agent.task_ledger import RepeatToolGuard
    from tests.helpers.shared import run_tool_calls

    calls = {"count": 0}
    catalog, ctx = make_tool_env(tmp_path, tmp_path)
    guard = RepeatToolGuard(block_after=3)

    def _handler(ctx, value: str):
        del ctx, value
        calls["count"] += 1
        if calls["count"] in {1, 3, 4}:
            return "⚠️ TOOL_ERROR: failed"
        return "ok"

    catalog.add(
        static_tool_provider(
            ToolEntry(
                "repeat_reset_probe",
                {
                    "name": "repeat_reset_probe",
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
                _handler,
            )
        )
    )
    messages: list[dict] = []
    trace = {"tool_calls": []}

    for index in range(5):
        run_tool_calls(
            [
                ToolCall(
                    id=f"call-{index}",
                    function=ToolCallFunction(name="repeat_reset_probe", arguments='{"value":"same"}'),
                )
            ],
            catalog,
            tmp_path / "logs",
            "task-1",
            messages,
            trace,
            lambda _: None,
            ctx=ctx,
            active_tool_names=None,
            repeat_guard=guard,
        )

    assert calls["count"] == 4
    assert "TOOL_REPEAT_BLOCKED" in messages[-1]["content"]


def test_run_llm_loop_injects_tool_outcome_ledger_before_next_request(tmp_path, monkeypatch):
    import queue

    import ouroboros.agent.loop_adapters as loop_adapters_mod
    from new_core.tools.entry import ToolEntry
    from tests.helpers.factories import make_mock_llm

    catalog, ctx = make_tool_env(tmp_path, tmp_path)

    def _handler(ctx, value: str):
        del ctx, value
        return "⚠️ TOOL_ERROR: failed raw secret_token=sk-abcdef"

    catalog.add(
        static_tool_provider(
            ToolEntry(
                "ledger_probe",
                {"name": "ledger_probe", "description": "test", "parameters": {"type": "object", "properties": {}}},
                _handler,
            )
        )
    )

    requests: list[list[dict]] = []

    def _fake_call(_llm, messages, *_args, **_kwargs):
        requests.append([dict(message) for message in messages])
        if len(requests) == 1:
            return LLMMessage(
                content="",
                tool_calls=[
                    ToolCall(id="call-1", function=ToolCallFunction(name="ledger_probe", arguments='{"value":"same"}'))
                ],
            )
        return LLMMessage(content="done", tool_calls=None)

    monkeypatch.setattr(
        task_graph_mod,
        "bind_tool_surface",
        lambda _providers, _ctx, _messages: (
            [
                {
                    "type": "function",
                    "function": {
                        "name": "ledger_probe",
                        "description": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            {"ledger_probe"},
        ),
    )
    monkeypatch.setattr(loop_adapters_mod, "call_llm_with_retry", _fake_call)
    monkeypatch.setattr(loop_adapters_mod.TranscriptCompactorAdapter, "seal", lambda self, _messages: None)
    monkeypatch.setattr("ouroboros.policy.safety.check_safety", permissive_safety)

    fake_llm = make_mock_llm()

    drive_loop(
        messages=[{"role": "user", "content": "hi"}],
        catalog=catalog,
        tool_ctx=ctx,
        llm=fake_llm,
        drive_logs=tmp_path / "logs",
        emit_progress=lambda _text: None,
        incoming_messages=queue.Queue(),
        task_id="task-1",
        drive_root=tmp_path,
    )

    second_request = requests[1]
    ledger_notes = [
        message
        for message in second_request
        if "[SYSTEM RUNTIME NOTE: TOOL OUTCOME LEDGER]" in str(message.get("content") or "")
    ]
    assert len(ledger_notes) == 1
    assert ledger_notes[0]["role"] == "user"
    assert "Tool outcome ledger" in ledger_notes[0]["content"]
    assert "ledger_probe" in ledger_notes[0]["content"]
    assert "secret_token" not in ledger_notes[0]["content"]


def test_run_llm_loop_updates_single_tool_outcome_ledger_note(tmp_path, monkeypatch):
    import queue

    import ouroboros.agent.loop_adapters as loop_adapters_mod
    from new_core.tools.entry import ToolEntry
    from tests.helpers.factories import make_mock_llm

    catalog, ctx = make_tool_env(tmp_path, tmp_path)

    def _handler(ctx, value: str):
        del ctx, value
        return "⚠️ TOOL_ERROR: failed"

    catalog.add(
        static_tool_provider(
            ToolEntry(
                "ledger_repeat_probe",
                {
                    "name": "ledger_repeat_probe",
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
                _handler,
            )
        )
    )

    requests: list[list[dict]] = []

    def _fake_call(_llm, messages, *_args, **_kwargs):
        requests.append([dict(message) for message in messages])
        if len(requests) < 3:
            return LLMMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call-{len(requests)}",
                        function=ToolCallFunction(name="ledger_repeat_probe", arguments='{"value":"same"}'),
                    )
                ],
            )
        return LLMMessage(content="done", tool_calls=None)

    monkeypatch.setattr(
        task_graph_mod,
        "bind_tool_surface",
        lambda _providers, _ctx, _messages: (
            [
                {
                    "type": "function",
                    "function": {
                        "name": "ledger_repeat_probe",
                        "description": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            {"ledger_repeat_probe"},
        ),
    )
    monkeypatch.setattr(loop_adapters_mod, "call_llm_with_retry", _fake_call)
    monkeypatch.setattr(loop_adapters_mod.TranscriptCompactorAdapter, "seal", lambda self, _messages: None)
    monkeypatch.setattr("ouroboros.policy.safety.check_safety", permissive_safety)

    fake_llm = make_mock_llm()

    drive_loop(
        messages=[{"role": "user", "content": "hi"}],
        catalog=catalog,
        tool_ctx=ctx,
        llm=fake_llm,
        drive_logs=tmp_path / "logs",
        emit_progress=lambda _text: None,
        incoming_messages=queue.Queue(),
        task_id="task-1",
        drive_root=tmp_path,
    )

    third_request = requests[2]
    ledger_notes = [
        message
        for message in third_request
        if "[SYSTEM RUNTIME NOTE: TOOL OUTCOME LEDGER]" in str(message.get("content") or "")
    ]
    assert len(ledger_notes) == 1
    assert ledger_notes[0]["role"] == "user"
    assert ledger_notes[0]["content"].count("[SYSTEM RUNTIME NOTE: TOOL OUTCOME LEDGER]") == 1
