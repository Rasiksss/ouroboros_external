import time

import pytest
from sqlalchemy import event, text

from infra.sql_connection import store_deadline
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.runtime_serialization import dump_json, load_json
from supervisor.interfaces.cleanup_checkpoint import CleanupCheckpoint, ICleanupCheckpointRepository
from supervisor.interfaces.owner_state import OwnerStateDocument
from tests.helpers.cleanup_checkpoint import sql_cleanup_checkpoint_repository
from tests.helpers.cleanup_checkpoint_conformance import CleanupCheckpointRepositoryConformance
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from transport.dialogs.infra.repositories.repo import SqlDialogsCursorRepository

CHECKPOINT = CleanupCheckpoint("2026-07-01T00:00:00+00:00", {"tool_invocations": 7})


def _seed_body(body: str) -> None:
    with runtime_persistence.current.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO owner_runtime_state (owner_id, agent_id, payload_json, updated_at) "
                "VALUES (:owner, :agent, :body, 'original') "
                "ON CONFLICT(owner_id, agent_id) DO UPDATE SET payload_json=excluded.payload_json"
            ),
            {"owner": TEST_SCOPE.owner_id, "agent": TEST_SCOPE.agent_id, "body": body},
        )


def _read_payload(scope: OwnerScope) -> dict | None:
    with runtime_persistence.current.engine.connect() as conn:
        row = conn.execute(
            text("SELECT payload_json FROM owner_runtime_state WHERE owner_id=:owner AND agent_id=:agent"),
            {"owner": scope.owner_id, "agent": scope.agent_id},
        ).fetchone()
    return load_json(row[0]) if row is not None else None


class TestSqlCleanupCheckpointRepository(CleanupCheckpointRepositoryConformance):
    def make_repository(self) -> ICleanupCheckpointRepository:
        return sql_cleanup_checkpoint_repository()

    def read_saved(self, repository: ICleanupCheckpointRepository, scope: OwnerScope) -> CleanupCheckpoint | None:
        payload = _read_payload(scope)
        if payload is None or "working_retrieval_cleanup" not in payload:
            return None
        return CleanupCheckpoint(**payload["working_retrieval_cleanup"])


def test_checkpoint_save_preserves_fields_updated_after_the_interval_read() -> None:
    repository = sql_cleanup_checkpoint_repository()
    assert repository.get_last_run_at(TEST_SCOPE) == ""
    latest = {"session_id": "new", "owner_id": 42, "custom": {"keep": True}, "provider_state": {"dialogs": {"seq": 17}}}
    _seed_body(dump_json(latest))

    repository.save_checkpoint(CHECKPOINT, TEST_SCOPE)

    assert _read_payload(TEST_SCOPE) == {
        **latest,
        "working_retrieval_cleanup": {"last_run_at": CHECKPOINT.last_run_at, "removed": CHECKPOINT.removed},
    }


@parametrize_from_dict({"first insert": {"existing": False}, "update": {"existing": True}})
def test_checkpoint_retries_keep_concurrent_owner_and_transport_updates(existing: bool) -> None:
    engine = runtime_persistence.current.engine
    checkpoints = sql_cleanup_checkpoint_repository(engine)
    owners = sql_owner_state_repository(engine)
    cursor_repository = SqlDialogsCursorRepository(engine, scope=TEST_SCOPE)
    if existing:
        owners.save(OwnerStateDocument({"session_id": "old"}), TEST_SCOPE)
    writes = []

    def competing_write(conn, cursor, statement, parameters, context, executemany):
        if writes or not statement.lstrip().startswith(
            ("INSERT INTO owner_runtime_state", "UPDATE owner_runtime_state")
        ):
            return
        writes.append(True)
        owners.save(OwnerStateDocument({"session_id": "fresh", "spent_calls": 42}), TEST_SCOPE)
        cursor_repository.save_cursor(17)

    event.listen(engine, "before_cursor_execute", competing_write)
    try:
        checkpoints.save_checkpoint(CHECKPOINT, TEST_SCOPE)
    finally:
        event.remove(engine, "before_cursor_execute", competing_write)

    assert writes == [True]
    assert _read_payload(TEST_SCOPE) == {
        "session_id": "fresh",
        "spent_calls": 42,
        "provider_state": {"dialogs": {"seq": 17}},
        "working_retrieval_cleanup": {"last_run_at": CHECKPOINT.last_run_at, "removed": CHECKPOINT.removed},
    }


