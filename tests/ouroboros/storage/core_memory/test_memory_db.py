"""Unified memory over a real PostgreSQL runtime DB: adapter, concurrency, factory."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from infra.migrations.runner import upgrade_head
from ouroboros.storage.core_memory.memory import MemoryFactory, MemoryNotFoundError, get_memory
from ouroboros.storage.core_memory.memory.domain.models import policy_for
from ouroboros.storage.core_memory.memory.domain.service import Memory
from ouroboros.storage.core_memory.memory.infra.psql import PsqlMemory
from ouroboros.storage.core_memory.memory.interfaces.memory import MemoryRow
from tests.helpers.conversations import make_sql_runtime_persistence
from tests.helpers.memory import MEMORY_SECTION
from tests.helpers.revision_schema import query_plan, table_names
from tests.helpers.shared import TEST_SCOPE, scope_with


@pytest.fixture
def engine(blank_schema):
    engine = blank_schema()
    upgrade_head(engine)
    return engine


@pytest.fixture
def memory(engine) -> Memory:
    return Memory(PsqlMemory(engine))


@pytest.fixture
def memory_backend(engine, memory):

    scope = scope_with(TEST_SCOPE, owner_id=uuid.uuid4().hex)
    store = make_sql_runtime_persistence(engine, db_url=engine.url.render_as_string(hide_password=False))
    return store, memory, scope


@pytest.mark.parametrize("loss", ["stop", "expired", "superseded", "live", "foreground", "distillation"])
@pytest.mark.parametrize("operation", ["overwrite", "append", "delete", "prune"])
def test_named_memory_fences_mutations_after_lock_wait(memory_backend, tmp_path, monkeypatch, loss, operation):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.tools import memory_tools
    from tests.helpers.shared import make_catalog, make_tool_ctx, run_tool, static_tool_provider

    store, memory, scope = memory_backend
    monkeypatch.setattr(
        runtime_persistence,
        "_persistence",
        store,
    )
    monkeypatch.setattr(memory_tools, "get_memory", lambda: memory)
    name, kind = "old", "project"
    memory.save(scope, name, kind, "d", "original")
    if operation == "prune":
        name = "new"
        monkeypatch.setattr(memory, "_select_overflow", lambda kind, headers: ["old"])
    before = memory.list_headers(scope)
    detached = loss not in {"foreground", "distillation"}
    store.tasks_repo.create_task(TaskRequest(id=scope.owner_id, detached=detached, scope=scope.to_dict()))
    claim = store.tasks_repo.claim_next_task(runtime_id=scope.owner_id)
    ctx = make_tool_ctx(
        drive_root=tmp_path,
        repo_dir=tmp_path,
        scope=scope,
        task_id=claim.task_id,
        detached=detached,
        execution_claim=claim.metadata["execution_claim"] if loss != "distillation" else "",
        current_task_type="distillation" if loss == "distillation" else "task",
    )
    catalog = make_catalog([static_tool_provider(*memory_tools.get_tools(ctx))])
    tool = "memory_delete" if operation == "delete" else "memory_save"
    args = {
        "name": name,
        "memory_type": kind,
        "description": "d",
        "content": "changed",
        "mode": "append" if operation == "append" else "overwrite",
    }
    reached = threading.Event()

    def waiting(conn, cursor, statement, *unused):
        if "pg_advisory_xact_lock" in statement or "FOR UPDATE" in statement or "DELETE FROM memory" in statement:
            reached.set()

    with store.engine.connect() as blocker, ThreadPoolExecutor(1) as pool:
        lock_sql = memory._repo.kind_lock_sql
        if operation == "delete":
            lock_sql = "SELECT name FROM memory WHERE owner_id=:owner_id FOR UPDATE"
        blocker.execute(text(lock_sql), {**scope.to_dict(), "kind": kind})
        event.listen(store.engine, "before_cursor_execute", waiting)
        try:
            future = pool.submit(run_tool, catalog, ctx, tool, args)
            assert reached.wait(3)
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.05)
            changes = {
                "stop": "stop_requested_at='2026-01-01'",
                "expired": "heartbeat_at='2000-01-01T00:00:00+00:00'",
                "superseded": "metadata_json='{}'",
            }
            if loss in changes:
                blocker.execute(text(f"UPDATE tasks SET {changes[loss]} WHERE task_id=:id"), {"id": claim.task_id})
            blocker.commit()
            result = future.result(timeout=3)
        finally:
            blocker.rollback()
            event.remove(store.engine, "before_cursor_execute", waiting)
    if loss in changes:
        assert memory.list_headers(scope) == before
        assert memory.fetch(scope, before[0].name).content.strip() == "original"
        assert "authority" in result.lower()
    else:
        assert "✅" in result
        assert (memory.fetch(scope, name) is None) == (operation == "delete")


def _rows(engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text("SELECT * FROM memory ORDER BY name")).mappings()]


# ------------------------------------------------------------------- adapter


def test_the_schema_holds_no_separate_memory_stores(engine):
    tables = table_names(engine)

    assert "memory" in tables
    assert "memory_topics" not in tables
    assert "knowledge_topics" not in tables
    assert "scratchpad_blocks" in tables


def test_crud_roundtrip(memory):
    assert memory.save(TEST_SCOPE, "user_role", "user", "Who the user is", "Senior engineer.") == "user_role"

    content, first_recorded_at, updated_at = memory.read(TEST_SCOPE, "user_role")
    assert "Senior engineer." in content
    assert "name: user_role" in content
    assert first_recorded_at > 0
    assert updated_at > 0

    index, truncated = memory.render_index(TEST_SCOPE, MEMORY_SECTION)
    assert truncated is False
    assert "n=user_role" in index

    assert memory.delete(TEST_SCOPE, "user_role") is True
    assert memory.delete(TEST_SCOPE, "user_role") is False
    with pytest.raises(MemoryNotFoundError):
        memory.read(TEST_SCOPE, "user_role")


def test_a_fact_and_a_knowledge_memory_share_one_table(engine, memory):
    memory.save(TEST_SCOPE, "project_x", "project", "Project context", "project body")
    memory.save(TEST_SCOPE, "git-recipes", "knowledge", "Rebase workflow", "knowledge body")

    rows = {row["name"]: row for row in _rows(engine)}
    assert rows["project_x"]["kind"] == "project"
    assert rows["git-recipes"]["kind"] == "knowledge"
    assert rows["project_x"]["first_recorded_at"] > 0
    assert rows["git-recipes"]["first_recorded_at"] > 0


def test_upsert_replaces_the_row_in_place(engine, memory):
    memory.save(TEST_SCOPE, "project_x", "project", "first", "first content")
    memory.save(TEST_SCOPE, "project_x", "project", "second", "second content")

    rows = _rows(engine)
    assert len(rows) == 1
    row = next(r for r in rows if r["name"] == "project_x")
    assert row["description"] == "second"
    assert "second content" in row["content"]
    assert "first content" not in row["content"]


def test_a_name_taken_by_one_kind_cannot_be_reused_by_another(engine, memory):
    memory.save(TEST_SCOPE, "deploy", "knowledge", "How deploys work", "knowledge body")
    memory.save(TEST_SCOPE, "deploy", "project", "Current deploy state", "project body")

    named = [row for row in _rows(engine) if row["name"] == "deploy"]
    assert len(named) == 1
    assert named[0]["kind"] == "project"


def test_append_concatenates_through_the_transaction(engine, memory):
    memory.save(TEST_SCOPE, "t", "knowledge", "d", "# Heading\n\nalpha")
    memory.save(TEST_SCOPE, "t", "knowledge", "d", "beta\n", mode="append")

    row = next(r for r in _rows(engine) if r["name"] == "t")
    assert row["content"] == "# Heading\n\nalpha\nbeta\n"


def test_append_to_an_absent_memory_creates_it(memory):
    memory.save(TEST_SCOPE, "fresh", "project", "d", "body\n", mode="append")

    assert "body" in memory.read(TEST_SCOPE, "fresh")[0]


def test_mark_recalled_and_list_order(memory, monkeypatch):
    import ouroboros.storage.core_memory.memory.domain.service as service_mod

    class _Clock:
        def __init__(self) -> None:
            self._t = 1_700_000_000.0

        def time(self) -> float:
            self._t += 1.0
            return self._t

    monkeypatch.setattr(service_mod, "time", _Clock())

    memory.save(TEST_SCOPE, "oldest", "project", "d", "c")
    memory.save(TEST_SCOPE, "newest", "project", "d", "c")

    assert [h.name for h in memory.list_headers(TEST_SCOPE) if h.kind == "project"] == ["newest", "oldest"]

    memory.mark_recalled(TEST_SCOPE, ["oldest"])
    stats = {h.name: (h.recall_count, h.last_recalled_at) for h in memory.list_headers(TEST_SCOPE)}
    assert stats["oldest"][0] == 1
    assert stats["oldest"][1] > 0
    assert stats["newest"] == (0, 0.0)


def test_owners_do_not_see_each_others_memories(memory):
    other_scope = scope_with(TEST_SCOPE, owner_id="other")

    memory.save(TEST_SCOPE, "shared", "project", "d", "global body\n")

    assert memory.fetch(other_scope, "shared") is None
    assert memory.list_headers(other_scope) == []


def test_agents_of_one_owner_do_not_see_each_others_memories(memory):
    """The ownership key is the pair: one owner's two agents keep separate namespaces."""
    second = scope_with(TEST_SCOPE, agent_id="second")

    memory.save(TEST_SCOPE, "shared", "project", "d", "main body\n")
    memory.save(second, "shared", "project", "d", "second body\n")

    # read() returns the rendered document, frontmatter included; the body is what differs.
    assert "main body" in memory.read(TEST_SCOPE, "shared")[0]
    assert "second body" in memory.read(second, "shared")[0]
    assert "second body" not in memory.read(TEST_SCOPE, "shared")[0]
    # One row per scope under that name — the pair keys it, so neither shadows the other.
    assert [h.name for h in memory.list_headers(TEST_SCOPE)].count("shared") == 1
    assert [h.name for h in memory.list_headers(second)].count("shared") == 1


