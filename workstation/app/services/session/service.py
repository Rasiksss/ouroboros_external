"""Business logic for session lifecycle and command execution."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import re
import secrets
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.models.session import Session, SessionStatus
from app.repositories.audit_event import AuditEventRepository
from app.repositories.session import SessionRepository
from app.schemas.session import (
    ExecRequest,
    FileListRequest,
    SessionAuthInfo,
    SessionCreateRequest,
)
from app.services.exec_locks import session_create_locks, session_exec_operations, session_file_locks
from app.services.aifa_controlled_policy import AifaControlledPolicy
from app.services.file_gateway import DownloadedFile, FileGateway, FileNode, UploadResult
from app.services.policy_engine import PolicyEngine
from app.services.runtime import RuntimeAdapter
from app.services.runtime.types import ExecResult, ExecStreamEvent, RuntimeContext
from app.services.state_machine import ensure_action_allowed, ensure_transition
from app.services.system_events import NoopSystemEventPublisher, SystemEventPublisher
from app.services.workspace_persistence import (
    WorkspacePersistenceRequest,
    WorkspacePersistenceService,
    build_workspace_persistence_service,
)
from app.utils.exceptions.common import (
    InternalServerErrorException,
    InvalidStateException,
    NotFoundException,
    QuotaExceededException,
    TimeoutException,
    ValidationErrorException,
)
from app.utils.logging import redact_log_value
from app.utils.time import ensure_utc, utcnow

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
logger = logging.getLogger(__name__)

# Per-process last-touch timestamps. _touch_session debounces DB writes against
# this map so hot sessions (many exec/file ops per second) do not generate one
# UPDATE per request — at most one write per SESSION_TOUCH_DEBOUNCE_SEC.
_last_touch_at: dict[str, datetime] = {}


@dataclass(slots=True)
class SessionCreateResult:
    created: bool
    session: Session
    token: str | None


@dataclass(slots=True, frozen=True)
class ReconcileResult:
    failed_missing_rootfs: int
    removed_orphans: int


class SessionService:
    def __init__(
        self,
        session_repository: SessionRepository,
        audit_repository: AuditEventRepository,
        runtime: RuntimeAdapter | None = None,
        policy: PolicyEngine | None = None,
        file_gateway: FileGateway | None = None,
        system_events: SystemEventPublisher | None = None,
        workspace_persistence: WorkspacePersistenceService | None = None,
        aifa_policy: AifaControlledPolicy | None = None,
    ) -> None:
        self._sessions = session_repository
        self._audit = audit_repository
        self._runtime = runtime or RuntimeAdapter()
        self._policy = policy or PolicyEngine()
        self._aifa_policy = aifa_policy or AifaControlledPolicy()
        self._files = file_gateway or FileGateway()
        self._system_events = system_events or NoopSystemEventPublisher()
        self._workspace_persistence = workspace_persistence or build_workspace_persistence_service(
            session_repository.session
        )

    async def create_session(
        self,
        *,
        session_id: str,
        payload: SessionCreateRequest,
    ) -> SessionCreateResult:
        op = "SessionService.create_session"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {}}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            self._validate_session_id(session_id)
            idle_timeout = self._resolve_ttl(
                value=payload.idle_timeout_sec,
                default=settings.SESSION_IDLE_TIMEOUT_SEC,
                field_name="idleTimeoutSec",
            )
            absolute_ttl = self._resolve_ttl(
                value=payload.absolute_ttl_sec,
                default=settings.SESSION_ABSOLUTE_TTL_SEC,
                field_name="absoluteTtlSec",
            )
            rootfs_path = self._rootfs_path(session_id)
            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "session-create-start",
                    "sessionId": session_id,
                    "rootfsPath": str(rootfs_path),
                    "idleTimeoutSec": idle_timeout,
                    "absoluteTtlSec": absolute_ttl,
                }
            )
            async with session_create_locks.serialize(session_id):
                existing = await self._sessions.get_by_id(session_id)
                if existing is None:
                    token = self._generate_token()
                    current = utcnow()
                    session = await self._sessions.create(
                        id=session_id,
                        status=SessionStatus.PROVISIONING.value,
                        token_hash=self._hash_token(token),
                        rootfs_path=str(rootfs_path),
                        created_at=current,
                        last_used_at=current,
                        expires_at=self._calculate_expires_at(
                            created_at=current,
                            current=current,
                            idle_timeout_sec=idle_timeout,
                            absolute_ttl_sec=absolute_ttl,
                        ),
                        idle_timeout_sec=idle_timeout,
                        absolute_ttl_sec=absolute_ttl,
                    )
                    try:
                        await self._provision_session_rootfs(session)
                        await self._restore_session_workspace(session, payload)
                    except Exception as exc:
                        extra_diagnostics["diagnostics"].update(
                            {
                                **_session_diagnostics(session),
                                "phase": "session-create-failed",
                                "errorType": type(exc).__name__,
                                "errorMessage": str(exc),
                            }
                        )
                        await self._persist_failed_creation(
                            session_id=session_id,
                            rootfs_path=str(rootfs_path),
                            payload=payload,
                            idle_timeout_sec=idle_timeout,
                            absolute_ttl_sec=absolute_ttl,
                            fail_reason=str(exc),
                        )
                        raise
                    await self._audit.add_event(
                        session_id=session.id,
                        kind="session.create",
                        actor=f"session:{session.id}",
                        payload={"reused": False, "status": session.status},
                    )
                    await self._set_status(session, SessionStatus.READY)
                    await self._audit.add_event(
                        session_id=session.id,
                        kind="state.transition",
                        actor=f"session:{session.id}",
                        payload={"from": SessionStatus.PROVISIONING.value, "to": SessionStatus.READY.value},
                    )
                    await self._publish_session_created(session)
                    extra_diagnostics["diagnostics"].update(
                        {
                            **_session_diagnostics(session),
                            "phase": "session-create-ready",
                            "created": True,
                        }
                    )
                    result = SessionCreateResult(created=True, session=session, token=token)

                elif existing.status == SessionStatus.READY.value:
                    extra_diagnostics["diagnostics"].update(
                        {
                            **_session_diagnostics(existing),
                            "phase": "session-create-reuse",
                            "created": False,
                        }
                    )
                    await self._audit.add_event(
                        session_id=existing.id,
                        kind="session.create",
                        actor=f"session:{existing.id}",
                        payload={"reused": True, "status": existing.status},
                    )
                    result = SessionCreateResult(created=False, session=existing, token=None)

                elif existing.status in {SessionStatus.PROVISIONING.value, SessionStatus.TERMINATING.value}:
                    extra_diagnostics["diagnostics"].update(
                        {
                            **_session_diagnostics(existing),
                            "phase": "session-create-busy",
                        }
                    )
                    raise InvalidStateException(
                        "Сессия уже создается или завершается",
                        details={"sessionId": existing.id, "status": existing.status},
                    )

                else:
                    token = self._generate_token()
                    current = utcnow()
                    ensure_transition(existing.status, SessionStatus.PROVISIONING.value)
                    await self._sessions.recreate(
                        existing,
                        status=SessionStatus.PROVISIONING.value,
                        token_hash=self._hash_token(token),
                        rootfs_path=str(rootfs_path),
                        created_at=current,
                        last_used_at=current,
                        expires_at=self._calculate_expires_at(
                            created_at=current,
                            current=current,
                            idle_timeout_sec=idle_timeout,
                            absolute_ttl_sec=absolute_ttl,
                        ),
                        idle_timeout_sec=idle_timeout,
                        absolute_ttl_sec=absolute_ttl,
                    )
                    try:
                        await self._provision_session_rootfs(existing)
                        await self._restore_session_workspace(existing, payload)
                    except Exception as exc:
                        extra_diagnostics["diagnostics"].update(
                            {
                                **_session_diagnostics(existing),
                                "phase": "session-recreate-failed",
                                "errorType": type(exc).__name__,
                                "errorMessage": str(exc),
                            }
                        )
                        await self._persist_failed_creation(
                            session_id=session_id,
                            rootfs_path=str(rootfs_path),
                            payload=payload,
                            idle_timeout_sec=idle_timeout,
                            absolute_ttl_sec=absolute_ttl,
                            fail_reason=str(exc),
                        )
                        raise
                    await self._audit.add_event(
                        session_id=existing.id,
                        kind="session.create",
                        actor=f"session:{existing.id}",
                        payload={"reused": False, "recreated": True, "status": existing.status},
                    )
                    await self._set_status(existing, SessionStatus.READY)
                    await self._audit.add_event(
                        session_id=existing.id,
                        kind="state.transition",
                        actor=f"session:{existing.id}",
                        payload={"from": SessionStatus.PROVISIONING.value, "to": SessionStatus.READY.value},
                    )
                    await self._publish_session_created(existing)
                    extra_diagnostics["diagnostics"].update(
                        {
                            **_session_diagnostics(existing),
                            "phase": "session-create-ready",
                            "created": True,
                            "recreated": True,
                        }
                    )
                    result = SessionCreateResult(created=True, session=existing, token=token)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    async def get_session(self, auth: SessionAuthInfo) -> Session:
        op = "SessionService.get_session"
        started = time.perf_counter()
        extra_diagnostics = {}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            session = await self._require_session(auth.session_id)
            ensure_action_allowed(session.status, "get")
            if session.status == SessionStatus.READY.value:
                await self._touch_session(session)
            await self._audit.add_event(
                session_id=session.id,
                kind="session.get",
                actor=auth.actor,
                payload={"status": session.status},
            )
            extra_diagnostics = {
                "diagnostics": {
                    **_auth_diagnostics(auth),
                    **_session_diagnostics(session),
                    "phase": "session-get",
                }
            }
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return session

    async def terminate_session(self, auth: SessionAuthInfo) -> Session:
        op = "SessionService.terminate_session"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {}}
        session = None
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            session = await self._require_session(auth.session_id)
            ensure_action_allowed(session.status, "terminate")
            if session.status == SessionStatus.TERMINATED.value:
                await self._audit.add_event(
                    session_id=session.id,
                    kind="session.terminate",
                    actor=auth.actor,
                    payload={"idempotent": True, "status": session.status},
                )
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        "phase": "session-terminate-idempotent",
                    }
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                logger.info(
                    f"{op} success",
                    extra={
                        "event.category": "processing",
                        "event.action": "complete",
                        "app.operation": op,
                        "event.outcome": "success",
                        "app.status": "processed",
                        "app.duration_ms": elapsed_ms,
                        **extra_diagnostics,
                    },
                )
                return session

            async with session_exec_operations.exclusive(session.id):
                if session_file_locks.is_busy(session.id):
                    extra_diagnostics["diagnostics"].update(
                        {
                            **_auth_diagnostics(auth),
                            **_session_diagnostics(session),
                            "phase": "session-terminate-busy",
                            "busyLock": "file",
                        }
                    )
                    raise InvalidStateException("Сессия занята другой операцией", details={"sessionId": session.id})

                result = await self._terminate_existing_session(
                    session,
                    actor=auth.actor,
                    audit_kind="session.terminate",
                )
        except InvalidStateException as exc:
            if session is None:
                raise

            if session_exec_operations.is_busy(session.id):
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        "phase": "session-terminate-busy",
                        "busyLock": "exec",
                    }
                )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    async def exec_command(self, auth: SessionAuthInfo, payload: ExecRequest) -> ExecResult:
        op = "SessionService.exec_command"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {}}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            session = await self._require_session(auth.session_id)
            ensure_action_allowed(session.status, "exec")
            timeout_sec = self._resolve_exec_timeout(payload.timeout_sec)
            stdin = self._decode_stdin(payload.stdin, payload.stdin_encoding)
            try:
                self._aifa_policy.validate_exec(payload.command, rootfs_path=session.rootfs_path)
                decision = self._policy.validate_exec(payload.command, payload.cwd, payload.env)
            except Exception:
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        **_exec_request_diagnostics(
                            command=payload.command,
                            cwd=payload.cwd,
                            env=payload.env,
                            timeout_sec=timeout_sec,
                            stdin=stdin,
                            stdin_encoding=payload.stdin_encoding,
                        ),
                        "phase": "exec-policy-denied",
                    }
                )
                await self._audit.add_event(
                    session_id=session.id,
                    kind="policy.deny",
                    actor=auth.actor,
                    payload={"command": payload.command, "cwd": payload.cwd},
                )
                raise

            async with session_exec_operations.track(session.id):
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        **_exec_request_diagnostics(
                            command=payload.command,
                            cwd=decision.cwd,
                            env=decision.env,
                            timeout_sec=timeout_sec,
                            stdin=stdin,
                            stdin_encoding=payload.stdin_encoding,
                        ),
                        "phase": "exec-start",
                    }
                )
                await self._touch_session(session)
                await self._enforce_pre_exec_quota(session)
                await self._audit.add_event(
                    session_id=session.id,
                    kind="exec.request",
                    actor=auth.actor,
                    payload={
                        "command": payload.command,
                        "cwd": decision.cwd,
                        "timeoutSec": timeout_sec,
                        **self._runtime.audit_context(),
                    },
                )
                result = await self._runtime.exec(
                    self._runtime_context(session),
                    command=payload.command,
                    cwd=decision.cwd,
                    env=decision.env,
                    timeout_sec=timeout_sec,
                    output_limit_bytes=settings.EXEC_OUTPUT_LIMIT_BYTES,
                    stdin=stdin,
                )
                await self._files.refresh_usage(session)
                await self._touch_session(session)
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        "phase": "exec-finish",
                        "exitCode": result.exit_code,
                        "timedOut": result.timed_out,
                        "durationMs": result.duration_ms,
                        "stdoutBytes": len(result.stdout.encode("utf-8")),
                        "stderrBytes": len(result.stderr.encode("utf-8")),
                        "stdoutTruncated": result.stdout_truncated,
                        "stderrTruncated": result.stderr_truncated,
                        **self._runtime.audit_context(stderr=result.stderr),
                    }
                )
                await self._audit.add_event(
                    session_id=session.id,
                    kind="exec.response",
                    actor=auth.actor,
                    payload={
                        "exitCode": result.exit_code,
                        "timedOut": result.timed_out,
                        "durationMs": result.duration_ms,
                        "stdoutTruncated": result.stdout_truncated,
                        "stderrTruncated": result.stderr_truncated,
                        **self._runtime.audit_context(stderr=result.stderr),
                    },
                )

            if result.kill_reason == "quota":
                raise QuotaExceededException(
                    "session total bytes quota exceeded during exec",
                    details={
                        "sessionId": session.id,
                        "maxTotalBytes": settings.MAX_TOTAL_BYTES,
                        "fileBytesUsed": session.file_bytes_used,
                    },
                )
            if result.timed_out:
                raise TimeoutException(
                    "Команда превысила timeout выполнения",
                    details={"sessionId": session.id, "durationMs": result.duration_ms},
                )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    async def stream_command(
        self,
        auth: SessionAuthInfo,
        payload: ExecRequest,
        *,
        disconnect_checker: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[ExecStreamEvent]:
        op = "SessionService.stream_command"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {}}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            session = await self._require_session(auth.session_id)
            ensure_action_allowed(session.status, "exec")
            timeout_sec = self._resolve_exec_timeout(payload.timeout_sec)
            stdin = self._decode_stdin(payload.stdin, payload.stdin_encoding)
            try:
                self._aifa_policy.validate_exec(payload.command, rootfs_path=session.rootfs_path)
                decision = self._policy.validate_exec(payload.command, payload.cwd, payload.env)
            except Exception:
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        **_exec_request_diagnostics(
                            command=payload.command,
                            cwd=payload.cwd,
                            env=payload.env,
                            timeout_sec=timeout_sec,
                            stdin=stdin,
                            stdin_encoding=payload.stdin_encoding,
                        ),
                        "phase": "stream-policy-denied",
                    }
                )
                await self._audit.add_event(
                    session_id=session.id,
                    kind="policy.deny",
                    actor=auth.actor,
                    payload={"command": payload.command, "cwd": payload.cwd, "stream": True},
                )
                raise
            exit_event: ExecStreamEvent | None = None

            async with session_exec_operations.track(session.id):
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        **_exec_request_diagnostics(
                            command=payload.command,
                            cwd=decision.cwd,
                            env=decision.env,
                            timeout_sec=timeout_sec,
                            stdin=stdin,
                            stdin_encoding=payload.stdin_encoding,
                        ),
                        "phase": "stream-start",
                    }
                )
                await self._touch_session(session)
                await self._enforce_pre_exec_quota(session)
                await self._audit.add_event(
                    session_id=session.id,
                    kind="exec.request",
                    actor=auth.actor,
                    payload={
                        "command": payload.command,
                        "cwd": decision.cwd,
                        "timeoutSec": timeout_sec,
                        "stream": True,
                        **self._runtime.audit_context(),
                    },
                )
                workspace_root = session.rootfs_path
                async for event in self._runtime.stream_exec(
                    self._runtime_context(session),
                    command=payload.command,
                    cwd=decision.cwd,
                    env=decision.env,
                    timeout_sec=timeout_sec,
                    stdin=stdin,
                    disconnect_checker=disconnect_checker,
                ):
                    if event.kind == "exit":
                        exit_event = ExecStreamEvent(
                            kind=event.kind,
                            data=event.data,
                            exit_code=event.exit_code,
                            duration_ms=event.duration_ms,
                            timed_out=event.timed_out,
                            disconnected=event.disconnected,
                            workspace_root=workspace_root,
                            kill_reason=event.kill_reason,
                        )
                        yield exit_event
                    else:
                        yield event

                await self._files.refresh_usage(session)
                await self._touch_session(session)
                if exit_event is None:
                    exit_event = ExecStreamEvent(
                        kind="exit",
                        exit_code=0,
                        duration_ms=0,
                        timed_out=False,
                        workspace_root=workspace_root,
                    )
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        "phase": "stream-finish",
                        "exitCode": exit_event.exit_code,
                        "timedOut": exit_event.timed_out,
                        "durationMs": exit_event.duration_ms,
                        "disconnected": bool(exit_event.disconnected),
                        **self._runtime.audit_context(),
                    }
                )
                await self._audit.add_event(
                    session_id=session.id,
                    kind="exec.response",
                    actor=auth.actor,
                    payload={
                        "exitCode": exit_event.exit_code,
                        "timedOut": exit_event.timed_out,
                        "durationMs": exit_event.duration_ms,
                        "stream": True,
                        "disconnected": bool(exit_event.disconnected),
                        **self._runtime.audit_context(),
                    },
                )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )

    async def cleanup_expired_sessions(self) -> int:
        op = "SessionService.cleanup_expired_sessions"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {}}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            expired_sessions = await self._sessions.list_expired_sessions(utcnow())
            cleaned = 0
            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "session-cleanup-start",
                    "expiredCount": len(expired_sessions),
                }
            )
            for session in expired_sessions:
                busy_lock = None
                if session_exec_operations.is_busy(session.id):
                    busy_lock = "exec"
                elif session_file_locks.is_busy(session.id):
                    busy_lock = "file"
                if busy_lock is not None:
                    extra_diagnostics["diagnostics"].update(
                        {
                            **_session_diagnostics(session),
                            "phase": "session-cleanup-skip-busy",
                            "busyLock": busy_lock,
                        }
                    )
                    continue
                try:
                    async with session_exec_operations.exclusive(session.id):
                        if session_file_locks.is_busy(session.id):
                            extra_diagnostics["diagnostics"].update(
                                {
                                    **_session_diagnostics(session),
                                    "phase": "session-cleanup-skip-busy",
                                    "busyLock": "file",
                                }
                            )
                            continue
                        await self._terminate_existing_session(
                            session,
                            actor="cleanup-worker",
                            audit_kind="session.cleanup",
                        )
                except InvalidStateException:
                    extra_diagnostics["diagnostics"].update(
                        {
                            **_session_diagnostics(session),
                            "phase": "session-cleanup-skip-busy",
                            "busyLock": "exec",
                        }
                    )
                    continue
                except Exception as exc:
                    extra_diagnostics["diagnostics"].update(
                        {
                            **_session_diagnostics(session),
                            "phase": "session-cleanup-failed",
                            "errorType": type(exc).__name__,
                            "errorMessage": str(exc),
                        }
                    )
                    continue
                cleaned += 1
            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "session-cleanup-finish",
                    "expiredCount": len(expired_sessions),
                    "cleanedCount": cleaned,
                }
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return cleaned

    async def reconcile_sessions(self) -> ReconcileResult:
        op = "SessionService.reconcile_sessions"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {}}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            sessions = await self._sessions.list_non_terminated_sessions()
            failed_missing_rootfs = 0
            rootfs_root = Path(settings.ROOTFS_ROOT)
            rootfs_root.mkdir(parents=True, exist_ok=True)
            known_rootfs_paths = {Path(session.rootfs_path).resolve(strict=False) for session in sessions}
            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "session-reconcile-start",
                    "sessionCount": len(sessions),
                    "rootfsRoot": str(rootfs_root),
                }
            )

            for session in sessions:
                if Path(session.rootfs_path).exists():
                    await self._files.refresh_usage(session)
                    continue
                if session.status == SessionStatus.FAILED.value:
                    continue
                await self._set_status(
                    session,
                    SessionStatus.FAILED,
                    fail_reason="rootfs missing on startup reconcile",
                )
                await self._audit.add_event(
                    session_id=session.id,
                    kind="session.reconcile",
                    actor="startup-reconcile",
                    payload={"missingRootfs": True, "status": session.status},
                )
                extra_diagnostics["diagnostics"].update(
                    {
                        **_session_diagnostics(session),
                        "phase": "session-reconcile-missing-rootfs",
                    }
                )
                failed_missing_rootfs += 1

            removed_orphans = 0
            for child in rootfs_root.iterdir():
                child_path = child.resolve(strict=False)
                if child_path in known_rootfs_paths:
                    continue
                await self._runtime.destroy_rootfs(str(child))
                extra_diagnostics["diagnostics"].update(
                    {
                        "phase": "session-reconcile-removed-orphan",
                        "rootfsPath": str(child),
                    }
                )
                removed_orphans += 1

            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "session-reconcile-finish",
                    "failedMissingRootfs": failed_missing_rootfs,
                    "removedOrphans": removed_orphans,
                }
            )
            result = ReconcileResult(
                failed_missing_rootfs=failed_missing_rootfs,
                removed_orphans=removed_orphans,
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    async def upload_file(self, auth: SessionAuthInfo, *, path: str, content: bytes) -> UploadResult:
        op = "SessionService.upload_file"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {}}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            session = await self._require_session(auth.session_id)
            ensure_action_allowed(session.status, "upload")
            async with session_file_locks.serialize(session.id):
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        "phase": "file-upload-start",
                        "path": path,
                        "bytes": len(content),
                    }
                )
                await self._touch_session(session)
                result = await self._files.upload_file(session, path=path, content=content)
                await self._touch_session(session)
                await self._audit.add_event(
                    session_id=session.id,
                    kind="file.upload",
                    actor=auth.actor,
                    payload={"path": result.path, "bytes": result.bytes_written, "sha256": result.sha256},
                )
                extra_diagnostics["diagnostics"].update(
                    {
                        **_auth_diagnostics(auth),
                        **_session_diagnostics(session),
                        "phase": "file-upload-finish",
                        "path": result.path,
                        "bytes": result.bytes_written,
                        "sha256": result.sha256,
                        "fileBytesUsed": session.file_bytes_used,
                    }
                )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    async def download_file(self, auth: SessionAuthInfo, *, path: str) -> DownloadedFile:
        op = "SessionService.download_file"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {}}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            session = await self._require_session(auth.session_id)
            ensure_action_allowed(session.status, "download")
            extra_diagnostics["diagnostics"].update(
                {
                    **_auth_diagnostics(auth),
                    **_session_diagnostics(session),
                    "phase": "file-download-start",
                    "path": path,
                }
            )
            await self._touch_session(session)
            result = await self._files.resolve_file(session, path=path)
            await self._touch_session(session)
            await self._audit.add_event(
                session_id=session.id,
                kind="file.download",
                actor=auth.actor,
                payload={"path": result.path, "bytes": result.bytes_count, "sha256": result.sha256},
            )
            extra_diagnostics["diagnostics"].update(
                {
                    **_auth_diagnostics(auth),
                    **_session_diagnostics(session),
                    "phase": "file-download-finish",
                    "path": result.path,
                    "bytes": result.bytes_count,
                    "sha256": result.sha256,
                }
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    async def list_files(self, auth: SessionAuthInfo, payload: FileListRequest) -> FileNode:
        op = "SessionService.list_files"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {}}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            session = await self._require_session(auth.session_id)
            ensure_action_allowed(session.status, "list_files")
            extra_diagnostics["diagnostics"].update(
                {
                    **_auth_diagnostics(auth),
                    **_session_diagnostics(session),
                    "phase": "file-list-start",
                    "path": payload.path,
                    "recursive": payload.recursive,
                    "maxDepth": payload.max_depth,
                    "includeSha256": payload.include_sha256,
                }
            )
            await self._touch_session(session)
            result = await self._files.list_files(
                session,
                path=payload.path,
                recursive=payload.recursive,
                max_depth=payload.max_depth,
                include_sha256=payload.include_sha256,
            )
            await self._touch_session(session)
            await self._audit.add_event(
                session_id=session.id,
                kind="file.list",
                actor=auth.actor,
                payload={"path": payload.path, "recursive": payload.recursive, "maxDepth": payload.max_depth},
            )
            extra_diagnostics["diagnostics"].update(
                {
                    **_auth_diagnostics(auth),
                    **_session_diagnostics(session),
                    "phase": "file-list-finish",
                    "path": result.path,
                    "kind": result.kind,
                    "childCount": len(result.children),
                }
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    async def _require_session(self, session_id: str) -> Session:
        session = await self._sessions.get_by_id(session_id)
        if session is None:
            raise NotFoundException("Сессия не найдена", details={"sessionId": session_id})
        return session

    def _resolve_ttl(self, *, value: int | None, default: int, field_name: str) -> int:
        resolved = default if value is None else value
        if resolved <= 0:
            raise ValidationErrorException(
                f"{field_name} must be greater than zero",
                details={"field": field_name},
            )
        if resolved > default:
            raise ValidationErrorException(
                f"{field_name} exceeds service hard cap",
                details={"field": field_name, "max": default},
            )
        return resolved

    def _resolve_exec_timeout(self, timeout_sec: int | None) -> int:
        resolved = settings.EXEC_TIMEOUT_SEC if timeout_sec is None else timeout_sec
        if resolved <= 0:
            raise ValidationErrorException("timeoutSec must be greater than zero", details={"field": "timeoutSec"})
        if resolved > settings.EXEC_TIMEOUT_SEC:
            raise ValidationErrorException(
                "timeoutSec exceeds service hard cap",
                details={"field": "timeoutSec", "max": settings.EXEC_TIMEOUT_SEC},
            )
        return resolved

    def _validate_session_id(self, session_id: str) -> None:
        if not session_id.strip():
            raise ValidationErrorException("X-Session-Id must not be empty")
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise ValidationErrorException(
                "X-Session-Id must contain only letters, digits, dots, underscores and hyphens",
                details={"field": "X-Session-Id"},
            )

    def _rootfs_path(self, session_id: str) -> Path:
        rootfs_root = Path(settings.ROOTFS_ROOT).resolve(strict=False)
        candidate = (rootfs_root / session_id).resolve(strict=False)
        if not candidate.is_relative_to(rootfs_root):
            raise ValidationErrorException(
                "X-Session-Id resolves outside ROOTFS_ROOT",
                details={"field": "X-Session-Id"},
            )
        return candidate

    def _generate_token(self) -> str:
        return secrets.token_urlsafe(32)

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _decode_stdin(self, stdin: str | None, encoding: str) -> str | bytes | None:
        if stdin is None:
            return None
        if encoding == "utf8":
            return stdin
        if encoding == "base64":
            try:
                return base64.b64decode(stdin, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValidationErrorException(
                    "stdin is not valid base64",
                    details={"field": "stdin"},
                ) from exc
        raise ValidationErrorException(
            "stdinEncoding must be either 'utf8' or 'base64'",
            details={"field": "stdinEncoding"},
        )

    def _runtime_context(self, session: Session) -> RuntimeContext:
        return RuntimeContext(
            session_id=session.id,
            rootfs_path=session.rootfs_path,
        )

    async def _enforce_pre_exec_quota(self, session: Session) -> None:
        await self._files.refresh_usage(session)
        if session.file_bytes_used <= settings.MAX_TOTAL_BYTES:
            return
        logger.warning(
            "exec.pre_quota_block",
            extra={
                "event.category": "processing",
                "event.action": "skip",
                "app.operation": "SessionService._enforce_pre_exec_quota",
                "event.outcome": "failure",
                "app.status": "skipped",
                "diagnostics": {
                    **_session_diagnostics(session),
                    "phase": "exec-pre-quota-block",
                    "fileBytesUsed": session.file_bytes_used,
                    "maxTotalBytes": settings.MAX_TOTAL_BYTES,
                },
            },
        )
        raise QuotaExceededException(
            "session total bytes quota exceeded",
            details={
                "sessionId": session.id,
                "maxTotalBytes": settings.MAX_TOTAL_BYTES,
                "fileBytesUsed": session.file_bytes_used,
            },
        )

    async def _touch_session(self, session: Session) -> Session:
        current = utcnow()
        expires_at = self._calculate_expires_at(
            created_at=session.created_at,
            current=current,
            idle_timeout_sec=session.idle_timeout_sec,
            absolute_ttl_sec=session.absolute_ttl_sec,
        )

        # Debounce: skip the DB write if we touched the same session very recently.
        # Trades up to SESSION_TOUCH_DEBOUNCE_SEC of TTL staleness for far fewer
        # SQLite write-lock acquisitions on hot sessions.
        debounce_sec = settings.SESSION_TOUCH_DEBOUNCE_SEC
        last = _last_touch_at.get(session.id)
        if last is not None and (current - last).total_seconds() < debounce_sec:
            return session
        _last_touch_at[session.id] = current

        # Run the UPDATE in its own short-lived AsyncSession so the request
        # transaction never holds the SQLite write lock during slow exec/FS work.
        # Use a bulk UPDATE (no ORM load) so the lock is held for milliseconds.
        bind = self._sessions.session.bind
        assert bind is not None
        maker = async_sessionmaker(bind=bind, expire_on_commit=False, autoflush=False)
        async with maker() as s:
            await s.execute(
                sa_update(Session)
                .where(Session.id == session.id)
                .values(last_used_at=current, updated_at=current, expires_at=expires_at)
            )
            await s.commit()
        return session

    def _calculate_expires_at(
        self,
        *,
        created_at: datetime,
        current: datetime,
        idle_timeout_sec: int,
        absolute_ttl_sec: int,
    ) -> datetime:
        absolute_deadline = ensure_utc(created_at) + timedelta(seconds=absolute_ttl_sec)
        idle_deadline = current + timedelta(seconds=idle_timeout_sec)
        return min(absolute_deadline, idle_deadline)

    async def _provision_session_rootfs(self, session: Session) -> None:
        op = "SessionService._provision_session_rootfs"
        started = time.perf_counter()
        extra_diagnostics = {"diagnostics": {**_session_diagnostics(session), "phase": "session-provision-start"}}
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
                **extra_diagnostics,
            },
        )
        try:
            await self._runtime.provision_rootfs(session.rootfs_path)
        except Exception as exc:
            await self._set_status(session, SessionStatus.FAILED, fail_reason=str(exc))
            await self._audit.add_event(
                session_id=session.id,
                kind="state.transition",
                actor=f"session:{session.id}",
                payload={"from": SessionStatus.PROVISIONING.value, "to": SessionStatus.FAILED.value},
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            extra_diagnostics["diagnostics"].update(
                {
                    **_session_diagnostics(session),
                    "phase": "session-provision-failed",
                    "errorType": type(exc).__name__,
                    "errorMessage": str(exc),
                }
            )
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            if isinstance(exc, InternalServerErrorException):
                raise
            raise InternalServerErrorException(
                "failed to provision session runtime",
                details={"sessionId": session.id},
                additional=str(exc),
            ) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        extra_diagnostics["diagnostics"].update({**_session_diagnostics(session), "phase": "session-provision-finish"})
        logger.info(
            f"{op} success",
            extra={
                "event.category": "processing",
                "event.action": "complete",
                "app.operation": op,
                "event.outcome": "success",
                "app.status": "processed",
                "app.duration_ms": elapsed_ms,
                **extra_diagnostics,
            },
        )

    async def _restore_session_workspace(self, session: Session, payload: SessionCreateRequest) -> None:
        op = "SessionService._restore_session_workspace"
        started = time.perf_counter()
        request = self._workspace_persistence_request(payload)
        if request is None:
            await self._workspace_persistence.restore(session, None)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} skipped",
                extra={
                    "event.category": "processing",
                    "event.action": "skip",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "skipped",
                    "app.duration_ms": elapsed_ms,
                    "diagnostics": {**_session_diagnostics(session), "phase": "workspace-restore-skip"},
                },
            )
            return
        extra_diagnostics = {
            "diagnostics": {
                **_session_diagnostics(session),
                "phase": "workspace-restore-start",
                "workspaceScopeId": request.scope_id,
                "workspaceRoot": request.root,
                "storeEnabled": self._workspace_persistence.enabled,
            }
        }
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
                **extra_diagnostics,
            },
        )
        try:
            result = await asyncio.wait_for(
                self._workspace_persistence.restore(session, request),
                timeout=settings.WORKSPACE_STORE_RESTORE_TIMEOUT_SEC,
            )
            await self._files.refresh_usage(session)
        except Exception as exc:
            await self._set_status(session, SessionStatus.FAILED, fail_reason=str(exc))
            await self._audit.add_event(
                session_id=session.id,
                kind="state.transition",
                actor=f"session:{session.id}",
                payload={"from": SessionStatus.PROVISIONING.value, "to": SessionStatus.FAILED.value},
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            extra_diagnostics["diagnostics"].update(
                {
                    **_session_diagnostics(session),
                    "phase": "workspace-restore-failed",
                    "errorType": type(exc).__name__,
                    "errorMessage": str(exc),
                }
            )
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            if isinstance(exc, InternalServerErrorException):
                raise
            raise InternalServerErrorException(
                "failed to restore workspace",
                details={"sessionId": session.id},
                additional=str(exc),
            ) from exc
        await self._audit.add_event(
            session_id=session.id,
            kind="workspace.restore",
            actor=f"session:{session.id}",
            payload={
                "enabled": result.enabled,
                "restored": result.restored,
                "revisionId": result.revision_id,
                "fileCount": result.file_count,
                "bytesCount": result.bytes_count,
            },
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        extra_diagnostics["diagnostics"].update(
            {
                **_session_diagnostics(session),
                "phase": "workspace-restore-finish",
                "storeEnabled": result.enabled,
                "restored": result.restored,
                "revisionId": result.revision_id,
                "fileCount": result.file_count,
                "bytesCount": result.bytes_count,
            }
        )
        logger.info(
            f"{op} success",
            extra={
                "event.category": "processing",
                "event.action": "complete",
                "app.operation": op,
                "event.outcome": "success",
                "app.status": "processed",
                "app.duration_ms": elapsed_ms,
                **extra_diagnostics,
            },
        )

    async def _snapshot_session_workspace(self, session: Session, *, actor: str) -> None:
        op = "SessionService._snapshot_session_workspace"
        started = time.perf_counter()
        if not session.workspace_persistence_scope_id:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} skipped",
                extra={
                    "event.category": "processing",
                    "event.action": "skip",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "skipped",
                    "app.duration_ms": elapsed_ms,
                    "diagnostics": {
                        **_session_diagnostics(session),
                        "phase": "workspace-snapshot-skip",
                        "actor": actor,
                    },
                },
            )
            return
        extra_diagnostics = {
            "diagnostics": {
                **_session_diagnostics(session),
                "phase": "workspace-snapshot-start",
                "workspaceScopeId": session.workspace_persistence_scope_id,
                "workspaceRoot": session.workspace_persistence_root,
                "storeEnabled": self._workspace_persistence.enabled,
            }
        }
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
                **extra_diagnostics,
            },
        )
        try:
            result = await asyncio.wait_for(
                self._workspace_persistence.snapshot(session),
                timeout=settings.WORKSPACE_STORE_SNAPSHOT_TIMEOUT_SEC,
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            extra_diagnostics["diagnostics"].update(
                {
                    **_session_diagnostics(session),
                    "phase": "workspace-snapshot-failed",
                    "errorType": type(exc).__name__,
                    "errorMessage": str(exc),
                }
            )
            logger.error(
                f"{op} failed",
                exc_info=True,
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            if isinstance(exc, InternalServerErrorException):
                raise
            raise InternalServerErrorException(
                "failed to snapshot workspace",
                details={"sessionId": session.id},
                additional=str(exc),
            ) from exc
        await self._audit.add_event(
            session_id=session.id,
            kind="workspace.snapshot",
            actor=actor,
            payload={
                "enabled": result.enabled,
                "snapshot": result.snapshot,
                "revisionId": result.revision_id,
                "fileCount": result.file_count,
                "bytesCount": result.bytes_count,
            },
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        extra_diagnostics["diagnostics"].update(
            {
                **_session_diagnostics(session),
                "phase": "workspace-snapshot-finish",
                "storeEnabled": result.enabled,
                "snapshot": result.snapshot,
                "revisionId": result.revision_id,
                "fileCount": result.file_count,
                "bytesCount": result.bytes_count,
            }
        )
        logger.info(
            f"{op} success",
            extra={
                "event.category": "processing",
                "event.action": "complete",
                "app.operation": op,
                "event.outcome": "success",
                "app.status": "processed",
                "app.duration_ms": elapsed_ms,
                **extra_diagnostics,
            },
        )

    def _workspace_persistence_request(
        self,
        payload: SessionCreateRequest,
    ) -> WorkspacePersistenceRequest | None:
        if payload.workspace_persistence is None:
            return None
        return WorkspacePersistenceRequest(
            scope_id=payload.workspace_persistence.scope_id,
            root=payload.workspace_persistence.root,
        )

    async def _persist_failed_creation(
        self,
        *,
        session_id: str,
        rootfs_path: str,
        payload: SessionCreateRequest,
        idle_timeout_sec: int,
        absolute_ttl_sec: int,
        fail_reason: str,
    ) -> None:
        # Intentional deviation from invariant §2.4 ("only get_session commits"):
        # the caller's request transaction is about to roll back (we re-raise), so
        # we open an independent session here to persist the FAILED state + audit
        # row so the failure survives the rollback.
        bind = self._sessions.session.bind
        assert bind is not None
        maker = async_sessionmaker(bind=bind, expire_on_commit=False, autoflush=False)
        async with maker() as session:
            sessions = SessionRepository(session)
            audit = AuditEventRepository(session)
            failed = await sessions.get_by_id(session_id)
            if failed is None:
                current = utcnow()
                failed = await sessions.create(
                    id=session_id,
                    status=SessionStatus.PROVISIONING.value,
                    token_hash=self._hash_token(self._generate_token()),
                    rootfs_path=rootfs_path,
                    created_at=current,
                    last_used_at=current,
                    expires_at=self._calculate_expires_at(
                        created_at=current,
                        current=current,
                        idle_timeout_sec=idle_timeout_sec,
                        absolute_ttl_sec=absolute_ttl_sec,
                    ),
                    idle_timeout_sec=idle_timeout_sec,
                    absolute_ttl_sec=absolute_ttl_sec,
                )
            await sessions.set_status(failed, SessionStatus.FAILED, fail_reason=fail_reason)
            await audit.add_event(
                session_id=failed.id,
                kind="state.transition",
                actor=f"session:{failed.id}",
                payload={"from": SessionStatus.PROVISIONING.value, "to": SessionStatus.FAILED.value},
            )
            await session.commit()
        await self._runtime.destroy_rootfs(rootfs_path)

    async def _terminate_existing_session(
        self,
        session: Session,
        *,
        actor: str,
        audit_kind: str,
    ) -> Session:
        op = "SessionService._terminate_existing_session"
        started = time.perf_counter()
        if session.status == SessionStatus.TERMINATED.value:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} skipped",
                extra={
                    "event.category": "processing",
                    "event.action": "skip",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "skipped",
                    "app.duration_ms": elapsed_ms,
                    "diagnostics": {
                        **_session_diagnostics(session),
                        "phase": "session-terminate-skip-already-terminated",
                        "actor": actor,
                        "auditKind": audit_kind,
                    },
                },
            )
            return session

        previous_status = session.status
        extra_diagnostics = {
            "diagnostics": {
                **_session_diagnostics(session),
                "phase": "session-terminate-start",
                "actor": actor,
                "auditKind": audit_kind,
            }
        }
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
                **extra_diagnostics,
            },
        )
        try:
            await self._snapshot_session_workspace(session, actor=actor)
            if session.status != SessionStatus.TERMINATING.value:
                await self._set_status(session, SessionStatus.TERMINATING)
                await self._audit.add_event(
                    session_id=session.id,
                    kind="state.transition",
                    actor=actor,
                    payload={"from": previous_status, "to": SessionStatus.TERMINATING.value},
                )

            await self._runtime.destroy_rootfs(session.rootfs_path)
            await self._set_status(session, SessionStatus.TERMINATED)
            await self._audit.add_event(
                session_id=session.id,
                kind="state.transition",
                actor=actor,
                payload={"from": SessionStatus.TERMINATING.value, "to": SessionStatus.TERMINATED.value},
            )
            await self._audit.add_event(
                session_id=session.id,
                kind=audit_kind,
                actor=actor,
                payload={"status": session.status},
            )
            _last_touch_at.pop(session.id, None)
            await self._publish_session_deleted(session)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            extra_diagnostics["diagnostics"].update(
                {
                    **_session_diagnostics(session),
                    "phase": "session-terminate-failed",
                    "actor": actor,
                    "auditKind": audit_kind,
                    "errorType": type(exc).__name__,
                    "errorMessage": str(exc),
                }
            )
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        extra_diagnostics["diagnostics"].update(
            {
                **_session_diagnostics(session),
                "phase": "session-terminate-finish",
                "actor": actor,
                "auditKind": audit_kind,
                "previousStatus": previous_status,
            }
        )
        logger.info(
            f"{op} success",
            extra={
                "event.category": "processing",
                "event.action": "complete",
                "app.operation": op,
                "event.outcome": "success",
                "app.status": "processed",
                "app.duration_ms": elapsed_ms,
                **extra_diagnostics,
            },
        )
        return session

    async def _set_status(
        self,
        session: Session,
        next_status: SessionStatus,
        *,
        fail_reason: str | None = None,
    ) -> Session:
        ensure_transition(session.status, next_status.value)
        return await self._sessions.set_status(session, next_status, fail_reason=fail_reason)

    async def _publish_session_created(self, session: Session) -> None:
        """Опубликовать событие создания сессии без влияния на основной lifecycle."""
        try:
            total = await self._sessions.count_ready_sessions()
            await self._system_events.publish_session_created(session, total_sandbox_sessions=total)
        except Exception:
            logger.exception("session.system_event_create_failed session_id=%s", session.id)

    async def _publish_session_deleted(self, session: Session) -> None:
        """Опубликовать событие завершения сессии без влияния на основной lifecycle."""
        try:
            total = await self._sessions.count_ready_sessions()
            await self._system_events.publish_session_deleted(session, total_sandbox_sessions=total)
        except Exception:
            logger.exception("session.system_event_delete_failed session_id=%s", session.id)


def _session_diagnostics(session: Session) -> dict[str, object]:
    return {
        "sessionId": session.id,
        "status": session.status,
        "rootfsPath": session.rootfs_path,
        "fileBytesUsed": session.file_bytes_used,
        "createdAt": session.created_at.isoformat() if session.created_at else None,
        "lastUsedAt": session.last_used_at.isoformat() if session.last_used_at else None,
        "expiresAt": session.expires_at.isoformat() if session.expires_at else None,
    }


def _auth_diagnostics(auth: SessionAuthInfo) -> dict[str, object]:
    return {
        "actor": auth.actor,
        "authSessionId": auth.session_id,
    }


def _exec_request_diagnostics(
    *,
    command: list[str],
    cwd: str,
    env: dict[str, str],
    timeout_sec: int,
    stdin: str | bytes | None,
    stdin_encoding: str,
) -> dict[str, object]:
    return {
        "command": redact_log_value(command),
        "cwd": cwd,
        "envKeys": sorted(env.keys()),
        "timeoutSec": timeout_sec,
        "stdinBytes": _stdin_size(stdin),
        "stdinEncoding": stdin_encoding,
    }


def _stdin_size(stdin: str | bytes | None) -> int:
    if stdin is None:
        return 0
    if isinstance(stdin, bytes):
        return len(stdin)
    return len(stdin.encode("utf-8"))
