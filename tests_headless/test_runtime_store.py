from __future__ import annotations

import contextlib

import pytest

from infra.migrations.runner import schema_version
from new_core.coordination.inprocess import NullWorkSignal
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.owner_scope import configured_agent_id, configured_owner_id
from ouroboros.storage import task_transitions
from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.interfaces.owner_state import OwnerStateDocument
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.conversations import make_sql_runtime_persistence, sql_conversation_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.runtime_store import claim_task, make_tool_confirmation, park_run
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader
from transport.browser.infra.repositories.owner_history.repo import SqlOwnerHistoryRepository
from transport.dialogs.infra.repositories.repo import SqlDialogsCursorRepository


def _scope(source: str, chat_id: int | str) -> OwnerScope:
    """The scope a transport would build for a message arriving on ``source``."""
    return OwnerScope(
        owner_id=configured_owner_id(),
        agent_id=configured_agent_id(),
        surface_id=SurfaceId(source),
        chat_id=str(chat_id),
    )


#: The scope every row in this module is written and read under.
TEST_SCOPE = _scope("browser", 1)


def test_legacy_task_models_derive_the_logical_job_identity() -> None:
    from ouroboros.storage.runtime_models import TaskRecord, TaskRequest

    request = TaskRequest(id="legacy-task", text="work", scope=TEST_SCOPE.to_dict())
    record = TaskRecord(task_id="legacy-task", owner_id="owner", agent_id="agent")

    assert request.job_id == "legacy-task"
    assert record.job_id == "legacy-task"
    assert request.detached is False
    assert record.detached is False
    assert request.to_task_dict()["job_id"] == "legacy-task"


def test_runtime_store_task_lifecycle(tmp_path) -> None:
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(
            id="task1",
            type="task",
            chat_id=42,
            text="Do it",
            priority=2,
            source="dialogs",
            scope=TEST_SCOPE.to_dict(),
        )
    )

    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="test-runtime")
    assert claimed is not None
    assert claimed.task_id == "task1"
    assert claimed.status == "running"
    assert claimed.input["text"] == "Do it"

    store.tasks_repo.heartbeat_task("task1", phase="running")
    completed = store.tasks_repo.complete_task("task1", result="done", duration_sec=0.1)

    assert completed is not None
    assert completed.status == "completed"
    assert completed.result == "done"
    assert completed.metadata["duration_sec"] == 0.1


def _admission_anchor(store):
    from ouroboros.storage.runtime_models import ConversationMessage

    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(message_id="anchor", dedup_key="anchor", **TEST_SCOPE.to_dict()), scope=TEST_SCOPE
    )
    return "anchor"


def test_root_admission_is_idempotent_and_owner_quota_is_atomic(tmp_path) -> None:
    import threading

    from ouroboros.storage.runtime_backend import TaskIdentityConflictError
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    original = TaskRequest(
        id="stable",
        job_id="stable",
        detached=True,
        through_message_id=_admission_anchor(store),
        text="same",
        attachment_ids=["a", "b"],
        scope=TEST_SCOPE.to_dict(),
    )
    assert (
        runtime_persistence.current.create_root_task.execute(original, enabled=True, max_active=1).task_id == "stable"
    )
    assert (
        runtime_persistence.current.create_root_task.execute(original, enabled=False, max_active=1).task_id == "stable"
    )
    with pytest.raises(TaskIdentityConflictError):
        runtime_persistence.current.create_root_task.execute(
            original.model_copy(update={"text": "changed"}), enabled=False, max_active=1
        )

    outcomes = []
    barrier = threading.Barrier(2)

    def admit(task_id: str) -> None:
        barrier.wait()
        outcomes.append(
            runtime_persistence.current.create_root_task.execute(
                TaskRequest(
                    id=task_id, detached=True, through_message_id="anchor", text=task_id, scope=TEST_SCOPE.to_dict()
                ),
                enabled=True,
                max_active=2,
            )
        )

    threads = [threading.Thread(target=admit, args=(name,)) for name in ("next-a", "next-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(row.status for row in outcomes) == ["queued", "rejected_duplicate"]
    assert [row.error_code for row in outcomes if row.status == "rejected_duplicate"] == ["owner_active_job_limit"]


def test_legacy_rows_written_after_migration_keep_distinct_logical_job_identity(tmp_path) -> None:
    from sqlalchemy import text

    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    with store.engine.begin() as conn:
        for index, task_id in enumerate(("legacy-a", "legacy-b")):
            conn.execute(
                text(
                    "INSERT INTO tasks (task_id, owner_id, agent_id, status, task_type, input_json, "
                    "metadata_json, payload_json, created_at) VALUES "
                    "(:task_id, :owner_id, :agent_id, 'queued', 'task', :input_json, '{}', '{}', :created_at)"
                ),
                {
                    "task_id": task_id,
                    "owner_id": TEST_SCOPE.owner_id,
                    "agent_id": TEST_SCOPE.agent_id,
                    "input_json": '{"text":"legacy work"}',
                    "created_at": f"2026-01-01T00:00:0{index}Z",
                },
            )

    current = store.tasks_repo.list_current_jobs(scope=TEST_SCOPE, limit=10, active_only=True)
    refused = runtime_persistence.current.create_root_task.execute(
        TaskRequest(
            id="new-job",
            detached=True,
            through_message_id=_admission_anchor(store),
            text="new work",
            scope=TEST_SCOPE.to_dict(),
        ),
        enabled=True,
        max_active=2,
    )
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")

    assert [row.job_id for row in current] == ["legacy-b", "legacy-a"]
    assert refused.status == "rejected_duplicate"
    assert refused.error_code == "owner_active_job_limit"
    assert claimed is not None and claimed.job_id == claimed.task_id


def test_legacy_empty_job_id_is_addressable_by_its_task_id(tmp_path) -> None:
    from sqlalchemy import text

    store = runtime_persistence.initialize()
    with store.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, owner_id, agent_id, status, task_type, input_json, "
                "metadata_json, payload_json, created_at) VALUES "
                "('legacy-job', :owner_id, :agent_id, 'queued', 'task', '{}', '{}', '{}', '2026-01-01')"
            ),
            {"owner_id": TEST_SCOPE.owner_id, "agent_id": TEST_SCOPE.agent_id},
        )

    current = store.tasks_repo.get_current_job("legacy-job", scope=TEST_SCOPE)
    stopped = store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="legacy-job")

    assert current is not None and current.job_id == "legacy-job"
    assert len(stopped) == 1 and stopped[0].status == "cancelled"


def test_job_lookup_and_stop_are_owner_scoped_and_idempotent(tmp_path) -> None:
    from new_core.identity import OwnerScope
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    other = OwnerScope(owner_id="other", agent_id="main", surface_id=TEST_SCOPE.surface_id, chat_id="1")
    for task_id, scope in (("mine", TEST_SCOPE), ("theirs", other)):
        store.tasks_repo.create_task(TaskRequest(id=task_id, detached=True, text=task_id, scope=scope.to_dict()))

    assert [row.job_id for row in store.tasks_repo.list_current_jobs(scope=TEST_SCOPE, limit=100)] == ["mine"]
    assert store.tasks_repo.get_current_job("theirs", scope=TEST_SCOPE) is None
    stopped = store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="mine")
    again = store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="mine")

    assert stopped[0].status == "cancelled"
    assert again[0].status == "cancelled"
    assert store.tasks_repo.get_current_job("theirs", scope=other).status == "queued"


def test_stopping_a_job_closes_its_whole_task_tree_and_no_other_tree(tmp_path) -> None:
    """One transaction stops every active descendant, including one below a terminal row."""
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()

    def create(task_id: str, status: str, *, parent: str = "") -> None:
        store.tasks_repo.create_task(
            TaskRequest(
                id=task_id,
                job_id=task_id,
                parent_task_id=parent,
                text=task_id,
                scope=TEST_SCOPE.to_dict(),
            ),
            status=status,
        )

    create("root", "running")
    create("requested-child", "requested", parent="root")
    create("queued-child", "queued", parent="root")
    create("scheduled-child", "scheduled", parent="root")
    create("confirmation-child", "waiting_confirmation", parent="root")
    create("interrupted-child", "interrupted", parent="root")
    create("running-child", "running", parent="root")
    create("finished-child", "completed", parent="root")
    create("grandchild", "requested", parent="finished-child")
    create("unrelated", "scheduled")

    stopped = store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="root")

    assert [row.task_id for row in stopped[:1]] == ["root"]
    assert {row.task_id for row in stopped} == {
        "root",
        "requested-child",
        "queued-child",
        "scheduled-child",
        "confirmation-child",
        "interrupted-child",
        "running-child",
        "grandchild",
    }
    for task_id in (
        "requested-child",
        "queued-child",
        "scheduled-child",
        "interrupted-child",
        "grandchild",
    ):
        assert store.tasks_repo.get_task(task_id).status == "cancelled"
    assert store.tasks_repo.get_task("root").status == "running"
    assert store.tasks_repo.get_task("root").stop_requested_at
    assert store.tasks_repo.get_task("running-child").status == "running"
    assert store.tasks_repo.get_task("running-child").stop_requested_at
    assert store.tasks_repo.get_task("finished-child").status == "completed"
    assert store.tasks_repo.get_task("unrelated").status == "scheduled"

    confirmation = store.tasks_repo.get_task("confirmation-child")
    assert (confirmation.status, bool(confirmation.stop_requested_at)) == ("queued", True)


