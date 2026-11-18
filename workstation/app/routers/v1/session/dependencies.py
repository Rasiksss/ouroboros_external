"""Dependencies for session lifecycle routes."""

from __future__ import annotations

import hashlib
import logging
import time
import traceback
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.middleware.auth import SessionAuthContext
from app.models.session import Session
from app.repositories.session import SessionRepository
from app.schemas.session import SessionAuthInfo
from app.serializers.session import SessionSerializer
from app.services.session.factory import build_session_service
from app.services.session.service import SessionService
from app.services.session.stream_encoder import SessionStreamEncoder
from app.utils.exceptions.common import NotFoundException, auth_required

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AuthenticatedSession:
    auth: SessionAuthInfo
    session: Session


_bearer_scheme = HTTPBearer(auto_error=False)


def get_session_serializer() -> SessionSerializer:
    return SessionSerializer()


def get_session_stream_encoder() -> SessionStreamEncoder:
    return SessionStreamEncoder()


def get_session_service(session: AsyncSession = Depends(get_session)) -> SessionService:
    return build_session_service(session)


async def document_session_auth(
    _bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    _x_session_id: Annotated[
        str,
        Header(
            alias="X-Session-Id",
            description=(
                "Непрозрачный идентификатор сессии. Должен совпадать с session_id в path и bearer token этой сессии."
            ),
        ),
    ],
) -> None:
    """OpenAPI-only auth shape for Swagger.

    Real auth enforcement stays in middleware + `get_authenticated_session`.
    """


async def get_authenticated_session(
    request: Request,
    session_id: str,
    db_session: AsyncSession = Depends(get_session),
) -> AuthenticatedSession:
    auth_context = getattr(request.state, "session_auth", None)
    if not isinstance(auth_context, SessionAuthContext) or not auth_context.is_authenticated:
        raise auth_required("missing-auth-context")
    if auth_context.session_id != session_id:
        raise auth_required("session-id-mismatch")

    repository = SessionRepository(db_session)
    try:
        logger.debug(
            "SessionRepository.get_by_id start",
            extra={
                "event.category": "processing",
                "event.action": "process",
                "app.operation": "SessionRepository.get_by_id",
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        session = await repository.get_by_id(session_id)
    except Exception as exc:
        logger.error(
            "SessionRepository.get_by_id failed",
            extra={
                "event.category": "processing",
                "event.action": "process",
                "app.operation": "SessionRepository.get_by_id",
                "event.outcome": "failure",
                "app.status": "failed",
                "error.type": type(exc).__name__,
                "error.message": str(exc),
                "error.stack_trace": traceback.format_exc(),
            },
        )
        raise
    else:
        logger.info(
            "SessionRepository.get_by_id success",
            extra={
                "event.category": "processing",
                "event.action": "process",
                "app.operation": "SessionRepository.get_by_id",
                "event.outcome": "success",
                "app.status": "processed",
            },
        )

    if session is None:
        raise NotFoundException("Сессия не найдена", details={"sessionId": session_id})

    token_hash = hashlib.sha256(auth_context.bearer_token.encode("utf-8")).hexdigest()
    if token_hash != session.token_hash:
        raise auth_required("token-mismatch")

    return AuthenticatedSession(
        auth=SessionAuthInfo(
            session_id=session_id,
            bearer_token=auth_context.bearer_token,
            actor=f"session:{session_id}",
        ),
        session=session,
    )


def log_endpoint(app_operation: str) -> Callable[[Request], AsyncGenerator[None]]:
    async def log_endpoint_worker(_: Request) -> AsyncGenerator[None]:
        logger.debug(
            "request_accepted",
            extra={
                "event.category": "request",
                "event.action": "receive",
                "app.operation": app_operation,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        started = time.perf_counter()
        try:
            yield None
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                "request_failed",
                extra={
                    "event.category": "request",
                    "event.action": "failure",
                    "app.operation": app_operation,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(e).__name__,
                    "error.message": str(e),
                    "error.stack_trace": traceback.format_exc(),
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "request_completed",
                extra={
                    "event.category": "request",
                    "event.action": "complete",
                    "app.operation": app_operation,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                },
            )

    return log_endpoint_worker
