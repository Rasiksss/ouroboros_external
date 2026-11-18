"""Durable /workspace snapshot and restore support."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import stat
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.outbound_tracing import add_botocore_tracing_headers
from app.models.session import Session
from app.repositories.workspace_persistence import WorkspaceFileRecord, WorkspacePersistenceRepository
from app.utils.exceptions.common import InternalServerErrorException, ValidationErrorException
from app.utils.time import ensure_utc

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = "/workspace"

# Restore reads the latest pointer from the object store, never from the database:
# the station's DB may be ephemeral (see DATABASE_URL), while S3 holds both the data
# and the pointer, so a wiped DB no longer orphans persisted workspaces.
# v2 manifests add sessionCreatedAt/parentRevisionId; v1 manifests written before
# ordering existed lack both and are still restorable.
MANIFEST_SCHEMA_VERSION = 2


@dataclass(slots=True, frozen=True)
class WorkspacePersistenceRequest:
    scope_id: str
    root: str = WORKSPACE_ROOT


@dataclass(slots=True, frozen=True)
class WorkspacePersistenceResult:
    enabled: bool
    restored: bool = False
    snapshot: bool = False
    revision_id: str | None = None
    file_count: int = 0
    bytes_count: int = 0


@dataclass(slots=True, frozen=True)
class ScannedWorkspaceFile:
    relative_path: str
    absolute_path: Path
    size: int
    mode: int
    mtime_ns: int
    sha256: str


class WorkspaceObjectStore(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def put_blob(self, sha256: str, content: bytes) -> str: ...

    async def put_manifest(self, *, scope_id: str, revision_id: str, manifest: dict[str, Any]) -> str: ...

    async def put_latest(self, *, scope_id: str, manifest: dict[str, Any]) -> str: ...

    async def get_latest(self, scope_id: str) -> dict[str, Any] | None: ...

    async def get_json(self, object_key: str) -> dict[str, Any]: ...

    async def get_blob(self, object_key: str) -> bytes: ...


class DisabledWorkspaceStore:
    @property
    def enabled(self) -> bool:
        return False

    async def put_blob(self, sha256: str, content: bytes) -> str:
        raise RuntimeError("workspace store is disabled")

    async def put_manifest(self, *, scope_id: str, revision_id: str, manifest: dict[str, Any]) -> str:
        raise RuntimeError("workspace store is disabled")

    async def put_latest(self, *, scope_id: str, manifest: dict[str, Any]) -> str:
        raise RuntimeError("workspace store is disabled")

    async def get_latest(self, scope_id: str) -> dict[str, Any] | None:
        raise RuntimeError("workspace store is disabled")

    async def get_json(self, object_key: str) -> dict[str, Any]:
        raise RuntimeError("workspace store is disabled")

    async def get_blob(self, object_key: str) -> bytes:
        raise RuntimeError("workspace store is disabled")


class S3WorkspaceStore:
    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        region_name: str | None = None,
    ) -> None:
        try:
            import boto3  # type: ignore[import-not-found]
            from botocore.config import Config  # type: ignore[import-not-found]
            from botocore.exceptions import ClientError  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("boto3 is required when WORKSPACE_STORE_ENABLED=true") from exc
        # Credentials are passed in because secman secrets never land in
        # os.environ, where boto3's default chain would look for them. None
        # keeps that chain intact for role-based deployments.
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            aws_session_token=session_token,
            region_name=region_name,
            config=Config(s3={"addressing_style": "path"}),
        )
        self._client.meta.events.register(
            "before-call.s3",
            add_botocore_tracing_headers,
        )
        # Held on the instance because boto3 is imported lazily here; get_latest needs
        # it to tell "no pointer yet" apart from a real store failure.
        self._client_error = ClientError
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    @property
    def enabled(self) -> bool:
        return True

    def _key(self, suffix: str) -> str:
        return f"{self._prefix}/{suffix.lstrip('/')}" if self._prefix else suffix.lstrip("/")

    async def put_blob(self, sha256: str, content: bytes) -> str:
        key = self._key(f"blobs/{sha256[:2]}/{sha256}")
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=content,
            ContentType="application/octet-stream",
        )
        return key

    async def put_manifest(self, *, scope_id: str, revision_id: str, manifest: dict[str, Any]) -> str:
        return await self._put_json(self._key(f"scopes/{scope_id}/revisions/{revision_id}.json"), manifest)

    async def put_latest(self, *, scope_id: str, manifest: dict[str, Any]) -> str:
        return await self._put_json(self._latest_key(scope_id), manifest)

    async def get_latest(self, scope_id: str) -> dict[str, Any] | None:
        try:
            return await self.get_json(self._latest_key(scope_id))
        except self._client_error as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404"}:
                return None
            raise

    def _latest_key(self, scope_id: str) -> str:
        return self._key(f"scopes/{scope_id}/latest.json")

    async def _put_json(self, key: str, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        return key

    async def get_json(self, object_key: str) -> dict[str, Any]:
        response = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=object_key)
        body = await asyncio.to_thread(response["Body"].read)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("workspace manifest is not a JSON object")
        return payload

    async def get_blob(self, object_key: str) -> bytes:
        response = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=object_key)
        return await asyncio.to_thread(response["Body"].read)


def build_workspace_object_store() -> WorkspaceObjectStore:
    if not settings.WORKSPACE_STORE_ENABLED:
        return DisabledWorkspaceStore()
    backend = settings.WORKSPACE_STORE_BACKEND.lower()
    if backend != "s3":
        raise RuntimeError(f"unsupported WORKSPACE_STORE_BACKEND: {settings.WORKSPACE_STORE_BACKEND}")
    if not settings.WORKSPACE_STORE_S3_BUCKET:
        raise RuntimeError("WORKSPACE_STORE_S3_BUCKET is required when WORKSPACE_STORE_ENABLED=true")
    return S3WorkspaceStore(
        bucket=settings.WORKSPACE_STORE_S3_BUCKET,
        prefix=settings.WORKSPACE_STORE_S3_PREFIX,
        endpoint_url=settings.WORKSPACE_STORE_S3_ENDPOINT_URL,
        access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
        session_token=settings.AWS_SESSION_TOKEN or None,
        region_name=settings.AWS_REGION or None,
    )


class WorkspacePersistenceService:
    def __init__(
        self,
        *,
        repository: WorkspacePersistenceRepository,
        object_store: WorkspaceObjectStore,
    ) -> None:
        self._repo = repository
        self._store = object_store

    @property
    def enabled(self) -> bool:
        return self._store.enabled

    async def restore(
        self,
        session: Session,
        request: WorkspacePersistenceRequest | None,
    ) -> WorkspacePersistenceResult:
        if request is None:
            session.workspace_persistence_scope_id = None
            session.workspace_persistence_root = None
            session.workspace_restored_revision_id = None
            return WorkspacePersistenceResult(enabled=self.enabled)
        self._validate_request(request)
        session.workspace_persistence_scope_id = request.scope_id
        session.workspace_persistence_root = request.root
        session.workspace_restored_revision_id = None
        if not self.enabled:
            return WorkspacePersistenceResult(enabled=False)

        op = "WorkspacePersistenceService.restore"
        started = time.perf_counter()
        extra_diagnostics = {
            "diagnostics": {
                "phase": "workspace-restore-download-start",
                "sessionId": session.id,
                "workspaceScopeId": request.scope_id,
                "revisionId": None,
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
            manifest = await self._store.get_latest(request.scope_id)
            if manifest is None:
                # Distinct from a failure: the scope has never been snapshotted, so the
                # session legitimately starts with an empty workspace. Logged explicitly
                # because the caller re-seeds it and the result looks like a fresh start.
                extra_diagnostics["diagnostics"]["phase"] = "workspace-restore-empty"
                logger.info(
                    f"{op} empty",
                    extra={
                        "event.category": "processing",
                        "event.action": "complete",
                        "app.operation": op,
                        "event.outcome": "success",
                        "app.status": "processed",
                        "app.duration_ms": int((time.perf_counter() - started) * 1000),
                        **extra_diagnostics,
                    },
                )
                return WorkspacePersistenceResult(enabled=True)
            revision_id = _manifest_revision_id(manifest)
            extra_diagnostics["diagnostics"]["revisionId"] = revision_id
            await self._restore_manifest(session.rootfs_path, manifest)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "workspace-restore-download-failed",
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
            raise
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        file_count, bytes_count = _manifest_totals(manifest)
        extra_diagnostics["diagnostics"].update(
            {
                "phase": "workspace-restore-downloaded",
                "fileCount": file_count,
                "bytesCount": bytes_count,
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
        session.workspace_restored_revision_id = revision_id
        return WorkspacePersistenceResult(
            enabled=True,
            restored=True,
            revision_id=revision_id,
            file_count=file_count,
            bytes_count=bytes_count,
        )

    async def snapshot(self, session: Session) -> WorkspacePersistenceResult:
        scope_id = session.workspace_persistence_scope_id
        root = session.workspace_persistence_root
        if not scope_id or root != WORKSPACE_ROOT:
            return WorkspacePersistenceResult(enabled=self.enabled)
        if not self.enabled:
            return WorkspacePersistenceResult(enabled=False)
        if not Path(session.rootfs_path).exists():
            return WorkspacePersistenceResult(enabled=True)
        workspace_root = _workspace_host_path(session.rootfs_path)
        if workspace_root.is_symlink():
            raise InternalServerErrorException(
                "workspace root must not be a symlink",
                details={"sessionId": session.id},
            )

        revision_id = uuid4().hex
        scanned = list(_scan_workspace(session.rootfs_path))
        files: list[WorkspaceFileRecord] = []
        manifest_files: list[dict[str, Any]] = []
        bytes_count = 0

        op = "WorkspacePersistenceService.snapshot"
        started = time.perf_counter()
        extra_diagnostics = {
            "diagnostics": {
                "phase": "workspace-snapshot-upload-start",
                "sessionId": session.id,
                "workspaceScopeId": scope_id,
                "revisionId": revision_id,
                "totalFiles": len(scanned),
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
            for file in scanned:
                content = file.absolute_path.read_bytes()
                object_key = await self._store.put_blob(file.sha256, content)
                bytes_count += file.size
                files.append(
                    WorkspaceFileRecord(
                        path=file.relative_path,
                        sha256=file.sha256,
                        size=file.size,
                        object_key=object_key,
                        mode=file.mode,
                        mtime_ns=file.mtime_ns,
                    )
                )
                manifest_files.append(
                    {
                        "path": file.relative_path,
                        "sha256": file.sha256,
                        "size": file.size,
                        "objectKey": object_key,
                        "mode": file.mode,
                        "mtimeNs": file.mtime_ns,
                    }
                )

            session_created_at = _isoformat_or_none(session.created_at)
            manifest = {
                "schemaVersion": MANIFEST_SCHEMA_VERSION,
                "scopeId": scope_id,
                "revisionId": revision_id,
                "root": WORKSPACE_ROOT,
                "sessionCreatedAt": session_created_at,
                # Lineage is diagnostic only; ordering is decided by sessionCreatedAt
                # because sibling revisions can share a parent (two sessions restoring
                # the same revision) and lineage cannot order them.
                "parentRevisionId": session.workspace_restored_revision_id,
                "files": manifest_files,
            }
            manifest_key = await self._store.put_manifest(
                scope_id=scope_id,
                revision_id=revision_id,
                manifest=manifest,
            )
            # A session orphaned by a gateway restart is reaped later than the session
            # that replaced it, so termination order must not decide the pointer:
            # an older session never overwrites a newer session's workspace.
            is_latest = _supersedes_latest(
                await self._store.get_latest(scope_id),
                session_created_at=session_created_at,
                revision_id=revision_id,
            )
            if is_latest:
                await self._store.put_latest(scope_id=scope_id, manifest=manifest)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "workspace-snapshot-upload-failed",
                    "uploadedFiles": len(files),
                    "uploadedBytes": bytes_count,
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
            raise
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        extra_diagnostics["diagnostics"].update(
            {
                "phase": "workspace-snapshot-uploaded" if is_latest else "workspace-snapshot-superseded",
                "fileCount": len(files),
                "bytesCount": bytes_count,
                "latest": is_latest,
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
        revision = await self._repo.persist_successful_revision(
            revision_id=revision_id,
            scope_id=scope_id,
            session_id=session.id,
            root=WORKSPACE_ROOT,
            manifest_object_key=manifest_key,
            files=files,
            bytes_count=bytes_count,
            is_latest=is_latest,
        )
        session.workspace_snapshot_revision_id = revision.id
        return WorkspacePersistenceResult(
            enabled=True,
            snapshot=True,
            revision_id=revision.id,
            file_count=revision.file_count,
            bytes_count=revision.bytes_count,
        )

    def _validate_request(self, request: WorkspacePersistenceRequest) -> None:
        if request.root != WORKSPACE_ROOT:
            raise ValidationErrorException(
                "workspacePersistence.root must be /workspace",
                details={"field": "workspacePersistence.root"},
            )
        if not request.scope_id.strip():
            raise ValidationErrorException(
                "workspacePersistence.scopeId must not be empty",
                details={"field": "workspacePersistence.scopeId"},
            )
        if len(request.scope_id) > 128:
            raise ValidationErrorException(
                "workspacePersistence.scopeId is too long",
                details={"field": "workspacePersistence.scopeId", "max": 128},
            )

    async def _restore_manifest(self, rootfs_path: str, manifest: dict[str, Any]) -> None:
        if manifest.get("root") != WORKSPACE_ROOT:
            raise InternalServerErrorException("workspace manifest root mismatch")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise InternalServerErrorException("workspace manifest files must be a list")

        workspace_root = _workspace_host_path(rootfs_path)
        _replace_workspace_root(workspace_root)
        for raw_entry in files:
            if not isinstance(raw_entry, dict):
                raise InternalServerErrorException("workspace manifest file entry is invalid")
            entry = _normalize_manifest_entry(raw_entry)
            content = await self._store.get_blob(entry.object_key)
            actual_sha = hashlib.sha256(content).hexdigest()
            if actual_sha != entry.sha256:
                raise InternalServerErrorException(
                    "workspace blob checksum mismatch",
                    details={"path": entry.path},
                )
            if len(content) != entry.size:
                raise InternalServerErrorException(
                    "workspace blob size mismatch",
                    details={"path": entry.path},
                )
            target = workspace_root / Path(entry.path)
            _ensure_inside_workspace(workspace_root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            os.chmod(target, entry.mode & 0o777)
            os.utime(target, ns=(entry.mtime_ns, entry.mtime_ns))


def build_workspace_persistence_service(session: AsyncSession) -> WorkspacePersistenceService:
    return WorkspacePersistenceService(
        repository=WorkspacePersistenceRepository(session),
        object_store=build_workspace_object_store(),
    )


def _isoformat_or_none(value: datetime | None) -> str | None:
    return ensure_utc(value).isoformat() if value is not None else None


def _manifest_revision_id(manifest: dict[str, Any]) -> str:
    revision_id = manifest.get("revisionId")
    if not isinstance(revision_id, str) or not revision_id:
        raise InternalServerErrorException("workspace manifest revisionId is invalid")
    return revision_id


def _manifest_totals(manifest: dict[str, Any]) -> tuple[int, int]:
    files = manifest.get("files")
    entries = [entry for entry in files if isinstance(entry, dict)] if isinstance(files, list) else []
    bytes_count = sum(entry["size"] for entry in entries if isinstance(entry.get("size"), int))
    return len(entries), bytes_count


def _supersedes_latest(
    current: dict[str, Any] | None,
    *,
    session_created_at: str | None,
    revision_id: str,
) -> bool:
    """Decide whether a finished session's snapshot becomes the scope's latest.

    Ordering key is the session's creation time, never the snapshot time: a session
    orphaned by a gateway restart is reaped *after* the session that replaced it, so
    ordering by snapshot time would let its stale workspace win. Ties break on
    revision id so the decision is deterministic.
    """
    if current is None:
        return True
    current_created_at = current.get("sessionCreatedAt")
    if not isinstance(current_created_at, str):
        # v1 pointer, written before ordering existed: it carries no position,
        # so any current snapshot supersedes it.
        return True
    if session_created_at is None:
        return False
    if session_created_at != current_created_at:
        return _parse_timestamp(session_created_at) > _parse_timestamp(current_created_at)
    return revision_id > str(current.get("revisionId") or "")


def _parse_timestamp(value: str) -> datetime:
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError as exc:
        raise InternalServerErrorException("workspace manifest sessionCreatedAt is invalid") from exc


def _workspace_host_path(rootfs_path: str) -> Path:
    rootfs = Path(rootfs_path).resolve(strict=False)
    return rootfs / WORKSPACE_ROOT.removeprefix("/")


def _replace_workspace_root(workspace_root: Path) -> None:
    if workspace_root.is_symlink() or workspace_root.is_file():
        workspace_root.unlink()
    elif workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)


def _scan_workspace(rootfs_path: str) -> list[ScannedWorkspaceFile]:
    workspace_root = _workspace_host_path(rootfs_path)
    if workspace_root.is_symlink() or not workspace_root.exists():
        return []
    if not workspace_root.is_dir():
        return []

    scanned: list[ScannedWorkspaceFile] = []
    stack: list[tuple[Path, str]] = [(workspace_root, "")]
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), relative))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat_result = entry.stat(follow_symlinks=False)
                absolute_path = Path(entry.path)
                content = absolute_path.read_bytes()
                scanned.append(
                    ScannedWorkspaceFile(
                        relative_path=relative,
                        absolute_path=absolute_path,
                        size=stat_result.st_size,
                        mode=stat.S_IMODE(stat_result.st_mode),
                        mtime_ns=stat_result.st_mtime_ns,
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                )
    return sorted(scanned, key=lambda item: item.relative_path)


@dataclass(slots=True, frozen=True)
class _ManifestEntry:
    path: str
    sha256: str
    size: int
    object_key: str
    mode: int
    mtime_ns: int


def _normalize_manifest_entry(raw: dict[str, Any]) -> _ManifestEntry:
    path_value = raw.get("path")
    if not isinstance(path_value, str):
        raise InternalServerErrorException("workspace manifest path is invalid")
    path = PurePosixPath(path_value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts or not path.parts:
        raise InternalServerErrorException("workspace manifest path escapes workspace")
    object_key = raw.get("objectKey")
    sha256 = raw.get("sha256")
    size = raw.get("size")
    mode = raw.get("mode")
    mtime_ns = raw.get("mtimeNs")
    if not isinstance(object_key, str) or not isinstance(sha256, str):
        raise InternalServerErrorException("workspace manifest object metadata is invalid")
    if not isinstance(size, int) or not isinstance(mode, int) or not isinstance(mtime_ns, int):
        raise InternalServerErrorException("workspace manifest file metadata is invalid")
    return _ManifestEntry(
        path=path.as_posix(),
        sha256=sha256,
        size=size,
        object_key=object_key,
        mode=mode,
        mtime_ns=mtime_ns,
    )


def _ensure_inside_workspace(workspace_root: Path, target: Path) -> None:
    root = workspace_root.resolve(strict=False)
    candidate = target.resolve(strict=False)
    if candidate == root or candidate.is_relative_to(root):
        return
    raise InternalServerErrorException("workspace manifest path escapes workspace")
