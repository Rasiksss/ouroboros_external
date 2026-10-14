from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from ouroboros.storage import task_transitions
from ouroboros.storage.runtime_models import ConversationMessage, TaskRequest
from ouroboros.storage.runtime_persistence import create_runtime_persistence
from tests.helpers.conversations import make_sql_runtime_persistence, sql_conversation_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import scope_with
from tests.helpers.task_stop_state import sql_task_stop_reader


def test_postgres_detached_finish_atomically_writes_metadata(postgres_task_scope) -> None:
    urls, scope = postgres_task_scope
    store = create_runtime_persistence(configured_urls=urls)
    task_id = f"pg-finish-{uuid.uuid4().hex}"
    store.tasks_repo.create_task(TaskRequest(id=task_id, detached=True, text="work", scope=scope.to_dict()))
    claim = store.tasks_repo.claim_next_task(runtime_id="runtime")
    finished = store.tasks_repo.finish_detached_attempt(
        task_id,
        status="completed",
        result="done",
        result_ref="result-ref",
        metadata={"total_rounds": 1},
        claim_token=claim.metadata["execution_claim"],
    )

    assert finished is not None
    assert (finished.result, finished.result_ref, finished.metadata, finished.finished_at) == (
        "done",
        "result-ref",
        {"total_rounds": 1},
        store.tasks_repo.get_task(task_id).finished_at,
    )


def test_postgres_terminal_claims_are_disjoint_across_replicas(postgres_task_scope) -> None:
    urls, owner_scope = postgres_task_scope
    stores = [create_runtime_persistence(configured_urls=urls) for _ in range(2)]
    task_ids = [f"pg-terminal-{index}-{uuid.uuid4().hex}" for index in range(4)]
    barrier = threading.Barrier(2)

    def claim(store):
        barrier.wait()
        return store.result_repo.claim_terminal_jobs(limit=2)

    for task_id in task_ids:
        stores[0].tasks_repo.create_task(
            TaskRequest(id=task_id, detached=True, scope=owner_scope.to_dict()), status="cancelled"
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, stores))

    claimed_ids = [{record.task_id for record in batch} for batch in claimed]
    assert [len(batch) for batch in claimed_ids] == [2, 2]
    assert claimed_ids[0].isdisjoint(claimed_ids[1])
    assert claimed_ids[0] | claimed_ids[1] == set(task_ids)
    old = claimed[0][0]
    with stores[0].engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET terminal_publish_after='2000-01-01T00:00:00+00:00' WHERE task_id=:id"),
            {"id": old.task_id},
        )
    replacement = stores[1].result_repo.claim_terminal_jobs(limit=1)[0]
    assert replacement.task_id == old.task_id and replacement.terminal_publish_claim != old.terminal_publish_claim
    assert not stores[0].result_repo.record_terminal_delivery(
        old.task_id, claim=old.terminal_publish_claim, item="text", disposition="accepted"
    )
    assert not stores[0].result_repo.finish_terminal_publication(
        old.task_id, claim=old.terminal_publish_claim, accepted=True
    )
    assert stores[1].result_repo.record_terminal_delivery(
        old.task_id, claim=replacement.terminal_publish_claim, item="text", disposition="accepted"
    )
    assert stores[1].result_repo.finish_terminal_publication(
        old.task_id, claim=replacement.terminal_publish_claim, accepted=True
    )


@pytest.fixture
def admission_stores(postgres_task_scope):
    urls, owner_scope = postgres_task_scope
    stores = [create_runtime_persistence(configured_urls=urls) for _ in range(2)]
    try:
        yield stores, owner_scope
    finally:
        for store in stores:
            store.engine.dispose()


