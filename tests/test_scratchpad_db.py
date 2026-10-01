"""Scratchpad over a real (tmp) SQLite runtime DB: adapter + migration backfill."""

from __future__ import annotations

import json
import pathlib
import threading

import pytest
from sqlalchemy import text

from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.core_memory.scratchpad.domain.service import Scratchpad
from ouroboros.storage.core_memory.scratchpad.infra.sql import PsqlScratchpad, SqliteScratchpad, SqlScratchpad
from ouroboros.storage.migrations.runner import upgrade_head
from ouroboros.storage.runtime_factory import sqlite_url_for_path
from ouroboros.storage.sql_runtime_store import create_runtime_engine


@pytest.fixture(autouse=True)
def _reset_core_memory_services() -> None:
    """Drop the cached service so this test binds to its own runtime DB."""
    get_scratchpad.cache_clear()


def _upgraded_engine(tmp_path: pathlib.Path):
    # ``create_runtime_engine`` (not a bare ``create_engine``) so the SQLite
    # connection carries the runtime busy timeout and is thread-safe — what the
    # concurrent-append test needs to serialize rather than raise "database is
    # locked".
    engine = create_runtime_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    upgrade_head(engine)
    return engine


def _scratchpad_on_tmp_db(tmp_path: pathlib.Path) -> Scratchpad:
    return Scratchpad(SqlScratchpad(_upgraded_engine(tmp_path)))


def _rows(engine) -> list[dict]:
    with engine.connect() as conn:
        query = "SELECT id, owner_id, ts, source, content, status FROM scratchpad_blocks ORDER BY id"
        return [dict(r) for r in conn.execute(text(query)).mappings()]


# ---------------------------------------------------------------- adapter


def test_append_takes_an_increasing_database_assigned_id(tmp_path):
    scratchpad = _scratchpad_on_tmp_db(tmp_path)

    for i in range(3):
        scratchpad.append_block(f"block {i}")

    rows = scratchpad.list_blocks()
    ids = [r.id for r in rows]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3
    assert [r.content for r in rows] == ["block 0", "block 1", "block 2"]


def test_eviction_leaves_exactly_the_cap_active_and_keeps_evicted_content(tmp_path):
    engine = _upgraded_engine(tmp_path)
    scratchpad = Scratchpad(SqlScratchpad(engine))

    for i in range(SCRATCHPAD_MAX_BLOCKS + 3):
        scratchpad.append_block(f"block {i}")

    active = scratchpad.list_blocks()
    assert len(active) == SCRATCHPAD_MAX_BLOCKS
    assert active[0].content == "block 3"

    evicted = [r for r in _rows(engine) if r["status"] == "evicted"]
    assert [r["content"] for r in evicted] == ["block 0", "block 1", "block 2"]
    assert all(r["ts"] for r in evicted)


def test_a_failed_eviction_rolls_the_append_back_with_it(tmp_path, monkeypatch):
    """The append and the eviction it triggers are one transaction.

    Committed separately, a failure of the second half left the buffer at
    ``cap + 1`` active blocks permanently — the next append evicts down to the
    cap and the extra one is simply never noticed.
    """
    from ouroboros.storage.core_memory.scratchpad.infra import sql as sql_module

    engine = _upgraded_engine(tmp_path)
    scratchpad = Scratchpad(SqlScratchpad(engine))
    for i in range(SCRATCHPAD_MAX_BLOCKS):
        scratchpad.append_block(f"block {i}")

    monkeypatch.setattr(sql_module, "_EVICT_OVER_CAP_SQL", "UPDATE no_such_table SET status='evicted'")
    with pytest.raises(Exception, match="(?i)no_such_table"):
        scratchpad.append_block("one too many")

    assert [r["content"] for r in _rows(engine)] == [f"block {i}" for i in range(SCRATCHPAD_MAX_BLOCKS)]
    assert len(scratchpad.list_blocks()) == SCRATCHPAD_MAX_BLOCKS


def test_the_buffer_is_never_observable_over_its_cap(tmp_path):
    """A reader between two separate commits used to see ``cap + 1`` blocks.

    The adapter now commits once, so the over-cap state has no window to be read
    in — checked here on the value the eviction reports back for the block that
    tips the buffer over.
    """
    engine = _upgraded_engine(tmp_path)
    repo = SqlScratchpad(engine)
    for i in range(SCRATCHPAD_MAX_BLOCKS):
        assert (
            repo.append_and_evict("global", "2026-07-01T10:00:00+00:00", "task", f"block {i}", SCRATCHPAD_MAX_BLOCKS)
            == 0
        )

    evicted = repo.append_and_evict(
        "global", "2026-07-01T10:00:00+00:00", "task", "tips it over", SCRATCHPAD_MAX_BLOCKS
    )

    assert evicted == 1
    assert len(repo.list_active("global")) == SCRATCHPAD_MAX_BLOCKS


