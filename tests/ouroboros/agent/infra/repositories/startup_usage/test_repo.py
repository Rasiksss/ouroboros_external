import time

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from infra.sql_connection import store_deadline
from new_core.identity import OwnerScope
from ouroboros.agent.infra.repositories.startup_usage.repo import SqlStartupUsageRepository
from ouroboros.agent.interfaces.startup_usage import IStartupUsageRepository, StartupUsage
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.startup_usage_conformance import StartupUsageRepositoryConformance


def _seed(scope: OwnerScope, body: str) -> None:
    with runtime_persistence.current.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO owner_runtime_state (owner_id, agent_id, payload_json, updated_at) "
                "VALUES (:owner, :agent, :body, 'original') "
                "ON CONFLICT(owner_id, agent_id) DO UPDATE SET payload_json=excluded.payload_json"
            ),
            {"owner": scope.owner_id, "agent": scope.agent_id, "body": body},
        )


class TestSqlStartupUsageRepository(StartupUsageRepositoryConformance):
    def make_repository(self) -> IStartupUsageRepository:
        return SqlStartupUsageRepository(runtime_persistence.current.engine)

    def seed(self, repository: IStartupUsageRepository, scope: OwnerScope, usage: StartupUsage) -> None:
        _seed(scope, usage.model_dump_json())


@parametrize_from_dict(
    {
        "empty object": {"body": "{}", "calls": 0},
        "malformed JSON": {"body": "not json", "calls": 0},
        "array": {"body": "[]", "calls": 0},
        "numeric string": {"body": '{"spent_calls":"7"}', "calls": 7},
        "unrelated malformed fields": {"body": '{"spent_calls":7,"owner_scope":42,"provider_state":[]}', "calls": 7},
        "checkpoint without session": {
            "body": '{"spent_calls":7,"working_retrieval_cleanup":{"last_run_at":"saved"}}',
            "calls": 7,
        },
    }
)
def test_diagnostic_reads_counters_without_normalizing_or_minting_a_session(body: str, calls: int) -> None:
    engine = runtime_persistence.current.engine
    _seed(TEST_SCOPE, body)

    assert SqlStartupUsageRepository(engine).get_usage(TEST_SCOPE) == StartupUsage(spent_calls=calls)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT payload_json, updated_at FROM owner_runtime_state")).one() == (
            body,
            "original",
        )


def test_missing_owner_stays_missing_after_the_diagnostic_read() -> None:
    engine = runtime_persistence.current.engine

    assert SqlStartupUsageRepository(engine).get_usage(TEST_SCOPE) == StartupUsage()

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM owner_runtime_state")).scalar_one() == 0


@parametrize_from_dict(
    {"null counter": {"value": "null"}, "invalid counter": {"value": '"bad"'}, "fractional counter": {"value": "1.5"}}
)
def test_invalid_counters_remain_a_diagnostic_error(value: str) -> None:
    engine = runtime_persistence.current.engine
    body = '{"spent_calls":' + value + "}"
    _seed(TEST_SCOPE, body)

    with pytest.raises(ValidationError):
        SqlStartupUsageRepository(engine).get_usage(TEST_SCOPE)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT payload_json FROM owner_runtime_state")).scalar_one() == body


def test_startup_usage_read_respects_the_store_deadline() -> None:
    repository = SqlStartupUsageRepository(runtime_persistence.current.engine)
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.get_usage(TEST_SCOPE)
    finally:
        store_deadline.reset(token)
