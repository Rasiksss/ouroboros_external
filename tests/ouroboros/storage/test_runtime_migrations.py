from __future__ import annotations

import argparse
import ast
import json
import logging
import pathlib
import uuid

import pytest
from alembic import command
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError

from infra.migrations.runner import alembic_config, upgrade_head
from supervisor.infra.repositories.working_retrieval.repo import SqlWorkingRetrievalRepository
from tests.helpers.conversations import make_sql_runtime_persistence, sql_conversation_repository
from tests.helpers.revision_schema import (
    column_names,
    constraint_names,
    index_columns,
    index_definition,
    index_names,
    primary_key_columns,
    query_plan,
    table_names,
    unique_constraint_columns,
    unique_constraint_names,
)
from tests.helpers.scope import scope_for

_LEGACY_INSERT = text(
    "INSERT INTO messages (message_id, conversation_id, session_id, source, source_message_id, "
    "actor_type, channel, content, content_ref, content_hash, created_at, metadata_json, payload_json) "
    "VALUES (:message_id, 'c1', 's1', 'dialogs', '', 'owner', 'chat', :content, '', '', "
    ":created_at, :metadata_json, '{}')"
)


def test_alembic_upgrade_head_is_idempotent(blank_schema) -> None:
    engine = blank_schema()

    upgrade_head(engine)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = table_names(conn)
        message_columns = column_names(conn, "messages")
        message_indexes = index_names(conn, "messages")
        episode_columns = column_names(conn, "task_episodes")
        invocation_columns = column_names(conn, "tool_invocations")

    assert version == "0034"
    assert {
        "agents",
        "chats",
        "security_contexts",
        "mcp_user_tokens",
        "a2a_user_tokens",
        "tasks",
        "task_plans",
        "messages",
        "owner_runtime_state",
        "task_episodes",
        "tool_invocations",
        "consolidation_leases",
        "user_settings",
        "skill_activations",
        "tool_confirmations",
        "file_transfer_leases",
    } <= tables
    assert "schema_migrations" not in tables
    assert "chat_history_visible" in message_columns
    assert {"owner_id", "agent_id"} <= message_columns
    assert "conversation_id" not in message_columns
    assert "idx_messages_owner_agent_chat_visible_created" in message_indexes
    assert "idx_messages_owner_agent_chat_created" in message_indexes
    assert "inbound_unprocessed" in message_columns
    assert "idx_messages_inbound_unprocessed" in message_indexes
    assert {
        "episode_id",
        "task_id",
        "owner_id",
        "agent_id",
        "surface_id",
        "chat_id",
        "user_text",
        "answer_preview",
        "search_terms_json",
        "expires_at",
    } <= episode_columns
    assert {
        "tool_invocation_id",
        "task_id",
        "owner_id",
        "agent_id",
        "surface_id",
        "chat_id",
        "tool_name",
        "args_redacted_json",
        "result_summary_json",
        "search_terms_json",
        "expires_at",
    } <= invocation_columns

    upgrade_head(engine)
    with engine.connect() as conn:
        version_again = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version_again == version


@pytest.fixture
def rollout_engines(blank_schema):
    return [blank_schema(), blank_schema()]


def _schema_snapshot(engine, *, data=False):
    inspector = inspect(engine)
    result = {}
    with engine.connect() as conn:
        for table in sorted(inspector.get_table_names()):
            result[table] = {
                kind: getattr(inspector, f"get_{kind}")(table)
                for kind in (
                    "columns",
                    "indexes",
                    "pk_constraint",
                    "unique_constraints",
                    "foreign_keys",
                    "check_constraints",
                )
            }
            if data:
                result[table]["rows"] = sorted(map(tuple, conn.execute(text(f'SELECT * FROM "{table}"'))), key=repr)
    return json.dumps(result, default=str, sort_keys=True)


def _rollback_preflight(engine):
    cfg = alembic_config(str(engine.url))
    cfg.cmd_opts = argparse.Namespace(x=["binary_rollback"])
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        command.current(cfg)


def _migration_module(name: str):
    """Import a version file by path — its module name starts with a digit."""
    import importlib.util

    import infra.migrations as migrations

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


def _downgrade_to(engine, revision: str) -> None:
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, revision)
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


def test_the_legacy_backfill_hides_progress_and_synthetic_ids(blank_schema) -> None:
    """0003 -> head: rows 0004 defaulted are decided by 0006's predicate, and stay decided.

    That predicate hid progress *and* every negative chat id, and the rule has
    since narrowed to progress alone — but only for rows written under it. A
    revision is history once applied, so what it wrote to rows that predate the
    narrowing is what they keep: legacy A2A and skill-bridge traffic stays out of
    chat history, and nothing walks the table to change its mind.
    """
    engine = blank_schema()
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
            str(row[0])
            for row in conn.execute(text("SELECT message_id FROM messages WHERE chat_history_visible = true"))
        }
    assert visible == {
        "msg_owner_1",
        "msg_no_metadata_5",
        "msg_unparseable_6",
    }


def test_both_history_reads_agree_on_the_backfilled_column(blank_schema) -> None:
    """Chat history and the owner's own view answer from one column, so they agree.

    Including on the legacy rows 0006 hid: the A2A one below is absent from both,
    rather than from one of them. Whichever surface the owner or the prompt reads,
    the conversation it shows is the same conversation.
    """

    engine = blank_schema()
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

    # The deployment brings the schema up and the runtime then starts against it, which is
    # the order a real deploy takes: the store constructor migrates nothing.
    upgrade_head(engine)
    store = make_sql_runtime_persistence(engine, db_url="")

    scope = scope_for("dialogs", 42)
    chat_history = {
        m.message_id
        for m in sql_conversation_repository(store.engine).list_chat_history_messages(scope=scope, limit=100)
    }
    owner_view = {
        m.message_id
        for m in sql_conversation_repository(store.engine).list_owner_messages(
            scope=scope, limit=100, include_progress=False
        )
    }

    assert chat_history == {"msg_owner_1", "msg_owner_4"}
    assert chat_history == owner_view


def test_the_chat_history_backfill_is_idempotent(blank_schema) -> None:
    """A second run over an already-repaired table changes nothing."""
    engine = blank_schema()
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


def test_the_chat_history_backfill_pages_past_its_batch_size(blank_schema) -> None:
    """Keyset paging over the column it writes must not skip rows.

    The scan predicate is the column being updated, so an ``OFFSET`` walk drops
    rows as earlier pages leave the predicate. With more rows than one page and
    every one of them hideable, that regression leaves survivors behind.
    """
    total = _migration_module("0006_core_memory").ChatHistoryVisibilityBackfill.page_size * 2 + 7
    engine = blank_schema()
    _upgrade_to(engine, "0003")
    with engine.begin() as conn:
        _insert_legacy_messages(
            conn,
            [(f"msg_{index:05d}", {"chat_id": 42, "is_progress": True}) for index in range(total)],
        )

    upgrade_head(engine)

    with engine.connect() as conn:
        still_visible = conn.execute(
            text("SELECT COUNT(*) FROM messages WHERE chat_history_visible = true")
        ).scalar_one()
    assert still_visible == 0


def test_no_revision_imports_a_shared_migration_helper() -> None:
    """A revision is history the moment it is applied, so it carries its own helpers.

    One imported from beside it is a helper whose next edit rewrites every revision that
    imports it — silently, and only for databases still to be created, which is how two
    deployments come to agree on ``alembic_version`` and disagree on what it means. The
    ``ouroboros.storage.*`` imports revisions do keep are the readers of live data
    (``message_field_crypto``, settings), which have to stay current by definition.
    """
    versions = pathlib.Path(__file__).resolve().parents[3] / "infra/migrations/versions"
    offenders = [
        path.name
        for path in sorted(versions.glob("[0-9]*.py"))
        if "from infra.migrations import" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"revisions importing a shared migration helper: {', '.join(offenders)}"


def test_revisions_only_import_field_crypto_api_from_production() -> None:
    versions = pathlib.Path(__file__).resolve().parents[3] / "infra/migrations/versions"
    offenders: dict[str, list[str]] = {}
    for path in sorted(versions.glob("[0-9]*.py")):
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and str(node.module or "").startswith("ouroboros"):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names if alias.name.startswith("ouroboros"))
        extra = sorted(imported - {"decrypt_field", "encrypt_field", "FieldKeyUnavailable"})
        if extra:
            offenders[path.name] = extra
    assert offenders == {}, f"revisions reaching into production code: {offenders}"


def test_the_revisions_that_decrypt_pin_the_keyword_the_production_signature_keeps() -> None:
    import inspect

    from ouroboros.storage.message_field_crypto import decrypt_field

    signature = inspect.signature(decrypt_field)
    assert "dialect_name" in signature.parameters
    assert signature.parameters["dialect_name"].default == ""
    assert signature.parameters["dialect_name"].kind is inspect.Parameter.KEYWORD_ONLY
    assert decrypt_field("plain text", dialect_name="sqlite") == "plain text"

    versions = pathlib.Path(__file__).resolve().parents[3] / "infra/migrations/versions"
    callers = sorted(
        path.name for path in versions.glob("[0-9]*.py") if "dialect_name" in path.read_text(encoding="utf-8")
    )
    assert callers == [
        "0006_core_memory.py",
        "0016_chat_keyed_history_kindless_agents_and_journal.py",
        "0021_chat_execution.py",
    ], callers


def test_0006_upgrades_with_no_object_storage_configured(blank_schema, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.delenv("OUROBOROS_S3_BUCKET", raising=False)
    engine = blank_schema()

    upgrade_head(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM memory")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM scratchpad_blocks")).scalar_one() == 0


