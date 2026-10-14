"""Tests for optional AES-256-GCM chat message field encryption."""

from __future__ import annotations

import base64
import json

import pytest
from sqlalchemy import text

from infra.migrations.runner import upgrade_head
from ouroboros.storage.message_field_crypto import (
    FieldKeyUnavailable,
    decrypt_field,
    encrypt_field,
    encryption_active,
    reset_encryption_cache_for_tests,
    validate_chat_encryption,
)
from ouroboros.storage.runtime_models import ContextSummary, ConversationMessage, TaskRecord
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.composition_roots import START_UP_ROOTS, called_names_in_order
from tests.helpers.conversations import make_sql_runtime_persistence, sql_conversation_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


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
    encrypted = encrypt_field(plaintext)
    assert encrypted.startswith("enc:v1:")
    assert encrypted != plaintext
    assert decrypt_field(encrypted) == plaintext


def test_plaintext_passthrough_when_flag_disabled(monkeypatch):
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    plaintext = "legacy row"
    assert encrypt_field(plaintext) == plaintext
    assert decrypt_field(plaintext) == plaintext
    assert encryption_active() is False


def test_legacy_plaintext_row_readable(encryption_env):
    legacy = '{"content": "hello"}'
    assert decrypt_field(legacy) == legacy


def test_enabled_without_key_warns_in_development(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "development")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    assert encryption_active() is False
    assert encrypt_field("hello") == "hello"


