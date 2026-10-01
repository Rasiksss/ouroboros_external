"""Tests for memory distillation as a durable task_type="memory_distillation" job."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ouroboros.storage.core_memory.memory import MemoryFactory, get_memory
from ouroboros.storage.settings.user_settings import reset_user_settings_for_tests
from tests.helpers.user_settings import StubUserSettingsStore


def _make_task(text="Investigate the deploy pipeline failure.", task_type="task", **extra):
    task = {"id": "src1", "type": task_type, "text": text, "chat_id": 123}
    task.update(extra)
    return task


@pytest.fixture(autouse=True)
def _reset_long_term_memory_cache() -> None:
    """Drop the cached service so this test binds to its own runtime DB."""
    get_memory.cache_clear()


# ---------------------------------------------------------------------------
# _enqueue_memory_distillation: eligibility + dedup
# ---------------------------------------------------------------------------


def test_enqueue_skipped_when_setting_disabled(tmp_path):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.queue import TaskQueue
    from supervisor.state import get_state_repository

    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_MEMORY_DISTILL": "false"}))

    env = MagicMock(drive_root=tmp_path)
    _enqueue_memory_distillation(env, _make_task(), "A" * 200)

    store = init_runtime_store(tmp_path)
    queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=get_state_repository(tmp_path))
    assert not queue.queue_has_task_type("memory_distillation")


def test_enqueue_skipped_for_short_response(tmp_path):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.queue import TaskQueue
    from supervisor.state import get_state_repository

    env = MagicMock(drive_root=tmp_path)
    _enqueue_memory_distillation(env, _make_task(), "too short")

    store = init_runtime_store(tmp_path)
    queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=get_state_repository(tmp_path))
    assert not queue.queue_has_task_type("memory_distillation")


def test_enqueue_skipped_for_internal_task_types(tmp_path):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.queue import TaskQueue
    from supervisor.state import get_state_repository

    env = MagicMock(drive_root=tmp_path)
    _enqueue_memory_distillation(env, _make_task(task_type="cron"), "A" * 200)

    store = init_runtime_store(tmp_path)
    queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=get_state_repository(tmp_path))
    assert not queue.queue_has_task_type("memory_distillation")


def test_enqueue_creates_memory_distillation_task(tmp_path):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.queue import TaskQueue
    from supervisor.state import get_state_repository

    env = MagicMock(drive_root=tmp_path)
    task = _make_task()
    response_text = "A" * 200
    _enqueue_memory_distillation(env, task, response_text)

    store = init_runtime_store(tmp_path)
    queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=get_state_repository(tmp_path))
    assert queue.queue_has_task_type("memory_distillation")

    records = [r for r in store.list_tasks() if r.task_type == "memory_distillation"]
    assert len(records) == 1
    payload = dict(records[0].input)
    assert payload["task_text"] == task["text"]
    assert payload["response_text"] == response_text
    assert payload["owner_id"] == "global"
    assert payload["chat_id"] == 123
    assert payload["source_task_id"] == "src1"


def test_enqueue_always_uses_global_memory_namespace(tmp_path):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store

    env = MagicMock(drive_root=tmp_path)
    _enqueue_memory_distillation(
        env,
        _make_task(owner_id="owner-42", user_id="user-42"),
        "A" * 200,
    )

    store = init_runtime_store(tmp_path)
    records = [r for r in store.list_tasks() if r.task_type == "memory_distillation"]
    assert len(records) == 1
    assert records[0].input["owner_id"] == "global"


def test_enqueue_truncates_distillation_payloads_with_omission_notes(tmp_path):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store

    env = MagicMock(drive_root=tmp_path)
    _enqueue_memory_distillation(
        env,
        _make_task(text="T" * 900),
        "R" * 1300,
    )

    store = init_runtime_store(tmp_path)
    records = [r for r in store.list_tasks() if r.task_type == "memory_distillation"]
    assert len(records) == 1
    payload = dict(records[0].input)
    assert "OMISSION NOTE" in payload["task_text"]
    assert "OMISSION NOTE" in payload["response_text"]


def test_enqueue_skips_failed_tool_trace(tmp_path, caplog):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store

    caplog.set_level("INFO")
    env = MagicMock(drive_root=tmp_path)
    _enqueue_memory_distillation(
        env,
        _make_task(),
        "A" * 200,
        llm_trace={"tool_calls": [{"tool": "data_read", "status": "error", "is_error": True}]},
    )

    store = init_runtime_store(tmp_path)
    records = [r for r in store.list_tasks() if r.task_type == "memory_distillation"]
    assert records == []
    assert "unsafe trace (tool failure without args fingerprint: tool=data_read) source_task_id=src1" in caplog.text


@pytest.mark.parametrize(
    ("case", "blocked"),
    [("matching", False), ("different_args", True), ("different_tool", True), ("missing_hash", True)],
)
def test_trace_only_accepts_matching_real_args_fingerprint(case, blocked, tmp_path):
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.agent.task_ledger import safe_args_hash
    from ouroboros.agent.task_pipeline import _trace_blocks_memory_distillation
    from tests._shared import collecting_event_sink, make_catalog

    failed_args = None if case == "missing_hash" else {"url": "/projects"}
    success_args = {} if case == "missing_hash" else failed_args
    if case == "different_args":
        success_args = {"url": "/health"}
    success_tool = "health_check" if case == "different_tool" else "data_read"
    results = [
        {
            "tool_call_id": "failed",
            "fn_name": "data_read",
            "tool_args": failed_args,
            "args_for_log": {"url": "[OMITTED]"},
            "result": "failed",
            "is_error": True,
        },
        {
            "tool_call_id": "success",
            "fn_name": success_tool,
            "tool_args": success_args,
            "args_for_log": {"url": "[OMITTED]"},
            "result": "authoritative state",
            "is_error": False,
        },
    ]
    trace = {"tool_calls": []}
    executor = build_tool_call_executor(
        catalog=make_catalog([]),
        drive_logs=tmp_path / "logs",
        events=collecting_event_sink(),
    )
    executor.process_results(results, messages=[], trace=trace)
    if case == "missing_hash":
        assert "args_hash" not in trace["tool_calls"][0]
    else:
        assert trace["tool_calls"][0]["args_hash"] == safe_args_hash(failed_args)
    assert bool(_trace_blocks_memory_distillation(trace)) is blocked


@pytest.mark.parametrize("warning_failed", [False, True])
def test_trace_does_not_clear_provenance_warning_after_success(warning_failed):
    from ouroboros.agent.task_pipeline import _trace_blocks_memory_distillation

    trace = {
        "tool_calls": [
            {
                "tool": "data_read",
                "args_hash": "same-call",
                "status": "error" if warning_failed else "ok",
                "is_error": warning_failed,
                "result": "DATA_PROVENANCE_WARNING: untrusted",
            },
            {
                "tool": "data_read",
                "args_hash": "same-call",
                "status": "ok",
                "result": "authoritative state",
            },
        ]
    }

    assert _trace_blocks_memory_distillation(trace)


def test_enqueue_skips_provenance_only_trace(tmp_path):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store

    env = MagicMock(drive_root=tmp_path)
    _enqueue_memory_distillation(
        env,
        _make_task(),
        "A" * 200,
        llm_trace={
            "tool_calls": [
                {
                    "tool": "data_read",
                    "status": "ok",
                    "result": "DATA_PROVENANCE_WARNING: agent-generated artifact, not authoritative memory.\n\nnote",
                }
            ]
        },
    )

    store = init_runtime_store(tmp_path)
    records = [r for r in store.list_tasks() if r.task_type == "memory_distillation"]
    assert records == []


def test_enqueue_includes_bounded_trace_summary(tmp_path):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store

    trace = {
        "tool_calls": [
            {"tool": "data_read", "status": "ok", "args": {"path": "refs/deploy.md"}},
            {"tool": "memory_list", "status": "ok"},
        ],
        "reasoning_notes": ["x" * 3000],
    }
    env = MagicMock(drive_root=tmp_path)
    _enqueue_memory_distillation(env, _make_task(), "A" * 200, llm_trace=trace)

    store = init_runtime_store(tmp_path)
    records = [r for r in store.list_tasks() if r.task_type == "memory_distillation"]
    assert len(records) == 1
    payload = dict(records[0].input)
    assert "data_read" in payload["trace_summary"]
    assert "memory_list" in payload["trace_summary"]
    assert "OMISSION NOTE" in payload["trace_summary"]


def test_enqueue_creates_separate_tasks_per_call(tmp_path):
    from ouroboros.agent.task_pipeline import _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store

    env = MagicMock(drive_root=tmp_path)
    _enqueue_memory_distillation(env, _make_task(), "A" * 200)
    _enqueue_memory_distillation(env, _make_task(), "B" * 200)

    store = init_runtime_store(tmp_path)
    records = [r for r in store.list_tasks() if r.task_type == "memory_distillation"]
    assert len(records) == 2
    assert records[0].input["response_text"] != records[1].input["response_text"]


def test_enqueue_throttles_at_max_pending(tmp_path, caplog):
    from ouroboros.agent.task_pipeline import _MAX_PENDING_DISTILLATIONS, _enqueue_memory_distillation
    from ouroboros.storage.runtime_store import init_runtime_store

    caplog.set_level("INFO")
    env = MagicMock(drive_root=tmp_path)
    for i in range(_MAX_PENDING_DISTILLATIONS + 3):
        _enqueue_memory_distillation(env, _make_task(), f"Response {i} " + "x" * 200)

    store = init_runtime_store(tmp_path)
    records = [r for r in store.list_tasks() if r.task_type == "memory_distillation"]
    assert len(records) == _MAX_PENDING_DISTILLATIONS
    assert "pending cap" in caplog.text


# ---------------------------------------------------------------------------
# run_memory_distillation_task: tool loop, usage events, type restriction
# ---------------------------------------------------------------------------


def _env_for(tmp_path):
    return MagicMock(drive_root=tmp_path, repo_dir=tmp_path)


def test_distillation_nothing_to_save_completes_with_no_usage(tmp_path):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    llm = MagicMock()
    llm.chat.return_value = (
        {"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"},
        {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "provider": "gigachat-b2bank",
            "resolved_model": "GigaChat-Pro",
        },
    )

    payload = {"task_text": "task", "response_text": "response", "owner_id": "global", "chat_id": 123}
    import queue as queue_mod

    event_q: queue_mod.Queue = queue_mod.Queue()

    result = run_memory_distillation_task(_env_for(tmp_path), llm, payload, event_queue=event_q, task_id="dt1")

    assert result.status == "completed"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 2
    assert result.rounds == 1
    assert result.saved_count == 0
    assert result.saved_names == []

    events = []
    while not event_q.empty():
        events.append(event_q.get_nowait())
    assert len(events) == 1
    assert events[0]["type"] == "llm_usage"
    assert events[0]["task_id"] == "dt1"
    assert events[0]["category"] == "memory_distillation"
    assert events[0]["source"] == "memory_distill"
    assert events[0]["provider"] == "gigachat-b2bank"
    assert events[0]["model"] == "GigaChat-Pro"


def test_distillation_saves_project_memory_via_tool_call(tmp_path):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    llm = MagicMock()
    first_msg = {
        "content": "",
        "finish_reason": "tool_calls",
        "tool_calls": [
            {
                "id": "call_1",
                "function": {
                    "name": "memory_save",
                    "arguments": (
                        '{"name": "deploy_incident", "memory_type": "project", '
                        '"description": "Deploy pipeline incident", "content": "Pipeline failed due to X."}'
                    ),
                },
            }
        ],
    }
    second_msg = {"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}
    llm.chat.side_effect = [
        (first_msg, {"prompt_tokens": 5, "completion_tokens": 5}),
        (second_msg, {"prompt_tokens": 5, "completion_tokens": 5}),
    ]

    payload = {
        "task_text": "task",
        "response_text": "response",
        "trace_summary": "data_read status=ok confirmed source",
        "owner_id": "global",
        "chat_id": 123,
    }
    result = run_memory_distillation_task(_env_for(tmp_path), llm, payload, task_id="dt2")

    assert result.status == "completed"
    assert result.rounds == 2
    assert result.saved_count == 1
    assert result.saved_names == ["deploy_incident"]

    store = MemoryFactory().get_memory()
    content, _, _ = store.read("deploy_incident")
    assert "Pipeline failed due to X." in content
    assert "source: distilled" in content


@pytest.mark.parametrize("invalid_arguments", ["{", "[]", "null", "42", '"text"'])
def test_distillation_reports_invalid_tool_arguments_with_valid_history(tmp_path, invalid_arguments):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    llm = MagicMock()
    llm.chat.side_effect = [
        (
            {
                "content": "",
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_bad",
                        "function": {"name": "memory_save", "arguments": invalid_arguments},
                    }
                ],
            },
            {},
        ),
        ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}),
    ]

    payload = {"task_text": "task", "response_text": "response", "owner_id": "global", "chat_id": 123}
    result = run_memory_distillation_task(_env_for(tmp_path), llm, payload, task_id="dt-invalid-args")

    assert result.status == "completed"
    assert result.rounds == 2
    second_round = llm.chat.call_args_list[1].kwargs["messages"]
    assistant_call = next(message for message in second_round if message.get("role") == "assistant")
    assert assistant_call["tool_calls"][0]["function"]["arguments"] == "{}"
    tool_result = next(message for message in second_round if message.get("role") == "tool")
    assert "TOOL_ARG_ERROR" in tool_result["content"]


@pytest.mark.parametrize(
    "invalid_call",
    [
        {"id": "call_bad"},
        {"id": "call_bad", "function": {"arguments": "{}"}},
    ],
    ids=["missing-function", "missing-name"],
)
def test_distillation_falls_back_from_invalid_tool_call_structure(tmp_path, monkeypatch, invalid_call):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    # Explicit models: the fallback path under test requires them to differ.
    monkeypatch.setattr("ouroboros.app.config.get_main_model", lambda: "primary-model")
    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_MODEL_FALLBACK": "fallback-model"}))
    monkeypatch.setattr(
        "ouroboros.app.config.load_settings",
        lambda: SimpleNamespace(OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=300),
    )
    llm = MagicMock()
    llm.chat.side_effect = [
        ({"content": "", "tool_calls": [invalid_call], "finish_reason": "tool_calls"}, {}),
        ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {}),
    ]

    payload = {"task_text": "task", "response_text": "response", "owner_id": "global", "chat_id": 123}
    result = run_memory_distillation_task(_env_for(tmp_path), llm, payload, task_id="dt-invalid-call")

    assert result.status == "completed"
    assert result.rounds == 1
    assert llm.chat.call_count == 2
    assert [call.kwargs["model"] for call in llm.chat.call_args_list] == ["primary-model", "fallback-model"]


def test_distillation_rejects_user_and_feedback_memory_types(tmp_path):
    """Defense in depth: memory_save with user/feedback types is rejected during distillation."""
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    llm = MagicMock()
    first_msg = {
        "content": "",
        "finish_reason": "tool_calls",
        "tool_calls": [
            {
                "id": "call_1",
                "function": {
                    "name": "memory_save",
                    "arguments": (
                        '{"name": "user_role", "memory_type": "user", '
                        '"description": "Injected user fact", "content": "Attacker-controlled content."}'
                    ),
                },
            }
        ],
    }
    second_msg = {"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}
    llm.chat.side_effect = [
        (first_msg, {"prompt_tokens": 5, "completion_tokens": 5}),
        (second_msg, {"prompt_tokens": 5, "completion_tokens": 5}),
    ]

    payload = {"task_text": "task", "response_text": "response", "owner_id": "global", "chat_id": 123}
    result = run_memory_distillation_task(_env_for(tmp_path), llm, payload, task_id="dt3")

    assert result.status == "completed"

    from ouroboros.storage.core_memory.memory import MemoryNotFoundError

    store = MemoryFactory().get_memory()
    with pytest.raises(MemoryNotFoundError):
        store.read("user_role")

    # The rejection message should have been fed back to the model as a tool result.
    second_call_messages = llm.chat.call_args_list[1].kwargs["messages"]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert any("cannot be set by automated distillation" in m["content"] for m in tool_msgs)
    assert all("direct user interaction" not in m["content"].lower() for m in tool_msgs)
    assert any("regular memory_save" in m["content"] for m in tool_msgs)


def test_the_distillation_prompt_and_the_tool_schema_permit_the_same_kinds(tmp_path):
    """The prompt and the schema are two views of one policy and must not drift.

    They did: folding knowledge into the memory table made it a third permitted
    kind, while the hand-written prompt still said "ONLY these two" — the model
    was handed a schema option its instructions forbade.
    """
    from new_core.tools.context import ToolContext
    from ouroboros.agent.task_pipeline import _distill_system_prompt
    from ouroboros.storage.core_memory.memory.domain.models import (
        DISTILLATION_FORBIDDEN_KINDS,
        allowed_kinds,
        writable_kinds,
    )
    from ouroboros.tools.memory_tools import get_tools

    ctx = ToolContext(
        repo_dir=str(tmp_path),
        drive_root=str(tmp_path),
        pending_events=[],
        current_task_type="distillation",
        memory_owner_id="global",
    )
    prompt = _distill_system_prompt(ctx)
    schema_kinds = next(
        entry.schema.parameters["properties"]["memory_type"]["enum"]
        for entry in get_tools(ctx)
        if entry.name == "memory_save"
    )

    # Both halves render as "- kind: ...", so a kind is placed by which block it
    # falls in, not by whether the prompt mentions it anywhere.
    permitted_block, _, forbidden_block = prompt.partition("Automated post-task extraction cannot write")

    rendered_forbidden = DISTILLATION_FORBIDDEN_KINDS.intersection(writable_kinds())

    assert set(schema_kinds) == set(allowed_kinds(ctx))
    for kind in schema_kinds:
        assert f"- {kind}:" in permitted_block, f"{kind} is offered by the schema but not described in the prompt"
        assert f"- {kind}:" not in forbidden_block, f"{kind} is offered by the schema but listed as forbidden"
    for kind in rendered_forbidden:
        assert f"- {kind}:" not in permitted_block
        assert f"- {kind}:" in forbidden_block, f"{kind} is withheld but the prompt never names it as forbidden"
    for kind in DISTILLATION_FORBIDDEN_KINDS - rendered_forbidden:
        assert f"- {kind}:" not in forbidden_block


def test_the_prompt_does_not_name_a_kind_no_one_can_write(tmp_path):
    """With self-edit off, identity is offered to no one, so it is not named as withheld."""
    from new_core.tools.context import ToolContext
    from ouroboros.agent.task_pipeline import _distill_system_prompt

    ctx = ToolContext(
        repo_dir=str(tmp_path),
        drive_root=str(tmp_path),
        pending_events=[],
        current_task_type="distillation",
        memory_owner_id="global",
    )
    _, _, forbidden_block = _distill_system_prompt(ctx).partition("Automated post-task extraction cannot write")

    assert "identity" not in forbidden_block
    assert "user" in forbidden_block
    assert "feedback" in forbidden_block


def test_the_prompt_does_not_call_identity_a_record_of_the_user(tmp_path, identity_self_edit_on):
    """Identity is the agent's own document; the shared sentence said otherwise."""
    from new_core.tools.context import ToolContext
    from ouroboros.agent.task_pipeline import _distill_system_prompt

    ctx = ToolContext(
        repo_dir=str(tmp_path),
        drive_root=str(tmp_path),
        pending_events=[],
        current_task_type="distillation",
        memory_owner_id="global",
    )
    identity_line = next(line for line in _distill_system_prompt(ctx).splitlines() if line.startswith("- identity:"))

    assert "the agent's own identity document" in identity_line
    assert "who the user is" not in identity_line


