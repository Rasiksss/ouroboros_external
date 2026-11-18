"""Repository for session lifecycle data."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session, SessionStatus
from app.repositories.sqla import BaseSQLARepository
from app.utils.time import utcnow


class SessionRepository(BaseSQLARepository[Session]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session=session, model=Session)

    async def get_by_id(self, session_id: str) -> Session | None:
        return await self._session.get(Session, session_id)

    async def recreate(
        self,
        session: Session,
        *,
        status: str,
        token_hash: str,
        rootfs_path: str,
        created_at: datetime,
        last_used_at: datetime,
        expires_at: datetime,
        idle_timeout_sec: int,
        absolute_ttl_sec: int,
    ) -> Session:
        session.status = status
        session.token_hash = token_hash
        session.rootfs_path = rootfs_path
        session.created_at = created_at
        session.updated_at = created_at
        session.last_used_at = last_used_at
        session.expires_at = expires_at
        session.idle_timeout_sec = idle_timeout_sec
        session.absolute_ttl_sec = absolute_ttl_sec
        session.file_bytes_used = 0
        session.workspace_persistence_scope_id = None
        session.workspace_persistence_root = None
        session.workspace_restored_revision_id = None
        session.workspace_snapshot_revision_id = None
        session.fail_reason = None
        session.terminated_at = None
        await self._session.flush()
        return session

    async def touch(self, session: Session, *, current: datetime, expires_at: datetime) -> Session:
        session.last_used_at = current
        session.updated_at = current
        session.expires_at = expires_at
        await self._session.flush()
        return session

    async def list_expired_sessions(self, current: datetime) -> list[Session]:
        stmt = select(Session).where(
            Session.status.in_(
                [
                    SessionStatus.PROVISIONING.value,
                    SessionStatus.READY.value,
                    SessionStatus.TERMINATING.value,
                    SessionStatus.FAILED.value,
                ]
            ),
            Session.expires_at <= current,
        )
        return await self.execute_query(stmt)

    async def list_non_terminated_sessions(self) -> list[Session]:
        stmt = select(Session).where(Session.status != SessionStatus.TERMINATED.value)
        return await self.execute_query(stmt)

    async def count_ready_sessions(self) -> int:
        """Посчитать sandbox-сессии, которые фактически готовы к работе."""
        return await self.get_count(status=SessionStatus.READY.value)

    async def set_status(
        self,
        session: Session,
        next_status: SessionStatus,
        *,
        fail_reason: str | None = None,
    ) -> Session:
        session.status = next_status.value
        session.updated_at = utcnow()
        if next_status is SessionStatus.FAILED:
            session.fail_reason = fail_reason
        if next_status is SessionStatus.TERMINATED:
            session.terminated_at = utcnow()
        await self._session.flush()
        return session
