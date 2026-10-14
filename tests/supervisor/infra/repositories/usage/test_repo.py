from __future__ import annotations

import time

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from infra.sql_connection import store_deadline
from ouroboros.storage.runtime_models import LlmUsageRecord
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.interfaces.usage import IUsageRepository
from tests.helpers.usage import sql_usage_repository
from tests.helpers.usage_conformance import SCOPE, UsageRepositoryConformance


class TestSqlUsageRepository(UsageRepositoryConformance):
    def make_repository(self) -> IUsageRepository:
        return sql_usage_repository()


def test_duplicate_usage_id_does_not_overwrite_the_original() -> None:
    repository = sql_usage_repository()
    repository.record_usage(LlmUsageRecord(usage_id="same", prompt_tokens=17))

    with pytest.raises(IntegrityError):
        repository.record_usage(LlmUsageRecord(usage_id="same", prompt_tokens=99))

    assert repository.sum_usage_stats()["spent_tokens_prompt"] == 17


def test_expired_deadline_refuses_usage_without_persisting_it() -> None:
    repository = sql_usage_repository()
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.record_usage(LlmUsageRecord(prompt_tokens=17))
    finally:
        store_deadline.reset(token)

    assert repository.sum_usage_stats()["spent_calls"] == 0


def test_deadline_expiring_before_commit_rolls_back_usage() -> None:
    engine = runtime_persistence.current.engine
    repository = sql_usage_repository(engine)

    def expire_after_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO llm_usage"):
            conn.info["operation_deadline"] = time.monotonic() - 1

    token = store_deadline.set(time.monotonic() + 60)
    event.listen(engine, "after_cursor_execute", expire_after_insert)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            repository.record_usage(LlmUsageRecord(owner_id=SCOPE.owner_id, agent_id=SCOPE.agent_id, prompt_tokens=17))
    finally:
        event.remove(engine, "after_cursor_execute", expire_after_insert)
        store_deadline.reset(token)

    assert repository.sum_usage_stats(scope=SCOPE)["spent_calls"] == 0