# ---------------------------------------------------------------- concurrency


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
    # A thread still alive means the append lock deadlocked rather than
    # serialized — without this the test would pass on whatever the survivors
    # happened to write.
    assert not [t for t in threads if t.is_alive()], "a worker did not finish"
    assert not errors, errors


def test_concurrent_same_owner_appends_do_not_exceed_the_cap(tmp_path):
    """More concurrent appends than the cap, against a full buffer, still settle
    at exactly the cap — and every append is persisted, none lost.

    Without serialization two concurrent appends each evict the same single
    oldest block against a snapshot missing the other's insert, so the buffer
    ends up over the cap. On SQLite the insert (the append's first write) takes
    the whole-database write lock, so the second appender waits and then evicts
    against a snapshot that already includes the first insert; the PostgreSQL
    path buys the same isolation with ``owner_lock_sql``. Proven here on the
    dialect whose write lock enforces it."""
    engine = _upgraded_engine(tmp_path)
    repo = SqlScratchpad(engine)
    for i in range(SCRATCHPAD_MAX_BLOCKS):
        repo.append_and_evict("global", "2026-07-01T10:00:00+00:00", "task", f"seed {i}", SCRATCHPAD_MAX_BLOCKS)

    extra = SCRATCHPAD_MAX_BLOCKS + 8
    _run_concurrently(
        [
            (
                lambda i=i: repo.append_and_evict(
                    "global", "2026-07-01T10:00:00+00:00", "task", f"append {i}", SCRATCHPAD_MAX_BLOCKS
                )
            )
            for i in range(extra)
        ]
    )

    assert len(repo.list_active("global")) == SCRATCHPAD_MAX_BLOCKS
    # Every seed and every concurrent append committed a row; the overflow is
    # evicted, not dropped.
    rows = _rows(engine)
    assert len(rows) == SCRATCHPAD_MAX_BLOCKS + extra
    assert {r["status"] for r in rows} <= {"active", "evicted"}
    assert len([r for r in rows if r["status"] == "active"]) == SCRATCHPAD_MAX_BLOCKS


def test_owner_lock_sql_is_set_on_postgres_only():
    """SQLite's whole-DB write lock already serializes same-owner appends (the
    insert is the append's first write); PostgreSQL needs an explicit advisory
    lock, keyed on ``owner_id`` alone so scratchpad locks never collide with
    memory's two-arg locks. Mirrors the memory package's ``kind_lock_sql``
    shape assertion."""
    assert SqliteScratchpad.owner_lock_sql == ""
    assert "pg_advisory_xact_lock" in PsqlScratchpad.owner_lock_sql
    assert "hashtext(:owner_id)" in PsqlScratchpad.owner_lock_sql


def test_owners_do_not_see_each_others_blocks(tmp_path):
    engine = _upgraded_engine(tmp_path)
    Scratchpad(SqlScratchpad(engine), owner_id="global").append_block("global note")
    other = Scratchpad(SqlScratchpad(engine), owner_id="other")

    other.append_block("other note")

    assert [r.content for r in other.list_blocks()] == ["other note"]


# ---------------------------------------------------------------- factory


def test_cache_clear_rebinds_to_the_current_drive_root(drive_root, tmp_path, monkeypatch):
    get_scratchpad().append_block("first root")

    other_root = tmp_path / "other_data"
    other_root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(other_root))
    get_scratchpad.cache_clear()

    assert get_scratchpad().list_blocks() == []


# ---------------------------------------------------------------- migration


@pytest.fixture
def drive_root(tmp_path, monkeypatch) -> pathlib.Path:
    root = tmp_path / "ouroboros_data"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "file")
    return root


def _write_scratchpad_blobs(
    drive_root: pathlib.Path,
    blocks: list[dict] | None = None,
    journal: list[dict] | None = None,
) -> None:
    memory_dir = drive_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    if blocks is not None:
        (memory_dir / "scratchpad_blocks.json").write_text(json.dumps(blocks), encoding="utf-8")
    if journal is not None:
        (memory_dir / "scratchpad_journal.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in journal), encoding="utf-8"
        )


