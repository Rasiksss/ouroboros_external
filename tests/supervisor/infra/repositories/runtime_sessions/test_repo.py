import json
import time

import pytest
from sqlalchemy import event, text

from infra.sql_connection import store_deadline
from ouroboros.storage.runtime_models import RuntimeSession
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.infra.repositories.runtime_sessions.repo import SqlRuntimeSessionRepository
from supervisor.interfaces.runtime_sessions import IRuntimeSessionRepository
from tests.helpers.runtime_sessions_conformance import CREATED_AT, UPDATED_AT, RuntimeSessionRepositoryConformance


class TestSqlRuntimeSessionRepository(RuntimeSessionRepositoryConformance):
    def make_repository(self) -> IRuntimeSessionRepository:
        return SqlRuntimeSessionRepository(runtime_persistence.current.engine)

    def read_session(self, repository: IRuntimeSessionRepository, session_id: str) -> RuntimeSession:
        with runtime_persistence.current.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM sessions WHERE session_id=:id"), {"id": session_id}).mappings().one()
        return RuntimeSession.model_validate({key: row[key] for key in RuntimeSession.model_fields})


def test_upsert_payload_keeps_callers_creation_time_and_extra_fields() -> None:
    engine = runtime_persistence.current.engine
    repository = SqlRuntimeSessionRepository(engine)
    repository.create_or_update_session(RuntimeSession(session_id="same", created_at=CREATED_AT))
    update = RuntimeSession.model_validate(
        {"session_id": "same", "created_at": UPDATED_AT, "extra_metadata": {"label": "сессия"}}
    )

    repository.create_or_update_session(update)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT created_at, payload_json FROM sessions WHERE session_id='same'")).one()
    assert row.created_at == CREATED_AT
    assert json.loads(row.payload_json) == update.model_dump(mode="json")


def test_expired_deadline_refuses_session_without_persisting_it() -> None:
    engine = runtime_persistence.current.engine
    repository = SqlRuntimeSessionRepository(engine)
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.create_or_update_session(RuntimeSession(session_id="new"))
    finally:
        store_deadline.reset(token)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM sessions")).scalar_one() == 0


def test_deadline_expiring_before_commit_preserves_previous_session() -> None:
    engine = runtime_persistence.current.engine
    repository = SqlRuntimeSessionRepository(engine)
    repository.create_or_update_session(RuntimeSession(session_id="same", owner_external_id="original"))

    def expire_after_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO sessions"):
            conn.info["operation_deadline"] = time.monotonic() - 1

    token = store_deadline.set(time.monotonic() + 60)
    event.listen(engine, "after_cursor_execute", expire_after_insert)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.create_or_update_session(RuntimeSession(session_id="same", owner_external_id="changed"))
    finally:
        event.remove(engine, "after_cursor_execute", expire_after_insert)
        store_deadline.reset(token)

    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT owner_external_id FROM sessions WHERE session_id='same'")).scalar_one()
            == "original"
        )