def test_the_distillation_prompt_covers_every_kind_distillation_may_write(tmp_path):
    """Guidance is looked up by kind, so a new distillable kind must arrive with its line.

    Narrowed by ``DISTILLATION_FORBIDDEN_KINDS`` because that is what the lookup
    actually iterates (``allowed_kinds``). ``identity`` is writable when the
    self-edit setting is on and has no guidance entry — by design: distillation
    may never write it.
    """
    from ouroboros.agent.task_pipeline import _DISTILL_KIND_GUIDANCE
    from ouroboros.storage.core_memory.memory.domain.models import DISTILLATION_FORBIDDEN_KINDS, writable_kinds

    assert set(writable_kinds()) - DISTILLATION_FORBIDDEN_KINDS <= set(_DISTILL_KIND_GUIDANCE)


def test_regular_memory_save_allows_user_and_feedback_types(tmp_path):

    from new_core.tools.context import ToolContext
    from ouroboros.tools.memory_tools import get_tools

    memory_save = next(tool for tool in get_tools() if tool.name == "memory_save")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, current_task_type="task")

    user_result = memory_save.handler(
        ctx,
        name="user_role",
        memory_type="user",
        description="User role",
        content="The user owns product decisions.",
    )
    feedback_result = memory_save.handler(
        ctx,
        name="feedback_style",
        memory_type="feedback",
        description="Feedback preference",
        content="Keep implementation changes narrow.",
    )

    assert "saved (user, overwrite)" in user_result
    assert "saved (feedback, overwrite)" in feedback_result

    store = MemoryFactory().get_memory()
    user_content, _, _ = store.read("user_role")
    feedback_content, _, _ = store.read("feedback_style")
    assert "The user owns product decisions." in user_content
    assert "Keep implementation changes narrow." in feedback_content


