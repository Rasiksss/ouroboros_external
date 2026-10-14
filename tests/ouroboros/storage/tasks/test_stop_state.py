import time

import pytest
from sqlalchemy import text

from infra.sql_connection import store_deadline
from ouroboros.storage.runtime_models import TaskRequest, TaskStopState
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.task_stop_state import sql_task_stop_reader


@parametrize_from_dict(
    {
        "active": {
            "status": "running",
            "stop": None,
            "expected": TaskStopState(stop_requested=False, is_terminal=False),
        },
        "blank stop": {
            "status": "running",
            "stop": "  ",
            "expected": TaskStopState(stop_requested=False, is_terminal=False),
        },
        "requested stop": {
            "status": "running",
            "stop": "now",
            "expected": TaskStopState(stop_requested=True, is_terminal=False),
        },
        "completed": {
            "status": "completed",
            "stop": None,
            "expected": TaskStopState(stop_requested=False, is_terminal=True),
        },
        "failed": {"status": "failed", "stop": None, "expected": TaskStopState(stop_requested=False, is_terminal=True)},
        "cancelled": {
            "status": "cancelled",
            "stop": "now",
            "expected": TaskStopState(stop_requested=True, is_terminal=True),
        },
        "parked": {
            "status": "waiting_confirmation",
            "stop": None,
            "expected": TaskStopState(stop_requested=False, is_terminal=False),
        },
    }
)
def test_stop_state_comes_from_current_columns(status, stop, expected):
    store = runtime_persistence.current
    store.tasks_repo.create_task(TaskRequest(id="task", scope=TEST_SCOPE.to_dict()))
    with store.engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET status=:status, stop_requested_at=:stop WHERE task_id='task'"),
            {"status": status, "stop": stop},
        )
    assert sql_task_stop_reader().read_task_stop_state("task") == expected


def test_missing_task_is_terminal():
    assert sql_task_stop_reader().read_task_stop_state("missing") == TaskStopState(
        stop_requested=False, is_terminal=True
    )


def test_read_respects_deadline():
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            sql_task_stop_reader().read_task_stop_state("task")
    finally:
        store_deadline.reset(token)