def test_a_cancelled_requested_child_cannot_be_requeued_from_a_stale_event(tmp_path) -> None:
    """The queue's requested-to-scheduled write loses to a tree stop in another process."""
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root", text="root", scope=TEST_SCOPE.to_dict()), status="running")
    request = TaskRequest(id="child", parent_task_id="root", text="child", scope=TEST_SCOPE.to_dict())
    requested = store.tasks_repo.create_task(request, status="requested")
    stale_scheduled = requested.model_copy(update={"status": "scheduled"})

    store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="root")
    winner = store.tasks_repo.upsert_task(stale_scheduled, expected_status="requested")

    assert winner.status == "cancelled"
    assert store.tasks_repo.get_task("child").status == "cancelled"


def test_stop_jobs_returns_the_terminal_root_that_won_before_the_tree_update(tmp_path) -> None:
    """The command receives the committed winner, not the root snapshot selected before its update."""
    from sqlalchemy import event

    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root", text="root", scope=TEST_SCOPE.to_dict()), status="running")
    injected = False

    def finish_after_selection(connection, _cursor, statement, _parameters, _context, _many) -> None:
        nonlocal injected
        if injected or "ROW_NUMBER() OVER" not in statement or "parent_task_id=''" not in statement:
            return
        injected = True
        connection.exec_driver_sql(
            "UPDATE tasks SET status='completed', result='won', finished_at='2026-01-01' WHERE task_id='root'"
        )

    event.listen(store.engine, "after_cursor_execute", finish_after_selection)
    try:
        [winner] = store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="root")
    finally:
        event.remove(store.engine, "after_cursor_execute", finish_after_selection)

    assert winner.status == "completed"
    assert winner.result == "won"
    assert winner.stop_requested_at is None


def test_stop_all_jobs_includes_a_terminal_root_with_an_active_descendant(tmp_path) -> None:
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="root", text="root", scope=TEST_SCOPE.to_dict()), status="completed")
    store.tasks_repo.create_task(
        TaskRequest(id="child", parent_task_id="root", text="child", scope=TEST_SCOPE.to_dict()),
        status="scheduled",
    )

    stopped = store.tasks_repo.stop_jobs(scope=TEST_SCOPE)

    assert [row.task_id for row in stopped] == ["root", "child"]
    assert stopped[0].status == "completed"
    assert stopped[1].status == "cancelled"


@pytest.mark.parametrize("successor_time", ["2026-01-02", "2026-01-01"])
@pytest.mark.parametrize("terminal", [False, True])
def test_current_attempt_selection_drives_listing_stop_and_terminal_claims(
    tmp_path, monkeypatch, successor_time, terminal
):
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    monkeypatch.setattr("ouroboros.storage.tasks.records.utc_now_iso", lambda: "2026-01-02")
    request = TaskRequest(id="z-old", detached=True, text="work", scope=TEST_SCOPE.to_dict())
    store.tasks_repo.create_task(request)
    first_start = store.tasks_repo.claim_next_task(runtime_id="runtime").started_at
    monkeypatch.setattr("ouroboros.storage.tasks.records.utc_now_iso", lambda: successor_time)
    successor = request.model_copy(update={"id": "a-new"})
    assert store.tasks_repo.transition_running(
        "z-old",
        runtime_id="runtime",
        result="release",
        kind="instance_released",
        attempt=1,
        successor=successor,
        claim_token=store.tasks_repo.get_task("z-old").metadata["execution_claim"],
    )[1]
    store.tasks_repo.claim_next_task(runtime_id="runtime")
    if terminal:
        store.tasks_repo.finish_detached_attempt(
            "a-new",
            claim_token=str(store.tasks_repo.get_task("a-new").metadata.get("execution_claim") or ""),
            status="completed",
            result="successor answer",
        )
    current = store.tasks_repo.get_current_job("z-old", scope=TEST_SCOPE)
    assert current.task_id == "a-new"
    assert (current.created_at, current.started_at) == ("2026-01-02", first_start)
    assert (current.lineage_ordinal, current.attempt) == (1, 1)
    monkeypatch.setattr("ouroboros.storage.tasks.records.utc_now_iso", lambda: "2026-01-01T12:00:00")
    store.tasks_repo.create_task(TaskRequest(id="middle", detached=True, text="other", scope=TEST_SCOPE.to_dict()))
    listed = store.tasks_repo.list_current_jobs(scope=TEST_SCOPE, limit=10)
    assert [row.task_id for row in listed] == ["a-new", "middle"]
    assert (listed[0].created_at, listed[0].started_at) == ("2026-01-02", first_start)
    assert [row.task_id for row in runtime_persistence.current.result_repo.claim_terminal_jobs()] == (
        ["a-new"] if terminal else []
    )
    assert runtime_persistence.current.create_root_task.execute(request, enabled=True, max_active=5).task_id == "a-new"
    [stopped] = store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="z-old")
    assert (stopped.task_id, stopped.created_at, stopped.started_at) == ("a-new", "2026-01-02", first_start)
    assert stopped.status == ("completed" if terminal else "running")
    assert store.tasks_repo.get_task("z-old").status == "cancelled"
    assert store.tasks_repo.get_task("a-new").created_at == successor_time


