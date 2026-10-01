from __future__ import annotations

import json
import pathlib

from alembic import command
from sqlalchemy import create_engine, text

from ouroboros.storage.migrations.runner import alembic_config, upgrade_head
from ouroboros.storage.runtime_factory import sqlite_url_for_path

_LEGACY_INSERT = text(
    "INSERT INTO messages (message_id, conversation_id, session_id, source, source_message_id, "
    "actor_type, channel, content, content_ref, content_hash, created_at, metadata_json, payload_json) "
    "VALUES (:message_id, 'c1', 's1', 'dialogs', '', 'owner', 'chat', :content, '', '', "
    ":created_at, :metadata_json, '{}')"
)


def test_alembic_upgrade_head_is_idempotent(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "state" / "runtime.sqlite3"
    url = sqlite_url_for_path(db_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    upgrade_head(engine)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = {
            row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
        }
        message_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(messages)"))}
        message_indexes = {row[1] for row in conn.execute(text("PRAGMA index_list(messages)"))}
    assert version == "0007"
    assert "tasks" in tables
    assert "messages" in tables
    assert "owner_runtime_state" in tables
    assert "task_episodes" in tables
    assert "tool_invocations" in tables
    assert "user_settings" in tables
    assert "schema_migrations" not in tables
    assert "chat_history_visible" in message_columns
    assert "idx_messages_chat_history_visible_created" in message_indexes

    with engine.connect() as conn:
        message_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(messages)"))}
        episode_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(task_episodes)"))}
        invocation_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tool_invocations)"))}
    assert "chat_history_visible" in message_columns
    assert {
        "episode_id",
        "task_id",
        "conversation_id",
        "user_text",
        "answer_preview",
        "search_terms_json",
        "expires_at",
    } <= episode_columns
    assert {
        "tool_invocation_id",
        "task_id",
        "conversation_id",
        "tool_name",
        "args_redacted_json",
        "result_summary_json",
        "search_terms_json",
        "expires_at",
    } <= invocation_columns

    upgrade_head(engine)
    with engine.connect() as conn:
        version_again = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version_again == "0007"


def _migration_module(name: str):
    """Import a version file by path — its module name starts with a digit."""
    import importlib.util

    import ouroboros.storage.migrations as migrations

    path = pathlib.Path(migrations.__file__).parent / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_to(engine, revision: str) -> None:
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)
    cfg.attributes.pop("connection", None)


def _insert_legacy_messages(conn, specs: list[tuple[str, dict]]) -> None:
    """Write rows the way the store did before 0004 added the column.

    ``payload_json`` is left as ``{}`` so the reader falls back to
    ``metadata_json``, which is the shape the backfill has to decide from.
    """
    conn.execute(
        _LEGACY_INSERT,
        [
            {
                "message_id": message_id,
                "content": f"body of {message_id}",
                "created_at": f"2026-07-20T10:{index // 60:02d}:{index % 60:02d}+00:00",
                "metadata_json": json.dumps(metadata),
            }
            for index, (message_id, metadata) in enumerate(specs)
        ],
    )


def test_the_legacy_backfill_hides_progress_and_synthetic_ids(tmp_path: pathlib.Path) -> None:
    """0003 -> head: rows 0004 defaulted are decided by 0006's predicate, and stay decided.

    That predicate hid progress *and* every negative chat id, and the rule has
    since narrowed to progress alone — but only for rows written under it. A
    revision is history once applied, so what it wrote to rows that predate the
    narrowing is what they keep: legacy A2A and skill-bridge traffic stays out of
    chat history, and nothing walks the table to change its mind.
    """
    engine = create_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    _upgrade_to(engine, "0003")

    with engine.begin() as conn:
        _insert_legacy_messages(
            conn,
            [
                ("msg_owner_1", {"chat_id": 42}),
                ("msg_progress_2", {"chat_id": 42, "is_progress": True}),
                ("msg_a2a_3", {"chat_id": -1001}),
                ("msg_a2a_str_4", {"chat_id": "-1002"}),
                ("msg_no_metadata_5", {}),
                ("msg_unparseable_6", {}),
            ],
        )
        # Metadata the reader answers `{}` for: not a progress message, so visible.
        conn.execute(text("UPDATE messages SET metadata_json = 'not json' WHERE message_id = 'msg_unparseable_6'"))

    upgrade_head(engine)

    with engine.connect() as conn:
        visible = {
            str(row[0]) for row in conn.execute(text("SELECT message_id FROM messages WHERE chat_history_visible = 1"))
        }
    assert visible == {
        "msg_owner_1",
        "msg_no_metadata_5",
        "msg_unparseable_6",
    }


