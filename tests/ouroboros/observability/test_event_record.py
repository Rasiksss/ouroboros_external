from __future__ import annotations

import pytest

from ouroboros.observability.event_record import record_event
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.utils import sanitize_task_for_event, set_log_sink
from tests.helpers.audit_events import sql_audit_event_repository


def test_record_event_persists_events_stream_to_runtime_store_not_jsonl(tmp_path):
    store = runtime_persistence.initialize()
    payload = record_event(
        "events",
        "sample_event",
        {"task_id": "t1", "value": 7},
        audit_event_repository=sql_audit_event_repository(),
    )
    assert payload["type"] == "sample_event"
    assert not (tmp_path / "logs" / "events.jsonl").exists()
    stored_events = sql_audit_event_repository(store.engine).list_audit_events(log_stream="events", limit=10)
    assert len(stored_events) == 1
    stored = stored_events[0].metadata
    assert stored["type"] == "sample_event"
    assert stored["task_id"] == "t1"
    assert stored["log_stream"] == "events"
    assert stored["log_kind"] == "runtime_event"


def test_record_event_events_stream_emits_live_log_without_jsonl(tmp_path):
    seen: list[dict] = []
    runtime_persistence.initialize()
    set_log_sink(seen.append)
    try:
        payload = record_event(
            "events",
            "live_sample",
            {"task_id": "t-live"},
            audit_event_repository=sql_audit_event_repository(),
        )
    finally:
        set_log_sink(None)

    assert payload["type"] == "live_sample"
    assert not (tmp_path / "logs" / "events.jsonl").exists()
    assert seen == [payload]


def test_sanitize_task_for_event_writes_no_prompt_object(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    sanitized = sanitize_task_for_event({"id": "task-1", "text": "x" * 5000})

    assert not (tmp_path / "logs" / "tasks").exists()
    assert "text_full_path" not in sanitized
    assert sanitized["text_len"] == 5000
    assert sanitized["text_sha256"]
    assert sanitized["text_truncated"] is True


def test_record_event_writes_audit_when_enabled(tmp_path):
    repository = sql_audit_event_repository()

    record_event(
        "events",
        "secure_decision",
        {"task_id": "task-77"},
        decision="deny",
        reason="policy",
        audit_event_repository=repository,
    )
    assert len(repository.list_audit_events()) == 1
    event = repository.list_audit_events()[0]
    assert event.event_type == "secure_decision"
    assert event.decision == "deny"
    assert event.reason == "policy"


def test_record_event_uses_injected_repository_without_a_composed_runtime_store(tmp_path):
    repository = sql_audit_event_repository()
    runtime_persistence.close()
    record_event("events", "sample_event", {"task_id": "t1"}, audit_event_repository=repository)
    assert repository.list_audit_events()[0].event_type == "sample_event"
    with pytest.raises(RuntimeError, match="not composed"):
        _ = runtime_persistence.current


def test_record_event_persists_tools_stream_to_runtime_store_not_jsonl(tmp_path):
    store = runtime_persistence.initialize()
    payload = record_event(
        "tools",
        "tool_call",
        {"task_id": "t1", "tool": "shell"},
        audit_event_repository=sql_audit_event_repository(),
    )
    assert payload["type"] == "tool_call"
    assert not (tmp_path / "logs" / "tools.jsonl").exists()
    stored_events = sql_audit_event_repository(store.engine).list_audit_events(log_stream="tools", limit=10)
    assert len(stored_events) == 1
    stored = stored_events[0].metadata
    assert stored["type"] == "tool_call"
    assert stored["task_id"] == "t1"
    assert stored["tool"] == "shell"
    assert stored["log_stream"] == "tools"


def test_record_event_persists_supervisor_stream_to_runtime_store_not_jsonl(tmp_path):
    store = runtime_persistence.initialize()
    payload = record_event(
        "supervisor",
        "supervisor_sample",
        {"task_id": "t1", "value": 7},
        audit_event_repository=sql_audit_event_repository(),
    )
    assert payload["type"] == "supervisor_sample"
    assert not (tmp_path / "logs" / "supervisor.jsonl").exists()
    stored_events = sql_audit_event_repository(store.engine).list_audit_events(log_stream="supervisor", limit=10)
    assert len(stored_events) == 1
    stored = stored_events[0]
    assert stored.log_stream == "supervisor"
    assert stored.metadata["type"] == "supervisor_sample"
    assert stored.metadata["task_id"] == "t1"


def test_list_audit_events_filters_by_log_stream_column(tmp_path):
    store = runtime_persistence.initialize()
    record_event("events", "sample_event", {"task_id": "t1"}, audit_event_repository=sql_audit_event_repository())
    record_event(
        "tools",
        "tool_call",
        {"task_id": "t1", "tool": "shell"},
        audit_event_repository=sql_audit_event_repository(),
    )
    record_event(
        "supervisor",
        "task_metrics_event",
        {"task_id": "t1"},
        audit_event_repository=sql_audit_event_repository(),
    )

    assert [
        e.event_type for e in sql_audit_event_repository(store.engine).list_audit_events(log_stream="events", limit=10)
    ] == ["sample_event"]
    assert [
        e.event_type for e in sql_audit_event_repository(store.engine).list_audit_events(log_stream="tools", limit=10)
    ] == ["tool_call"]
    assert [
        e.event_type
        for e in sql_audit_event_repository(store.engine).list_audit_events(log_stream="supervisor", limit=10)
    ] == ["task_metrics_event"]
    assert len(sql_audit_event_repository(store.engine).list_audit_events(limit=10)) == 3


def test_record_event_passes_decision_and_reason(tmp_path):
    repository = sql_audit_event_repository()

    record_event(
        "events",
        "secure_decision",
        {"task_id": "task-77"},
        decision="deny",
        reason="policy",
        audit_event_repository=repository,
    )
    assert len(repository.list_audit_events()) == 1
    event = repository.list_audit_events()[0]
    assert event.event_type == "secure_decision"
    assert event.log_stream == "events"
    assert event.decision == "deny"
    assert event.reason == "policy"
