"""StorageProvider protocol (wiki Solution Architecture)."""

from __future__ import annotations

import pathlib
from typing import Any, BinaryIO, Protocol


class ObjectTooLargeError(ValueError):
    """Raised after a bounded storage read exceeds its actual-byte limit."""


class StorageProvider(Protocol):
    """Durable object storage for non-DB agent data."""

    def exists(self, scope: str, key: str) -> bool: ...

    def put_text(self, scope: str, key: str, content: str, *, content_type: str = "text/plain") -> None: ...

    def get_text(self, scope: str, key: str) -> str | None: ...

    def put_bytes(
        self,
        scope: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None: ...

    def put_fileobj(
        self,
        scope: str,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
    ) -> None: ...

    def get_bytes(self, scope: str, key: str) -> bytes | None: ...

    def copy_to_path(
        self,
        scope: str,
        key: str,
        destination: pathlib.Path,
        *,
        max_bytes: int,
    ) -> int: ...

    def put_json(self, scope: str, key: str, value: dict[str, Any]) -> None: ...

    def get_json(self, scope: str, key: str) -> dict[str, Any] | None: ...

    def create_json_if_absent(
        self,
        scope: str,
        key: str,
        value: dict[str, Any],
    ) -> bool: ...

    def delete_json_if_matches(
        self,
        scope: str,
        key: str,
        expected: dict[str, Any],
    ) -> bool: ...

    def replace_json_if_matches(
        self,
        scope: str,
        key: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool: ...

    def append_event(self, scope: str, stream: str, event: dict[str, Any]) -> bool: ...

    def list_keys(
        self,
        scope: str,
        prefix: str = "",
        *,
        limit: int | None = None,
        start_after: str = "",
    ) -> list[str]: ...

    def delete(self, scope: str, key: str) -> None: ...

    def atomic_put_json(self, scope: str, key: str, value: dict[str, Any]) -> None: ...


__all__ = ["ObjectTooLargeError", "StorageProvider"]
