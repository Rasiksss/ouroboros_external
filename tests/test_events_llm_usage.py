"""Tests for supervisor/events.py _handle_llm_usage event persistence."""


def test_llm_usage_writes_cached_tokens_and_cache_write_tokens(tmp_path):
    from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store
    from supervisor import events as ev_module

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    init_runtime_store(tmp_path)

    class FakeCtx:
        DRIVE_ROOT = tmp_path

        def update_usage_from_llm(self, usage):
            self.last_usage = usage

    evt = {
        "type": "llm_usage",
        "model": "anthropic/claude-sonnet-4.6",
        "usage": {
            "prompt_tokens": 2000,
            "completion_tokens": 300,
            "cached_tokens": 1200,
            "cache_write_tokens": 400,
        },
        "category": "compaction",
        "provider": "openrouter",
        "source": "loop",
        "model_category": "light",
        "api_key_type": "openrouter",
    }
    ctx = FakeCtx()
    ev_module._handle_llm_usage(evt, ctx)

    events_file = tmp_path / "logs" / "events.jsonl"
    assert not events_file.exists()
    store = get_runtime_store(tmp_path)
    assert store is not None
    written = store.list_audit_events(log_stream="events", event_type="llm_usage", limit=1)[0].metadata
    assert written.get("cached_tokens") == 1200
    assert written.get("cache_write_tokens") == 400
    assert written.get("category") == "compaction"
    assert written.get("provider") == "openrouter"
    assert written.get("source") == "loop"
    assert written.get("model_category") == "light"
    assert written.get("api_key_type") == "openrouter"
    assert ctx.last_usage["cached_tokens"] == 1200


def test_task_metrics_are_persisted_to_supervisor_stream(tmp_path):
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor import events as ev_module

    store = init_runtime_store(tmp_path)

    class FakeCtx:
        DRIVE_ROOT = tmp_path

    evt = {
        "ts": "2026-03-31T10:11:12Z",
        "task_id": "task-99",
        "task_type": "task",
        "duration_sec": 3.14159,
        "tool_calls": 4,
        "tool_errors": 1,
    }
    ev_module._handle_task_metrics(evt, FakeCtx())

    assert not (tmp_path / "logs" / "supervisor.jsonl").exists()
    stored_events = store.list_audit_events(log_stream="supervisor", limit=10)
    assert len(stored_events) == 1
    written = stored_events[0].metadata
    assert written["type"] == "task_metrics_event"
    assert written["task_id"] == "task-99"
    assert written["tool_calls"] == 4
    assert written["duration_sec"] == 3.142
    assert written["tool_errors"] == 1


def test_usage_status_helpers_read_runtime_events_without_jsonl(tmp_path, monkeypatch):
    from ouroboros.observability.event_record import record_event
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor import state as state_module

    store = init_runtime_store(tmp_path)
    repo = state_module.RuntimeStateRepository(tmp_path, store=store)
    monkeypatch.setattr(state_module, "get_state_repository", lambda drive_root=None: repo)

    record_event(
        tmp_path,
        "events",
        "llm_usage",
        {
            "task_id": "task-1",
            "model": "provider/model-a",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 30,
        },
    )

    assert not (tmp_path / "logs" / "events.jsonl").exists()
    assert state_module.model_breakdown()["provider/model-a"]["calls"] == 1
    task_usage = state_module.per_task_usage_summary(max_tasks=1)
    assert task_usage[0]["task_id"] == "task-1"
    assert task_usage[0]["prompt_tokens"] == 100
    assert task_usage[0]["completion_tokens"] == 20
