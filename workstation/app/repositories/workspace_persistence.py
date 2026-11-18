"""Repositories for durable /workspace snapshot metadata."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workspace_persistence import WorkspaceFile, WorkspaceRevision, WorkspaceScope
from app.repositories.sqla import BaseSQLARepository
from app.utils.time import utcnow


@dataclass(slots=True, frozen=True)
class WorkspaceFileRecord:
    path: str
    sha256: str
    size: int
    object_key: str
    mode: int
    mtime_ns: int


class WorkspacePersistenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._scopes = BaseSQLARepository(session=session, model=WorkspaceScope)
        self._revisions = BaseSQLARepository(session=session, model=WorkspaceRevision)

    async def get_scope(self, scope_id: str) -> WorkspaceScope | None:
        return await self._session.get(WorkspaceScope, scope_id)

    async def get_revision(self, revision_id: str) -> WorkspaceRevision | None:
        return await self._session.get(WorkspaceRevision, revision_id)

    async def get_latest_revision(self, scope_id: str) -> WorkspaceRevision | None:
        scope = await self.get_scope(scope_id)
        if scope is None or not scope.latest_revision_id:
            return None
        return await self.get_revision(scope.latest_revision_id)

    async def list_revision_files(self, revision_id: str) -> list[WorkspaceFile]:
        stmt = select(WorkspaceFile).where(WorkspaceFile.revision_id == revision_id).order_by(WorkspaceFile.path)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def persist_successful_revision(
        self,
        *,
        revision_id: str,
        scope_id: str,
        session_id: str,
        root: str,
        manifest_object_key: str,
        files: Sequence[WorkspaceFileRecord],
        bytes_count: int,
        is_latest: bool,
    ) -> WorkspaceRevision:
        current = utcnow()
        scope = await self.get_scope(scope_id)
        if scope is None:
            scope = await self._scopes.create(
                id=scope_id,
                latest_revision_id=None,
                file_count=0,
                bytes_count=0,
                created_at=current,
                updated_at=current,
            )
        revision = await self._revisions.create(
            id=revision_id,
            scope_id=scope_id,
            created_from_session_id=session_id,
            status="complete",
            root=root,
            manifest_object_key=manifest_object_key,
            file_count=len(files),
            bytes_count=bytes_count,
            error=None,
            created_at=current,
        )
        self._session.add_all(
            [
                WorkspaceFile(
                    revision_id=revision_id,
                    path=file.path,
                    sha256=file.sha256,
                    size=file.size,
                    object_key=file.object_key,
                    mode=file.mode,
                    mtime_ns=file.mtime_ns,
                )
                for file in files
            ]
        )
        # Audit mirror of the S3 pointer: restore reads scopes/<id>/latest.json, so this
        # column must not claim a superseded revision is current.
        if is_latest:
            scope.latest_revision_id = revision_id
            scope.file_count = len(files)
            scope.bytes_count = bytes_count
        scope.updated_at = current
        await self._session.flush()
        await self._session.refresh(revision)
        return revision
