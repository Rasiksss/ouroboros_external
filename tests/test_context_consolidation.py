from __future__ import annotations

import json
from threading import Event
from types import SimpleNamespace

import pytest

from ouroboros.agent import context, context_consolidation
from ouroboros.storage.runtime_models import ContextSummary, ConversationMessage
from ouroboros.storage.settings.user_settings import reset_user_settings_for_tests
from tests._shared import FakeCancellation, FakeForegroundPressure
from tests.helpers.user_settings import StubUserSettingsStore

SETTINGS = SimpleNamespace(
    OUROBOROS_CHAT_SUMMARY_MAX_TOKENS=32_768,
    OUROBOROS_MODEL_CONTEXT_TOKENS=200_000,
    OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=300,
)
LEASE_CASES = (("short", 512 * 1_024, False), ("Ж" * 1_500, 2_048, False), ("short", 512 * 1_024, True))


class FakeLLM:
    def __init__(
        self, *, content: str = "updated summary", finish_reason: str = "stop", gate: tuple[Event, Event] | None = None
    ) -> None:
        self.content, self.finish_reason, self.gate, self.calls = content, finish_reason, gate, []

    def chat(self, **kwargs):
        if self.gate:
            entered, release = self.gate
            entered.set()
            assert release.wait(5)
        self.calls.append(kwargs)
        return {"role": "assistant", "content": self.content, "finish_reason": self.finish_reason}, {
            "prompt_tokens": 10,
            "completion_tokens": 4,
        }


@pytest.fixture
def runtime_parts(tmp_path, request, monkeypatch):
    import supervisor.message_bus as message_bus
    from supervisor.runtime_context import reset_runtime_for_tests
    from supervisor.testing import bootstrap_supervisor_for_tests

    monkeypatch.setattr(context_consolidation, "load_settings", lambda: SETTINGS)
    monkeypatch.setattr("ouroboros.app.config.get_light_model", lambda: "test-model")
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=message_bus.LocalChatBridge({}))
    request.addfinalizer(reset_runtime_for_tests)
    request.addfinalizer(runtime.executor.shutdown)
    return runtime


def append_message(store, index: int, *, content: str | None = None) -> None:
    store.append_message(
        ConversationMessage(
            message_id=f"message-{index:04d}",
            conversation_id="dialogs:1",
            actor_type="owner",
            content=content or f"msg-{index:04d}|",
            created_at=f"2026-08-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
            metadata={"chat_id": 1},
        )
    )


def run_transition(parts, llm, payload):
    continuation, summary, detail = context_consolidation.run_context_consolidation_task(
        parts, llm, payload, cancellation=FakeCancellation(), foreground_pending=FakeForegroundPressure()
    )
    if summary is not None and not parts.store._commit_context_consolidation(
        None, expected_cursor=str(payload.get("expected_cursor") or ""), summary=summary
    ):
        return None, "STALE_CURSOR"
    return continuation, detail


def test_overdue_consolidation_catches_up_while_foreground_is_pending(runtime_parts, monkeypatch):
    for index in range(101):
        append_message(runtime_parts.store, index)
    runtime_parts.task_queue.enqueue_dict({"id": "ctx", "type": "context_consolidation", "expected_cursor": ""})
    runtime_parts.task_queue.enqueue_dict({"id": "fg", "type": "task", "text": "pending"})
    record = runtime_parts.store.get_task("ctx")
    runtime_parts.store.upsert_task(record.model_copy(update={"created_at": "2020-01-01T00:00:00+00:00"}))
    llm = FakeLLM()
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda: llm)
    runtime_parts.executor.ensure_started()
    runtime_parts.executor.get_slot(0).busy_task_id = "occupied"
    runtime_parts.executor.assign_next()
    runtime_parts.executor.get_slot(1).future.result(timeout=5)
    assert runtime_parts.store.get_task("ctx").status == "interrupted"
    runtime_parts.executor.assign_next()
    runtime_parts.executor.get_slot(1).future.result(timeout=5)
    assert runtime_parts.store.get_task("ctx").status == "completed"
    assert len(llm.calls) == 2