@parametrize_from_dict(
    {
        "ordinary-flag-off": {"enabled": False, "detached": False},
        "ordinary-flag-on": {"enabled": True, "detached": False},
        "detached": {"enabled": True, "detached": True},
    }
)
def test_admission_and_claim_races_obey_owner_limits(admission_stores, enabled, detached) -> None:
    stores, owner_scope = admission_stores
    scopes = [scope_with(owner_scope, agent_id=f"agent-{i}") for i in range(6)]
    submissions = [TaskRequest(id=uuid.uuid4().hex, scope=scope.to_dict()) for scope in scopes]
    for submission in submissions[:4]:
        assert stores[0].create_root_task.execute(submission, enabled=enabled, max_active=5).status == "queued"
    if detached:
        for submission, scope in zip(submissions[4:], scopes[4:], strict=True):
            anchor = uuid.uuid4().hex
            sql_conversation_repository(stores[0].engine).append_message(
                ConversationMessage(message_id=anchor, dedup_key=anchor, **scope.to_dict()), scope=scope
            )
            submission.detached, submission.through_message_id = True, anchor
    barrier = threading.Barrier(2)

    def admit(pair):
        store, submission = pair
        barrier.wait(timeout=5)
        return store.create_root_task.execute(submission, enabled=enabled, max_active=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = list(pool.map(admit, zip(stores, submissions[4:], strict=True)))
    assert sorted(row.status for row in admitted) == ["queued", "rejected_duplicate" if detached else "queued"]
    assert (
        stores[1].create_root_task.execute(submissions[0], enabled=enabled, max_active=5).task_id == submissions[0].id
    )

    for _ in range(2):
        assert stores[0].tasks_repo.claim_next_task(runtime_id=uuid.uuid4().hex)
    barrier = threading.Barrier(2)

    def claim(store):
        barrier.wait(timeout=5)
        return store.tasks_repo.claim_next_task(runtime_id=uuid.uuid4().hex)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, stores))
    assert sum(row is not None for row in claimed) == 1
    assert (
        sum(row.owner_id == owner_scope.owner_id for row in stores[0].tasks_repo.list_tasks(statuses=["running"])) == 3
    )


