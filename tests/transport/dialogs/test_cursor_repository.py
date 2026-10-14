from __future__ import annotations

import time

import pytest
from sqlalchemy import event, text

from infra.sql_connection import store_deadline
from ouroboros.storage.owner_state import default_state_dict, ensure_state_defaults
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.runtime_serialization import dump_json
from supervisor.interfaces.owner_state import OwnerStateDocument
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from transport.dialogs.cursor import DialogsCursor
from transport.dialogs.infra.repositories.repo import SqlDialogsCursorRepository


def test_cursor_survives_repository_recreation_and_seeds_stable_owner_defaults() -> None:
    store = runtime_persistence.current
    repository = SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE)
    assert repository.load_cursor() == 0

    repository.save_cursor(17)

    state = sql_owner_state_repository(store.engine).load(TEST_SCOPE).payload
    assert state is not None
    assert state["session_id"]
    assert ensure_state_defaults(state) == state
    assert SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE).load_cursor() == 17
    repository.save_cursor(23)
    assert sql_owner_state_repository(store.engine).load(TEST_SCOPE).payload["session_id"] == state["session_id"]


def test_cursor_and_owner_repository_preserve_each_others_fields() -> None:
    store = runtime_persistence.current
    initial = {**default_state_dict(), "spent_calls": 12, "custom": "keep", "provider_state": {"other": {"seq": 81}}}
    sql_owner_state_repository(store.engine).save(OwnerStateDocument(initial), TEST_SCOPE)
    stale = sql_owner_state_repository(store.engine).load(TEST_SCOPE).payload
    repository = SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE)

    repository.save_cursor(17)
    state = sql_owner_state_repository(store.engine).load(TEST_SCOPE).payload
    assert state == {**stale, "provider_state": {"other": {"seq": 81}, "dialogs": {"seq": 17}}}

    sql_owner_state_repository(store.engine).save(OwnerStateDocument({**stale, "spent_calls": 24}), TEST_SCOPE)
    assert repository.load_cursor() == 17
    assert sql_owner_state_repository(store.engine).load(TEST_SCOPE).payload["spent_calls"] == 24


@parametrize_from_dict(
    {
        "another owner": {"axis": {"owner_id": "other-owner"}},
        "another agent": {"axis": {"agent_id": "other-agent"}},
    }
)
def test_cursor_is_isolated_by_owner_and_agent(axis) -> None:
    engine = runtime_persistence.current.engine
    repository = SqlDialogsCursorRepository(engine, scope=TEST_SCOPE)
    other = SqlDialogsCursorRepository(engine, scope=scope_with(TEST_SCOPE, **axis))

    repository.save_cursor(17)
    assert other.load_cursor() == 0
    other.save_cursor(23)

    assert repository.load_cursor() == 17
    assert other.load_cursor() == 23


@parametrize_from_dict(
    {
        "missing providers": {"providers": None, "expected": 0},
        "malformed providers": {"providers": [], "expected": 0},
        "missing dialogs": {"providers": {}, "expected": 0},
        "malformed dialogs": {"providers": {"dialogs": []}, "expected": 0},
        "missing sequence": {"providers": {"dialogs": {}}, "expected": 0},
        "numeric string": {"providers": {"dialogs": {"seq": "17"}}, "expected": 17},
    }
)
def test_repository_reads_existing_payloads(providers, expected) -> None:
    store = runtime_persistence.current
    sql_owner_state_repository(store.engine).save(
        OwnerStateDocument({**default_state_dict(), "provider_state": providers}), TEST_SCOPE
    )

    assert SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE).load_cursor() == expected