def test_memory_read_always_shows_recorded_timestamp(tmp_path):

    from new_core.tools.context import ToolContext
    from ouroboros.tools.memory_tools import get_tools

    store = MemoryFactory().get_memory()
    store.save("exchange_rates", "user", "FX rates", "1 USD = 50 RUB")

    memory_read = next(tool for tool in get_tools() if tool.name == "memory_read")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    result = memory_read.handler(ctx, name="exchange_rates")

    assert result.startswith("Recorded:")
    assert "exchange_rates" in result


def test_memory_save_uses_observed_at_for_new_topic(tmp_path):

    from new_core.tools.context import ToolContext
    from ouroboros.tools.memory_tools import get_tools

    memory_save = next(tool for tool in get_tools() if tool.name == "memory_save")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, current_task_type="task")
    observed = "2026-06-24T05:03:00+00:00"
    result = memory_save.handler(
        ctx,
        name="exchange_rates",
        memory_type="user",
        description="FX rates",
        content="- observed_at: 2026-06-24T05:03:00+00:00 — 1 USD = 50 RUB",
        observed_at=observed,
    )
    assert "saved (user, overwrite)" in result

    store = MemoryFactory().get_memory()
    topics = store.list_headers()
    assert topics[0].first_recorded_at == topics[0].updated_at or topics[0].first_recorded_at > 0
    content, first_recorded_at, _ = store.read("exchange_rates")
    assert "recorded_at: 2026-06-24T05:03:00+00:00" in content
    assert first_recorded_at > 0