def test_postgres_stop_serializes_with_retry_successor(monkeypatch, postgres_task_scope) -> None:
    urls, scope = postgres_task_scope
    stores = [create_runtime_persistence(configured_urls=urls) for _ in range(2)]
    task_id = f"pg-stop-{uuid.uuid4().hex}"
    stores[0].tasks_repo.create_task(TaskRequest(id=task_id, detached=True, text="work", scope=scope.to_dict()))
    claimed = stores[0].tasks_repo.claim_next_task(runtime_id="runtime")
    token = claimed.metadata["execution_claim"]
    successor = TaskRequest(id=f"{task_id}-retry", job_id=task_id, detached=True, text="work", scope=scope.to_dict())
    inserting = threading.Event()
    stop_waiting = threading.Event()
    release = threading.Event()
    original = stores[0].tasks_repo._insert_task
    stop_owner_lock = task_transitions.serialize_owner

    def pause_before_insert(*args, **kwargs):
        inserting.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def observe_stop_lock(conn, owner_id):
        if conn.engine is stores[1].engine:
            stop_waiting.set()
        return stop_owner_lock(conn, owner_id)

    monkeypatch.setattr(stores[0].tasks_repo, "_insert_task", pause_before_insert)
    monkeypatch.setattr(task_transitions, "serialize_owner", observe_stop_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(
            stores[0].tasks_repo.transition_running,
            task_id,
            result="released",
            kind="instance_released",
            runtime_id="runtime",
            claim_token=token,
            attempt=1,
            successor=successor,
        )
        assert inserting.wait(5)
        stopped = pool.submit(stores[1].tasks_repo.stop_jobs, scope=scope, job_id=task_id)
        assert stop_waiting.wait(5)
        release.set()
        assert failed.result(5) is not None
        assert stopped.result(5)

    current = stores[0].tasks_repo.get_current_job(task_id, scope=scope)
    assert current is not None and current.status == "cancelled"


@pytest.mark.parametrize("transition", ["fail", "reclaim", "release"])
def test_postgres_retry_transition_observes_a_concurrent_stop(monkeypatch, transition, postgres_task_scope) -> None:
    urls, scope = postgres_task_scope
    stores = [create_runtime_persistence(configured_urls=urls) for _ in range(2)]
    task_id = f"pg-request-stop-{transition}-{uuid.uuid4().hex}"
    stores[0].tasks_repo.create_task(TaskRequest(id=task_id, detached=True, text="work", scope=scope.to_dict()))
    claimed = stores[0].tasks_repo.claim_next_task(runtime_id="runtime")
    token = claimed.metadata["execution_claim"]
    stores[0].tasks_repo.heartbeat_task(task_id, phase="model", claim_token=token)
    if transition == "reclaim":
        claimed.heartbeat_at = "2000-01-01T00:00:00+00:00"
        stores[0].tasks_repo.upsert_task(claimed)
    successor = TaskRequest(
        id=f"{task_id}-retry",
        job_id=task_id,
        detached=True,
        text="work",
        scope=scope.to_dict(),
    )
    selected = threading.Event()
    release = threading.Event()
    original = task_transitions.task_for_transition

    def pause_before_selection(conn, selected_task_id):
        if conn.engine is stores[0].engine:
            selected.set()
            assert release.wait(5)
        return original(conn, selected_task_id)

    monkeypatch.setattr(task_transitions, "task_for_transition", pause_before_selection)

    def run_transition():
        return stores[0].tasks_repo.transition_running(
            task_id,
            result="recovery",
            attempt=2,
            successor=successor,
            kind={"fail": "hard_timeout", "reclaim": "lease_lost", "release": "instance_released"}[transition],
            runtime_id="runtime",
            claim_token=token,
            hard_timeout_sec=0,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_transition)
        assert selected.wait(5)
        try:
            assert stores[1].tasks_repo.request_task_stop(task_id, scope=scope)
        finally:
            release.set()
        result = future.result(5)

    assert result is not None
    closed, successor_created = result
    assert (closed.status, closed.result, closed.error_code) == ("cancelled", "Task cancelled.", "")
    assert successor_created is False
    assert stores[0].tasks_repo.get_task(successor.id) is None


def test_postgres_stop_can_finish_after_claim_selects_its_candidate(monkeypatch, postgres_task_scope) -> None:
    urls, scope = postgres_task_scope
    stores = [create_runtime_persistence(configured_urls=urls) for _ in range(2)]
    task_id = f"pg-claim-stop-{uuid.uuid4().hex}"
    stores[0].tasks_repo.create_task(TaskRequest(id=task_id, detached=True, text="work", scope=scope.to_dict()))
    selected = threading.Event()
    release = threading.Event()
    original = task_transitions.serialize_owner

    def pause_before_owner_lock(conn, owner_id):
        if conn.engine is stores[0].engine:
            selected.set()
            assert release.wait(5)
        return original(conn, owner_id)

    monkeypatch.setattr(task_transitions, "serialize_owner", pause_before_owner_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claim = pool.submit(stores[0].tasks_repo.claim_next_task, runtime_id="runtime")
        assert selected.wait(5)
        stop = pool.submit(stores[1].tasks_repo.stop_jobs, scope=scope, job_id=task_id)
        try:
            assert stop.result(5)[0].status == "cancelled"
        finally:
            release.set()
        assert claim.result(5) is None


def test_postgres_memory_cas_race_has_one_winner(postgres_task_scope) -> None:
    from ouroboros.storage.core_memory.memory import Memory, MemoryConflictError
    from ouroboros.storage.core_memory.memory.infra.psql import PsqlMemory

    urls, scope = postgres_task_scope
    store = create_runtime_persistence(configured_urls=urls)
    memory = Memory(PsqlMemory(store.engine))
    memory.save(scope, "shared", "project", "CAS", "initial")
    initial = memory.fetch(scope, "shared")
    barrier = threading.Barrier(2)

    def save(content):
        barrier.wait()
        try:
            memory.save(scope, "shared", "project", "CAS", content, expected_updated_at=initial.updated_at)
            return "saved"
        except MemoryConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, ("first", "second")))
    assert sorted(outcomes) == ["conflict", "saved"]


