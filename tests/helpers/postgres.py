"""Postgres test helpers extracted from test_sql_runtime_store_postgres."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from ouroboros.storage.runtime_persistence import create_runtime_persistence
from tests.helpers.schema_harness import _ABSENT_DATABASE
from tests.helpers.shared import TEST_SCOPE, scope_with


def postgres_urls() -> list[str]:
    from ouroboros.storage.db_url import collect_runtime_db_urls
    from ouroboros.storage.settings.schema import AppSettings

    settings = AppSettings.model_validate(
        {key: value for key, value in os.environ.items() if key.startswith("OUROBOROS_RUNTIME_DB_URL")}
    )
    urls = collect_runtime_db_urls(settings)
    if not urls or not urls[0].startswith("postgresql"):
        raise RuntimeError(_ABSENT_DATABASE)
    return urls


@pytest.fixture
def postgres_task_scope():
    urls = postgres_urls()
    scope = scope_with(TEST_SCOPE, owner_id=f"postgres-test-{uuid.uuid4().hex}")
    try:
        yield urls, scope
    finally:
        store = create_runtime_persistence(configured_urls=urls)
        with store.engine.begin() as conn:
            conn.execute(text("DELETE FROM tasks WHERE owner_id=:owner_id"), {"owner_id": scope.owner_id})
            for table in ("memory", "task_episodes", "audit_events"):
                conn.execute(text(f"DELETE FROM {table} WHERE owner_id=:owner_id"), {"owner_id": scope.owner_id})