def test_overdue_consolidation_alternates_with_pending_distillation(runtime_parts, monkeypatch):
    for index in range(101):
        append_message(runtime_parts.store, index)
    runtime_parts.task_queue.enqueue_dict({"id": "ctx", "type": "context_consolidation", "expected_cursor": ""})
    runtime_parts.task_queue.enqueue_dict({"id": "fg", "type": "task", "text": "pending"})
    runtime_parts.task_queue.enqueue_dict(
        {"id": "distill", "type": "memory_distillation", "task_text": "t", "response_text": "r", "owner_id": "global"}
    )
    record = runtime_parts.store.get_task("ctx")
    runtime_parts.store.upsert_task(record.model_copy(update={"created_at": "2020-01-01T00:00:00+00:00"}))
    llm = FakeLLM()
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda: llm)
    runtime_parts.executor.ensure_started()
    runtime_parts.executor.get_slot(0).busy_task_id = "occupied"

    claimed_types = []
    for _ in range(3):
        runtime_parts.executor.assign_next()
        slot = runtime_parts.executor.get_slot(1)
        claimed_types.append(runtime_parts.store.get_task(slot.busy_task_id).task_type)
        slot.future.result(timeout=5)

    # The overdue consolidation catches up without starving the capped distillation queue.
    assert claimed_types == ["context_consolidation", "memory_distillation", "context_consolidation"]


@pytest.mark.parametrize(("age_sec", "expected"), [(599, False), (600, True)])
def test_context_consolidation_age_override_boundary(monkeypatch, age_sec, expected):
    from supervisor import workers

    monkeypatch.setattr(workers.time, "time", lambda: float(age_sec))
    monkeypatch.setattr(workers, "parse_iso_to_ts", lambda _: 0.0)
    record = SimpleNamespace(task_type="context_consolidation", created_at="created-at")
    assert workers._context_consolidation_overdue(record) is expected


def test_context_consolidation_unparseable_created_at_is_overdue(monkeypatch, caplog):
    from supervisor import workers

    caplog.set_level("WARNING")
    monkeypatch.setattr(workers, "parse_iso_to_ts", lambda _: None)
    record = SimpleNamespace(task_type="context_consolidation", task_id="ctx", created_at="not-a-timestamp")

    assert workers._context_consolidation_overdue(record) is True
    assert "unparseable created_at" in caplog.text


def test_consolidation_falls_back_after_primary_timeout(runtime_parts):
    append_message(runtime_parts.store, 1)
    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_MODEL_FALLBACK": "fallback-model"}))
    attempts = []

    def chat(**kwargs):
        attempts.append((kwargs["model"], kwargs["timeout_sec"], kwargs["read_timeout_sec"], kwargs["deadline"]))
        if kwargs["model"] == "test-model":
            raise TimeoutError("primary timed out")
        return {"content": "summary", "finish_reason": "stop"}, {}

    continuation, detail = run_transition(runtime_parts, SimpleNamespace(chat=chat), {"expected_cursor": ""})
    assert (continuation, detail) == (None, "COMMITTED")
    assert [(model, read) for model, _, read, _ in attempts] == [("test-model", 300), ("fallback-model", 300)]
    # Each attempt is capped by the shared wall-clock deadline (read window + margin).
    assert all(deadline is not None and 0 < timeout <= 360 for _, timeout, _, deadline in attempts)


# Per-outcome breaker mutation is covered in test_provider_failure_reporting;
# here: the fallback commits and both attempts' usage is recorded.
def test_consolidation_falls_back_after_invalid_response_and_records_each_usage(runtime_parts):
    append_message(runtime_parts.store, 1)
    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_MODEL_FALLBACK": "fallback-model"}))
    attempts, events = [], []

    def chat(**kwargs):
        attempts.append(kwargs["model"])
        if kwargs["model"] == "test-model":
            return {"content": "partial", "finish_reason": "length"}, {"prompt_tokens": 123}
        return {"content": "fallback summary", "finish_reason": "stop"}, {"prompt_tokens": 5}

    continuation, summary, detail = context_consolidation.run_context_consolidation_task(
        runtime_parts,
        SimpleNamespace(chat=chat),
        {"expected_cursor": ""},
        cancellation=FakeCancellation(),
        foreground_pending=FakeForegroundPressure(),
        event_queue=SimpleNamespace(put_nowait=events.append),
    )

    assert (continuation, summary.summary_ref, detail) == (None, "fallback summary", "COMMITTED")
    assert attempts == ["test-model", "fallback-model"]
    assert [event["prompt_tokens"] for event in events] == [123, 5]