@pytest.mark.parametrize("transition", ["fail", "reclaim", "release"])
def test_retry_transition_uses_committed_stop_state_after_a_stale_selection(tmp_path, monkeypatch, transition) -> None:
    from sqlalchemy import text

    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    import datetime

    task_id = f"{transition}-stopped-race"
    store.tasks_repo.create_task(TaskRequest(id=task_id, detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    store.tasks_repo.heartbeat_task(task_id, phase="model", claim_token=claimed.metadata["execution_claim"])
    clock = datetime.datetime.fromisoformat(claimed.heartbeat_at) + datetime.timedelta(
        seconds=121 if transition == "reclaim" else 1
    )
    monkeypatch.setattr(task_transitions, "database_now", lambda conn: clock)
    with store.engine.connect() as conn:
        stale = conn.execute(text("SELECT * FROM tasks WHERE task_id=:task_id"), {"task_id": task_id}).fetchone()
    assert stale is not None
    assert store.tasks_repo.request_task_stop(task_id, scope=TEST_SCOPE)
    monkeypatch.setattr(task_transitions, "task_for_transition", lambda _conn, _task_id: stale)
    successor = TaskRequest(
        id=f"{task_id}-retry",
        job_id=task_id,
        detached=True,
        text="work",
        scope=TEST_SCOPE.to_dict(),
    )

    if transition == "reclaim":
        with store.engine.begin() as conn:
            conn.execute(
                text("UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE task_id=:id"), {"id": task_id}
            )
        result = store.tasks_repo.transition_running(
            task_id,
            cutoff_iso="9999-01-01T00:00:00+00:00",
            result="lease lost",
            kind="lease_lost",
            attempt=2,
            successor=successor,
        )
    elif transition == "release":
        result = store.tasks_repo.transition_running(
            task_id,
            runtime_id="runtime",
            claim_token=claimed.metadata["execution_claim"],
            result="released",
            kind="instance_released",
            attempt=2,
            successor=successor,
        )
    else:
        result = store.tasks_repo.transition_running(
            task_id,
            result="timed out",
            hard_timeout_sec=0,
            kind="hard_timeout",
            attempt=2,
            successor=successor,
        )

    assert result is not None
    closed, successor_created = result
    assert (closed.status, closed.result, closed.error_code) == ("cancelled", "Task cancelled.", "")
    assert successor_created is False
    assert store.tasks_repo.get_task(successor.id) is None


@pytest.mark.parametrize("writer", ["facade", "status", "upsert"])
def test_task_results_facade_does_not_overwrite_detached_terminal_winner(tmp_path, writer) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.task_results import STATUS_COMPLETED, write_task_result

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    assert store.tasks_repo.claim_next_task(runtime_id="runtime") is not None
    write_task_result(
        tmp_path,
        "job",
        STATUS_COMPLETED,
        execution_claim=store.tasks_repo.get_task("job").metadata["execution_claim"],
        result="first",
        result_ref="first-ref",
        error_code="winner",
        source="browser",
        correlation_id="first-correlation",
        chat_id=42,
        total_rounds=1,
        ts="2026-01-01T00:00:00+00:00",
    )
    winner = store.tasks_repo.get_task("job")
    assert winner is not None

    fields = {
        "result": "late",
        "result_ref": "late-ref",
        "error_code": "late",
        "finished_at": "2026-01-03",
        "metadata": {"llm_trace": "late"},
    }
    if writer == "facade":
        write_task_result(tmp_path, "job", STATUS_COMPLETED, total_rounds=2, llm_trace="late", **fields)
    elif writer == "status":
        store.tasks_repo.update_task_status("job", "completed", **fields)
    else:
        store.tasks_repo.upsert_task(winner.model_copy(update=fields))
    assert store.tasks_repo.get_task("job") == winner


def test_detached_terminal_snapshot_is_atomic_before_reconciliation(tmp_path, monkeypatch) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.task_results import STATUS_COMPLETED, write_task_result

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    assert store.tasks_repo.claim_next_task(runtime_id="runtime") is not None
    terminal_committed = threading.Event()
    release_winner = threading.Event()
    finish = store.tasks_repo.finish_detached_attempt

    def pause_after_terminal(*args, **kwargs):
        record = finish(*args, **kwargs)
        terminal_committed.set()
        assert release_winner.wait(5)
        return record

    monkeypatch.setattr(store.tasks_repo, "finish_detached_attempt", pause_after_terminal)

    def write(result, result_ref, total_rounds, ts):
        return write_task_result(
            tmp_path,
            "job",
            STATUS_COMPLETED,
            execution_claim=store.tasks_repo.get_task("job").metadata["execution_claim"],
            result=result,
            result_ref=result_ref,
            error_code="winner" if total_rounds == 1 else "late",
            total_rounds=total_rounds,
            llm_trace={"tool_calls": [{"tool": "lookup"}]},
            episode_search_terms=["answer"],
            ts=ts,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(write, "first", "first-ref", 1, "2026-01-02T00:00:00+00:00")
        assert terminal_committed.wait(5)
        [claimed] = runtime_persistence.current.result_repo.claim_terminal_jobs(limit=50)
        loser = pool.submit(write, "late", "late-ref", 2, "2026-01-03T00:00:00+00:00")
        loser.result(5)
        release_winner.set()
        winner.result(5)

    final = store.tasks_repo.get_task("job")
    assert final is not None
    assert (claimed.result, claimed.result_ref, claimed.error_code) == ("first", "first-ref", "winner")
    assert (final.result, final.result_ref, final.error_code, final.finished_at) == (
        claimed.result,
        claimed.result_ref,
        claimed.error_code,
        claimed.finished_at,
    )
    assert final.metadata == claimed.metadata
    assert claimed.metadata["total_rounds"] == 1
    assert claimed.input["text"] == "work"
    assert claimed.metadata["llm_trace"] == {"tool_calls": [{"tool": "lookup"}]}
    assert claimed.metadata["episode_search_terms"] == ["answer"]


def test_stopped_detached_late_writer_preserves_the_winning_payload(tmp_path) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.task_results import STATUS_FAILED, load_task_result, write_task_result

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(
            id="job",
            detached=True,
            text="work",
            source="browser",
            correlation_id="winner-correlation",
            chat_id=42,
            scope=TEST_SCOPE.to_dict(),
        )
    )
    winner_metadata = {
        "description": "winner description",
        "context": "winner context",
        "trace_id": "winner trace",
        "trace_summary": "winner summary",
        "transport": {"winner": True},
    }
    store.tasks_repo.upsert_task(store.tasks_repo.get_task("job").model_copy(update={"metadata": winner_metadata}))
    assert store.tasks_repo.claim_next_task(runtime_id="runtime") is not None
    assert store.tasks_repo.request_task_stop("job", scope=TEST_SCOPE)

    payload = write_task_result(
        tmp_path,
        "job",
        STATUS_FAILED,
        execution_claim=store.tasks_repo.get_task("job").metadata["execution_claim"],
        result="unsafe late failure",
        error_code="execution_failed",
        source="dialogs",
        correlation_id="late-correlation",
        chat_id=99,
        description="late description",
        context="late context",
        trace_id="late trace",
        trace_summary="late summary",
        transport={"late": True},
        total_rounds=7,
    )

    record = store.tasks_repo.get_task("job")
    loaded = load_task_result(tmp_path, "job")
    assert record is not None and loaded is not None
    assert (record.status, record.result, record.result_ref, record.error_code) == (
        "cancelled",
        "Task cancelled.",
        "",
        "",
    )
    assert payload["error_code"] == loaded["error_code"] == ""
    assert (record.source, record.correlation_id, record.chat_id) == (
        "browser",
        "winner-correlation",
        TEST_SCOPE.chat_id,
    )
    for key, value in winner_metadata.items():
        assert record.metadata[key] == payload[key] == loaded[key] == value
    assert record.metadata == {**winner_metadata, "execution_claim": record.metadata["execution_claim"]}


@pytest.mark.parametrize("offset", [0, 1])
def test_terminal_publication_claims_expire_and_are_token_fenced(tmp_path, monkeypatch, offset) -> None:
    import datetime as dt

    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job", detached=True, scope=TEST_SCOPE.to_dict()), status="cancelled")
    first = runtime_persistence.current.result_repo.claim_terminal_jobs()[0]
    claim = first.terminal_publish_claim
    assert not runtime_persistence.current.result_repo.finish_terminal_publication("job", claim="wrong", accepted=True)
    assert not runtime_persistence.current.result_repo.finish_terminal_publication(
        "job", claim=claim, accepted=True
    )  # no text receipt
    expiry = dt.datetime.fromisoformat(first.terminal_publish_after)
    monkeypatch.setattr(task_transitions, "database_now", lambda conn: expiry + dt.timedelta(seconds=offset))
    assert runtime_persistence.current.result_repo.terminal_publication_remaining("job", claim=claim) == 0
    assert not runtime_persistence.current.result_repo.record_terminal_delivery(
        "job", claim=claim, item="text", disposition="accepted"
    )
    assert not runtime_persistence.current.result_repo.finish_terminal_publication("job", claim=claim, accepted=True)
    second = runtime_persistence.current.result_repo.claim_terminal_jobs()[0]
    assert second.terminal_publish_claim != claim
    claim = second.terminal_publish_claim
    assert runtime_persistence.current.result_repo.record_terminal_delivery(
        "job", claim=claim, item="text", disposition="accepted"
    )
    assert runtime_persistence.current.result_repo.finish_terminal_publication(
        "job", claim=claim, accepted=False, retry_after_sec=0
    )
    assert runtime_persistence.current.result_repo.claim_terminal_jobs() == []
    assert (
        0
        < (dt.datetime.fromisoformat(store.tasks_repo.get_task("job").terminal_publish_after) - expiry).total_seconds()
        <= 60
    )


@pytest.mark.parametrize(("limit", "expected"), [(0, 0), (-1, 0), (3, 3), (51, 50)])
def test_terminal_claim_limit_is_positive_and_bounded(tmp_path, limit, expected) -> None:

    store = runtime_persistence.initialize()
    from ouroboros.storage.runtime_models import TaskRequest

    for index in range(51):
        store.tasks_repo.create_task(
            TaskRequest(id=f"terminal-{index}", detached=True, scope=TEST_SCOPE.to_dict()), status="cancelled"
        )
    assert len(runtime_persistence.current.result_repo.claim_terminal_jobs(limit=limit)) == expected
    assert sum(bool(record.terminal_publish_claim) for record in store.tasks_repo.list_tasks()) == expected


def test_task_results_facade_writes_the_database_only(tmp_path) -> None:
    from ouroboros.storage.task_results import STATUS_COMPLETED, load_task_result, write_task_result

    store = runtime_persistence.initialize()
    write_task_result(tmp_path, "task2", STATUS_COMPLETED, scope=TEST_SCOPE, result="ok")

    record = store.tasks_repo.get_task("task2")
    assert record is not None
    assert record.status == STATUS_COMPLETED
    assert record.result == "ok"
    # The ownership columns of a row this door creates come from the work's scope, not
    # from the instance's configured owner: the two agree only while both are constants.
    assert (record.owner_id, record.agent_id) == (TEST_SCOPE.owner_id, TEST_SCOPE.agent_id)
    assert not (tmp_path / "task_results" / "task2.json").exists()

    loaded = load_task_result(tmp_path, "task2")
    assert loaded is not None
    assert loaded["status"] == STATUS_COMPLETED
    assert loaded["result"] == "ok"


def _claimed_task(tmp_path, task_id: str = "task1"):
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id=task_id, type="task", text="run", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="test-runtime")
    assert claimed is not None and claimed.task_id == task_id
    return store


@pytest.mark.parametrize("authority", ["live", "expired", "foreign", "missing", "replaced", "stopped"])
def test_execution_requires_exact_live_current_claim_at_database_time(tmp_path, monkeypatch, authority) -> None:
    import datetime

    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="holder")
    token = claimed.metadata.get("execution_claim", "")
    assert token
    now = datetime.datetime.fromisoformat(claimed.heartbeat_at) + datetime.timedelta(
        seconds=120 if authority == "expired" else 1
    )
    monkeypatch.setattr(task_transitions, "database_now", lambda conn: now)
    monkeypatch.setattr("ouroboros.storage.tasks.repository.utc_now_iso", lambda: "9999-01-01T00:00:00+00:00")
    if authority == "replaced":
        from ouroboros.storage.tasks.records import _task_from_request

        successor = _task_from_request(
            TaskRequest(id="new", job_id="job", detached=True, scope=TEST_SCOPE.to_dict()), status="queued"
        )
        successor.lineage_ordinal = 1
        store.tasks_repo.upsert_task(successor)
    if authority == "stopped":
        store.tasks_repo.request_task_stop("job", scope=TEST_SCOPE)
    if authority == "expired":
        from sqlalchemy import text

        with store.engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE task_id='job'"))
    supplied = "foreign" if authority == "foreign" else "" if authority == "missing" else token
    before = store.tasks_repo.get_task("job")
    live = authority == "live"
    assert bool(store.tasks_repo.list_stale_leases(cutoff_iso="1900-01-01")) is (authority == "expired")
    assert store.tasks_repo.is_task_attempt_active("job", claim_token=supplied) is live
    beat = store.tasks_repo.heartbeat_task("job", phase="model", claim_token=supplied)
    assert (beat is not None) is live
    if live:
        assert beat.metadata["run_started_at"] == now.isoformat()
    finished = store.tasks_repo.finish_detached_attempt(
        "job", status="completed", result="answer", claim_token=supplied
    )
    assert (finished is not None) is (live or authority == "stopped")
    if finished is not None:
        assert (finished.status, finished.result, finished.error_code) == (
            ("completed", "answer", "") if live else ("cancelled", "Task cancelled.", "")
        )
        assert (
            store.tasks_repo.finish_detached_attempt("job", status="failed", result="late", claim_token=token) is None
        )
        assert not store.tasks_repo.is_task_attempt_active("job", claim_token=token)
    if not live and authority != "stopped":
        store.tasks_repo.update_task_status("job", "completed", result="bypass")
        assert store.tasks_repo.get_task("job") == before


def test_late_completion_cannot_overwrite_a_failed_task(tmp_path) -> None:
    store = _claimed_task(tmp_path)
    store.tasks_repo.fail_task("task1", result="Task killed by hard timeout after 1800s.")

    store.tasks_repo.complete_task("task1", result="Finished, eventually.")

    record = store.tasks_repo.get_task("task1")
    assert record.status == "failed"
    assert record.result == "Task killed by hard timeout after 1800s."