def test_migration_copies_blocks_in_array_order(drive_root, tmp_path):
    _write_scratchpad_blobs(
        drive_root,
        blocks=[
            {"ts": "2026-07-01T10:00:00+00:00", "source": "task", "content": "first"},
            {"ts": "2026-07-02T10:00:00+00:00", "source": "consolidation", "content": "second"},
        ],
    )

    rows = _rows(_upgraded_engine(tmp_path))

    assert [r["content"] for r in rows] == ["first", "second"]
    assert [r["status"] for r in rows] == ["active", "active"]
    assert rows[0]["ts"] == "2026-07-01T10:00:00+00:00"
    assert rows[1]["source"] == "consolidation"
    assert rows[0]["id"] < rows[1]["id"]
    assert all(r["owner_id"] == "global" for r in rows)


def test_migration_leaves_the_eviction_journal_unread(drive_root, tmp_path):
    """Pre-cutover eviction history is not imported: nothing ever read those rows."""
    _write_scratchpad_blobs(
        drive_root,
        blocks=[{"ts": "2026-07-01T10:00:00+00:00", "source": "task", "content": "live"}],
        journal=[
            {"ts": "2026-06-01T09:00:00+00:00", "type": "block_appended", "content_len": 42},
            {
                "ts": "2026-06-02T09:00:00+00:00",
                "type": "block_evicted",
                "evicted_block_ts": "2026-06-01T08:00:00+00:00",
                "evicted_block_source": "task",
                "evicted_block_content": "archived body",
            },
        ],
    )

    rows = _rows(_upgraded_engine(tmp_path))

    assert [r["status"] for r in rows] == ["active"]
    assert [r["content"] for r in rows] == ["live"]


def test_migration_survives_a_journal_it_could_not_have_parsed(drive_root, tmp_path):
    """Proof the journal is not opened at all: unparseable content is no obstacle."""
    _write_scratchpad_blobs(
        drive_root, blocks=[{"ts": "2026-07-01T10:00:00+00:00", "source": "task", "content": "live"}]
    )
    (drive_root / "memory" / "scratchpad_journal.jsonl").write_text("NOT VALID JSON {{{\n", encoding="utf-8")

    rows = _rows(_upgraded_engine(tmp_path))

    assert [r["content"] for r in rows] == ["live"]


def test_migration_is_a_no_op_when_no_scratchpad_files_exist(drive_root, tmp_path):
    assert _rows(_upgraded_engine(tmp_path)) == []


def test_migration_aborts_when_the_block_payload_cannot_be_parsed(drive_root, tmp_path):
    (drive_root / "memory").mkdir(parents=True, exist_ok=True)
    (drive_root / "memory" / "scratchpad_blocks.json").write_text("NOT VALID JSON {{{", encoding="utf-8")

    with pytest.raises(Exception, match="(?i)json|expecting"):
        _upgraded_engine(tmp_path)


@pytest.mark.parametrize("payload", ["", "   \n\t  "])
def test_migration_aborts_when_the_block_payload_exists_but_is_empty(drive_root, tmp_path, payload):
    """A blank blocks file is corruption, not absence.

    Reading it as absence finished the migration with an empty table and a
    stamped revision — after which the scratchpad is gone for good, because the
    tools read the table and no longer fall back to the blob.
    """
    (drive_root / "memory").mkdir(parents=True, exist_ok=True)
    (drive_root / "memory" / "scratchpad_blocks.json").write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="exists but is empty"):
        _upgraded_engine(tmp_path)


def test_migration_treats_an_absent_block_payload_as_absent(drive_root, tmp_path):
    """The other side of the same rule: an empty JSON array still migrates cleanly."""
    (drive_root / "memory").mkdir(parents=True, exist_ok=True)
    (drive_root / "memory" / "scratchpad_blocks.json").write_text("[]", encoding="utf-8")

    assert _rows(_upgraded_engine(tmp_path)) == []


def test_migration_aborts_when_the_blocks_object_exists_but_cannot_be_read(drive_root, tmp_path):
    """The copy is mandatory: booting with an empty scratchpad is not an option.

    A directory at the object's path is an ``OSError`` that is not
    ``FileNotFoundError`` — exactly the "present but unreadable" case the reader
    must not report as absence.
    """
    (drive_root / "memory" / "scratchpad_blocks.json").mkdir(parents=True)

    with pytest.raises(OSError):
        _upgraded_engine(tmp_path)


def test_migration_leaves_the_legacy_blobs_in_place(drive_root, tmp_path):
    blocks = [{"ts": "2026-07-01T10:00:00+00:00", "source": "task", "content": "live"}]
    _write_scratchpad_blobs(drive_root, blocks=blocks, journal=[])
    rendered = drive_root / "memory" / "scratchpad.md"
    rendered.write_text("stale rendered view", encoding="utf-8")

    _upgraded_engine(tmp_path)

    assert json.loads((drive_root / "memory" / "scratchpad_blocks.json").read_text(encoding="utf-8")) == blocks
    assert rendered.read_text(encoding="utf-8") == "stale rendered view"
