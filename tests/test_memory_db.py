"""Unified memory over a real (tmp) SQLite runtime DB: adapter, concurrency, factory, migration."""

from __future__ import annotations

import json
import pathlib
import threading
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from ouroboros.storage.core_memory.memory import MemoryFactory, MemoryNotFoundError, get_memory
from ouroboros.storage.core_memory.memory.domain.models import DEFAULT_IDENTITY, policy_for
from ouroboros.storage.core_memory.memory.domain.service import Memory
from ouroboros.storage.core_memory.memory.infra.psql import _ENSURE_ROW_SQL as _PSQL_ENSURE_ROW_SQL
from ouroboros.storage.core_memory.memory.infra.sqlite import SqliteMemory
from ouroboros.storage.core_memory.memory.interfaces.memory import MemoryRow
from ouroboros.storage.migrations.runner import upgrade_head
from ouroboros.storage.runtime_factory import sqlite_url_for_path
from ouroboros.storage.sql_runtime_store import create_runtime_engine
from tests.conftest import fail_reads_of, refresh_test_config
from tests.test_memory_domain import KNOWLEDGE_SECTION, MEMORY_SECTION


@pytest.fixture
def drive_root(tmp_path, monkeypatch) -> pathlib.Path:
    root = tmp_path / "ouroboros_data"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "file")
    return root


def _upgraded_engine(tmp_path: pathlib.Path):
    engine = create_runtime_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    upgrade_head(engine)
    return engine


def _migration_module():
    """The 0006 module, for the helpers it exposes at module level.

    Alembic executes the file as an object of its own, so this copy is only good
    for reading module-level definitions and calling the pure helpers — never
    for patching what a live migration run will execute.
    """
    import importlib

    return importlib.import_module("ouroboros.storage.migrations.versions.0006_core_memory")


def _memory_on_tmp_db(tmp_path: pathlib.Path) -> Memory:
    return Memory(SqliteMemory(_upgraded_engine(tmp_path)))


def _rows(engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text("SELECT * FROM memory ORDER BY name")).mappings()]


# ------------------------------------------------------------------- adapter


def test_crud_roundtrip(tmp_path):
    memory = _memory_on_tmp_db(tmp_path)

    assert memory.save("user_role", "user", "Who the user is", "Senior engineer.") == "user_role"

    content, first_recorded_at, updated_at = memory.read("user_role")
    assert "Senior engineer." in content
    assert "name: user_role" in content
    assert first_recorded_at > 0
    assert updated_at > 0

    index, truncated = memory.render_index(MEMORY_SECTION)
    assert truncated is False
    assert "n=user_role" in index

    assert memory.delete("user_role") is True
    assert memory.delete("user_role") is False
    with pytest.raises(MemoryNotFoundError):
        memory.read("user_role")


def test_a_fact_and_a_knowledge_memory_share_one_table(tmp_path):
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))

    memory.save("project_x", "project", "Project context", "project body")
    memory.save("git-recipes", "knowledge", "Rebase workflow", "knowledge body")

    rows = {row["name"]: row for row in _rows(engine)}
    assert rows["project_x"]["kind"] == "project"
    assert rows["git-recipes"]["kind"] == "knowledge"
    assert rows["project_x"]["first_recorded_at"] > 0
    assert rows["git-recipes"]["first_recorded_at"] > 0


def test_upsert_replaces_the_row_in_place(tmp_path):
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))

    memory.save("project_x", "project", "first", "first content")
    memory.save("project_x", "project", "second", "second content")

    rows = _rows(engine)
    assert len(rows) == 2  # the seeded identity row plus this one
    row = next(r for r in rows if r["name"] == "project_x")
    assert row["description"] == "second"
    assert "second content" in row["content"]
    assert "first content" not in row["content"]


def test_a_name_taken_by_one_kind_cannot_be_reused_by_another(tmp_path):
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))

    memory.save("deploy", "knowledge", "How deploys work", "knowledge body")
    memory.save("deploy", "project", "Current deploy state", "project body")

    named = [row for row in _rows(engine) if row["name"] == "deploy"]
    assert len(named) == 1
    assert named[0]["kind"] == "project"


def test_append_concatenates_through_the_transaction(tmp_path):
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))

    memory.save("t", "knowledge", "d", "# Heading\n\nalpha")
    memory.save("t", "knowledge", "d", "beta\n", mode="append")

    row = next(r for r in _rows(engine) if r["name"] == "t")
    assert row["content"] == "# Heading\n\nalpha\nbeta\n"


def test_append_to_an_absent_memory_creates_it(tmp_path):
    memory = _memory_on_tmp_db(tmp_path)
    memory.save("fresh", "project", "d", "body\n", mode="append")

    assert "body" in memory.read("fresh")[0]


def test_mark_recalled_and_list_order(tmp_path, monkeypatch):
    import ouroboros.storage.core_memory.memory.domain.service as service_mod

    class _Clock:
        def __init__(self) -> None:
            self._t = 1_700_000_000.0

        def time(self) -> float:
            self._t += 1.0
            return self._t

    monkeypatch.setattr(service_mod, "time", _Clock())

    memory = _memory_on_tmp_db(tmp_path)
    memory.save("oldest", "project", "d", "c")
    memory.save("newest", "project", "d", "c")

    assert [h.name for h in memory.list_headers() if h.kind == "project"] == ["newest", "oldest"]

    memory.mark_recalled(["oldest"])
    stats = {h.name: (h.recall_count, h.last_recalled_at) for h in memory.list_headers()}
    assert stats["oldest"][0] == 1
    assert stats["oldest"][1] > 0
    assert stats["newest"] == (0, 0.0)