def test_distillation_recorded_at_uses_source_task_timestamp(tmp_path):
    from ouroboros.agent.task_pipeline import _distillation_recorded_at
    from ouroboros.storage.runtime_models import TaskRecord
    from ouroboros.storage.runtime_store import init_runtime_store

    env = _env_for(tmp_path)
    store = init_runtime_store(tmp_path)
    store.upsert_task(
        TaskRecord(
            task_id="source-task-1",
            task_type="task",
            status="completed",
            created_at="2026-06-24T05:03:00+00:00",
            finished_at="2026-06-24T05:04:00+00:00",
        )
    )

    ts = _distillation_recorded_at(env, {"source_task_id": "source-task-1"})
    assert ts is not None
    from datetime import datetime, timezone

    assert datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().startswith("2026-06-24T05:04")


def test_distillation_memory_save_uses_source_task_timestamp(tmp_path):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task
    from ouroboros.storage.runtime_models import TaskRecord
    from ouroboros.storage.runtime_store import init_runtime_store

    env = _env_for(tmp_path)
    runtime = init_runtime_store(tmp_path)
    runtime.upsert_task(
        TaskRecord(
            task_id="source-task-2",
            task_type="task",
            status="completed",
            created_at="2026-06-24T05:03:00+00:00",
            finished_at="2026-06-24T05:04:00+00:00",
        )
    )

    llm = MagicMock()
    llm.chat.return_value = (
        {
            "content": "",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "function": {
                        "name": "memory_save",
                        "arguments": (
                            '{"name":"deploy_incident","memory_type":"project",'
                            '"description":"Deploy incident","content":"Rollback at 05:00 UTC"}'
                        ),
                    }
                }
            ],
        },
        {},
    )

    payload = {
        "task_text": "task",
        "response_text": "A" * 120,
        "owner_id": "global",
        "chat_id": 123,
        "source_task_id": "source-task-2",
    }
    result = run_memory_distillation_task(env, llm, payload, task_id="dt-recorded-at")
    assert result.status == "completed"

    store = MemoryFactory().get_memory()
    # The seeded identity row is always present; the distilled memory is the other one.
    topics = [h for h in store.list_headers() if h.kind != "identity"]
    assert len(topics) == 1
    from datetime import datetime, timezone

    recorded = datetime.fromtimestamp(topics[0].first_recorded_at, tz=timezone.utc)
    assert recorded.isoformat().startswith("2026-06-24T05:04")