def test_both_history_reads_agree_on_the_backfilled_column(tmp_path: pathlib.Path) -> None:
    """Chat history and the owner's own view answer from one column, so they agree.

    Including on the legacy rows 0006 hid: the A2A one below is absent from both,
    rather than from one of them. Whichever surface the owner or the prompt reads,
    the conversation it shows is the same conversation.
    """
    from ouroboros.storage.sql_runtime_store import SqlRuntimeStore

    engine = create_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    _upgrade_to(engine, "0003")
    with engine.begin() as conn:
        _insert_legacy_messages(
            conn,
            [
                ("msg_owner_1", {"chat_id": 42}),
                ("msg_progress_2", {"chat_id": 42, "is_progress": True}),
                ("msg_a2a_3", {"chat_id": -1001}),
                ("msg_owner_4", {"chat_id": 42}),
            ],
        )

    # The constructor runs the migration, so this is the upgrade path the store
    # takes on a real first start after the deploy.
    store = SqlRuntimeStore(engine, db_url="")

    chat_history = {m.message_id for m in store.list_chat_history_messages(limit=100)}
    owner_view = {m.message_id for m in store.list_owner_messages(limit=100, include_progress=False)}

    assert chat_history == {"msg_owner_1", "msg_owner_4"}
    assert chat_history == owner_view


def test_the_chat_history_backfill_is_idempotent(tmp_path: pathlib.Path) -> None:
    """A second run over an already-repaired table changes nothing."""
    engine = create_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    _upgrade_to(engine, "0003")
    with engine.begin() as conn:
        _insert_legacy_messages(
            conn,
            [
                ("msg_owner_1", {"chat_id": 42}),
                ("msg_progress_2", {"chat_id": 42, "is_progress": True}),
            ],
        )

    upgrade_head(engine)
    with engine.connect() as conn:
        first = dict(conn.execute(text("SELECT message_id, chat_history_visible FROM messages")).fetchall())

    upgrade_head(engine)
    with engine.connect() as conn:
        second = dict(conn.execute(text("SELECT message_id, chat_history_visible FROM messages")).fetchall())

    assert first == second
    assert first["msg_owner_1"]
    assert not first["msg_progress_2"]


def test_the_chat_history_backfill_pages_past_its_batch_size(tmp_path: pathlib.Path) -> None:
    """Keyset paging over the column it writes must not skip rows.

    The scan predicate is the column being updated, so an ``OFFSET`` walk drops
    rows as earlier pages leave the predicate. With more rows than one page and
    every one of them hideable, that regression leaves survivors behind.
    """
    total = _migration_module("0006_core_memory").ChatHistoryVisibilityBackfill.page_size * 2 + 7
    engine = create_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))
    _upgrade_to(engine, "0003")
    with engine.begin() as conn:
        _insert_legacy_messages(
            conn,
            [(f"msg_{index:05d}", {"chat_id": 42, "is_progress": True}) for index in range(total)],
        )

    upgrade_head(engine)

    with engine.connect() as conn:
        still_visible = conn.execute(text("SELECT COUNT(*) FROM messages WHERE chat_history_visible = 1")).scalar_one()
    assert still_visible == 0


def test_the_sqlite_runtime_engine_waits_for_the_write_lock(tmp_path: pathlib.Path) -> None:
    """Regression: core_memory shares this engine and takes no process-local lock.

    ``revise`` holds ``BEGIN IMMEDIATE`` across a Python callback, so writers do
    contend. With pysqlite's 5-second default the loser raised ``database is
    locked`` and the memory it was saving was lost; WAL additionally keeps
    readers out of the writer's way.
    """
    from ouroboros.storage.sql_runtime_store import _SQLITE_BUSY_TIMEOUT_MS, create_runtime_engine

    engine = create_runtime_engine(sqlite_url_for_path(tmp_path / "state" / "runtime.sqlite3"))

    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar_one() == _SQLITE_BUSY_TIMEOUT_MS
        assert str(conn.execute(text("PRAGMA journal_mode")).scalar_one()).lower() == "wal"


def test_no_revision_imports_a_shared_migration_helper() -> None:
    """A revision is history the moment it is applied, so it carries its own helpers.

    One imported from beside it is a helper whose next edit rewrites every revision that
    imports it — silently, and only for databases still to be created, which is how two
    deployments come to agree on ``alembic_version`` and disagree on what it means. The
    ``ouroboros.storage.*`` imports revisions do keep are the readers of live data
    (``message_field_crypto``, settings), which have to stay current by definition.
    """
    versions = pathlib.Path(__file__).resolve().parents[1] / "ouroboros/storage/migrations/versions"
    offenders = [
        path.name
        for path in sorted(versions.glob("[0-9]*.py"))
        if "from ouroboros.storage.migrations import" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"revisions importing a shared migration helper: {', '.join(offenders)}"