def test_owners_do_not_see_each_others_memories(tmp_path):
    repo = SqliteMemory(_upgraded_engine(tmp_path))
    Memory(repo, owner_id="global").save("shared", "project", "d", "global body\n")
    other = Memory(repo, owner_id="other")

    assert other.fetch("shared") is None
    assert other.list_headers() == []


# ------------------------------------------------------- body-bearing reads


def test_every_body_bearing_read_is_keyed_by_name():
    """D12's structural guard: no listing, index, or capacity check can pull a body.

    One statement reads ``content`` — a single name — and the revise lock-select
    is that same statement plus a dialect lock clause. The guard is the shape of
    the key, not the count: anything selecting a body must say which name it
    wants, so no listing can grow into one.
    """
    import ouroboros.storage.core_memory.memory.infra._sql_base as base
    from ouroboros.storage.core_memory.memory.infra.psql import PsqlMemory

    statements = [
        value if isinstance(value, str) else str(getattr(value, "text", ""))
        for name, value in vars(base).items()
        if name.endswith("_SQL")
    ]
    statements += [PsqlMemory.lock_select_sql, SqliteMemory.lock_select_sql]

    keyed_by_name = f"SELECT {base._COLUMNS} FROM {base._TABLE} WHERE owner_id=:owner_id AND name"
    selects_with_content = [sql for sql in statements if sql.strip().upper().startswith("SELECT") and "content" in sql]
    assert len(selects_with_content) >= 1, "expected the single-name fetch statement"
    for sql in selects_with_content:
        assert sql.startswith(keyed_by_name), f"a body-bearing read that is not keyed by name: {sql!r}"


def test_list_headers_carries_no_body(tmp_path):
    memory = _memory_on_tmp_db(tmp_path)
    memory.save("a", "knowledge", "d", "a very long body the index must never load\n")

    headers = memory.list_headers()

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


def test_the_prompt_listing_excludes_archived_rows_in_the_query(tmp_path):
    """The status predicate is the store's, not a filter every caller repeats."""
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))
    memory.save("kept", "project", "d", "c")
    memory.save("gone", "project", "d", "c")
    memory._repo.set_status("global", ["gone"], "archived")
    statements = _memory_sql(engine)

    prompt_names = {h.name for h in memory.list_prompt_headers()}

    # The migration seeds identity, so the listing is the two saves plus it.
    assert prompt_names == {"kept", "identity"}
    assert "status IN" in " ".join(statements[0][0].split())
    # The audit listing still sees everything.
    assert {h.name for h in memory.list_headers()} == {"kept", "gone", "identity"}


def test_the_prompt_listing_runs_on_the_index_without_a_sort(tmp_path):
    """Regression: the shipped index named columns no predicate uses, so this was a scan + filesort."""
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))
    memory.save("a", "project", "d", "c")
    statements = _memory_sql(engine)

    memory.list_prompt_headers()

    statement, parameters = statements[-1]
    with engine.connect() as conn:
        plan = " | ".join(str(row[-1]) for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters))

    assert "idx_memory_owner_status_updated" in plan, plan
    assert "SCAN memory" not in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan


def test_the_table_refuses_a_status_outside_the_closed_pair(tmp_path):
    """``superseded`` was the legacy value; the migration maps it in, the CHECK keeps it out.

    Without it the fail-open normalizer would call an unknown status active
    while the prompt listing's SQL called it absent.
    """
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))
    memory.save("a", "project", "d", "c")

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


def test_concurrent_appends_to_one_memory_both_land(tmp_path):
    """The race the transaction closes: without it one fragment is lost."""
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))
    memory.save("t", "project", "d", "base")

    _run_concurrently(
        [
            lambda: memory.save("t", "project", "d", "from-first\n", mode="append"),
            lambda: memory.save("t", "project", "d", "from-second\n", mode="append"),
        ]
    )

    content = next(r for r in _rows(engine) if r["name"] == "t")["content"]
    assert "from-first" in content
    assert "from-second" in content
    assert content.startswith("base\n")


def test_concurrent_appends_that_both_create_the_memory_lose_nothing(tmp_path):
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))

    _run_concurrently(
        [
            lambda: memory.save("fresh", "project", "d", "from-first\n", mode="append"),
            lambda: memory.save("fresh", "project", "d", "from-second\n", mode="append"),
        ]
    )

    content = next(r for r in _rows(engine) if r["name"] == "fresh")["content"]
    assert "from-first" in content
    assert "from-second" in content


# ------------------------------------------ the materializing revise (psql shape)


class _MaterializingMemory(SqliteMemory):
    """The PostgreSQL revise shape — insert a row to lock, then lock it — on SQLite.

    PostgreSQL is not available in the suite, and the branch under test lives in
    the base class both adapters share. The statement is the psql adapter's own,
    verbatim: its column list and named parameters are dialect-neutral, and
    SQLite has supported ``ON CONFLICT ... DO NOTHING RETURNING`` since 3.35.
    """

    ensure_row_sql = _PSQL_ENSURE_ROW_SQL


