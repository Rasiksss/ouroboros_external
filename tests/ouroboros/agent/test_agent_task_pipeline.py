from types import SimpleNamespace

import pytest

import ouroboros.agent.task_pipeline as pipeline
from ouroboros.agent_events.interfaces import LlmUsageReported, OutboundText, TaskFinished, TaskMetrics
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import TEST_SCOPE


def test_non_detached_stop_settles_a_child_persisted_after_the_tree_write(tmp_path) -> None:
    """A schedule call already in flight cannot publish a child after its parent stop committed."""
    from ouroboros.agent_events.interfaces import SubtaskRequested
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.tools.service.orchestration import _schedule_task
    from tests.helpers.shared import make_tool_ctx

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="parent", text="work", scope=TEST_SCOPE.to_dict()), status="running")
    store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="parent")
    ctx = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_id="parent",
        job_id="parent",
        root_task_id="parent",
    )
    assert "Task request queued" in _schedule_task(ctx, "late child")
    child_id = ctx.pending_events[0].task_id
    assert store.tasks_repo.get_task(child_id).status == "cancelled"

    pipeline.emit_task_results(
        SimpleNamespace(drive_root=tmp_path),
        None,
        ctx.pending_events,
        store.tasks_repo.get_task("parent").to_agent_input(),
        "",
        {},
        {"tool_calls": []},
        0,
        tmp_path,
        ctx=ctx,
        scope=TEST_SCOPE,
        has_answer=False,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert store.tasks_repo.get_task(child_id).status == "cancelled"
    assert not any(isinstance(event, SubtaskRequested) for event in ctx.pending_events)


@pytest.mark.parametrize("outcome", ["stopped", "superseded", "lost_completion", "recovered", "completed"])
def test_detached_parent_settles_unpublished_child_requests(tmp_path, outcome, monkeypatch):
    from ouroboros.agent_events.interfaces import SubtaskRequested
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import write_task_result
    from ouroboros.tools.service.orchestration import _schedule_task
    from tests.helpers.shared import make_tool_ctx

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="parent", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    ctx = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_id="parent",
        job_id="parent",
        root_task_id="parent",
        detached=True,
        execution_claim=claimed.metadata["execution_claim"],
    )
    store.tasks_repo.create_task(TaskRequest(id="another", scope=TEST_SCOPE.to_dict()), status="completed")
    assert "Task request queued" in _schedule_task(
        ctx, "child work", parent_task_id="another" if outcome == "recovered" else ""
    )
    child_id = ctx.pending_events[0].task_id
    write_task_result(
        tmp_path, "unrelated", "requested", scope=TEST_SCOPE, parent_task_id="another", root_task_id="another"
    )
    write_task_result(
        tmp_path,
        "child-with-other-root",
        "requested",
        scope=TEST_SCOPE,
        parent_task_id="parent",
        root_task_id="another",
    )
    requests = list(ctx.pending_events)
    if outcome in {"stopped", "recovered"}:
        store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="parent")
        assert store.tasks_repo.get_task("child-with-other-root").status == "cancelled"
        assert store.tasks_repo.get_task("unrelated").status == "requested"
    elif outcome in {"superseded", "lost_completion"}:
        store.tasks_repo.finish_detached_attempt(
            "parent",
            status="failed" if outcome == "superseded" else "completed",
            result="another verdict",
            claim_token=claimed.metadata["execution_claim"],
        )

    def emit():
        return pipeline.emit_task_results(
            SimpleNamespace(drive_root=tmp_path),
            None,
            ctx.pending_events,
            claimed.to_agent_input(),
            "done",
            {},
            {"tool_calls": []},
            0,
            tmp_path,
            ctx=ctx,
            scope=TEST_SCOPE,
            execution_memory_repository=sql_execution_memory_repository(),
            audit_event_repository=sql_audit_event_repository(),
        )

    if outcome == "recovered":
        from sqlalchemy import text

        from tests.helpers.executor import make_executor

        with monkeypatch.context() as patch:

            def fail(*args, **kwargs):
                raise OSError("completion database unavailable")

            patch.setattr(store.tasks_repo, "finish_detached_attempt", fail)
            from ouroboros.storage.runtime_backend import ExecutionClaimUnavailable

            with pytest.raises(ExecutionClaimUnavailable):
                emit()
        assert not ctx.pending_events
        with store.engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE task_id='parent'"))
        make_executor(tmp_path, store).task_queue.reclaim_stale_leases()
        assert store.tasks_repo.get_task(child_id).status == "cancelled"

    finished_at = []
    for _ in range(2 if outcome != "completed" else 1):
        ctx.pending_events[:] = requests
        emit()
        finished_at.append(store.tasks_repo.get_task(child_id).finished_at)
    child = store.tasks_repo.get_task(child_id)
    assert store.tasks_repo.get_task("unrelated").status == "requested"
    assert store.tasks_repo.get_task("child-with-other-root").status == (
        "cancelled" if outcome in {"stopped", "recovered"} else "requested"
    )
    if outcome == "completed":
        assert child.status == "requested"
        assert any(isinstance(event, SubtaskRequested) for event in ctx.pending_events)
    else:
        assert child.status == "cancelled"
        assert finished_at[0] == finished_at[1] and finished_at[0]
        assert not any(isinstance(event, SubtaskRequested) for event in ctx.pending_events)
        assert store.tasks_repo.claim_next_task(runtime_id="runtime") is None