_SAVE_CALL = [{"function": {"name": "memory_save", "arguments": "{}"}}]


@pytest.mark.parametrize(
    ("validator_name", "response", "expected"),
    [
        # The committed-verbatim summary requires an explicit successful
        # finish_reason; distillation content is only loop control.
        ("summary", {"content": "summary", "finish_reason": "end_turn"}, None),
        ("summary", {"content": "summary", "finish_reason": None}, "response_rejected"),
        ("distillation", {"content": "Saved the fact.", "tool_calls": [], "finish_reason": None}, None),
        ("distillation", {"content": "", "tool_calls": _SAVE_CALL, "finish_reason": "length"}, None),
        ("summary", {"content": "", "finish_reason": None}, "incomplete_response"),
        ("distillation", {"content": "", "tool_calls": [], "finish_reason": None}, "incomplete_response"),
        ("summary", {"content": "partial", "finish_reason": "length"}, "response_rejected"),
        ("distillation", {"content": "partial", "tool_calls": [], "finish_reason": "length"}, "response_rejected"),
    ],
)
def test_background_response_validators_return_typed_outcomes(validator_name, response, expected):
    from ouroboros.agent.task_pipeline import _distillation_response_error

    validators = {
        "summary": context_consolidation._summary_response_error,
        "distillation": _distillation_response_error,
    }

    outcome = validators[validator_name](response)

    assert (outcome.value if outcome is not None else None) == expected


@pytest.mark.parametrize(
    ("message_count", "finish_reason"), [(50, "stop"), (51, "stop"), (99, "stop"), (100, "stop"), (100, "error")]
)
def test_agent_persistence_controls_scheduling_and_tail_boundary(
    runtime_parts, monkeypatch, message_count, finish_reason
):
    import supervisor.message_bus as message_bus

    for index in range(message_count - 1):
        append_message(runtime_parts.store, index)
    scheduling = SimpleNamespace(
        OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=100, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000
    )
    monkeypatch.setattr(message_bus, "load_settings", lambda: scheduling)
    append = message_bus.append_chat_message
    monkeypatch.setattr(message_bus, "append_chat_message", lambda *args, **kwargs: None)
    message_bus.log_chat("agent", "chat-1", "owner-1", "not persisted", source="browser")
    assert not runtime_parts.task_queue.queue_has_task_type("context_consolidation")
    monkeypatch.setattr(message_bus, "append_chat_message", append)
    message_bus.log_chat("agent", "chat-1", "owner-1", f"msg-{message_count - 1:04d}|", source="browser")
    section = context._build_recent_chat_section(runtime_parts.drive_root)
    assert all(f"msg-{index:04d}|" in section for index in range(message_count))
    assert "CHAT BACKLOG" not in section
    assert runtime_parts.task_queue.queue_has_task_type("context_consolidation") is (message_count == 100)
    if message_count == 100:
        entered, release = Event(), Event()
        monkeypatch.setattr(
            "ouroboros.llm.LLMClient", lambda: FakeLLM(gate=(entered, release), finish_reason=finish_reason)
        )
        runtime_parts.executor.assign_next()
        assert entered.wait(5)
        message_bus.log_chat("agent", "chat-1", "owner-1", "X" * 600_000, source="browser")
        release.set()
        runtime_parts.executor.get_slot(1).future.result(timeout=5)
        # A rejected response is a failed run: it schedules nothing even though the tail moved.
        assert runtime_parts.task_queue.queue_has_task_type("context_consolidation") is (finish_reason != "error")
        if finish_reason == "error":
            assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)
            return
        runtime_parts.executor.assign_next()
        runtime_parts.executor.get_slot(1).future.result(timeout=5)
        assert runtime_parts.task_queue.queue_has_task_type("context_consolidation")
        assert any(
            task.input.get("scheduled_tail_message_id")
            for task in runtime_parts.store.list_tasks(statuses=["interrupted"])
        )


