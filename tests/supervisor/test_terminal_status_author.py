"""A task's terminal status has one author, and it is the body that ran it.

The consumer of a run's reports records the echo and performs no status transition. That is
not an optimisation: once the consumer can be a different process, a status it writes is a
fact recorded by a component that did not witness it, arriving after the witness already
recorded it.
"""

from __future__ import annotations

from types import SimpleNamespace

from new_core.coordination.inprocess import NullWorkSignal
from ouroboros.agent_events.interfaces import AgentEvent, IAgentEventPublisher, OutboundText, TaskFinished
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.task_results import TERMINAL_STATUSES
from supervisor.queue import TaskQueue
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import (
    TEST_SCOPE,
    make_empty_extension_runtime,
    make_empty_skill_snapshot,
    make_skill_availability,
)
from tests.helpers.startup_usage import sql_startup_usage_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def _emit(tmp_path, task_id: str, *, text: str = "the answer") -> list[AgentEvent]:
    from ouroboros.agent.task_pipeline import emit_task_results

    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    pending: list[AgentEvent] = []
    emit_task_results(
        SimpleNamespace(drive_root=tmp_path),
        object(),
        pending,
        {"id": task_id, "type": "task", "chat_id": 7, "text": "do it"},
        text,
        {"rounds": 3, "prompt_tokens": 120, "completion_tokens": 45},
        {"tool_calls": []},
        0.0,
        tmp_path / "logs",
        scope=TEST_SCOPE,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    return pending


def test_the_record_is_terminal_before_the_run_reports_it(tmp_path) -> None:
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="t1", type="task", text="do it", scope=TEST_SCOPE.to_dict()), status="running"
    )

    pending = _emit(tmp_path, "t1")

    # The report exists, and the record was already closed when it was appended.
    from ouroboros.agent_events.interfaces import TaskMetrics

    assert [type(event) for event in pending] == [OutboundText, TaskMetrics, TaskFinished]
    assert store.tasks_repo.get_task("t1").status in TERMINAL_STATUSES


def test_the_counters_are_written_by_the_run_that_holds_them(tmp_path) -> None:
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="t1", type="task", text="do it", scope=TEST_SCOPE.to_dict()), status="running"
    )

    _emit(tmp_path, "t1")

    metadata = store.tasks_repo.get_task("t1").metadata
    assert metadata["total_rounds"] == 3
    assert metadata["prompt_tokens"] == 120
    assert metadata["completion_tokens"] == 45


def test_a_crashed_run_closes_its_own_record(tmp_path) -> None:
    """No consumer of its reports is required for a crash to end the task."""
    from ouroboros.agent_runtime.executor import TaskExecutor

    store = runtime_persistence.initialize()
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    queue.enqueue_dict({"id": "boom", "type": "task", "text": "go", "scope": TEST_SCOPE.to_dict()})
    record = queue.claim_next(worker_id=0, runtime_id="runtime-a")
    assert record is not None

    class _CrashingAgent:
        def handle_task(self, task, *, cancellation):
            raise RuntimeError("the body exploded")

    events = RecordingAgentEvents()
    executor = TaskExecutor(
        startup_usage_repository=sql_startup_usage_repository(),
        session_repository=sql_owner_session_repository(),
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_queue=queue,
        tasks_repo=store.tasks_repo,
        events=events,
        skill_snapshot=make_empty_skill_snapshot(),
        skill_availability=make_skill_availability(),
        extension_runtime=make_empty_extension_runtime(),
        runtime_id="runtime-a",
        work_signal=NullWorkSignal(),
        execution_memory_repository=sql_execution_memory_repository(),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        audit_event_repository=sql_audit_event_repository(),
    )
    executor.new_agent = lambda: _CrashingAgent()  # type: ignore[method-assign]

    executor._run_task(record, cancellation=None)

    closed = store.tasks_repo.get_task("boom")
    assert closed.status == "failed"
    assert "the body exploded" in closed.result
    # The echo is published too, and nothing depends on anybody reading it.
    assert [type(event) for event in events.published] == [TaskFinished]


