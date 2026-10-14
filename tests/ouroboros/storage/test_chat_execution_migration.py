"""Chat execution rollout preflights before touching either data or schema."""

import json
from collections.abc import Iterator

import pytest
from psycopg import Cursor
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine

from new_core.identity import OwnerScope
from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
from ouroboros.storage.message_field_crypto import FieldKeyUnavailable, encrypt_field, reset_encryption_cache_for_tests
from tests.helpers.conversations import make_sql_runtime_persistence
from tests.helpers.migrations import schema_snapshot, upgrade_to
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


@pytest.fixture
def legacy_engine(blank_schema, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXphYmNkZWY=")
    reset_encryption_cache_for_tests()
    engine = blank_schema()
    upgrade_to(engine, "0020")
    try:
        yield engine
    finally:
        reset_encryption_cache_for_tests()


def _seed(engine: Engine, *, scopes: list[dict[str, str]], corrupt: bool = False) -> None:
    with engine.begin() as conn:
        for i, scope in enumerate(scopes):
            task_input = json.dumps({"scope": scope})
            payload = json.dumps({"input": {"scope": scope}, "chat_id": "stale"})
            conn.execute(
                text(
                    "INSERT INTO tasks (task_id, job_id, owner_id, agent_id, status, input_json, payload_json, created_at) "
                    "VALUES (:id, :id, :owner_id, :agent_id, 'completed', :input, :payload, '2026-01-01')"
                ),
                {
                    "id": str(i),
                    **scope,
                    "input": encrypt_field(task_input),
                    "payload": "unreadable" if corrupt else encrypt_field(payload),
                },
            )
        for job, status in [("", "active"), ("", "evicted"), ("detached-job", "active")]:
            conn.execute(
                text(
                    "INSERT INTO scratchpad_blocks (owner_id, agent_id, job_id, ts, source, content, status) "
                    "VALUES (:owner_id, :agent_id, :job, '2026-01-01', 'task', :content, :status)"
                ),
                {**TEST_SCOPE.to_dict(), "job": job, "status": status, "content": status + job},
            )


@parametrize_from_dict(
    {
        "no-chat": {"scopes": [], "preserve": False},
        "one-chat": {"scopes": [TEST_SCOPE.to_dict()], "preserve": True},
        "internal-work": {
            "scopes": [
                TEST_SCOPE.to_dict(),
                {**TEST_SCOPE.to_dict(), "chat_id": "-1"},
                {**TEST_SCOPE.to_dict(), "surface_id": "a2a"},
            ],
            "preserve": True,
        },
        "two-chats": {
            "scopes": [TEST_SCOPE.to_dict(), {**TEST_SCOPE.to_dict(), "chat_id": "other"}],
            "preserve": False,
        },
        "two-surfaces": {
            "scopes": [TEST_SCOPE.to_dict(), {**TEST_SCOPE.to_dict(), "surface_id": "dialogs"}],
            "preserve": False,
        },
    }
)
def test_migration_preserves_only_unambiguous_blocks_and_accepts_new_writes(
    legacy_engine: Engine, scopes: list[dict[str, str]], preserve: bool
) -> None:
    _seed(legacy_engine, scopes=scopes)
    columns = "id, owner_id, agent_id, job_id, ts, source, content, status"
    with legacy_engine.connect() as conn:
        before = conn.execute(text(f"SELECT {columns} FROM scratchpad_blocks ORDER BY id")).all()
    upgrade_to(legacy_engine, "head")
    with legacy_engine.connect() as conn:
        assert conn.execute(text(f"SELECT {columns} FROM scratchpad_blocks ORDER BY id")).all() == (
            before if preserve else [before[-1]]
        )
        assert conn.execute(text("SELECT surface_id, chat_id FROM scratchpad_blocks ORDER BY id")).all() == (
            [(str(TEST_SCOPE.surface_id), TEST_SCOPE.chat_id)] * 2 if preserve else []
        ) + [("", "")]
    tasks = make_sql_runtime_persistence(legacy_engine, db_url=str(legacy_engine.url)).tasks_repo
    for i, scope in enumerate(scopes):
        row = tasks.get_task(str(i))
        assert row.to_agent_input()["scope"] == scope
        assert (row.surface_id, row.chat_id) == (scope["surface_id"], scope["chat_id"])
    scratchpad = SqlScratchpad(legacy_engine)
    scratchpad.append_and_evict(TEST_SCOPE, "2026-01-02", "task", "new note", 10)
    assert [block.content for block in scratchpad.list_active(TEST_SCOPE)] == (
        ["active", "new note"] if preserve else ["new note"]
    )


@parametrize_from_dict(
    {
        f"{source}-{case}": {"source": source, "conflicting": conflicting}
        for source in ("owner-state", "message", "chat-registry")
        for case, conflicting in (("only-evidence", False), ("conflicts-with-task", True))
    }
)
def test_migration_resolves_historical_chat_evidence(legacy_engine: Engine, source: str, conflicting: bool) -> None:
    _seed(legacy_engine, scopes=[TEST_SCOPE.to_dict()] if conflicting else [])
    scope = {**TEST_SCOPE.to_dict(), "chat_id": "other" if conflicting else TEST_SCOPE.chat_id}
    statements = {
        "owner-state": (
            "INSERT INTO owner_runtime_state (owner_id, agent_id, payload_json, updated_at) "
            "VALUES (:owner_id, :agent_id, :payload, '')"
        ),
        "message": (
            "INSERT INTO messages (message_id, dedup_key, owner_id, agent_id, source, "
            "actor_type, created_at, chat_id, metadata_json, payload_json) "
            "VALUES ('historical', 'historical', :owner_id, :agent_id, :surface_id, 'owner', '', :chat_id, '{}', '{}')"
        ),
        "chat-registry": "INSERT INTO chats (owner_id, agent_id, chat_id) VALUES (:owner_id, :agent_id, :chat_id)",
    }
    with legacy_engine.begin() as conn:
        conn.execute(
            text(statements[source]),
            {**scope, "payload": encrypt_field(json.dumps({"owner_scope": scope}))},
        )
    upgrade_to(legacy_engine, "head")
    assert [block.content for block in SqlScratchpad(legacy_engine).list_active(TEST_SCOPE)] == (
        [] if conflicting else ["active"]
    )


@parametrize_from_dict(
    {
        "unreadable": {"payload": "unreadable"},
        "wrong-owner": {"payload": json.dumps({"owner_scope": {**TEST_SCOPE.to_dict(), "owner_id": "other-owner"}})},
    }
)
def test_invalid_owner_state_resets_only_its_own_foreground_blocks(legacy_engine: Engine, payload: str) -> None:
    _seed(legacy_engine, scopes=[TEST_SCOPE.to_dict()])
    neighbors = [
        OwnerScope.from_dict({**TEST_SCOPE.to_dict(), "owner_id": "other-owner"}),
        OwnerScope.from_dict({**TEST_SCOPE.to_dict(), "agent_id": "other-agent"}),
    ]
    with legacy_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO owner_runtime_state (owner_id, agent_id, payload_json, updated_at) "
                "VALUES (:owner_id, :agent_id, :payload, '')"
            ),
            {**TEST_SCOPE.to_dict(), "payload": payload},
        )
        for scope in neighbors:
            conn.execute(
                text("INSERT INTO chats (owner_id, agent_id, chat_id) VALUES (:owner_id, :agent_id, :chat_id)"),
                scope.to_dict(),
            )
            conn.execute(
                text(
                    "INSERT INTO scratchpad_blocks (owner_id, agent_id, ts, source, content, status) "
                    "VALUES (:owner_id, :agent_id, '', 'task', :content, 'active')"
                ),
                {**scope.to_dict(), "content": f"{scope.owner_id}/{scope.agent_id}"},
            )
    upgrade_to(legacy_engine, "head")
    pad = SqlScratchpad(legacy_engine)
    assert pad.list_active(TEST_SCOPE) == []
    assert [block.content for block in pad.list_active(TEST_SCOPE, job_id="detached-job")] == ["activedetached-job"]
    for scope in neighbors:
        assert [block.content for block in pad.list_active(scope)] == [f"{scope.owner_id}/{scope.agent_id}"]