# ------------------------------------------------------- body-bearing reads


def test_every_body_bearing_read_is_keyed_by_name():
    """D12's structural guard: no listing, index, or capacity check can pull a body.

    One statement reads ``content`` — a single name — and the write lock-select
    is that same statement plus its lock clause. The guard is the shape of
    the key, not the count: anything selecting a body must say which name it
    wants, so no listing can grow into one.
    """
    import ouroboros.storage.core_memory.memory.infra._sql_base as base

    statements = [
        value if isinstance(value, str) else str(getattr(value, "text", ""))
        for name, value in vars(base).items()
        if name.endswith("_SQL")
    ]
    statements += [PsqlMemory.lock_select_sql]

    keyed_by_name = (
        "SELECT owner_id, agent_id, kind, name, description, content, status, source, "
        "first_recorded_at, updated_at, last_recalled_at, recall_count "
        "FROM memory "
        "WHERE owner_id=:owner_id AND agent_id=:agent_id AND name=:name"
    )
    selects_with_content = [
        " ".join(sql.split()) for sql in statements if sql.strip().upper().startswith("SELECT") and "content" in sql
    ]
    assert len(selects_with_content) >= 1, "expected the single-name fetch statement"
    for sql in selects_with_content:
        assert sql.startswith(keyed_by_name), f"a body-bearing read that is not keyed by name: {sql!r}"


