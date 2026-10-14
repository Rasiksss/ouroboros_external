from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine

from ouroboros.storage.runtime_persistence import runtime_persistence


@contextmanager
def fail_sql(
    match: str, error: Exception, *, engine: Engine | None = None, expect_failure: bool = True
) -> Iterator[None]:
    target = engine if engine is not None else runtime_persistence.current.engine

    failed = False

    def fail(conn, cursor, statement, parameters, context, executemany):
        nonlocal failed
        if match in statement:
            failed = True
            raise error

    event.listen(target, "before_cursor_execute", fail)
    try:
        yield
    finally:
        event.remove(target, "before_cursor_execute", fail)
    assert failed is expect_failure, f"SQL fault {match!r}: triggered={failed}, expected={expect_failure}"
