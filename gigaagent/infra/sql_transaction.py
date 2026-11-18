from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.engine import Connection, Engine

from infra.sql_connection import connection_deadline, sql_connection


def _execution() -> tuple[int, asyncio.Task | None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@dataclass
class _TransactionScope:
    connection: Connection
    execution: tuple[int, asyncio.Task | None]
    active: bool = True
    aborted: bool = False


class SqlTransactionManager:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._scope: ContextVar[_TransactionScope | None] = ContextVar("sql_transaction", default=None)

    def _current(self) -> Connection | None:
        scope = self._scope.get()
        if scope is None:
            return None
        if scope.execution != _execution() or not scope.active:
            raise RuntimeError("SQL transaction belongs to a different or finished execution")
        if scope.aborted:
            raise RuntimeError("SQL transaction was aborted during savepoint completion")
        return scope.connection

    @property
    def transaction(self) -> AbstractContextManager[None]:
        return self._transaction()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        current = self._current()
        if current is not None:
            with connection_deadline(current, restore_timeout=True):
                nested = current.begin_nested()
                try:
                    yield
                except BaseException:
                    try:
                        nested.rollback()
                    except BaseException:
                        self._abort(current)
                        raise
                    raise
                else:
                    try:
                        nested.commit()
                    except BaseException:
                        self._abort(current)
                        raise
            return
        with sql_connection(self._engine) as conn:
            scope = _TransactionScope(conn, _execution())
            token = self._scope.set(scope)
            try:
                with conn.begin():
                    yield
                    if scope.aborted:
                        raise RuntimeError("SQL transaction was aborted during savepoint completion")
            finally:
                scope.active = False
                self._scope.reset(token)

    def _abort(self, conn: Connection) -> None:
        scope = self._scope.get()
        if scope is not None:
            scope.aborted = True
        conn.rollback()

    @contextmanager
    def connection(self, *, deadline: float | None = None) -> Iterator[Connection]:
        current = self._current()
        if current is not None:
            with connection_deadline(current, deadline=deadline, restore_timeout=True):
                yield current
            return
        with sql_connection(self._engine, deadline=deadline) as conn:
            yield conn
