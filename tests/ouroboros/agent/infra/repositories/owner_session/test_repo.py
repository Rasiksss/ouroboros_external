import json
import time

import pytest
from sqlalchemy import text

from infra.sql_connection import store_deadline
from new_core.identity import OwnerScope
from ouroboros.agent.infra.repositories.owner_session.repo import SqlOwnerSessionRepository
from ouroboros.agent.interfaces.owner_session import IOwnerSessionRepository
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.agent_owner_session_conformance import OwnerSessionRepositoryConformance
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


def _save_body(scope: OwnerScope, body: str) -> None:
    with runtime_persistence.current.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO owner_runtime_state (owner_id, agent_id, payload_json, updated_at) "
                "VALUES (:owner_id, :agent_id, :body, 'original') "
                "ON CONFLICT(owner_id, agent_id) DO UPDATE SET payload_json=excluded.payload_json"
            ),
            {"owner_id": scope.owner_id, "agent_id": scope.agent_id, "body": body},
        )


class TestSqlOwnerSessionRepository(OwnerSessionRepositoryConformance):
    def make_repository(self) -> IOwnerSessionRepository:
        return SqlOwnerSessionRepository(runtime_persistence.current.engine)

    def put_session(self, repository: IOwnerSessionRepository, scope: OwnerScope, session_id: str) -> None:
        _save_body(scope, json.dumps({"session_id": session_id}))


@parametrize_from_dict(
    {
        "missing field": {"body": "{}", "expected": ""},
        "null session": {"body": '{"session_id": null}', "expected": ""},
        "false session": {"body": '{"session_id": false}', "expected": ""},
        "zero session": {"body": '{"session_id": 0}', "expected": ""},
        "numeric session": {"body": '{"session_id": 17}', "expected": "17"},
        "non-object payload": {"body": "[]", "expected": ""},
        "malformed payload": {"body": "not json", "expected": ""},
        "empty payload": {"body": "", "expected": ""},
        "transport fields": {"body": '{"session_id":"saved", "provider_state":{"cursor":7}}', "expected": "saved"},
    }
)
def test_session_read_preserves_legacy_payload_handling_without_normalizing_the_row(body: str, expected: str) -> None:
    engine = runtime_persistence.current.engine
    repository = SqlOwnerSessionRepository(engine)
    _save_body(TEST_SCOPE, body)

    assert repository.get_session_id(TEST_SCOPE) == expected

    with engine.connect() as conn:
        assert conn.execute(text("SELECT payload_json, updated_at FROM owner_runtime_state")).one() == (
            body,
            "original",
        )


def test_session_read_does_not_create_an_owner_state() -> None:
    engine = runtime_persistence.current.engine
    repository = SqlOwnerSessionRepository(engine)

    assert repository.get_session_id(TEST_SCOPE) == ""

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM owner_runtime_state")).scalar_one() == 0


def test_session_read_respects_the_store_deadline() -> None:
    repository = SqlOwnerSessionRepository(runtime_persistence.current.engine)
    _save_body(TEST_SCOPE, '{"session_id":"saved"}')
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.get_session_id(TEST_SCOPE)
    finally:
        store_deadline.reset(token)

    assert repository.get_session_id(TEST_SCOPE) == "saved"