def test_heartbeat_on_a_running_task_advances_heartbeat_at(tmp_path, monkeypatch) -> None:
    import datetime

    store = _claimed_task(tmp_path)
    claimed_at = store.tasks_repo.get_task("task1").heartbeat_at
    now = datetime.datetime.fromisoformat(claimed_at) + datetime.timedelta(seconds=1)
    monkeypatch.setattr(task_transitions, "database_now", lambda conn: now)

    store.tasks_repo.heartbeat_task("task1", phase="tool_call")

    record = store.tasks_repo.get_task("task1")
    assert record.status == "running"
    assert record.heartbeat_at == now.isoformat()
    assert record.metadata["heartbeat_phase"] == "tool_call"


def test_dialogs_cursor_creates_a_complete_owner_row(tmp_path) -> None:
    """A transport cursor can be the first writer; the row it makes is a whole owner row."""
    from ouroboros.storage.owner_state import default_state_dict
    from ouroboros.storage.runtime_models import OwnerRuntimeState

    store = runtime_persistence.initialize()
    SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE).save_cursor(7)

    row = sql_owner_state_repository(store.engine).load(TEST_SCOPE).payload
    assert row is not None
    assert set(row) == set(default_state_dict())
    assert OwnerRuntimeState.model_validate(row).session_id
    assert row["provider_state"] == {"dialogs": {"seq": 7}}


def test_dialogs_cursor_keeps_the_owner_row_it_found(tmp_path) -> None:

    store = runtime_persistence.initialize()
    sql_owner_state_repository(store.engine).save(
        OwnerStateDocument({"owner_id": 42, "session_id": "sess-1"}), TEST_SCOPE
    )

    SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE).save_cursor(7)

    row = sql_owner_state_repository(store.engine).load(TEST_SCOPE).payload
    assert row is not None
    assert row["owner_id"] == 42
    assert row["session_id"] == "sess-1"
    assert row["provider_state"] == {"dialogs": {"seq": 7}}


def test_owner_messages_narrow_by_actor_and_source_before_the_limit(tmp_path) -> None:
    from ouroboros.storage.runtime_models import ConversationMessage

    store = runtime_persistence.initialize()
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(
            message_id="wanted",
            dedup_key="wanted",
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            chat_id=TEST_SCOPE.chat_id,
            source="browser",
            actor_type="owner",
            content="wanted",
            created_at="2026-01-01T00:00:00+00:00",
        ),
        scope=TEST_SCOPE,
    )
    for index in range(6):
        sql_conversation_repository(store.engine).append_message(
            ConversationMessage(
                message_id=f"noise-{index}",
                dedup_key=f"noise-{index}",
                owner_id=TEST_SCOPE.owner_id,
                agent_id=TEST_SCOPE.agent_id,
                chat_id=TEST_SCOPE.chat_id,
                source="dialogs" if index % 2 else "browser",
                actor_type="owner" if index % 2 else "agent",
                content=f"noise {index}",
                created_at=f"2026-01-01T00:01:0{index}+00:00",
            ),
            scope=TEST_SCOPE,
        )

    window = sql_conversation_repository(store.engine).list_owner_messages(
        scope=TEST_SCOPE, limit=3, actor_type="owner", source="browser", newest_first=True
    )

    assert [message.content for message in window] == ["wanted"]


def test_rewriting_the_same_terminal_status_still_lands(tmp_path) -> None:
    store = _claimed_task(tmp_path)
    store.tasks_repo.fail_task("task1", result="first")

    store.tasks_repo.fail_task("task1", result="second", total_rounds=5)

    record = store.tasks_repo.get_task("task1")
    assert record.result == "second"
    assert record.metadata["total_rounds"] == 5


def test_only_one_caller_can_fail_a_record_that_was_running(tmp_path) -> None:
    """The sweep learns it performed the kill from a write that names ``running``.

    The absorbing rule cannot tell it: a repeat satisfies the same-status carve-out, so the
    record reads exactly as it would have had this caller closed it.
    """
    store = _claimed_task(tmp_path)

    killed = store.tasks_repo.transition_running("task1", result="killed by the sweep", kind="hard_timeout", attempt=2)
    second = store.tasks_repo.transition_running("task1", result="killed again", kind="hard_timeout", attempt=3)

    assert killed is not None
    killed, successor_created = killed
    assert (killed.status, killed.result, killed.attempt) == ("failed", "killed by the sweep", 2)
    assert successor_created is False
    assert second is None
    assert store.tasks_repo.get_task("task1").result == "killed by the sweep"


def test_a_record_that_left_running_for_an_active_status_is_not_failed(tmp_path) -> None:
    """The predicate is `running`, not "not terminal", and that is narrower on purpose.

    `fail_task`, which this replaced, closed a record that had moved to another *active*
    status. Nothing writes `waiting_confirmation` or `interrupted` onto a supervisor-run
    record today, so this pins the narrowing rather than a live path: the sweep kills what is
    running, and a record in another state is somebody else's to finish.
    """
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="held", type="task", text="run", scope=TEST_SCOPE.to_dict()),
        status="waiting_confirmation",
    )

    refused = store.tasks_repo.transition_running("held", result="killed by the sweep", kind="hard_timeout", attempt=2)

    assert refused is None
    record = store.tasks_repo.get_task("held")
    assert (record.status, record.result) == ("waiting_confirmation", "")


def test_the_absorbing_rule_alone_does_not_refuse_a_repeated_outcome(tmp_path) -> None:
    """The contrast that motivates the conditional closer, and the carve-out that stays.

    `update_task_status`'s predicate carries `OR status=:status`, so a second caller offering
    the outcome the record already holds still applies — and reads back exactly as the first
    caller's own kill would, which is why the status of the returned record cannot decide
    anything.
    """
    store = _claimed_task(tmp_path)
    store.tasks_repo.transition_running("task1", result="killed by the sweep", kind="hard_timeout", attempt=2)

    repeat = store.tasks_repo.fail_task("task1", result="a second sweeper's result", error_code="hard_timeout")

    assert repeat is not None and repeat.status == "failed"
    assert store.tasks_repo.get_task("task1").result == "a second sweeper's result"


def test_the_soft_timeout_notice_is_claimed_once(tmp_path) -> None:
    store = _claimed_task(tmp_path)

    assert store.tasks_repo.claim_soft_timeout_notice("task1") is True
    claimed_at = store.tasks_repo.get_task("task1").soft_notice_at

    assert store.tasks_repo.claim_soft_timeout_notice("task1") is False
    # The second claim leaves the first one's time in place, as a second stop request does.
    assert store.tasks_repo.get_task("task1").soft_notice_at == claimed_at


def test_a_record_that_is_no_longer_running_cannot_claim_the_notice(tmp_path) -> None:
    store = _claimed_task(tmp_path)
    store.tasks_repo.complete_task("task1", result="done")

    assert store.tasks_repo.claim_soft_timeout_notice("task1") is False
    assert store.tasks_repo.get_task("task1").soft_notice_at is None


def test_a_full_row_write_cannot_clear_a_claimed_notice(tmp_path) -> None:
    """The claim survives a read-modify-write that began before it — that erasure is the bug."""
    store = _claimed_task(tmp_path)
    read_before_the_claim = store.tasks_repo.get_task("task1")
    assert store.tasks_repo.claim_soft_timeout_notice("task1") is True

    store.tasks_repo.upsert_task(read_before_the_claim)

    assert store.tasks_repo.get_task("task1").soft_notice_at
    assert store.tasks_repo.claim_soft_timeout_notice("task1") is False


def _second_handle(tmp_path):
    """A second store over the same database — its own engine, its own pool.

    Two handles are how a second process looks from here: nothing they share in memory can
    make a transition safe, so what stands between them has to be the statement.
    """
    from infra.runtime_database import create_runtime_engine

    composed = runtime_persistence.current
    return make_sql_runtime_persistence(create_runtime_engine(composed.db_urls[0]), db_url=composed.db_urls[0])


def test_two_handles_cannot_both_close_one_record(tmp_path) -> None:
    store = _claimed_task(tmp_path)
    elsewhere = _second_handle(tmp_path)

    store.tasks_repo.fail_task("task1", result="killed by the timeout enforcer")
    late = elsewhere.tasks_repo.complete_task("task1", result="the body's answer")

    assert late is not None
    # The loser is handed the record that won, not the one it hoped to write.
    assert late.status == "failed"
    assert late.result == "killed by the timeout enforcer"
    assert store.tasks_repo.get_task("task1").result == "killed by the timeout enforcer"


def test_retry_insertion_failure_rolls_back_predecessor_closure(tmp_path, monkeypatch) -> None:
    import pytest

    from ouroboros.storage.runtime_models import TaskRequest

    store = _claimed_task(tmp_path)
    successor = TaskRequest(id="retry-2", text="retry", scope=TEST_SCOPE.to_dict())

    def fail_insert(*_args, **_kwargs):
        raise RuntimeError("insertion failed")

    monkeypatch.setattr(store.tasks_repo, "_insert_task", fail_insert)
    with pytest.raises(RuntimeError, match="insertion failed"):
        store.tasks_repo.transition_running(
            "task1",
            result="timeout",
            kind="hard_timeout",
            attempt=2,
            successor=successor,
        )

    assert store.tasks_repo.get_task("task1").status == "running"
    assert store.tasks_repo.get_task("retry-2") is None


def test_a_heartbeat_from_a_second_handle_advances_nothing_on_a_terminal_record(tmp_path) -> None:
    store = _claimed_task(tmp_path)
    elsewhere = _second_handle(tmp_path)
    store.tasks_repo.fail_task("task1", result="killed")
    failed_at = store.tasks_repo.get_task("task1").heartbeat_at

    beat = elsewhere.tasks_repo.heartbeat_task("task1", phase="running")

    assert beat is None
    assert store.tasks_repo.get_task("task1").heartbeat_at == failed_at