def test_list_headers_carries_no_body(memory):
    memory.save(TEST_SCOPE, "a", "knowledge", "d", "a very long body the index must never load\n")

    headers = memory.list_headers(TEST_SCOPE)

    assert "a" in {h.name for h in headers}
    assert not hasattr(headers[0], "content")


def _memory_sql(engine) -> list[tuple[str, Any]]:
    """Record (statement, parameters) for every statement against ``memory``."""
    recorded: list[tuple[str, Any]] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        if "FROM memory" in " ".join(str(statement).split()):
            recorded.append((statement, parameters))

    return recorded


def test_the_prompt_listing_excludes_archived_rows_in_the_query(engine, memory):
    """The status predicate is the store's, not a filter every caller repeats."""
    memory.save(TEST_SCOPE, "kept", "project", "d", "c")
    memory.save(TEST_SCOPE, "gone", "project", "d", "c")
    memory._repo.set_status(TEST_SCOPE, ["gone"], "archived")
    statements = _memory_sql(engine)

    prompt_names = {h.name for h in memory.list_prompt_headers(TEST_SCOPE)}

    assert prompt_names == {"kept"}
    assert "status IN" in " ".join(statements[0][0].split())
    # The audit listing still sees everything.
    assert {h.name for h in memory.list_headers(TEST_SCOPE)} == {"kept", "gone"}


