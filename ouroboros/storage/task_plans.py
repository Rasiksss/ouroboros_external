"""Scope-bound persistence for the root's plan and review history."""

from __future__ import annotations

import json
from collections.abc import Callable

from sqlalchemy import text

from infra.credentials_key import CredentialsKey
from infra.sql_transaction import SqlTransactionManager
from new_core.identity import OwnerScope
from ouroboros.contracts.task_plan import PlanState
from ouroboros.storage.dialect import execution_authority_sql

_SCOPE = "owner_id=:owner_id AND agent_id=:agent_id AND surface_id=:surface_id AND chat_id=:chat_id"


class SqlTaskPlanRepository:
    def __init__(self, transaction_manager: SqlTransactionManager, key: Callable[[], CredentialsKey]) -> None:
        self._transactions = transaction_manager
        self._key = key

    def load(self, scope: OwnerScope, root_task_id: str) -> PlanState:
        params = {**scope.to_dict(), "root_task_id": root_task_id}
        with self._transactions.connection() as conn:
            value = conn.execute(
                text(f"SELECT state_encrypted FROM task_plans WHERE root_task_id=:root_task_id AND {_SCOPE}"), params
            ).scalar()
        associated_data = json.dumps(params, sort_keys=True).encode()
        return (
            PlanState.model_validate_json(self._key().unseal(value, associated_data=associated_data))
            if value
            else PlanState()
        )

    def change(
        self,
        scope: OwnerScope,
        root_task_id: str,
        expected_version: int | None,
        mutation: Callable[[PlanState], None],
        *,
        claim_token: str = "",
    ) -> PlanState:
        params = {**scope.to_dict(), "root_task_id": root_task_id, "claim_token": claim_token}
        with self._transactions.transaction, self._transactions.connection() as conn:
            row = (
                conn.execute(
                    text(
                        f"SELECT status, parent_task_id, detached, stop_requested_at FROM tasks WHERE task_id=:root_task_id AND {_SCOPE} FOR UPDATE"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if row is None or row["parent_task_id"] or row["status"] != "running" or row["stop_requested_at"]:
                raise ValueError("only the running root can change its plan")
            if (
                row["detached"]
                and not conn.execute(
                    text(
                        f"SELECT 1 FROM tasks WHERE task_id=:root_task_id AND {execution_authority_sql()} AND stop_requested_at IS NULL"
                    ),
                    params,
                ).scalar()
            ):
                raise ValueError("root execution claim is no longer active")
            state = self.load(scope, root_task_id)
            if expected_version is not None and state.version != expected_version:
                raise ValueError("stale expected_version; read the current plan")
            mutation(state)
            state.version += 1
            associated_data = json.dumps({**scope.to_dict(), "root_task_id": root_task_id}, sort_keys=True).encode()
            params["state_encrypted"] = self._key().seal(state.model_dump_json(), associated_data=associated_data)
            conn.execute(
                text(
                    "INSERT INTO task_plans (root_task_id,owner_id,agent_id,surface_id,chat_id,state_encrypted) VALUES (:root_task_id,:owner_id,:agent_id,:surface_id,:chat_id,:state_encrypted) ON CONFLICT (root_task_id) DO UPDATE SET state_encrypted=EXCLUDED.state_encrypted"
                ),
                params,
            )
            if (
                row["detached"]
                and not conn.execute(
                    text(
                        f"SELECT 1 FROM tasks WHERE task_id=:root_task_id AND {execution_authority_sql()} AND stop_requested_at IS NULL"
                    ),
                    params,
                ).scalar()
            ):
                raise ValueError("root execution claim expired before plan commit")
        return state