def _template(name: str) -> MemoryRow:
    return MemoryRow(owner_id="global", name=name, kind="project", description="d", content="", updated_at=1.0)


def _materializing(tmp_path: pathlib.Path) -> _MaterializingMemory:
    return _MaterializingMemory(_upgraded_engine(tmp_path))


def test_revise_reports_an_absent_name_as_absent_even_when_a_row_is_materialized(tmp_path):
    """Half one of the contract: the callback maps the *existing* row, None when absent."""
    repo = _materializing(tmp_path)
    seen: list[MemoryRow | None] = []

    repo.revise(
        "global", "brand_new", lambda current: seen.append(current) or _template("brand_new"), _template("brand_new")
    )

    assert seen == [None]


def test_revise_returning_none_commits_without_leaving_a_materialized_row(tmp_path):
    """Half two: 'commit without writing' has to mean nothing was written.

    The row the adapter inserts to have something to lock is its own business —
    a caller that reads and declines must not find an empty entry in the index.
    """
    repo = _materializing(tmp_path)

    repo.revise("global", "declined", lambda _current: None, _template("declined"))

    assert repo.fetch("global", "declined") is None
    assert "declined" not in {header.name for header in repo.list_headers("global")}


def test_revise_still_sees_and_keeps_an_existing_row(tmp_path):
    repo = _materializing(tmp_path)
    memory = Memory(repo)
    memory.save("present", "project", "d", "body\n")
    seen: list[MemoryRow | None] = []

    repo.revise("global", "present", lambda current: seen.append(current) or None, _template("present"))

    assert seen and seen[0] is not None
    assert "body" in str(seen[0].content)
    assert repo.fetch("global", "present") is not None


def test_ensure_row_runs_before_the_locking_select(tmp_path):
    """The plumbing the PostgreSQL adapter needs to lock a not-yet-existing memory.

    ``FOR UPDATE`` locks rows, so two appends creating the same name would both
    read nothing and one fragment would be lost. The dialect SQL itself needs a
    real PostgreSQL to exercise; this pins the base-class hook it hangs on, and
    that the materialized row is what the caller would have created — so both
    dialects hand ``revise`` something that appends to the same result.
    """
    from ouroboros.storage.core_memory.memory.infra.psql import _ENSURE_ROW_SQL

    class EnsureRowMemory(SqliteMemory):
        ensure_row_sql = _ENSURE_ROW_SQL

    engine = _upgraded_engine(tmp_path)
    memory = Memory(EnsureRowMemory(engine))

    memory.save("fresh", "project", "d", "body\n", mode="append")
    memory.save("fresh", "project", "d", "more\n", mode="append")

    rows = [r for r in _rows(engine) if r["name"] == "fresh"]
    assert len(rows) == 1
    assert rows[0]["content"] == "body\nmore\n"


def test_sqlite_needs_no_ensure_row_because_begin_immediate_serializes():
    from ouroboros.storage.core_memory.memory.infra.psql import PsqlMemory

    assert SqliteMemory.ensure_row_sql == ""
    assert SqliteMemory.begin_sql == "BEGIN IMMEDIATE"
    assert "ON CONFLICT" in PsqlMemory.ensure_row_sql
    assert "DO NOTHING" in PsqlMemory.ensure_row_sql
    assert "FOR UPDATE" in PsqlMemory.lock_select_sql


def test_kind_lock_serializes_write_and_prune_on_postgres_only():
    """SQLite's whole-DB write lock already covers (owner, kind); PostgreSQL needs
    an explicit advisory lock because per-name FOR UPDATE does not serialize the
    prune cohort across different names of one kind."""
    from ouroboros.storage.core_memory.memory.infra.psql import PsqlMemory

    assert SqliteMemory.kind_lock_sql == ""
    assert "pg_advisory_xact_lock" in PsqlMemory.kind_lock_sql
    assert "hashtext(:owner_id)" in PsqlMemory.kind_lock_sql
    assert "hashtext(:kind)" in PsqlMemory.kind_lock_sql


def test_concurrent_writers_do_not_lose_memories(tmp_path):
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))

    _run_concurrently([(lambda i=i: memory.save(f"fact_{i}", "project", f"d {i}", f"c {i}")) for i in range(12)])

    names = {h.name for h in memory.list_headers()}
    assert {f"fact_{i}" for i in range(12)} <= names


def test_concurrent_same_kind_saves_do_not_exceed_capacity(tmp_path):
    """The prune rides the write transaction, so a race cannot leave a kind over
    capacity — not even transiently by the time the last save returns."""
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))
    capacity = policy_for("user").capacity

    _run_concurrently([(lambda i=i: memory.save(f"user_{i}", "user", f"d {i}", f"c {i}")) for i in range(capacity + 8)])

    active = [h for h in memory.list_headers() if h.kind == "user" and h.status == "active"]
    assert len(active) == capacity
    # Every save committed a row; the overflow is archived, not dropped.
    stored = [r for r in _rows(engine) if r["kind"] == "user"]
    assert len(stored) == capacity + 8