def test_core_memory_is_owned_by_an_owner_and_an_agent(blank_schema) -> None:
    """0008: the ownership key is the pair, so two agents of one owner do not collide."""
    engine = blank_schema()
    upgrade_head(engine)

    with engine.connect() as conn:
        memory_columns = column_names(conn, "memory")
        scratchpad_columns = column_names(conn, "scratchpad_blocks")
        memory_constraints = constraint_names(conn, "memory")
    assert "agent_id" in memory_columns
    assert "agent_id" in scratchpad_columns
    assert "uq_memory_owner_agent_name" in memory_constraints
    assert "uq_memory_owner_name" not in memory_constraints
    assert "ck_memory_status" in memory_constraints

    index_ddl = index_definition(engine, "idx_memory_owner_status_updated")
    plan = query_plan(
        engine,
        "SELECT name FROM memory WHERE owner_id=%(owner)s AND agent_id=%(agent)s "
        "AND status IN ('active') ORDER BY updated_at DESC, name ASC",
        {"owner": "global", "agent": "main"},
    )
    # An index reflected without its per-column sort order would leave the prompt listing
    # paying a sort for its ORDER BY on every task.
    assert "updated_at DESC" in index_ddl
    assert "Sort" not in plan, plan

    insert = text("INSERT INTO memory (owner_id, agent_id, kind, name) VALUES (:owner, :agent, 'user', 'notes')")
    with engine.begin() as conn:
        conn.execute(insert, {"owner": "global", "agent": "main"})
        conn.execute(insert, {"owner": "global", "agent": "second"})
        conn.execute(insert, {"owner": "other", "agent": "main"})

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert, {"owner": "global", "agent": "main"})


def test_existing_core_memory_rows_are_backfilled_to_the_single_agent(blank_schema) -> None:
    """A row written before the axis existed keeps working; the column default names its agent."""
    engine = blank_schema()
    config = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0007")

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO memory (owner_id, kind, name, content) VALUES ('global','user','legacy','body')")
        )
        conn.execute(
            text("INSERT INTO scratchpad_blocks (owner_id, ts, source, content) VALUES ('global','t','task','note')")
        )

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0008")

    with engine.connect() as conn:
        memory_row = conn.execute(text("SELECT agent_id, content FROM memory WHERE name='legacy'")).mappings().one()
        block = conn.execute(text("SELECT agent_id, content FROM scratchpad_blocks")).mappings().one()
    assert memory_row == {"agent_id": "main", "content": "body"}
    assert block == {"agent_id": "main", "content": "note"}


def _at_0007(blank_schema):
    engine = blank_schema()
    config = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0007")
    return engine, config


def _upgrade_to_0008(engine, config) -> None:
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0008")


_LEGACY_TASK_INSERT = text(
    "INSERT INTO tasks (task_id, session_id, conversation_id, source, correlation_id, status, task_type, "
    "chat_id, input_json, created_at, payload_json) VALUES (:task_id, '', :conversation_id, :source, '', "
    ":status, 'task', :chat_id, :input_json, 't', :payload_json)"
)


def _legacy_task(**overrides) -> dict[str, object]:
    task_id = str(overrides.get("task_id") or "t1")
    chat_id = overrides.get("chat_id", "42")
    source = str(overrides.get("source") or "dialogs")
    stored_input = {"id": task_id, "type": "task", "text": "do it", "chat_id": chat_id, "source": source}
    return {
        "task_id": task_id,
        "conversation_id": str(overrides.get("conversation_id") or ""),
        "source": source,
        "status": str(overrides.get("status") or "queued"),
        "chat_id": chat_id,
        "input_json": json.dumps(stored_input),
        "payload_json": json.dumps({"task_id": task_id, "status": "queued", "input": stored_input}),
    }


def test_a_task_pending_across_the_rekey_can_still_be_claimed(blank_schema) -> None:
    """0008: a claim reads identity from ``input["scope"]``, and a queued row carries none.

    Both blobs are written, because the reader prefers the payload and falls back to the
    stored input — backfilling one leaves the other saying the row has no owner.
    """
    engine, config = _at_0007(blank_schema)
    with engine.begin() as conn:
        conn.execute(_LEGACY_TASK_INSERT, _legacy_task(task_id="queued-1", chat_id="42", source="sberchat"))
        conn.execute(_LEGACY_TASK_INSERT, _legacy_task(task_id="queued-2", chat_id="-9001", source="unknown"))
        conn.execute(_LEGACY_TASK_INSERT, _legacy_task(task_id="done-1", chat_id="42", status="completed"))

    _upgrade_to_0008(engine, config)

    with engine.connect() as conn:
        rows = {
            row["task_id"]: row
            for row in conn.execute(text("SELECT task_id, status, input_json, payload_json FROM tasks")).mappings()
        }
    first = json.loads(rows["queued-1"]["input_json"])["scope"]
    assert first == {"owner_id": "global", "agent_id": "main", "surface_id": "dialogs", "chat_id": "42"}
    assert json.loads(rows["queued-1"]["payload_json"])["input"]["scope"] == first
    # No source names a surface, so the chat id does: this one is below the A2A ceiling.
    assert json.loads(rows["queued-2"]["input_json"])["scope"]["surface_id"] == "a2a"
    # A finished row is never claimed again, so it is left exactly as it was.
    assert "scope" not in json.loads(rows["done-1"]["input_json"])


def test_a_pending_task_with_no_chat_is_failed_rather_than_left_to_crash(blank_schema) -> None:
    engine, config = _at_0007(blank_schema)
    with engine.begin() as conn:
        conn.execute(_LEGACY_TASK_INSERT, _legacy_task(task_id="stranded", chat_id=None))

    _upgrade_to_0008(engine, config)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT status, error_code FROM tasks WHERE task_id='stranded'")).mappings().one()
    assert row["status"] == "failed"
    assert row["error_code"] == "owner_scope_rekey"


def test_working_memory_keyed_by_the_chat_alone_survives_the_rekey(blank_schema) -> None:
    """0008: every episode and invocation was keyed ``chat:<id>``, naming no surface.

    Reading that prefix through the chat-id windows is what keeps working memory; parking it
    would have left both tables unreachable and their rows unmodellable.
    """
    engine, config = _at_0007(blank_schema)
    with engine.begin() as conn:
        for episode_id, key in (("e1", "chat:42"), ("e2", "chat:1"), ("e3", "chat:-9001"), ("e4", "garbage")):
            conn.execute(
                text(
                    "INSERT INTO task_episodes (episode_id, task_id, conversation_id, created_at) "
                    "VALUES (:episode_id, 'task-1', :conversation_id, 't')"
                ),
                {"episode_id": episode_id, "conversation_id": key},
            )
        conn.execute(
            text(
                "INSERT INTO tool_invocations (tool_invocation_id, task_id, conversation_id, tool_name, created_at) "
                "VALUES ('i1', 'task-1', 'chat:42', 'bash', 't')"
            )
        )

    _upgrade_to_0008(engine, config)

    with engine.connect() as conn:
        episodes = {
            row["episode_id"]: (row["surface_id"], row["chat_id"])
            for row in conn.execute(text("SELECT episode_id, surface_id, chat_id FROM task_episodes")).mappings()
        }
        invocation = (
            conn.execute(text("SELECT surface_id, chat_id FROM tool_invocations WHERE tool_invocation_id='i1'"))
            .mappings()
            .one()
        )
    assert episodes["e1"] == ("dialogs", "42")
    assert episodes["e2"] == ("browser", "1")
    assert episodes["e3"] == ("a2a", "-9001")
    # A key that names neither a surface nor a chat is still parked, and still matches nothing.
    assert episodes["e4"] == ("skill-bridge", "")
    assert (invocation["surface_id"], invocation["chat_id"]) == ("dialogs", "42")


def test_two_context_summaries_are_refused_before_any_table_is_altered(blank_schema) -> None:
    """0008: the refusal comes before the first DDL, not beside the constraint it protects.

    Raising beside the constraint left ``messages`` rekeyed and ``alembic_version`` at 0007,
    and the preflight then refused every retry — a database no upgrade could reach. The
    listener is what still distinguishes the two on PostgreSQL, where a rolled-back
    transaction hides the difference.
    """
    engine, config = _at_0007(blank_schema)
    insert = text(
        "INSERT INTO context_summaries (summary_id, conversation_id, created_at, payload_json) "
        "VALUES (:summary_id, :conversation_id, 't', '{}')"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"summary_id": "s1", "conversation_id": "chat:1"})
        conn.execute(insert, {"summary_id": "s2", "conversation_id": "chat:2"})

    statements: list[str] = []

    def _record(conn, cursor, sql, *rest):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        with pytest.raises(RuntimeError, match="context_summaries"):
            _upgrade_to_0008(engine, config)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert not any(sql.lstrip().split()[0].upper() in {"ALTER", "DROP", "CREATE"} for sql in statements), statements

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        message_columns = column_names(conn, "messages")
    assert version == "0007"
    assert "conversation_id" in message_columns
    assert "owner_id" not in message_columns


def test_the_soft_notice_column_goes_up_and_comes_back_down(blank_schema) -> None:
    """0010 adds one nullable column and drops it again: the shape of one claim.

    A row written before the upgrade reads as "not yet noticed": there is no backfill, and a
    time nobody observed would be worse than the one extra notice that costs.
    """
    engine = blank_schema()
    _upgrade_to(engine, "0009")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, session_id, owner_id, agent_id, source, correlation_id, "
                "status, task_type, chat_id, input_json, input_ref, result, result_ref, error_code, "
                "priority, attempt, worker_id, queued_at, created_at, started_at, heartbeat_at, "
                "finished_at, metadata_json, payload_json) VALUES ('before', '', 'global', 'main', "
                "'dialogs', '', 'running', 'task', '42', '{}', '', '', '', '', 1, 1, 0, '', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', "
                "'', '{}', '{}')"
            )
        )

    _upgrade_to(engine, "0010")

    with engine.connect() as conn:
        columns = column_names(conn, "tasks")
        unnoticed = conn.execute(text("SELECT soft_notice_at FROM tasks WHERE task_id='before'")).scalar_one()
    assert "soft_notice_at" in columns
    assert unnoticed is None

    _downgrade_to(engine, "0009")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        columns = column_names(conn, "tasks")
    assert version == "0009"
    assert "soft_notice_at" not in columns


def test_the_consolidation_lease_table_goes_up_and_comes_back_down(blank_schema) -> None:
    """0010 creates one table with a two-column primary key and drops it again.

    The key *is* the single-flight rule, so the test is the constraint: a second row for one
    ownership pair is refused by the database rather than by a check somebody can skip.
    """
    engine = blank_schema()
    _upgrade_to(engine, "0010")

    with engine.connect() as conn:
        columns = column_names(conn, "consolidation_leases")
    assert {
        "owner_id",
        "agent_id",
        "holder_id",
        "lease_expires_at",
        "consecutive_failures",
        "last_failure_at",
        "updated_at",
    } == columns

    insert = text(
        "INSERT INTO consolidation_leases (owner_id, agent_id, holder_id, lease_expires_at, "
        "consecutive_failures, last_failure_at, updated_at) VALUES ('global', 'main', :holder, "
        "'2999-01-01T00:00:00+00:00', 0, '', '2026-08-01T00:00:00+00:00')"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"holder": "holder-a"})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert, {"holder": "holder-b"})

    _downgrade_to(engine, "0009")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = table_names(conn)
    assert version == "0009"
    assert "consolidation_leases" not in tables