def test_the_prompt_listing_runs_on_the_index_without_a_sort(engine, memory):
    """Regression: the shipped index named columns no predicate uses, so this was a scan + filesort."""
    memory.save(TEST_SCOPE, "a", "project", "d", "c")
    statements = _memory_sql(engine)

    memory.list_prompt_headers(TEST_SCOPE)

    statement, parameters = statements[-1]
    plan = query_plan(engine, statement, parameters)

    assert "idx_memory_owner_status_updated" in plan, plan
    assert "Sort" not in plan, plan


def test_the_table_refuses_a_status_outside_the_closed_pair(engine, memory):
    """``superseded`` was the legacy value; the migration maps it in, the CHECK keeps it out.

    Without it the fail-open normalizer would call an unknown status active
    while the prompt listing's SQL called it absent.
    """
    memory.save(TEST_SCOPE, "a", "project", "d", "c")

    with pytest.raises(IntegrityError):
        with engine.connect() as conn:
            conn.execute(text("UPDATE memory SET status='superseded' WHERE name='a'"))
            conn.commit()


# --------------------------------------------------------------- concurrency


def _run_concurrently(fns: list) -> None:
    barrier = threading.Barrier(len(fns))
    errors: list[BaseException] = []

    def _wrapped(fn):
        def run():
            barrier.wait()
            try:
                fn()
            except BaseException as e:
                errors.append(e)

        return run

    threads = [threading.Thread(target=_wrapped(fn)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    # A thread still alive means the lock deadlocked rather than serialized —
    # without this the test would pass on whatever the survivors happened to write.
    assert not [t for t in threads if t.is_alive()], "a worker did not finish"
    assert not errors, errors


def test_concurrent_appends_to_one_memory_both_land(memory_backend):
    """The race the transaction closes: without it one fragment is lost."""
    store, memory, scope = memory_backend
    engine = store.engine
    memory.save(scope, "t", "project", "d", "base")

    _run_concurrently(
        [
            lambda: memory.save(scope, "t", "project", "d", "from-first\n", mode="append"),
            lambda: memory.save(scope, "t", "project", "d", "from-second\n", mode="append"),
        ]
    )

    content = next(r for r in _rows(engine) if r["name"] == "t")["content"]
    assert "from-first" in content
    assert "from-second" in content
    assert content.startswith("base\n")


def test_concurrent_appends_that_both_create_the_memory_lose_nothing(memory_backend):
    store, memory, scope = memory_backend
    engine = store.engine

    _run_concurrently(
        [
            lambda: memory.save(scope, "fresh", "project", "d", "from-first\n", mode="append"),
            lambda: memory.save(scope, "fresh", "project", "d", "from-second\n", mode="append"),
        ]
    )

    content = next(r for r in _rows(engine) if r["name"] == "fresh")["content"]
    assert "from-first" in content
    assert "from-second" in content


def test_a_save_takes_the_kind_lock_and_the_name_lock_before_the_rows_it_reads(engine, memory):
    """Per-name ``FOR UPDATE`` does not serialize the prune cohort across different names
    of one kind, so the write transaction takes an explicit advisory lock as well."""
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(str(statement).split()))

    memory.save(TEST_SCOPE, "a", "user", "d", "c")

    kind_lock = next(
        index
        for index, sql in enumerate(statements)
        if "pg_advisory_xact_lock(hashtext(%(owner_id)s || ':' || %(agent_id)s), hashtext(%(kind)s))" in sql
    )
    name_lock = next(
        index
        for index, sql in enumerate(statements)
        if "pg_advisory_xact_lock(hashtextextended(%(name_key)s, 0))" in sql
    )
    locked_reads = [index for index, sql in enumerate(statements) if "FOR UPDATE" in sql]

    assert len(locked_reads) >= 2
    assert kind_lock < name_lock < min(locked_reads)


def test_concurrent_writers_do_not_lose_memories(memory):
    _run_concurrently(
        [(lambda i=i: memory.save(TEST_SCOPE, f"fact_{i}", "project", f"d {i}", f"c {i}")) for i in range(12)]
    )

    names = {h.name for h in memory.list_headers(TEST_SCOPE)}
    assert {f"fact_{i}" for i in range(12)} <= names


def test_concurrent_same_kind_saves_do_not_exceed_capacity(memory_backend):
    """The prune rides the write transaction, so a race cannot leave a kind over
    capacity — not even transiently by the time the last save returns."""
    store, memory, scope = memory_backend
    engine = store.engine
    capacity = policy_for("user").capacity

    _run_concurrently(
        [(lambda i=i: memory.save(scope, f"user_{i}", "user", f"d {i}", f"c {i}")) for i in range(capacity + 8)]
    )

    active = [h for h in memory.list_headers(scope) if h.kind == "user" and h.status == "active"]
    assert len(active) == capacity
    # Every save committed a row; the overflow is archived, not dropped.
    stored = [r for r in _rows(engine) if r["kind"] == "user"]
    assert len(stored) == capacity + 8


def test_write_and_prune_rolls_back_the_write_when_the_prune_raises(engine):
    """Failure atomicity: the write and its prune commit together or not at all."""
    repo = PsqlMemory(engine)

    def _write(_current):
        return MemoryRow(name="boom", kind="project", description="d", content="c\n", status="active")

    def _prune(_headers):
        raise RuntimeError("prune blew up")

    with pytest.raises(RuntimeError, match="prune blew up"):
        repo.write_and_prune(TEST_SCOPE, "project", "boom", _write, _prune, statuses=frozenset({"active"}))

    assert repo.fetch(TEST_SCOPE, "boom") is None


def test_a_concurrent_overwrite_is_not_evicted_by_a_racing_prune(memory_backend):
    """A row one save refreshes (bumping updated_at to newest) cannot be archived by
    another same-kind save's prune, because the two saves now serialize."""
    store, memory, scope = memory_backend
    engine = store.engine
    capacity = policy_for("user").capacity
    for i in range(capacity):
        memory.save(scope, f"user_{i}", "user", f"d {i}", f"c {i}")

    # user_0 is the oldest — the eviction candidate. Concurrently refresh it while
    # a new row pushes the kind over capacity.
    _run_concurrently(
        [
            lambda: memory.save(scope, "user_0", "user", "d 0", "refreshed\n"),
            lambda: memory.save(scope, f"user_{capacity}", "user", "d new", "c new"),
        ]
    )

    by_name = {r["name"]: r for r in _rows(engine) if r["kind"] == "user"}
    assert by_name["user_0"]["status"] == "active"
    assert "refreshed" in by_name["user_0"]["content"]
    active = [r for r in by_name.values() if r["status"] == "active"]
    assert len(active) == capacity


def test_get_memory_caches_one_service_per_process():
    first = get_memory()
    assert get_memory() is first

    first.save(TEST_SCOPE, "user_role", "user", "d", "c")
    assert "user_role" in {h.name for h in get_memory().list_headers(TEST_SCOPE)}


def test_cache_clear_rebinds_to_the_engine_the_process_composed(tmp_path):
    """cache_clear() is the rebinding hook — how tests stay isolated."""
    from ouroboros.storage.runtime_persistence import runtime_persistence

    before = get_memory()
    runtime_persistence.close()
    recomposed = runtime_persistence.initialize()

    assert get_memory() is before

    get_memory.cache_clear()

    assert get_memory() is not before
    assert get_memory()._repo._engine is recomposed.engine


def test_factory_builds_against_current_settings(tmp_path):
    """The factory itself holds nothing: each build follows the composed engine."""
    from ouroboros.storage.runtime_persistence import runtime_persistence

    factory = MemoryFactory()
    first = factory.get_memory()
    runtime_persistence.close()
    recomposed = runtime_persistence.initialize()

    assert first._repo._engine is not recomposed.engine
    assert factory.get_memory()._repo._engine is recomposed.engine


def test_no_sibling_factories_remain():
    import ouroboros.storage.core_memory as core_memory

    assert not hasattr(core_memory, "get_identity")
    assert not hasattr(core_memory, "get_knowledge")
    assert not hasattr(core_memory, "get_long_term_memory")
