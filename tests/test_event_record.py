from __future__ import annotations

from ouroboros.observability.event_record import record_event
from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store
from ouroboros.utils import set_log_sink


def test_record_event_persists_events_stream_to_runtime_store_not_jsonl(tmp_path):
    store = init_runtime_store(tmp_path)
    payload = record_event(tmp_path, "events", "sample_event", {"task_id": "t1", "value": 7})
    assert payload["type"] == "sample_event"
    assert not (tmp_path / "logs" / "events.jsonl").exists()
    stored_events = store.list_audit_events(log_stream="events", limit=10)
    assert len(stored_events) == 1
    stored = stored_events[0].metadata
    assert stored["type"] == "sample_event"
    assert stored["task_id"] == "t1"
    assert stored["log_stream"] == "events"
    assert stored["log_kind"] == "runtime_event"


def test_record_event_events_stream_emits_live_log_without_jsonl(tmp_path):
    seen: list[dict] = []
    init_runtime_store(tmp_path)
    set_log_sink(seen.append)
    try:
        payload = record_event(tmp_path, "events", "live_sample", {"task_id": "t-live"})
    finally:
        set_log_sink(None)

    assert payload["type"] == "live_sample"
    assert not (tmp_path / "logs" / "events.jsonl").exists()
    assert seen == [payload]


def test_record_event_writes_audit_when_enabled(tmp_path, monkeypatch):
    captured: list[object] = []

    class _Store:
        def append_audit_event(self, event):
            captured.append(event)

    monkeypatch.setattr(
        "ouroboros.observability.event_record.get_runtime_store",
        lambda _drive_root: _Store(),
    )
    record_event(
        tmp_path,
        "events",
        "secure_decision",
        {"task_id": "task-77"},
        decision="deny",
        reason="policy",
    )
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == "secure_decision"
    assert event.decision == "deny"
    assert event.reason == "policy"


def test_record_event_events_stream_initializes_runtime_store(tmp_path):
    record_event(tmp_path, "events", "sample_event", {"task_id": "t1"})
    store = get_runtime_store(tmp_path)
    assert store is not None
    assert store.list_audit_events(log_stream="events", limit=1)[0].event_type == "sample_event"


def test_append_jsonl_rejects_events_log_path(tmp_path):
    from ouroboros.utils import append_jsonl

    event_log = tmp_path / "logs" / "events.jsonl"
    assert append_jsonl(event_log, {"type": "legacy"}) is False
    assert not event_log.exists()


def test_record_event_persists_tools_stream_to_runtime_store_not_jsonl(tmp_path):
    store = init_runtime_store(tmp_path)
    payload = record_event(tmp_path, "tools", "tool_call", {"task_id": "t1", "tool": "shell"})
    assert payload["type"] == "tool_call"
    assert not (tmp_path / "logs" / "tools.jsonl").exists()
    stored_events = store.list_audit_events(log_stream="tools", limit=10)
    assert len(stored_events) == 1
    stored = stored_events[0].metadata
    assert stored["type"] == "tool_call"
    assert stored["task_id"] == "t1"
    assert stored["tool"] == "shell"
    assert stored["log_stream"] == "tools"


def test_record_event_persists_supervisor_stream_to_runtime_store_not_jsonl(tmp_path):
    store = init_runtime_store(tmp_path)
    payload = record_event(tmp_path, "supervisor", "supervisor_sample", {"task_id": "t1", "value": 7})
    assert payload["type"] == "supervisor_sample"
    assert not (tmp_path / "logs" / "supervisor.jsonl").exists()
    stored_events = store.list_audit_events(log_stream="supervisor", limit=10)
    assert len(stored_events) == 1
    stored = stored_events[0]
    assert stored.log_stream == "supervisor"
    assert stored.metadata["type"] == "supervisor_sample"
    assert stored.metadata["task_id"] == "t1"


def test_list_audit_events_filters_by_log_stream_column(tmp_path):
    store = init_runtime_store(tmp_path)
    record_event(tmp_path, "events", "sample_event", {"task_id": "t1"})
    record_event(tmp_path, "tools", "tool_call", {"task_id": "t1", "tool": "shell"})
    record_event(tmp_path, "supervisor", "task_metrics_event", {"task_id": "t1"})

    assert [e.event_type for e in store.list_audit_events(log_stream="events", limit=10)] == ["sample_event"]
    assert [e.event_type for e in store.list_audit_events(log_stream="tools", limit=10)] == ["tool_call"]
    assert [e.event_type for e in store.list_audit_events(log_stream="supervisor", limit=10)] == ["task_metrics_event"]
    assert len(store.list_audit_events(limit=10)) == 3


def test_record_event_passes_decision_and_reason(tmp_path, monkeypatch):
    captured: list[object] = []

    class _Store:
        def append_audit_event(self, event):
            captured.append(event)

    monkeypatch.setattr(
        "ouroboros.observability.event_record.get_runtime_store",
        lambda _drive_root: _Store(),
    )
    record_event(
        tmp_path,
        "events",
        "secure_decision",
        {"task_id": "task-77"},
        decision="deny",
        reason="policy",
    )
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == "secure_decision"
    assert event.log_stream == "events"
    assert event.decision == "deny"
    assert event.reason == "policy"
