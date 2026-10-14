from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from infra.sql_transaction import SqlTransactionManager
from ouroboros.observability.infra.repositories.audit_events.repository import SqlAuditEventRepository
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.runtime_models import AuditEvent
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.tool_confirmation.repository import SqlToolConfirmationRepository
from tests.helpers.runtime_store import claim_task, make_tool_confirmation
from tests.helpers.shared import TEST_SCOPE


def _repositories():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    audit = SqlAuditEventRepository(transaction_manager)
    return SqlToolConfirmationRepository(transaction_manager, audit_event_repository=audit), audit


def _park(repository, task_id="task1"):
    return repository.park_and_open(
        task_id=task_id,
        claim_token="",
        confirmation=make_tool_confirmation(task_id=task_id),
        snapshot={"round_idx": 1},
        workspace="workspace",
        arguments={},
    )


def test_conflict_returns_none_and_rolls_back_only_its_own_parking():
    store = runtime_persistence.current
    claim_task(store)
    repository, audit = _repositories()
    first = _park(repository)
    claim_task(store, "task2")
    with repository.transaction:
        audit.append_audit_event(AuditEvent(event_id="before", event_type="outer.before"))
        assert _park(repository, "task2") is None
        audit.append_audit_event(AuditEvent(event_id="after", event_type="outer.after"))
    task = store.tasks_repo.get_task("task2")
    assert task.status == "running"
    assert "parked_workspace" not in task.metadata
    assert repository.read_open_tool_confirmation(TEST_SCOPE) == first
    assert {item.event_type for item in audit.list_audit_events()} == {
        "outer.before",
        "outer.after",
        "policy.tool_confirmation_requested",
    }


def test_failed_audit_rolls_back_parking_and_outer_transaction_can_continue():
    store = runtime_persistence.current
    claim_task(store)
    repository, audit = _repositories()

    def reject_audit(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO audit_events" in statement:
            conn.exec_driver_sql("SELECT 1/0")

    with repository.transaction:
        event.listen(store.engine, "before_cursor_execute", reject_audit)
        try:
            with pytest.raises(DBAPIError, match="division by zero"):
                _park(repository)
        finally:
            event.remove(store.engine, "before_cursor_execute", reject_audit)
        assert repository.read_open_tool_confirmation(TEST_SCOPE) is None
        audit.append_audit_event(AuditEvent(event_id="after", event_type="outer.after"))
    assert store.tasks_repo.get_task("task1").status == "running"
    assert [item.event_type for item in audit.list_audit_events()] == ["outer.after"]


def test_concurrent_snapshot_handoff_has_one_winner():
    store = runtime_persistence.current
    claim_task(store)
    repository, _ = _repositories()
    _park(repository)
    barrier = Barrier(2)

    def take():
        barrier.wait(timeout=5)
        return repository.take_parked_snapshot("task1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(take) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results.count({"round_idx": 1}) == 1
    assert results.count(None) == 1


def test_expired_detached_claim_cannot_park_or_write_audit():
    store = runtime_persistence.current
    task = claim_task(store)
    with store.engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET detached=true, heartbeat_at='2000-01-01T00:00:00+00:00' WHERE task_id='task1'")
        )
    repository, audit = _repositories()
    assert (
        repository.park_and_open(
            task_id="task1",
            claim_token=task.metadata["execution_claim"],
            confirmation=make_tool_confirmation(),
            snapshot={},
            workspace="",
            arguments={},
        )
        is None
    )
    assert store.tasks_repo.get_task("task1").status == "running"
    assert audit.list_audit_events() == []


def test_injected_audit_failure_rolls_back_confirmation_and_task():
    store = runtime_persistence.current
    claim_task(store)
    audit = Mock(spec=IAuditEventRepository)
    audit.append_audit_event.side_effect = OSError("audit unavailable")
    repository = SqlToolConfirmationRepository(
        runtime_persistence.current.transaction_manager, audit_event_repository=audit
    )

    with pytest.raises(OSError, match="audit unavailable"):
        _park(repository)

    assert store.tasks_repo.get_task("task1").status == "running"
    assert repository.read_open_tool_confirmation(TEST_SCOPE) is None
    assert audit.append_audit_event.call_args.args[0].event_type == "policy.tool_confirmation_requested"