def test_a_transition_out_of_an_active_status_still_applies(tmp_path) -> None:
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    for index, status in enumerate(("queued", "scheduled", "interrupted", "running")):
        task_id = f"t{index}"
        store.tasks_repo.create_task(
            TaskRequest(id=task_id, type="task", text="run", scope=TEST_SCOPE.to_dict()), status=status
        )

        assert store.tasks_repo.cancel_task(task_id).status == "cancelled"


def test_racing_terminal_transitions_leave_one_whole_outcome(tmp_path) -> None:
    """Real threads, separate handles, one record: the statement decides, not a shared lock.

    Each thread offers a different terminal outcome. Exactly one may stand, and it has to
    stand whole — a status from one caller with the result of another would be the torn write
    a check-then-act produces.
    """
    import threading

    store = _claimed_task(tmp_path)
    handles = [store] + [_second_handle(tmp_path) for _ in range(3)]
    offered = [
        ("failed", "the timeout enforcer", "hard_timeout"),
        ("cancelled", "the owner", "cancelled_by_owner"),
        ("completed", "the body", ""),
        ("budget_exceeded", "the budget", "over_budget"),
    ]
    start = threading.Barrier(len(handles))
    returned: list[str] = []
    lock = threading.Lock()

    def _close(handle, outcome) -> None:
        status, result, error_code = outcome
        start.wait()
        record = handle.tasks_repo.update_task_status("task1", status, result=result, error_code=error_code)
        with lock:
            returned.append("" if record is None else record.status)

    threads = [threading.Thread(target=_close, args=pair) for pair in zip(handles, offered, strict=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stored = store.tasks_repo.get_task("task1")
    assert (stored.status, stored.result, stored.error_code) in offered
    assert returned and all(status for status in returned)


def test_a_full_row_write_is_only_reachable_from_a_paths_own_record(tmp_path) -> None:
    """`upsert_task` carries the writer's whole snapshot, so only an author may call it."""
    import pathlib as _pathlib
    import re

    repo_root = _pathlib.Path(__file__).resolve().parents[1]
    allowed = {
        # The store's own creation path.
        "ouroboros/storage/tasks/repository.py",
        # Creates a record, or re-schedules a `requested` row it is the author of.
        "supervisor/queue.py",
        # Creates the row for a status written before any task record existed.
        "ouroboros/storage/task_results.py",
        # Protocol rows rebuilt whole from the A2A payload; nothing else writes them.
        "transport/a2a/task_store.py",
        # The declaration of the method, not a call site.
        "ouroboros/storage/tasks/interfaces.py",
    }
    offenders: list[str] = []
    for root in ("ouroboros", "supervisor", "transport", "new_core", "background_memory"):
        base = repo_root / root
        if not base.exists():
            continue
        for source_file in base.rglob("*.py"):
            rel = str(source_file.relative_to(repo_root))
            if rel in allowed:
                continue
            if re.search(r"\bupsert_task\s*\(", source_file.read_text(encoding="utf-8")):
                offenders.append(f"{rel}: mutates a task record through the full-row write")
    assert offenders == []


def test_two_components_obtain_the_same_store() -> None:

    composed = runtime_persistence.initialize()

    assert runtime_persistence.current is composed
    # A second caller composes nothing new: the process has one runtime database.
    assert runtime_persistence.initialize() is composed


def test_reaching_for_the_store_before_composition_raises() -> None:
    """A holder of the test's own: nothing has composed it, so reading it is a failure."""
    import pytest

    from ouroboros.storage.runtime_persistence import RuntimePersistenceProvider

    with pytest.raises(RuntimeError, match="composition root"):
        assert RuntimePersistenceProvider().current


def test_closing_the_holder_forgets_the_store_and_disposes_its_engine() -> None:
    """A forgotten pool keeps its connections open until the collector gets round to it."""
    import pytest

    from ouroboros.storage.runtime_persistence import RuntimePersistenceProvider

    factory = RuntimePersistenceProvider()
    first = factory.initialize()
    pool = first.engine.pool

    factory.close()

    # ``dispose`` replaces the pool rather than emptying it, which is how a disposed engine
    # is told apart from a merely idle one.
    assert first.engine.pool is not pool
    with pytest.raises(RuntimeError, match="composition root"):
        assert factory.current
    # Closing twice is not an error, and neither is closing one that never composed.
    factory.close()
    RuntimePersistenceProvider().close()
    assert factory.initialize() is not first


def test_neither_half_composes_a_runtime_store_of_its_own() -> None:
    """Both bootstraps ask for the composed store; composing one is a second composition root."""
    import pathlib as _pathlib

    repo_root = _pathlib.Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for rel in ("supervisor/bootstrap.py", "ouroboros/agent_runtime/bootstrap.py"):
        source = (repo_root / rel).read_text(encoding="utf-8")
        if "runtime_persistence.initialize" in source:
            offenders.append(f"{rel}: composes a runtime store instead of asking for the composed one")
        if "runtime_persistence.current" not in source:
            offenders.append(f"{rel}: does not ask for the composed store")
    assert offenders == []


def test_constructing_the_store_migrates_nothing(blank_schema) -> None:
    """Schema is a deployment step; a starting runtime that upgraded would race its peers."""
    from sqlalchemy import inspect as sa_inspect

    from infra.runtime_database import create_runtime_engine

    # A schema of its own: the one this test's composition root brought up is already at
    # head, and would prove nothing about what the constructor does.
    url = str(blank_schema().url.render_as_string(hide_password=False))
    engine = create_runtime_engine(url)

    make_sql_runtime_persistence(engine, db_url=url)

    assert sa_inspect(engine).get_table_names() == []


def test_a_runtime_below_the_required_schema_refuses_to_start(blank_schema) -> None:
    import pytest
    from alembic import command

    from infra.migrations.runner import (
        UPGRADE_COMMAND,
        RuntimeSchemaOutdatedError,
        alembic_config,
        assert_schema_at_head,
        head_schema_version,
    )
    from infra.runtime_database import create_runtime_engine

    url = str(blank_schema().url.render_as_string(hide_password=False))
    engine = create_runtime_engine(url)
    cfg = alembic_config(url)
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "0008")
    store = make_sql_runtime_persistence(engine, db_url=url)
    behind = schema_version(store.engine)

    with pytest.raises(RuntimeSchemaOutdatedError) as raised:
        assert_schema_at_head(store.engine)

    assert UPGRADE_COMMAND in str(raised.value)
    assert str(head_schema_version()) in str(raised.value)
    # And it applied nothing on the way out.
    assert schema_version(store.engine) == behind


def test_a_runtime_against_a_database_with_no_alembic_history_refuses_by_the_schema(blank_schema) -> None:
    """The never-migrated database is the deployment that forgot the migration step.

    It has to arrive as the refusal that names the upgrade command, not as an undefined
    ``alembic_version`` table: with four tiers and two instances of each, eight processes
    would otherwise die naming a table nobody asked about.
    """
    import pytest

    from infra.migrations.runner import (
        UPGRADE_COMMAND,
        RuntimeSchemaOutdatedError,
        assert_schema_at_head,
        head_schema_version,
    )
    from infra.runtime_database import create_runtime_engine

    url = str(blank_schema().url.render_as_string(hide_password=False))
    store = make_sql_runtime_persistence(create_runtime_engine(url), db_url=url)

    assert schema_version(store.engine) == 0

    with pytest.raises(RuntimeSchemaOutdatedError) as raised:
        assert_schema_at_head(store.engine)

    assert UPGRADE_COMMAND in str(raised.value)
    assert str(head_schema_version()) in str(raised.value)


def test_a_database_that_cannot_be_opened_raises_rather_than_reporting_schema_zero() -> None:
    """The other half of the answer above, and the one a "defensive" edit would take away.

    "No Alembic history" is a schema answer; "this database did not open" is not. If the
    existence question were ever wrapped in a `try/except: return 0`, an unreachable database
    would start reporting itself as a fleet that merely needs migrating — and the refusal
    would name an upgrade command that cannot help. A database name the server does not
    have is one this process cannot open, and it is not a schema answer.
    """
    import pytest
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import OperationalError

    from infra.runtime_database import create_runtime_engine
    from tests.helpers.schema_harness import server_url

    url = (
        make_url(server_url())
        .set(database="ouroboros_no_such_database")
        .difference_update_query(["options"])
        .render_as_string(hide_password=False)
    )
    store = make_sql_runtime_persistence(create_runtime_engine(url), db_url=url)

    with pytest.raises(OperationalError):
        schema_version(store.engine)


def test_a_runtime_at_the_required_schema_starts(tmp_path) -> None:
    from infra.migrations.runner import assert_schema_at_head

    assert_schema_at_head(runtime_persistence.initialize().engine)


def test_the_store_keeps_no_lock_in_front_of_its_statements() -> None:
    """A lock left beside conditional writes is where the next read-modify-write is added.

    It also never protected what its shape suggests: connections come from a pool, one per
    call, and what needed serialising was a handful of decisions that are predicates now.
    """
    import inspect

    from ouroboros.storage.tasks.repository import SqlTaskRepository

    assert "_lock" not in inspect.getsource(SqlTaskRepository)


def test_a_requeued_row_keeps_its_ownership_columns_in_step_with_its_scope(tmp_path) -> None:
    """The scheduled-subtask path: an existing ``requested`` row is updated, never created.

    ``create_task`` is the only place that reads the ownership axes off the request's scope,
    and this branch never reaches it. Without the two columns in the update, a row created
    by one path keeps its ownership while ``input["scope"]`` says someone else — and no
    statement fails on the disagreement.
    """
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.task_results import STATUS_REQUESTED, write_task_result
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    placeholder_scope = _scope("browser", 1)
    write_task_result(tmp_path, "child-1", STATUS_REQUESTED, scope=placeholder_scope, chat_id=1)

    planner_scope = OwnerScope(owner_id="another-owner", agent_id="another-agent", surface_id="dialogs", chat_id="9")
    record = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    ).enqueue(
        TaskRequest(id="child-1", type="task", text="do it", chat_id=9, source="dialogs", scope=planner_scope.to_dict())
    )

    assert record.status == "scheduled"
    assert (record.owner_id, record.agent_id) == (planner_scope.owner_id, planner_scope.agent_id)
    assert record.input["scope"] == planner_scope.to_dict()