def test_the_schedule_tables_go_up_and_come_back_down(blank_schema) -> None:
    """0015 creates the schedule rows and their slot receipts, and drops both again.

    The receipt's primary key *is* the at-most-once dispatch rule, so the test is the
    constraint: a second claim of one slot is refused by the database rather than by a
    check somebody can skip.
    """
    engine = blank_schema()
    _upgrade_to(engine, "0018")

    with engine.connect() as conn:
        schedule_columns = column_names(conn, "schedules")
        run_columns = column_names(conn, "schedule_runs")
    assert {
        "schedule_id",
        "owner_id",
        "agent_id",
        "chat_id",
        "revision",
        "state",
        "next_run_at",
        "payload_json",
        "created_at",
        "updated_at",
    } == schedule_columns
    assert {
        "schedule_id",
        "slot_id",
        "owner_id",
        "agent_id",
        "schedule_revision",
        "status",
        "claimed_at",
        "finished_at",
        "error",
        "permanent",
    } == run_columns
    with engine.connect() as conn:
        run_indexes = index_names(conn, "schedule_runs")
    # The due scan asks "does this schedule have a live claim", and every replica sweeps settled
    # receipts — neither may become a full scan.
    assert {"idx_schedule_runs_claim", "idx_schedule_runs_settled"} <= run_indexes

    insert = text(
        "INSERT INTO schedule_runs (schedule_id, slot_id, owner_id, agent_id, schedule_revision, "
        "status, claimed_at, finished_at, error) VALUES ('sched-1', '1700000000', 'global', 'main', 1, "
        ":status, '2026-08-01T00:00:00+00:00', '', '')"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"status": "claimed"})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert, {"status": "dispatched"})

    _downgrade_to(engine, "0014")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = table_names(conn)
    assert version == "0014"
    assert "schedules" not in tables
    assert "schedule_runs" not in tables


def test_the_announcement_table_goes_up_and_comes_back_down(blank_schema) -> None:
    """0011 creates one table keyed by the announced configuration, and drops it again.

    The primary key *is* the once-per-deployment rule: a second announcer for one
    configuration is refused by the database rather than by a check somebody can skip.
    """
    engine = blank_schema()
    _upgrade_to(engine, "0011")

    with engine.connect() as conn:
        columns = column_names(conn, "agent_configuration_announcements")
    assert {"config_hash", "component", "announced_at"} == columns

    insert = text(
        "INSERT INTO agent_configuration_announcements (config_hash, component, announced_at) "
        "VALUES ('hash-a', :component, '2026-08-01T00:00:00+00:00')"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"component": "supervisor"})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert, {"component": "agent-runtime"})

    _downgrade_to(engine, "0010")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = table_names(conn)
    assert version == "0010"
    assert "agent_configuration_announcements" not in tables


def test_a_row_written_before_the_upgrade_has_no_instance_and_no_stop(blank_schema) -> None:
    """No backfill: an existing row reads as claimed by nobody we can name."""
    engine = blank_schema()
    _upgrade_to(engine, "0008")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, session_id, owner_id, agent_id, source, correlation_id, "
                "status, task_type, chat_id, input_json, input_ref, result, result_ref, error_code, "
                "priority, attempt, worker_id, queued_at, created_at, started_at, heartbeat_at, "
                "finished_at, metadata_json, payload_json) VALUES ('legacy', '', 'global', 'main', "
                "'dialogs', '', 'running', 'task', '42', '{}', '', '', '', '', 1, 1, 0, '', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', "
                "'', '{}', '{}')"
            )
        )

    upgrade_head(engine)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT runtime_id, stop_requested_at FROM tasks WHERE task_id='legacy'")).one()
    assert row == (None, None)


# --- 0010: runtime state, spend and audit belong to an owner ------------------


def _at_0009(blank_schema):
    engine = blank_schema()
    _upgrade_to(engine, "0009")
    return engine


def _seed_pre_0010(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO owner_runtime_state (tenant_id, payload_json, updated_at) "
                "VALUES ('default', :payload, 'then')"
            ),
            {"payload": json.dumps({"session_id": "s1", "spent_calls": 3})},
        )
        conn.execute(text("INSERT INTO llm_usage (usage_id, created_at, payload_json) VALUES ('u1', 'then', '{}')"))
        conn.execute(
            text(
                "INSERT INTO audit_events (event_id, timestamp, event_type, payload_json) "
                "VALUES ('e1', 'then', 'thing', '{}')"
            )
        )


def test_0010_rekeys_the_state_row_onto_the_ownership_pair(blank_schema) -> None:
    engine = _at_0009(blank_schema)
    _seed_pre_0010(engine)

    upgrade_head(engine)

    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text("SELECT * FROM owner_runtime_state")).mappings()]
        columns = column_names(conn, "owner_runtime_state")
    assert columns == {"owner_id", "agent_id", "payload_json", "updated_at"}
    assert len(rows) == 1
    assert (rows[0]["owner_id"], rows[0]["agent_id"]) == ("global", "main")
    # The row keeps its meaning: it is the same state, now under the owner it described.
    assert json.loads(rows[0]["payload_json"]) == {"session_id": "s1", "spent_calls": 3}


def test_0010_backfills_spend_and_audit_with_the_legacy_owner(blank_schema) -> None:
    """Never null: a null would have to be handled by every reader forever."""
    engine = _at_0009(blank_schema)
    _seed_pre_0010(engine)

    upgrade_head(engine)

    with engine.connect() as conn:
        usage = conn.execute(text("SELECT owner_id, agent_id FROM llm_usage WHERE usage_id='u1'")).one()
        audit = conn.execute(text("SELECT owner_id, agent_id FROM audit_events WHERE event_id='e1'")).one()
        usage_indexes = index_names(conn, "llm_usage")
        audit_indexes = index_names(conn, "audit_events")
    assert tuple(usage) == ("global", "main")
    assert tuple(audit) == ("global", "main")
    assert "idx_llm_usage_owner" in usage_indexes
    assert "idx_audit_events_owner" in audit_indexes


def test_0010_downgrades_back_to_the_tenant_key(blank_schema) -> None:
    engine = _at_0009(blank_schema)
    _seed_pre_0010(engine)
    _upgrade_to(engine, "0010")

    _downgrade_to(engine, "0009")

    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text("SELECT * FROM owner_runtime_state")).mappings()]
        usage_columns = column_names(conn, "llm_usage")
    assert [row["tenant_id"] for row in rows] == ["default"]
    assert json.loads(rows[0]["payload_json"]) == {"session_id": "s1", "spent_calls": 3}
    assert "owner_id" not in usage_columns


def test_0010_refuses_several_tenant_rows_rather_than_choosing_one(blank_schema) -> None:
    """Collapsing them onto one owner would silently lose whichever it did not pick."""
    engine = _at_0009(blank_schema)
    _seed_pre_0010(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO owner_runtime_state (tenant_id, payload_json, updated_at) VALUES ('second', '{}', 'then')"
            )
        )

    with pytest.raises(Exception, match="several tenant rows"):
        upgrade_head(engine)

    with engine.connect() as conn:
        assert column_names(conn, "owner_runtime_state") == {
            "tenant_id",
            "payload_json",
            "updated_at",
        }


# -- 0010: a message's identity carries its ownership --------------------------


