"""Repository for audit event data."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit_event import AuditEvent
from app.repositories.sqla import BaseSQLARepository
from app.utils.audit import AUDIT_FAIL_CODE, AUDIT_SUCCESS_CODE, audit_action


class AuditEventRepository(BaseSQLARepository[AuditEvent]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session=session, model=AuditEvent)

    async def add_event(
        self,
        *,
        session_id: str,
        kind: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        audit_action(
            "Workstation event",
            kind,
            {
                "session_id": session_id,
                "actor": actor,
                "payload": dict(payload or {}),
            },
            code=_audit_code_for_event(kind, payload),
        )
        # Audit is gated by AUDIT_ENABLED. When off, return a transient row so
        # callers that expect a Pydantic-shaped object still work, but skip the
        # write entirely — every request hits this path, so the savings are
        # significant on SQLite where each insert grabs a write lock.
        if not settings.AUDIT_ENABLED:
            return AuditEvent(
                session_id=session_id,
                kind=kind,
                actor=actor,
                payload=dict(payload or {}),
            )
        return await self.create(
            session_id=session_id,
            kind=kind,
            actor=actor,
            payload=dict(payload or {}),
        )

    async def list_for_session(self, session_id: str) -> list[AuditEvent]:
        stmt = self.build_query(session_id=session_id, order_by="ts")
        return await self.execute_query(stmt)


def _audit_code_for_event(kind: str, payload: dict[str, Any] | None) -> str:
    normalized_kind = kind.lower()
    if any(marker in normalized_kind for marker in ("deny", "denied", "fail", "error", "timeout")):
        return AUDIT_FAIL_CODE

    data = payload or {}
    status = str(data.get("status") or data.get("state") or data.get("result") or "").lower()
    if status in {"denied", "failed", "failure", "error", "timeout", "timed_out"}:
        return AUDIT_FAIL_CODE
    if any(key in data for key in ("error", "errorType", "errorMessage", "failReason")):
        return AUDIT_FAIL_CODE
    return AUDIT_SUCCESS_CODE
