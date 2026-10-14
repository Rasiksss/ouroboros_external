import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, text

from infra.sql_connection import store_deadline
from ouroboros.app.infra.repositories.deployment_keyspace.repo import SqlDeploymentKeyspaceRepository
from ouroboros.app.interfaces.deployment_keyspace import IDeploymentKeyspaceRepository
from ouroboros.storage.runtime_models import DeploymentKeyspace
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.deployment_keyspace_conformance import DeploymentKeyspaceRepositoryConformance
from tests.helpers.parametrize import parametrize_from_dict


class TestSqlDeploymentKeyspaceRepository(DeploymentKeyspaceRepositoryConformance):
    def make_repository(self) -> IDeploymentKeyspaceRepository:
        return SqlDeploymentKeyspaceRepository(runtime_persistence.current.engine)


def test_concurrent_first_starts_return_the_same_winner() -> None:
    engine = runtime_persistence.current.engine
    barrier = threading.Barrier(2)

    def record(prefix: str) -> DeploymentKeyspace:
        repository = SqlDeploymentKeyspaceRepository(engine)
        barrier.wait(timeout=5)
        return repository.record_deployment_keyspace(DeploymentKeyspace(backend="s3", key_prefix=prefix))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(record, ("first", "second")))

    assert results[0] == results[1]
    assert results[0].key_prefix in {"first", "second"}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM deployment_keyspace")).scalar_one() == 1


@parametrize_from_dict({"record": {"replace": False}, "replace": {"replace": True}})
def test_write_returns_the_persisted_value_after_commit(replace: bool) -> None:
    engine = runtime_persistence.current.engine
    repository = SqlDeploymentKeyspaceRepository(engine)
    requested = DeploymentKeyspace(backend="s3", key_prefix="requested")

    def replace_before_read(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT * FROM deployment_keyspace"):
            with engine.begin() as other:
                other.execute(text("UPDATE deployment_keyspace SET key_prefix='concurrent'"))

    event.listen(engine, "before_cursor_execute", replace_before_read)
    try:
        write = repository.replace_deployment_keyspace if replace else repository.record_deployment_keyspace
        stored = write(requested)
    finally:
        event.remove(engine, "before_cursor_execute", replace_before_read)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM deployment_keyspace")).scalar_one() == 1

    assert stored.key_prefix == "concurrent"
    assert requested.key_prefix == "requested"


@parametrize_from_dict({"record": {"replace": False}, "replace": {"replace": True}})
def test_deadline_before_commit_rolls_back_the_write(replace: bool) -> None:
    engine = runtime_persistence.current.engine
    repository = SqlDeploymentKeyspaceRepository(engine)
    original = None
    if replace:
        original = repository.record_deployment_keyspace(DeploymentKeyspace(backend="s3", key_prefix="original"))

    def expire_after_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith(("INSERT INTO deployment_keyspace", "UPDATE deployment_keyspace")):
            conn.info["operation_deadline"] = time.monotonic() - 1

    token = store_deadline.set(time.monotonic() + 60)
    event.listen(engine, "after_cursor_execute", expire_after_write)
    try:
        write = repository.replace_deployment_keyspace if replace else repository.record_deployment_keyspace
        with pytest.raises(TimeoutError, match="deadline expired"):
            write(DeploymentKeyspace(backend="s3", key_prefix="changed"))
    finally:
        event.remove(engine, "after_cursor_execute", expire_after_write)
        store_deadline.reset(token)

    assert repository.get_deployment_keyspace() == original
