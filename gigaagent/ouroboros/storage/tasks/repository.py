"""SQL persistence for the task lifecycle."""

from __future__ import annotations

import datetime
import logging
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from infra.sql_connection import store_deadline
from infra.sql_transaction import SqlTransactionManager
from new_core.identity import OwnerScope
from ouroboros.storage import task_transitions
from ouroboros.storage.dialect import (
    ACTIVE_STATUS_SQL,
    CURRENT_EXECUTION_SQL,
    claim_select_sql,
    execution_authority_sql,
    lease_live_sql,
)
from ouroboros.storage.message_field_crypto import encrypt_field
from ouroboros.storage.runtime_backend import (
    STORE_FAILURES,
    TERMINAL_CLAIM_MAX_BATCH,
)
from ouroboros.storage.runtime_models import (
    TaskRecord,
    TaskRequest,
    TaskStatus,
    TaskStopState,
)
from ouroboros.storage.runtime_serialization import (
    dump_json as _dump_json,
)
from ouroboros.storage.tasks.interfaces import ITaskRepository
from ouroboros.storage.tasks.records import _STATUS_EXTRA_COLUMNS, _row_to_task, _task_from_request, _task_params
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


def _logical_job_id_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"COALESCE(NULLIF({prefix}job_id, ''), {prefix}task_id)"


#: A stop reaches a run through its own thread; these states have none to reach. A parked record
#: has none either, but it holds a workspace only a run removes, so it is queued with the stop.
_STOPPABLE_WITHOUT_A_RUN = "'requested','queued','scheduled','interrupted'"
_HAS_CONFIRMATION_SNAPSHOT_SQL = (
    "EXISTS (SELECT 1 FROM tool_confirmations AS parked "
    "WHERE parked.task_id=tasks.task_id AND parked.run_state_json<>'')"
)


def _metadata_without_run_clock_sql() -> str:
    return "(metadata_json::jsonb - 'run_started_at')::text"


def _current_attempt_order_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"{prefix}lineage_ordinal DESC"


def _job_chronology_sql(alias: str = "tasks") -> str:
    return ", ".join(
        f"COALESCE((SELECT origin.{column} FROM tasks AS origin WHERE origin.owner_id={alias}.owner_id "
        f"AND origin.job_id={_logical_job_id_sql(alias)} AND origin.job_id<>'' AND origin.lineage_ordinal=0), "
        f"(SELECT origin.{column} FROM tasks AS origin WHERE origin.owner_id={alias}.owner_id "
        f"AND origin.task_id={_logical_job_id_sql(alias)} AND origin.job_id='' "
        f"AND origin.lineage_ordinal=0)) AS logical_{column}"
        for column in ("created_at", "started_at")
    )


_OWNED = "owner_id=:owner_id AND agent_id=:agent_id"
_CHATTED = f"{_OWNED} AND chat_id=:chat_id"
_SCOPED = f"{_OWNED} AND surface_id=:surface_id AND chat_id=:chat_id"


def _owned_params(scope: OwnerScope) -> dict[str, Any]:
    return {"owner_id": scope.owner_id, "agent_id": scope.agent_id}


def _chat_params(scope: OwnerScope) -> dict[str, Any]:
    return {**_owned_params(scope), "chat_id": scope.chat_id}


def _scoped_params(scope: OwnerScope) -> dict[str, Any]:
    return {**_chat_params(scope), "surface_id": str(scope.surface_id)}