@parametrize_from_dict(
    {
        "concurrent first insert": {"existing": False},
        "concurrent update": {"existing": True},
    }
)
def test_cursor_retries_after_a_competing_owner_state_write(existing) -> None:
    store = runtime_persistence.current
    if existing:
        sql_owner_state_repository(store.engine).save(
            OwnerStateDocument({**default_state_dict(), "provider_state": {"other": {"seq": 81}}}), TEST_SCOPE
        )
    writes = []

    def competing_write(conn, cursor, statement, parameters, context, executemany):
        if "owner_runtime_state" not in statement or not statement.lstrip().startswith(("INSERT", "UPDATE")):
            return
        if writes:
            return
        writes.append(True)
        sql_owner_state_repository(store.engine).save(
            OwnerStateDocument({**default_state_dict(), "spent_calls": 42, "provider_state": {"other": {"seq": 81}}}),
            TEST_SCOPE,
        )

    event.listen(store.engine, "before_cursor_execute", competing_write)
    try:
        SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE).save_cursor(17)
    finally:
        event.remove(store.engine, "before_cursor_execute", competing_write)

    state = sql_owner_state_repository(store.engine).load(TEST_SCOPE).payload
    assert writes == [True]
    assert state["spent_calls"] == 42
    assert state["provider_state"] == {"other": {"seq": 81}, "dialogs": {"seq": 17}}


def test_exhausted_conflicts_raise_without_overwriting_the_competing_writer() -> None:
    store = runtime_persistence.current
    SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE).save_cursor(7)
    writes = []

    def competing_write(conn, cursor, statement, parameters, context, executemany):
        if "payload_json=" not in statement or "seen_body" not in parameters:
            return
        writes.append(len(writes) + 1)
        with store.engine.begin() as other:
            other.execute(
                text("UPDATE owner_runtime_state SET payload_json=:payload WHERE owner_id=:owner AND agent_id=:agent"),
                {
                    "payload": dump_json({"spent_calls": len(writes), "provider_state": {"dialogs": {"seq": 7}}}),
                    "owner": TEST_SCOPE.owner_id,
                    "agent": TEST_SCOPE.agent_id,
                },
            )

    event.listen(store.engine, "before_cursor_execute", competing_write)
    try:
        with pytest.raises(RuntimeError, match="rewritten faster"):
            SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE).save_cursor(17)
    finally:
        event.remove(store.engine, "before_cursor_execute", competing_write)

    assert len(writes) == 5
    assert SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE).load_cursor() == 7


@parametrize_from_dict({"read": {"write": False}, "write": {"write": True}})
def test_expired_deadline_refuses_cursor_access_without_changing_state(write: bool) -> None:
    engine = runtime_persistence.current.engine
    repository = SqlDialogsCursorRepository(engine, scope=TEST_SCOPE)
    repository.save_cursor(7)
    owners = sql_owner_state_repository(engine)
    original = owners.load(TEST_SCOPE)

    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            if write:
                repository.save_cursor(17)
            else:
                repository.load_cursor()
    finally:
        store_deadline.reset(token)

    assert owners.load(TEST_SCOPE) == original


@parametrize_from_dict({"first insert": {"existing": False}, "update": {"existing": True}})
def test_deadline_before_commit_preserves_persisted_state_and_cached_cursor(existing: bool) -> None:
    engine = runtime_persistence.current.engine
    repository = SqlDialogsCursorRepository(engine, scope=TEST_SCOPE)
    if existing:
        repository.save_cursor(7)
    owners = sql_owner_state_repository(engine)
    original = owners.load(TEST_SCOPE)
    cursor = DialogsCursor(repository)
    previous = cursor.value

    def expire_after_write(conn, db_cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith(("INSERT INTO owner_runtime_state", "UPDATE owner_runtime_state")):
            conn.info["operation_deadline"] = time.monotonic() - 1

    token = store_deadline.set(time.monotonic() + 60)
    event.listen(engine, "after_cursor_execute", expire_after_write)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            cursor.commit(17)
    finally:
        event.remove(engine, "after_cursor_execute", expire_after_write)
        store_deadline.reset(token)

    assert cursor.value == previous
    assert owners.load(TEST_SCOPE) == original


def test_cursor_can_be_read_and_committed_within_the_deadline() -> None:
    repository = SqlDialogsCursorRepository(runtime_persistence.current.engine, scope=TEST_SCOPE)
    token = store_deadline.set(time.monotonic() + 60)
    try:
        assert repository.load_cursor() == 0
        repository.save_cursor(17)
        assert repository.load_cursor() == 17
    finally:
        store_deadline.reset(token)
