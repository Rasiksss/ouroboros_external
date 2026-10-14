from __future__ import annotations

import contextlib
import json
import pathlib
import threading
from collections.abc import Iterator
from typing import Any, BinaryIO

from new_core.identity import OwnerScope
from ouroboros.storage.blob.factory import (
    install_storage_provider_builder_for_tests,
    reset_storage_provider_for_tests,
)
from ouroboros.storage.blob.keys import normalize_storage_key, normalized_scope, owner_scope_prefix
from ouroboros.storage.blob.protocol import ObjectTooLargeError

_OBJECTS: dict[tuple[str, str], bytes] = {}
_LOCK = threading.Lock()
_INSTALLED = False


class InMemoryStorageProvider:
    def _address(self, owner_scope: OwnerScope, scope: str, key: str) -> tuple[str, str]:
        scope_norm = normalized_scope(scope)
        rel = normalize_storage_key(key)
        return scope_norm, f"{owner_scope_prefix(owner_scope, scope_norm)}{rel}"

    def exists(self, owner_scope: OwnerScope, scope: str, key: str) -> bool:
        return self._address(owner_scope, scope, key) in _OBJECTS

    def size(self, owner_scope: OwnerScope, scope: str, key: str) -> int:
        data = _OBJECTS.get(self._address(owner_scope, scope, key))
        if data is None:
            raise FileNotFoundError(key)
        return len(data)

    def put_text(
        self, owner_scope: OwnerScope, scope: str, key: str, content: str, *, content_type: str = "text/plain"
    ) -> None:
        del content_type
        _OBJECTS[self._address(owner_scope, scope, key)] = str(content).encode("utf-8")

    def get_text(self, owner_scope: OwnerScope, scope: str, key: str) -> str | None:
        data = _OBJECTS.get(self._address(owner_scope, scope, key))
        return None if data is None else data.decode("utf-8")

    def put_bytes(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        del content_type
        _OBJECTS[self._address(owner_scope, scope, key)] = bytes(data)

    def put_fileobj(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        del content_type
        chunks = list(iter(lambda: fileobj.read(64 * 1024), b""))
        _OBJECTS[self._address(owner_scope, scope, key)] = b"".join(chunks)

    def get_bytes(self, owner_scope: OwnerScope, scope: str, key: str) -> bytes | None:
        data = _OBJECTS.get(self._address(owner_scope, scope, key))
        return None if data is None else bytes(data)

    def copy_to_path(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        destination: pathlib.Path,
        *,
        max_bytes: int,
    ) -> int:
        data = _OBJECTS.get(self._address(owner_scope, scope, key))
        if data is None:
            raise FileNotFoundError(key)
        target = pathlib.Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if len(data) > max_bytes:
            target.unlink(missing_ok=True)
            raise ObjectTooLargeError(f"object exceeds {max_bytes} bytes")
        target.write_bytes(data)
        return len(data)

    def put_json(self, owner_scope: OwnerScope, scope: str, key: str, value: dict[str, Any]) -> None:
        self.atomic_put_json(owner_scope, scope, key, value)

    def get_json(self, owner_scope: OwnerScope, scope: str, key: str) -> dict[str, Any] | None:
        return _decode(_OBJECTS.get(self._address(owner_scope, scope, key)))

    def create_json_if_absent(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        value: dict[str, Any],
    ) -> bool:
        address = self._address(owner_scope, scope, key)
        with _LOCK:
            if address in _OBJECTS:
                return False
            _OBJECTS[address] = _encode(value)
            return True

    def delete_json_if_matches(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        expected: dict[str, Any],
    ) -> bool:
        address = self._address(owner_scope, scope, key)
        with _LOCK:
            if _decode(_OBJECTS.get(address)) != expected:
                return False
            del _OBJECTS[address]
            return True

    def replace_json_if_matches(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool:
        address = self._address(owner_scope, scope, key)
        with _LOCK:
            if _decode(_OBJECTS.get(address)) != expected:
                return False
            _OBJECTS[address] = _encode(replacement)
            return True

    def list_keys(
        self,
        owner_scope: OwnerScope,
        scope: str,
        prefix: str = "",
        *,
        limit: int | None = None,
        start_after: str = "",
    ) -> list[str]:
        if limit is not None and limit <= 0:
            return []
        scope_norm = normalized_scope(scope)
        owner_prefix = owner_scope_prefix(owner_scope, scope_norm)
        norm_prefix = normalize_storage_key(prefix) if prefix else ""
        after = normalize_storage_key(start_after) if start_after else ""
        with _LOCK:
            stored = sorted(
                physical[len(owner_prefix) :]
                for (stored_scope, physical) in _OBJECTS
                if stored_scope == scope_norm and physical.startswith(owner_prefix)
            )
        matched = [key for key in stored if key.startswith(norm_prefix) and key > after]
        return matched[:limit] if limit is not None else matched

    def delete(self, owner_scope: OwnerScope, scope: str, key: str) -> None:
        with _LOCK:
            _OBJECTS.pop(self._address(owner_scope, scope, key), None)

    def retire_file(self, owner_scope: OwnerScope, scope: str, key: str) -> None:
        self.put_bytes(owner_scope, scope, key, b"")

    def atomic_put_json(self, owner_scope: OwnerScope, scope: str, key: str, value: dict[str, Any]) -> None:
        _OBJECTS[self._address(owner_scope, scope, key)] = _encode(value)


class RecordingStorageProvider(InMemoryStorageProvider):
    def __init__(self) -> None:
        self.unbounded_reads: list[str] = []

    def get_bytes(self, owner_scope: OwnerScope, scope: str, key: str) -> bytes | None:
        self.unbounded_reads.append(key)
        return super().get_bytes(owner_scope, scope, key)


@contextlib.contextmanager
def blob_double_disabled() -> Iterator[None]:
    install_storage_provider_builder_for_tests(None)
    reset_storage_provider_for_tests()
    try:
        yield
    finally:
        install_storage_provider_builder_for_tests(InMemoryStorageProvider)
        reset_storage_provider_for_tests()


@contextlib.contextmanager
def recording_blob_double() -> Iterator[RecordingStorageProvider]:
    recorder = RecordingStorageProvider()
    install_storage_provider_builder_for_tests(lambda: recorder)
    reset_storage_provider_for_tests()
    try:
        yield recorder
    finally:
        install_storage_provider_builder_for_tests(InMemoryStorageProvider)
        reset_storage_provider_for_tests()


def _encode(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _decode(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def install_blob_double() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    install_storage_provider_builder_for_tests(InMemoryStorageProvider)
    _INSTALLED = True


def reset_blob_double() -> None:
    with _LOCK:
        _OBJECTS.clear()


__all__ = [
    "InMemoryStorageProvider",
    "RecordingStorageProvider",
    "blob_double_disabled",
    "install_blob_double",
    "recording_blob_double",
    "reset_blob_double",
]