class SqlTaskRepository(ITaskRepository):
    """Persistence for task execution and cancellation."""

    def __init__(self, transaction_manager: SqlTransactionManager) -> None:
        self.transaction_manager = transaction_manager

    @contextmanager
    def _connection(self, *, deadline: float | None = None) -> Iterator[Connection]:
        with self.transaction_manager.connection(deadline=deadline) as conn:
            yield conn

    @property
    def transaction(self) -> AbstractContextManager[None]:
        return self.transaction_manager.transaction

    @contextmanager
    def _operation(self, *, deadline: float | None = None) -> Iterator[Connection]:
        inherited = store_deadline.get()
        token = store_deadline.set(inherited if inherited is not None else deadline)
        try:
            with self.transaction, self.transaction_manager.connection() as conn:
                yield conn
        finally:
            store_deadline.reset(token)

    def _finish_retry_transition(
        self, conn: Connection, row: Any, successor: TaskRequest | None
    ) -> tuple[TaskRecord, bool] | None:
        if row is None:
            return None
        task = _row_to_task(row)
        if task.detached:
            self._cancel_requested_children(conn, task)
        created = successor is not None and not task.stop_requested_at
        if successor is not None and created:
            successor.job_id = task.job_id
            next_task = _task_from_request(successor, status="queued")
            next_task.lineage_ordinal = task.lineage_ordinal + 1
            self._insert_task(conn, next_task)
        return task, created

    def list_terminal_memory(self, *, limit: int = TERMINAL_CLAIM_MAX_BATCH) -> list[TaskRecord]:
        pending = "COALESCE(metadata_json::jsonb->>'terminal_memory_after', '')"
        done = "COALESCE(metadata_json::jsonb->>'terminal_memory_done', 'false')='false'"
        with self._connection() as conn:
            rows = conn.execute(
                text(
                    f"SELECT * FROM tasks WHERE detached AND status='completed' AND {done} AND {pending}<=:now "
                    "AND NOT EXISTS (SELECT 1 FROM tasks newer WHERE newer.owner_id=tasks.owner_id "
                    "AND newer.job_id=tasks.job_id AND newer.lineage_ordinal>tasks.lineage_ordinal) "
                    "ORDER BY finished_at, task_id LIMIT :limit"
                ),
                {"now": task_transitions.database_now(conn).isoformat(), "limit": max(0, min(50, limit))},
            ).fetchall()
            return [_row_to_task(row) for row in rows]

    def finish_terminal_memory(self, task_id: str, *, filed: bool) -> bool:
        with self._operation() as conn:
            row = conn.execute(
                text(
                    "SELECT * FROM tasks WHERE task_id=:id AND detached AND status='completed' "
                    "AND NOT EXISTS (SELECT 1 FROM tasks newer WHERE newer.owner_id=tasks.owner_id "
                    "AND newer.job_id=tasks.job_id AND newer.lineage_ordinal>tasks.lineage_ordinal) FOR UPDATE"
                ),
                {"id": task_id},
            ).fetchone()
            if row is None:
                return False
            record = _row_to_task(row)
            claimed = conn.execute(
                text("SELECT 1 FROM audit_events WHERE event_id=:id"),
                {"id": f"memory-distillation:{record.owner_id}:{record.job_id}"},
            ).scalar()
            done = bool(record.metadata.get("terminal_memory_done") or (filed and claimed))
            record.metadata.update(
                terminal_memory_done=done,
                terminal_memory_after=(task_transitions.database_now(conn) + datetime.timedelta(seconds=5)).isoformat(),
            )
            conn.execute(
                text("UPDATE tasks SET metadata_json=:metadata WHERE task_id=:id"),
                {"metadata": _dump_json(record.metadata), "id": task_id},
            )
            return done

    @contextmanager
    def admission_transaction(self, owner_id: str) -> Iterator[None]:
        with self._operation() as conn:
            task_transitions.serialize_owner(conn, owner_id)
            yield

    def get_owner_job(self, job_id: str, *, owner_id: str) -> TaskRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                text(
                    f"SELECT *, {_job_chronology_sql()} FROM tasks WHERE owner_id=:owner_id "
                    f"AND {_logical_job_id_sql()}=:job_id ORDER BY {_current_attempt_order_sql()} LIMIT 1"
                ),
                {"owner_id": owner_id, "job_id": job_id},
            ).fetchone()
            return _row_to_task(row) if row is not None else None

    def count_active_root_jobs(self, *, owner_id: str) -> int:
        with self._connection() as conn:
            return int(
                conn.execute(
                    text(
                        f"SELECT COUNT(DISTINCT {_logical_job_id_sql()}) FROM tasks WHERE owner_id=:owner_id "
                        "AND task_type='task' AND parent_task_id='' "
                        f"AND status IN ({ACTIVE_STATUS_SQL})"
                    ),
                    {"owner_id": owner_id},
                ).scalar_one()
                or 0
            )

    def insert_task(self, task: TaskRecord) -> None:
        with self._operation() as conn:
            self._insert_task(conn, task)

    def create_task(self, request: TaskRequest, *, status: TaskStatus = "queued") -> TaskRecord:
        return self.upsert_task(_task_from_request(request, status=status))

    def _insert_task(self, conn: Connection, task: TaskRecord) -> None:
        params = _task_params(task)
        names = tuple(params)
        conn.execute(
            text(f"INSERT INTO tasks ({', '.join(names)}) VALUES ({', '.join(':' + name for name in names)})"),
            params,
        )

    def upsert_task(
        self,
        task: TaskRecord,
        *,
        execution_task_id: str | None = None,
        claim_token: str = "",
        expected_status: TaskStatus | None = None,
    ) -> TaskRecord:
        """A full-row write, and therefore only for a caller writing a record it authored."""
        if not task.created_at:
            task.created_at = utc_now_iso()
        params = _task_params(task)
        prepared_status = task.status
        with self._operation(deadline=time.monotonic() + 5 if execution_task_id is not None else None) as conn:
            if execution_task_id is not None:
                creator = task_transitions.task_for_transition(conn, execution_task_id)
                if (
                    creator is None
                    or (creator.owner_id, creator.agent_id) != (task.owner_id, task.agent_id)
                    or not self.is_task_attempt_active(execution_task_id, claim_token=claim_token, conn=conn)
                ):
                    raise RuntimeError("Task execution authority lost")
            self._cancel_stopped_child_before_acceptance(conn, task, expected_status=expected_status)
            if task.status != prepared_status:
                params = _task_params(task)
            names = tuple(params)
            terminal_in, terminal_params = task_transitions.terminal_status_binds()
            params.update(terminal_params)
            params.update(
                expected_status=expected_status or "",
                require_expected_status=expected_status is not None,
            )
            immutable = {
                "task_id",
                "runtime_id",
                "stop_requested_at",
                "soft_notice_at",
                "terminal_published_at",
                "terminal_publish_claim",
                "terminal_publish_after",
            }
            updates = ", ".join(f"{name}=excluded.{name}" for name in names if name not in immutable)
            write_result = conn.execute(
                text(
                    f"""
                    INSERT INTO tasks ({", ".join(names)})
                    VALUES ({", ".join(":" + name for name in names)})
                    ON CONFLICT(task_id) DO UPDATE SET {updates}
                    -- `runtime_id`, `stop_requested_at` and `soft_notice_at` are deliberately
                    -- absent above: they are written only by the claim, by the conditional
                    -- stop and by the soft-notice claim, so a read-modify-write round trip
                    -- from another instance cannot erase a stop, or a notice already claimed,
                    -- recorded between its read and its write.
                    WHERE (:require_expected_status AND tasks.status=:expected_status)
                       OR (NOT :require_expected_status AND (
                           (NOT tasks.detached AND tasks.task_type != 'context_consolidation')
                           OR tasks.status NOT IN ({terminal_in})
                       ))
                    """
                ),
                params,
            )
            stored = None
            if write_result.rowcount == 0:
                stored = conn.execute(
                    text("SELECT * FROM tasks WHERE task_id=:task_id"), {"task_id": task.task_id}
                ).fetchone()
            if execution_task_id is not None and not self.is_task_attempt_active(
                execution_task_id, claim_token=claim_token, conn=conn
            ):
                raise RuntimeError("Task execution authority lost")
        return _row_to_task(stored) if stored else task

    def _cancel_stopped_child_before_acceptance(
        self,
        conn: Connection,
        task: TaskRecord,
        *,
        expected_status: TaskStatus | None,
    ) -> None:
        """Serialize a child write with tree stop and preserve the stop's terminal verdict."""
        if not task.parent_task_id or (task.status != "requested" and expected_status != "requested"):
            return
        task_transitions.serialize_owner(conn, task.owner_id)
        parent = conn.execute(
            text(
                "SELECT status, stop_requested_at FROM tasks "
                "WHERE task_id=:task_id AND owner_id=:owner_id AND agent_id=:agent_id"
            ),
            {
                "task_id": task.parent_task_id,
                "owner_id": task.owner_id,
                "agent_id": task.agent_id,
            },
        ).fetchone()
        if parent is None:
            return
        if str(parent._mapping["status"]) != "cancelled" and not parent._mapping["stop_requested_at"]:
            return
        task.status = "cancelled"
        task.result = "Task cancelled."
        task.finished_at = utc_now_iso()

    def _task_type_claim_filter(
        self,
        *,
        include_task_types: Iterable[str] | None = None,
        exclude_task_types: Iterable[str] | None = None,
    ) -> tuple[str, dict[str, object]]:
        clauses: list[str] = []
        params: dict[str, object] = {}
        include = sorted({str(t or "").strip() for t in (include_task_types or []) if str(t or "").strip()})
        exclude = sorted({str(t or "").strip() for t in (exclude_task_types or []) if str(t or "").strip()})
        if include:
            names = []
            for idx, value in enumerate(include):
                key = f"include_task_type_{idx}"
                names.append(f":{key}")
                params[key] = value
            clauses.append(f"task_type IN ({', '.join(names)})")
        if exclude:
            names = []
            for idx, value in enumerate(exclude):
                key = f"exclude_task_type_{idx}"
                names.append(f":{key}")
                params[key] = value
            clauses.append(f"(task_type IS NULL OR task_type NOT IN ({', '.join(names)}))")
        return " AND ".join(clauses), params

    def claim_next_task(
        self,
        worker_id: int = 0,
        *,
        runtime_id: str,
        include_task_types: Iterable[str] | None = None,
        exclude_task_types: Iterable[str] | None = None,
        max_running_root_jobs_per_owner: int = 3,
        claim_guard: Callable[[TaskRecord], AbstractContextManager[bool]] | None = None,
    ) -> TaskRecord | None:
        """Atomically claim one eligible record after serializing a root's owner."""
        extra_where, params = self._task_type_claim_filter(
            include_task_types=include_task_types,
            exclude_task_types=exclude_task_types,
        )
        params.update(
            max_running_root_jobs_per_owner=int(max_running_root_jobs_per_owner),
        )
        try:
            with ExitStack() as guard, self._operation(deadline=time.monotonic() + 5) as conn:
                row = conn.execute(text(claim_select_sql(extra_where)), params).fetchone()
                if row is None:
                    return None
                task = _row_to_task(row)
                if task.task_type == "task" and not task.parent_task_id:
                    task_transitions.serialize_owner(conn, task.owner_id)
                selected = f"task_id=:selected_task_id{f' AND ({extra_where})' if extra_where else ''}"
                row = conn.execute(
                    text(claim_select_sql(selected) + " FOR UPDATE SKIP LOCKED"),
                    {**params, "selected_task_id": task.task_id},
                ).fetchone()
                if row is None:
                    return None
                task = _row_to_task(row)
                now = task_transitions.database_now(conn).isoformat()
                task.status = "running"
                task.worker_id = worker_id
                # Which instance holds the run, stamped in the same statement that makes it
                # running: a lane number is not an address, because every instance has a lane 0.
                task.runtime_id = str(runtime_id)
                task.started_at = now
                task.heartbeat_at = now
                task.metadata["execution_claim"] = uuid.uuid4().hex
                task_payload = encrypt_field(_dump_json(task.model_dump(mode="json")))
                conn.execute(
                    text(
                        """
                        UPDATE tasks SET status=:status, worker_id=:worker_id, runtime_id=:runtime_id,
                            started_at=:started_at, heartbeat_at=:heartbeat_at, payload_json=:payload_json,
                            metadata_json=:metadata_json
                        WHERE task_id=:task_id AND status IN ('queued', 'scheduled', 'interrupted')
                        """
                    ),
                    {
                        "status": task.status,
                        "worker_id": worker_id,
                        "runtime_id": task.runtime_id,
                        "started_at": now,
                        "heartbeat_at": now,
                        "payload_json": task_payload,
                        "metadata_json": _dump_json(task.metadata),
                        "task_id": task.task_id,
                    },
                )
                admitted = guard.enter_context(claim_guard(task) if claim_guard is not None else nullcontext(True))
                if not admitted:
                    raise _ClaimDeclined
            return task
        except (_ClaimDeclined, IntegrityError):
            return None

    def read_task_stop_state(self, task_id: str) -> TaskStopState:
        """Read stop intent and terminal status; a missing task is terminal."""
        with self._connection() as conn:
            row = conn.execute(
                text("SELECT status, stop_requested_at FROM tasks WHERE task_id=:task_id"),
                {"task_id": str(task_id)},
            ).fetchone()
        # Status helpers compose runtime storage; defer the import to avoid a bootstrap cycle.
        from ouroboros.storage.task_results import TERMINAL_STATUSES

        if row is None:
            return TaskStopState(stop_requested=False, is_terminal=True)
        return TaskStopState(
            stop_requested=bool(str(row._mapping["stop_requested_at"] or "").strip()),
            is_terminal=str(row._mapping["status"] or "") in TERMINAL_STATUSES,
        )

    def request_task_stop(self, task_id: str, *, scope: OwnerScope) -> bool:
        """Record that a stop was asked for. Returns whether the write applied."""
        now = utc_now_iso()
        with self._operation() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE tasks SET stop_requested_at=:now
                    WHERE task_id=:task_id AND owner_id=:owner_id AND agent_id=:agent_id
                      AND status IN ('running','waiting_confirmation') AND stop_requested_at IS NULL
                    """
                ),
                {"task_id": str(task_id), "now": now, **_owned_params(scope)},
            )
            applied = bool(result.rowcount)
        return applied

    def claim_soft_timeout_notice(self, task_id: str) -> bool:
        """Take the right to announce the soft timeout once. Returns whether it applied."""
        with self._operation() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE tasks SET soft_notice_at=:now
                    WHERE task_id=:task_id AND status='running' AND soft_notice_at IS NULL
                    """
                ),
                {"task_id": str(task_id), "now": utc_now_iso()},
            )
            applied = bool(result.rowcount)
        return applied

    def heartbeat_task(self, task_id: str, *, phase: str = "", claim_token: str = "") -> TaskRecord | None:
        with self._operation(deadline=time.monotonic() + 5) as conn:
            row = task_transitions.task_for_transition(conn, task_id)
            if row is None:
                return None
            task = _row_to_task(row)
            params = task_transitions.lease_params(conn, claim_token)
            metadata = dict(task.metadata, heartbeat_phase=phase)
            if phase == "model" or (phase == "start" and not task.detached):
                metadata.setdefault("run_started_at", params["now"])
            row = conn.execute(
                text(
                    "UPDATE tasks SET heartbeat_at=:now, metadata_json=:metadata WHERE task_id=:task_id "
                    f"AND ((NOT detached AND status='running') OR (detached AND {execution_authority_sql()} AND stop_requested_at IS NULL)) RETURNING *"
                ),
                {**params, "task_id": task_id, "metadata": _dump_json(metadata)},
            ).fetchone()
            return _row_to_task(row) if row is not None else None

    def complete_task(
        self, task_id: str, *, result: str = "", result_ref: str = "", **fields: Any
    ) -> TaskRecord | None:
        return self.update_task_status(task_id, "completed", result=result, result_ref=result_ref, **fields)

    def fail_task(
        self,
        task_id: str,
        *,
        result: str = "",
        error_code: str = "",
        status: TaskStatus = "failed",
        **fields: Any,
    ) -> TaskRecord | None:
        return self.update_task_status(task_id, status, result=result, error_code=error_code, **fields)

    def cancel_task(self, task_id: str, *, result: str = "Task cancelled.") -> TaskRecord | None:
        return self.update_task_status(task_id, "cancelled", result=result)

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: str | None = None,
        result_ref: str | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
        **fields: Any,
    ) -> TaskRecord | None:
        """Conditionally update status while terminal rows absorb later transitions."""
        fields.pop("chat_id", None)
        now = utc_now_iso()
        completing = status == "completed"
        assignments = [
            "status=CASE WHEN stop_requested_at IS NOT NULL THEN 'cancelled' ELSE :status END"
            if completing
            else "status=:status"
        ]
        params: dict[str, Any] = {"task_id": str(task_id), "status": str(status)}
        if status == "running" and "started_at" not in fields:
            # Only the first start stamps the clock; re-writing `running` must not move it.
            assignments.append("started_at=CASE WHEN started_at IS NULL OR started_at='' THEN :now ELSE started_at END")
        terminal_in, terminal_params = task_transitions.terminal_status_binds()
        params.update(terminal_params)
        # A caller that names the instant itself keeps it: a late body recording its trace
        # against a closed record must not stamp a new completion time on it.
        if str(status) in set(terminal_params.values()) and "finished_at" not in fields:
            assignments.append("finished_at=:now")
        if any(":now" in assignment for assignment in assignments):
            params["now"] = now
        for column, value in (("result", result), ("result_ref", result_ref), ("error_code", error_code)):
            if value is not None:
                if completing and column == "result":
                    assignments.append(
                        "result=CASE WHEN stop_requested_at IS NOT NULL THEN 'Task cancelled.' ELSE :result END"
                    )
                elif completing and column in {"result_ref", "error_code"}:
                    assignments.append(f"{column}=CASE WHEN stop_requested_at IS NOT NULL THEN '' ELSE :{column} END")
                else:
                    assignments.append(f"{column}=:{column}")
                params[column] = value
        extra_metadata = dict(metadata or {})
        for key, value in fields.items():
            if key in _STATUS_EXTRA_COLUMNS:
                assignments.append(f"{key}=:field_{key}")
                params[f"field_{key}"] = value
            else:
                extra_metadata[key] = value
        if extra_metadata:
            stored = self.get_task(task_id)
            if stored is None:
                return None
            merged = dict(stored.metadata)
            merged.update(extra_metadata)
            assignments.append("metadata_json=:metadata_json")
            params["metadata_json"] = _dump_json(merged)
        where = f"task_id=:task_id AND NOT detached AND (status NOT IN ({terminal_in}) OR status=:status)"
        with self._operation() as conn:
            applied = conn.execute(
                text(f"UPDATE tasks SET {', '.join(assignments)} WHERE {where}"),
                params,
            ).rowcount
        task = self.get_task(task_id)
        if not applied and task is not None:
            log.debug(
                "Task status transition %s -> %s for %s applied nothing",
                task.status,
                status,
                task_id,
            )
        return task

    def list_stale_leases(self, *, cutoff_iso: str, limit: int = 100) -> list[TaskRecord]:
        """Running records whose lease has gone stale, oldest first."""
        with self._connection() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM tasks
                    WHERE status='running' AND runtime_id IS NOT NULL
                      AND ((detached AND NOT ({lease_live_sql()})) OR (NOT detached AND heartbeat_at < :legacy_cutoff))
                    ORDER BY heartbeat_at ASC, task_id ASC LIMIT :limit
                    """
                ),
                {**task_transitions.lease_params(conn), "legacy_cutoff": str(cutoff_iso), "limit": int(limit)},
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def transition_running(
        self,
        task_id: str,
        *,
        kind: str,
        result: str,
        attempt: int,
        successor: TaskRequest | None = None,
        runtime_id: str = "",
        claim_token: str = "",
        cutoff_iso: str = "",
        hard_timeout_sec: float = 1800,
    ) -> tuple[TaskRecord, bool] | None:
        if kind not in {"lease_lost", "instance_released", "hard_timeout"}:
            raise ValueError(f"Invalid recovery kind: {kind}")
        successor = successor.model_copy() if successor is not None else None
        outcome = None
        try:
            with self._operation(deadline=time.monotonic() + 5) as conn:
                row = task_transitions.task_for_transition(conn, task_id)
                if row is None:
                    return None
                task = _row_to_task(row)
                params = task_transitions.lease_params(conn, claim_token)
                condition = CURRENT_EXECUTION_SQL
                if kind == "lease_lost":
                    condition += f" AND {'NOT (' + lease_live_sql() + ')' if task.detached else 'heartbeat_at <= :cutoff'} AND runtime_id IS NOT NULL"
                    if not task.detached:
                        params["cutoff"] = cutoff_iso
                elif kind == "instance_released":
                    if task.detached:
                        left = (
                            datetime.datetime.fromisoformat(task.heartbeat_at)
                            - datetime.datetime.fromisoformat(params["cutoff"])
                        ).total_seconds()
                        if left <= 0:
                            return None
                        conn.info["operation_deadline"] = min(
                            conn.info.get("operation_deadline", float("inf")), time.monotonic() + left
                        )
                    condition += " AND runtime_id=:runtime_id"
                    if task.detached:
                        condition += f" AND {execution_authority_sql()}"
                elif task.detached:
                    began = str(task.metadata.get("run_started_at") or "")
                    if (
                        not began
                        or (
                            datetime.datetime.fromisoformat(params["now"]) - datetime.datetime.fromisoformat(began)
                        ).total_seconds()
                        < hard_timeout_sec
                    ):
                        return None
                    condition += f" AND {lease_live_sql()}"
                row = conn.execute(
                    text(
                        "UPDATE tasks SET status=CASE WHEN stop_requested_at IS NOT NULL OR :released THEN 'cancelled' ELSE 'failed' END, "
                        "result=CASE WHEN stop_requested_at IS NULL THEN :result ELSE 'Task cancelled.' END, "
                        "error_code=CASE WHEN stop_requested_at IS NULL THEN :error_code ELSE '' END, "
                        f"attempt=:attempt, finished_at=:now WHERE task_id=:task_id AND {condition} RETURNING *"
                    ),
                    {
                        **params,
                        "task_id": task_id,
                        "result": result,
                        "error_code": kind,
                        "attempt": attempt,
                        "runtime_id": runtime_id,
                        "released": kind == "instance_released",
                    },
                ).fetchone()
                outcome = self._finish_retry_transition(conn, row, successor)
        except STORE_FAILURES:
            if outcome is None:
                raise
            task, created = outcome
            persisted = self.get_task(task.task_id)
            following = self.get_task(successor.id) if created and successor is not None else None
            if persisted is None or persisted.status == "running":
                raise
            if (persisted.status, persisted.result, persisted.error_code, persisted.finished_at) != (
                task.status,
                task.result,
                task.error_code,
                task.finished_at,
            ) or (
                created
                and (
                    following is None
                    or following.job_id != task.job_id
                    or following.lineage_ordinal != task.lineage_ordinal + 1
                )
            ):
                return None
            return persisted, created
        return outcome

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                text("SELECT * FROM tasks WHERE task_id=:task_id"),
                {"task_id": str(task_id)},
            ).fetchone()
        return _row_to_task(row) if row else None

    def is_task_attempt_active(self, task_id: str, *, claim_token: str = "", conn: Connection | None = None) -> bool:
        with nullcontext(conn) if conn is not None else self._connection(deadline=time.monotonic() + 5) as conn:
            return bool(
                conn.execute(
                    text(
                        f"SELECT 1 FROM tasks WHERE task_id=:task_id AND {execution_authority_sql()} AND stop_requested_at IS NULL"
                    ),
                    {"task_id": str(task_id), **task_transitions.lease_params(conn, claim_token)},
                ).fetchone()
            )

    def finish_detached_attempt(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        result: str = "",
        error_code: str = "",
        claim_token: str = "",
        pending_child_ids: tuple[str, ...] = (),
        result_ref: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord | None:
        if status not in {"completed", "cancelled", "failed", "budget_exceeded"}:
            raise ValueError(f"invalid detached terminal status: {status}")
        with self._operation(deadline=time.monotonic() + 5) as conn:
            parent = task_transitions.task_for_transition(conn, task_id)
            if parent is None:
                return None
            params = task_transitions.lease_params(conn, claim_token)
            applied = conn.execute(
                text(
                    "UPDATE tasks SET status=CASE WHEN stop_requested_at IS NULL THEN :status ELSE 'cancelled' END, "
                    "result=CASE WHEN stop_requested_at IS NULL THEN :result ELSE 'Task cancelled.' END, result_ref=CASE WHEN stop_requested_at IS NULL THEN :result_ref ELSE '' END, "
                    "error_code=CASE WHEN stop_requested_at IS NULL THEN :error_code ELSE '' END, metadata_json=CASE WHEN stop_requested_at IS NULL AND :replace_metadata THEN :metadata_json ELSE metadata_json END, "
                    f"finished_at=:now WHERE task_id=:task_id AND detached AND {execution_authority_sql()}"
                ),
                {
                    "task_id": str(task_id),
                    "status": status,
                    "result": result,
                    "result_ref": result_ref,
                    "error_code": error_code,
                    "replace_metadata": metadata is not None,
                    "metadata_json": _dump_json(metadata) if metadata is not None else None,
                    **params,
                },
            ).rowcount
            row = (
                conn.execute(text("SELECT * FROM tasks WHERE task_id=:task_id"), {"task_id": str(task_id)}).fetchone()
                if applied
                else None
            )
            if row is None or row.status != "completed":
                self._cancel_requested_children(conn, _row_to_task(row or parent), pending_child_ids)
        return _row_to_task(row) if applied and row is not None else None

    def _cancel_requested_children(self, conn: Connection, parent: TaskRecord, child_ids: tuple[str, ...] = ()) -> None:
        root = "metadata_json::jsonb->>'root_task_id'"
        conn.execute(
            text(
                "UPDATE tasks SET status='cancelled', result='Task cancelled.', finished_at=:now "
                f"WHERE status='requested' AND {_OWNED} AND ((:failed_parent <> '' AND {root}=:failed_parent) OR task_id=:child)"
            ),
            [
                {
                    "owner_id": parent.owner_id,
                    "agent_id": parent.agent_id,
                    "failed_parent": parent.task_id
                    if parent.status in {"failed", "cancelled", "budget_exceeded"}
                    else "",
                    "child": child,
                    "now": utc_now_iso(),
                }
                for child in child_ids or ("",)
            ],
        )

    def get_current_job(self, job_id: str, *, scope: OwnerScope) -> TaskRecord | None:
        with self._connection() as conn:
            row = conn.execute(
                text(
                    f"SELECT *, {_job_chronology_sql()} FROM tasks WHERE {_OWNED} AND {_logical_job_id_sql()}=:job_id "
                    "AND task_type='task' AND parent_task_id='' "
                    f"ORDER BY {_current_attempt_order_sql()} LIMIT 1"
                ),
                {**_owned_params(scope), "job_id": str(job_id)},
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    def get_job_or_task_statuses(self, ids: Iterable[str], *, scope: OwnerScope) -> dict[str, TaskStatus]:
        """Prefer each owned current root job, falling back to its globally unique task ID; omit missing rows."""
        requested_ids = sorted(set(ids))
        if not requested_ids:
            return {}
        with self._connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT requested.id, COALESCE(current_job.status, attempt.status) AS status "
                    "FROM unnest(CAST(:ids AS text[])) AS requested(id) "
                    "LEFT JOIN LATERAL ("
                    f"SELECT status FROM tasks WHERE {_OWNED} AND {_logical_job_id_sql()}=requested.id "
                    "AND task_type='task' AND parent_task_id='' "
                    f"ORDER BY {_current_attempt_order_sql()} LIMIT 1"
                    ") AS current_job ON TRUE "
                    "LEFT JOIN tasks AS attempt ON attempt.task_id=requested.id AND current_job.status IS NULL"
                ),
                {"ids": requested_ids, **_owned_params(scope)},
            ).fetchall()
        return {row.id: row.status for row in rows if row.status is not None}

    def list_current_jobs(self, *, scope: OwnerScope, limit: int, active_only: bool = False) -> list[TaskRecord]:
        active = f"AND status IN ({ACTIVE_STATUS_SQL})" if active_only else ""
        newer = (
            "SELECT 1 FROM tasks AS newer WHERE newer.owner_id=current.owner_id "
            "AND newer.agent_id=current.agent_id AND newer.task_type='task' AND newer.parent_task_id='' "
            "AND newer.lineage_ordinal>current.lineage_ordinal "
        )
        # Materializing candidates keeps PostgreSQL's anti-joins from pulling in other owners' histories.
        with self._connection() as conn:
            rows = conn.execute(
                text(
                    "WITH candidates AS MATERIALIZED (SELECT task_id, job_id, owner_id, agent_id, lineage_ordinal "
                    f"FROM tasks WHERE {_OWNED} AND task_type='task' AND parent_task_id='' {active}), "
                    f"selected AS (SELECT current.task_id, {_job_chronology_sql('current')} FROM candidates AS current "
                    f"WHERE NOT EXISTS ({newer} AND newer.job_id={_logical_job_id_sql('current')} AND newer.job_id<>'') "
                    f"AND NOT EXISTS ({newer} AND newer.task_id={_logical_job_id_sql('current')} AND newer.job_id='') "
                    f"ORDER BY logical_created_at DESC, {_logical_job_id_sql()} DESC LIMIT :limit) "
                    "SELECT tasks.*, selected.logical_created_at, selected.logical_started_at "
                    "FROM selected JOIN tasks ON tasks.task_id=selected.task_id "
                    f"ORDER BY selected.logical_created_at DESC, {_logical_job_id_sql('tasks')} DESC"
                ),
                {**_owned_params(scope), "limit": int(limit)},
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def stop_jobs(self, *, scope: OwnerScope, job_id: str | None = None) -> list[TaskRecord]:
        with self._operation() as conn:
            task_transitions.serialize_owner(conn, scope.owner_id)
            params: dict[str, Any] = _owned_params(scope)
            job_filter = (
                f"AND {_logical_job_id_sql()}=:job_id" if job_id else "AND surface_id=:surface_id AND chat_id=:chat_id"
            )
            if not job_id:
                params.update(_scoped_params(scope))
            if job_id:
                params["job_id"] = str(job_id)
            else:
                job_filter += (
                    f" AND {_logical_job_id_sql()} IN (SELECT job_id FROM active_ancestors WHERE parent_task_id='')"
                )
            current_roots_sql = (
                f"SELECT current.*, {_job_chronology_sql('current')} FROM (SELECT tasks.*, ROW_NUMBER() OVER (PARTITION BY "
                f"{_logical_job_id_sql()} ORDER BY {_current_attempt_order_sql()}) AS job_row FROM tasks WHERE "
                f"{_OWNED} AND task_type='task' AND parent_task_id='' {job_filter}) AS current WHERE job_row=1"
            )
            if job_id:
                root_query = (
                    f"{current_roots_sql} ORDER BY logical_created_at DESC, {_logical_job_id_sql('current')} DESC"
                )
            else:
                root_query = (
                    "WITH RECURSIVE active_ancestors(task_id,parent_task_id,job_id) AS ("
                    f"SELECT task_id,parent_task_id,{_logical_job_id_sql()} FROM tasks WHERE {_OWNED} "
                    f"AND status IN ({_STOPPABLE_WITHOUT_A_RUN},'waiting_confirmation','running') UNION "
                    f"SELECT parent.task_id,parent.parent_task_id,{_logical_job_id_sql('parent')} "
                    "FROM tasks AS parent JOIN active_ancestors AS child ON parent.task_id=child.parent_task_id "
                    "WHERE parent.owner_id=:owner_id AND parent.agent_id=:agent_id) "
                    f"{current_roots_sql} ORDER BY logical_created_at DESC, {_logical_job_id_sql('current')} DESC"
                )
            roots = conn.execute(text(root_query), params).fetchall()
            if not roots:
                return []
            root_params = {f"root_{index}": str(row._mapping["task_id"]) for index, row in enumerate(roots)}
            root_binds = ", ".join(f":{name}" for name in root_params)
            now = utc_now_iso()
            sibling_roots_sql = (
                f"SELECT t.task_id FROM tasks t WHERE t.task_type='task' AND t.parent_task_id='' "
                f"AND t.owner_id=:owner_id AND t.agent_id=:agent_id "
                f"AND {_logical_job_id_sql('t')} IN "
                f"(SELECT {_logical_job_id_sql('r')} FROM tasks r WHERE r.task_id IN ({root_binds}))"
            )
            needs_cleanup = (
                "(status='waiting_confirmation' OR (status IN ('queued','interrupted') "
                f"AND {_HAS_CONFIRMATION_SNAPSHOT_SQL}))"
            )
            cancel_without_run = f"status IN ({_STOPPABLE_WITHOUT_A_RUN}) AND NOT {needs_cleanup}"
            changed = conn.execute(
                text(
                    "WITH RECURSIVE task_tree(task_id) AS ("
                    f"SELECT task_id FROM tasks WHERE task_id IN ({root_binds}) UNION "
                    f"{sibling_roots_sql} UNION "
                    "SELECT child.task_id FROM tasks AS child JOIN task_tree AS parent "
                    "ON child.parent_task_id=parent.task_id "
                    "WHERE child.owner_id=:owner_id AND child.agent_id=:agent_id) "
                    "UPDATE tasks SET "
                    f"status=CASE WHEN {cancel_without_run} THEN 'cancelled' "
                    f"WHEN {needs_cleanup} THEN 'queued' ELSE status END, "
                    f"result=CASE WHEN {cancel_without_run} THEN 'Task cancelled.' ELSE result END, "
                    f"finished_at=CASE WHEN {cancel_without_run} THEN :now ELSE finished_at END, "
                    f"metadata_json=CASE WHEN {needs_cleanup} "
                    f"THEN {_metadata_without_run_clock_sql()} ELSE metadata_json END, "
                    f"stop_requested_at=CASE WHEN status='running' OR {needs_cleanup} "
                    "THEN COALESCE(stop_requested_at,:now) "
                    "ELSE stop_requested_at END WHERE task_id IN (SELECT task_id FROM task_tree) "
                    f"AND status IN ({_STOPPABLE_WITHOUT_A_RUN},'waiting_confirmation','running') RETURNING *"
                ),
                {"now": now, **params, **root_params},
            ).fetchall()
            refreshed_roots = conn.execute(
                text(f"SELECT *, {_job_chronology_sql()} FROM tasks WHERE task_id IN ({root_binds}) AND {_OWNED}"),
                {**params, **root_params},
            ).fetchall()
            tree_members = conn.execute(
                text(
                    "WITH RECURSIVE task_tree(root_task_id,task_id) AS ("
                    f"SELECT task_id,task_id FROM tasks WHERE task_id IN ({root_binds}) UNION "
                    f"SELECT r.task_id,t.task_id FROM tasks AS r JOIN tasks AS t "
                    f"ON {_logical_job_id_sql('t')}={_logical_job_id_sql('r')} "
                    "AND t.owner_id=r.owner_id AND t.agent_id=r.agent_id "
                    f"WHERE r.task_id IN ({root_binds}) AND t.task_type='task' AND t.parent_task_id='' UNION "
                    "SELECT parent.root_task_id,child.task_id FROM tasks AS child "
                    "JOIN task_tree AS parent ON child.parent_task_id=parent.task_id "
                    "WHERE child.owner_id=:owner_id AND child.agent_id=:agent_id) "
                    "SELECT root_task_id,task_id FROM task_tree"
                ),
                {**params, **root_params},
            ).fetchall()
        changed_by_id = {str(row._mapping["task_id"]): _row_to_task(row) for row in changed}
        changed_ids = frozenset(changed_by_id)
        changed_root_ids = {
            str(row._mapping["root_task_id"]) for row in tree_members if str(row._mapping["task_id"]) in changed_ids
        }
        refreshed_by_id = {str(row._mapping["task_id"]): _row_to_task(row) for row in refreshed_roots}
        root_records: list[TaskRecord] = []
        for task_id in root_params.values():
            changed_by_id.pop(task_id, None)
            refreshed = refreshed_by_id.get(task_id)
            if refreshed is not None and (job_id or task_id in changed_root_ids):
                root_records.append(refreshed)
        descendants = sorted(changed_by_id.values(), key=lambda row: (row.created_at, row.task_id))
        return [*root_records, *descendants]

    def list_tasks(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int | None = 500,
        activity_first: bool = False,
        scope: OwnerScope | None = None,
        runtime_id: str | None = None,
        chat_only: bool = False,
    ) -> list[TaskRecord]:
        """Records, newest activity first when asked. ``scope`` narrows to one owner's."""
        wanted = [str(status) for status in (statuses or []) if str(status).strip()]
        params: dict[str, Any] = {"limit": int(limit)} if limit is not None else {}
        limit_clause = "LIMIT :limit" if limit is not None else ""
        order = (
            "COALESCE(NULLIF(finished_at, ''), NULLIF(started_at, ''), created_at) DESC, task_id DESC"
            if activity_first
            else "created_at ASC, task_id ASC"
        )
        clauses: list[str] = []
        if wanted:
            placeholders = ", ".join(f":status_{index}" for index, _ in enumerate(wanted))
            clauses.append(f"status IN ({placeholders})")
            for index, status in enumerate(wanted):
                params[f"status_{index}"] = status
        if scope is not None:
            clauses.append(_SCOPED if chat_only else _OWNED)
            params.update(_scoped_params(scope) if chat_only else _owned_params(scope))
        if runtime_id is not None:
            clauses.append("runtime_id = :runtime_id")
            params["runtime_id"] = runtime_id
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                text(f"SELECT * FROM tasks {where} ORDER BY {order} {limit_clause}"),
                params,
            ).fetchall()
        return [_row_to_task(row) for row in rows]


class _ClaimDeclined(Exception):
    pass
