"""Serializers for session domain DTOs."""

from __future__ import annotations

from app.core.config import settings
from app.models.session import Session
from app.schemas.session import (
    ExecResponse,
    FileNodeResponse,
    FileUploadResponse,
    SessionCreateResponse,
    SessionQuotaSchema,
    SessionRead,
    SessionTerminateResponse,
    SessionWorkspacePersistenceState,
)
from app.serializers.sqla import SQLASerializer
from app.services.file_gateway import FileNode, UploadResult
from app.services.runtime.types import ExecResult
from app.utils.time import ensure_utc


class SessionSerializer(SQLASerializer[Session, SessionRead]):
    """Session response builder.

    Inherits `SQLASerializer.get_single/get_list` for the plain `Session → SessionRead`
    path. Create/terminate/exec/upload/file-node responses are separate shapes that
    wrap the ORM row with extra context (token, quotas, runtime results), so they
    stay as dedicated methods on top of the base.
    """

    def __init__(self) -> None:
        super().__init__(read_schema=SessionRead)

    def _serialize_datetime(self, value):
        return ensure_utc(value) if value is not None else None

    def _quotas(self) -> SessionQuotaSchema:
        return SessionQuotaSchema(
            max_file_bytes=settings.MAX_FILE_SIZE,
            max_total_bytes=settings.MAX_TOTAL_BYTES,
            exec_timeout_sec=settings.EXEC_TIMEOUT_SEC,
        )

    def _workspace_persistence(self, session: Session) -> SessionWorkspacePersistenceState | None:
        if not session.workspace_persistence_scope_id and not settings.WORKSPACE_STORE_ENABLED:
            return None
        return SessionWorkspacePersistenceState(
            enabled=settings.WORKSPACE_STORE_ENABLED,
            root=session.workspace_persistence_root,
            scope_id=session.workspace_persistence_scope_id,
            restored_revision_id=session.workspace_restored_revision_id,
            snapshot_revision_id=session.workspace_snapshot_revision_id,
        )

    def serialize_read(self, session: Session) -> SessionRead:
        return SessionRead(
            id=session.id,
            status=session.status,
            created_at=self._serialize_datetime(session.created_at),
            updated_at=self._serialize_datetime(session.updated_at),
            last_used_at=self._serialize_datetime(session.last_used_at),
            expires_at=self._serialize_datetime(session.expires_at),
            file_bytes_used=session.file_bytes_used,
            fail_reason=session.fail_reason,
            quotas=self._quotas(),
            workspace_persistence=self._workspace_persistence(session),
        )

    def serialize_create(self, session: Session, *, token: str | None) -> SessionCreateResponse:
        return SessionCreateResponse(
            id=session.id,
            status=session.status,
            token=token,
            created_at=self._serialize_datetime(session.created_at),
            expires_at=self._serialize_datetime(session.expires_at),
            quotas=self._quotas(),
            workspace_persistence=self._workspace_persistence(session),
        )

    def serialize_terminate(self, session: Session) -> SessionTerminateResponse:
        assert session.terminated_at is not None
        return SessionTerminateResponse(
            id=session.id,
            status=session.status,
            terminated_at=self._serialize_datetime(session.terminated_at),
        )

    def serialize_exec(self, result: ExecResult, *, workspace_root: str) -> ExecResponse:
        return ExecResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            workspace_root=workspace_root,
        )

    def serialize_upload(self, result: UploadResult) -> FileUploadResponse:
        return FileUploadResponse(
            path=result.path,
            bytes=result.bytes_written,
            sha256=result.sha256,
        )

    def serialize_file_node(self, node: FileNode) -> FileNodeResponse:
        return FileNodeResponse(
            name=node.name,
            path=node.path,
            kind=node.kind,
            bytes=node.bytes_count,
            sha256=node.sha256,
            children=[self.serialize_file_node(child) for child in node.children],
        )