def test_build_trace_summary_shows_structured_failure_facts():
    from ouroboros.utils import build_trace_summary

    trace = {
        "tool_calls": [
            {
                "tool": "run_shell",
                "args": {"cmd": ["npm", "install", "-g", "@anthropic-ai/claude-code"]},
                "result": "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=-9 (signal=SIGKILL).",
                "is_error": True,
                "status": "non_zero_exit",
                "exit_code": -9,
                "signal": "SIGKILL",
            }
        ],
        "reasoning_notes": ["Thought this might still work."],
    }

    summary = build_trace_summary(trace)

    assert "status=non_zero_exit" in summary
    assert "exit_code=-9" in summary
    assert "signal=SIGKILL" in summary
    assert "Agent notes (supplementary, not source of truth)" in summary


def test_store_task_result_persists_review_evidence(tmp_path):
    env = SimpleNamespace(drive_root=tmp_path)

    pipeline._store_task_result(
        env=env,
        task={"id": "task-store", "type": "task", "text": "hi"},
        text="done",
        usage={"rounds": 2},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        existing={},
        scope=TEST_SCOPE,
        review_evidence={"has_evidence": True, "open_obligations": [{"item": "tests_affected"}]},
    )

    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import load_task_result

    runtime_persistence.initialize()
    payload = load_task_result(tmp_path, "task-store")
    assert payload is not None
    assert payload["review_evidence"]["has_evidence"] is True
    assert payload["review_evidence"]["open_obligations"][0]["item"] == "tests_affected"


def test_store_task_result_preserves_failed_status(tmp_path):
    """A body finishing after the record went terminal records its trace, not its verdict.

    This write door bypasses the store's absorbing-status guard, so it applies the rule itself.
    """
    from ouroboros.storage.task_results import STATUS_FAILED, load_task_result, write_task_result

    env = SimpleNamespace(drive_root=tmp_path)
    write_task_result(tmp_path, "task-failed", STATUS_FAILED, scope=TEST_SCOPE, result="initial failure")
    closed = load_task_result(tmp_path, "task-failed")

    pipeline._store_task_result(
        env=env,
        task={"id": "task-failed", "type": "task", "text": "hi"},
        text="final failure reply",
        usage={"rounds": 1},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        existing=closed,
        scope=TEST_SCOPE,
        review_evidence={},
    )

    payload = load_task_result(tmp_path, "task-failed")
    assert payload is not None
    assert payload["status"] == STATUS_FAILED
    assert payload["result"] == "initial failure"
    assert payload["trace_summary"]
    # The completion time is the one the supervisor recorded, not the late body's return.
    assert payload["ts"] == closed["ts"]


def test_store_task_result_rereads_the_record_before_writing(tmp_path):
    """The caller's snapshot is taken before publication; the sweep may have closed the record since."""
    from ouroboros.storage.task_results import STATUS_FAILED, STATUS_RUNNING, load_task_result, write_task_result

    env = SimpleNamespace(drive_root=tmp_path)
    write_task_result(tmp_path, "task-late", STATUS_RUNNING, scope=TEST_SCOPE, result="Task is running.")
    stale = load_task_result(tmp_path, "task-late")
    write_task_result(
        tmp_path, "task-late", STATUS_FAILED, scope=TEST_SCOPE, result="Task killed by hard timeout after 1800s."
    )

    pipeline._store_task_result(
        env=env,
        task={"id": "task-late", "type": "task", "text": "hi"},
        text="the answer",
        usage={"rounds": 1},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        existing=stale,
        scope=TEST_SCOPE,
        review_evidence={},
    )

    payload = load_task_result(tmp_path, "task-late")
    assert payload is not None
    assert payload["status"] == STATUS_FAILED
    assert payload["result"] == "Task killed by hard timeout after 1800s."
    # The write still lands as the same status, so the late body's trace is kept.
    assert payload["trace_summary"]