def _run_step(engine, name: str) -> None:
    """Apply one named step of 0010 by itself.

    The revision folds seven changes into one stamp, so the states *between* two of its
    steps are no longer reachable through alembic. Driving the revision's own functions is
    what keeps those states testable — and it is the revision's code that runs, not a
    reimplementation of it.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    revision = _migration_module("0010_multiuser_runtime")
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            getattr(revision, name)()


def test_the_message_lift_reads_no_encrypted_field() -> None:
    """The lift has to run on a deployment whose chat encryption key is absent.

    Asserted against the lift's own code rather than by feeding it a value that looks
    encrypted: the latter passes whether or not anything decrypts. Only the lift's
    functions are read — other steps of this revision legitimately carry rows by their
    ``payload_json``.
    """
    import ast
    import importlib

    revision = importlib.import_module("infra.migrations.versions.0010_multiuser_runtime")
    tree = ast.parse(pathlib.Path(revision.__file__).read_text(encoding="utf-8"))
    lift = {"_dedup_key", "_lifted_identities", "_write_identities", "_verify_unique", "_lift_message_identity"}
    bodies = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in lift]
    assert {node.name for node in bodies} == lift, "the lift's functions moved or were renamed"
    code = "\n".join(ast.unparse(node) for node in bodies)

    for forbidden in ("message_field_crypto", "decrypt_field", "metadata_json", "payload_json"):
        assert forbidden not in code, f"the lift must not touch {forbidden}"


def test_the_revision_refuses_a_database_it_does_not_recognise(blank_schema) -> None:
    """Preflight before any DDL: a half-applied revision is not a state anyone reaches."""
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    revision = importlib.import_module("infra.migrations.versions.0010_multiuser_runtime")
    engine = blank_schema()

    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context), pytest.raises(RuntimeError, match="expected tables missing"):
            revision.upgrade()


def test_the_lift_gives_every_existing_message_an_owner_carrying_identity(blank_schema) -> None:

    engine = blank_schema()

    _upgrade_to(engine, "0009")
    with engine.begin() as conn:
        for index, owner in enumerate(("alice", "bob")):
            conn.execute(
                text(
                    "INSERT INTO messages (message_id, owner_id, agent_id, session_id, source, "
                    "source_message_id, actor_type, channel, content, content_ref, content_hash, "
                    "created_at, metadata_json, payload_json) VALUES (:message_id, :owner_id, 'main', "
                    "'s1', 'browser', '1', 'owner', 'chat', 'hello', '', '', '2026-01-01T00:00:00Z', "
                    "'<encrypted>', '<encrypted>')"
                ),
                {"message_id": f"m-{index}", "owner_id": owner},
            )

    upgrade_head(engine)

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT message_id, dedup_key FROM messages")).fetchall())
        indexes = index_names(conn, "messages")

    assert all(rows.values()), "every row must carry the new identity"
    assert len(set(rows.values())) == 2
    assert "uq_messages_dedup_key" in indexes
    # The same fold the application computes, from the three plain columns.
    expected = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["alice", "main", "m-0"], separators=(",", ":"))).hex
    assert rows["m-0"] == expected


def test_lifted_messages_are_reachable_by_the_application_derivation(blank_schema) -> None:
    """A message written before the change and looked up after it resolves to its own row."""
    from ouroboros.storage.chat_store import message_dedup_key
    from tests.helpers.shared import TEST_SCOPE

    engine = blank_schema()

    _upgrade_to(engine, "0009")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO messages (message_id, owner_id, agent_id, session_id, source, "
                "source_message_id, actor_type, channel, content, content_ref, content_hash, "
                "created_at, metadata_json, payload_json) VALUES ('old-key', :owner_id, :agent_id, "
                "'s1', 'browser', '1', 'owner', 'chat', 'hello', '', '', '2026-01-01T00:00:00Z', "
                "'{}', '{}')"
            ),
            {"owner_id": TEST_SCOPE.owner_id, "agent_id": TEST_SCOPE.agent_id},
        )

    upgrade_head(engine)

    with engine.connect() as conn:
        stored = conn.execute(text("SELECT dedup_key FROM messages WHERE message_id='old-key'")).scalar_one()
    assert stored == message_dedup_key(TEST_SCOPE, "old-key")


def test_the_revision_refuses_to_run_twice(blank_schema) -> None:
    """Preflight before any DDL, so a partly-applied revision is not a state anyone reaches."""
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    revision_0010 = importlib.import_module("infra.migrations.versions.0010_multiuser_runtime")

    engine = blank_schema()
    upgrade_head(engine)

    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            with pytest.raises(RuntimeError, match="already exists"):
                revision_0010.upgrade()


# -- 0010: a task's parent is a column the claim can read -----------------------


def _at_0009_with_run_addressing(blank_schema):
    """A database at 0009 with only the first step of 0010 applied.

    The parentage step reads the run-addressing columns and hands rows back by clearing
    them, so the state it acts on exists only between two steps of one revision. Alembic
    cannot stop there any more; the revision's own functions can.
    """
    engine = blank_schema()
    _upgrade_to(engine, "0009")
    _run_step(engine, "_add_run_addressing")
    return engine


def _addressed_task_row(conn, task_id: str, *, status: str, payload: dict, owner_id: str = "alice") -> None:
    conn.execute(
        text(
            "INSERT INTO tasks (task_id, session_id, owner_id, agent_id, source, correlation_id, "
            "status, task_type, chat_id, input_json, input_ref, result, result_ref, error_code, "
            "priority, attempt, worker_id, queued_at, created_at, started_at, heartbeat_at, "
            "finished_at, metadata_json, payload_json, runtime_id, stop_requested_at) "
            "VALUES (:task_id, '', :owner_id, 'main', 'browser', '', :status, 'task', '42', '{}', "
            "'', '', '', '', 1, 1, 0, '', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', "
            "'2026-08-01T00:00:00+00:00', '', '{}', :payload_json, 'rt-1', NULL)"
        ),
        {
            "task_id": task_id,
            "owner_id": owner_id,
            "status": status,
            "payload_json": json.dumps(payload),
        },
    )


def test_the_parentage_step_backfills_a_parent_written_inside_the_task_input(blank_schema) -> None:
    """Where every row written before the column actually carries it.

    ``TaskRecord`` gained ``parent_task_id`` on this branch; before it, the parent lived
    only in the agent's task dict at ``payload["input"]["parent_task_id"]``. Reading the top
    level alone backfilled nothing, which made every stored subtask look like a root.
    """
    engine = _at_0009_with_run_addressing(blank_schema)
    with engine.begin() as conn:
        _addressed_task_row(conn, "root1", status="running", payload={"input": {}})
        _addressed_task_row(conn, "child1", status="queued", payload={"input": {"parent_task_id": "root1"}})

    _run_step(engine, "_add_task_parentage")

    with engine.connect() as conn:
        parents = dict(conn.execute(text("SELECT task_id, parent_task_id FROM tasks")).fetchall())
        statuses = dict(conn.execute(text("SELECT task_id, status FROM tasks")).fetchall())

    assert parents["child1"] == "root1"
    assert parents["root1"] == ""
    # And because the subtask is now known to be one, the running root is left alone.
    assert statuses["root1"] == "running"


def test_the_parentage_step_does_not_demote_a_running_subtask_as_a_second_root(blank_schema) -> None:
    """The duplicate-root sweep counts roots; a subtask is not one.

    With the parent unread, a running subtask read as an owner's second running root and
    was demoted to ``interrupted`` with its instance cleared — a live run silently killed
    by an upgrade.
    """
    engine = _at_0009_with_run_addressing(blank_schema)
    with engine.begin() as conn:
        _addressed_task_row(conn, "root1", status="running", payload={"input": {}})
        _addressed_task_row(conn, "child1", status="running", payload={"input": {"parent_task_id": "root1"}})

    _run_step(engine, "_add_task_parentage")

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT task_id, status FROM tasks")).fetchall())

    assert rows == {"root1": "running", "child1": "running"}


def test_the_parentage_step_clears_the_stop_on_a_row_it_hands_back(blank_schema) -> None:
    """A demoted row is meant to be claimed again, so it must not carry the old run's stop."""
    engine = _at_0009_with_run_addressing(blank_schema)
    with engine.begin() as conn:
        _addressed_task_row(conn, "first", status="running", payload={"input": {}})
        _addressed_task_row(conn, "second", status="running", payload={"input": {}})
        conn.execute(text("UPDATE tasks SET stop_requested_at='2026-08-01T00:00:00+00:00'"))

    _run_step(engine, "_add_task_parentage")

    with engine.connect() as conn:
        demoted = conn.execute(
            text("SELECT status, runtime_id, stop_requested_at FROM tasks WHERE status='interrupted'")
        ).fetchall()

    assert len(demoted) == 1
    assert demoted[0][1] is None
    assert demoted[0][2] is None


def test_the_run_addressing_columns_go_up_and_come_back_down(blank_schema) -> None:
    """0010 adds two nullable columns and the sweep's index, and drops all three again."""
    engine = blank_schema()
    _upgrade_to(engine, "0010")

    with engine.connect() as conn:
        columns = column_names(conn, "tasks")
        indexes = index_names(conn, "tasks")
    assert {"runtime_id", "stop_requested_at"} <= columns
    assert "idx_tasks_status_heartbeat" in indexes

    _downgrade_to(engine, "0008")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        columns = column_names(conn, "tasks")
        indexes = index_names(conn, "tasks")
    assert version == "0008"
    assert not ({"runtime_id", "stop_requested_at"} & columns)
    assert "idx_tasks_status_heartbeat" not in indexes


def test_0014_clean_and_actual_develop_upgrade_have_identical_schema_and_codecs(rollout_engines):
    from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
    from ouroboros.storage.runtime_models import TaskRequest

    fresh, upgraded = rollout_engines
    _upgrade_to(upgraded, "0013")
    with upgraded.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, owner_id, agent_id, status, task_type, input_json, "
                "metadata_json, payload_json, created_at, source, chat_id) VALUES ('legacy', 'owner', 'agent', 'queued', "
                "'task', '{}', '{}', '{}', '2026-01-01T00:00:00Z', 'browser', 'chat')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO scratchpad_blocks (owner_id, agent_id, ts, source, content, status) "
                "VALUES ('owner', 'agent', 't', 'task', 'main note', 'active')"
            )
        )
    upgrade_head(fresh)
    upgrade_head(upgraded)
    schema = _schema_snapshot(fresh)
    assert _schema_snapshot(upgraded) == schema
    before = _schema_snapshot(upgraded, data=True)
    upgrade_head(upgraded)
    assert _schema_snapshot(upgraded, data=True) == before
    from tests.helpers.shared import scope_with

    scope = scope_with(scope_for("browser", "chat"), owner_id="owner", agent_id="agent")
    store = make_sql_runtime_persistence(upgraded, db_url="")
    for task_id in ("legacy", "rolling-old-writer"):
        if task_id != "legacy":
            with upgraded.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO tasks (task_id, owner_id, agent_id, status, task_type, input_json, "
                        "metadata_json, payload_json, created_at) SELECT :id, owner_id, agent_id, 'queued', "
                        "task_type, input_json, metadata_json, payload_json, created_at FROM tasks WHERE task_id='legacy'"
                    ),
                    {"id": task_id},
                )
        task = store.tasks_repo.get_task(task_id)
        assert (task.job_id, task.detached, task.lineage_ordinal, task.status) == (task_id, False, 0, "queued")
        assert (task.terminal_published_at, task.terminal_publish_claim, task.terminal_publish_after) == (
            None,
            None,
            None,
        )
        assert task.metadata == {}
        store.tasks_repo.update_task_status(task_id, "completed", result="legacy result")
        assert store.tasks_repo.get_task(task_id).result == "legacy result"
    assert [b.content for b in SqlScratchpad(upgraded).list_active(scope)] == ["main note"]
    created = store.tasks_repo.create_task(TaskRequest(id="new-writer", scope=scope.to_dict(), text="round trip"))
    assert store.tasks_repo.get_task(created.task_id).model_dump() == created.model_dump()
    assert {
        "idx_tasks_owner_job",
        "idx_tasks_terminal_publication",
        "uq_tasks_one_running_foreground_root_per_chat",
    } <= {i["name"] for i in inspect(upgraded).get_indexes("tasks")}
    assert "idx_scratchpad_blocks_partition_status_id" in schema


