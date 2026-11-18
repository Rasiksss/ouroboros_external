"""Тесты публикации system-events из lifecycle sandbox-сессий."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session, SessionStatus
from app.schemas.session import SessionAuthInfo, SessionCreateRequest
from app.services.session.factory import build_session_service


@dataclass(slots=True)
class PublishedSessionEvent:
    kind: str
    session_id: str
    total: int


class FakeSystemEventPublisher:
    """Fake publisher, который сохраняет факты публикации для assertions."""

    def __init__(self) -> None:
        self.events: list[PublishedSessionEvent] = []

    async def publish_workstation_start(self) -> None:
        """Startup-событие в этих тестах не используется."""

    async def publish_session_created(self, session: Session, *, total_sandbox_sessions: int) -> None:
        """Запомнить событие создания сессии."""
        self.events.append(PublishedSessionEvent("create", session.id, total_sandbox_sessions))

    async def publish_session_deleted(self, session: Session, *, total_sandbox_sessions: int) -> None:
        """Запомнить событие завершения сессии."""
        self.events.append(PublishedSessionEvent("delete", session.id, total_sandbox_sessions))

    async def close(self) -> None:
        """Закрытие fake publisher'у не требуется."""


class FailingSystemEventPublisher(FakeSystemEventPublisher):
    """Publisher, который имитирует ошибку Kafka без падения workflow."""

    async def publish_session_created(self, session: Session, *, total_sandbox_sessions: int) -> None:
        """Имитировать ошибку при публикации create-события."""
        raise RuntimeError("kafka is unavailable")


def _auth(session_id: str, token: str) -> SessionAuthInfo:
    """Собрать auth DTO для прямых вызовов SessionService."""
    return SessionAuthInfo(
        session_id=session_id,
        bearer_token=token,
        actor=f"session:{session_id}",
    )


@pytest.mark.asyncio
async def test_create_reuse_and_terminate_publish_expected_system_events(session: AsyncSession) -> None:
    publisher = FakeSystemEventPublisher()
    service = build_session_service(session, system_events=publisher)

    created = await service.create_session(session_id="system-events-session", payload=SessionCreateRequest())
    reused = await service.create_session(session_id="system-events-session", payload=SessionCreateRequest())
    terminated = await service.terminate_session(_auth("system-events-session", created.token or ""))
    idempotent = await service.terminate_session(_auth("system-events-session", created.token or ""))

    assert created.created is True
    assert reused.created is False
    assert terminated.status == SessionStatus.TERMINATED.value
    assert idempotent.status == SessionStatus.TERMINATED.value
    assert publisher.events == [
        PublishedSessionEvent("create", "system-events-session", 1),
        PublishedSessionEvent("delete", "system-events-session", 0),
    ]


@pytest.mark.asyncio
async def test_recreate_publishes_new_create_event_after_terminated_session(session: AsyncSession) -> None:
    publisher = FakeSystemEventPublisher()
    service = build_session_service(session, system_events=publisher)

    created = await service.create_session(session_id="system-events-recreate", payload=SessionCreateRequest())
    await service.terminate_session(_auth("system-events-recreate", created.token or ""))
    recreated = await service.create_session(session_id="system-events-recreate", payload=SessionCreateRequest())

    assert recreated.created is True
    assert recreated.token is not None
    assert publisher.events == [
        PublishedSessionEvent("create", "system-events-recreate", 1),
        PublishedSessionEvent("delete", "system-events-recreate", 0),
        PublishedSessionEvent("create", "system-events-recreate", 1),
    ]


@pytest.mark.asyncio
async def test_cleanup_expired_sessions_publishes_delete_event(session: AsyncSession) -> None:
    publisher = FakeSystemEventPublisher()
    service = build_session_service(session, system_events=publisher)
    result = await service.create_session(session_id="system-events-cleanup", payload=SessionCreateRequest())
    result.session.expires_at = result.session.created_at
    await session.flush()

    cleaned = await service.cleanup_expired_sessions()

    assert cleaned == 1
    assert result.session.status == SessionStatus.TERMINATED.value
    assert not Path(result.session.rootfs_path).exists()
    assert publisher.events == [
        PublishedSessionEvent("create", "system-events-cleanup", 1),
        PublishedSessionEvent("delete", "system-events-cleanup", 0),
    ]


@pytest.mark.asyncio
async def test_system_event_publisher_failure_does_not_break_session_creation(session: AsyncSession) -> None:
    service = build_session_service(session, system_events=FailingSystemEventPublisher())

    result = await service.create_session(session_id="system-events-best-effort", payload=SessionCreateRequest())

    assert result.created is True
    assert result.session.status == SessionStatus.READY.value