def test_write_and_prune_rolls_back_the_write_when_the_prune_raises(tmp_path):
    """Failure atomicity: the write and its prune commit together or not at all."""
    engine = _upgraded_engine(tmp_path)
    repo = SqliteMemory(engine)
    template = MemoryRow(owner_id="global", name="boom", kind="project")

    def _write(_current):
        return MemoryRow(
            owner_id="global", name="boom", kind="project", description="d", content="c\n", status="active"
        )

    def _prune(_headers):
        raise RuntimeError("prune blew up")

    with pytest.raises(RuntimeError, match="prune blew up"):
        repo.write_and_prune("global", "project", "boom", _write, template, _prune, statuses=frozenset({"active"}))

    assert repo.fetch("global", "boom") is None


def test_a_concurrent_overwrite_is_not_evicted_by_a_racing_prune(tmp_path):
    """A row one save refreshes (bumping updated_at to newest) cannot be archived by
    another same-kind save's prune, because the two saves now serialize."""
    engine = _upgraded_engine(tmp_path)
    memory = Memory(SqliteMemory(engine))
    capacity = policy_for("user").capacity
    for i in range(capacity):
        memory.save(f"user_{i}", "user", f"d {i}", f"c {i}")

    # user_0 is the oldest — the eviction candidate. Concurrently refresh it while
    # a new row pushes the kind over capacity.
    _run_concurrently(
        [
            lambda: memory.save("user_0", "user", "d 0", "refreshed\n"),
            lambda: memory.save(f"user_{capacity}", "user", "d new", "c new"),
        ]
    )

    by_name = {r["name"]: r for r in _rows(engine) if r["kind"] == "user"}
    assert by_name["user_0"]["status"] == "active"
    assert "refreshed" in by_name["user_0"]["content"]
    active = [r for r in by_name.values() if r["status"] == "active"]
    assert len(active) == capacity


def test_concurrent_identity_seeding_converges(tmp_path):
    engine = _upgraded_engine(tmp_path)
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM memory WHERE name='identity'"))
        conn.commit()
    memory = Memory(SqliteMemory(engine))
    seen: list[str] = []

    _run_concurrently([lambda: seen.append(memory.load_identity()) for _ in range(4)])

    assert seen == [DEFAULT_IDENTITY] * 4
    assert len([r for r in _rows(engine) if r["name"] == "identity"]) == 1


# ------------------------------------------------------------------ factory


def test_seeding_identity_touches_no_blob_storage(tmp_path):
    """Identity is a row now: no file appears under {DATA_DIR}/memory/ on any backend."""
    from ouroboros.storage.settings.paths import load_path_settings

    engine = _upgraded_engine(tmp_path)
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM memory WHERE name='identity'"))
        conn.commit()

    assert Memory(SqliteMemory(engine)).load_identity() == DEFAULT_IDENTITY
    assert not (load_path_settings().data_dir / "memory" / "identity.md").exists()


def test_get_memory_caches_one_service_per_process():
    first = get_memory()
    assert get_memory() is first

    first.save("user_role", "user", "d", "c")
    assert "user_role" in {h.name for h in get_memory().list_headers()}


def test_cache_clear_rebinds_to_the_current_drive_root(tmp_path, monkeypatch):
    """cache_clear() is the rebinding hook — how tests stay isolated."""
    get_memory().save("user_role", "user", "d", "c")

    other_root = tmp_path / "other_data"
    other_root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(other_root))
    refresh_test_config()

    # Still the old service until the cache is dropped.
    assert "user_role" in {h.name for h in get_memory().list_headers()}

    get_memory.cache_clear()

    assert "user_role" not in {h.name for h in get_memory().list_headers()}
    assert (other_root / "state" / "runtime.sqlite3").is_file()


def test_factory_builds_against_current_settings(tmp_path, monkeypatch):
    """The factory itself holds nothing: each build follows the current settings."""
    factory = MemoryFactory()
    factory.get_memory().save("user_role", "user", "d", "c")

    other_root = tmp_path / "other_data"
    other_root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(other_root))
    refresh_test_config()

    assert "user_role" not in {h.name for h in factory.get_memory().list_headers()}


def test_no_sibling_factories_remain():
    import ouroboros.storage.core_memory as core_memory

    assert not hasattr(core_memory, "get_identity")
    assert not hasattr(core_memory, "get_knowledge")
    assert not hasattr(core_memory, "get_long_term_memory")


# ---------------------------------------------------------------- migration