def _failure_state(parts) -> dict:
    state = parts.store.load_owner_runtime_state() or {}
    return dict(state.get("context_consolidation_failures") or {})


def test_final_hard_timeout_schedules_no_followup(runtime_parts, monkeypatch):
    from supervisor.queue import QUEUE_MAX_RETRIES

    scheduling = SimpleNamespace(
        OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000
    )
    append_message(runtime_parts.store, 1)
    assert runtime_parts.task_queue.schedule_context_consolidation(scheduling)
    queued = runtime_parts.store.list_tasks(statuses=["queued"])[0]
    runtime_parts.store.upsert_task(queued.model_copy(update={"attempt": QUEUE_MAX_RETRIES + 1}))
    entered, release = Event(), Event()
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda: FakeLLM(gate=(entered, release)))
    runtime_parts.executor.assign_next()
    future = runtime_parts.executor.get_slot(1).future
    assert entered.wait(5)
    append_message(runtime_parts.store, 2)
    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)

    runtime_parts.task_queue.hard_timeout_sec = 0
    runtime_parts.task_queue.enforce_task_timeouts(runtime_parts.executor)
    release.set()
    future.result(timeout=5)

    # The kill is a failed run: no follow-up task, and the moved tail does not get one either.
    assert runtime_parts.store.list_tasks(statuses=["queued", "scheduled", "interrupted", "running"]) == []
    assert _failure_state(runtime_parts)["consecutive"] == 1
    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)


def test_failing_consolidation_in_active_chat_creates_one_task(runtime_parts, monkeypatch):
    scheduling = SimpleNamespace(
        OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000
    )
    append_message(runtime_parts.store, 1)
    assert runtime_parts.task_queue.schedule_context_consolidation(scheduling)
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda: FakeLLM(finish_reason="error"))
    runtime_parts.executor.assign_next()
    runtime_parts.executor.get_slot(1).future.result(timeout=5)

    assert _failure_state(runtime_parts)["consecutive"] == 1
    for index in range(2, 12):
        append_message(runtime_parts.store, index)
        assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)
    assert len(runtime_parts.store.list_tasks()) == 1


def test_completed_run_resets_the_failure_counter(runtime_parts, monkeypatch):
    scheduling = SimpleNamespace(
        OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000
    )
    append_message(runtime_parts.store, 1)
    runtime_parts.task_queue.record_consolidation_outcome("failed")
    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)

    runtime_parts.task_queue.record_consolidation_outcome("completed")

    assert _failure_state(runtime_parts) == {"consecutive": 0, "last_failure_at": ""}
    assert runtime_parts.task_queue.schedule_context_consolidation(scheduling)


@pytest.mark.parametrize(("consecutive", "expected"), [(0, 0), (1, 300), (2, 600), (4, 2_400), (5, 3_600), (99, 3_600)])
def test_consolidation_cooldown_grows_and_stops_at_the_cap(consecutive, expected):
    from supervisor import queue as queue_mod

    assert queue_mod._consolidation_failure_cooldown_sec(consecutive) == expected


def test_expired_cooldown_allows_one_attempt(runtime_parts, monkeypatch):
    from supervisor import queue as queue_mod

    scheduling = SimpleNamespace(
        OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000
    )
    append_message(runtime_parts.store, 1)
    runtime_parts.task_queue.record_consolidation_outcome("failed")
    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)

    failed_at = queue_mod.parse_iso_to_ts(_failure_state(runtime_parts)["last_failure_at"])
    monkeypatch.setattr(
        queue_mod.time, "time", lambda: failed_at + queue_mod._CONSOLIDATION_FAILURE_COOLDOWN_BASE_SEC + 1
    )

    assert runtime_parts.task_queue.schedule_context_consolidation(scheduling)
    # Exactly one: the single-flight guard holds the rest until the run reports an outcome.
    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)