def test_distillation_handles_exception(tmp_path, monkeypatch):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task
    from ouroboros.storage.settings.store import load_settings

    monkeypatch.setattr("ouroboros.app.config.get_main_model", lambda: "primary-model")
    # No-fallback instance: an override cannot express "empty", so the instance must.
    no_fallback = load_settings().model_copy(update={"OUROBOROS_MODEL_FALLBACK": ""})
    monkeypatch.setattr("ouroboros.storage.settings.user_settings.load_settings", lambda: no_fallback)
    reset_user_settings_for_tests(StubUserSettingsStore())
    monkeypatch.setattr(
        "ouroboros.app.config.load_settings",
        lambda: SimpleNamespace(OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=300),
    )
    llm = MagicMock()
    llm.chat.side_effect = RuntimeError("boom")

    payload = {"task_text": "task", "response_text": "response", "owner_id": "global", "chat_id": 123}
    result = run_memory_distillation_task(_env_for(tmp_path), llm, payload, task_id="dt4")

    # An opaque provider error is routed (and, with a fallback configured,
    # retried there); without a fallback it surfaces as a routing failure.
    assert result.status == "failed"
    assert "primary-model [provider_outage/RuntimeError]" in result.error
    assert llm.chat.call_count == 1


def test_distillation_uses_background_timeout_cap(tmp_path, monkeypatch):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    monkeypatch.setattr("ouroboros.app.config.get_main_model", lambda: "primary-model")
    monkeypatch.setattr(
        "ouroboros.app.config.load_settings",
        lambda: SimpleNamespace(OUROBOROS_MODEL_FALLBACK="", OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=120),
    )
    llm = MagicMock()
    llm.chat.return_value = ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {})

    payload = {"task_text": "task", "response_text": "response", "owner_id": "global", "chat_id": 123}
    result = run_memory_distillation_task(_env_for(tmp_path), llm, payload, task_id="dt-timeout")

    assert result.status == "completed"
    assert llm.chat.call_args.kwargs["timeout_sec"] == 30
    assert llm.chat.call_args.kwargs["read_timeout_sec"] == 120
    assert llm.chat.call_args.kwargs["deadline"] is not None


def test_distillation_falls_back_after_rejected_response_and_stays_on_fallback(tmp_path, monkeypatch):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    monkeypatch.setattr("ouroboros.app.config.get_main_model", lambda: "primary-model")
    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_MODEL_FALLBACK": "fallback-model"}))
    monkeypatch.setattr(
        "ouroboros.app.config.load_settings",
        lambda: SimpleNamespace(OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=30),
    )
    models, events = [], []

    def chat(**kwargs):
        models.append(kwargs["model"])
        if models == ["primary-model"]:
            return {"content": "partial", "tool_calls": [], "finish_reason": "length"}, {"prompt_tokens": 2}
        if models == ["primary-model", "fallback-model"]:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "save-1",
                        "function": {
                            "name": "memory_save",
                            "arguments": (
                                '{"name":"deploy_incident","memory_type":"project",'
                                '"description":"Deploy incident","content":"Rollback at 05:00 UTC"}'
                            ),
                        },
                    }
                ],
                "finish_reason": None,
            }, {"prompt_tokens": 3, "completion_tokens": 4}
        if models == ["primary-model", "fallback-model", "fallback-model"]:
            return {"content": "Saved the fact.", "tool_calls": [], "finish_reason": "stop"}, {
                "prompt_tokens": 5,
                "completion_tokens": 1,
            }
        raise AssertionError(f"unexpected model sequence: {models}")

    result = run_memory_distillation_task(
        _env_for(tmp_path),
        SimpleNamespace(chat=chat),
        {"task_text": "task", "response_text": "response", "owner_id": "global"},
        event_queue=SimpleNamespace(put_nowait=events.append),
    )

    assert result.status == "completed"
    assert result.rounds == 2
    assert result.saved_count == 1
    assert result.saved_names == ["deploy_incident"]
    # Sticky routing: after a successful fallback the next round starts there,
    # and the rejected primary's usage (2 tokens) is still recorded: 2 + 3 + 5.
    assert models == ["primary-model", "fallback-model", "fallback-model"]
    assert (result.prompt_tokens, result.completion_tokens) == (10, 5)
    assert [event["prompt_tokens"] for event in events] == [2, 3, 5]


