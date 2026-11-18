from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.exc import DBAPIError

store_deadline: ContextVar[float | None] = ContextVar("store_deadline", default=None)


@contextmanager
def sql_connection(engine: Engine, *, deadline: float | None = None) -> Iterator[Connection]:
    with engine.connect() as conn, connection_deadline(conn, deadline=deadline):
        yield conn


@contextmanager
def connection_deadline(
    conn: Connection, *, deadline: float | None = None, restore_timeout: bool = False
) -> Iterator[Connection]:
    stored = store_deadline.get()
    deadline = stored if stored is not None else deadline
    if deadline is None:
        yield conn
        return
    previous = conn.info.get("operation_deadline")
    if previous is not None and deadline >= previous:
        yield conn
        return
    deadline = min(previous, deadline) if previous is not None else deadline
    previous_timeout: str | None = None

    def remaining(_conn: Connection) -> int:
        seconds = conn.info["operation_deadline"] - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Runtime store deadline expired")
        return max(1, int(seconds * 1000))

    def before_cursor(_conn: Connection, cursor: DBAPICursor, statement: str, *_args: object) -> None:
        # Recovery must work after both a deadline and a PostgreSQL statement failure.
        if statement.startswith("ROLLBACK TO SAVEPOINT "):
            return
        ms = remaining(conn)
        cursor.execute(f"SET LOCAL statement_timeout={ms}")

    def before_commit(_conn: Connection) -> None:
        try:
            remaining(conn)
        except TimeoutError:
            # SQLAlchemy deactivates the transaction when this hook raises.
            conn.connection.rollback()
            raise

    if restore_timeout and previous is None and deadline > time.monotonic():
        previous_timeout = str(conn.exec_driver_sql("SHOW statement_timeout").scalar_one())
    conn.info["operation_deadline"] = deadline
    event.listen(conn, "before_cursor_execute", before_cursor)
    event.listen(conn, "commit", before_commit)
    try:
        remaining(conn)
        yield conn
    finally:
        failed = sys.exc_info()[0] is not None
        event.remove(conn, "before_cursor_execute", before_cursor)
        event.remove(conn, "commit", before_commit)
        if not conn.invalidated:
            if previous is None:
                conn.info.pop("operation_deadline", None)
            else:
                conn.info["operation_deadline"] = previous
            if previous_timeout is not None and conn.in_transaction():
                try:
                    conn.execute(
                        text("SELECT set_config('statement_timeout', :timeout, true)"), {"timeout": previous_timeout}
                    )
                except DBAPIError:
                    if not failed:
                        raise