def test_parking_a_run_moves_its_record_and_opens_the_request_at_once(tmp_path) -> None:

    store = runtime_persistence.initialize()
    claim_task(store)

    opened = park_run(store)

    assert opened is not None
    assert store.tasks_repo.get_task("task1").status == "waiting_confirmation"
    assert (
        sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE).confirmation_id
        == opened.confirmation_id
    )


def test_a_chat_window_does_not_name_a_request_another_chat_waits_on() -> None:
    from ouroboros.storage.runtime_models import ConversationMessage

    store = runtime_persistence.initialize()
    for task_id, scope in (("task1", _scope("browser", 2)), ("task2", _scope("dialogs", TEST_SCOPE.chat_id))):
        claim_task(store, task_id, scope=scope)
        park_run(store, task_id=task_id, scope=scope)
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(
            message_id="question",
            dedup_key="question",
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            chat_id=TEST_SCOPE.chat_id,
            source="browser",
            actor_type="agent",
            content="question",
            created_at="2026-01-01T00:00:00+00:00",
        ),
        scope=TEST_SCOPE,
    )

    window, open_confirmation_id = SqlOwnerHistoryRepository(
        store.engine, history_transactions=file_transaction
    ).list_owner_messages_with_open_confirmation(scope=TEST_SCOPE, limit=5)

    assert [message.content for message in window] == ["question"]
    assert open_confirmation_id is None


def test_a_scope_already_asking_keeps_the_second_run_going(tmp_path) -> None:
    """Parking behind a question the owner will never be shown would strand the record."""
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    claim_task(store)
    park_run(store)
    store.tasks_repo.create_task(TaskRequest(id="task2", type="task", text="run", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="test-runtime")

    refused = park_run(store, task_id="task2")

    assert refused is None
    assert store.tasks_repo.get_task("task2").status == "running"


def test_a_record_queued_off_its_question_forgets_when_its_run_began(tmp_path) -> None:
    """The hard timeout counts from `run_started_at`, and the owner's thinking time is not the run's."""

    store = runtime_persistence.initialize()
    claim_task(store)
    store.tasks_repo.heartbeat_task("task1", phase="model")
    assert store.tasks_repo.get_task("task1").metadata.get("run_started_at")
    park_run(store)

    sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1")

    assert "run_started_at" not in store.tasks_repo.get_task("task1").metadata


def test_a_run_that_is_stopping_does_not_park(tmp_path) -> None:
    """Nothing would wake it: the stop has already passed over the record it would park."""

    store = runtime_persistence.initialize()
    claim_task(store)
    store.tasks_repo.request_task_stop("task1", scope=TEST_SCOPE)

    assert park_run(store) is None
    assert store.tasks_repo.get_task("task1").status == "running"
    assert sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE) is None


def test_a_question_outlives_no_parking(tmp_path) -> None:
    """Only a run that reaches `confirm` withdraws its own question, and a stopped one never does."""
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    park_run(store)
    store.tasks_repo.cancel_task("task1")

    sql_tool_confirmation_reader(store.engine).close_confirmations_nobody_is_parked_on(now_iso=utc_now_iso())

    assert sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE) is None


def test_a_question_the_owner_may_still_answer_is_left_open(tmp_path) -> None:
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store)

    sql_tool_confirmation_reader(store.engine).close_confirmations_nobody_is_parked_on(now_iso=utc_now_iso())

    assert (
        sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE).confirmation_id
        == opened.confirmation_id
    )


def test_a_question_asked_of_a_stopping_run_is_closed(tmp_path) -> None:
    """The stop is recorded before the record moves, and nothing will answer it after."""
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    park_run(store)
    store.tasks_repo.request_task_stop("task1", scope=TEST_SCOPE)

    sql_tool_confirmation_reader(store.engine).close_confirmations_nobody_is_parked_on(now_iso=utc_now_iso())

    assert sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE) is None


def _parked_run(store):
    claim_task(store)
    return park_run(store)


def _stopped_parked_run(store):
    opened = _parked_run(store)
    store.tasks_repo.request_task_stop("task1", scope=TEST_SCOPE)
    return opened


@contextlib.contextmanager
def _without_the_audit_table():
    from sqlalchemy import text

    from ouroboros.storage.runtime_engine import resolve_runtime_engine

    def rename(source: str, target: str) -> None:
        with resolve_runtime_engine("audit trail").connect() as conn:
            conn.execute(text(f"ALTER TABLE {source} RENAME TO {target}"))
            conn.commit()

    rename("audit_events", "audit_events_hidden")
    try:
        yield
    finally:
        rename("audit_events_hidden", "audit_events")


@parametrize_from_dict(
    {
        "open": {"arrange": claim_task, "act": lambda store, _: park_run(store)},
        "answer": {
            "arrange": _parked_run,
            "act": lambda store, opened: sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
                opened.confirmation_id, decision="approved", now_iso="2026-01-01T00:00:00+00:00"
            ),
        },
        "withdraw": {
            "arrange": _parked_run,
            "act": lambda store, opened: sql_tool_confirmation_reader(store.engine).expire_tool_confirmation(
                opened.confirmation_id, now_iso="2026-01-01T00:00:00+00:00"
            ),
        },
        "sweep": {
            "arrange": _stopped_parked_run,
            "act": lambda store, _: sql_tool_confirmation_reader(store.engine).close_confirmations_nobody_is_parked_on(
                now_iso="2026-01-01T00:00:00+00:00"
            ),
        },
    }
)
def test_a_request_does_not_move_without_its_audit_record(tmp_path, arrange, act) -> None:
    """A request opened or ended with no record of it is the stuck decision the audit trail exists to show."""
    from ouroboros.storage.runtime_backend import STORE_FAILURES

    store = runtime_persistence.initialize()
    opened = arrange(store)
    before = (
        store.tasks_repo.get_task("task1").status,
        sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE),
    )

    with _without_the_audit_table(), pytest.raises(STORE_FAILURES):
        act(store, opened)

    assert (
        store.tasks_repo.get_task("task1").status,
        sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE),
    ) == before


def test_requeue_does_nothing_unless_the_record_is_parked(tmp_path) -> None:

    store = runtime_persistence.initialize()
    claim_task(store)

    assert sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1") is False

    park_run(store)

    assert sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1") is True
    assert store.tasks_repo.get_task("task1").status == "queued"
    assert sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1") is False


def test_an_undelivered_question_leaves_the_record_running(tmp_path) -> None:
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store)

    assert sql_tool_confirmation_reader(store.engine).unpark_task(opened.confirmation_id, now_iso=utc_now_iso()) is True
    assert store.tasks_repo.get_task("task1").status == "running"


def test_an_undelivered_question_leaves_the_run_on_the_clock_it_had(tmp_path) -> None:
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    store.tasks_repo.heartbeat_task("task1", phase="model")
    began = store.tasks_repo.get_task("task1").metadata["run_started_at"]
    opened = park_run(store)

    sql_tool_confirmation_reader(store.engine).unpark_task(opened.confirmation_id, now_iso=utc_now_iso())

    assert store.tasks_repo.get_task("task1").metadata["run_started_at"] == began


def test_a_run_that_withdrew_its_question_gets_its_round_back_with_nothing_to_rewind_into(tmp_path) -> None:
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    approved = park_run(store, round_idx=3)
    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        approved.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )
    sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1")
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="test-runtime")
    sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1")
    undelivered = park_run(store, round_idx=3, args_hash="hash-of-call-1")
    sql_tool_confirmation_reader(store.engine).expire_tool_confirmation(
        undelivered.confirmation_id, now_iso=utc_now_iso()
    )

    sql_tool_confirmation_reader(store.engine).unpark_task(undelivered.confirmation_id, now_iso=utc_now_iso())

    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1") is None
    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_decisions("task1", round_idx=3) == {}


def test_a_record_whose_slot_was_taken_stays_parked(tmp_path) -> None:
    """The owner runs one foreground root at a time, and parking is what let the next one start."""
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store)
    store.tasks_repo.create_task(TaskRequest(id="task2", type="task", text="run", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="test-runtime")

    assert (
        sql_tool_confirmation_reader(store.engine).unpark_task(opened.confirmation_id, now_iso=utc_now_iso()) is False
    )
    assert store.tasks_repo.get_task("task1").status == "waiting_confirmation"
    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1") is not None


def test_a_decision_is_keyed_by_the_round_it_was_asked_in(tmp_path) -> None:
    """Some providers repeat tool-call ids between rounds; one answer allows exactly one call."""
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store, round_idx=1)
    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        opened.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )

    decided = sql_tool_confirmation_reader(store.engine).read_tool_confirmation_decisions("task1", round_idx=1)

    assert decided == {(0, "data_write", "hash-of-call-0"): "approved"}
    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_decisions("task1", round_idx=2) == {}