def test_0014_binary_rollback_refuses_each_pending_obligation_and_preserves_drained_jobs(rollout_engines, monkeypatch):
    from infra.migrations import runner
    from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
    from ouroboros.storage.settings.schema import AppSettings

    engine = rollout_engines[0]
    upgrade_head(engine)
    store = make_sql_runtime_persistence(engine, db_url="")
    monkeypatch.setattr(runner, "head_schema_version", lambda: 13)
    settings = AppSettings()
    assert settings.OUROBOROS_DURABLE_JOBS_ENABLED is False
    monkeypatch.setattr("ouroboros.storage.settings.store.load_settings", lambda: settings)
    settled = {
        "terminal_memory_done": True,
        "terminal_attachments": [{"id": "sent"}, {"id": "lost"}],
        "terminal_delivery": {"text": "accepted", "sent": "accepted", "lost": "unavailable"},
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, job_id, owner_id, agent_id, status, detached, metadata_json, created_at, payload_json) "
                "VALUES ('old-attempt', 'job', 'owner', 'agent', 'failed', true, '{}', 't', '{}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, job_id, lineage_ordinal, owner_id, agent_id, status, detached, "
                "created_at, payload_json, terminal_published_at, metadata_json) VALUES ('winner', 'job', 1, 'owner', 'agent', "
                "'completed', true, 't', '{}', 'done', :metadata)"
            ),
            {"metadata": json.dumps(settled)},
        )
    _rollback_preflight(engine)  # superseded attempts have no publication/memory obligation
    runner.assert_schema_at_head(store.engine)
    baseline = _schema_snapshot(engine, data=True)
    cases = [
        ("status", state)
        for state in ("queued", "scheduled", "running", "interrupted", "requested", "waiting_confirmation")
    ]
    cases += [
        ("terminal_published_at", None),
        ("terminal_publish_claim", "claim"),
        ("terminal_publish_after", "2999-01-01"),
    ]
    cases += [
        ("metadata_json", json.dumps(metadata))
        for metadata in (
            {**settled, "terminal_delivery": {}},
            {**settled, "terminal_memory_done": False},
            {**settled, "terminal_memory_done": False, "terminal_memory_after": "2999-01-01"},
            {**settled, "terminal_attachments": [{"id": "pending"}]},
            {
                **settled,
                "terminal_delivery": {**settled["terminal_delivery"], "sent": "retryable"},
            },
        )
    ]
    for column, value in cases:
        with engine.begin() as conn:
            previous = conn.execute(text(f"SELECT {column} FROM tasks WHERE task_id='winner'")).scalar_one()
            conn.execute(text(f"UPDATE tasks SET {column}=:value WHERE task_id='winner'"), {"value": value})
        blocked = _schema_snapshot(engine, data=True)
        with pytest.raises(RuntimeError, match="binary rollback"):
            _rollback_preflight(engine)
        with pytest.raises(RuntimeError, match="binary rollback"):
            runner.assert_schema_at_head(store.engine)
        assert _schema_snapshot(engine, data=True) == blocked, (column, value)
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE tasks SET {column}=:value WHERE task_id='winner'"), {"value": previous})
    settings.OUROBOROS_DURABLE_JOBS_ENABLED = True
    with pytest.raises(RuntimeError, match="binary rollback"):
        _rollback_preflight(engine)
    settings.OUROBOROS_DURABLE_JOBS_ENABLED = False
    assert _schema_snapshot(engine, data=True) == baseline
    for job_id, status in (("job", "active"), ("job", "evicted"), ("orphan", "evicted")):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO scratchpad_blocks (owner_id, agent_id, job_id, ts, source, content, status) "
                    "VALUES ('owner', 'agent', :job, 't', 'task', 'job note', :status)"
                ),
                {"job": job_id, "status": status},
            )
        blocked = _schema_snapshot(engine, data=True)
        with pytest.raises(RuntimeError, match="binary rollback"):
            _rollback_preflight(engine)
        assert _schema_snapshot(engine, data=True) == blocked
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO scratchpad_blocks (owner_id, agent_id, ts, source, content, status) "
                "VALUES ('owner', 'agent', 't', 'task', 'main note', 'active')"
            )
        )
    assert (
        SqlWorkingRetrievalRepository(
            store.engine, scratchpad=SqlScratchpad(store.engine)
        ).cleanup_expired_working_retrieval(now_iso="", cleanup_retrieval=False)["scratchpad_blocks"]
        == 3
    )
    drained = _schema_snapshot(engine, data=True)
    _rollback_preflight(engine)
    runner.assert_schema_at_head(store.engine)
    assert _schema_snapshot(engine, data=True) == drained
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT content FROM scratchpad_blocks WHERE owner_id='owner' AND agent_id='agent' AND status='active'"
            )
        ).scalars().all() == ["main note"]
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0034"


def test_0014_downgrade_refuses_before_any_write_and_repeated_attempts_preserve_everything(rollout_engines):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = rollout_engines[0]
    # Stopped here rather than at head: the revisions above come down cleanly, so a run started
    # above them would have written before this revision ever refused.
    _upgrade_to(engine, "0014")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, job_id, owner_id, agent_id, detached, status, created_at, payload_json) "
                "VALUES ('job', 'job', 'owner', 'agent', true, 'running', 't', '{}')"
            )
        )
        for job in ("", "job"):
            conn.execute(
                text(
                    "INSERT INTO scratchpad_blocks (owner_id, agent_id, job_id, ts, source, content, status) "
                    "VALUES ('owner', 'agent', :job, 't', 'task', 'keep me', 'active')"
                ),
                {"job": job},
            )
    # 0014 is the floor, not the head: the revisions above it unwind on their own first,
    # and only from 0014 does "refuses before any write" describe one statement.
    _downgrade_to(engine, "0014")
    before = _schema_snapshot(engine, data=True)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cursor, sql, *a: statements.append(sql))
    for _ in range(2):
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="0014.*unsupported"):
                _migration_module("0014_durable_background_jobs").downgrade()
        with pytest.raises(RuntimeError, match="0014.*unsupported"):
            _downgrade_to(engine, "base")
        assert not any(
            sql.lstrip().split()[0].upper()
            in {"CREATE", "DROP", "ALTER", "INSERT", "UPDATE", "DELETE", "REPLACE", "TRUNCATE"}
            for sql in statements
        )
        assert _schema_snapshot(engine, data=True) == before


def test_a_parked_run_refuses_binary_rollback_and_a2a_waiting_does_not(rollout_engines):
    """The image being rolled back to cannot resume a parked run, and nothing else would wake it."""
    engine = rollout_engines[0]
    upgrade_head(engine)
    waiting = (
        "INSERT INTO tasks (task_id, session_id, owner_id, agent_id, source, status, task_type, chat_id, "
        "input_json, payload_json, metadata_json, priority, attempt, worker_id, created_at) VALUES "
        "(:task_id, '', 'global', 'main', 'dialogs', 'waiting_confirmation', 'task', '1', '{}', '{}', '{}', "
        "1, 1, 0, 'then')"
    )
    with engine.begin() as conn:
        conn.execute(text(waiting), {"task_id": "a2a-input"})

    _rollback_preflight(engine)  # a2a writes this status for its own input_required and parks nothing

    with engine.begin() as conn:
        conn.execute(text(waiting), {"task_id": "parked"})
        conn.execute(
            text(
                "INSERT INTO tool_confirmations (confirmation_id, owner_id, agent_id, surface_id, chat_id, "
                "task_id, tool_name, run_state_json, status, created_at) VALUES "
                "('c-parked', 'global', 'main', 'dialogs', '1', 'parked', 'data_write', "
                "'{\"round_idx\": 1}', 'pending', 'then')"
            )
        )

    with pytest.raises(RuntimeError, match="binary rollback"):
        _rollback_preflight(engine)


def test_0017_indexes_the_open_request_per_scope_and_the_rows_a_round_reads(rollout_engines):
    """One open request per scope, and the decisions and snapshot a resumed round reads by task."""
    engine = rollout_engines[0]
    upgrade_head(engine)

    indexes = {index["name"]: index for index in inspect(engine).get_indexes("tool_confirmations")}

    assert indexes["uq_tool_confirmations_open"]["unique"]
    assert indexes["uq_tool_confirmations_open"]["column_names"] == ["owner_id", "agent_id", "surface_id", "chat_id"]
    assert indexes["idx_tool_confirmations_task"]["column_names"] == ["task_id", "round_idx"]


def test_0017_takes_its_table_back_down(rollout_engines):
    engine = rollout_engines[0]
    _upgrade_to(engine, "0018")

    _downgrade_to(engine, "0016")

    assert "tool_confirmations" not in inspect(engine).get_table_names()


def test_0012_renames_the_agent_in_a_stored_identity(blank_schema) -> None:
    """A deployment seeded before the rename keeps its document and stops calling itself Ouroboros.

    The seed is written once and never replaced, so without this step every running
    deployment keeps the old name however the prompt reads.
    """
    engine = blank_schema()
    _upgrade_to(engine, "0011")

    insert = text(
        "INSERT INTO memory (owner_id, agent_id, kind, name, content) VALUES (:owner, 'main', :kind, :name, :content)"
    )
    seeded = "# Who I Am\n\nI am Ouroboros.\n\nI speak plainly.\n"
    edited = "# Who I Am\n\nI am Ouroboros.\n\nI answer the duty desk first.\n"
    with engine.begin() as conn:
        conn.execute(insert, {"owner": "global", "kind": "identity", "name": "identity", "content": seeded})
        conn.execute(insert, {"owner": "other", "kind": "identity", "name": "identity", "content": edited})
        conn.execute(
            insert,
            {"owner": "global", "kind": "knowledge", "name": "history", "content": "I am Ouroboros. was the old name"},
        )

    _upgrade_to(engine, "0012")

    with engine.connect() as conn:
        identities = dict(conn.execute(text("SELECT owner_id, content FROM memory WHERE kind='identity'")).all())
        knowledge = conn.execute(text("SELECT content FROM memory WHERE kind='knowledge'")).scalar_one()

    assert identities["global"] == "# Who I Am\n\nI am ГигаАгент.\n\nI speak plainly.\n"
    # A hand-edited document keeps its edit: only the sentence that names the agent moves.
    assert identities["other"] == "# Who I Am\n\nI am ГигаАгент.\n\nI answer the duty desk first.\n"
    assert knowledge == "I am Ouroboros. was the old name"

    _downgrade_to(engine, "0011")

    with engine.connect() as conn:
        restored = dict(conn.execute(text("SELECT owner_id, content FROM memory WHERE kind='identity'")).all())
    assert restored == {"global": seeded, "other": edited}


# -- 0016: history is keyed by the conversation --------------------------------


_MESSAGE_INSERT_0014 = text(
    "INSERT INTO messages (message_id, dedup_key, owner_id, agent_id, session_id, source, "
    "source_message_id, actor_type, channel, content, content_ref, content_hash, created_at, "
    "metadata_json, payload_json, chat_history_visible) VALUES (:message_id, :message_id, "
    ":owner_id, 'main', 's1', 'browser', '', :actor_type, 'chat', :content, '', '', "
    "'2026-01-01T00:00:00+00:00', :metadata_json, '{}', true)"
)