def test_an_unmoved_chat_tail_skips_the_backlog_queries(runtime_parts, monkeypatch):
    """Every persisted message asks; without a new one the backlog cannot have grown."""
    scheduling = SimpleNamespace(
        OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=100, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000
    )
    append_message(runtime_parts.store, 1)
    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)
    probes: list = []
    monkeypatch.setattr(
        runtime_parts.store,
        "get_latest_context_summary",
        lambda *args, **kwargs: probes.append(args) or None,
    )

    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)
    assert probes == []

    # A new message reopens the question, and so does the caller that just consumed backlog.
    append_message(runtime_parts.store, 2)
    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)
    assert len(probes) == 1
    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling, force=True)
    assert len(probes) == 2


def test_absent_failure_state_is_not_throttled(runtime_parts):
    scheduling = SimpleNamespace(
        OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000
    )
    append_message(runtime_parts.store, 1)

    assert _failure_state(runtime_parts) == {}
    assert runtime_parts.task_queue.schedule_context_consolidation(scheduling)


def test_declined_schedule_names_the_count_and_the_remaining_cooldown(runtime_parts, caplog):
    scheduling = SimpleNamespace(
        OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000
    )
    append_message(runtime_parts.store, 1)
    runtime_parts.task_queue.record_consolidation_outcome("failed")
    runtime_parts.task_queue.record_consolidation_outcome("failed")
    caplog.set_level("INFO")

    assert not runtime_parts.task_queue.schedule_context_consolidation(scheduling)
    assert "2 consecutive failures" in caplog.text
    assert "of cooldown left" in caplog.text


@pytest.mark.parametrize("content", ["Ж" * 1_500 + "MESSAGE-END", "A" * 3_000 + "MESSAGE-END"])
def test_oversized_message_continues_in_bounded_fragments(runtime_parts, monkeypatch, content):
    append_message(runtime_parts.store, 1, content=content)
    monkeypatch.setattr(context_consolidation, "CONTEXT_CONSOLIDATION_INPUT_MAX_BYTES", 2_048)
    llm, state = FakeLLM(), {"expected_cursor": ""}
    while True:
        calls_before = len(llm.calls)
        continuation, _ = run_transition(runtime_parts, llm, state)
        assert len(llm.calls) == calls_before + 1
        if continuation is None:
            break
        if not state.get("pending_offset"):
            fragment = llm.calls[-1]["messages"][1]["content"].rsplit("CHAT_DATA: ", 1)[-1].splitlines()[0]
            assert continuation["pending_offset"] == len(fragment.encode())
        state = continuation
        assert runtime_parts.store.get_latest_context_summary("global") is None
    assert runtime_parts.store.get_latest_context_summary("global").source_offsets_ref == "message-0001"
    fragments = [call["messages"][1]["content"].rsplit("CHAT_DATA: ", 1)[-1].splitlines()[0] for call in llm.calls]
    for call, fragment in zip(llm.calls, fragments, strict=True):
        request_bytes = len(json.dumps(call["messages"], ensure_ascii=False, separators=(",", ":")).encode())
        assert request_bytes <= context_consolidation.CONTEXT_CONSOLIDATION_INPUT_MAX_BYTES
        assert fragment.startswith("← 00:00 [User] ")
        assert call["max_tokens"] == SETTINGS.OUROBOROS_CHAT_SUMMARY_MAX_TOKENS
    assert "".join(fragment.removeprefix("← 00:00 [User] ") for fragment in fragments) == content


@pytest.mark.parametrize(("content", "input_bytes", "reclaimed"), LEASE_CASES)
def test_late_result_preserves_cancel_or_reclaimed_lease(runtime_parts, monkeypatch, content, input_bytes, reclaimed):
    store = runtime_parts.store
    append_message(runtime_parts.store, 1, content=content)
    runtime_parts.task_queue.enqueue_dict({"id": "ctx", "type": "context_consolidation", "expected_cursor": ""})
    entered, release = Event(), Event()
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda: FakeLLM(gate=(entered, release)))
    monkeypatch.setattr(context_consolidation, "CONTEXT_CONSOLIDATION_INPUT_MAX_BYTES", input_bytes)
    executor = runtime_parts.executor
    executor.assign_next()
    future = executor.get_slot(1).future
    assert entered.wait(5)
    if reclaimed:
        claimed = store.get_task("ctx")
        store.upsert_task(claimed.model_copy(update={"status": "interrupted", "worker_id": None}))
        new_lease = store.claim_next_task(worker_id=1, include_task_types={"context_consolidation"})
        assert new_lease.started_at != claimed.started_at
    else:
        assert runtime_parts.task_queue.cancel_task_by_id("ctx", executor)
    protected = store.get_task("ctx")
    if not reclaimed:
        assert (
            store.upsert_task(protected.model_copy(update={"status": "completed"}))
            == protected
            == store.get_task("ctx")
        )
    release.set()
    future.result(timeout=5)
    events = [executor.get_event_q().get_nowait() for _ in range(executor.get_event_q().qsize())]
    assert (store.get_task("ctx"), store.get_latest_context_summary("global")) == (protected, None)
    usage = [(event["task_id"], event["model"], event["category"]) for event in events if event["type"] == "llm_usage"]
    assert usage == [("ctx", "test-model", "context_consolidation")]


