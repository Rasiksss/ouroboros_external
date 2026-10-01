from __future__ import annotations

import os

import pytest

from ouroboros.storage.runtime_factory import create_runtime_store
from ouroboros.storage.runtime_models import TaskRequest


@pytest.mark.integration
def test_postgres_task_lifecycle() -> None:
    from ouroboros.storage.db_url import collect_runtime_db_urls
    from ouroboros.storage.settings.schema import AppSettings

    settings = AppSettings.model_validate(
        {key: value for key, value in os.environ.items() if key.startswith("OUROBOROS_RUNTIME_DB_URL")}
    )
    urls = collect_runtime_db_urls(settings)
    if not urls or not urls[0].startswith("postgresql"):
        pytest.skip("OUROBOROS_RUNTIME_DB_URL / _1..N not set to a PostgreSQL URL")

    store = create_runtime_store("/tmp/unused", configured_urls=urls)
    task = store.create_task(
        TaskRequest(
            id="pg-task-1",
            type="task",
            chat_id=1,
            text="postgres smoke",
            priority=1,
            source="internal",
        )
    )
    assert task.task_id == "pg-task-1"

    claimed = store.claim_next_task(worker_id=0)
    assert claimed is not None
    assert claimed.status == "running"

    completed = store.complete_task("pg-task-1", result="ok")
    assert completed is not None
    assert completed.status == "completed"
