from sqlalchemy.engine import Engine

from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.tasks.interfaces import ITaskRepository
from tests.helpers.conversations import make_sql_runtime_persistence


def sql_task_stop_reader(engine: Engine | None = None) -> ITaskRepository:
    return (
        make_sql_runtime_persistence(engine, db_url="").tasks_repo
        if engine is not None
        else runtime_persistence.current.tasks_repo
    )