def _insert_0014_messages(conn, rows: list[dict]) -> None:
    conn.execute(
        _MESSAGE_INSERT_0014,
        [{"owner_id": "global", "actor_type": "owner", "content": row["message_id"], **row} for row in rows],
    )


def _store_for(engine):

    return make_sql_runtime_persistence(engine, db_url="")


def test_0016_keys_both_dialogue_shapes_by_the_conversation(blank_schema) -> None:
    engine = blank_schema()
    upgrade_head(engine)

    with engine.connect() as conn:
        message_columns = column_names(conn, "messages")
        summary_columns = column_names(conn, "context_summaries")
        summary_uniques = unique_constraint_names(conn, "context_summaries")
        summary_unique_columns = unique_constraint_columns(conn, "context_summaries")
        indexes = {
            name: index_columns(conn, "messages", name)
            for name in (
                "idx_messages_owner_agent_chat_visible_created",
                "idx_messages_owner_agent_chat_created",
            )
        }

    assert "chat_id" in message_columns
    assert "chat_id" in summary_columns
    assert "uq_context_summaries_owner_agent_chat" in summary_uniques
    assert frozenset({"owner_id", "agent_id", "chat_id"}) in summary_unique_columns
    assert "uq_context_summaries_owner_agent" not in summary_uniques
    assert indexes["idx_messages_owner_agent_chat_created"] == ["owner_id", "agent_id", "chat_id", "created_at"]
    assert indexes["idx_messages_owner_agent_chat_visible_created"] == [
        "owner_id",
        "agent_id",
        "chat_id",
        "chat_history_visible",
        "created_at",
    ]


def test_0016_recovers_a_readable_conversation_and_parks_what_names_none(blank_schema) -> None:
    """A row whose metadata names a chat keeps it; one that names none is parked, not deleted."""
    from tests.helpers.shared import TEST_SCOPE, scope_with

    engine = blank_schema()
    _upgrade_to(engine, "0014")
    with engine.begin() as conn:
        _insert_0014_messages(
            conn,
            [
                {"message_id": "named", "content": "in a real chat", "metadata_json": json.dumps({"chat_id": 77})},
                {
                    "message_id": "parked",
                    "content": "no conversation at all",
                    "metadata_json": json.dumps({"user_id": 1}),
                },
            ],
        )

    upgrade_head(engine)

    store = _store_for(engine)
    named_scope = scope_with(TEST_SCOPE, chat_id="77")
    assert [m.content for m in sql_conversation_repository(store.engine).list_messages(scope=named_scope)] == [
        "in a real chat"
    ]
    with engine.connect() as conn:
        chats = dict(conn.execute(text("SELECT message_id, chat_id FROM messages")).all())
    assert chats == {"named": "77", "parked": ""}

    # A parked row matches no minted scope, because no scope can carry an empty chat.
    with pytest.raises(Exception):
        scope_with(TEST_SCOPE, chat_id="")


def test_0016_aborts_before_any_ddl_when_a_message_blob_cannot_be_decrypted(blank_schema) -> None:
    """The one place the revision refuses to guess: an unreadable blob is a key problem, not data."""
    import base64

    from ouroboros.storage.message_field_crypto import reset_encryption_cache_for_tests

    engine = blank_schema()
    _upgrade_to(engine, "0014")
    with engine.begin() as conn:
        _insert_0014_messages(
            conn,
            [
                {
                    "message_id": "unreadable",
                    "content": "written under a key this deployment no longer has",
                    "metadata_json": "enc:v1:" + base64.b64encode(b"not a valid ciphertext").decode("ascii"),
                }
            ],
        )

    reset_encryption_cache_for_tests()
    try:
        with pytest.raises(Exception):
            upgrade_head(engine)
    finally:
        reset_encryption_cache_for_tests()

    # Before any ALTER: the columns the revision adds are absent, so a retry starts clean.
    with engine.connect() as conn:
        message_columns = column_names(conn, "messages")
        summary_columns = column_names(conn, "context_summaries")
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert "chat_id" not in message_columns
    assert "chat_id" not in summary_columns
    assert "inbound_unprocessed" not in message_columns
    assert version != "0016"


def test_0016_refuses_a_schema_it_does_not_recognise(blank_schema) -> None:
    engine = blank_schema()
    _upgrade_to(engine, "0014")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE messages ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''"))

    with pytest.raises(Exception, match="0016 preflight"):
        upgrade_head(engine)

    with engine.connect() as conn:
        summary_columns = column_names(conn, "context_summaries")
    assert "chat_id" not in summary_columns


def test_0016_downgrade_refuses_when_a_pair_holds_two_summaries(blank_schema) -> None:
    """``(owner_id, agent_id)`` cannot readmit them, and no merge would be truthful."""
    engine = blank_schema()
    _upgrade_to(engine, "0018")
    insert = text(
        "INSERT INTO context_summaries (summary_id, owner_id, agent_id, chat_id, created_at, payload_json) "
        "VALUES (:summary_id, 'global', 'main', :chat_id, '2026-01-01T00:00:00+00:00', '{}')"
    )
    with engine.begin() as conn:
        conn.execute(insert, [{"summary_id": "s1", "chat_id": "a"}, {"summary_id": "s2", "chat_id": "b"}])

    with pytest.raises(Exception, match="global/main"):
        _downgrade_to(engine, "0014")

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM context_summaries WHERE summary_id='s2'"))
    _downgrade_to(engine, "0014")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0014"
        assert "chat_id" not in column_names(conn, "messages")


def test_every_conflict_target_matches_a_unique_constraint_of_the_head_schema(blank_schema) -> None:
    """Text-built conflict targets must name a unique constraint in the head schema."""
    import re

    engine = blank_schema()
    upgrade_head(engine)

    unique_sets: dict[str, list[frozenset[str]]] = {}
    with engine.connect() as conn:
        tables = sorted(table_names(conn))
        for table in tables:
            primary_key = frozenset(primary_key_columns(conn, table))
            unique_indexes = [
                frozenset(index["column_names"] or [])
                for index in inspect(conn).get_indexes(table)
                if index.get("unique")
            ]
            unique_sets[table] = [primary_key, *unique_indexes, *unique_constraint_columns(conn, table)]

    root = pathlib.Path(__file__).resolve().parents[3]
    pattern = re.compile(r"INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+(\w+)|ON\s+CONFLICT\s*\(([^)]*)\)", re.IGNORECASE)
    checked = 0
    statement_files = (
        root / "ouroboros/storage/dialect.py",
        root / "ouroboros/storage/result_delivery/repository.py",
        root / "ouroboros/storage/tasks/repository.py",
        root / "ouroboros/storage/conversations/repository.py",
        root / "ouroboros/storage/execution_memory/repository.py",
        root / "ouroboros/storage/tool_confirmation/repository.py",
        root / "ouroboros/app/infra/repositories/deployment_keyspace/repo.py",
        root / "supervisor/infra/repositories/runtime_sessions/repo.py",
        root / "supervisor/infra/repositories/owner_state/repo.py",
        root / "supervisor/infra/repositories/cleanup_checkpoint/repo.py",
        # The consolidation lease and the committed summary are the background-memory
        # package's statements now; they are checked against the same head schema.
        root / "background_memory/infra/repositories/lease.py",
        root / "background_memory/infra/repositories/summary.py",
    )
    for path in statement_files:
        table = ""
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            if match.group(1):
                table = match.group(1)
                continue
            target = frozenset(column.strip() for column in match.group(2).split(","))
            assert table in unique_sets, f"{path.name}: ON CONFLICT before any INSERT INTO"
            assert target in unique_sets[table], (
                f"{path.name}: ON CONFLICT {sorted(target)} on {table} is not a unique constraint of the head schema"
            )
            checked += 1
    # Exact, not a floor: with a floor, half the clauses could vanish and this still passed.
    assert checked == 13, f"the tree's ON CONFLICT clause count changed ({checked}); check the new one"


def test_the_message_insert_binds_every_column_it_names() -> None:
    """The other half of that hole: a column named in ``insert_message_sql`` without a bind."""
    import re

    from ouroboros.storage.dialect import insert_message_sql

    pattern = re.compile(r"INSERT\s+INTO\s+messages\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", re.IGNORECASE)
    match = pattern.search(insert_message_sql())
    assert match is not None, "the message insert no longer reads as one INSERT ... VALUES"
    columns = [column.strip() for column in match.group(1).split(",")]
    assert [value.strip() for value in match.group(2).split(",")] == [f":{column}" for column in columns]
    assert "inbound_unprocessed" in columns


def test_0016_downgrade_survives_a_pair_that_had_a_summary_before_the_upgrade(blank_schema) -> None:
    """The parked row is this revision's own artifact, so its downgrade unwinds it.

    Left in place it becomes the second row of every pair that had one, and the collision check
    then refuses every downgrade from that pair's first real commit onward.
    """
    engine = blank_schema()
    _upgrade_to(engine, "0014")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO context_summaries (summary_id, owner_id, agent_id, created_at, payload_json) "
                "VALUES ('legacy', 'global', 'main', '2026-01-01T00:00:00+00:00', '{}')"
            )
        )

    _upgrade_to(engine, "0018")

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO context_summaries (summary_id, owner_id, agent_id, chat_id, created_at, payload_json) "
                "VALUES ('fresh', 'global', 'main', 'chat-a', '2026-01-02T00:00:00+00:00', '{}')"
            )
        )

    _downgrade_to(engine, "0014")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0014"
        assert [row[0] for row in conn.execute(text("SELECT summary_id FROM context_summaries"))] == ["fresh"]


def test_0016_downgrade_keeps_a_parked_summary_that_collides_with_nothing(blank_schema) -> None:
    """Upgrade then downgrade with nothing committed in between loses no summary."""
    engine = blank_schema()
    _upgrade_to(engine, "0014")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO context_summaries (summary_id, owner_id, agent_id, source_offsets_ref, created_at, "
                "payload_json) VALUES ('legacy', 'global', 'main', 'message-9', '2026-01-01T00:00:00+00:00', '{}')"
            )
        )

    _upgrade_to(engine, "0018")
    _downgrade_to(engine, "0014")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT summary_id, source_offsets_ref FROM context_summaries")).all()
    assert rows == [("legacy", "message-9")]