def test_emit_task_results_reports_an_unsuccessful_task_and_writes_no_memory_row(tmp_path):
    """The verdict the caller acts on, plus the row that no longer exists.

    ``emit_task_results`` used to enqueue distillation itself; now it only reports
    whether the task ended badly, and the adapter decides what to do with that.
    """
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import STATUS_FAILED, write_task_result

    runtime_persistence.initialize()
    write_task_result(tmp_path, "task-failed", STATUS_FAILED, scope=TEST_SCOPE, result="initial failure")
    env = SimpleNamespace(drive_root=tmp_path)
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    unsuccessful = pipeline.emit_task_results(
        env=env,
        llm=object(),
        pending_events=[],
        task={"id": "task-failed", "type": "task", "chat_id": 1, "text": "do it"},
        text="⚠️ Error during processing: RuntimeError: " + "x" * 120,
        usage={"rounds": 1},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=0.0,
        drive_logs=drive_logs,
        scope=TEST_SCOPE,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert unsuccessful is True
    store = runtime_persistence.initialize()
    assert [r for r in store.tasks_repo.list_tasks() if r.task_type == "memory_distillation"] == []


def test_a_stop_that_wins_completion_suppresses_the_late_answer(tmp_path):
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import STATUS_CANCELLED, STATUS_RUNNING, write_task_result

    store = runtime_persistence.initialize()
    write_task_result(tmp_path, "task-stopped", STATUS_RUNNING, scope=TEST_SCOPE, result="Task is running.")
    assert store.tasks_repo.request_task_stop("task-stopped", scope=TEST_SCOPE)
    pending = []

    unsuccessful = pipeline.emit_task_results(
        env=SimpleNamespace(drive_root=tmp_path),
        llm=object(),
        pending_events=pending,
        task={"id": "task-stopped", "type": "task", "chat_id": TEST_SCOPE.chat_id, "text": "do it"},
        text="too late",
        usage={"rounds": 1},
        llm_trace={"tool_calls": []},
        start_time=0.0,
        drive_logs=tmp_path,
        scope=TEST_SCOPE,
        has_answer=True,
        stopped_externally=True,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert unsuccessful is True
    assert store.tasks_repo.get_task("task-stopped").status == STATUS_CANCELLED
    assert not any(isinstance(event, OutboundText) for event in pending)
    assert next(event for event in pending if isinstance(event, TaskFinished)).result == "Task cancelled."


def test_observed_foreground_stop_suppresses_pending_effects_when_result_storage_fails(tmp_path, monkeypatch):
    from ouroboros.agent_events.interfaces import SubtaskRequested, TaskCancellationRequested
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import STATUS_RUNNING, write_task_result

    store = runtime_persistence.initialize()
    write_task_result(tmp_path, "parent", STATUS_RUNNING, scope=TEST_SCOPE, result="Task is running.")
    store.tasks_repo.create_task(TaskRequest(id="child", scope=TEST_SCOPE.to_dict()), status="requested")
    pending = [
        OutboundText(scope=TEST_SCOPE, text="must not send"),
        SubtaskRequested(scope=TEST_SCOPE, task_id="child"),
        TaskCancellationRequested(scope=TEST_SCOPE, task_id="unrelated"),
        LlmUsageReported(scope=TEST_SCOPE, task_id="parent", model="model"),
    ]
    monkeypatch.setattr(pipeline, "_store_task_result", lambda *_args, **_kwargs: "")

    unsuccessful = pipeline.emit_task_results(
        env=SimpleNamespace(drive_root=tmp_path),
        llm=object(),
        pending_events=pending,
        task={"id": "parent", "type": "task", "chat_id": TEST_SCOPE.chat_id, "text": "do it"},
        text="too late",
        usage={},
        llm_trace={"tool_calls": []},
        start_time=0.0,
        drive_logs=tmp_path,
        scope=TEST_SCOPE,
        stop_observed=True,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert unsuccessful is True
    assert store.tasks_repo.get_task("child").status == "cancelled"
    assert not any(isinstance(event, (OutboundText, SubtaskRequested, TaskCancellationRequested)) for event in pending)
    assert any(isinstance(event, LlmUsageReported) for event in pending)
    assert [type(event) for event in pending[-2:]] == [TaskMetrics, TaskFinished]


@pytest.mark.parametrize("stopped", [False, True])
def test_detached_result_echo_contains_the_persisted_body(tmp_path, stopped):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job-result", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    assert claimed is not None
    if stopped:
        store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id=claimed.job_id)
    pending = [OutboundText(scope=TEST_SCOPE, text="Proactive result", task_id="job-result", is_progress=False)]

    unsuccessful = pipeline.emit_task_results(
        env=SimpleNamespace(drive_root=tmp_path),
        llm=object(),
        pending_events=pending,
        task=claimed.to_agent_input(),
        text="done",
        usage={"rounds": 1},
        llm_trace={"tool_calls": []},
        start_time=0.0,
        drive_logs=tmp_path,
        scope=TEST_SCOPE,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert unsuccessful is stopped
    assert not any(isinstance(event, OutboundText) for event in pending)
    assert [type(event) for event in pending] == [TaskMetrics, TaskFinished]
    stored = store.tasks_repo.get_task(claimed.task_id)
    assert (pending[-1].status, pending[-1].result) == (stored.status, stored.result)


@pytest.mark.parametrize("failing_call", ["get_task", "finish_detached_attempt"])
def test_detached_result_purges_direct_text_before_store_failure(tmp_path, monkeypatch, failing_call):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="job-store-error", detached=True, text="work", scope=TEST_SCOPE.to_dict())
    )
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    assert claimed is not None
    retained = [TaskMetrics(scope=TEST_SCOPE)]
    pending = [OutboundText(scope=TEST_SCOPE, text="must not leak"), TaskFinished(scope=TEST_SCOPE), *retained]

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"{failing_call} failed")

    monkeypatch.setattr(store.tasks_repo, failing_call, fail)

    with pytest.raises(RuntimeError, match=failing_call):
        pipeline.emit_task_results(
            env=SimpleNamespace(drive_root=tmp_path),
            llm=object(),
            pending_events=pending,
            task=claimed.to_agent_input(),
            text="done",
            usage={},
            llm_trace={"tool_calls": []},
            start_time=0.0,
            drive_logs=tmp_path,
            scope=TEST_SCOPE,
            execution_memory_repository=sql_execution_memory_repository(),
            audit_event_repository=sql_audit_event_repository(),
        )

    assert pending == retained