def _write_memory_blobs(drive_root: pathlib.Path, entries: list[dict], bodies: dict[str, str]) -> None:
    global_dir = drive_root / "memory" / "global"
    global_dir.mkdir(parents=True, exist_ok=True)
    (global_dir / "_index.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    for name, body in bodies.items():
        (global_dir / f"{name}.md").write_text(body, encoding="utf-8")


def _write_topic_files(drive_root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    kdir = drive_root / "memory" / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (kdir / name).write_text(body, encoding="utf-8")
    return kdir


def test_the_schema_holds_no_separate_memory_stores(drive_root, tmp_path):
    engine = _upgraded_engine(tmp_path)
    with engine.connect() as conn:
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}

    assert "memory" in tables
    assert "memory_topics" not in tables
    assert "knowledge_topics" not in tables
    # The scratchpad stays its own capability and its own table.
    assert "scratchpad_blocks" in tables


def test_long_term_memories_keep_their_type_as_their_kind(drive_root, tmp_path):
    _write_memory_blobs(
        drive_root,
        entries=[
            {
                "name": "user_role",
                "type": "user",
                "description": "Who the user is",
                "updated_at": 1_700_000_100.0,
                "first_recorded_at": 1_700_000_000.0,
                "status": "active",
                "source": "distilled",
                "recall_count": 3,
                "last_recalled_at": 1_700_000_050.0,
            },
            {"name": "old_fact", "type": "project", "description": "legacy", "updated_at": 1.0, "status": "superseded"},
            {"name": "dashboards", "type": "reference", "description": "Grafana", "updated_at": 2.0},
        ],
        bodies={
            "user_role": "---\nname: user_role\ntype: user\n---\n\nSenior engineer.\n",
            "old_fact": "---\nname: old_fact\n---\n\nLegacy body.\n",
            "dashboards": "Grafana link.\n",
        },
    )

    rows = {row["name"]: row for row in _rows(_upgraded_engine(tmp_path))}

    assert rows["user_role"]["kind"] == "user"
    assert rows["dashboards"]["kind"] == "reference"
    assert rows["user_role"]["content"].strip() == "Senior engineer."
    assert "---" not in rows["user_role"]["content"]
    assert rows["user_role"]["source"] == "distilled"
    assert rows["user_role"]["first_recorded_at"] == 1_700_000_000.0
    assert rows["user_role"]["recall_count"] == 3
    assert rows["old_fact"]["status"] == "archived"


def test_topics_are_copied_as_knowledge_with_derived_descriptions(drive_root, tmp_path):
    _write_topic_files(
        drive_root,
        {
            "patterns.md": "# Patterns\n\nREVIEW_BLOCKED means the plan changed\n",
            "git-recipes.md": "Rebase before push\n",
        },
    )

    rows = {row["name"]: row for row in _rows(_upgraded_engine(tmp_path))}

    assert rows["git-recipes"]["kind"] == "knowledge"
    assert rows["git-recipes"]["content"] == "Rebase before push\n"
    assert rows["patterns"]["description"] == "REVIEW_BLOCKED means the plan changed"
    # ``patterns`` is copied as an ordinary knowledge row — no name is privileged.
    assert rows["patterns"]["kind"] == "knowledge"


def test_migrated_topics_reach_the_knowledge_index(drive_root, tmp_path):
    _write_topic_files(
        drive_root,
        {"patterns.md": "REVIEW_BLOCKED means the plan changed\n", "git-recipes.md": "Rebase before push\n"},
    )
    memory = Memory(SqliteMemory(_upgraded_engine(tmp_path)))

    index, _ = memory.render_index(KNOWLEDGE_SECTION)

    # ``patterns`` is ordinary knowledge now: it gets an index line like any other.
    assert "n=git-recipes" in index
    assert "n=patterns" in index
    assert "REVIEW_BLOCKED" in memory.read("patterns")[0]


def test_migration_skips_the_derived_index_and_private_drafts(drive_root, tmp_path):
    _write_topic_files(
        drive_root,
        {
            "patterns.md": "real topic\n",
            "index-full.md": "# Knowledge Base Index\n",
            "_draft.md": "not a topic\n",
        },
    )

    names = {row["name"] for row in _rows(_upgraded_engine(tmp_path))}
    assert names == {"identity", "patterns"}


def test_migration_skips_a_name_no_tool_could_reach(drive_root, tmp_path):
    _write_topic_files(drive_root, {"patterns.md": "real\n", "not a topic.md": "hand-placed\n", "con.md": "reserved\n"})

    names = {row["name"] for row in _rows(_upgraded_engine(tmp_path))}
    assert names == {"identity", "patterns"}


def test_identity_becomes_a_row(drive_root, tmp_path):
    (drive_root / "memory").mkdir(parents=True, exist_ok=True)
    (drive_root / "memory" / "identity.md").write_text("# Who I Am\n\nOperator wrote this.\n", encoding="utf-8")

    row = next(r for r in _rows(_upgraded_engine(tmp_path)) if r["name"] == "identity")

    assert row["kind"] == "identity"
    assert row["content"] == "# Who I Am\n\nOperator wrote this.\n"


def test_absent_identity_seeds_the_default(drive_root, tmp_path):
    row = next(r for r in _rows(_upgraded_engine(tmp_path)) if r["name"] == "identity")
    assert row["content"] == DEFAULT_IDENTITY


def test_a_leftover_identity_blob_is_inert_after_the_migration(drive_root, tmp_path):
    identity_path = drive_root / "memory" / "identity.md"
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text("from the blob\n", encoding="utf-8")
    memory = Memory(SqliteMemory(_upgraded_engine(tmp_path)))

    identity_path.write_text("edited after the migration\n", encoding="utf-8")

    assert memory.load_identity() == "from the blob\n"
    assert identity_path.is_file()


def test_a_name_collision_aborts_before_anything_is_written(drive_root, tmp_path):
    _write_memory_blobs(
        drive_root,
        entries=[{"name": "deploy", "type": "project", "description": "d", "updated_at": 1.0}],
        bodies={"deploy": "fact body\n"},
    )
    _write_topic_files(drive_root, {"deploy.md": "topic body\n"})

    with pytest.raises(ValueError, match="collision"):
        _upgraded_engine(tmp_path)

    # Neither table exists: the preflight runs before any DDL, so a collision
    # leaves the schema exactly as it was.
    engine = create_runtime_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    with engine.connect() as conn:
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    assert "memory" not in tables


def test_a_collision_with_the_identity_name_also_aborts(drive_root, tmp_path):
    _write_topic_files(drive_root, {"identity.md": "a topic that shadows identity\n"})

    with pytest.raises(ValueError, match="collision"):
        _upgraded_engine(tmp_path)


class _RecordingReader:
    """Blob reader over a dict, counting every fetch. Writable mid-test."""

    def __init__(self, blobs: dict[str, str]) -> None:
        self.blobs = dict(blobs)
        self.reads: list[str] = []

    def get_text(self, key: str) -> str | None:
        self.reads.append(key)
        return self.blobs.get(key)

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.blobs if key.startswith(prefix))


