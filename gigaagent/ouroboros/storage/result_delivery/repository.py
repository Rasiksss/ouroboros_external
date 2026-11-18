"""Durable result publication and delivery receipts."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection

from infra.sql_connection import connection_deadline, store_deadline
from infra.sql_transaction import SqlTransactionManager
from ouroboros.storage import task_transitions
from ouroboros.storage.result_delivery.interfaces import IResultDeliveryRepository
from ouroboros.storage.runtime_backend import TERMINAL_CLAIM_MAX_BATCH
from ouroboros.storage.runtime_models import TaskRecord
from ouroboros.storage.runtime_serialization import dump_json as _dump_json
from ouroboros.storage.tasks.records import _row_to_task


class SqlResultDeliveryRepository(IResultDeliveryRepository):
    def __init__(self, transaction_manager: SqlTransactionManager) -> None:
        self.transaction_manager = transaction_manager

    @property
    def transaction(self) -> AbstractContextManager[None]:
        return self.transaction_manager.transaction

    @contextmanager
    def _connection(self, *, deadline: float | None = None) -> Iterator[Connection]:
        with (
            self.transaction_manager.connection() as conn,
            connection_deadline(conn, deadline=deadline, restore_timeout=True),
        ):
            yield conn

    @contextmanager
    def _operation(self, *, deadline: float | None = None) -> Iterator[Connection]:
        inherited = store_deadline.get()
        token = store_deadline.set(inherited if inherited is not None else deadline)
        try:
            with self.transaction, self.transaction_manager.connection() as conn:
                yield conn
        finally:
            store_deadline.reset(token)

    def claim_terminal_jobs(self, *, limit: int = TERMINAL_CLAIM_MAX_BATCH) -> list[TaskRecord]:
        claim_limit = min(int(limit), TERMINAL_CLAIM_MAX_BATCH)
        if claim_limit <= 0:
            return []
        terminal_binds, terminal_params = task_transitions.terminal_status_binds("publish")
        with self._operation() as conn:
            rows = conn.execute(
                text(
                    f"SELECT * FROM tasks AS candidate WHERE detached AND terminal_published_at IS NULL AND status IN ({terminal_binds}) "
                    "AND (terminal_publish_after IS NULL OR terminal_publish_after<=:now) "
                    "AND NOT EXISTS (SELECT 1 FROM tasks newer WHERE newer.owner_id=candidate.owner_id "
                    "AND newer.job_id=candidate.job_id AND newer.lineage_ordinal>candidate.lineage_ordinal) "
                    "ORDER BY finished_at, task_id LIMIT :limit FOR UPDATE SKIP LOCKED"
                ),
                {"now": task_transitions.database_now(conn).isoformat(), "limit": claim_limit, **terminal_params},
            ).fetchall()
            expires = (task_transitions.database_now(conn) + datetime.timedelta(seconds=60)).isoformat()
            claimed = [_row_to_task(row) for row in rows]
            for record in claimed:
                record.terminal_publish_claim = uuid.uuid4().hex
                record.terminal_publish_after = expires
            if claimed:
                conn.execute(
                    text(
                        "UPDATE tasks SET terminal_publish_claim=:claim, terminal_publish_after=:expires WHERE task_id=:id"
                    ),
                    [{"id": r.task_id, "claim": r.terminal_publish_claim, "expires": expires} for r in claimed],
                )
        return claimed

    def _terminal_claim(
        self, conn: Connection, task_id: str, claim: str
    ) -> tuple[TaskRecord, datetime.datetime] | None:
        if not claim:
            return None
        terminal, params = task_transitions.terminal_status_binds()
        row = conn.execute(
            text(
                f"SELECT * FROM tasks WHERE task_id=:id AND detached AND status IN ({terminal}) "
                "AND terminal_publish_claim=:claim AND terminal_published_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM tasks newer WHERE newer.owner_id=tasks.owner_id "
                "AND newer.job_id=tasks.job_id AND newer.lineage_ordinal>tasks.lineage_ordinal) FOR UPDATE"
            ),
            {"id": task_id, "claim": claim, **params},
        ).fetchone()
        now = task_transitions.database_now(conn)  # After any row-lock wait, never transaction-start time.
        if (
            row is None
            or not row.terminal_publish_after
            or datetime.datetime.fromisoformat(row.terminal_publish_after) <= now
        ):
            return None
        return _row_to_task(row), now

    def terminal_publication_remaining(self, task_id: str, *, claim: str) -> float:
        with self._connection() as conn:
            live = self._terminal_claim(conn, task_id, claim)
            return (
                (datetime.datetime.fromisoformat(str(live[0].terminal_publish_after)) - live[1]).total_seconds()
                if live
                else 0.0
            )

    def _publication_live_sql(self) -> str:
        return "terminal_publish_after::timestamptz > clock_timestamp()"

    def record_terminal_delivery(self, task_id: str, *, claim: str, item: str, disposition: str) -> bool:
        if disposition not in {"accepted", "unavailable", "retryable"}:
            raise ValueError("invalid terminal disposition")
        with self._operation() as conn:
            live = self._terminal_claim(conn, task_id, claim)
            if live is None:
                return False
            record, _now = live
            delivery = record.terminal_delivery
            record.metadata["terminal_delivery"] = delivery
            if delivery.get(item) not in {"accepted", "unavailable"}:
                delivery[item] = disposition
            applied = conn.execute(
                text("UPDATE tasks SET metadata_json=:metadata WHERE task_id=:id AND " + self._publication_live_sql()),
                {"metadata": _dump_json(record.metadata), "id": task_id},
            ).rowcount
            return bool(applied)

    def finish_terminal_publication(
        self, task_id: str, *, claim: str, accepted: bool, retry_after_sec: int = 5
    ) -> bool:
        with self._operation() as conn:
            live = self._terminal_claim(conn, task_id, claim)
            if live is None:
                return False
            record, now = live
            delivery = record.terminal_delivery
            if accepted and (
                delivery.get("text") != "accepted"
                or any(
                    delivery.get(item["id"]) not in {"accepted", "unavailable"} for item in record.terminal_attachments
                )
            ):
                return False
            after = (
                None if accepted else (now + datetime.timedelta(seconds=min(60, max(1, retry_after_sec)))).isoformat()
            )
            applied = conn.execute(
                text(
                    "UPDATE tasks SET terminal_published_at=:published, terminal_publish_claim=NULL, "
                    "terminal_publish_after=:after WHERE task_id=:task_id AND " + self._publication_live_sql()
                ),
                {"published": now.isoformat() if accepted else None, "after": after, "task_id": task_id},
            ).rowcount
            return bool(applied)