def test_0016_backfills_a_table_larger_than_one_page(blank_schema) -> None:
    """The keyset loop's second iteration, and the batched write behind it."""
    engine = blank_schema()
    _upgrade_to(engine, "0014")
    total = 1500
    with engine.begin() as conn:
        _insert_0014_messages(
            conn,
            [
                {
                    "message_id": f"m-{index:05d}",
                    "content": f"row {index}",
                    "metadata_json": json.dumps({"chat_id": index % 3}),
                }
                for index in range(total)
            ],
        )

    upgrade_head(engine)

    with engine.connect() as conn:
        counted = dict(conn.execute(text("SELECT chat_id, COUNT(*) FROM messages GROUP BY chat_id")).all())
    assert counted == {"0": 500, "1": 500, "2": 500}


def test_a_metadata_update_cannot_move_a_row_between_conversations(blank_schema) -> None:
    """The one write path that rewrites ``payload_json`` wholesale must not touch the axis."""
    from tests.helpers.shared import TEST_SCOPE, scope_with

    engine = blank_schema()
    _upgrade_to(engine, "0014")
    with engine.begin() as conn:
        _insert_0014_messages(
            conn,
            [
                {
                    "message_id": "m-1",
                    "owner_id": TEST_SCOPE.owner_id,
                    "content": "stays put",
                    "metadata_json": json.dumps({"chat_id": "here"}),
                }
            ],
        )
    upgrade_head(engine)
    store = _store_for(engine)
    here = scope_with(TEST_SCOPE, chat_id="here")

    assert sql_conversation_repository(store.engine).update_message_metadata(
        "m-1", {"chat_id": "elsewhere", "inbound_processed": True}
    )

    assert [m.content for m in sql_conversation_repository(store.engine).list_messages(scope=here)] == ["stays put"]
    assert sql_conversation_repository(store.engine).get_message("m-1").chat_id == "here"
    assert (
        sql_conversation_repository(store.engine).list_messages(scope=scope_with(TEST_SCOPE, chat_id="elsewhere")) == []
    )


# -- 0016: the inbound journal marker becomes a column --------------------------


_MESSAGE_INSERT_HEAD = text(
    "INSERT INTO messages (message_id, dedup_key, owner_id, agent_id, chat_id, session_id, source, "
    "source_message_id, actor_type, channel, content, content_ref, content_hash, created_at, "
    "metadata_json, payload_json, chat_history_visible) VALUES (:message_id, :message_id, 'global', "
    "'main', :chat_id, 's1', 'browser', '', :actor_type, 'chat', :message_id, '', '', "
    "'2026-01-01T00:00:00+00:00', :metadata_json, '{}', true)"
)

_JOURNAL_REVISION = "0016_chat_keyed_history_kindless_agents_and_journal"


def _insert_head_messages(conn, rows: list[dict]) -> None:
    conn.execute(_MESSAGE_INSERT_HEAD, [{"actor_type": "owner", "chat_id": "1", **row} for row in rows])


def test_0016_marks_only_the_rows_the_journal_never_handed_over(blank_schema) -> None:
    """The column states what the Python predicate stated, for every row already stored."""
    engine = blank_schema()
    _upgrade_to(engine, "0015")
    unhandled = json.dumps({"chat_id": "1", "inbound_processed": False})
    with engine.begin() as conn:
        _insert_0014_messages(
            conn,
            [
                {"message_id": "stranded", "metadata_json": unhandled},
                {
                    "message_id": "handed-over",
                    "metadata_json": json.dumps({"chat_id": "1", "inbound_processed": True}),
                },
                {"message_id": "never-a-journal-row", "metadata_json": json.dumps({"chat_id": "1"})},
                {"message_id": "unreadable", "metadata_json": "{not json"},
                {"message_id": "an-agents-own-turn", "actor_type": "agent", "metadata_json": unhandled},
                {
                    "message_id": "parked-by-the-key-column",
                    "metadata_json": json.dumps({"inbound_processed": False}),
                },
                {
                    "message_id": "replayed-as-history",
                    "metadata_json": json.dumps({"chat_id": "1", "inbound_processed": False, "history_only": True}),
                },
            ],
        )

    upgrade_head(engine)

    with engine.connect() as conn:
        marked = {
            str(row[0]) for row in conn.execute(text("SELECT message_id FROM messages WHERE inbound_unprocessed"))
        }
        index = index_columns(conn, "messages", "idx_messages_inbound_unprocessed")
    partial = index_definition(engine, "idx_messages_inbound_unprocessed")
    assert marked == {"stranded"}
    assert index == ["owner_id", "agent_id", "source", "created_at"]
    assert "WHERE inbound_unprocessed" in partial


def test_0016_leaves_a_schema_the_downgrade_puts_back(blank_schema) -> None:
    """Both derived columns come from metadata every row retains, so the reversal loses nothing."""
    engine = blank_schema()
    _upgrade_to(engine, "0018")
    with engine.begin() as conn:
        _insert_head_messages(
            conn,
            [{"message_id": "stranded", "metadata_json": json.dumps({"chat_id": "1", "inbound_processed": False})}],
        )

    _downgrade_to(engine, "0015")
    with engine.connect() as conn:
        columns = column_names(conn, "messages")
    assert {"inbound_unprocessed", "chat_id"}.isdisjoint(columns)

    _upgrade_to(engine, "0016")
    with engine.connect() as conn:
        marked = dict(conn.execute(text("SELECT message_id, chat_id FROM messages WHERE inbound_unprocessed")).all())
    assert marked == {"stranded": "1"}


def test_0016_refuses_a_schema_that_already_carries_the_column(blank_schema) -> None:
    """A partly applied revision is a schema a retry must not run over."""
    engine = blank_schema()
    upgrade_head(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num='0015'"))

    with pytest.raises(Exception, match="0016 preflight"):
        upgrade_head(engine)


def test_0016_reports_a_backfill_that_marked_nothing(blank_schema, caplog) -> None:
    """Examined nothing and found nothing are the same silence unless the empty pass says so."""
    engine = blank_schema()
    _upgrade_to(engine, "0015")
    with engine.begin() as conn:
        _insert_0014_messages(
            conn,
            [
                {
                    "message_id": "handed-over",
                    "metadata_json": json.dumps({"chat_id": "1", "inbound_processed": True}),
                }
            ],
        )

    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        _upgrade_to(engine, "0016")

    backfilled = [record for record in caplog.records if "0016 backfilled" in record.getMessage()]
    assert [record.levelno for record in backfilled] == [logging.INFO]
    assert "over 0 stranded" in backfilled[0].getMessage()
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING and "0016" in record.getMessage()
    ]


def test_0016_downgrade_runs_again_over_a_schema_that_already_lost_the_column(blank_schema) -> None:
    """The re-entrancy the summary refusal needs: SQLite keeps the drops, alembic retries them."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = blank_schema()
    upgrade_head(engine)

    module = _migration_module(_JOURNAL_REVISION)
    for _ in range(2):
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            module._drop_the_journal_marker()

    with engine.connect() as conn:
        assert "inbound_unprocessed" not in column_names(conn, "messages")
        indexes = index_names(conn, "messages")
    assert "idx_messages_inbound_unprocessed" not in indexes


def test_0016_backfills_a_table_larger_than_one_page_a_page_at_a_time(blank_schema) -> None:
    """The keyset read's second page, counted: the result alone cannot tell paging from one unbounded read."""
    page = _migration_module(_JOURNAL_REVISION)._PAGE
    engine = blank_schema()
    _upgrade_to(engine, "0015")
    total = page + 1
    unhandled = json.dumps({"chat_id": "1", "inbound_processed": False})
    with engine.begin() as conn:
        _insert_0014_messages(
            conn, [{"message_id": f"m-{index:05d}", "metadata_json": unhandled} for index in range(total)]
        )
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda c, cursor, sql, *a: statements.append(sql))

    _upgrade_to(engine, "0016")

    with engine.connect() as conn:
        marked = (
            conn.execute(text("SELECT message_id FROM messages WHERE inbound_unprocessed ORDER BY message_id"))
            .scalars()
            .all()
        )
    reads = [sql for sql in statements if "SELECT message_id, actor_type, metadata_json FROM messages" in sql]
    assert len(reads) > 1
    assert len(marked) == total
    assert (marked[0], marked[-1]) == ("m-00000", f"m-{total - 1:05d}")


_SCHEDULE_INSERT_0015 = text(
    "INSERT INTO schedules (schedule_id, owner_id, agent_id, revision, state, next_run_at, payload_json, "
    "created_at, updated_at) VALUES (:schedule_id, 'global', 'main', 1, :state, :next_run_at, :payload_json, "
    "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
)


def _stored_schedule(chat_id: str | None) -> str:
    scope = {"owner_id": "global", "agent_id": "main", "surface_id": "dialogs"}
    if chat_id is not None:
        scope["chat_id"] = chat_id
    return json.dumps({"id": "s-1", "what": "ping", "recurrence": "daily", "delivery": {"scope": scope}})


def _insert_0015_schedules(conn, rows: list[dict]) -> None:
    conn.execute(
        _SCHEDULE_INSERT_0015,
        [{"state": "active", "next_run_at": "2026-06-01T10:00:00+00:00", **row} for row in rows],
    )


def test_0016_takes_each_schedules_conversation_from_the_delivery_it_already_stored(blank_schema) -> None:
    engine = blank_schema()
    _upgrade_to(engine, "0015")
    with engine.begin() as conn:
        _insert_0015_schedules(
            conn,
            [
                {"schedule_id": "keyed", "payload_json": _stored_schedule("77")},
                {"schedule_id": "peer", "payload_json": _stored_schedule("peer-42")},
            ],
        )

    upgrade_head(engine)

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT schedule_id, chat_id FROM schedules")).all())
        states = dict(conn.execute(text("SELECT schedule_id, state FROM schedules")).all())
        keyed_index = index_columns(conn, "schedules", "idx_schedules_owner_chat")
        present = index_names(conn, "schedules")
    assert rows == {"keyed": "77", "peer": "peer-42"}
    assert states == {"keyed": "active", "peer": "active"}
    assert keyed_index == ["owner_id", "agent_id", "chat_id", "state"]
    assert "idx_schedules_owner" not in present


def test_0016_parks_a_schedule_whose_payload_names_no_delivery_scope(blank_schema) -> None:
    engine = blank_schema()
    _upgrade_to(engine, "0015")
    with engine.begin() as conn:
        _insert_0015_schedules(
            conn,
            [
                {"schedule_id": "scopeless", "payload_json": _stored_schedule(None)},
                {"schedule_id": "unparseable", "payload_json": "not json at all"},
                {"schedule_id": "keyed", "payload_json": _stored_schedule("77")},
            ],
        )

    upgrade_head(engine)

    with engine.connect() as conn:
        parked = conn.execute(
            text("SELECT schedule_id, chat_id, state, next_run_at FROM schedules ORDER BY schedule_id")
        ).all()
    assert parked == [
        ("keyed", "77", "active", "2026-06-01T10:00:00+00:00"),
        ("scopeless", "", "needs_attention", ""),
        ("unparseable", "", "needs_attention", ""),
    ]