def _legacy_blobs(**extra: str) -> dict[str, str]:
    base = {
        "global/_index.json": json.dumps(
            {"entries": [{"name": "deploy", "type": "project", "description": "d", "updated_at": 1.0}]}
        ),
        "global/deploy.md": "fact body\n",
        "identity.md": "# Who I Am\n",
    }
    base.update(extra)
    return base


def test_a_write_after_the_snapshot_cannot_change_what_the_copy_sees():
    """Rolling deployment: the check and the copy consume one immutable snapshot.

    The preflight and the copy used to read the legacy sources separately. A
    legacy writer still serving traffic could add a conflicting name between
    them: the check passed on the first read, the copy hit the unique
    constraint on the second, and ``ON CONFLICT DO NOTHING`` discarded a memory
    while the migration reported success.

    Re-reading is now impossible by construction — ``preflight`` and
    ``backfill`` take the rows, not a reader — so what remains to pin is that
    the snapshot itself is taken once and does not track the backend
    afterwards.
    """
    migration = _migration_module()
    reader = _RecordingReader(_legacy_blobs())

    rows = migration.collect_source_rows(reader)
    assert migration.collect_name_collisions(rows) == {}
    body_reads = reader.reads.count("global/deploy.md")
    assert body_reads == 1, f"the fact body was fetched {body_reads} times while building one snapshot"

    # The old pod writes a conflicting topic after the snapshot was taken.
    reader.blobs["knowledge/deploy.md"] = "topic written by an old pod\n"

    assert migration.collect_name_collisions(rows) == {}
    assert [row["name"] for row in rows] == ["deploy", "identity"]
    assert next(row for row in rows if row["name"] == "deploy")["kind"] == "project"


def test_a_collision_inside_the_snapshot_is_still_caught():
    """Closing the window must not cost the check it protects."""
    migration = _migration_module()
    reader = _RecordingReader(_legacy_blobs(**{"knowledge/deploy.md": "topic body\n"}))

    collisions = migration.collect_name_collisions(migration.collect_source_rows(reader))

    assert set(collisions) == {"deploy"}
    assert sorted(collisions["deploy"]) == ["memory/global", "memory/knowledge"]


def test_the_copy_does_not_suppress_unique_conflicts():
    """A conflict here means the snapshot disagrees with the table — never silence.

    The preflight makes one unreachable in a correct run, which is exactly why
    ``ON CONFLICT DO NOTHING`` bought nothing and hid the failure that matters:
    a dropped memory under a migration reporting success.
    """
    assert "ON CONFLICT" not in _migration_module()._MEMORY_INSERT_SQL.upper()


def test_a_manifest_entry_without_a_body_no_longer_collides(drive_root, tmp_path):
    """Claims are counted over rows, so a name that produces no row cannot collide.

    The manifest outlived the blob, so the fact contributes nothing; aborting on
    it blocked a migration whose copy would have held exactly one ``deploy``.
    """
    _write_memory_blobs(
        drive_root,
        entries=[{"name": "deploy", "type": "project", "description": "d", "updated_at": 1.0}],
        bodies={},
    )
    _write_topic_files(drive_root, {"deploy.md": "topic body\n"})

    rows = {row["name"]: row for row in _rows(_upgraded_engine(tmp_path))}

    assert rows["deploy"]["kind"] == "knowledge"
    assert rows["deploy"]["content"].strip() == "topic body"


def test_no_silent_rename_occurs_on_a_collision(drive_root, tmp_path):
    _write_memory_blobs(
        drive_root,
        entries=[{"name": "deploy", "type": "project", "description": "d", "updated_at": 1.0}],
        bodies={"deploy": "fact body\n"},
    )
    _write_topic_files(drive_root, {"deploy.md": "topic body\n"})

    with pytest.raises(ValueError):
        _upgraded_engine(tmp_path)

    engine = create_runtime_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    with engine.connect() as conn:
        has_memory = bool(
            conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory'")).fetchone()
        )
    assert not has_memory


