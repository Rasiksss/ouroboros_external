import json
import uuid
from contextlib import AbstractContextManager

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from infra.sql_transaction import SqlTransactionManager
from new_core.identity import OwnerScope
from ouroboros.contracts.tool_confirmation import (
    CONFIRMATION_EXPIRED,
    CONFIRMATION_EXPIRED_EVENT,
    CONFIRMATION_PENDING,
    CONFIRMATION_REQUESTED_EVENT,
    CONFIRMATION_RESOLVED_EVENT,
)
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.dialect import execution_authority_sql, root_claim_sql
from ouroboros.storage.message_field_crypto import decrypt_field, encrypt_field
from ouroboros.storage.runtime_models import PARKED_WORKSPACE_KEY, AuditEvent, ToolConfirmation
from ouroboros.storage.runtime_rows import tool_confirmation_from_row
from ouroboros.storage.runtime_serialization import dump_json as _dump_json, load_json as _load_json
from ouroboros.storage.task_transitions import lease_params, task_for_transition, terminal_status_binds
from ouroboros.storage.tool_confirmation.interfaces import (
    ConfirmationArguments,
    ConfirmationSnapshot,
    IToolConfirmationRepository,
)
from ouroboros.utils import utc_now_iso


class SqlToolConfirmationRepository(IToolConfirmationRepository):
    def __init__(
        self,
        transaction_manager: SqlTransactionManager,
        *,
        audit_event_repository: IAuditEventRepository,
        max_running_root_jobs_per_owner: int = 3,
    ) -> None:
        self.transaction_manager = transaction_manager
        self._audit_event_repository = audit_event_repository
        self._max_running_root_jobs_per_owner = max_running_root_jobs_per_owner

    @property
    def transaction(self) -> AbstractContextManager[None]:
        return self.transaction_manager.transaction

    def read_tool_confirmation_status(self, confirmation_id: str) -> str:
        with self.transaction_manager.connection() as conn:
            row = conn.execute(
                text("SELECT status FROM tool_confirmations WHERE confirmation_id=:confirmation_id"),
                {"confirmation_id": str(confirmation_id)},
            ).fetchone()
        return str(row._mapping["status"] or "") if row else ""

    def read_open_tool_confirmation(self, scope: OwnerScope) -> ToolConfirmation | None:
        with self.transaction_manager.connection() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT confirmation_id, owner_id, agent_id, surface_id, chat_id, task_id, tool_name,
                           call_ordinal, round_idx, args_hash, status, created_at, decided_at
                    FROM tool_confirmations WHERE owner_id=:owner_id AND agent_id=:agent_id
                      AND surface_id=:surface_id AND chat_id=:chat_id AND status='{CONFIRMATION_PENDING}'
                    """
                ),
                scope.to_dict(),
            ).fetchone()
        return tool_confirmation_from_row(row) if row else None

    def read_tool_confirmation_decisions(self, task_id: str, *, round_idx: int) -> dict[tuple[int, str, str], str]:
        with self.transaction_manager.connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT call_ordinal, tool_name, args_hash, status FROM tool_confirmations "
                    f"WHERE task_id=:task_id AND round_idx=:round_idx AND status<>'{CONFIRMATION_PENDING}' "
                    "AND applied_at=''"
                ),
                {"task_id": str(task_id), "round_idx": int(round_idx)},
            ).fetchall()
        return {
            (
                int(row._mapping["call_ordinal"] or 0),
                str(row._mapping["tool_name"] or ""),
                str(row._mapping["args_hash"] or ""),
            ): str(row._mapping["status"])
            for row in rows
        }

    def park_and_open(
        self,
        *,
        task_id: str,
        claim_token: str,
        confirmation: ToolConfirmation,
        snapshot: ConfirmationSnapshot,
        workspace: str,
        arguments: ConfirmationArguments,
    ) -> ToolConfirmation | None:
        """Atomically park the run, open its question and record the audit event."""
        confirmation.confirmation_id = confirmation.confirmation_id or uuid.uuid4().hex
        confirmation.created_at = confirmation.created_at or utc_now_iso()
        params = {
            "confirmation_id": confirmation.confirmation_id,
            "owner_id": confirmation.owner_id,
            "agent_id": confirmation.agent_id,
            "surface_id": confirmation.surface_id,
            "chat_id": confirmation.chat_id,
            "task_id": confirmation.task_id,
            "tool_name": confirmation.tool_name,
            "call_ordinal": confirmation.call_ordinal,
            "round_idx": confirmation.round_idx,
            "args_hash": confirmation.args_hash,
            "run_state_json": encrypt_field(_dump_json(snapshot)),
            "status": CONFIRMATION_PENDING,
            "created_at": confirmation.created_at,
        }
        try:
            with self.transaction, self.transaction_manager.connection() as conn:
                row = task_for_transition(conn, task_id)
                if row is None:
                    raise _ParkingDeclined
                metadata = dict(_load_json(row._mapping["metadata_json"]))
                if workspace:
                    metadata[PARKED_WORKSPACE_KEY] = workspace
                parked = conn.execute(
                    text(
                        "UPDATE tasks SET status='waiting_confirmation', metadata_json=:metadata "
                        "WHERE task_id=:task_id AND stop_requested_at IS NULL AND ((NOT detached AND status='running') "
                        f"OR (detached AND {execution_authority_sql()}))"
                    ),
                    {**lease_params(conn, claim_token), "task_id": str(task_id), "metadata": _dump_json(metadata)},
                ).rowcount
                if not parked:
                    raise _ParkingDeclined
                conn.execute(
                    text(
                        f"""
                        UPDATE tool_confirmations SET status='{CONFIRMATION_EXPIRED}', decided_at=:now
                        WHERE owner_id=:owner_id AND agent_id=:agent_id AND surface_id=:surface_id AND chat_id=:chat_id AND status='{CONFIRMATION_PENDING}'
                          AND :task_id <> '' AND task_id=:task_id
                        """
                    ),
                    {**params, "now": confirmation.created_at},
                )
                opened = conn.execute(
                    text(
                        """
                        INSERT INTO tool_confirmations (
                            confirmation_id, owner_id, agent_id, surface_id, chat_id, task_id,
                            tool_name, call_ordinal, round_idx, args_hash,
                            run_state_json, status, created_at, decided_at, applied_at
                        ) VALUES (
                            :confirmation_id, :owner_id, :agent_id, :surface_id, :chat_id, :task_id,
                            :tool_name, :call_ordinal, :round_idx, :args_hash,
                            :run_state_json, :status, :created_at, '', ''
                        )
                        ON CONFLICT (owner_id, agent_id, surface_id, chat_id)
                        WHERE status='pending' DO NOTHING
                        """
                    ),
                    params,
                ).rowcount
                if not opened:
                    raise _ParkingDeclined
                self._audit_event_repository.append_audit_event(
                    _request_event(
                        CONFIRMATION_REQUESTED_EVENT, confirmation, at=confirmation.created_at, args=arguments
                    ),
                )
        except _ParkingDeclined:
            return None
        return confirmation

    def unpark_task(self, confirmation_id: str, *, now_iso: str) -> bool:
        """Resume a withdrawn request only if its chat and owner still have running capacity."""
        try:
            with self.transaction, self.transaction_manager.connection() as conn:
                request = conn.execute(
                    text("SELECT task_id, round_idx FROM tool_confirmations WHERE confirmation_id=:confirmation_id"),
                    {"confirmation_id": str(confirmation_id)},
                ).fetchone()
                if request is None:
                    return False
                task_id = str(request._mapping["task_id"])
                if task_for_transition(conn, task_id) is None:
                    return False
                applied = conn.execute(
                    text(
                        "UPDATE tasks SET status='running' WHERE task_id=:task_id "
                        "AND status='waiting_confirmation' AND stop_requested_at IS NULL "
                        f"AND {root_claim_sql()}"
                    ),
                    {
                        "task_id": task_id,
                        "max_running_root_jobs_per_owner": self._max_running_root_jobs_per_owner,
                    },
                ).rowcount
                if applied:
                    conn.execute(
                        text("UPDATE tool_confirmations SET run_state_json='' WHERE confirmation_id=:confirmation_id"),
                        {"confirmation_id": str(confirmation_id)},
                    )
                    self._spend_round(conn, task_id, round_idx=int(request._mapping["round_idx"]), now_iso=now_iso)

        except IntegrityError:
            return False
        return bool(applied)

    def requeue_parked_task(self, task_id: str) -> bool:
        with self.transaction, self.transaction_manager.connection() as conn:
            applied = conn.execute(
                text(
                    "UPDATE tasks SET status='queued', metadata_json=(metadata_json::jsonb - 'run_started_at')::text "
                    "WHERE task_id=:task_id AND status='waiting_confirmation'"
                ),
                {"task_id": str(task_id)},
            ).rowcount
        return bool(applied)

    def take_parked_snapshot(self, task_id: str) -> ConfirmationSnapshot | None:
        """Hand over the latest parked snapshot at most once."""
        with self.transaction, self.transaction_manager.connection() as conn:
            row = conn.execute(
                text(
                    "SELECT confirmation_id, run_state_json FROM tool_confirmations WHERE task_id=:task_id "
                    "AND run_state_json<>'' ORDER BY round_idx DESC, created_at DESC LIMIT 1"
                ),
                {"task_id": str(task_id)},
            ).fetchone()
            if row is None:
                return None
            stored = decrypt_field(row._mapping["run_state_json"])
            # Not `_load_json`: it answers `{}` for text it cannot parse, and an empty snapshot is
            # indistinguishable from never having parked — the run would start the task over.
            parsed = json.loads(stored)
            if not isinstance(parsed, dict):
                raise ValueError(f"stored run state for task {task_id} is not an object")
            handed = conn.execute(
                text(
                    "UPDATE tool_confirmations SET run_state_json='' "
                    "WHERE confirmation_id=:confirmation_id AND run_state_json<>''"
                ),
                {"confirmation_id": row._mapping["confirmation_id"]},
            ).rowcount
        # Two readers of one row: the drop that applied is the hand-over, the other one read nothing.
        return parsed if handed else None

    def spend_round_decisions(self, task_id: str, *, round_idx: int, now_iso: str) -> int:
        """An answer allows one call of one round of one attempt; this stamp is the attempt."""
        with self.transaction, self.transaction_manager.connection() as conn:
            spent = self._spend_round(conn, task_id, round_idx=round_idx, now_iso=now_iso)
        return spent

    @staticmethod
    def _spend_round(conn: Connection, task_id: str, *, round_idx: int, now_iso: str) -> int:
        return int(
            conn.execute(
                text(
                    "UPDATE tool_confirmations SET applied_at=:now WHERE task_id=:task_id "
                    f"AND round_idx=:round_idx AND status<>'{CONFIRMATION_PENDING}' AND applied_at=''"
                ),
                {"task_id": str(task_id), "round_idx": int(round_idx), "now": now_iso},
            ).rowcount
        )

    def list_parked_tasks_without_pending_request(self, *, limit: int = 100) -> list[str]:
        """Answered, or left by a run that died. The stored snapshot is the filter: a2a writes
        ``waiting_confirmation`` for its own input_required and stores none."""
        with self.transaction_manager.connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT tasks.task_id FROM tasks JOIN tool_confirmations "
                    "ON tool_confirmations.task_id=tasks.task_id AND tool_confirmations.run_state_json<>'' "
                    "WHERE tasks.status='waiting_confirmation' AND NOT EXISTS ("
                    "SELECT 1 FROM tool_confirmations AS open_request WHERE open_request.task_id=tasks.task_id "
                    f"AND open_request.status='{CONFIRMATION_PENDING}') ORDER BY tasks.task_id LIMIT :limit"
                ),
                {"limit": int(limit)},
            ).fetchall()
        return [str(row._mapping["task_id"]) for row in rows]

    def close_confirmations_nobody_is_parked_on(self, *, now_iso: str) -> list[ToolConfirmation]:
        """Close every question whose run is no longer parked, recording how each ended in the same write."""
        with self.transaction, self.transaction_manager.connection() as conn:
            rows = conn.execute(
                text(
                    f"UPDATE tool_confirmations SET status='{CONFIRMATION_EXPIRED}', decided_at=:now "
                    f"WHERE status='{CONFIRMATION_PENDING}' AND NOT EXISTS ("
                    "SELECT 1 FROM tasks WHERE tasks.task_id=tool_confirmations.task_id "
                    "AND tasks.status='waiting_confirmation' AND tasks.stop_requested_at IS NULL) "
                    "RETURNING *"
                ),
                {"now": now_iso},
            ).fetchall()
            closed = [tool_confirmation_from_row(row) for row in rows]
            for request in closed:
                self._audit_event_repository.append_audit_event(
                    _request_event(CONFIRMATION_EXPIRED_EVENT, request, at=now_iso)
                )
        return closed

    def forget_snapshots_of_finished_runs(self, *, limit: int = 100) -> int:
        """Drop the stored run state of tasks that ended; the request row itself stays."""
        terminal, terminal_params = terminal_status_binds()
        with self.transaction, self.transaction_manager.connection() as conn:
            dropped = conn.execute(
                text(
                    "UPDATE tool_confirmations SET run_state_json='' WHERE confirmation_id IN ("
                    "SELECT confirmation_id FROM tool_confirmations AS parked "
                    "JOIN tasks ON tasks.task_id=parked.task_id "
                    f"WHERE parked.run_state_json<>'' AND tasks.status IN ({terminal}) LIMIT :limit)"
                ),
                {**terminal_params, "limit": int(limit)},
            ).rowcount
        return int(dropped)

    def resolve_tool_confirmation(self, confirmation_id: str, *, decision: str, now_iso: str) -> bool:
        """Record the owner's decision, but only while the run is neither stopping nor over."""
        terminal, terminal_params = terminal_status_binds()
        return self._decide_tool_confirmation(
            confirmation_id,
            decision=decision,
            event_type=CONFIRMATION_RESOLVED_EVENT,
            now_iso=now_iso,
            guard_predicate=(
                "NOT EXISTS (SELECT 1 FROM tasks "
                "WHERE tasks.task_id=tool_confirmations.task_id "
                f"AND (COALESCE(tasks.stop_requested_at,'')<>'' OR tasks.status IN ({terminal})))"
            ),
            guard_params=terminal_params,
        )

    def expire_tool_confirmation(self, confirmation_id: str, *, now_iso: str) -> bool:
        return self._decide_tool_confirmation(
            confirmation_id, decision=CONFIRMATION_EXPIRED, event_type=CONFIRMATION_EXPIRED_EVENT, now_iso=now_iso
        )

    def _decide_tool_confirmation(
        self,
        confirmation_id: str,
        *,
        decision: str,
        event_type: str,
        now_iso: str,
        guard_predicate: str = "",
        guard_params: dict[str, str] | None = None,
    ) -> bool:
        guard = f" AND {guard_predicate}" if guard_predicate else ""
        with self.transaction, self.transaction_manager.connection() as conn:
            decided = conn.execute(
                text(
                    f"""
                    UPDATE tool_confirmations SET status=:decision, decided_at=:now
                    WHERE confirmation_id=:confirmation_id
                      AND status='{CONFIRMATION_PENDING}'{guard}
                    RETURNING *
                    """
                ),
                {
                    "confirmation_id": str(confirmation_id),
                    "decision": str(decision),
                    "now": now_iso,
                    **(guard_params or {}),
                },
            ).fetchone()
            if decided is not None:
                self._audit_event_repository.append_audit_event(
                    _request_event(event_type, tool_confirmation_from_row(decided), at=now_iso)
                )
        return decided is not None


def _request_event(
    event_type: str, request: ToolConfirmation, *, at: str, args: ConfirmationArguments | None = None
) -> AuditEvent:
    decision = "" if request.status == CONFIRMATION_PENDING else request.status
    return AuditEvent.runtime_event(
        "events",
        event_type,
        {
            "ts": at,
            "task_id": request.task_id,
            "tool": request.tool_name,
            "confirmation_id": request.confirmation_id,
            "decision": decision,
            **({"args": args} if args is not None else {}),
        },
        owner_id=request.owner_id,
        agent_id=request.agent_id,
        decision=decision,
    )


class _ParkingDeclined(Exception):
    pass