def test_failure_after_scratchpad_deletion_rolls_back_schema_and_data(legacy_engine: Engine) -> None:
    _seed(legacy_engine, scopes=[])
    before = schema_snapshot(legacy_engine, data=True)

    def fail_after_delete(_conn: Connection, _cursor: Cursor, statement: str, *_unused: object) -> None:
        if statement.startswith("DELETE FROM scratchpad_blocks"):
            raise RuntimeError("failure after scratchpad deletion")

    event.listen(legacy_engine, "after_cursor_execute", fail_after_delete)
    try:
        with pytest.raises(RuntimeError, match="failure after scratchpad deletion"):
            upgrade_to(legacy_engine, "head")
    finally:
        event.remove(legacy_engine, "after_cursor_execute", fail_after_delete)
    assert schema_snapshot(legacy_engine, data=True) == before


def test_failed_task_preflight_preserves_schema_and_data(legacy_engine):
    _seed(legacy_engine, scopes=[TEST_SCOPE.to_dict()], corrupt=True)
    before = schema_snapshot(legacy_engine, data=True)
    with pytest.raises(RuntimeError, match=f"0021.*{TEST_SCOPE.owner_id}.*{TEST_SCOPE.agent_id}"):
        upgrade_to(legacy_engine, "head")
    assert schema_snapshot(legacy_engine, data=True) == before


def test_missing_owner_state_key_refuses_migration_without_reset(
    legacy_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(legacy_engine, scopes=[])
    payload = encrypt_field(json.dumps({"owner_scope": TEST_SCOPE.to_dict()}))
    assert payload.startswith("enc:v1:")
    with legacy_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO owner_runtime_state (owner_id, agent_id, payload_json, updated_at) "
                "VALUES (:owner_id, :agent_id, :payload, '')"
            ),
            {**TEST_SCOPE.to_dict(), "payload": payload},
        )
    before = schema_snapshot(legacy_engine, data=True)
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "false")
    reset_encryption_cache_for_tests()
    with pytest.raises(FieldKeyUnavailable):
        upgrade_to(legacy_engine, "head")
    assert schema_snapshot(legacy_engine, data=True) == before


def test_terminal_legacy_task_without_chat_does_not_block_reset(legacy_engine):
    _seed(legacy_engine, scopes=[TEST_SCOPE.to_dict()])
    with legacy_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, job_id, owner_id, agent_id, source, status, "
                "input_json, payload_json, metadata_json, created_at, chat_id) "
                "VALUES ('unknown', 'unknown', :owner_id, :agent_id, 'browser', 'failed', '{}', '{}', '{}', '', NULL)"
            ),
            TEST_SCOPE.to_dict(),
        )
    upgrade_to(legacy_engine, "head")
    with legacy_engine.connect() as conn:
        assert conn.execute(text("SELECT status, surface_id, chat_id FROM tasks WHERE task_id='unknown'")).one() == (
            "failed",
            "browser",
            "",
        )
        assert conn.execute(text("SELECT COUNT(*) FROM scratchpad_blocks WHERE job_id='' ")).scalar_one() == 0
