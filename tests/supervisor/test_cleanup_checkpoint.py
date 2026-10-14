from types import SimpleNamespace
from unittest.mock import Mock, call

from ouroboros.app.task_stop_notifications import create_task_stop_transport
from ouroboros.contracts.owner_scope import ServiceJob, service_identity
from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from ouroboros.storage.runtime_models import TaskEpisode
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.infra.repositories.working_retrieval.repo import SqlWorkingRetrievalRepository
from supervisor.interfaces.cleanup_checkpoint import CleanupCheckpoint
from supervisor.runtime import _maybe_cleanup_working_retrieval, build_supervisor_context
from tests.helpers.cleanup_checkpoint import sql_cleanup_checkpoint_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.sql_faults import fail_sql
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from tests.helpers.supervisor_owner_state import sql_owner_state_repository

NOW = "2026-07-02T00:00:00+00:00"
SCOPE = service_identity(job=ServiceJob.CONTEXT_CLEANUP).as_scope()
EMPTY_CLEANUP = {"task_episodes": 0, "tool_invocations": 0, "scratchpad_blocks": 0}


def _cleanup_context():
    checkpoints = Mock(wraps=sql_cleanup_checkpoint_repository())
    cleanup = Mock(wraps=SqlWorkingRetrievalRepository(runtime_persistence.current.engine, scratchpad=get_scratchpad()))
    operations = Mock()
    operations.attach_mock(checkpoints.get_last_run_at, "read")
    operations.attach_mock(checkpoints.save_checkpoint, "save")
    operations.attach_mock(cleanup.cleanup_expired_working_retrieval, "cleanup")
    return SimpleNamespace(working_retrieval_repository=cleanup, cleanup_checkpoint_repository=checkpoints), operations


def test_composed_supervisor_context_uses_its_checkpoint_repository(tmp_path) -> None:
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    publisher, _consumer = create_task_stop_transport()
    ctx = build_supervisor_context(
        settings={},
        runtime=runtime,
        bridge=runtime.bridge,
        request_restart=lambda: None,
        safe_restart=lambda: (True, "ok"),
        task_stop_publisher=publisher,
    )

    _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)

    assert ctx.cleanup_checkpoint_repository is runtime.cleanup_checkpoint_repository
    assert runtime.cleanup_checkpoint_repository.get_last_run_at(SCOPE) == NOW


def test_cleanup_reads_and_records_checkpoint_through_the_injected_repository(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS", "321")
    ctx, operations = _cleanup_context()

    _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)

    assert operations.mock_calls == [
        call.read(SCOPE),
        call.cleanup(now_iso=NOW, limit=321, cleanup_retrieval=True),
        call.save(CleanupCheckpoint(NOW, EMPTY_CLEANUP), SCOPE),
    ]
    assert sql_cleanup_checkpoint_repository().get_last_run_at(SCOPE) == NOW
    assert sql_owner_state_repository().load(SCOPE).payload["working_retrieval_cleanup"]["removed"] == EMPTY_CLEANUP


@parametrize_from_dict(
    {
        "recent": {"last_run_at": "2026-07-01T23:00:00+00:00", "due": False},
        "interval boundary": {"last_run_at": "2026-07-01T00:00:00+00:00", "due": True},
        "invalid timestamp": {"last_run_at": "invalid", "due": True},
        "future": {"last_run_at": "2026-07-03T00:00:00+00:00", "due": False},
    }
)
def test_cleanup_keeps_the_existing_interval_decision(monkeypatch, last_run_at: str, due: bool) -> None:
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_INTERVAL_HOURS", "24")
    sql_cleanup_checkpoint_repository().save_checkpoint(CleanupCheckpoint(last_run_at, {}), SCOPE)
    ctx, operations = _cleanup_context()

    _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)

    assert [item[0] for item in operations.mock_calls] == (["read", "cleanup", "save"] if due else ["read"])
    assert sql_cleanup_checkpoint_repository().get_last_run_at(SCOPE) == (NOW if due else last_run_at)


