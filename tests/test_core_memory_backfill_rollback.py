"""What a failed 0006 run leaves behind on SQLite, and that the retry still works.

Fail-closed is only sufficient because an abort leaves no partial copy behind,
so the next start is a complete retry. That was assumed to follow from the
migration transaction alone; on SQLite it does not. SQLAlchemy's pysqlite
driver autocommits ``CREATE TABLE``, so an aborted run rolls back the inserted
rows and the version stamp but leaves the tables created so far in place —
empty. The migration therefore creates only the tables that are missing, which
is what these tests pin: no rows and no stamp survive, and the retry copies
everything.

Where the abort lands decides how much schema survives. The memory sources are
read once, up front, so a source that cannot be read aborts before any DDL; the
scratchpad copy runs last, so a failure there leaves both tables behind. Both
are covered below.

Each test upgrades to ``0005`` first and then fails inside ``0006``, so the
assertions are about that one revision rather than about an empty database.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from ouroboros.storage.migrations.runner import alembic_config, upgrade_head
from ouroboros.storage.runtime_factory import sqlite_url_for_path
from tests.conftest import fail_reads_of

CORE_MEMORY_TABLES = ("memory", "scratchpad_blocks")


@pytest.fixture
def drive_root(tmp_path, monkeypatch) -> pathlib.Path:
    root = tmp_path / "ouroboros_data"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "file")
    return root


def _write_legacy_blobs(drive_root: pathlib.Path) -> None:
    memory = drive_root / "memory"
    (memory / "global").mkdir(parents=True, exist_ok=True)
    (memory / "knowledge").mkdir(parents=True, exist_ok=True)
    (memory / "global" / "_index.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"name": "good_fact", "type": "project", "description": "d", "updated_at": 2.0},
                    {"name": "bad_fact", "type": "project", "description": "d", "updated_at": 1.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    (memory / "global" / "good_fact.md").write_text("good body\n", encoding="utf-8")
    (memory / "global" / "bad_fact.md").write_text("bad body\n", encoding="utf-8")
    (memory / "scratchpad_blocks.json").write_text(
        json.dumps([{"ts": "2026-07-01T10:00:00+00:00", "source": "task", "content": "live"}]), encoding="utf-8"
    )
    (memory / "knowledge" / "patterns.md").write_text("real topic\n", encoding="utf-8")


def _engine(tmp_path: pathlib.Path):
    return create_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))


def _upgrade_to(engine, revision: str) -> None:
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)
    cfg.attributes.pop("connection", None)


def _stamped_revisions(engine) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))]


def _snapshot(engine) -> dict[str, list[dict]]:
    with engine.connect() as conn:
        return {
            "memory": [dict(r) for r in conn.execute(text("SELECT * FROM memory ORDER BY name")).mappings()],
            "scratchpad_blocks": [
                dict(r) for r in conn.execute(text("SELECT * FROM scratchpad_blocks ORDER BY id")).mappings()
            ],
        }


#: Columns two runs cannot agree on. The knowledge and identity rows carry the
#: migration's own clock rather than anything read from the source, and ``id``
#: is database-assigned.
_RUN_SPECIFIC_COLUMNS = frozenset({"id", "first_recorded_at", "updated_at"})


def _comparable(table: str, rows: list[dict]) -> list[dict]:
    if table != "memory":
        return rows
    return [{k: v for k, v in row.items() if k not in _RUN_SPECIFIC_COLUMNS} for row in rows]


def test_an_abort_rolls_back_the_rows_and_the_version_stamp_together(drive_root, tmp_path, monkeypatch):
    _write_legacy_blobs(drive_root)
    engine = _engine(tmp_path)
    _upgrade_to(engine, "0005")
    assert _stamped_revisions(engine) == ["0005"]

    # good_fact sorts first, so its row is already inserted when bad_fact fails.
    fail_reads_of(monkeypatch, "bad_fact.md", OSError("boom"))
    with pytest.raises(OSError, match="boom"):
        _upgrade_to(engine, "head")

    assert _stamped_revisions(engine) == ["0005"]
    for table in CORE_MEMORY_TABLES:
        if inspect(engine).has_table(table):
            with engine.connect() as conn:
                assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() == 0, table


def test_the_tables_an_aborted_run_leaves_behind_are_empty_not_partial(drive_root, tmp_path, monkeypatch):
    """SQLite autocommits DDL, so the tables outlive the abort — the rows must not.

    The failure is placed in the scratchpad copy on purpose: that is the last
    step, so by then both tables exist and ``memory`` has already been filled.
    This is the state ``_table_exists`` is written for, and the one a retry has
    to find empty rather than half-copied.
    """
    _write_legacy_blobs(drive_root)
    engine = _engine(tmp_path)
    _upgrade_to(engine, "0005")

    fail_reads_of(monkeypatch, "scratchpad_blocks.json", OSError("boom"))
    with pytest.raises(OSError, match="boom"):
        _upgrade_to(engine, "head")

    assert inspect(engine).has_table("memory")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM memory")).scalar() == 0


def test_an_unreadable_memory_source_aborts_before_any_table_is_created(drive_root, tmp_path, monkeypatch):
    """Reading every memory source up front moved this abort ahead of the DDL.

    The sources are now materialized once, before ``create_table``, so a source
    that cannot be read leaves the schema exactly as the preflight promises for
    a name collision — untouched, rather than holding two empty tables.
    """
    _write_legacy_blobs(drive_root)
    engine = _engine(tmp_path)
    _upgrade_to(engine, "0005")

    fail_reads_of(monkeypatch, "bad_fact.md", OSError("boom"))
    with pytest.raises(OSError, match="boom"):
        _upgrade_to(engine, "head")

    assert _stamped_revisions(engine) == ["0005"]
    for table in CORE_MEMORY_TABLES:
        assert not inspect(engine).has_table(table), table


def test_a_restart_after_the_repair_migrates_as_completely_as_a_clean_run(drive_root, tmp_path, monkeypatch):
    _write_legacy_blobs(drive_root)
    engine = _engine(tmp_path)
    _upgrade_to(engine, "0005")

    fail_reads_of(monkeypatch, "bad_fact.md", OSError("boom"))
    with pytest.raises(OSError, match="boom"):
        _upgrade_to(engine, "head")

    monkeypatch.undo()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(drive_root))
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "file")
    upgrade_head(engine)

    retried = _snapshot(engine)
    assert [r["name"] for r in retried["memory"]] == ["bad_fact", "good_fact", "identity", "patterns"]
    assert [r["content"] for r in retried["scratchpad_blocks"]] == ["live"]

    clean = _engine(tmp_path / "clean")
    upgrade_head(clean)
    clean_rows = _snapshot(clean)
    for table in CORE_MEMORY_TABLES:
        assert _comparable(table, retried[table]) == _comparable(table, clean_rows[table]), table
