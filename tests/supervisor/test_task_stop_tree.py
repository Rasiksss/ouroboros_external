from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import text

from new_core.coordination.inprocess import NullWorkSignal
from ouroboros.agent_events.interfaces import SubtaskRequested
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.queue import TaskQueue
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx


def test_named_stop_cancels_a_real_request_before_supervisor_acceptance(tmp_path) -> None:
    """A completed parent's unaccepted schedule request remains part of its stop tree."""
    from ouroboros.tools.service.orchestration import _schedule_task

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root", text="root", scope=TEST_SCOPE.to_dict()), status="running")
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, task_id="root", root_task_id="root")
    _schedule_task(ctx, "child")
    event = next(event for event in ctx.pending_events if isinstance(event, SubtaskRequested))
    store.tasks_repo.complete_task("root", result="Child requested.")

    store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="root")
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    accepted = queue.enqueue(
        TaskRequest(
            id=event.task_id,
            parent_task_id=event.parent_task_id,
            text=event.description,
            scope=event.scope.to_dict(),
        )
    )

    assert accepted.parent_task_id == "root"
    assert accepted.status == "cancelled"
    assert store.tasks_repo.get_task("root").status == "completed"
    assert queue.claim_next(runtime_id="test") is None


def test_a_stale_requested_read_cannot_requeue_a_cancelled_child(tmp_path, monkeypatch) -> None:
    """A second supervisor's stale read loses to the tree stop already committed in the database."""
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root", text="root", scope=TEST_SCOPE.to_dict()), status="running")
    request = TaskRequest(id="child", parent_task_id="root", text="child", scope=TEST_SCOPE.to_dict())
    stale = store.tasks_repo.create_task(request, status="requested")
    store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="root")
    stored_get = store.tasks_repo.get_task
    monkeypatch.setattr(
        store.tasks_repo, "get_task", lambda task_id: stale if task_id == "child" else stored_get(task_id)
    )
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )

    winner = queue.enqueue(request)

    assert winner.status == "cancelled"
    assert stored_get("child").status == "cancelled"


def test_late_requested_child_is_cancelled_when_recovery_retries_its_acceptance(tmp_path) -> None:
    """A cleanup outage cannot let a persisted child start after its parent was stopped."""

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root", text="root", scope=TEST_SCOPE.to_dict()), status="running")
    store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="root")
    child = TaskRequest(id="child", parent_task_id="root", text="child", scope=TEST_SCOPE.to_dict())
    store.tasks_repo.create_task(TaskRequest(id="child", text="child", scope=TEST_SCOPE.to_dict()), status="requested")
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET parent_task_id='root' WHERE task_id='child'"))

    accepted = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    ).enqueue(child)

    assert accepted.status == "cancelled"
    assert store.tasks_repo.get_task("child").status == "cancelled"
    assert (
        TaskQueue(
            store.tasks_repo,
            tmp_path,
            work_signal=NullWorkSignal(),
            audit_event_repository=sql_audit_event_repository(),
        ).claim_next(runtime_id="test")
        is None
    )


def test_a_stale_subtask_event_does_not_acknowledge_a_cancelled_child(tmp_path, monkeypatch) -> None:
    from supervisor import events

    monkeypatch.setattr(events, "_find_duplicate_task", lambda *_args, **_kwargs: None)
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root", text="root", scope=TEST_SCOPE.to_dict()), status="running")
    request = TaskRequest(id="child", parent_task_id="root", text="child", scope=TEST_SCOPE.to_dict())
    stale = store.tasks_repo.create_task(request, status="requested")
    store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="root")
    stored_get = store.tasks_repo.get_task
    monkeypatch.setattr(
        store.tasks_repo, "get_task", lambda task_id: stale if task_id == "child" else stored_get(task_id)
    )
    sent: list[str] = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        enqueue_task=TaskQueue(
            store.tasks_repo,
            tmp_path,
            work_signal=NullWorkSignal(),
            audit_event_repository=sql_audit_event_repository(),
        ).enqueue_dict,
        send_with_budget=lambda message, **_kwargs: sent.append(message),
    )

    events._handle_schedule_task(
        SubtaskRequested(
            scope=TEST_SCOPE,
            task_id="child",
            description="child",
            parent_task_id="root",
            root_task_id="root",
            source="browser",
        ),
        ctx,
    )

    assert stored_get("child").status == "cancelled"
    assert sent == ["🛑 Task child was not scheduled because its parent was stopped."]


@parametrize_from_dict(
    {
        "named_running": {"job_id": "root-1", "successor_completed": False},
        "all_running": {"job_id": None, "successor_completed": False},
        "all_completed": {"job_id": None, "successor_completed": True},
    }
)
def test_stop_after_retry_cancels_children_of_the_old_attempt(tmp_path, job_id, successor_completed) -> None:
    """Stopping a job includes old-attempt children even after its current attempt finishes."""
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root-1", text="root", scope=TEST_SCOPE.to_dict()), status="running")
    store.tasks_repo.create_task(
        TaskRequest(id="child-of-old", parent_task_id="root-1", text="child", scope=TEST_SCOPE.to_dict()),
        status="running",
    )
    successor = TaskRequest.model_validate(
        {
            **store.tasks_repo.get_task("root-1").to_agent_input(),
            "id": uuid.uuid5(uuid.NAMESPACE_URL, "task-successor:root-1").hex,
            "original_task_id": "root-1",
            "_attempt": 1,
            "timeout_retry_from": "root-1",
        }
    )
    store.tasks_repo.transition_running(
        "root-1",
        kind="hard_timeout",
        result="Task killed by hard timeout after 60s.",
        attempt=1,
        successor=successor,
        hard_timeout_sec=0,
    )

    if successor_completed:
        store.tasks_repo.complete_task(successor.id, result="done")
    store.tasks_repo.create_task(
        TaskRequest(id="unrelated", text="other", scope=TEST_SCOPE.to_dict()), status="completed"
    )

    stopped = store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id=job_id)

    child = store.tasks_repo.get_task("child-of-old")
    assert child.stop_requested_at is not None
    assert child.status == "running"
    assert any(row.task_id == "child-of-old" for row in stopped)
    assert stopped[0].task_id == successor.id
    assert all(row.task_id != "unrelated" for row in stopped)