def test_distinct_names_across_all_three_sources_complete_normally(drive_root, tmp_path):
    _write_memory_blobs(
        drive_root,
        entries=[{"name": "user_role", "type": "user", "description": "d", "updated_at": 1.0}],
        bodies={"user_role": "fact body\n"},
    )
    _write_topic_files(drive_root, {"git-recipes.md": "topic body\n"})

    rows = {row["name"]: row["kind"] for row in _rows(_upgraded_engine(tmp_path))}

    assert rows == {"user_role": "user", "git-recipes": "knowledge", "identity": "identity"}


def test_migration_backfill_is_idempotent(drive_root, tmp_path):
    _write_memory_blobs(
        drive_root,
        entries=[{"name": "user_role", "type": "user", "description": "d", "updated_at": 1.0}],
        bodies={"user_role": "body\n"},
    )

    engine = _upgraded_engine(tmp_path)
    upgrade_head(engine)

    assert len(_rows(engine)) == 2


def test_migration_backfill_uses_frontmatter_when_the_manifest_lacks_an_entry(drive_root, tmp_path):
    global_dir = drive_root / "memory" / "global"
    global_dir.mkdir(parents=True, exist_ok=True)
    (global_dir / "orphan.md").write_text(
        "---\nname: orphan\ntype: reference\ndescription: dashboards\n"
        "recorded_at: 2024-06-24T05:03:00+00:00\nupdated_at: 2024-06-24T06:03:00+00:00\n---\n\nGrafana link.\n",
        encoding="utf-8",
    )

    row = next(r for r in _rows(_upgraded_engine(tmp_path)) if r["name"] == "orphan")

    assert row["kind"] == "reference"
    assert row["description"] == "dashboards"
    assert row["content"].strip() == "Grafana link."
    assert row["first_recorded_at"] == pytest.approx(1_719_205_380.0)
    assert row["updated_at"] == pytest.approx(1_719_208_980.0)


def test_migration_fails_when_a_memory_cannot_be_read(drive_root, tmp_path, monkeypatch):
    """The copy is mandatory: an unreadable memory aborts the migration, it is not skipped."""
    _write_memory_blobs(
        drive_root,
        entries=[
            {"name": "good_fact", "type": "project", "description": "d", "updated_at": 2.0},
            {"name": "bad_fact", "type": "project", "description": "d", "updated_at": 1.0},
        ],
        bodies={"good_fact": "body\n", "bad_fact": "body\n"},
    )
    fail_reads_of(monkeypatch, "bad_fact.md", OSError("boom"))

    with pytest.raises(OSError, match="boom"):
        _upgraded_engine(tmp_path)


def test_migration_aborts_when_a_topic_exists_but_cannot_be_read(drive_root, tmp_path, monkeypatch):
    _write_topic_files(drive_root, {"patterns.md": "real topic\n"})
    fail_reads_of(monkeypatch, "patterns.md", PermissionError("storage backend unreachable"))

    with pytest.raises(PermissionError, match="storage backend unreachable"):
        _upgraded_engine(tmp_path)


def test_migration_aborts_when_the_manifest_exists_but_cannot_be_parsed(drive_root, tmp_path):
    """Rebuilding from frontmatter alone would silently drop status/source/recall counters."""
    _write_memory_blobs(
        drive_root,
        entries=[{"name": "user_role", "type": "user", "description": "d", "updated_at": 1.0}],
        bodies={"user_role": "body\n"},
    )
    (drive_root / "memory" / "global" / "_index.json").write_text("NOT VALID JSON {{{", encoding="utf-8")

    with pytest.raises(Exception, match="(?i)json|expecting"):
        _upgraded_engine(tmp_path)


@pytest.mark.parametrize("payload", ["", "   \n\t  "])
def test_migration_aborts_when_the_manifest_exists_but_is_empty(drive_root, tmp_path, payload):
    """A blank manifest is a damaged payload, not a deployment that never had one.

    Reading it as absence rebuilds every memory from frontmatter and drops
    status, source, recall_count, and last_recalled_at — while reporting success
    and stamping the revision, so nothing ever retries.
    """
    _write_memory_blobs(
        drive_root,
        entries=[
            {
                "name": "user_role",
                "type": "user",
                "description": "d",
                "updated_at": 1.0,
                "status": "archived",
                "source": "distilled",
                "recall_count": 7,
            }
        ],
        bodies={"user_role": "body\n"},
    )
    (drive_root / "memory" / "global" / "_index.json").write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="exists but is empty"):
        _upgraded_engine(tmp_path)


def test_migration_treats_an_absent_manifest_as_absent(drive_root, tmp_path):
    """The other side of the same rule: no manifest at all is a fresh deployment."""
    _write_memory_blobs(drive_root, entries=[], bodies={"user_role": "body\n"})
    (drive_root / "memory" / "global" / "_index.json").unlink()

    rows = {row["name"]: row for row in _rows(_upgraded_engine(tmp_path))}
    assert set(rows) == {"identity", "user_role"}
    assert rows["user_role"]["status"] == "active"


def test_migration_skips_a_manifest_name_whose_body_is_gone(drive_root, tmp_path):
    """Absent is tolerated — and contributes no row, rather than an empty memory."""
    _write_memory_blobs(
        drive_root,
        entries=[
            {"name": "user_role", "type": "user", "description": "d", "updated_at": 1.0},
            {"name": "deleted_fact", "type": "user", "description": "d", "updated_at": 1.0},
        ],
        bodies={"user_role": "body\n"},
    )

    names = {row["name"] for row in _rows(_upgraded_engine(tmp_path))}
    assert names == {"identity", "user_role"}