def test_distillation_failure_preserves_completed_rounds_usage_and_saves(tmp_path, monkeypatch):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    monkeypatch.setattr("ouroboros.app.config.get_main_model", lambda: "primary-model")
    monkeypatch.setattr(
        "ouroboros.app.config.load_settings",
        lambda: SimpleNamespace(OUROBOROS_MODEL_FALLBACK="", OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=30),
    )
    llm = MagicMock()
    llm.chat.side_effect = [
        (
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "save-1",
                        "function": {
                            "name": "memory_save",
                            "arguments": (
                                '{"name":"deploy_incident","memory_type":"project",'
                                '"description":"Deploy incident","content":"Rollback at 05:00 UTC"}'
                            ),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"prompt_tokens": 3, "completion_tokens": 4},
        ),
        RuntimeError("late round failed"),
    ]

    result = run_memory_distillation_task(
        _env_for(tmp_path),
        llm,
        {"task_text": "task", "response_text": "response", "owner_id": "global"},
    )

    assert result.status == "failed"
    assert "primary-model [provider_outage/RuntimeError]" in result.error
    assert result.rounds == 2
    assert (result.prompt_tokens, result.completion_tokens) == (3, 4)
    assert result.saved_count == 1
    assert result.saved_names == ["deploy_incident"]


@pytest.mark.parametrize(
    ("expiry", "expected_error"), [("waiting_for_slot", "local_timeout"), ("during_preparation", "deadline expired")]
)
def test_distillation_deadline_before_any_response_fails_for_retry(tmp_path, monkeypatch, expiry, expected_error):
    """Regression: a deadline expiring before the first response analyzed
    nothing, so the job must stay retryable instead of reporting completed."""
    from ouroboros.agent.task_pipeline import run_memory_distillation_task
    from ouroboros.llm.client import LocalLLMDeadlineError

    monkeypatch.setattr("ouroboros.app.config.get_main_model", lambda: "primary-model")
    monkeypatch.setattr(
        "ouroboros.app.config.load_settings",
        lambda: SimpleNamespace(OUROBOROS_MODEL_FALLBACK="", OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=30),
    )
    llm = MagicMock()
    if expiry == "during_preparation":
        monkeypatch.setattr("ouroboros.agent.task_pipeline._DISTILL_TIMEOUT_SEC", -1.0)
        llm.chat.side_effect = AssertionError("no LLM call is expected after the deadline")
    else:
        llm.chat.side_effect = LocalLLMDeadlineError("LLM deadline expired while waiting for a concurrency slot")

    result = run_memory_distillation_task(
        _env_for(tmp_path),
        llm,
        {"task_text": "task", "response_text": "response", "owner_id": "global"},
    )

    assert result.status == "failed"
    assert expected_error in result.error
    assert result.saved_count == 0


def test_memory_wording_avoids_code_domain_terms():
    from ouroboros.agent.task_pipeline import _distill_system_prompt
    from ouroboros.storage.core_memory.memory.domain.render import memory_freshness_note
    from ouroboros.tools.memory_tools import get_tools

    memory_save = next(tool for tool in get_tools() if tool.name == "memory_save")
    combined = "\n".join(
        [
            # Rendered for the real caller: the wording rule covers every line the
            # distillation model actually sees, including the per-kind guidance.
            _distill_system_prompt(SimpleNamespace(current_task_type="distillation")),
            memory_save.schema.description,
            memory_freshness_note(0.0),
        ]
    ).lower()

    forbidden = [
        "code",
        "file",
        "files",
        "git",
        "implementation",
        "architecture",
        "file-line",
        "file:line",
        "project state",
        "task results",
        "direct user interaction",
        "direct conversation",
    ]
    for term in forbidden:
        assert term not in combined


def test_distillation_prompt_truncates_the_manifest_with_an_omission_note(tmp_path):
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    store = MemoryFactory().get_memory()
    for i in range(60):
        store.save(
            f"topic_{i:02d}",
            "project",
            f"Long description {i} " + ("x" * 150),
            f"content {i}",
        )

    llm = MagicMock()
    llm.chat.return_value = ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {})

    payload = {"task_text": "task", "response_text": "response", "owner_id": "global", "chat_id": 123}
    result = run_memory_distillation_task(_env_for(tmp_path), llm, payload, task_id="dt-index-trunc")

    assert result.status == "completed"
    user_prompt = llm.chat.call_args.kwargs["messages"][1]["content"]
    assert "Existing memories:" in user_prompt
    assert "OMISSION NOTE" in user_prompt


