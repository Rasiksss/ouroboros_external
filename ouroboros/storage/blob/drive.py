"""DriveStorage facade: DATA_DIR-relative paths via StorageProvider."""

from __future__ import annotations

import logging
import pathlib
import threading
from typing import Any

from new_core.identity import OwnerScope
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.keys import parse_data_rel_path
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.blob.scopes import ALL_SCOPES

log = logging.getLogger(__name__)

_STATE_LOCK = threading.Lock()


class DriveStorage:
    """Maps paths under ``drive_root`` to scoped StorageProvider keys."""

    def __init__(
        self,
        drive_root: pathlib.Path,
        provider: StorageProvider | None = None,
    ) -> None:
        self.drive_root = pathlib.Path(drive_root).resolve()
        self._provider = provider or get_storage_provider()

    @property
    def provider(self) -> StorageProvider:
        return self._provider

    def rel_from_path(self, path: pathlib.Path) -> str | None:
        """Return DATA_DIR-relative path when ``path`` is under ``drive_root``."""
        try:
            rel = pathlib.Path(path).resolve().relative_to(self.drive_root)
        except ValueError:
            return None
        return rel.as_posix()

    def _scope_key(self, rel: str) -> tuple[str, str]:
        return parse_data_rel_path(rel)

    def _log_op(self, operation: str, rel: str, *, size: int | None = None) -> None:
        if size is not None:
            log.info("storage_op operation=%s rel=%s size=%d", operation, rel, size)
        else:
            log.info("storage_op operation=%s rel=%s", operation, rel)

    def read_text_rel(self, owner_scope: OwnerScope, rel: str) -> str | None:
        scope, key = self._scope_key(rel)
        content = self._provider.get_text(owner_scope, scope, key)
        if content is not None:
            self._log_op("read", rel, size=len(content.encode("utf-8")))
        else:
            log.debug("storage_op operation=read_miss rel=%s", rel)
        return content

    def write_text_rel(self, owner_scope: OwnerScope, rel: str, content: str) -> None:
        scope, key = self._scope_key(rel)
        self._provider.put_text(owner_scope, scope, key, content)
        self._log_op("write", rel, size=len(content.encode("utf-8")))

    def read_bytes_rel(self, owner_scope: OwnerScope, rel: str) -> bytes | None:
        scope, key = self._scope_key(rel)
        data = self._provider.get_bytes(owner_scope, scope, key)
        if data is not None:
            self._log_op("read_bytes", rel, size=len(data))
        else:
            log.debug("storage_op operation=read_bytes_miss rel=%s", rel)
        return data

    def write_bytes_rel(
        self,
        owner_scope: OwnerScope,
        rel: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        scope, key = self._scope_key(rel)
        self._provider.put_bytes(owner_scope, scope, key, data, content_type=content_type)
        self._log_op("write_bytes", rel, size=len(data))

    def read_json_rel(self, owner_scope: OwnerScope, rel: str) -> dict[str, Any] | None:
        scope, key = self._scope_key(rel)
        return self._provider.get_json(owner_scope, scope, key)

    def atomic_write_json_rel(self, owner_scope: OwnerScope, rel: str, payload: dict[str, Any]) -> None:
        scope, key = self._scope_key(rel)
        self._provider.atomic_put_json(owner_scope, scope, key, payload)

    def exists_rel(self, owner_scope: OwnerScope, rel: str) -> bool:
        scope, key = self._scope_key(rel)
        return self._provider.exists(owner_scope, scope, key)

    def path_exists(self, owner_scope: OwnerScope, path: pathlib.Path) -> bool:
        """Return True when the object exists in the shared object storage."""
        rel = self.rel_from_path(path)
        if rel is None:
            return pathlib.Path(path).is_file()
        try:
            exists = self.exists_rel(owner_scope, rel)
        except ValueError:
            log.debug("path_exists: unknown scope for %s", rel)
            return False
        return exists

    def list_entries(self, owner_scope: OwnerScope, rel_dir: str) -> list[str]:
        """List immediate children under a DATA_DIR-relative directory (S3-aware)."""
        norm = str(rel_dir or "").strip().replace("\\", "/").strip("/")
        if not norm:
            children: list[str] = []
            for scope in sorted(ALL_SCOPES):
                if self._provider.list_keys(owner_scope, scope, ""):
                    children.append(f"{scope}/")
            return children
        parts = norm.split("/", 1)
        scope = parts[0].lower()
        prefix = f"{parts[1]}/" if len(parts) > 1 else ""
        try:
            keys = self._provider.list_keys(owner_scope, scope, prefix)
        except ValueError:
            return []
        entry_names: set[str] = set()
        for key in keys:
            remainder = key[len(prefix) :] if prefix and key.startswith(prefix) else key
            if not remainder:
                continue
            segments = remainder.split("/")
            if len(segments) == 1:
                entry_names.add(segments[0])
            else:
                entry_names.add(f"{segments[0]}/")
        return sorted(entry_names)

    def delete_rel(self, owner_scope: OwnerScope, rel: str) -> None:
        scope, key = self._scope_key(rel)
        self._provider.delete(owner_scope, scope, key)
        self._log_op("delete", rel)

    def delete_prefix(self, owner_scope: OwnerScope, rel_dir: str) -> int:
        """Delete all objects under a DATA_DIR-relative directory prefix. Returns delete count."""
        norm = str(rel_dir or "").strip().replace("\\", "/").strip("/")
        if not norm:
            return 0
        parts = norm.split("/", 1)
        scope = parts[0].lower()
        prefix = f"{parts[1]}/" if len(parts) > 1 else ""
        try:
            keys = self._provider.list_keys(owner_scope, scope, prefix)
        except ValueError:
            return 0
        deleted = 0
        for key in keys:
            self._provider.delete(owner_scope, scope, key)
            deleted += 1
        if deleted:
            log.info("storage_op operation=delete_prefix rel_dir=%s scope=%s count=%d", norm, scope, deleted)
        return deleted

    def read_text(self, owner_scope: OwnerScope, path: pathlib.Path) -> str | None:
        rel = self.rel_from_path(path)
        if rel is None:
            return None
        try:
            return self.read_text_rel(owner_scope, rel)
        except ValueError:
            return None

    def read_bytes(self, owner_scope: OwnerScope, path: pathlib.Path) -> bytes | None:
        rel = self.rel_from_path(path)
        if rel is None:
            return None
        try:
            return self.read_bytes_rel(owner_scope, rel)
        except ValueError:
            return None

    def write_text(self, owner_scope: OwnerScope, path: pathlib.Path, content: str) -> None:
        rel = self.rel_from_path(path)
        if rel is None:
            raise ValueError(f"Path {path} is outside drive_root {self.drive_root}")
        self.write_text_rel(owner_scope, rel, content)

    def write_bytes(
        self,
        owner_scope: OwnerScope,
        path: pathlib.Path,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        rel = self.rel_from_path(path)
        if rel is None:
            raise ValueError(f"Path {path} is outside drive_root {self.drive_root}")
        self.write_bytes_rel(owner_scope, rel, data, content_type=content_type)

    def read_json_dict(self, owner_scope: OwnerScope, path: pathlib.Path) -> dict[str, Any] | None:
        rel = self.rel_from_path(path)
        if rel is None:
            return None
        try:
            return self.read_json_rel(owner_scope, rel)
        except ValueError:
            return None

    def atomic_write_json(self, owner_scope: OwnerScope, path: pathlib.Path, payload: dict[str, Any]) -> None:
        rel = self.rel_from_path(path)
        if rel is None:
            raise ValueError(f"Path {path} is outside drive_root {self.drive_root}")
        self.atomic_write_json_rel(owner_scope, rel, payload)


def resolve_drive_root(path: pathlib.Path | Any) -> pathlib.Path:
    """Find OUROBOROS_DATA_DIR root from a path under a storage scope."""
    current = pathlib.Path(path).resolve()
    if current.name in ALL_SCOPES:
        return current.parent
    for parent in current.parents:
        if parent.name in ALL_SCOPES:
            return parent.parent
    if current.is_dir():
        return current
    if current.parent.name == "state":
        return current.parent.parent
    try:
        from ouroboros.storage.settings.paths import DATA_DIR

        configured = pathlib.Path(DATA_DIR).resolve()
        if current == configured or configured in current.parents:
            return configured
        if current in configured.parents:
            return configured
    except Exception:
        log.debug("resolve_drive_root: DATA_DIR lookup failed", exc_info=True)
    return current


def get_drive_storage(drive_root: pathlib.Path | Any) -> DriveStorage:
    root = resolve_drive_root(drive_root)
    return DriveStorage(root, get_storage_provider())


def state_write_lock() -> threading.Lock:
    return _STATE_LOCK
