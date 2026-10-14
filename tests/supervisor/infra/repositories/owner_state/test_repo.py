import time

import pytest
from sqlalchemy import event, text

from infra.sql_connection import store_deadline
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.runtime_serialization import dump_json
from supervisor.interfaces.cleanup_checkpoint import CleanupCheckpoint
from supervisor.interfaces.owner_state import IOwnerStateRepository, OwnerStateDocument
from tests.helpers.cleanup_checkpoint import sql_cleanup_checkpoint_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.supervisor_owner_state_conformance import OwnerStateRepositoryConformance
from transport.dialogs.infra.repositories.repo import SqlDialogsCursorRepository


def _seed_body(scope: OwnerScope, body: str) -> None:
    with runtime_persistence.current.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO owner_runtime_state (owner_id, agent_id, payload_json, updated_at) "
                "VALUES (:owner_id, :agent_id, :body, 'original') "
                "ON CONFLICT(owner_id, agent_id) DO UPDATE SET payload_json=excluded.payload_json"
            ),
            {"owner_id": scope.owner_id, "agent_id": scope.agent_id, "body": body},
        )


class TestSqlOwnerStateRepository(OwnerStateRepositoryConformance):
    def make_repository(self) -> IOwnerStateRepository:
        return sql_owner_state_repository()

    def seed(self, repository: IOwnerStateRepository, scope: OwnerScope, document: OwnerStateDocument) -> None:
        _seed_body(scope, dump_json(document.payload))


@parametrize_from_dict({"first insert": {"existing": False}, "update": {"existing": True}})
def test_save_retries_after_a_competing_transport_write(existing: bool) -> None:
    engine = runtime_persistence.current.engine
    repository = sql_owner_state_repository(engine)
    cursor_repository = SqlDialogsCursorRepository(engine, scope=TEST_SCOPE)
    if existing:
        cursor_repository.save_cursor(7)
    writes = []

    def competing_write(conn, cursor, statement, parameters, context, executemany):
        if writes or not statement.lstrip().startswith(
            ("INSERT INTO owner_runtime_state", "UPDATE owner_runtime_state")
        ):
            return
        writes.append(True)
        cursor_repository.save_cursor(17)

    event.listen(engine, "before_cursor_execute", competing_write)
    try:
        repository.save(OwnerStateDocument({"session_id": "new", "spent_calls": 42}), TEST_SCOPE)
    finally:
        event.remove(engine, "before_cursor_execute", competing_write)

    assert writes == [True]
    assert repository.load(TEST_SCOPE) == OwnerStateDocument(
        {"session_id": "new", "spent_calls": 42, "provider_state": {"dialogs": {"seq": 17}}}
    )


def test_five_conflicts_raise_without_overwriting_the_transport_winner() -> None:
    engine = runtime_persistence.current.engine
    repository = sql_owner_state_repository(engine)
    cursor_repository = SqlDialogsCursorRepository(engine, scope=TEST_SCOPE)
    cursor_repository.save_cursor(7)
    writes = []
    inside_transport = False

    def competing_write(conn, cursor, statement, parameters, context, executemany):
        nonlocal inside_transport
        if inside_transport or not statement.lstrip().startswith("UPDATE owner_runtime_state"):
            return
        writes.append(len(writes) + 1)
        inside_transport = True
        try:
            cursor_repository.save_cursor(7 + len(writes))
        finally:
            inside_transport = False

    event.listen(engine, "before_cursor_execute", competing_write)
    try:
        with pytest.raises(RuntimeError, match="is being rewritten faster than it can be merged"):
            repository.save(OwnerStateDocument({"session_id": "must-not-win"}), TEST_SCOPE)
    finally:
        event.remove(engine, "before_cursor_execute", competing_write)

    assert len(writes) == 5
    assert cursor_repository.load_cursor() == 12
    saved = repository.load(TEST_SCOPE)
    assert saved is not None and saved.payload["session_id"] != "must-not-win"


@parametrize_from_dict({"malformed": {"body": "not json"}, "array": {"body": "[]"}, "empty": {"body": ""}})
def test_legacy_payload_is_read_without_repair_and_replaced_using_its_exact_body(body: str) -> None:
    engine = runtime_persistence.current.engine
    repository = sql_owner_state_repository(engine)
    _seed_body(TEST_SCOPE, body)

    assert repository.load(TEST_SCOPE) == OwnerStateDocument({})
    with engine.connect() as conn:
        assert conn.execute(text("SELECT payload_json, updated_at FROM owner_runtime_state")).one() == (
            body,
            "original",
        )
    repository.save(OwnerStateDocument({"session_id": "repaired"}), TEST_SCOPE)
    assert repository.load(TEST_SCOPE) == OwnerStateDocument({"session_id": "repaired"})


def test_deadline_before_commit_rolls_back_the_owner_state_write() -> None:
    engine = runtime_persistence.current.engine
    repository = sql_owner_state_repository(engine)
    original = OwnerStateDocument({"session_id": "original", "provider_state": {"dialogs": {"seq": 7}}})
    repository.save(original, TEST_SCOPE)

    def expire_after_update(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("UPDATE owner_runtime_state"):
            conn.info["operation_deadline"] = time.monotonic() - 1

    token = store_deadline.set(time.monotonic() + 60)
    event.listen(engine, "after_cursor_execute", expire_after_update)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.save(OwnerStateDocument({"session_id": "changed"}), TEST_SCOPE)
    finally:
        event.remove(engine, "after_cursor_execute", expire_after_update)
        store_deadline.reset(token)

    assert repository.load(TEST_SCOPE) == original


@parametrize_from_dict({"first insert": {"existing": False}, "update": {"existing": True}})
def test_owner_save_keeps_a_checkpoint_written_during_its_cas_attempt(existing: bool) -> None:
    engine = runtime_persistence.current.engine
    owners = sql_owner_state_repository(engine)
    checkpoints = sql_cleanup_checkpoint_repository(engine)
    if existing:
        owners.save(OwnerStateDocument({"session_id": "old"}), TEST_SCOPE)
    writes = []
    checkpoint = CleanupCheckpoint("2026-07-01T00:00:00+00:00", {"scratchpad_blocks": 17})

    def competing_write(conn, cursor, statement, parameters, context, executemany):
        if writes or not statement.lstrip().startswith(
            ("INSERT INTO owner_runtime_state", "UPDATE owner_runtime_state")
        ):
            return
        writes.append(True)
        checkpoints.save_checkpoint(checkpoint, TEST_SCOPE)

    event.listen(engine, "before_cursor_execute", competing_write)
    try:
        owners.save(OwnerStateDocument({"session_id": "new"}), TEST_SCOPE)
    finally:
        event.remove(engine, "before_cursor_execute", competing_write)

    assert writes == [True]
    assert owners.load(TEST_SCOPE) == OwnerStateDocument(
        {
            "session_id": "new",
            "working_retrieval_cleanup": {
                "last_run_at": checkpoint.last_run_at,
                "removed": checkpoint.removed,
            },
        }
    )
