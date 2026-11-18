from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from background_memory.infra.repositories._rows import to_consolidation_lease
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import ConsolidationLease, ConsolidationOutcome


def _owned(scope: OwnerScope) -> dict[str, str]:
    return {"owner_id": scope.owner_id, "agent_id": scope.agent_id}


class SqlConsolidationLeaseRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def read(self, scope: OwnerScope) -> ConsolidationLease | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("SELECT * FROM consolidation_leases WHERE owner_id=:owner_id AND agent_id=:agent_id"),
                _owned(scope),
            )
            row = result.mappings().first()
        return to_consolidation_lease(row) if row is not None else None

    async def acquire(self, scope: OwnerScope, *, holder_id: str, now_iso: str, expires_at_iso: str) -> bool:
        params = {**_owned(scope), "holder_id": str(holder_id), "expires_at": str(expires_at_iso), "now": str(now_iso)}
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    INSERT INTO consolidation_leases (
                        owner_id, agent_id, holder_id, lease_expires_at,
                        consecutive_failures, last_failure_at, updated_at
                    ) VALUES (:owner_id, :agent_id, :holder_id, :expires_at, 0, '', :now)
                    ON CONFLICT(owner_id, agent_id) DO UPDATE
                    SET holder_id=excluded.holder_id,
                        lease_expires_at=excluded.lease_expires_at,
                        updated_at=excluded.updated_at
                    WHERE COALESCE(consolidation_leases.holder_id, '')=''
                       OR COALESCE(consolidation_leases.lease_expires_at, '')<=:now
                       OR consolidation_leases.holder_id=:holder_id
                    """
                ),
                params,
            )
            applied = result.rowcount
        return bool(applied)

    async def renew(self, scope: OwnerScope, *, holder_id: str, now_iso: str, expires_at_iso: str) -> bool:
        params = {**_owned(scope), "holder_id": str(holder_id), "expires_at": str(expires_at_iso), "now": str(now_iso)}
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    UPDATE consolidation_leases SET lease_expires_at=:expires_at, updated_at=:now
                    WHERE owner_id=:owner_id AND agent_id=:agent_id AND holder_id=:holder_id
                    """
                ),
                params,
            )
            applied = result.rowcount
        return bool(applied)

    async def release(self, scope: OwnerScope, *, holder_id: str, outcome: ConsolidationOutcome, now_iso: str) -> bool:
        params = {**_owned(scope), "holder_id": str(holder_id), "now": str(now_iso)}
        async with self._engine.begin() as conn:
            if outcome == "completed":
                applied = (
                    await conn.execute(
                        text(
                            """
                            DELETE FROM consolidation_leases
                            WHERE owner_id=:owner_id AND agent_id=:agent_id AND holder_id=:holder_id
                            """
                        ),
                        params,
                    )
                ).rowcount
                if not applied:
                    applied = (
                        await conn.execute(
                            text(
                                """
                                UPDATE consolidation_leases
                                SET consecutive_failures=0, last_failure_at='', updated_at=:now
                                WHERE owner_id=:owner_id AND agent_id=:agent_id AND consecutive_failures>0
                                """
                            ),
                            params,
                        )
                    ).rowcount
            elif outcome == "failed":
                applied = (
                    await conn.execute(
                        text(
                            """
                            UPDATE consolidation_leases
                            SET holder_id='', lease_expires_at='', updated_at=:now,
                                consecutive_failures=consecutive_failures+1, last_failure_at=:now
                            WHERE owner_id=:owner_id AND agent_id=:agent_id AND holder_id=:holder_id
                            """
                        ),
                        params,
                    )
                ).rowcount
            else:
                applied = (
                    await conn.execute(
                        text(
                            """
                            DELETE FROM consolidation_leases
                            WHERE owner_id=:owner_id AND agent_id=:agent_id AND holder_id=:holder_id
                              AND consecutive_failures<=0
                            """
                        ),
                        params,
                    )
                ).rowcount
                if not applied:
                    applied = (
                        await conn.execute(
                            text(
                                """
                                UPDATE consolidation_leases
                                SET holder_id='', lease_expires_at='', updated_at=:now
                                WHERE owner_id=:owner_id AND agent_id=:agent_id AND holder_id=:holder_id
                                """
                            ),
                            params,
                        )
                    ).rowcount
        return bool(applied)


__all__ = ["SqlConsolidationLeaseRepo"]