def test_enabled_without_key_fails_in_production(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    with pytest.raises(ValueError, match="OUROBOROS_CHAT_ENCRYPTION_KEY"):
        encryption_active()


def test_a_misconfigured_key_reads_as_this_readers_fault_not_the_rows(monkeypatch):
    """Readers classify by this type: mistaking it for corruption parks every encrypted row fleet-wide."""
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", "not-base64-at-all")
    reset_encryption_cache_for_tests()

    with pytest.raises(FieldKeyUnavailable):
        encryption_active()


def test_the_start_up_check_refuses_an_enabled_encryption_without_a_usable_key_in_production(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", "not-base64-at-all")
    reset_encryption_cache_for_tests()

    with pytest.raises(FieldKeyUnavailable, match="OUROBOROS_CHAT_ENCRYPTION_KEY") as refused:
        validate_chat_encryption()

    assert "not-base64-at-all" not in str(refused.value)


def test_the_start_up_check_disables_an_enabled_encryption_without_a_key_in_development(monkeypatch, caplog):
    monkeypatch.setenv("OUROBOROS_ENV", "development")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    with caplog.at_level("WARNING", logger="ouroboros.storage.message_field_crypto"):
        validate_chat_encryption()

    assert "chat encryption disabled" in caplog.text
    assert encryption_active() is False


def test_the_start_up_check_passes_a_disabled_encryption_without_a_key(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", "")
    reset_encryption_cache_for_tests()

    validate_chat_encryption()

    assert encryption_active() is False


@parametrize_from_dict({root: {"root": root} for root in START_UP_ROOTS})
def test_every_root_checks_chat_encryption_after_storage_and_before_the_mode(root):
    calls = called_names_in_order(root)

    assert calls.count("validate_chat_encryption") == 1, root
    assert calls.index("validate_production_storage") < calls.index("validate_chat_encryption"), root
    assert calls.index("validate_chat_encryption") < calls.index("validate_multiuser_configuration"), root


def test_sql_runtime_store_encrypted_round_trip(blank_schema, encryption_env):
    engine = blank_schema()
    upgrade_head(engine)
    store = make_sql_runtime_persistence(engine, db_url=engine.url.render_as_string(hide_password=False))

    message = ConversationMessage(
        owner_id=TEST_SCOPE.owner_id,
        agent_id=TEST_SCOPE.agent_id,
        chat_id=TEST_SCOPE.chat_id,
        message_id="msg-enc-1",
        dedup_key="msg-enc-1",
        session_id="sess-1",
        source="sberchat",
        actor_type="owner",
        channel="sberchat",
        content="private owner text",
        metadata={"chat_id": 1, "user_id": 1},
    )
    sql_conversation_repository(store.engine).append_message(message, scope=TEST_SCOPE)

    with store.engine.connect() as conn:
        row = conn.execute(
            text("SELECT content, metadata_json, payload_json FROM messages WHERE message_id=:id"),
            {"id": "msg-enc-1"},
        ).fetchone()
    assert row is not None
    assert str(row.content).startswith("enc:v1:")
    assert str(row.metadata_json).startswith("enc:v1:")
    assert str(row.payload_json).startswith("enc:v1:")

    listed = sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE)
    assert len(listed) == 1
    assert listed[0].content == "private owner text"
    assert listed[0].metadata.get("chat_id") == 1

    sql_conversation_repository(store.engine).upsert_context_summary(
        ContextSummary(
            summary_id="shared-chat",
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            chat_id=TEST_SCOPE.chat_id,
            summary_ref="initial private summary",
        )
    )
    with store.engine.connect() as conn:
        summary_row = conn.execute(
            text(
                "SELECT summary_ref, metadata_json, payload_json FROM context_summaries WHERE summary_id='shared-chat'"
            )
        ).fetchone()
    assert summary_row is not None
    assert all(str(value).startswith("enc:v1:") for value in summary_row)
    assert (
        sql_conversation_repository(store.engine).get_latest_context_summary(TEST_SCOPE).summary_ref
        == "initial private summary"
    )

    lease = TaskRecord(
        task_id="ctx",
        owner_id=TEST_SCOPE.owner_id,
        agent_id=TEST_SCOPE.agent_id,
        status="running",
        task_type="context_consolidation",
        input={"expected_cursor": "", "pending_summary": "partial private summary"},
        worker_id=1,
        started_at="2026-08-01T00:00:00+00:00",
    )
    store.tasks_repo.upsert_task(lease)
    with store.engine.connect() as conn:
        task_row = conn.execute(text("SELECT input_json, payload_json FROM tasks WHERE task_id='ctx'")).fetchone()
    assert all(str(value).startswith("enc:v1:") for value in task_row)
    assert store.tasks_repo.get_task("ctx").input["expected_cursor"] == ""


def test_sql_runtime_store_plaintext_without_encryption(blank_schema, monkeypatch):
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    engine = blank_schema()
    upgrade_head(engine)
    store = make_sql_runtime_persistence(engine, db_url=engine.url.render_as_string(hide_password=False))

    message = ConversationMessage(
        owner_id=TEST_SCOPE.owner_id,
        agent_id=TEST_SCOPE.agent_id,
        chat_id=TEST_SCOPE.chat_id,
        message_id="msg-plain-1",
        dedup_key="msg-plain-1",
        actor_type="owner",
        content="plain text",
        metadata={"chat_id": 2},
    )
    sql_conversation_repository(store.engine).append_message(message, scope=TEST_SCOPE)

    with store.engine.connect() as conn:
        row = conn.execute(
            text("SELECT content FROM messages WHERE message_id=:id"),
            {"id": "msg-plain-1"},
        ).fetchone()
    assert row is not None
    assert row.content == "plain text"

    listed = sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE)
    assert listed[0].content == "plain text"


@pytest.mark.parametrize("detached", [False, True])
def test_admission_snapshot_stays_encrypted_through_upsert_and_claim(tmp_path, encryption_env, detached):
    from ouroboros.storage.chat_store import append_chat_message
    from ouroboros.storage.runtime_models import TaskRequest

    store = runtime_persistence.initialize()
    anchor = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type="owner",
        chat_id=TEST_SCOPE.chat_id,
        user_id=TEST_SCOPE.owner_id,
        text="work",
        source="browser",
        client_message_id=f"anchor-{detached}",
    )
    secret = "historical secret in committed summary"
    sql_conversation_repository(store.engine).upsert_context_summary(
        ContextSummary(
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            chat_id=TEST_SCOPE.chat_id,
            source_offsets_ref=anchor.message_id,
            summary_ref=secret,
        )
    )
    record = runtime_persistence.current.create_root_task.execute(
        TaskRequest(
            id=f"snapshot-{detached}",
            text="work",
            detached=detached,
            scope=TEST_SCOPE.to_dict(),
            through_message_id=anchor.message_id,
        ),
        enabled=detached,
        max_active=5,
    )
    for phase in ("admission", "upsert", "claim"):
        if phase == "upsert":
            with store.engine.begin() as conn:
                conn.execute(
                    text("UPDATE tasks SET input_json=:input, payload_json=:payload WHERE task_id=:id"),
                    {
                        "input": json.dumps(record.input),
                        "payload": record.model_dump_json(),
                        "id": record.task_id,
                    },
                )
            assert store.tasks_repo.get_task(record.task_id).input["committed_summary_snapshot"] == secret
            store.tasks_repo.upsert_task(store.tasks_repo.get_task(record.task_id))
        elif phase == "claim":
            assert store.tasks_repo.claim_next_task(runtime_id="runtime")
        with store.engine.connect() as conn:
            row = conn.execute(
                text("SELECT input_json, payload_json FROM tasks WHERE task_id=:id"), {"id": record.task_id}
            ).one()
        assert all(secret not in value and value.startswith("enc:v1:") for value in row), phase
        assert store.tasks_repo.get_task(record.task_id).to_agent_input()["committed_summary_snapshot"] == secret