def test_a_spent_decision_does_not_reach_a_later_attempt_of_the_same_round(tmp_path) -> None:
    """The resumed run died after its round ran; a reclaim repeats that round and must ask again."""
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store, round_idx=3, snapshot={"round_idx": 3})
    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        opened.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )
    sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1")
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="test-runtime")
    sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1")

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_decisions("task1", round_idx=3) != {}
    assert (
        sql_tool_confirmation_reader(store.engine).spend_round_decisions("task1", round_idx=3, now_iso=utc_now_iso())
        == 1
    )
    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_decisions("task1", round_idx=3) == {}
    assert (
        sql_tool_confirmation_reader(store.engine).spend_round_decisions("task1", round_idx=3, now_iso=utc_now_iso())
        == 0
    )


def test_an_undecided_request_is_no_decision_yet(tmp_path) -> None:

    store = runtime_persistence.initialize()
    claim_task(store)
    park_run(store)

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_decisions("task1", round_idx=1) == {}


def test_the_snapshot_comes_back_to_the_run_that_was_parked(tmp_path) -> None:

    store = runtime_persistence.initialize()
    claim_task(store)
    park_run(store, snapshot={"round_idx": 3, "messages": []})

    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1")["round_idx"] == 3
    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("unknown") is None


def test_the_snapshot_is_handed_to_one_of_two_readers(tmp_path, monkeypatch) -> None:
    """Two instances read the same row; the drop decides who was handed it, not the read that came first."""
    from ouroboros.storage.tool_confirmation import repository as module

    store = runtime_persistence.initialize()
    claim_task(store)
    park_run(store, snapshot={"round_idx": 3, "messages": []})
    elsewhere = _second_handle(tmp_path)
    read_back = module.decrypt_field
    taken: list = []

    def the_other_reader_takes_it_first(value):
        monkeypatch.setattr(module, "decrypt_field", read_back)
        taken.append(sql_tool_confirmation_reader(elsewhere.engine).take_parked_snapshot("task1"))
        return read_back(value)

    monkeypatch.setattr(module, "decrypt_field", the_other_reader_takes_it_first)

    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1") is None
    assert taken[0]["round_idx"] == 3


def test_binary_rollback_waits_for_a_run_queued_after_its_answer(tmp_path) -> None:
    """Answered but not yet resumed, the record reads `queued`, and the older image would start it over."""
    from infra.migrations.runner import assert_binary_rollback_safe
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store)
    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        opened.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )
    sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1")

    with store.engine.connect() as conn, pytest.raises(RuntimeError, match="owner confirmation"):
        assert_binary_rollback_safe(conn)

    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="test-runtime")
    sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1")
    with store.engine.connect() as conn:
        assert_binary_rollback_safe(conn)


def test_the_snapshot_of_the_latest_round_wins_whatever_the_writing_clock_said(tmp_path) -> None:
    """`created_at` is the writing instance's clock; the round a run parked in is not."""
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    first = make_tool_confirmation(round_idx=1)
    sql_tool_confirmation_reader(store.engine).park_and_open(
        task_id="task1", claim_token="", confirmation=first, snapshot={"round_idx": 1}, workspace="", arguments={}
    )
    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        first.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )
    sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1")
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    lagging = make_tool_confirmation(args_hash="hash-of-call-1", round_idx=2)
    lagging.created_at = "2000-01-01T00:00:00+00:00"

    sql_tool_confirmation_reader(store.engine).park_and_open(
        task_id="task1", claim_token="", confirmation=lagging, snapshot={"round_idx": 2}, workspace="", arguments={}
    )

    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1")["round_idx"] == 2


def test_stop_jobs_queues_a_parked_job_with_the_stop_recorded(tmp_path) -> None:
    """A parked run has no thread to notice a stop, so the run the queue starts for it reads it."""

    store = runtime_persistence.initialize()
    claim_task(store)
    store.tasks_repo.heartbeat_task("task1", phase="model")
    park_run(store)

    [stopped] = store.tasks_repo.stop_jobs(scope=TEST_SCOPE)

    assert (stopped.status, bool(stopped.stop_requested_at)) == ("queued", True)
    assert "run_started_at" not in store.tasks_repo.get_task("task1").metadata


def test_the_orphan_sweep_only_wakes_a_parked_run_whose_question_is_answered(tmp_path) -> None:
    """A2A writes `waiting_confirmation` for its own input_required, and nothing there was parked."""
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="a2a-task", type="task", text="remote", scope=_scope("a2a", 5).to_dict()),
        status="waiting_confirmation",
    )
    claim_task(store)
    opened = park_run(store)

    assert sql_tool_confirmation_reader(store.engine).list_parked_tasks_without_pending_request() == []

    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        opened.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )

    assert sql_tool_confirmation_reader(store.engine).list_parked_tasks_without_pending_request() == ["task1"]


def test_the_snapshot_of_a_finished_run_is_dropped_and_its_row_kept(tmp_path) -> None:
    """Nothing will resume that run, and the snapshot is the whole transcript of a round."""
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store)
    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        opened.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )

    assert sql_tool_confirmation_reader(store.engine).forget_snapshots_of_finished_runs() == 0, "the run is still going"

    sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1")
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="test-runtime")
    store.tasks_repo.update_task_status("task1", "completed", result="done")

    assert sql_tool_confirmation_reader(store.engine).forget_snapshots_of_finished_runs() == 1
    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1") is None
    assert (
        sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status(opened.confirmation_id) == "approved"
    )


def test_a_request_open_for_another_scope_is_invisible_here(tmp_path) -> None:

    store = runtime_persistence.initialize()
    other = _scope("dialogs", 77)
    claim_task(store, "other-task", scope=other)
    sql_tool_confirmation_reader(store.engine).park_and_open(
        task_id="other-task",
        claim_token="",
        confirmation=make_tool_confirmation(scope=other, task_id="other-task"),
        snapshot={"round_idx": 1},
        workspace="",
        arguments={},
    )

    assert sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE) is None
    assert sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(other) is not None


@parametrize_from_dict(
    {
        "stopping": {
            "before": lambda store, cid, now: store.tasks_repo.request_task_stop("task1", scope=TEST_SCOPE),
            "status": "pending",
        },
        "ended": {"before": lambda store, cid, now: store.tasks_repo.cancel_task("task1"), "status": "pending"},
        "closed-unanswered": {
            "before": lambda store, cid, now: sql_tool_confirmation_reader(store.engine).expire_tool_confirmation(
                cid, now_iso=now
            ),
            "status": "expired",
        },
        "already-decided": {
            "before": lambda store, cid, now: sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
                cid, decision="approved", now_iso=now
            ),
            "status": "approved",
        },
    }
)
def test_a_decision_is_recorded_only_while_the_request_and_its_run_wait_for_it(tmp_path, before, status) -> None:
    """The write is what has to see a stop or an earlier answer: the reply path may have read the record first."""
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store)
    before(store, opened.confirmation_id, utc_now_iso())

    assert (
        sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
            opened.confirmation_id, decision="denied", now_iso=utc_now_iso()
        )
        is False
    )
    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status(opened.confirmation_id) == status


def test_an_answer_that_arrived_first_survives_the_waiter_giving_up(tmp_path) -> None:
    from ouroboros.utils import utc_now_iso

    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store)
    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        opened.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )

    assert (
        sql_tool_confirmation_reader(store.engine).expire_tool_confirmation(
            opened.confirmation_id, now_iso=utc_now_iso()
        )
        is False
    )
    assert (
        sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status(opened.confirmation_id) == "approved"
    )


def test_two_conversations_of_one_pair_keep_separate_histories_and_summaries(tmp_path) -> None:
    """The narrowed store surface, spelled out in the profile that ships."""
    from ouroboros.storage.runtime_models import ContextSummary, ConversationMessage

    store = runtime_persistence.initialize()
    here, elsewhere = TEST_SCOPE, _scope("browser", 2)
    for index, scope in enumerate((here, elsewhere)):
        sql_conversation_repository(store.engine).append_message(
            ConversationMessage(
                message_id=f"m-{index}",
                dedup_key=f"m-{index}",
                owner_id=scope.owner_id,
                agent_id=scope.agent_id,
                chat_id=scope.chat_id,
                source="browser",
                actor_type="owner",
                content=f"said in {scope.chat_id}",
                created_at=f"2026-01-01T00:00:0{index}+00:00",
            ),
            scope=scope,
        )
        sql_conversation_repository(store.engine).upsert_context_summary(
            ContextSummary(
                owner_id=scope.owner_id,
                agent_id=scope.agent_id,
                chat_id=scope.chat_id,
                summary_ref=f"summary of {scope.chat_id}",
            )
        )

    assert [m.content for m in sql_conversation_repository(store.engine).list_messages(scope=here)] == ["said in 1"]
    assert [m.content for m in sql_conversation_repository(store.engine).list_messages(scope=elsewhere)] == [
        "said in 2"
    ]
    assert sql_conversation_repository(store.engine).get_latest_context_summary(here).summary_ref == "summary of 1"
    assert sql_conversation_repository(store.engine).get_latest_context_summary(elsewhere).summary_ref == "summary of 2"
    # And the pair-wide reader still sees every conversation, which is what the sweep needs.
    pair = sql_conversation_repository(store.engine).list_pair_messages(owner_id=here.owner_id, agent_id=here.agent_id)
    assert [m.content for m in pair] == ["said in 1", "said in 2"]


def _journal_row(
    scope,
    *,
    message_id: str,
    created_at: str,
    handed_over: bool,
    actor_type: str = "owner",
    metadata: dict[str, object] | None = None,
):
    from ouroboros.storage.runtime_models import ConversationMessage

    return ConversationMessage(
        message_id=message_id,
        dedup_key=message_id,
        owner_id=scope.owner_id,
        agent_id=scope.agent_id,
        chat_id=scope.chat_id,
        source="browser",
        actor_type=actor_type,
        content=message_id,
        created_at=created_at,
        metadata={"inbound_processed": handed_over, **(metadata or {})},
    )


