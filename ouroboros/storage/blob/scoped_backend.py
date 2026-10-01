"""Scope-routed StorageProvider: local logs, remote object storage for other scopes."""

from __future__ import annotations

import pathlib
from typing import Any, BinaryIO

from ouroboros.storage.blob.file_backend import FileStorageProvider
from ouroboros.storage.blob.scopes import SCOPE_LOGS

_MEMORY_JOURNAL_SUFFIX = "_journal.jsonl"


def is_local_append_jsonl_scope(scope: str, key: str) -> bool:
    """Return True when JSONL append must use the local file backend."""
    scope_norm = str(scope or "").strip().lower()
    return scope_norm == SCOPE_LOGS or (scope_norm == "memory" and str(key or "").endswith(_MEMORY_JOURNAL_SUFFIX))


class ScopedHybridProvider:
    """Route ``logs/`` (and memory journals) to disk; other scopes to S3."""

    def __init__(
        self,
        *,
        remote: Any,
        local: FileStorageProvider,
    ) -> None:
        self._remote = remote
        self._local = local

    @property
    def local_file(self) -> FileStorageProvider:
        return self._local

    @property
    def remote(self) -> Any:
        return self._remote

    def _pick(self, scope: str, key: str = "") -> Any:
        scope_norm = str(scope or "").strip().lower()
        if scope_norm == SCOPE_LOGS:
            return self._local
        if is_local_append_jsonl_scope(scope, key):
            return self._local
        return self._remote

    def exists(self, scope: str, key: str) -> bool:
        return self._pick(scope, key).exists(scope, key)

    def put_text(self, scope: str, key: str, content: str, *, content_type: str = "text/plain") -> None:
        self._pick(scope, key).put_text(scope, key, content, content_type=content_type)

    def get_text(self, scope: str, key: str) -> str | None:
        return self._pick(scope, key).get_text(scope, key)

    def put_bytes(
        self,
        scope: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        self._pick(scope, key).put_bytes(scope, key, data, content_type=content_type)

    def put_fileobj(
        self,
        scope: str,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        self._pick(scope, key).put_fileobj(
            scope,
            key,
            fileobj,
            content_type=content_type,
        )

    def get_bytes(self, scope: str, key: str) -> bytes | None:
        return self._pick(scope, key).get_bytes(scope, key)

    def copy_to_path(
        self,
        scope: str,
        key: str,
        destination: pathlib.Path,
        *,
        max_bytes: int,
    ) -> int:
        return self._pick(scope, key).copy_to_path(
            scope,
            key,
            destination,
            max_bytes=max_bytes,
        )

    def put_json(self, scope: str, key: str, value: dict[str, Any]) -> None:
        self._pick(scope, key).put_json(scope, key, value)

    def get_json(self, scope: str, key: str) -> dict[str, Any] | None:
        return self._pick(scope, key).get_json(scope, key)

    def create_json_if_absent(
        self,
        scope: str,
        key: str,
        value: dict[str, Any],
    ) -> bool:
        return self._pick(scope, key).create_json_if_absent(scope, key, value)

    def delete_json_if_matches(
        self,
        scope: str,
        key: str,
        expected: dict[str, Any],
    ) -> bool:
        return self._pick(scope, key).delete_json_if_matches(scope, key, expected)

    def replace_json_if_matches(
        self,
        scope: str,
        key: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool:
        return self._pick(scope, key).replace_json_if_matches(
            scope,
            key,
            expected,
            replacement,
        )

    def append_event(self, scope: str, stream: str, event: dict[str, Any]) -> bool:
        return self._pick(scope, stream).append_event(scope, stream, event)

    def list_keys(
        self,
        scope: str,
        prefix: str = "",
        *,
        limit: int | None = None,
        start_after: str = "",
    ) -> list[str]:
        return self._pick(scope, prefix).list_keys(
            scope,
            prefix,
            limit=limit,
            start_after=start_after,
        )

    def delete(self, scope: str, key: str) -> None:
        self._pick(scope, key).delete(scope, key)

    def atomic_put_json(self, scope: str, key: str, value: dict[str, Any]) -> None:
        backend = self._pick(scope, key)
        if isinstance(backend, FileStorageProvider):
            backend.atomic_put_json(scope, key, value)
        else:
            backend.put_json(scope, key, value)

    def local_path(self, scope: str, key: str) -> Any:
        """Resolved path when scope is stored on the local file backend."""
        if self._pick(scope, key) is self._local and hasattr(self._local, "local_path"):
            return self._local.local_path(scope, key)
        return None
