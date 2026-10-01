"""Tests for optional AES-256-GCM chat message field encryption."""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import create_engine, text

from ouroboros.storage.message_field_crypto import (
    decrypt_field,
    encrypt_field,
    encryption_active_for_dialect,
    reset_encryption_cache_for_tests,
)
from ouroboros.storage.runtime_models import ContextSummary, ConversationMessage, TaskRecord
from ouroboros.storage.sql_runtime_store import SqlRuntimeStore


def _test_key_b64() -> str:
    return base64.b64encode(b"k" * 32).decode("ascii")


@pytest.fixture(autouse=True)
def _clear_encryption_cache():
    reset_encryption_cache_for_tests()
    yield
    reset_encryption_cache_for_tests()


@pytest.fixture
def encryption_env(monkeypatch):
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", _test_key_b64())
    reset_encryption_cache_for_tests()


def test_encrypt_decrypt_round_trip_postgres(encryption_env):
    plaintext = "owner secret message"
    encrypted = encrypt_field(plaintext, dialect_name="postgresql")
    assert encrypted.startswith("enc:v1:")
    assert encrypted != plaintext
    assert decrypt_field(encrypted, dialect_name="postgresql") == plaintext


def test_plaintext_passthrough_when_flag_disabled(monkeypatch):
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    plaintext = "legacy row"
    assert encrypt_field(plaintext, dialect_name="postgresql") == plaintext
    assert decrypt_field(plaintext, dialect_name="postgresql") == plaintext
    assert encryption_active_for_dialect("postgresql") is False


def test_sqlite_dialect_never_encrypts(encryption_env):
    plaintext = "dev sqlite chat"
    assert encrypt_field(plaintext, dialect_name="sqlite") == plaintext
    assert encryption_active_for_dialect("sqlite") is False


def test_legacy_plaintext_row_readable(encryption_env):
    legacy = '{"content": "hello"}'
    assert decrypt_field(legacy, dialect_name="postgresql") == legacy


def test_enabled_without_key_warns_in_development(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "development")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    assert encryption_active_for_dialect("postgresql") is False
    assert encrypt_field("hello", dialect_name="postgresql") == "hello"


def test_enabled_without_key_fails_in_production(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    with pytest.raises(ValueError, match="OUROBOROS_CHAT_ENCRYPTION_KEY"):
        encryption_active_for_dialect("postgresql")


def test_sql_runtime_store_encrypted_round_trip(tmp_path, encryption_env):
    db_path = tmp_path / "runtime.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    store = SqlRuntimeStore(engine, db_url=f"sqlite:///{db_path}")
    store._dialect = "postgresql"

    message = ConversationMessage(
        message_id="msg-enc-1",
        conversation_id="chat:1",
        session_id="sess-1",
        source="sberchat",
        actor_type="owner",
        channel="sberchat",
        content="private owner text",
        metadata={"chat_id": 1, "user_id": 1},
    )
    store.append_message(message)

    with store._lock, store._connection() as conn:
        row = conn.execute(
            text("SELECT content, metadata_json, payload_json FROM messages WHERE message_id=:id"),
            {"id": "msg-enc-1"},
        ).fetchone()
    assert row is not None
    assert str(row.content).startswith("enc:v1:")
    assert str(row.metadata_json).startswith("enc:v1:")
    assert str(row.payload_json).startswith("enc:v1:")

    listed = store.list_messages(conversation_id="chat:1")
    assert len(listed) == 1
    assert listed[0].content == "private owner text"
    assert listed[0].metadata.get("chat_id") == 1

    store.upsert_context_summary(
        ContextSummary(summary_id="shared-chat", conversation_id="global", summary_ref="initial private summary")
    )
    with store._lock, store._connection() as conn:
        summary_row = conn.execute(
            text(
                "SELECT summary_ref, metadata_json, payload_json FROM context_summaries WHERE summary_id='shared-chat'"
            )
        ).fetchone()
    assert summary_row is not None
    assert all(str(value).startswith("enc:v1:") for value in summary_row)
    assert store.get_latest_context_summary("global").summary_ref == "initial private summary"

    lease = TaskRecord(
        task_id="ctx",
        status="running",
        task_type="context_consolidation",
        input={"expected_cursor": ""},
        worker_id=1,
        started_at="2026-08-01T00:00:00+00:00",
    )
    store.upsert_task(lease)
    assert store._commit_context_consolidation(
        lease,
        status="interrupted",
        task_input={"expected_cursor": "", "pending_summary": "partial private summary"},
        expected_cursor="",
        summary=ContextSummary(
            summary_id="shared-chat",
            conversation_id="global",
            source_offsets_ref="message-1",
            summary_ref="complete private summary",
        ),
    )
    with store._lock, store._connection() as conn:
        summary_row = conn.execute(
            text(
                "SELECT summary_ref, metadata_json, payload_json FROM context_summaries WHERE summary_id='shared-chat'"
            )
        ).fetchone()
        task_row = conn.execute(text("SELECT input_json, payload_json FROM tasks WHERE task_id='ctx'")).fetchone()
    assert all(str(value).startswith("enc:v1:") for value in (*summary_row, *task_row))
    assert store.get_latest_context_summary("global").summary_ref == "complete private summary"
    assert store.get_task("ctx").input["pending_summary"] == "partial private summary"
    reclaimed = store.get_task("ctx").model_copy(
        update={"status": "running", "worker_id": 1, "started_at": "2026-08-01T00:01:00+00:00"}
    )
    store.upsert_task(reclaimed)
    with store._lock, store._connection() as conn:
        task_row = conn.execute(text("SELECT input_json, payload_json FROM tasks WHERE task_id='ctx'")).fetchone()
    assert all(str(value).startswith("enc:v1:") for value in task_row)
    assert store._commit_context_consolidation(reclaimed, status="completed", task_input=reclaimed.input)
    assert "pending_summary" not in store.get_task("ctx").input


def test_sql_runtime_store_plaintext_without_encryption(tmp_path, monkeypatch):
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    db_path = tmp_path / "runtime.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    store = SqlRuntimeStore(engine, db_url=f"sqlite:///{db_path}")

    message = ConversationMessage(
        message_id="msg-plain-1",
        conversation_id="chat:2",
        actor_type="owner",
        content="plain text",
        metadata={"chat_id": 2},
    )
    store.append_message(message)

    with store._lock, store._connection() as conn:
        row = conn.execute(
            text("SELECT content FROM messages WHERE message_id=:id"),
            {"id": "msg-plain-1"},
        ).fetchone()
    assert row is not None
    assert row.content == "plain text"

    listed = store.list_messages(conversation_id="chat:2")
    assert listed[0].content == "plain text"


def test_encrypted_clean_removes_a2a_messages(tmp_path, encryption_env):
    """`/clean` reaches synthetic conversations; encryption does not exempt them."""
    db_path = tmp_path / "runtime.sqlite3"
    store = SqlRuntimeStore(create_engine(f"sqlite:///{db_path}"), db_url=f"sqlite:///{db_path}")
    store._dialect = "postgresql"
    for message_id, chat_id in (("owner", 1), ("a2a", -4001)):
        store.append_message(
            ConversationMessage(
                message_id=message_id,
                conversation_id="global",
                actor_type="owner",
                content=message_id,
                metadata={"chat_id": chat_id},
            )
        )

    store.delete_owner_messages()

    assert store.get_message("owner") is None
    assert store.get_message("a2a") is None