def test_0016_aborts_before_any_ddl_when_a_schedule_blob_cannot_be_decrypted(blank_schema) -> None:
    import base64

    from ouroboros.storage.message_field_crypto import reset_encryption_cache_for_tests

    engine = blank_schema()
    _upgrade_to(engine, "0015")
    with engine.begin() as conn:
        _insert_0015_schedules(
            conn,
            [
                {
                    "schedule_id": "unreadable",
                    "payload_json": "enc:v1:" + base64.b64encode(b"not a valid ciphertext").decode("ascii"),
                }
            ],
        )

    reset_encryption_cache_for_tests()
    try:
        with pytest.raises(Exception):
            upgrade_head(engine)
    finally:
        reset_encryption_cache_for_tests()

    with engine.connect() as conn:
        schedule_columns = column_names(conn, "schedules")
        message_columns = column_names(conn, "messages")
        indexes = index_names(conn, "schedules")
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert "chat_id" not in schedule_columns
    assert "chat_id" not in message_columns
    assert "idx_schedules_owner" in indexes
    assert version == "0015"


def test_0016_refuses_a_schema_whose_schedules_already_carry_the_column(blank_schema) -> None:
    engine = blank_schema()
    _upgrade_to(engine, "0015")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE schedules ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''"))

    with pytest.raises(Exception, match="0016 preflight"):
        upgrade_head(engine)

    with engine.connect() as conn:
        message_columns = column_names(conn, "messages")
        indexes = index_names(conn, "schedules")
    assert "chat_id" not in message_columns
    assert "idx_schedules_owner" in indexes


def test_0016_downgrade_gives_the_schedules_their_0015_index_back(blank_schema) -> None:
    engine = blank_schema()
    _upgrade_to(engine, "0018")

    _downgrade_to(engine, "0015")

    with engine.connect() as conn:
        columns = column_names(conn, "schedules")
        indexes = index_names(conn, "schedules")
        owner_index = index_columns(conn, "schedules", "idx_schedules_owner")
    assert "chat_id" not in columns
    assert "idx_schedules_owner_chat" not in indexes
    assert owner_index == ["owner_id", "agent_id", "state"]


def test_0016_reports_the_schedule_rows_it_keyed_and_the_ones_it_parked(blank_schema, caplog) -> None:
    engine = blank_schema()
    _upgrade_to(engine, "0015")
    with engine.begin() as conn:
        _insert_0015_schedules(
            conn,
            [
                {"schedule_id": "keyed", "payload_json": _stored_schedule("77")},
                {"schedule_id": "scopeless", "payload_json": _stored_schedule(None)},
            ],
        )

    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        upgrade_head(engine)

    reported = [record for record in caplog.records if "schedule row(s)" in record.getMessage()]
    messages = [record.getMessage() for record in reported]
    assert "0016 gave 1 schedule row(s) their conversation" in messages
    assert "0016 parked 1 schedule row(s)" in messages
    assert any(record.levelno >= logging.WARNING for record in reported)


def test_0016_backfills_the_schedules_a_page_at_a_time(blank_schema) -> None:
    page = _migration_module(_JOURNAL_REVISION)._PAGE
    engine = blank_schema()
    _upgrade_to(engine, "0015")
    total = page + 1
    with engine.begin() as conn:
        _insert_0015_schedules(
            conn,
            [{"schedule_id": f"s-{index:05d}", "payload_json": _stored_schedule("77")} for index in range(total)],
        )
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda c, cursor, sql, *a: statements.append(sql))

    _upgrade_to(engine, "0016")

    with engine.connect() as conn:
        keyed = conn.execute(text("SELECT COUNT(*) FROM schedules WHERE chat_id='77'")).scalar_one()
    reads = [sql for sql in statements if "SELECT schedule_id, payload_json FROM schedules" in sql]
    assert len(reads) > 1
    assert keyed == total


_0018_TABLES = {"security_contexts", "mcp_user_tokens"}


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        return table_names(conn)


def test_0018_creates_both_tables_and_drops_both_again(blank_schema) -> None:
    engine = blank_schema()
    _upgrade_to(engine, "0018")

    with engine.connect() as conn:
        columns = column_names(conn, "mcp_user_tokens")
    assert columns == {"id", "mcp_server_id", "agent_id", "owner_id", "token", "expires_at"}

    insert = text(
        "INSERT INTO mcp_user_tokens (id, mcp_server_id, agent_id, owner_id, token, expires_at) "
        "VALUES (:id, 'hr', 'main', 'alice', 'sealed', '2999-01-01T00:00:00.000000+00:00')"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"id": "first"})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert, {"id": "second"})

    _downgrade_to(engine, "0017")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0017"
    assert _0018_TABLES.isdisjoint(_tables(engine))


@pytest.mark.parametrize("table", sorted(_0018_TABLES))
def test_0018_refuses_a_schema_that_already_holds_either_table(blank_schema, table: str) -> None:
    engine = blank_schema()
    _upgrade_to(engine, "0017")
    with engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE "{table}" (id TEXT)'))

    with pytest.raises(RuntimeError, match=f"0018 preflight: {table} already exists"):
        _upgrade_to(engine, "0018")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0017"
    assert _0018_TABLES & _tables(engine) == {table}


@pytest.mark.parametrize("table", sorted(_0018_TABLES))
def test_0018_downgrade_drops_the_table_that_remains_when_the_other_is_gone(blank_schema, table: str) -> None:
    engine = blank_schema()
    _upgrade_to(engine, "0018")
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE "{table}"'))

    _downgrade_to(engine, "0017")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0017"
    assert _0018_TABLES.isdisjoint(_tables(engine))


_A2A_USER_TOKEN_COLUMNS = {"id", "a2a_agent_id", "agent_id", "owner_id", "token", "expires_at"}
_INSERT_MCP_USER_TOKEN = text(
    "INSERT INTO mcp_user_tokens (id, mcp_server_id, agent_id, owner_id, token, expires_at) "
    "VALUES ('mcp-row', 'hr', 'main', 'alice', 'sealed-mcp', '2999-01-01T00:00:00.000000+00:00')"
)
_INSERT_A2A_USER_TOKEN = text(
    "INSERT INTO a2a_user_tokens (id, a2a_agent_id, agent_id, owner_id, token, expires_at) "
    "VALUES (:id, 'hr', 'main', 'alice', 'sealed-a2a', '2999-01-01T00:00:00.000000+00:00')"
)


def _mcp_user_tokens(engine) -> tuple[list, list]:
    columns = [
        (column["name"], str(column["type"]), column["nullable"])
        for column in inspect(engine).get_columns("mcp_user_tokens")
    ]
    with engine.connect() as conn:
        rows = [tuple(row) for row in conn.execute(text("SELECT * FROM mcp_user_tokens ORDER BY id"))]
    return columns, rows


def _version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _owner_agent_indexes(engine, table: str) -> dict[str, list[str]]:
    inspector = inspect(engine)
    if table not in set(inspector.get_table_names()):
        return {}
    return {
        index["name"]: index["column_names"]
        for index in inspector.get_indexes(table)
        if index["name"] == f"idx_{table}_owner_agent"
    }


def test_0019_creates_a2a_user_tokens(rollout_engines) -> None:
    engine = rollout_engines[0]
    _upgrade_to(engine, "0018")
    with engine.begin() as conn:
        conn.execute(_INSERT_MCP_USER_TOKEN)
    mcp_before = _mcp_user_tokens(engine)

    _upgrade_to(engine, "0019")

    inspector = inspect(engine)
    columns = inspector.get_columns("a2a_user_tokens")
    assert {column["name"] for column in columns} == _A2A_USER_TOKEN_COLUMNS
    assert all(not column["nullable"] and str(column["type"]).upper() == "TEXT" for column in columns)
    primary_key = inspector.get_pk_constraint("a2a_user_tokens")
    assert (primary_key["name"], primary_key["constrained_columns"]) == ("pk_a2a_user_tokens", ["id"])
    assert {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("a2a_user_tokens")
    } == {"uq_a2a_user_tokens_remote_owner_agent": ["a2a_agent_id", "owner_id", "agent_id"]}
    assert _owner_agent_indexes(engine, "a2a_user_tokens") == {
        "idx_a2a_user_tokens_owner_agent": ["owner_id", "agent_id"]
    }
    assert _owner_agent_indexes(engine, "mcp_user_tokens") == {}
    assert _mcp_user_tokens(engine) == mcp_before
    assert _version(engine) == "0019"
    with engine.begin() as conn:
        conn.execute(_INSERT_A2A_USER_TOKEN, {"id": "first"})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(_INSERT_A2A_USER_TOKEN, {"id": "second"})


def test_0019_preflight_refuses_a_partly_run_revision(rollout_engines) -> None:
    engine = rollout_engines[0]
    _upgrade_to(engine, "0018")
    with engine.begin() as conn:
        conn.execute(_INSERT_MCP_USER_TOKEN)
        conn.execute(text("CREATE TABLE a2a_user_tokens (id TEXT)"))
    before = _schema_snapshot(engine, data=True)

    with pytest.raises(RuntimeError, match="0019 preflight: a2a_user_tokens already exists"):
        _upgrade_to(engine, "0019")

    assert _version(engine) == "0018"
    assert _schema_snapshot(engine, data=True) == before


def test_0019_downgrade_drops_only_its_table(rollout_engines) -> None:
    engine = rollout_engines[0]
    _upgrade_to(engine, "0019")
    with engine.begin() as conn:
        conn.execute(_INSERT_MCP_USER_TOKEN)
        conn.execute(_INSERT_A2A_USER_TOKEN, {"id": "a2a-row"})
    mcp_before = _mcp_user_tokens(engine)

    _downgrade_to(engine, "0018")

    assert _version(engine) == "0018"
    assert "a2a_user_tokens" not in inspect(engine).get_table_names()
    assert _owner_agent_indexes(engine, "a2a_user_tokens") == {}
    assert _mcp_user_tokens(engine) == mcp_before
    assert mcp_before[1] != []