def test_migration_leaves_the_source_files_in_place(drive_root, tmp_path):
    kdir = _write_topic_files(drive_root, {"patterns.md": "real topic\n"})

    _upgraded_engine(tmp_path)

    assert (kdir / "patterns.md").read_text(encoding="utf-8") == "real topic\n"


def test_the_backfill_lands_within_each_kinds_capacity(drive_root, tmp_path):
    """The backfill applies the capacity itself, oldest-first.

    Copying as-is left the first ``memory_save`` of that kind to archive the
    overflow, which it does silently — success in the tool reply, nothing in the
    log. Doing it here makes the choice recency-ordered and logged.
    """
    capacity = policy_for("user").capacity
    over = capacity + 3
    _write_memory_blobs(
        drive_root,
        entries=[
            {"name": f"user_{i:02d}", "type": "user", "description": "d", "updated_at": float(i)} for i in range(over)
        ],
        bodies={f"user_{i:02d}": "body\n" for i in range(over)},
    )

    rows = {row["name"]: row for row in _rows(_upgraded_engine(tmp_path))}

    active = {name for name, row in rows.items() if row["kind"] == "user" and row["status"] == "active"}
    archived = {name for name, row in rows.items() if row["kind"] == "user" and row["status"] == "archived"}
    assert len(active) == capacity
    # updated_at ascends with i, so the three oldest are the ones archived.
    assert archived == {"user_00", "user_01", "user_02"}
    # Nothing is dropped: an archived memory is still readable for audit.
    assert len(active | archived) == over


def test_the_first_save_after_the_backfill_archives_nothing_in_bulk(drive_root, tmp_path):
    capacity = policy_for("user").capacity
    _write_memory_blobs(
        drive_root,
        entries=[
            {"name": f"user_{i:02d}", "type": "user", "description": "d", "updated_at": float(i)}
            for i in range(capacity + 5)
        ],
        bodies={f"user_{i:02d}": "body\n" for i in range(capacity + 5)},
    )
    engine = _upgraded_engine(tmp_path)
    before = {row["name"] for row in _rows(engine) if row["kind"] == "user" and row["status"] == "active"}

    Memory(SqliteMemory(engine)).save("user_new", "user", "d", "c")

    after = {row["name"] for row in _rows(engine) if row["kind"] == "user" and row["status"] == "active"}
    # One in, exactly one out — not the five-at-once the backfill used to defer.
    assert after - before == {"user_new"}
    assert len(before - after) == 1


# --------------------------------------------------- backfill: name grammar


def test_the_backfill_keeps_a_legacy_name_the_new_grammar_rejects(drive_root, tmp_path):
    """The legacy store allowed a trailing ``-``; the new grammar does not.

    Dropping such a memory is permanent — the blobs are frozen after the
    cutover — so the name is trimmed into the new grammar instead.
    """
    _write_memory_blobs(
        drive_root,
        entries=[{"name": "roadmap-2026-", "type": "project", "description": "d", "updated_at": 1.0}],
        bodies={"roadmap-2026-": "the roadmap body\n"},
    )

    rows = {row["name"]: row for row in _rows(_upgraded_engine(tmp_path))}

    assert "roadmap-2026" in rows
    assert "the roadmap body" in rows["roadmap-2026"]["content"]
    assert rows["roadmap-2026"]["kind"] == "project"


def test_the_backfill_does_not_rename_a_hand_placed_knowledge_file(drive_root, tmp_path):
    """Trimming is for the fact store only.

    ``_sanitize_topic`` enforced the new grammar before the cutover as well, so
    a knowledge file breaking it was never agent-written and was never readable
    — surfacing it now would invent a topic under a name nobody chose.
    """
    _write_topic_files(drive_root, {"patterns.md": "real\n", "deploy notes.md": "hand-placed\n"})

    names = {row["name"] for row in _rows(_upgraded_engine(tmp_path))}

    assert names == {"identity", "patterns"}


def test_the_backfill_still_drops_a_name_nothing_addressable_survives(drive_root, tmp_path):
    _write_memory_blobs(
        drive_root,
        entries=[
            {"name": "---", "type": "user", "description": "d", "updated_at": 1.0},
            {"name": "user_role", "type": "user", "description": "d", "updated_at": 1.0},
        ],
        bodies={"---": "body\n", "user_role": "body\n"},
    )

    names = {row["name"] for row in _rows(_upgraded_engine(tmp_path))}

    assert names == {"identity", "user_role"}


def test_a_name_that_only_collides_once_trimmed_aborts_the_backfill(drive_root, tmp_path):
    """Trimming must not silently merge two memories into one row."""
    _write_memory_blobs(
        drive_root,
        entries=[
            {"name": "plan", "type": "project", "description": "d", "updated_at": 1.0},
            {"name": "plan-", "type": "project", "description": "d", "updated_at": 2.0},
        ],
        bodies={"plan": "first\n", "plan-": "second\n"},
    )

    with pytest.raises(Exception, match="(?i)collision"):
        _upgraded_engine(tmp_path)