def test_shutdown_interrupts_blocking_context_lease_without_late_commit(runtime_parts, monkeypatch):
    from supervisor.events import _handle_task_done

    store = runtime_parts.store
    append_message(runtime_parts.store, 1)
    runtime_parts.task_queue.enqueue_dict({"id": "ctx", "type": "context_consolidation", "expected_cursor": ""})
    entered, release = Event(), Event()
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda: FakeLLM(gate=(entered, release)))
    executor = runtime_parts.executor
    executor.assign_next()
    future = executor.get_slot(1).future
    assert entered.wait(5)
    original_input = store.get_task("ctx").input
    executor.shutdown()
    interrupted = store.get_task("ctx")
    release.set()
    assert (interrupted.status, interrupted.worker_id, interrupted.input) == ("interrupted", None, original_input)
    future.result(timeout=5)
    _handle_task_done(
        {
            "type": "task_done",
            "task_id": "ctx",
            "task_type": "context_consolidation",
            "worker_id": 1,
            "status": "completed",
            "result": "late",
        },
        SimpleNamespace(DRIVE_ROOT=runtime_parts.drive_root, executor=executor),
    )
    assert store.get_task("ctx") == interrupted
    assert store.get_latest_context_summary("global") is None
    reclaimed = store.claim_next_task(worker_id=1, include_task_types={"context_consolidation"})
    assert (reclaimed.task_id, reclaimed.status) == ("ctx", "running")


def test_missing_anchor_recovery_and_stale_replay_are_lossless(runtime_parts, monkeypatch):
    for index in (1, 2):
        append_message(runtime_parts.store, index)
    for reason in ("blacklist", "request_filter", "response_blacklist", "error", "length", ""):
        with pytest.raises(RuntimeError, match="unsuccessful LLM chat summary"):
            run_transition(runtime_parts, FakeLLM(finish_reason=reason), {"expected_cursor": ""})
    assert runtime_parts.store.get_latest_context_summary("global") is None
    runtime_parts.store.upsert_context_summary(
        ContextSummary(
            summary_id="shared-chat",
            conversation_id="global",
            source_offsets_ref="missing-anchor",
            summary_ref="previous summary",
        )
    )
    llm = FakeLLM(content="recovered summary")
    continuation, detail = run_transition(runtime_parts, llm, {"expected_cursor": "missing-anchor"})
    assert (continuation, detail) == (None, "COMMITTED")
    assert llm.calls[0]["messages"][1]["content"].index("msg-0001|") < llm.calls[0]["messages"][1]["content"].index(
        "msg-0002|"
    )
    assert runtime_parts.store.get_latest_context_summary("global").source_offsets_ref == "message-0002"
    append_message(runtime_parts.store, 3)
    continuation, detail = run_transition(runtime_parts, llm, {"expected_cursor": "missing-anchor"})
    assert (detail, continuation["expected_cursor"], len(llm.calls)) == ("STALE_CURSOR", "message-0002", 1)
    assert (continuation["pending_message_id"], continuation["pending_offset"], continuation["pending_summary"]) == (
        "",
        0,
        "",
    )
    run_transition(runtime_parts, llm, continuation)
    assert runtime_parts.store.get_latest_context_summary("global").source_offsets_ref == "message-0003"
