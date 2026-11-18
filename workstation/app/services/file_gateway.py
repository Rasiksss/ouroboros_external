"""File gateway with path safety and per-session quotas."""

from __future__ import annotations

import hashlib
import logging
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.models.session import Session
from app.services.policy_engine import PolicyEngine
from app.utils.disk import calculate_total_bytes
from app.utils.exceptions.common import (
    NotFoundException,
    PolicyDeniedException,
    QuotaExceededException,
    ValidationErrorException,
)
from app.utils.paths import map_virtual_to_host

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class UploadResult:
    path: str
    bytes_written: int
    sha256: str


@dataclass(slots=True, frozen=True)
class DownloadedFile:
    path: str
    host_path: Path
    bytes_count: int
    sha256: str


@dataclass(slots=True, frozen=True)
class FileNode:
    name: str
    path: str
    kind: str
    bytes_count: int | None = None
    sha256: str | None = None
    children: list[FileNode] = field(default_factory=list)


class FileGateway:
    """Secure file operations inside a session rootfs."""

    def __init__(self, policy: PolicyEngine | None = None) -> None:
        self._policy = policy or PolicyEngine()

    async def upload_file(self, session: Session, *, path: str, content: bytes) -> UploadResult:
        op = "FileGateway.upload_file"
        extra_diagnostics = {
            "diagnostics": {
                "phase": "file-gateway-upload-validate",
                "sessionId": session.id,
                "path": path,
                "bytes": len(content),
                "maxFileBytes": settings.MAX_FILE_SIZE,
                "maxTotalBytes": settings.MAX_TOTAL_BYTES,
                "fileBytesUsed": session.file_bytes_used,
            }
        }
        started = time.perf_counter()
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "process",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
                **extra_diagnostics,
            },
        )
        try:
            normalized_path = self._policy.validate_file_path(path)
            extra_diagnostics["diagnostics"].update({"normalizedPath": normalized_path})
            if len(content) > settings.MAX_FILE_SIZE:
                raise QuotaExceededException(
                    "file exceeds max file size quota",
                    details={"path": normalized_path, "maxFileBytes": settings.MAX_FILE_SIZE},
                )

            host_path = self._resolve_host_path(session.rootfs_path, normalized_path, allow_missing_leaf=True)
            if host_path.exists() and host_path.is_dir():
                extra_diagnostics["diagnostics"].update(
                    {
                        "phase": "file-gateway-upload-target-is-dir",
                        "sessionId": session.id,
                        "path": normalized_path,
                        "hostPath": str(host_path),
                    }
                )
                raise ValidationErrorException(
                    "upload target must be a file path",
                    details={"path": normalized_path},
                )

            old_size = host_path.stat().st_size if host_path.exists() and host_path.is_file() else 0
            projected_total = session.file_bytes_used - old_size + len(content)
            if projected_total > settings.MAX_TOTAL_BYTES:
                extra_diagnostics["diagnostics"].update(
                    {
                        "phase": "file-gateway-upload-total-quota-exceeded",
                        "sessionId": session.id,
                        "path": normalized_path,
                        "hostPath": str(host_path),
                        "oldBytes": old_size,
                        "incomingBytes": len(content),
                        "projectedTotalBytes": projected_total,
                        "maxTotalBytes": settings.MAX_TOTAL_BYTES,
                    }
                )
                raise QuotaExceededException(
                    "session total bytes quota exceeded",
                    details={"path": normalized_path, "maxTotalBytes": settings.MAX_TOTAL_BYTES},
                )

            host_path.parent.mkdir(parents=True, exist_ok=True)
            host_path.write_bytes(content)
            session.file_bytes_used = projected_total
            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "file-gateway-upload-written",
                    "sessionId": session.id,
                    "path": normalized_path,
                    "hostPath": str(host_path),
                    "oldBytes": old_size,
                    "incomingBytes": len(content),
                    "projectedTotalBytes": projected_total,
                }
            )
            result = UploadResult(
                path=normalized_path,
                bytes_written=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
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
                    "event.action": "process",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    async def refresh_usage(self, session: Session) -> int:
        op = "FileGateway.refresh_usage"
        started = time.perf_counter()
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "process",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        extra_diagnostics = {}
        try:
            total_bytes = calculate_total_bytes(Path(session.rootfs_path))
            session.file_bytes_used = total_bytes
            extra_diagnostics = {
                "diagnostics": {
                    "phase": "file-gateway-usage-refreshed",
                    "sessionId": session.id,
                    "rootfsPath": session.rootfs_path,
                    "totalBytes": total_bytes,
                }
            }
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
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
                    "event.action": "process",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return total_bytes

    async def resolve_file(self, session: Session, *, path: str) -> DownloadedFile:
        op = "FileGateway.resolve_file"
        started = time.perf_counter()
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "process",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        extra_diagnostics = {}
        try:
            normalized_path = self._policy.validate_file_path(path)
            host_path = self._resolve_host_path(session.rootfs_path, normalized_path, allow_missing_leaf=False)
            if not host_path.exists() or not host_path.is_file():
                extra_diagnostics = {
                    "diagnostics": {
                        "phase": "file-gateway-resolve-missing",
                        "sessionId": session.id,
                        "path": normalized_path,
                        "hostPath": str(host_path),
                    }
                }
                raise NotFoundException("Файл не найден", details={"path": normalized_path})
            result = DownloadedFile(
                path=normalized_path,
                host_path=host_path,
                bytes_count=host_path.stat().st_size,
                sha256=self._hash_file(host_path),
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
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
            extra_diagnostics = {
                "diagnostics": {
                    "phase": "file-gateway-resolve-success",
                    "sessionId": session.id,
                    "path": normalized_path,
                    "hostPath": str(host_path),
                    "bytes": host_path.stat().st_size,
                }
            }
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    async def list_files(
        self,
        session: Session,
        *,
        path: str,
        recursive: bool = True,
        max_depth: int = 5,
        include_sha256: bool = True,
    ) -> FileNode:
        op = "FileGateway.list_files"
        started = time.perf_counter()
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "process",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        extra_diagnostics = {"diagnostics": {}}
        try:
            normalized_path = self._policy.validate_file_path(path)
            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "file-gateway-list-start",
                    "sessionId": session.id,
                    "path": path,
                    "normalizedPath": normalized_path,
                    "recursive": recursive,
                    "maxDepth": max_depth,
                    "includeSha256": include_sha256,
                }
            )
            if max_depth < 0:
                raise ValidationErrorException("maxDepth must be non-negative", details={"maxDepth": max_depth})

            host_path = self._resolve_host_path(session.rootfs_path, normalized_path, allow_missing_leaf=False)
            if not host_path.exists():
                extra_diagnostics["diagnostics"].update(
                    {
                        "phase": "file-gateway-list-missing",
                        "sessionId": session.id,
                        "path": normalized_path,
                        "hostPath": str(host_path),
                    }
                )
                raise NotFoundException("Путь не найден", details={"path": normalized_path})

            result = self._build_file_node(
                host_path=host_path,
                virtual_path=normalized_path,
                recursive=recursive,
                max_depth=max_depth,
                include_sha256=include_sha256,
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
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
            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "file-gateway-list-finish",
                    "sessionId": session.id,
                    "path": normalized_path,
                    "hostPath": str(host_path),
                    "kind": result.kind,
                    "childCount": len(result.children),
                }
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    def _build_file_node(
        self,
        *,
        host_path: Path,
        virtual_path: str,
        recursive: bool,
        max_depth: int,
        include_sha256: bool,
    ) -> FileNode:
        if host_path.is_symlink():
            return FileNode(
                name=host_path.name,
                path=virtual_path,
                kind="file",
            )

        if host_path.is_dir():
            children: list[FileNode] = []
            if recursive and max_depth > 0:
                for entry in sorted(host_path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                    child_path = self._child_virtual_path(virtual_path, entry.name)
                    children.append(
                        self._build_file_node(
                            host_path=entry,
                            virtual_path=child_path,
                            recursive=recursive,
                            max_depth=max_depth - 1,
                            include_sha256=include_sha256,
                        )
                    )
            elif not recursive:
                for entry in sorted(host_path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                    child_path = self._child_virtual_path(virtual_path, entry.name)
                    children.append(self._build_shallow_node(entry, child_path, include_sha256=include_sha256))

            return FileNode(
                name=host_path.name or "/",
                path=virtual_path,
                kind="dir",
                children=children,
            )

        return FileNode(
            name=host_path.name,
            path=virtual_path,
            kind="file",
            bytes_count=host_path.stat().st_size,
            sha256=self._hash_file(host_path) if include_sha256 else None,
        )

    def _build_shallow_node(self, host_path: Path, virtual_path: str, *, include_sha256: bool) -> FileNode:
        if host_path.is_symlink():
            return FileNode(
                name=host_path.name,
                path=virtual_path,
                kind="file",
            )

        if host_path.is_dir():
            return FileNode(
                name=host_path.name,
                path=virtual_path,
                kind="dir",
                children=[],
            )

        return FileNode(
            name=host_path.name,
            path=virtual_path,
            kind="file",
            bytes_count=host_path.stat().st_size,
            sha256=self._hash_file(host_path) if include_sha256 else None,
        )

    def _resolve_host_path(self, rootfs_path: str, virtual_path: str, *, allow_missing_leaf: bool) -> Path:
        rootfs = Path(rootfs_path).resolve(strict=False)
        candidate = map_virtual_to_host(rootfs_path, virtual_path)
        resolved = candidate.resolve(strict=False)

        if allow_missing_leaf and not candidate.exists():
            parent = candidate.parent.resolve(strict=False)
            if not parent.is_relative_to(rootfs):
                raise PolicyDeniedException(
                    "path escapes session rootfs",
                    details={"path": virtual_path},
                )
            return candidate

        if not resolved.is_relative_to(rootfs):
            raise PolicyDeniedException(
                "path escapes session rootfs",
                details={"path": virtual_path},
            )
        return candidate

    def _child_virtual_path(self, parent_path: str, name: str) -> str:
        if parent_path == "/":
            return f"/{name}"
        return f"{parent_path.rstrip('/')}/{name}"

    def _hash_file(self, host_path: Path) -> str:
        digest = hashlib.sha256()
        with host_path.open("rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
