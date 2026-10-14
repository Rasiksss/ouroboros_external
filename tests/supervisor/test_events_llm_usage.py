"""Tests for supervisor/events.py _handle_llm_usage event persistence."""

from types import SimpleNamespace

from ouroboros.agent_events.interfaces import LlmUsageReported, TaskMetrics
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.usage import sql_usage_repository


def test_llm_usage_writes_cached_tokens_and_cache_write_tokens(tmp_path):
    from supervisor import events as ev_module

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    runtime_persistence.initialize()

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        state_repo = SimpleNamespace(audit_event_repository=sql_audit_event_repository())
        usage_repository = sql_usage_repository()

        def update_usage_from_llm(self, usage, **_kwargs):
            self.last_usage = usage

    evt = LlmUsageReported(
        scope=TEST_SCOPE,
        model="anthropic/claude-sonnet-4.6",
        prompt_tokens=2000,
        completion_tokens=300,
        cached_tokens=1200,
        cache_write_tokens=400,
        usage={
            "prompt_tokens": 2000,
            "completion_tokens": 300,
            "cached_tokens": 1200,
            "cache_write_tokens": 400,
        },
        category="compaction",
        provider="openrouter",
        source="loop",
        model_category="light",
        api_key_type="openrouter",
    )
    ctx = FakeCtx()
    ev_module._handle_llm_usage(evt, ctx)

    events_file = tmp_path / "logs" / "events.jsonl"
    assert not events_file.exists()
    store = runtime_persistence.current
    assert store is not None
    written = (
        sql_audit_event_repository(store.engine)
        .list_audit_events(log_stream="events", event_type="llm_usage", limit=1)[0]
        .metadata
    )
    assert written.get("cached_tokens") == 1200
    assert written.get("cache_write_tokens") == 400
    assert written.get("category") == "compaction"
    assert written.get("provider") == "openrouter"
    assert written.get("source") == "loop"
    assert written.get("model_category") == "light"
    assert written.get("api_key_type") == "openrouter"
    assert ctx.last_usage["cached_tokens"] == 1200


def test_task_metrics_are_persisted_to_supervisor_stream(tmp_path):
    from supervisor import events as ev_module

    store = runtime_persistence.initialize()

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        state_repo = SimpleNamespace(audit_event_repository=sql_audit_event_repository())
        usage_repository = sql_usage_repository()

    evt = TaskMetrics(
        scope=TEST_SCOPE,
        ts="2026-03-31T10:11:12Z",
        task_id="task-99",
        task_type="task",
        duration_sec=3.14159,
        tool_calls=4,
        tool_errors=1,
    )
    ev_module._handle_task_metrics(evt, FakeCtx())

    assert not (tmp_path / "logs" / "supervisor.jsonl").exists()
    stored_events = sql_audit_event_repository(store.engine).list_audit_events(log_stream="supervisor", limit=10)
    assert len(stored_events) == 1
    written = stored_events[0].metadata
    assert written["type"] == "task_metrics_event"
    assert written["task_id"] == "task-99"
    assert written["tool_calls"] == 4
    assert written["duration_sec"] == 3.142
    assert written["tool_errors"] == 1


def test_usage_status_helpers_read_runtime_events_without_jsonl(tmp_path, monkeypatch):
    from ouroboros.observability.event_record import record_event
    from supervisor import state as state_module

    runtime_persistence.initialize()
    repo = state_module.RuntimeStateRepository(
        tmp_path,
        owner_state_repository=sql_owner_state_repository(),
        scope=state_module.configured_owner_state_scope(),
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    monkeypatch.setattr(state_module, "get_state_repository", lambda drive_root=None: repo)

    record_event(
        "events",
        "llm_usage",
        {
            "task_id": "task-1",
            "model": "provider/model-a",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 30,
        },
        audit_event_repository=sql_audit_event_repository(),
    )

    assert not (tmp_path / "logs" / "events.jsonl").exists()
    assert state_module.model_breakdown()["provider/model-a"]["calls"] == 1
    task_usage = state_module.per_task_usage_summary(max_tasks=1, owner_scope=repo.scope)
    assert task_usage[0]["task_id"] == "task-1"
    assert task_usage[0]["prompt_tokens"] == 100
    assert task_usage[0]["completion_tokens"] == 20


def test_a_usage_row_is_attributed_to_the_owner_the_event_names(tmp_path, monkeypatch) -> None:
    """The reader filters on owner and agent, so a writer that omits the scope answers empty.

    `_handle_llm_usage` already passed `event.scope` to `record_usage` and dropped it from
    the audit row, so `per_task_usage_summary` — narrowed to one owner on this branch — was
    wired to a predicate its own writer never satisfied.
    """
    from types import SimpleNamespace

    from ouroboros.agent_events.interfaces import LlmUsageReported
    from ouroboros.observability.event_record import list_runtime_events
    from supervisor import events as events_module, state as state_module
    from tests.helpers.shared import TEST_SCOPE, scope_with

    store = runtime_persistence.initialize()
    usage = sql_usage_repository()
    repo = state_module.RuntimeStateRepository(
        tmp_path,
        owner_state_repository=sql_owner_state_repository(),
        scope=TEST_SCOPE,
        usage_repository=usage,
        audit_event_repository=sql_audit_event_repository(),
    )
    monkeypatch.setattr(state_module, "get_state_repository", lambda drive_root=None: repo)
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        tasks_repo=store.tasks_repo,
        state_repo=repo,
        usage_repository=usage,
        update_usage_from_llm=lambda *_a, **_k: None,
        bridge=SimpleNamespace(push_log=lambda *_a, **_k: None),
    )

    events_module._handle_llm_usage(
        LlmUsageReported(scope=TEST_SCOPE, task_id="task-1", model="provider/model-a", prompt_tokens=100),
        ctx,
    )

    mine = list_runtime_events(
        event_type="llm_usage",
        limit=10,
        scope=TEST_SCOPE,
        audit_event_repository=sql_audit_event_repository(),
    )
    theirs = list_runtime_events(
        event_type="llm_usage",
        limit=10,
        scope=scope_with(TEST_SCOPE, owner_id="somebody-else"),
        audit_event_repository=sql_audit_event_repository(),
    )
    assert [row["task_id"] for row in mine] == ["task-1"]
    assert theirs == []


def test_the_model_table_is_narrowed_to_the_owner_who_asked(tmp_path, monkeypatch) -> None:
    """A deployment-wide table tells one owner which models everybody else is spending on."""
    from ouroboros.observability.event_record import record_event
    from supervisor import state as state_module
    from tests.helpers.shared import TEST_SCOPE, scope_with

    runtime_persistence.initialize()
    repo = state_module.RuntimeStateRepository(
        tmp_path,
        owner_state_repository=sql_owner_state_repository(),
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    monkeypatch.setattr(state_module, "get_state_repository", lambda drive_root=None: repo)
    other = scope_with(TEST_SCOPE, owner_id="somebody-else")
    record_event(
        "events",
        "llm_usage",
        {"model": "mine", "prompt_tokens": 1},
        scope=TEST_SCOPE,
        audit_event_repository=sql_audit_event_repository(),
    )
    record_event(
        "events",
        "llm_usage",
        {"model": "theirs", "prompt_tokens": 1},
        scope=other,
        audit_event_repository=sql_audit_event_repository(),
    )

    assert set(state_module.model_breakdown(owner_scope=TEST_SCOPE)) == {"mine"}
    assert set(state_module.model_breakdown(owner_scope=other)) == {"theirs"}
    # An operator asking for the deployment still gets both.
    assert set(state_module.model_breakdown()) == {"mine", "theirs"}
