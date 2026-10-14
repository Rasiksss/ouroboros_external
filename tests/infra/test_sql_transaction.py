import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from infra.sql_connection import store_deadline
from infra.sql_transaction import SqlTransactionManager
from ouroboros.storage.runtime_persistence import runtime_persistence


def test_repositories_share_connection_and_outer_rollback():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    with pytest.raises(ValueError), transaction_manager.transaction:
        with transaction_manager.connection() as first:
            first.execute(
                text(
                    "INSERT INTO audit_events (timestamp, event_type, payload_json, event_id) VALUES ('now', 'test', '{}', 'rolled-back')"
                )
            )
        with transaction_manager.connection() as second:
            assert second is first
            assert second.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 1
        raise ValueError("abort")
    with transaction_manager.connection() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 0


def test_nested_failure_rolls_back_only_savepoint():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    with transaction_manager.transaction:
        with transaction_manager.connection() as conn:
            conn.execute(
                text(
                    "INSERT INTO audit_events (timestamp, event_type, payload_json, event_id) VALUES ('now', 'test', '{}', 'outer')"
                )
            )
        with pytest.raises(IntegrityError), transaction_manager.transaction:
            with transaction_manager.connection() as conn:
                conn.execute(
                    text(
                        "INSERT INTO audit_events (timestamp, event_type, payload_json, event_id) VALUES ('now', 'test', '{}', 'inner')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO audit_events (timestamp, event_type, payload_json, event_id) VALUES ('now', 'test', '{}', 'outer')"
                    )
                )
        with transaction_manager.connection() as conn:
            assert conn.execute(text("SELECT event_id FROM audit_events")).scalars().all() == ["outer"]
    with transaction_manager.connection() as conn:
        assert conn.execute(text("SELECT event_id FROM audit_events")).scalars().all() == ["outer"]


def test_concurrent_threads_have_independent_transactions():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    barrier = Barrier(2)

    def run(fail):
        try:
            with transaction_manager.transaction, transaction_manager.connection() as conn:
                conn.execute(
                    text(
                        "INSERT INTO audit_events (timestamp, event_type, payload_json, event_id) VALUES ('now', 'test', '{}', :id)"
                    ),
                    {"id": str(fail)},
                )
                barrier.wait(timeout=10)
                if fail:
                    raise ValueError("abort")
        except ValueError:
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, [False, True]))
    with transaction_manager.connection() as conn:
        assert conn.execute(text("SELECT event_id FROM audit_events")).scalars().all() == ["False"]


def test_copied_thread_context_cannot_borrow_connection():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)

    def borrow():
        with transaction_manager.connection():
            pytest.fail("borrowed another execution's connection")

    with ThreadPoolExecutor(max_workers=1) as pool, transaction_manager.transaction:
        with pytest.raises(RuntimeError, match="execution"):
            pool.submit(copy_context().run, borrow).result(timeout=10)


def test_child_asyncio_task_cannot_borrow_connection():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)

    async def borrow():
        with transaction_manager.connection():
            pytest.fail("borrowed another execution's connection")

    async def run():
        with transaction_manager.transaction:
            with pytest.raises(RuntimeError, match="execution"):
                await asyncio.create_task(borrow())

    asyncio.run(run())


def test_commit_deadline_rolls_back_and_connection_is_reusable(monkeypatch):
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    now = time.monotonic()
    token = store_deadline.set(now + 5)
    try:
        with pytest.raises(TimeoutError), transaction_manager.transaction:
            with transaction_manager.connection() as conn:
                conn.execute(
                    text(
                        "INSERT INTO audit_events (timestamp, event_type, payload_json, event_id) VALUES ('now', 'test', '{}', 'late')"
                    )
                )
            monkeypatch.setattr(time, "monotonic", lambda: now + 10)
    finally:
        store_deadline.reset(token)
    with transaction_manager.transaction, transaction_manager.connection() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 0


def test_joined_read_honours_shorter_deadline():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    with transaction_manager.transaction:
        token = store_deadline.set(time.monotonic() - 1)
        try:
            with pytest.raises(TimeoutError), transaction_manager.connection():
                pytest.fail("ignored the read deadline")
        finally:
            store_deadline.reset(token)
        with transaction_manager.connection() as conn:
            assert conn.execute(text("SELECT 1")).scalar_one() == 1


def test_managers_on_same_engine_do_not_share_transactions():
    engine = runtime_persistence.current.engine
    first, second = SqlTransactionManager(engine), SqlTransactionManager(engine)
    with first.transaction, first.connection() as one, second.connection() as two:
        assert one is not two


def test_expired_inner_deadline_cannot_prevent_savepoint_rollback(monkeypatch):
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    now = time.monotonic()
    with transaction_manager.transaction:
        token = store_deadline.set(now + 5)
        try:
            with pytest.raises(ValueError, match="abort inner"), transaction_manager.transaction:
                with transaction_manager.connection() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO audit_events (timestamp, event_type, payload_json, event_id) "
                            "VALUES ('now', 'test', '{}', 'inner')"
                        )
                    )
                monkeypatch.setattr(time, "monotonic", lambda: now + 10)
                raise ValueError("abort inner")
        finally:
            store_deadline.reset(token)
        with transaction_manager.connection() as conn:
            assert conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 0


def test_sql_error_with_deadline_can_roll_back_savepoint():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    token = store_deadline.set(time.monotonic() + 30)
    try:
        with transaction_manager.transaction:
            with pytest.raises(IntegrityError), transaction_manager.transaction:
                with transaction_manager.connection() as conn:
                    conn.execute(text("INSERT INTO audit_events (event_id) VALUES ('invalid')"))
            with transaction_manager.connection() as conn:
                assert conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 0
    finally:
        store_deadline.reset(token)


def test_inner_deadline_restores_server_statement_timeout():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    with transaction_manager.transaction:
        with transaction_manager.connection() as conn:
            conn.exec_driver_sql("SET LOCAL statement_timeout='20s'")
        token = store_deadline.set(time.monotonic() + 5)
        try:
            with transaction_manager.transaction, transaction_manager.connection() as conn:
                assert conn.execute(text("SELECT current_setting('statement_timeout')")).scalar_one() != "20s"
        finally:
            store_deadline.reset(token)
        with transaction_manager.connection() as conn:
            assert conn.execute(text("SHOW statement_timeout")).scalar_one() == "20s"


def test_deadline_at_savepoint_release_cannot_commit_inner_write(monkeypatch):
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    now = time.monotonic()
    with pytest.raises(RuntimeError, match="aborted"), transaction_manager.transaction:
        token = store_deadline.set(now + 5)
        try:
            with pytest.raises(TimeoutError), transaction_manager.transaction:
                with transaction_manager.connection() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO audit_events (timestamp, event_type, payload_json, event_id) "
                            "VALUES ('now', 'test', '{}', 'inner')"
                        )
                    )
                monkeypatch.setattr(time, "monotonic", lambda: now + 10)
        finally:
            store_deadline.reset(token)
    with transaction_manager.connection() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 0