def test_five_conflicts_raise_without_overwriting_the_winner() -> None:
    engine = runtime_persistence.current.engine
    checkpoints = sql_cleanup_checkpoint_repository(engine)
    owners = sql_owner_state_repository(engine)
    owners.save(OwnerStateDocument({"spent_calls": 0}), TEST_SCOPE)
    writes = []
    inside_writer = False

    def competing_write(conn, cursor, statement, parameters, context, executemany):
        nonlocal inside_writer
        if inside_writer or not statement.lstrip().startswith("UPDATE owner_runtime_state"):
            return
        writes.append(len(writes) + 1)
        inside_writer = True
        try:
            owners.save(OwnerStateDocument({"spent_calls": len(writes)}), TEST_SCOPE)
        finally:
            inside_writer = False

    event.listen(engine, "before_cursor_execute", competing_write)
    try:
        with pytest.raises(RuntimeError, match="is being rewritten faster than it can be merged"):
            checkpoints.save_checkpoint(CHECKPOINT, TEST_SCOPE)
    finally:
        event.remove(engine, "before_cursor_execute", competing_write)

    assert len(writes) == 5
    assert _read_payload(TEST_SCOPE) == {"spent_calls": 5}


@parametrize_from_dict(
    {
        "missing checkpoint": {"body": "{}", "expected": ""},
        "malformed payload": {"body": "not json", "expected": ""},
        "array payload": {"body": "[]", "expected": ""},
        "non-object checkpoint": {"body": '{"working_retrieval_cleanup": []}', "expected": ""},
        "missing timestamp": {"body": '{"working_retrieval_cleanup": {}}', "expected": ""},
        "null timestamp": {"body": '{"working_retrieval_cleanup": {"last_run_at": null}}', "expected": ""},
        "invalid timestamp": {
            "body": '{"working_retrieval_cleanup": {"last_run_at": "invalid"}}',
            "expected": "invalid",
        },
    }
)
def test_checkpoint_read_keeps_legacy_values_without_repairing_the_row(body: str, expected: str) -> None:
    _seed_body(body)

    assert sql_cleanup_checkpoint_repository().get_last_run_at(TEST_SCOPE) == expected

    with runtime_persistence.current.engine.connect() as conn:
        assert conn.execute(text("SELECT payload_json, updated_at FROM owner_runtime_state")).one() == (
            body,
            "original",
        )


@parametrize_from_dict({"first insert": {"existing": False}, "update": {"existing": True}})
def test_timeout_before_commit_rolls_back_checkpoint(existing: bool) -> None:
    engine = runtime_persistence.current.engine
    repository = sql_cleanup_checkpoint_repository(engine)
    if existing:
        _seed_body('{"session_id":"keep"}')
    before = _read_payload(TEST_SCOPE)

    def expire_after_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith(("INSERT INTO owner_runtime_state", "UPDATE owner_runtime_state")):
            conn.info["operation_deadline"] = time.monotonic() - 1

    token = store_deadline.set(time.monotonic() + 60)
    event.listen(engine, "after_cursor_execute", expire_after_write)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.save_checkpoint(CHECKPOINT, TEST_SCOPE)
    finally:
        event.remove(engine, "after_cursor_execute", expire_after_write)
        store_deadline.reset(token)

    assert _read_payload(TEST_SCOPE) == before


def test_cleanup_claim_excludes_other_connections_and_releases_after_failure():
    first = sql_cleanup_checkpoint_repository()
    second = sql_cleanup_checkpoint_repository()
    with pytest.raises(ValueError, match="aborted"), first.try_claim() as claimed:
        assert claimed
        with second.try_claim() as competing:
            assert not competing
        raise ValueError("aborted")
    with second.try_claim() as recovered:
        assert recovered