def test_detached_completion_preserves_trace_across_an_intervening_heartbeat(tmp_path, monkeypatch):
    """Terminal reconciliation must never observe a completed row without its trace inputs."""
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job-trace", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    assert claimed is not None

    original_finish = store.tasks_repo.finish_detached_attempt

    def finish_with_stale_heartbeat(*args, **kwargs):
        store.tasks_repo.heartbeat_task("job-trace", phase="running", claim_token=claimed.metadata["execution_claim"])
        record = original_finish(*args, **kwargs)
        assert record is not None
        assert record.metadata["llm_trace"] == {"tool_calls": [{"tool": "lookup"}]}
        assert record.metadata["trace_summary"] == "## Tool trace (1 calls, 0 errors)\n1. lookup()"
        assert record.metadata["episode_search_terms"] == ["needle"]
        return record

    monkeypatch.setattr(store.tasks_repo, "finish_detached_attempt", finish_with_stale_heartbeat)
    monkeypatch.setattr(pipeline, "episode_search_terms", lambda *_args: ["needle"])
    pipeline.emit_task_results(
        env=SimpleNamespace(drive_root=tmp_path),
        llm=object(),
        pending_events=[],
        task=claimed.to_agent_input(),
        text="done",
        usage={},
        llm_trace={"tool_calls": [{"tool": "lookup"}]},
        start_time=0.0,
        drive_logs=tmp_path,
        scope=TEST_SCOPE,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )


def test_truncate_review_artifact_never_clips_silently():
    """Truncation is always visible: a note names the limit and the original length."""
    from ouroboros.utils import truncate_review_artifact

    short = "hello"
    assert truncate_review_artifact(short, 100) == short

    long_text = "x" * 200
    result = truncate_review_artifact(long_text, 50)
    assert result.startswith("x" * 50)
    assert "50" in result  # omission note mentions limit
    assert len(result) > 50  # note appended, not just raw slice

    # Handles None gracefully
    assert truncate_review_artifact(None, 10) == ""


