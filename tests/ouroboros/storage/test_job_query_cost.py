from sqlalchemy import event, text

from ouroboros.storage.runtime_models import TaskRequest, TaskStatus
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


def _flush_statistics(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("SELECT pg_stat_force_next_flush()")
    cursor.close()
    dbapi_connection.rollback()


def _task_tuples_read(engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT t.seq_tup_read + COALESCE(SUM(i.idx_tup_read), 0) FROM pg_stat_user_tables t "
                    "LEFT JOIN pg_stat_user_indexes i ON i.relid=t.relid "
                    "WHERE t.relid='tasks'::regclass GROUP BY t.seq_tup_read"
                )
            ).scalar_one()
        )


@parametrize_from_dict({"list": {"operation": "list"}, "stop": {"operation": "stop"}})
def test_job_management_cost_grows_linearly_with_completed_history(operation):
    store = runtime_persistence.initialize()
    event.listen(store.engine, "checkin", _flush_statistics)
    try:
        costs = []
        previous_count = 0
        for count in (100, 1000):
            with store.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO tasks (task_id, job_id, owner_id, agent_id, surface_id, chat_id, "
                        "status, task_type, parent_task_id, detached, created_at, input_json, payload_json, "
                        "metadata_json) VALUES (:id, :id, :owner_id, :agent_id, :surface_id, :chat_id, "
                        "'completed', 'task', '', true, '2026-01-01', '{}', '{}', '{}')"
                    ),
                    [{"id": f"old-{i}", **TEST_SCOPE.to_dict()} for i in range(previous_count, count)],
                )
            active_id = f"active-{count}"
            store.tasks_repo.create_task(TaskRequest(id=active_id, detached=True, scope=TEST_SCOPE.to_dict()))
            # DELETE-based suite cleanup leaves dead tuples and stale planner statistics.
            with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text("VACUUM (ANALYZE) tasks"))
            before = _task_tuples_read(store.engine)
            rows = (
                store.tasks_repo.list_current_jobs(scope=TEST_SCOPE, limit=21, active_only=True)
                if operation == "list"
                else store.tasks_repo.stop_jobs(scope=TEST_SCOPE)
            )
            costs.append(_task_tuples_read(store.engine) - before)
            assert [row.task_id for row in rows] == [active_id]
            previous_count = count
            store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id=active_id)
    finally:
        event.remove(store.engine, "checkin", _flush_statistics)

    assert costs[0] > 0, costs
    assert costs[1] < costs[0] * 20, costs


@parametrize_from_dict({"active": {"status": "queued"}, "terminal": {"status": "completed"}})
@parametrize_from_dict({"canonical": {"legacy": False}, "legacy": {"legacy": True}})
def test_job_listing_does_not_revive_an_older_active_attempt(status: TaskStatus, legacy: bool) -> None:
    store = runtime_persistence.initialize()
    original = store.tasks_repo.create_task(TaskRequest(id="job", scope=TEST_SCOPE.to_dict()))
    if legacy:
        with store.engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET job_id='' WHERE task_id='job'"))
    store.tasks_repo.upsert_task(
        original.model_copy(
            update={"task_id": "next-attempt", "lineage_ordinal": 1, "status": status},
        )
    )

    current = store.tasks_repo.get_current_job("job", scope=TEST_SCOPE)

    assert current is not None and current.task_id == "next-attempt"
    assert store.tasks_repo.list_current_jobs(scope=TEST_SCOPE, limit=10) == [current]
    assert store.tasks_repo.list_current_jobs(scope=TEST_SCOPE, limit=10, active_only=True) == (
        [current] if status == "queued" else []
    )
