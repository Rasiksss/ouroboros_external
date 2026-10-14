from __future__ import annotations

import json

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from infra.migrations.runner import alembic_config, upgrade_head
from tests.helpers.migrations import upgrade_to
from tests.helpers.revision_schema import column_names, schema_version, table_names
from transport.a2a.executor import derive_a2a_chat_id

_INSERT = text(
    "INSERT INTO chats (owner_id, agent_id, chat_id, title, title_source, title_attempts, is_active, created_at) "
    "VALUES (:owner_id, :agent_id, :chat_id, '', :title_source, 0, true, '2026-09-01T00:00:00+00:00')"
)
#: The shape `0013` leaves behind — no `chat_id` column yet, so the conversation is where the
#: revision's own backfill has to find it. Seeding here rather than mid-revision is what the
#: merge forces and what makes these tests exercise the whole chain.
_MESSAGE_0013 = text(
    "INSERT INTO messages (message_id, dedup_key, owner_id, agent_id, session_id, source, "
    "source_message_id, actor_type, channel, content, content_ref, content_hash, created_at, metadata_json, "
    "payload_json, chat_history_visible) VALUES (:message_id, :message_id, :owner_id, :agent_id, "
    "'', :source, '', 'owner', 'chat', 'hi', '', '', '2026-01-01T00:00:00+00:00', :metadata_json, '{}', true)"
)


def _said(message_id: str, owner_id: str, chat_id: str, source: str, agent_id: str = "main") -> dict[str, str]:
    return {
        "message_id": message_id,
        "owner_id": owner_id,
        "agent_id": agent_id,
        "source": source,
        "metadata_json": json.dumps({"chat_id": chat_id} if chat_id else {}),
    }


def _seeded_at_0013(blank_schema, rows: list[dict[str, str]]):
    engine = blank_schema()
    config = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0013")
    config.attributes.pop("connection", None)
    with engine.begin() as conn:
        conn.execute(_MESSAGE_0013, rows)
    return engine


def _engine(blank_schema):
    engine = blank_schema()
    upgrade_head(engine)
    return engine


def _insert(engine, **row) -> None:
    with engine.begin() as conn:
        conn.execute(_INSERT, {"title_source": "provisional", **row})


def test_the_migration_is_reachable_from_head(blank_schema) -> None:
    engine = _engine(blank_schema)
    assert column_names(engine, "chats") == {
        "owner_id",
        "agent_id",
        "chat_id",
        "title",
        "title_source",
        "title_attempts",
        "is_active",
        "created_at",
    }


def test_a_chat_id_is_unique_within_its_owner_and_agent_and_free_across_them(blank_schema) -> None:
    engine = _engine(blank_schema)
    _insert(engine, owner_id="alice", agent_id="main", chat_id="c1")
    _insert(engine, owner_id="bob", agent_id="main", chat_id="c1")
    _insert(engine, owner_id="alice", agent_id="second", chat_id="c1")
    with pytest.raises(IntegrityError):
        _insert(engine, owner_id="alice", agent_id="main", chat_id="c1")


def test_the_title_origin_is_closed_to_its_three_values(blank_schema) -> None:
    """The check is what makes "the model never overwrites a rename" enforceable in a WHERE."""
    engine = _engine(blank_schema)
    for source in ("provisional", "model", "owner"):
        _insert(engine, owner_id="alice", agent_id="main", chat_id=source, title_source=source)
    with pytest.raises(IntegrityError):
        _insert(engine, owner_id="alice", agent_id="main", chat_id="c9", title_source="guessed")


def test_the_empty_chat_axis_cannot_become_a_record(blank_schema) -> None:
    """`0008` parks legacy rows on an empty chat; those are not conversations."""
    engine = _engine(blank_schema)
    with pytest.raises(IntegrityError):
        _insert(engine, owner_id="alice", agent_id="main", chat_id="")


def test_the_backfill_takes_ids_only_and_skips_the_parked_rows(blank_schema) -> None:
    """A title could only come from ciphertext or from a model, and a revision calls neither."""
    engine = _seeded_at_0013(
        blank_schema,
        [
            _said("m1", "alice", "c1", "browser"),
            _said("m2", "alice", "c1", "browser"),
            _said("m3", "bob", "c1", "browser"),
            _said("m4", "alice", "", "browser"),
            _said("m5", "alice", "c2", "web"),
        ],
    )

    upgrade_head(engine)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT owner_id, agent_id, chat_id, title, title_source FROM chats")).all()
    assert sorted(rows) == [
        ("alice", "main", "c1", "", "provisional"),
        # `web` is the spelling `0001` allowed for the same surface, so it is a browser conversation.
        ("alice", "main", "c2", "", "provisional"),
        ("bob", "main", "c1", "", "provisional"),
    ]


def test_the_backfill_leaves_every_other_surfaces_conversations_out(blank_schema) -> None:
    """The registry is the browser's: a dialogs peer or an A2A id here is listed and unaddressable."""
    a2a_message = _said("a1", "alice", str(derive_a2a_chat_id("task-a1")), "a2a")
    a2a_message["metadata_json"] = json.dumps(
        {
            **json.loads(a2a_message["metadata_json"]),
            "task_id": "task-a1",
            "transport": {"a2a_context_id": "context-a1"},
        }
    )
    engine = _seeded_at_0013(
        blank_schema,
        [
            _said("b1", "alice", "c1", "browser"),
            _said("d1", "alice", "4242", "dialogs"),
            a2a_message,
            _said("s1", "alice", "-1", "skill-bridge"),
        ],
    )

    upgrade_head(engine)

    with engine.connect() as conn:
        recorded = {row[0] for row in conn.execute(text("SELECT chat_id FROM chats"))}
    assert recorded == {"c1"}


def test_the_downgrade_takes_the_record_and_leaves_the_tables_it_stood_beside(blank_schema) -> None:
    """One revision, so one downgrade: the record goes with the column it was derived from."""
    engine = blank_schema()
    upgrade_to(engine, "0018")
    config = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0014")
    config.attributes.pop("connection", None)

    tables = table_names(engine)
    assert schema_version(engine) == "0014"
    assert "chats" not in tables
    assert "chat_id" not in column_names(engine, "messages")
    assert {"messages", "context_summaries", "agents", "user_settings"} <= tables