@pytest.mark.parametrize("backend", ["worker_schema", pytest.param("postgresql", marks=pytest.mark.integration)])
@pytest.mark.parametrize("order", ["parent_first", "child_first", "child_committed", "parent_completed"])
@pytest.mark.parametrize("reparent", [False, True])
def test_late_child_write_cannot_survive_its_creators_closure(tmp_path, monkeypatch, reparent, order, backend, request):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event

    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import create_runtime_persistence, runtime_persistence
    from ouroboros.tools.service.orchestration import _schedule_task
    from tests.helpers.shared import make_tool_ctx

    scope = TEST_SCOPE
    store = runtime_persistence.initialize()
    if backend == "postgresql":
        urls, scope = request.getfixturevalue("postgres_task_scope")
        store = create_runtime_persistence(configured_urls=urls)
        monkeypatch.setattr(
            runtime_persistence,
            "_persistence",
            store,
        )
    store.tasks_repo.create_task(TaskRequest(id="creator", detached=True, scope=scope.to_dict()))
    held = store.tasks_repo.claim_next_task(runtime_id="runtime")
    store.tasks_repo.create_task(TaskRequest(id="structural", scope=scope.to_dict()), status="completed")
    ctx = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_id=held.task_id,
        scope=scope,
        root_task_id=held.task_id,
        detached=True,
        execution_claim=held.metadata["execution_claim"],
    )
    store.tasks_repo.create_task(
        TaskRequest(id="accepted", parent_task_id=held.task_id, scope=scope.to_dict()), status="scheduled"
    )
    entered, resume, closing = Event(), Event(), Event()
    from ouroboros.storage.tasks import repository as task_repository

    params = task_repository._task_params

    def delay_write(task):
        if task.status == "requested":
            entered.set()
            assert resume.wait(5)
        return params(task)

    def delay_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO tasks"):
            entered.set()
            assert resume.wait(5)

    def close_parent():
        closing.set()
        return store.tasks_repo.finish_detached_attempt(
            held.task_id, status="cancelled", claim_token=ctx.execution_claim
        )

    def emit():
        pipeline.emit_task_results(
            SimpleNamespace(drive_root=tmp_path),
            None,
            ctx.pending_events,
            held.to_agent_input(),
            "late",
            {},
            {"tool_calls": []},
            0,
            tmp_path,
            ctx=ctx,
            scope=scope,
            execution_memory_repository=sql_execution_memory_repository(),
            audit_event_repository=sql_audit_event_repository(),
        )

    finish = store.tasks_repo.finish_detached_attempt

    def finish_before_child(*args, **kwargs):
        closed = finish(*args, **kwargs)
        resume.set()
        assert "Task request queued" not in result.result(timeout=5)
        return closed

    upsert = store.tasks_repo.upsert_task

    def delay_return(task, **kwargs):
        saved = upsert(task, **kwargs)
        entered.set()
        assert resume.wait(5)
        return saved

    if order in {"parent_first", "parent_completed"}:
        monkeypatch.setattr(task_repository, "_task_params", delay_write)
    elif order == "child_committed":
        monkeypatch.setattr(store.tasks_repo, "upsert_task", delay_return)
    else:
        event.listen(store.engine, "before_cursor_execute", delay_insert)
    with ThreadPoolExecutor(2) as pool:
        result = pool.submit(_schedule_task, ctx, "late child", parent_task_id="structural" if reparent else "")
        try:
            assert entered.wait(5)
            if order == "parent_completed":
                monkeypatch.setattr(store.tasks_repo, "finish_detached_attempt", finish_before_child)
                emit()
            elif order != "child_first":
                assert close_parent()
                ctx.pending_events.clear()
            else:
                from concurrent.futures import TimeoutError

                closed = pool.submit(close_parent)
                assert closing.wait(5)
                with pytest.raises(TimeoutError):
                    closed.result(timeout=0.2)
        finally:
            resume.set()
        answer = result.result(timeout=5)
        if order in {"parent_first", "parent_completed"}:
            assert "Task request queued" not in answer
            if order == "parent_first":
                assert not ctx.pending_events
        elif order == "child_first":
            assert closed.result(timeout=5)
            event.remove(store.engine, "before_cursor_execute", delay_insert)
            emit()
    from ouroboros.agent_events.interfaces import SubtaskRequested

    assert not any(isinstance(item, SubtaskRequested) for item in ctx.pending_events)
    assert store.tasks_repo.list_tasks(scope=scope, statuses=["requested"]) == []
    assert store.tasks_repo.get_task("accepted").status == "scheduled"