def test_distillation_sees_every_kind_of_existing_memory_not_just_headings(tmp_path):
    """Regression: the prompt sections' headings ate the budget before any entry.

    The prompt tells the model to check this list for duplicates; with ~750
    characters of heading and banner inside a 1 000-character cap it saw about
    seven entries and never a knowledge one, then re-created what it could not
    see under a new name.
    """
    from ouroboros.agent.task_pipeline import run_memory_distillation_task

    store = MemoryFactory().get_memory()
    for i in range(12):
        store.save(f"project_{i:02d}", "project", f"project desc {i}", f"content {i}")
    store.save("git-recipes", "knowledge", "Rebase before push", "knowledge body")
    store.save("patterns", "knowledge", "The pattern register", "pattern body")

    llm = MagicMock()
    llm.chat.return_value = ({"content": "NOTHING_TO_SAVE", "tool_calls": [], "finish_reason": "stop"}, {})

    payload = {"task_text": "task", "response_text": "response", "owner_id": "global", "chat_id": 123}
    run_memory_distillation_task(_env_for(tmp_path), llm, payload, task_id="dt-index-full")

    user_prompt = llm.chat.call_args.kwargs["messages"][1]["content"]
    manifest = user_prompt.split("Existing memories:", 1)[1]
    assert "OMISSION NOTE" not in manifest
    assert "[knowledge] git-recipes" in manifest
    # ``patterns`` is ordinary knowledge and identity holds a name too, so a
    # dedupe list has to show them both.
    assert "[knowledge] patterns" in manifest
    assert "[identity] identity" in manifest
    assert all(f"project_{i:02d}" in manifest for i in range(12))
    # None of the prompt's section furniture: no heading, no untrusted banner.
    assert "## Memory (" not in manifest
    assert "MEMORY_INDEX_DATA" not in manifest


# ---------------------------------------------------------------------------
# TaskExecutor routing + lifecycle wiring
# ---------------------------------------------------------------------------


def test_task_queue_claim_filters_memory_distillation(tmp_path):
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.queue import TaskQueue
    from supervisor.state import get_state_repository

    store = init_runtime_store(tmp_path)
    task_queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=get_state_repository(tmp_path))
    task_queue.enqueue_dict({"id": "mem1", "type": "memory_distillation", "task_text": "task", "response_text": "x"})
    task_queue.enqueue_dict({"id": "fg1", "type": "task", "text": "hello", "chat_id": 1})

    main = task_queue.claim_next(worker_id=0, exclude_task_types={"memory_distillation"})
    memory = task_queue.claim_next(worker_id=1, include_task_types={"memory_distillation"})

    assert main is not None
    assert main.task_id == "fg1"
    assert memory is not None
    assert memory.task_id == "mem1"


def test_executor_assigns_foreground_and_memory_slots_independently(tmp_path):
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.queue import TaskQueue
    from supervisor.state import get_state_repository
    from supervisor.workers import TaskExecutor, WorkerSlot

    class _PendingFuture:
        def done(self):
            return False

    class _FakePool:
        def __init__(self):
            self.submitted = []

        def submit(self, fn, record, slot=None):
            self.submitted.append(record.task_id)
            return _PendingFuture()

        def shutdown(self, wait=False, cancel_futures=True):
            return None

    store = init_runtime_store(tmp_path)
    task_queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=get_state_repository(tmp_path))
    task_queue.enqueue_dict({"id": "mem1", "type": "memory_distillation", "task_text": "task", "response_text": "x"})
    task_queue.enqueue_dict({"id": "fg1", "type": "task", "text": "hello", "chat_id": 1})

    executor = TaskExecutor(repo_dir=tmp_path, drive_root=tmp_path, task_queue=task_queue, store=store)
    fake_pool = _FakePool()
    executor._pool = fake_pool
    executor._slots = {0: WorkerSlot(worker_id=0), 1: WorkerSlot(worker_id=1)}

    executor.assign_next()

    assert executor._slots[0].busy_task_id == "fg1"
    assert executor._slots[1].busy_task_id == "mem1"
    assert fake_pool.submitted == ["fg1", "mem1"]


def test_memory_slot_is_created_lazily_only_when_memory_task_exists(tmp_path):
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.queue import TaskQueue
    from supervisor.state import get_state_repository
    from supervisor.workers import TaskExecutor

    class _PendingFuture:
        def done(self):
            return False

    class _FakePool:
        def __init__(self):
            self.submitted = []

        def submit(self, fn, record, slot=None):
            self.submitted.append(record.task_id)
            return _PendingFuture()

        def shutdown(self, wait=False, cancel_futures=True):
            return None

    store = init_runtime_store(tmp_path)
    task_queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=get_state_repository(tmp_path))
    executor = TaskExecutor(repo_dir=tmp_path, drive_root=tmp_path, task_queue=task_queue, store=store)
    executor.ensure_started()
    try:
        assert list(executor.worker_slots_dict()) == [0]

        executor._pool.shutdown(wait=False, cancel_futures=True)
        fake_pool = _FakePool()
        executor._pool = fake_pool
        task_queue.enqueue_dict(
            {"id": "mem1", "type": "memory_distillation", "task_text": "task", "response_text": "x"}
        )

        executor.assign_next()

        assert list(executor.worker_slots_dict()) == [0, 1]
        assert executor._slots[1].busy_task_id == "mem1"
        assert fake_pool.submitted == ["mem1"]
    finally:
        executor.shutdown()


def test_run_task_routes_memory_distillation_without_handle_task(tmp_path, monkeypatch):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.queue import TaskQueue
    from supervisor.state import get_state_repository
    from supervisor.workers import TaskExecutor, WorkerSlot

    store = init_runtime_store(tmp_path)
    task_queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=get_state_repository(tmp_path))
    executor = TaskExecutor(repo_dir=tmp_path, drive_root=tmp_path, task_queue=task_queue, store=store)

    record = store.create_task(
        TaskRequest(
            id="dt5",
            type="memory_distillation",
            task_text="task",
            response_text="A" * 100,
            owner_id="global",
        ),
        status="running",
    )

    from ouroboros.agent.task_pipeline import DistillResult

    fake_result = DistillResult(
        status="completed",
        rounds=1,
        prompt_tokens=3,
        completion_tokens=4,
        saved_count=1,
        saved_names=["deploy_incident"],
    )
    monkeypatch.setattr(
        "ouroboros.agent.task_pipeline.run_memory_distillation_task",
        lambda *a, **k: fake_result,
    )

    monkeypatch.setattr(
        executor,
        "get_agent",
        lambda: (_ for _ in ()).throw(AssertionError("memory lane must not use singleton agent")),
    )

    executor._run_task(record, WorkerSlot(worker_id=1).cancellation)

    events = []
    while not executor._event_q.empty():
        events.append(executor._event_q.get_nowait())
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "task_done"
    assert evt["task_id"] == "dt5"
    assert evt["task_type"] == "memory_distillation"
    assert evt["status"] == "completed"
    assert evt["total_rounds"] == 1
    assert evt["prompt_tokens"] == 3
    assert evt["completion_tokens"] == 4
    assert evt["result"] == "SAVED: deploy_incident"