@pytest.mark.parametrize("detached", [False, True])
def test_postgres_task_lifecycle_and_logical_memory_identities(postgres_task_scope, monkeypatch, detached):
    import asyncio

    from background_memory.env import MemoryEnv
    from background_memory.handlers.distillation import DistillationHandler
    from background_memory.infra.repositories import SqlAuditRepo, SqlMemoryHeaderRepo, SqlTaskRepo
    from background_memory.interfaces import MemoryDistillationEvent, MemoryTaskRef
    from infra.async_engine import create_async_runtime_engine
    from ouroboros.storage.task_episodes import store_task_episode
    from tests.helpers.user_settings import stub_user_settings

    urls, scope = postgres_task_scope
    stores = [create_runtime_persistence(configured_urls=urls) for _ in range(2)]
    task = stores[0].tasks_repo.create_task(
        TaskRequest(id=uuid.uuid4().hex, detached=detached, text="winning task", scope=scope.to_dict())
    )
    claimed = stores[0].tasks_repo.claim_next_task(runtime_id="test-runtime")
    assert claimed.task_id == task.task_id and claimed.status == "running"
    finished = (
        stores[0].tasks_repo.finish_detached_attempt(
            task.task_id, status="completed", result="winning payload", claim_token=claimed.metadata["execution_claim"]
        )
        if detached
        else stores[0].tasks_repo.complete_task(task.task_id, result="ok")
    )
    assert finished.status == "completed"
    if not detached:
        return
    barrier, calls = threading.Barrier(2), []
    event = MemoryDistillationEvent(
        scope=scope, trace_id=str(uuid.uuid4()), task=MemoryTaskRef(job_id=task.job_id, task_id=task.task_id)
    )

    async def claim():
        engine = create_async_runtime_engine(urls)
        handler = DistillationHandler(
            MemoryEnv(repo_dir="/tmp/unused", drive_root="/tmp/unused"),
            None,
            tasks=SqlTaskRepo(engine),
            audit=SqlAuditRepo(engine),
            memory_headers=SqlMemoryHeaderRepo(engine),
            user_settings=stub_user_settings(),
        )

        async def recording_distil(distilled):
            calls.append(distilled)
            return None

        monkeypatch.setattr(handler, "distil", recording_distil)
        try:
            await handler.handle(event)
        finally:
            await engine.dispose()

    def file_and_claim(store):
        barrier.wait(timeout=5)
        episode = store_task_episode(
            finished.to_agent_input(),
            finished.result,
            {},
            scope=scope,
            search_terms=[],
            created_at=finished.finished_at,
            execution_memory_repository=sql_execution_memory_repository(store.engine),
        )
        asyncio.run(claim())
        return episode

    with ThreadPoolExecutor(max_workers=2) as pool:
        episodes = list(pool.map(file_and_claim, stores))
    assert episodes[0].model_dump() == episodes[1].model_dump() and len(calls) == 1
    assert episodes[0].created_at == finished.finished_at and episodes[0].task_id == task.job_id
    assert stores[0].tasks_repo.finish_terminal_memory(task.task_id, filed=True)
    assert task.task_id not in {r.task_id for r in stores[1].tasks_repo.list_terminal_memory()}


def test_postgres_claim_stop_and_reclaim(postgres_task_scope) -> None:
    """The claim, the stop and the stale reclaim, against a real server."""
    import datetime

    urls, scope = postgres_task_scope
    store = create_runtime_persistence(configured_urls=urls)
    stop_task_id = f"pg-stop-{uuid.uuid4().hex}"
    stale_task_id = f"pg-stale-{uuid.uuid4().hex}"

    # A claim names the instance holding the run, and keeps the lane a lane.
    store.tasks_repo.create_task(
        TaskRequest(id=stop_task_id, type="task", chat_id=1, text="stop me", scope=scope.to_dict())
    )
    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="pg-runtime")
    assert claimed is not None and claimed.task_id == stop_task_id
    assert (claimed.runtime_id, claimed.worker_id) == ("pg-runtime", 0)

    # A stop is conditional, owner-scoped and idempotent.
    assert store.tasks_repo.request_task_stop(stop_task_id, scope=scope) is True
    first_stop = store.tasks_repo.get_task(stop_task_id).stop_requested_at
    assert first_stop
    assert store.tasks_repo.request_task_stop(stop_task_id, scope=scope) is False
    assert store.tasks_repo.get_task(stop_task_id).stop_requested_at == first_stop
    assert sql_task_stop_reader(store.engine).read_task_stop_state(stop_task_id).stop_requested is True
    store.tasks_repo.cancel_task(stop_task_id)

    # A reclaim applies once, and the second sweeper over the same record finds nothing.
    store.tasks_repo.create_task(
        TaskRequest(id=stale_task_id, type="task", chat_id=1, text="lost", scope=scope.to_dict())
    )
    stale = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="pg-runtime")
    assert stale is not None and stale.task_id == stale_task_id
    stale.heartbeat_at = (datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=600)).isoformat()
    store.tasks_repo.upsert_task(stale)
    cutoff = (datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=120)).isoformat()

    assert stale_task_id in [record.task_id for record in store.tasks_repo.list_stale_leases(cutoff_iso=cutoff)]
    reclaimed = store.tasks_repo.transition_running(
        stale_task_id, cutoff_iso=cutoff, result="instance stopped reporting", kind="lease_lost", attempt=2
    )
    assert reclaimed is not None
    reclaimed, successor_created = reclaimed
    assert reclaimed.status == "failed"
    assert successor_created is False
    assert (
        store.tasks_repo.transition_running(
            stale_task_id, cutoff_iso=cutoff, result="again", kind="lease_lost", attempt=3
        )
        is None
    )
    settled = store.tasks_repo.get_task(stale_task_id)
    assert (settled.status, settled.error_code, settled.attempt) == ("failed", "lease_lost", 2)


