"""Scratchpad over a real PostgreSQL runtime DB: adapter, job partitions, factory."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from infra.migrations.runner import upgrade_head
from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.core_memory.scratchpad.domain.service import Scratchpad
from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
from supervisor.infra.repositories.working_retrieval.repo import SqlWorkingRetrievalRepository
from tests.helpers.cleanup_checkpoint import sql_cleanup_checkpoint_repository
from tests.helpers.conversations import make_sql_runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


@pytest.fixture(autouse=True)
def _reset_core_memory_services() -> None:
    """Drop the cached service so this test binds to its own runtime DB."""
    get_scratchpad.cache_clear()


@pytest.fixture
def engine(blank_schema):
    engine = blank_schema()
    upgrade_head(engine)
    return engine


def _rows(engine) -> list[dict]:
    with engine.connect() as conn:
        query = "SELECT id, owner_id, ts, source, content, status FROM scratchpad_blocks ORDER BY id"
        return [dict(r) for r in conn.execute(text(query)).mappings()]


# ---------------------------------------------------------------- adapter


def test_eviction_keeps_content_and_database_order_with_exact_capacity(engine):
    repo = SqlScratchpad(engine)
    for i in range(SCRATCHPAD_MAX_BLOCKS + 3):
        evicted = repo.append_and_evict(TEST_SCOPE, "now", "task", str(i), SCRATCHPAD_MAX_BLOCKS)
        assert evicted == int(i >= SCRATCHPAD_MAX_BLOCKS)
        assert len(repo.list_active(TEST_SCOPE)) == min(i + 1, SCRATCHPAD_MAX_BLOCKS)
    rows = _rows(engine)
    assert [row["content"] for row in rows if row["status"] == "evicted"] == ["0", "1", "2"]
    ids = [row["id"] for row in rows]
    assert ids == sorted(set(ids)) and len(ids) == SCRATCHPAD_MAX_BLOCKS + 3


def test_a_failed_eviction_rolls_the_append_back_with_it(engine, monkeypatch):
    """Insert and FIFO eviction roll back together."""
    from ouroboros.storage.core_memory.scratchpad.infra import sql as sql_module

    scratchpad = Scratchpad(SqlScratchpad(engine))
    for i in range(SCRATCHPAD_MAX_BLOCKS):
        scratchpad.append_block(TEST_SCOPE, f"block {i}")

    monkeypatch.setattr(sql_module, "_EVICT_OVER_CAP_SQL", "UPDATE no_such_table SET status='evicted'")
    with pytest.raises(Exception, match="(?i)no_such_table"):
        scratchpad.append_block(TEST_SCOPE, "one too many")

    assert [r["content"] for r in _rows(engine)] == [f"block {i}" for i in range(SCRATCHPAD_MAX_BLOCKS)]
    assert len(scratchpad.list_blocks(TEST_SCOPE)) == SCRATCHPAD_MAX_BLOCKS


def _run_concurrently(fns: list) -> None:
    barrier = threading.Barrier(len(fns))

    def run(fn):
        barrier.wait(timeout=5)
        fn()

    with ThreadPoolExecutor(len(fns)) as pool:
        for future in [pool.submit(run, fn) for fn in fns]:
            future.result(timeout=30)


def _scratch_authority(store, scope, job):
    task = store.tasks_repo.get_current_job(job, scope=scope) if job else None
    return {"task_id": task.task_id, "claim_token": task.metadata["execution_claim"]} if task else {}


def test_concurrent_partition_appends_keep_exact_capacity_and_independent_pg_locks(partitions):
    store, pad, scope = partitions
    job = scope.owner_id + "one"
    pad.append_block(scope, "main")
    for i in range(10):
        pad.append_block(scope, str(i), job_id=job, **_scratch_authority(store, scope, job))
    _run_concurrently(
        [
            lambda i=i: pad.append_block(scope, str(i), job_id=job, **_scratch_authority(store, scope, job))
            for i in range(18)
        ]
    )
    assert len(pad.list_blocks(scope, job_id=job)) == 10
    assert [row.content for row in pad.list_blocks(scope)] == ["main"]
    assert len(_rows(store.engine)) == 29
    started, resume = threading.Event(), threading.Event()

    def hold(conn, cursor, statement, *args):
        if statement.startswith("INSERT INTO scratchpad_blocks") and threading.current_thread().name.startswith(
            "pad-lock"
        ):
            started.set()
            assert resume.wait(5)

    event.listen(store.engine, "before_cursor_execute", hold)
    try:
        with ThreadPoolExecutor(1, thread_name_prefix="pad-lock") as pool:
            blocked = pool.submit(pad.append_block, scope, "held", job_id=job, **_scratch_authority(store, scope, job))
            try:
                assert started.wait(5)
                with ThreadPoolExecutor(1) as other:
                    other.submit(pad.append_block, scope, "independent").result(timeout=1)
                    other.submit(
                        pad.append_block,
                        scope,
                        "independent",
                        job_id=scope.owner_id + "two",
                        **_scratch_authority(store, scope, scope.owner_id + "two"),
                    ).result(timeout=1)
            finally:
                resume.set()
            blocked.result(timeout=5)
    finally:
        event.remove(store.engine, "before_cursor_execute", hold)


@parametrize_from_dict({axis: {"axis": axis} for axis in ("owner_id", "agent_id", "chat_id", "surface_id")})
def test_partitions_isolate_both_ownership_axes(engine, axis):
    pad = Scratchpad(SqlScratchpad(engine))
    other = scope_with(TEST_SCOPE, **{axis: "dialogs" if axis == "surface_id" else "other"})
    pad.append_block(TEST_SCOPE, "mine")
    pad.append_block(other, "theirs")
    assert [row.content for row in pad.list_blocks(TEST_SCOPE)] == ["mine"]
    assert [row.content for row in pad.list_blocks(other)] == ["theirs"]
    for i in range(SCRATCHPAD_MAX_BLOCKS + 1):
        pad.append_block(other, str(i))
    assert [row.content for row in pad.list_blocks(TEST_SCOPE)] == ["mine"]
    pad.replace_blocks(other, [])
    assert [row.content for row in pad.list_blocks(TEST_SCOPE)] == ["mine"]
    resumed = Scratchpad(SqlScratchpad(engine))
    assert [row.content for row in resumed.list_blocks(TEST_SCOPE)] == ["mine"]


@pytest.fixture
def partitions(engine):
    from ouroboros.storage.runtime_models import TaskRequest

    scope = scope_with(TEST_SCOPE, owner_id=uuid.uuid4().hex)
    store = make_sql_runtime_persistence(engine, db_url=engine.url.render_as_string(hide_password=False))
    pad = Scratchpad(SqlScratchpad(engine))
    for job in ("one", "two"):
        store.tasks_repo.create_task(
            TaskRequest(id=scope.owner_id + job, job_id=scope.owner_id + job, detached=True, scope=scope.to_dict())
        )
        assert store.tasks_repo.claim_next_task(runtime_id="runtime") is not None
    return store, pad, scope


def test_partition_replacement_retires_old_blocks_and_allocates_order_atomically(partitions, monkeypatch):
    from ouroboros.storage.core_memory.scratchpad.infra import sql
    from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow

    store, pad, scope = partitions
    job = scope.owner_id + "one"
    for selected in ("", job, scope.owner_id + "two"):
        pad.append_block(scope, selected or "main", job_id=selected, **_scratch_authority(store, scope, selected))
    old = pad.list_blocks(scope, job_id=job)[0]
    blocks = [BlockRow(999, "2030" if i == 0 else "2000", "replacement", f"new {i}") for i in range(2)]
    pad.replace_blocks(scope, blocks, job_id=job, **_scratch_authority(store, scope, job))
    current = pad.list_blocks(scope, job_id=job)
    assert [row.content for row in current] == ["new 0", "new 1"]
    assert old.id < current[0].id < current[1].id and 999 not in [row.id for row in current]
    assert next(row for row in _rows(store.engine) if row["id"] == old.id)["status"] == "evicted"
    assert [row.content for row in pad.list_blocks(scope)] == ["main"]
    assert len(pad.list_blocks(scope, job_id=scope.owner_id + "two")) == 1
    with monkeypatch.context() as patch:
        patch.setattr(sql, "_INSERT_SQL", "INSERT INTO missing_table VALUES (1)")
        with pytest.raises(Exception, match="missing_table"):
            pad.replace_blocks(scope, blocks, job_id=job, **_scratch_authority(store, scope, job))
    assert pad.list_blocks(scope, job_id=job) == current
    pad.replace_blocks(scope, [])
    assert pad.list_blocks(scope) == [] and pad.list_blocks(scope, job_id=job) == current


@pytest.mark.parametrize("pause", ["begin", "before_cursor_execute"])
def test_delayed_writer_cannot_recreate_a_terminal_partition_after_bounded_cleanup(partitions, pause):
    store, pad, scope = partitions
    job = scope.owner_id + "one"
    assert pad.list_blocks(scope, job_id=job) == []
    for selected in ("", job, scope.owner_id + "two"):
        for i in range(12):
            pad.append_block(scope, str(i), job_id=selected, **_scratch_authority(store, scope, selected))
    started, resume = threading.Event(), threading.Event()

    def delay(conn, *args):
        if threading.current_thread().name.startswith("delayed-pad") and (
            not args or args[1].startswith("INSERT INTO scratchpad_blocks")
        ):
            started.set()
            assert resume.wait(5)

    def close():
        store.tasks_repo.stop_jobs(scope=scope, job_id=job)
        store.tasks_repo.finish_detached_attempt(
            job, status="cancelled", claim_token=store.tasks_repo.get_task(job).metadata["execution_claim"]
        )
        for _ in range(7):
            assert (
                SqlWorkingRetrievalRepository(
                    store.engine, scratchpad=SqlScratchpad(store.engine)
                ).cleanup_expired_working_retrieval(now_iso="", limit=2, cleanup_retrieval=False)["scratchpad_blocks"]
                <= 2
            )

    event.listen(store.engine, pause, delay)
    try:
        with ThreadPoolExecutor(1, thread_name_prefix="delayed-pad") as pool, ThreadPoolExecutor(1) as closer:
            writer = pool.submit(pad.append_block, scope, "stale", job_id=job, **_scratch_authority(store, scope, job))
            try:
                assert started.wait(5)
                cleanup = closer.submit(close)
                if pause == "begin":
                    cleanup.result(timeout=3)
                else:
                    with pytest.raises(FutureTimeout):
                        cleanup.result(timeout=0.1)
            finally:
                resume.set()
            cleanup.result(timeout=3)
            if pause == "begin":
                with pytest.raises(ValueError, match="closed"):
                    writer.result(timeout=3)
            else:
                writer.result(timeout=3)
    finally:
        event.remove(store.engine, pause, delay)
    assert len(_rows(store.engine)) == 24 and pad.list_blocks(scope, job_id=job) == []
    assert len(pad.list_blocks(scope)) == len(pad.list_blocks(scope, job_id=scope.owner_id + "two")) == 10


def test_terminal_cleanup_keeps_a_successors_partition_when_retrieval_is_disabled(partitions, monkeypatch):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.utils import utc_now_iso
    from supervisor.runtime import _maybe_cleanup_working_retrieval

    store, pad, scope = partitions
    claim = store.tasks_repo.get_task(scope.owner_id + "one")
    job = claim.job_id
    pad.append_block(scope, "attempt one", job_id=job, **_scratch_authority(store, scope, job))
    successor = TaskRequest.model_validate({**claim.input, "id": uuid.uuid4().hex})
    store.tasks_repo.transition_running(
        claim.task_id,
        kind="instance_released",
        result="release",
        attempt=1,
        runtime_id="runtime",
        claim_token=claim.metadata["execution_claim"],
        successor=successor,
    )
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_ENABLED", "false")
    _maybe_cleanup_working_retrieval(
        SimpleNamespace(store=store, cleanup_checkpoint_repository=sql_cleanup_checkpoint_repository(store.engine)),
        now_iso=utc_now_iso(),
    )
    assert [row.content for row in pad.list_blocks(scope, job_id=job)] == ["attempt one"]
    next_claim = store.tasks_repo.claim_next_task(runtime_id="runtime")
    assert next_claim.job_id == job
    pad.append_block(scope, "attempt two", job_id=job, **_scratch_authority(store, scope, job))
    store.tasks_repo.stop_jobs(scope=scope, job_id=job)
    store.tasks_repo.finish_detached_attempt(
        next_claim.task_id, status="cancelled", claim_token=next_claim.metadata["execution_claim"]
    )
    assert (
        SqlWorkingRetrievalRepository(
            store.engine, scratchpad=SqlScratchpad(store.engine)
        ).cleanup_expired_working_retrieval(now_iso="", cleanup_retrieval=False)["scratchpad_blocks"]
        == 2
    )


# ---------------------------------------------------------------- factory


def test_cache_clear_rebinds_to_the_engine_the_process_composed(tmp_path):
    from ouroboros.storage.runtime_persistence import runtime_persistence

    before = get_scratchpad()
    runtime_persistence.close()
    recomposed = runtime_persistence.initialize()

    assert get_scratchpad() is before

    get_scratchpad.cache_clear()

    assert get_scratchpad() is not before
    assert get_scratchpad()._repo._engine is recomposed.engine


def test_an_append_takes_the_partition_lock_before_the_rows_it_reads(engine):
    repo = SqlScratchpad(engine)
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(str(statement).split()))

    event.listen(engine, "before_cursor_execute", _record)
    try:
        repo.append_and_evict(TEST_SCOPE, "now", "task", "foreground work", SCRATCHPAD_MAX_BLOCKS)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    partition_lock = next(
        index
        for index, sql in enumerate(statements)
        if "pg_advisory_xact_lock(hashtextextended(%(partition)s, 0))" in sql
    )
    reads = [index for index, sql in enumerate(statements) if "FROM scratchpad_blocks" in sql]

    assert reads, statements
    assert partition_lock < min(reads)
