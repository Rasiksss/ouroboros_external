"""Factory assembling SessionService with its collaborators."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.audit_event import AuditEventRepository
from app.repositories.session import SessionRepository
from app.services.file_gateway import FileGateway
from app.services.policy_engine import PolicyEngine
from app.services.runtime import RuntimeAdapter
from app.services.session.service import SessionService
from app.services.system_events import SystemEventPublisher, system_event_publisher


def build_session_service(
    session: AsyncSession,
    *,
    system_events: SystemEventPublisher | None = None,
) -> SessionService:
    """Собрать SessionService с production-зависимостями и заменяемым publisher'ом."""
    return SessionService(
        session_repository=SessionRepository(session),
        audit_repository=AuditEventRepository(session),
        runtime=RuntimeAdapter(),
        policy=PolicyEngine(),
        file_gateway=FileGateway(),
        system_events=system_events or system_event_publisher,
    )
