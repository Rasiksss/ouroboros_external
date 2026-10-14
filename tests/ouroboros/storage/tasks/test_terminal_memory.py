import pytest
from sqlalchemy import text

from ouroboros.observability.infra.repositories.audit_events.repository import SqlAuditEventRepository
from ouroboros.storage.runtime_models import AuditEvent, TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.shared import TEST_SCOPE


def test_completion_requires_filed_audit_and_never_clears_done():
    tasks = runtime_persistence.current.tasks_repo
    tasks.create_task(TaskRequest(id="job", detached=True, scope=TEST_SCOPE.to_dict()), status="completed")
    assert [task.task_id for task in tasks.list_terminal_memory()] == ["job"]
    assert not tasks.finish_terminal_memory("job", filed=True)
    assert tasks.list_terminal_memory() == []
    audit = SqlAuditEventRepository(runtime_persistence.current.transaction_manager)
    audit.append_audit_event(
        AuditEvent(event_id=f"memory-distillation:{TEST_SCOPE.owner_id}:job", event_type="memory-distillation")
    )
    assert not tasks.finish_terminal_memory("job", filed=False)
    assert tasks.finish_terminal_memory("job", filed=True)
    assert tasks.finish_terminal_memory("job", filed=False)
    assert tasks.get_task("job").metadata["terminal_memory_done"] is True
    assert tasks.list_terminal_memory() == []


def test_memory_marker_and_audit_roll_back_together():
    tasks = runtime_persistence.current.tasks_repo
    audit = SqlAuditEventRepository(runtime_persistence.current.transaction_manager)
    tasks.create_task(TaskRequest(id="job", detached=True, scope=TEST_SCOPE.to_dict()), status="completed")
    with pytest.raises(ValueError, match="abort"):
        with tasks.transaction:
            audit.append_audit_event(
                AuditEvent(event_id=f"memory-distillation:{TEST_SCOPE.owner_id}:job", event_type="memory-distillation")
            )
            assert tasks.finish_terminal_memory("job", filed=True)
            raise ValueError("abort")
    assert [task.task_id for task in tasks.list_terminal_memory()] == ["job"]
    assert audit.list_audit_events() == []
    assert not tasks.get_task("job").metadata.get("terminal_memory_done")


def test_superseded_attempt_cannot_be_marked_or_selected():
    tasks = runtime_persistence.current.tasks_repo
    tasks.create_task(
        TaskRequest(id="old", job_id="job", detached=True, scope=TEST_SCOPE.to_dict()), status="completed"
    )
    tasks.create_task(TaskRequest(id="new", detached=True, scope=TEST_SCOPE.to_dict()))
    with runtime_persistence.current.engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET job_id='job', lineage_ordinal=1 WHERE task_id='new'"))
    assert tasks.list_terminal_memory() == []
    assert not tasks.finish_terminal_memory("old", filed=True)
    assert not tasks.get_task("old").metadata.get("terminal_memory_after")