def test_run_memory_distillation_success_completes_task_and_records_usage(tmp_path, monkeypatch):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.events import dispatch_event
    from supervisor.queue import TaskQueue
    from supervisor.runtime import build_supervisor_context
    from supervisor.runtime_context import SupervisorRuntime, reset_runtime_for_tests, set_runtime
    from supervisor.state import get_state_repository
    from supervisor.workers import TaskExecutor, WorkerSlot

    try:
        store = init_runtime_store(tmp_path)
        state_repo = get_state_repository(tmp_path)
        task_queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=state_repo)
        executor = TaskExecutor(repo_dir=tmp_path, drive_root=tmp_path, task_queue=task_queue, store=store)

        runtime = SupervisorRuntime(
            drive_root=tmp_path,
            repo_dir=tmp_path,
            state_repo=state_repo,
            store=store,
            task_queue=task_queue,
            executor=executor,
            orchestrator=MagicMock(),
            soft_timeout=10,
            hard_timeout=20,
            bridge=MagicMock(),
        )
        set_runtime(runtime)
        ctx = build_supervisor_context(
            settings={},
            runtime=runtime,
            bridge=MagicMock(),
            request_restart=lambda: None,
            safe_restart=lambda *a, **k: (True, "ok"),
        )

        record = store.create_task(
            TaskRequest(
                id="dt6",
                type="memory_distillation",
                task_text="task",
                response_text="A" * 100,
                owner_id="global",
            ),
            status="running",
        )

        from ouroboros.agent.task_pipeline import DistillResult

        fake_result = DistillResult(status="completed", rounds=1, prompt_tokens=7, completion_tokens=9)
        monkeypatch.setattr(
            "ouroboros.agent.task_pipeline.run_memory_distillation_task",
            lambda env, llm, payload, **k: (
                k["event_queue"].put(
                    {
                        "type": "llm_usage",
                        "ts": "2024-01-01T00:00:00Z",
                        "task_id": k["task_id"],
                        "model": "test-model",
                        "provider": "openrouter",
                        "category": "memory_distillation",
                        "source": "memory_distill",
                        "api_key_type": "",
                        "model_category": "other",
                        "prompt_tokens": 7,
                        "completion_tokens": 9,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "usage": {"prompt_tokens": 7, "completion_tokens": 9},
                    }
                ),
                fake_result,
            )[1],
        )
        monkeypatch.setattr(executor, "get_agent", lambda: MagicMock())

        executor._run_task(record, WorkerSlot(worker_id=1).cancellation)

        while not executor._event_q.empty():
            dispatch_event(executor._event_q.get_nowait(), ctx)

        updated = store.get_task("dt6")
        assert updated is not None
        assert updated.status == "completed"

        from sqlalchemy import text as sa_text

        with store._lock, store._connection() as conn:
            rows = conn.execute(
                sa_text("SELECT prompt_tokens, completion_tokens FROM llm_usage WHERE task_id=:t"),
                {"t": "dt6"},
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 7
        assert rows[0][1] == 9
    finally:
        reset_runtime_for_tests()


def test_run_memory_distillation_exception_marks_task_failed(tmp_path, monkeypatch):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.events import dispatch_event
    from supervisor.queue import TaskQueue
    from supervisor.runtime import build_supervisor_context
    from supervisor.runtime_context import SupervisorRuntime, reset_runtime_for_tests, set_runtime
    from supervisor.state import get_state_repository
    from supervisor.workers import TaskExecutor, WorkerSlot

    try:
        store = init_runtime_store(tmp_path)
        state_repo = get_state_repository(tmp_path)
        task_queue = TaskQueue(store, tmp_path, soft_timeout=10, hard_timeout=20, state_repo=state_repo)
        executor = TaskExecutor(repo_dir=tmp_path, drive_root=tmp_path, task_queue=task_queue, store=store)

        runtime = SupervisorRuntime(
            drive_root=tmp_path,
            repo_dir=tmp_path,
            state_repo=state_repo,
            store=store,
            task_queue=task_queue,
            executor=executor,
            orchestrator=MagicMock(),
            soft_timeout=10,
            hard_timeout=20,
            bridge=MagicMock(),
        )
        set_runtime(runtime)
        ctx = build_supervisor_context(
            settings={},
            runtime=runtime,
            bridge=MagicMock(),
            request_restart=lambda: None,
            safe_restart=lambda *a, **k: (True, "ok"),
        )

        record = store.create_task(
            TaskRequest(
                id="dt7",
                type="memory_distillation",
                task_text="task",
                response_text="A" * 100,
                owner_id="global",
            ),
            status="running",
        )

        def _boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr("ouroboros.agent.task_pipeline.run_memory_distillation_task", _boom)
        monkeypatch.setattr(executor, "get_agent", lambda: MagicMock())

        executor._run_task(record, WorkerSlot(worker_id=1).cancellation)

        events = []
        while not executor._event_q.empty():
            events.append(executor._event_q.get_nowait())
        assert len(events) == 1
        assert events[0]["type"] == "task_done"
        assert events[0]["status"] == "failed"
        assert "kaboom" in events[0]["result"]

        for evt in events:
            dispatch_event(evt, ctx)

        updated = store.get_task("dt7")
        assert updated is not None
        assert updated.status == "failed"
    finally:
        reset_runtime_for_tests()