def test_the_journal_window_is_taken_over_the_rows_nobody_handed_over(tmp_path) -> None:
    """The marker reaches the statement, so it applies before the LIMIT rather than after."""

    store = runtime_persistence.initialize()
    sql_conversation_repository(store.engine).append_message(
        _journal_row(TEST_SCOPE, message_id="stranded", created_at="2026-01-01T00:00:00+00:00", handed_over=False),
        scope=TEST_SCOPE,
    )
    for index in range(5):
        sql_conversation_repository(store.engine).append_message(
            _journal_row(
                TEST_SCOPE, message_id=f"newer-{index}", created_at=f"2026-01-02T00:00:0{index}+00:00", handed_over=True
            ),
            scope=TEST_SCOPE,
        )

    window = sql_conversation_repository(store.engine).list_pair_messages(
        owner_id=TEST_SCOPE.owner_id,
        agent_id=TEST_SCOPE.agent_id,
        limit=2,
        actor_type="owner",
        source="browser",
        newest_first=True,
        unprocessed_only=True,
    )

    assert [message.message_id for message in window] == ["stranded"]


def test_the_journal_window_holds_only_owner_rows_until_they_are_acknowledged(tmp_path) -> None:
    """An agent's own line is never inbound, and the acknowledgement path clears the owner's."""
    from ouroboros.storage.chat_store import mark_chat_message_processed

    store = runtime_persistence.initialize()
    sql_conversation_repository(store.engine).append_message(
        _journal_row(TEST_SCOPE, message_id="stranded", created_at="2026-01-01T00:00:00+00:00", handed_over=False),
        scope=TEST_SCOPE,
    )
    sql_conversation_repository(store.engine).append_message(
        _journal_row(
            TEST_SCOPE,
            message_id="agent-said",
            created_at="2026-01-01T00:00:01+00:00",
            handed_over=False,
            actor_type="agent",
        ),
        scope=TEST_SCOPE,
    )

    def _window():
        return sql_conversation_repository(store.engine).list_pair_messages(
            owner_id=TEST_SCOPE.owner_id, agent_id=TEST_SCOPE.agent_id, source="browser", unprocessed_only=True
        )

    assert [message.message_id for message in _window()] == ["stranded"]
    mark_chat_message_processed(tmp_path, "stranded")
    assert _window() == []


def test_a_row_whose_chat_is_gone_stays_out_of_the_journal_window(tmp_path) -> None:
    """The chat predicate is in the statement, so a column nothing recomputed cannot readmit the row."""
    from sqlalchemy import text

    store = runtime_persistence.initialize()
    sql_conversation_repository(store.engine).append_message(
        _journal_row(TEST_SCOPE, message_id="stranded", created_at="2026-01-01T00:00:00+00:00", handed_over=False),
        scope=TEST_SCOPE,
    )
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE messages SET chat_id='' WHERE message_id=:message_id"), {"message_id": "stranded"})

    pair = sql_conversation_repository(store.engine).list_pair_messages(
        owner_id=TEST_SCOPE.owner_id, agent_id=TEST_SCOPE.agent_id, source="browser"
    )
    window = sql_conversation_repository(store.engine).list_pair_messages(
        owner_id=TEST_SCOPE.owner_id, agent_id=TEST_SCOPE.agent_id, source="browser", unprocessed_only=True
    )

    assert [message.message_id for message in pair] == ["stranded"]
    assert window == []


def test_a_stored_history_only_row_never_enters_the_journal_window(tmp_path) -> None:
    """The stored corpus carries the unprocessed marker on history; the `history_only` term excludes it."""

    store = runtime_persistence.initialize()
    sql_conversation_repository(store.engine).append_message(
        _journal_row(TEST_SCOPE, message_id="stranded", created_at="2026-01-01T00:00:00+00:00", handed_over=False),
        scope=TEST_SCOPE,
    )
    sql_conversation_repository(store.engine).append_message(
        _journal_row(
            TEST_SCOPE,
            message_id="unaddressed",
            created_at="2026-01-01T00:00:01+00:00",
            handed_over=False,
            metadata={"history_only": True},
        ),
        scope=TEST_SCOPE,
    )

    pair = sql_conversation_repository(store.engine).list_pair_messages(
        owner_id=TEST_SCOPE.owner_id, agent_id=TEST_SCOPE.agent_id, source="browser"
    )
    window = sql_conversation_repository(store.engine).list_pair_messages(
        owner_id=TEST_SCOPE.owner_id,
        agent_id=TEST_SCOPE.agent_id,
        source="browser",
        actor_type="owner",
        unprocessed_only=True,
    )

    assert [message.message_id for message in pair] == ["stranded", "unaddressed"]
    assert [message.message_id for message in window] == ["stranded"]


def test_the_journal_window_statement_plans_onto_the_partial_marker_index(tmp_path) -> None:
    """The bare clause is what the planner can prove the partial index from; `= :flag` is not."""
    from sqlalchemy import event

    store = runtime_persistence.initialize()
    sql_conversation_repository(store.engine).append_message(
        _journal_row(TEST_SCOPE, message_id="stranded", created_at="2026-01-01T00:00:00+00:00", handed_over=False),
        scope=TEST_SCOPE,
    )
    emitted: list[tuple[str, object]] = []

    def _capture(_conn, _cursor, statement, parameters, *_args) -> None:
        if "FROM messages" in statement:
            emitted.append((statement, parameters))

    # The plan is taken over the statement the store emitted, never a copy of it: a copy
    # would keep planning onto the index after the real one stopped.
    event.listen(store.engine, "before_cursor_execute", _capture)
    try:
        sql_conversation_repository(store.engine).list_pair_messages(
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            limit=201,
            actor_type="owner",
            source="browser",
            newest_first=True,
            unprocessed_only=True,
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture)

    assert len(emitted) == 1
    statement, parameters = emitted[0]
    with store.engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        plan = " ".join(str(line[0]) for line in conn.exec_driver_sql(f"EXPLAIN {statement}", parameters))

    assert "AND inbound_unprocessed" in " ".join(statement.split())
    assert "inbound_unprocessed =" not in statement
    assert "inbound_unprocessed=" not in statement
    assert "idx_messages_inbound_unprocessed" in plan, plan


def test_the_pair_cursor_resumes_strictly_after_it_across_the_owner_boundary(tmp_path) -> None:
    """A pair below the cursor's owner and above its agent must not be readmitted."""

    store = runtime_persistence.initialize()
    pairs = [("aaron", "a2"), ("alice", "a1"), ("alice", "a2"), ("bob", "a1")]
    for index, (owner, agent) in enumerate(pairs):
        scope = OwnerScope(owner_id=owner, agent_id=agent, surface_id=SurfaceId("browser"), chat_id="1")
        sql_conversation_repository(store.engine).append_message(
            _journal_row(scope, message_id=f"m-{index}", created_at="2026-01-01T00:00:00+00:00", handed_over=False),
            scope=scope,
        )

    walked: list[tuple[str, str]] = []
    cursor: tuple[str, str] | None = None
    for _ in range(len(pairs) + 1):
        page = sql_conversation_repository(store.engine).list_owner_agent_pairs(limit=1, start_after=cursor)
        if not page:
            break
        walked.extend(page)
        cursor = page[-1]

    assert walked == pairs
    assert sql_conversation_repository(store.engine).list_owner_agent_pairs(limit=10) == pairs


@pytest.mark.parametrize("operation", ["finish", "heartbeat", "active", "hard_timeout", "release"])
def test_execution_authority_rechecks_time_after_snapshot(operation, tmp_path, monkeypatch):
    import datetime as dt
    import json
    import time

    from sqlalchemy import text

    from new_core.identity import OwnerScope
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.task_results import HEARTBEAT_STALE_SEC

    store = runtime_persistence.initialize()
    engine = store.engine
    scope = OwnerScope(owner_id="review", agent_id="review", surface_id="browser", chat_id="review")
    store.tasks_repo.create_task(TaskRequest(id="job", detached=True, text="review", scope=scope.to_dict()))
    held = store.tasks_repo.claim_next_task(runtime_id="review")
    assert held.task_id == "job"
    with engine.begin() as conn:
        now = task_transitions.database_now(conn)
        heartbeat = now - dt.timedelta(seconds=HEARTBEAT_STALE_SEC - 0.2)
        metadata = dict(held.metadata, run_started_at=(now - dt.timedelta(seconds=1801)).isoformat())
        conn.execute(
            text("UPDATE tasks SET heartbeat_at=:hb, metadata_json=:meta WHERE task_id=:id"),
            {"hb": heartbeat.isoformat(), "meta": json.dumps(metadata), "id": "job"},
        )
    expiry = heartbeat + dt.timedelta(seconds=HEARTBEAT_STALE_SEC)
    original = task_transitions.lease_params
    observed = []

    def suspend_after_clock_read(conn, claim_token=""):
        params = original(conn, claim_token)
        time.sleep(0.4)
        observed.append(task_transitions.database_now(conn) >= expiry)
        return params

    monkeypatch.setattr(task_transitions, "lease_params", suspend_after_clock_read)
    token = held.metadata["execution_claim"]
    if operation == "finish":
        result = store.tasks_repo.finish_detached_attempt("job", status="completed", result="late", claim_token=token)
    elif operation == "heartbeat":
        result = store.tasks_repo.heartbeat_task("job", claim_token=token)
    elif operation == "active":
        result = store.tasks_repo.is_task_attempt_active("job", claim_token=token)
    else:
        result = store.tasks_repo.transition_running(
            "job",
            kind="instance_released" if operation == "release" else "hard_timeout",
            result="late",
            attempt=1,
            runtime_id="review",
            claim_token=token,
        )
    assert observed == [True], "real database time must cross expiry before conditional statement"
    assert not result, f"{operation} accepted execution authority after database expiry: {result!r}"