@pytest.mark.parametrize("operation", ["finish", "heartbeat", "active", "hard_timeout", "release"])
def test_execution_authority_rechecks_time_after_snapshot(operation, tmp_path, monkeypatch, postgres_task_scope):
    import datetime as dt
    import json
    import time

    from sqlalchemy import text

    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "false")
    from infra.migrations.runner import upgrade_head
    from infra.runtime_database import create_runtime_engine
    from new_core.identity import OwnerScope
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.task_results import HEARTBEAT_STALE_SEC

    urls, scope = postgres_task_scope
    engine = create_runtime_engine(urls[0])
    try:
        upgrade_head(engine)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tasks WHERE owner_id='review'"))
        store = make_sql_runtime_persistence(engine, db_url="")
        scope = OwnerScope(owner_id="review", agent_id="review", surface_id="browser", chat_id="review")
        store.tasks_repo.create_task(TaskRequest(id="job", detached=True, text="review", scope=scope.to_dict()))
        held = store.tasks_repo.claim_next_task(runtime_id="review")
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
            result = store.tasks_repo.finish_detached_attempt(
                "job", status="completed", result="late", claim_token=token
            )
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
    finally:
        engine.dispose()


def test_expired_completion_is_refused_after_scratchpad_row_lock(postgres_task_scope):
    import datetime as dt
    import threading
    import time
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import event, text

    from infra.runtime_database import create_runtime_engine
    from new_core.identity import OwnerScope
    from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.task_results import HEARTBEAT_STALE_SEC

    urls, scope = postgres_task_scope
    engine = create_runtime_engine(urls[0])
    store = make_sql_runtime_persistence(engine, db_url="")
    suffix = uuid.uuid4().hex
    scope = OwnerScope(owner_id="diff-review-" + suffix, agent_id="review", surface_id="browser", chat_id="review")
    task_id = "diff-review-" + suffix
    resume = threading.Event()
    try:
        store.tasks_repo.create_task(TaskRequest(id=task_id, detached=True, text="review", scope=scope.to_dict()))
        claim = store.tasks_repo.claim_next_task(runtime_id="diff-review")
        assert claim.task_id == task_id
        with engine.begin() as conn:
            now = task_transitions.database_now(conn)
            heartbeat = now - dt.timedelta(seconds=HEARTBEAT_STALE_SEC - 0.6)
            conn.execute(
                text("UPDATE tasks SET heartbeat_at=:hb WHERE task_id=:id"),
                {"hb": heartbeat.isoformat(), "id": task_id},
            )
        scratch_locked = threading.Event()
        finish_started = threading.Event()

        def before_cursor(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO scratchpad_blocks"):
                scratch_locked.set()
                assert resume.wait(3)
            if statement.startswith("UPDATE tasks SET status=CASE") or (
                statement.endswith("FOR UPDATE") and scratch_locked.is_set()
            ):
                finish_started.set()

        event.listen(engine, "before_cursor_execute", before_cursor)
        with ThreadPoolExecutor(2) as pool:
            scratch = pool.submit(
                SqlScratchpad(engine).append_and_evict,
                scope,
                now.isoformat(),
                "task",
                "scratch notes",
                50,
                job_id=task_id,
                task_id=task_id,
                claim_token=claim.metadata["execution_claim"],
            )
            assert scratch_locked.wait(2)
            future = pool.submit(
                store.tasks_repo.finish_detached_attempt,
                task_id,
                status="completed",
                result="late after scratchpad lock",
                claim_token=claim.metadata["execution_claim"],
            )
            assert finish_started.wait(2)
            time.sleep(0.9)
            with engine.connect() as conn:
                expired = task_transitions.database_now(conn) >= heartbeat + dt.timedelta(seconds=HEARTBEAT_STALE_SEC)
            print("expired_before_scratchpad_resume", expired, "completion_waiting", not future.done(), flush=True)
            assert expired and not future.done(), "completion must wait for the actual scratchpad lock past expiry"
            resume.set()
            with pytest.raises(ValueError, match="scratchpad job partition is closed"):
                scratch.result(3)
            result = future.result(3)
        print("completion_after_expiry", result.status if result else None, flush=True)
        assert not result, "completion accepted expired execution after real SqlScratchpad row-lock wait"
    finally:
        resume.set()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tasks WHERE task_id=:id"), {"id": task_id})
            conn.execute(text("DELETE FROM scratchpad_blocks WHERE job_id=:id"), {"id": task_id})
        engine.dispose()


def test_the_candidate_scan_takes_no_row_lock_and_the_reselect_skips_locked(postgres_task_scope) -> None:
    from sqlalchemy import event

    urls, scope = postgres_task_scope
    store = create_runtime_persistence(configured_urls=urls)
    store.tasks_repo.create_task(TaskRequest(id=f"pg-claim-{uuid.uuid4().hex}", text="work", scope=scope.to_dict()))

    statements: list[str] = []

    @event.listens_for(store.engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(str(statement).split()))

    try:
        claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    finally:
        event.remove(store.engine, "before_cursor_execute", _record)

    assert claimed is not None
    selects = [sql for sql in statements if sql.startswith("SELECT * FROM tasks")]
    assert len(selects) == 2, statements

    scan, reselect = selects
    assert "FOR UPDATE" not in scan
    assert "selected_task_id" not in scan
    assert reselect.endswith("FOR UPDATE SKIP LOCKED")
    assert "selected_task_id" in reselect
    owner_lock = next(index for index, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql)
    assert statements.index(scan) < owner_lock < statements.index(reselect)


def test_terminal_publication_locks_its_candidates_and_its_claim(postgres_task_scope) -> None:
    from sqlalchemy import event

    urls, scope = postgres_task_scope
    store = create_runtime_persistence(configured_urls=urls)
    task_id = f"pg-terminal-lock-{uuid.uuid4().hex}"
    store.tasks_repo.create_task(TaskRequest(id=task_id, detached=True, scope=scope.to_dict()), status="cancelled")

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(str(statement).split()))

    event.listen(store.engine, "before_cursor_execute", _record)
    try:
        [claimed] = store.result_repo.claim_terminal_jobs(limit=1)
        statements.clear()
        store.result_repo.record_terminal_delivery(
            task_id, claim=claimed.terminal_publish_claim, item="text", disposition="accepted"
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", _record)

    reselect = next(sql for sql in statements if sql.startswith("SELECT * FROM tasks WHERE task_id="))
    assert "terminal_publish_claim=" in reselect
    assert reselect.endswith("FOR UPDATE")


def test_the_terminal_candidate_scan_skips_rows_a_peer_holds(postgres_task_scope) -> None:
    from sqlalchemy import event

    urls, scope = postgres_task_scope
    store = create_runtime_persistence(configured_urls=urls)
    store.tasks_repo.create_task(
        TaskRequest(id=f"pg-terminal-scan-{uuid.uuid4().hex}", detached=True, scope=scope.to_dict()),
        status="cancelled",
    )

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(str(statement).split()))

    event.listen(store.engine, "before_cursor_execute", _record)
    try:
        store.result_repo.claim_terminal_jobs(limit=1)
    finally:
        event.remove(store.engine, "before_cursor_execute", _record)

    scan = next(sql for sql in statements if sql.startswith("SELECT * FROM tasks AS candidate"))
    assert scan.endswith("FOR UPDATE SKIP LOCKED")


def test_a_message_handoff_locks_the_history_row_it_serializes_senders_on(postgres_task_scope) -> None:
    from sqlalchemy import event

    from ouroboros.storage.runtime_models import ConversationMessage

    urls, scope = postgres_task_scope
    store = create_runtime_persistence(configured_urls=urls)
    message_id = f"pg-handoff-{uuid.uuid4().hex}"
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(
            message_id=message_id,
            dedup_key=message_id,
            owner_id=scope.owner_id,
            agent_id=scope.agent_id,
            chat_id=scope.chat_id,
            actor_type="agent",
            content="answer",
        ),
        scope=scope,
    )

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(str(statement).split()))

    event.listen(store.engine, "before_cursor_execute", _record)
    try:
        assert (
            sql_conversation_repository(store.engine).claim_message_handoff(message_id, scope=scope, claim="first")
            is not None
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", _record)

    lookup = next(sql for sql in statements if sql.startswith("SELECT * FROM messages WHERE message_id="))
    assert lookup.endswith("FOR UPDATE")