def test_a_publish_that_raises_does_not_rewrite_a_completed_record(tmp_path) -> None:
    """The inverse direction: a report that cannot be published may not decide the run.

    In-process `publish` cannot raise — `queue.put` blocks. On a broker it is one
    connection blip away from a completed task being rewritten as failed by a component
    that did not witness it.
    """
    from ouroboros.agent_runtime.executor import TaskExecutor
    from ouroboros.storage.task_results import write_task_result

    store = runtime_persistence.initialize()
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    queue.enqueue_dict({"id": "t1", "type": "task", "text": "go", "scope": TEST_SCOPE.to_dict()})
    record = queue.claim_next(worker_id=0, runtime_id="runtime-a")
    assert record is not None

    class _FinishingAgent:
        """The body closes its own record, as the real pipeline does, and then reports."""

        def handle_task(self, task, *, cancellation):
            write_task_result(tmp_path, "t1", "completed", scope=TEST_SCOPE, result="the answer", total_rounds=1)
            return [OutboundText(scope=TEST_SCOPE, text="the answer", task_id="t1")]

    class _UnreachableBroker(IAgentEventPublisher):
        def publish(self, event: AgentEvent) -> None:
            raise ConnectionError("the broker is gone")

    executor = TaskExecutor(
        startup_usage_repository=sql_startup_usage_repository(),
        session_repository=sql_owner_session_repository(),
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_queue=queue,
        tasks_repo=store.tasks_repo,
        events=_UnreachableBroker(),
        skill_snapshot=make_empty_skill_snapshot(),
        skill_availability=make_skill_availability(),
        extension_runtime=make_empty_extension_runtime(),
        runtime_id="runtime-a",
        work_signal=NullWorkSignal(),
        execution_memory_repository=sql_execution_memory_repository(),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        audit_event_repository=sql_audit_event_repository(),
    )
    executor.new_agent = lambda: _FinishingAgent()  # type: ignore[method-assign]

    executor._run_task(record, cancellation=None)

    closed = store.tasks_repo.get_task("t1")
    assert closed.status == "completed"
    assert closed.result == "the answer"


def test_discarding_every_report_leaves_the_task_terminal_and_not_re_executed(tmp_path) -> None:
    store = runtime_persistence.initialize()
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    queue.enqueue_dict({"id": "t1", "type": "task", "text": "go", "scope": TEST_SCOPE.to_dict()})
    assert queue.claim_next(worker_id=0, runtime_id="runtime-a") is not None

    reports = _emit(tmp_path, "t1")
    reports.clear()  # every report from the completed run is lost

    record = store.tasks_repo.get_task("t1")
    assert record.status in TERMINAL_STATUSES
    assert record.result == "the answer"
    assert queue.claim_next(worker_id=0, runtime_id="runtime-a") is None


def test_a_failed_result_write_still_leaves_the_record_terminal(tmp_path, monkeypatch) -> None:
    """The one author has no second closer behind it, so a swallowed failure must not stand.

    The regression: `_store_task_result` caught everything and returned `""`. The owner had
    already been answered — the `OutboundText` is appended before the write — so the record
    stayed `running`, the lease sweep failed and requeued it two minutes later, and the
    whole task ran a second time.
    """
    from ouroboros.agent import task_pipeline

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="t1", type="task", text="do it", scope=TEST_SCOPE.to_dict()), status="running"
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("the database blinked")

    monkeypatch.setattr(task_pipeline, "write_task_result", _boom)

    pending = _emit(tmp_path, "t1")

    assert store.tasks_repo.get_task("t1").status in TERMINAL_STATUSES
    finished = [event for event in pending if isinstance(event, TaskFinished)]
    assert finished and finished[0].status in TERMINAL_STATUSES


def test_a_result_write_that_cannot_be_salvaged_reports_no_status(tmp_path, monkeypatch) -> None:
    """A genuinely dead store degrades to the lease sweep rather than inventing an outcome."""
    from ouroboros.agent import task_pipeline

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="t1", type="task", text="do it", scope=TEST_SCOPE.to_dict()), status="running"
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(task_pipeline, "write_task_result", _boom)
    monkeypatch.setattr(store.tasks_repo, "update_task_status", _boom)

    pending = _emit(tmp_path, "t1")

    finished = [event for event in pending if isinstance(event, TaskFinished)]
    assert finished and finished[0].status == ""
    assert store.tasks_repo.get_task("t1").status == "running"