def test_retrieval_disabled_still_runs_scratchpad_cleanup(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_ENABLED", "false")
    ctx, operations = _cleanup_context()

    _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)

    assert (
        ctx.working_retrieval_repository.cleanup_expired_working_retrieval.call_args.kwargs["cleanup_retrieval"]
        is False
    )
    assert [item[0] for item in operations.mock_calls] == ["read", "cleanup", "save"]
    assert sql_cleanup_checkpoint_repository().get_last_run_at(SCOPE) == NOW


@parametrize_from_dict(
    {
        "read failure": {"statement": "SELECT payload_json FROM owner_runtime_state", "expected": ["read"]},
        "cleanup failure": {"statement": "FROM task_episodes", "expected": ["read", "cleanup"]},
        "save failure": {"statement": "INSERT INTO owner_runtime_state", "expected": ["read", "cleanup", "save"]},
    }
)
def test_cleanup_failure_is_deferred_without_recording_success(statement: str, expected: list[str], caplog) -> None:
    ctx, operations = _cleanup_context()

    with fail_sql(statement, OSError("unavailable")):
        _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)

    assert [item[0] for item in operations.mock_calls] == expected
    assert sql_cleanup_checkpoint_repository().get_last_run_at(SCOPE) == ""
    assert "Working-memory cleanup deferred after error" in caplog.text


def test_cleanup_drains_multiple_batches_before_checkpoint(monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS", "2")
    monkeypatch.setattr("supervisor.runtime._WORKING_CLEANUP_TICK_BUDGET_SEC", 60)
    ctx, _ = _cleanup_context()
    batches = [{**EMPTY_CLEANUP, "task_episodes": count} for count in (2, 1, 0)]
    ctx.working_retrieval_repository.cleanup_expired_working_retrieval.side_effect = batches

    _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)

    assert ctx.working_retrieval_repository.cleanup_expired_working_retrieval.call_count == 3
    ctx.cleanup_checkpoint_repository.save_checkpoint.assert_called_once_with(
        CleanupCheckpoint(NOW, {**EMPTY_CLEANUP, "task_episodes": 3}), SCOPE
    )


def test_cleanup_budget_leaves_backlog_due_for_next_tick(monkeypatch):
    monkeypatch.setattr("supervisor.runtime._WORKING_CLEANUP_TICK_BUDGET_SEC", 0)
    ctx, _ = _cleanup_context()
    ctx.working_retrieval_repository.cleanup_expired_working_retrieval.side_effect = [
        {**EMPTY_CLEANUP, "task_episodes": 2},
        EMPTY_CLEANUP,
    ]

    _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)
    assert sql_cleanup_checkpoint_repository().get_last_run_at(SCOPE) == ""
    _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)
    assert sql_cleanup_checkpoint_repository().get_last_run_at(SCOPE) == NOW


def test_cleanup_skips_a_pass_claimed_by_another_replica():
    ctx, operations = _cleanup_context()
    with sql_cleanup_checkpoint_repository().try_claim() as claimed:
        assert claimed
        _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)
    assert operations.mock_calls == []


def test_cleanup_drains_postgres_backlog_larger_than_batch(monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS", "2")
    monkeypatch.setattr("supervisor.runtime._WORKING_CLEANUP_TICK_BUDGET_SEC", 60)
    memory = runtime_persistence.current.memory_repo
    for i in range(5):
        memory.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id=f"expired-{i}", expires_at="2025"))
    memory.record_task_episode(TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="live", expires_at="2027"))
    ctx, _ = _cleanup_context()

    _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)

    assert [item.episode_id for item in memory.list_task_episodes(scope=TEST_SCOPE)] == ["live"]
    assert ctx.working_retrieval_repository.cleanup_expired_working_retrieval.call_count == 4
    assert sql_cleanup_checkpoint_repository().get_last_run_at(SCOPE) == NOW


def test_cleanup_failure_after_a_batch_leaves_checkpoint_due_and_releases_claim():
    ctx, _ = _cleanup_context()
    ctx.working_retrieval_repository.cleanup_expired_working_retrieval.side_effect = [
        {**EMPTY_CLEANUP, "task_episodes": 2},
        OSError("next batch failed"),
    ]

    _maybe_cleanup_working_retrieval(ctx, now_iso=NOW)

    assert sql_cleanup_checkpoint_repository().get_last_run_at(SCOPE) == ""
    with sql_cleanup_checkpoint_repository().try_claim() as recovered:
        assert recovered
