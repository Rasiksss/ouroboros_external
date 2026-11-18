"""StorageProvider protocol (wiki Solution Architecture)."""

from __future__ import annotations

import pathlib
from typing import Any, BinaryIO, Protocol

from new_core.identity import OwnerScope


class ObjectTooLargeError(ValueError):
    """Raised after a bounded storage read exceeds its actual-byte limit."""


class StorageProvider(Protocol):
    """Durable object storage for non-DB agent data.

    Two scopes sit side by side on every method and never merge: ``owner_scope`` is
    the identity the call is made on behalf of, ``scope`` is the storage scope
    (``uploads``, ``state``, ``memory``, ``a2a_tasks``, ``workstation``). The identity is
    required everywhere, including scopes whose keys ignore it, so a call site
    cannot widen by omission.
    """

    def exists(self, owner_scope: OwnerScope, scope: str, key: str) -> bool: ...

    def size(self, owner_scope: OwnerScope, scope: str, key: str) -> int: ...

    def put_text(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        content: str,
        *,
        content_type: str = "text/plain",
    ) -> None: ...

    def get_text(self, owner_scope: OwnerScope, scope: str, key: str) -> str | None: ...

    def put_bytes(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None: ...

    def put_fileobj(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
    ) -> None: ...

    def get_bytes(self, owner_scope: OwnerScope, scope: str, key: str) -> bytes | None: ...

    def copy_to_path(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        destination: pathlib.Path,
        *,
        max_bytes: int,
    ) -> int: ...

    def put_json(self, owner_scope: OwnerScope, scope: str, key: str, value: dict[str, Any]) -> None: ...

    def get_json(self, owner_scope: OwnerScope, scope: str, key: str) -> dict[str, Any] | None: ...

    def create_json_if_absent(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        value: dict[str, Any],
    ) -> bool: ...

    def delete_json_if_matches(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        expected: dict[str, Any],
    ) -> bool: ...

    def replace_json_if_matches(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool: ...

    def list_keys(
        self,
        owner_scope: OwnerScope,
        scope: str,
        prefix: str = "",
        *,
        limit: int | None = None,
        start_after: str = "",
    ) -> list[str]: ...

    def delete(self, owner_scope: OwnerScope, scope: str, key: str) -> None: ...

    def retire_file(self, owner_scope: OwnerScope, scope: str, key: str) -> None: ...

    def atomic_put_json(self, owner_scope: OwnerScope, scope: str, key: str, value: dict[str, Any]) -> None: ...


__all__ = ["ObjectTooLargeError", "StorageProvider"]
