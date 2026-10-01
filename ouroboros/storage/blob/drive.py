"""DriveStorage facade: DATA_DIR-relative paths via StorageProvider."""

from __future__ import annotations

import json
import logging
import pathlib
import threading
from functools import lru_cache
from typing import Any, Literal

StorageTier = Literal["s3", "file"]

from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.file_backend import FileStorageProvider
from ouroboros.storage.blob.keys import parse_data_rel_path
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.blob.scoped_backend import ScopedHybridProvider, is_local_append_jsonl_scope
from ouroboros.storage.blob.scopes import ALL_SCOPES, SCOPE_LOGS

log = logging.getLogger(__name__)

_STATE_LOCK = threading.Lock()


@lru_cache(maxsize=256)
def _drive_storage_cache_key(drive_root: str, provider_id: int) -> str:
    return f"{drive_root}:{provider_id}"


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

    def is_file_backend(self) -> bool:
        return isinstance(self._provider, FileStorageProvider)

    def _local_file_provider(self) -> FileStorageProvider | None:
        if isinstance(self._provider, FileStorageProvider):
            return self._provider
        if isinstance(self._provider, ScopedHybridProvider):
            return self._provider.local_file
        return None

    def rel_from_path(self, path: pathlib.Path) -> str | None:
        """Return DATA_DIR-relative path when ``path`` is under ``drive_root``."""
        try:
            rel = pathlib.Path(path).resolve().relative_to(self.drive_root)
        except ValueError:
            return None
        return rel.as_posix()

    def _scope_key(self, rel: str) -> tuple[str, str]:
        return parse_data_rel_path(rel)

    def backend_for_rel(self, rel: str) -> StorageTier:
        """Return ``file`` or ``s3`` for the storage tier that owns ``rel``."""
        scope, key = self._scope_key(rel)
        if isinstance(self._provider, FileStorageProvider):
            return "file"
        if isinstance(self._provider, ScopedHybridProvider):
            picked = self._provider._pick(scope, key)
            if isinstance(picked, FileStorageProvider):
                return "file"
            return "s3"
        return "file"

    def _log_op(
        self,
        operation: str,
        rel: str,
        *,
        backend: StorageTier | None = None,
        size: int | None = None,
    ) -> None:
        tier = backend if backend is not None else self.backend_for_rel(rel)
        if size is not None:
            log.info("storage_op operation=%s rel=%s backend=%s size=%d", operation, rel, tier, size)
        else:
            log.info("storage_op operation=%s rel=%s backend=%s", operation, rel, tier)

    def _warn_stale_local_shadow(self, rel: str) -> None:
        """Log when a remote-scope object is missing in S3 but present on local disk."""
        try:
            if self.backend_for_rel(rel) != "s3":
                return
            local = self._local_file_provider()
            if local is None:
                return
            scope, key = self._scope_key(rel)
            shadow = local.local_path(scope, key)
            if shadow.is_file():
                log.warning("stale_local_shadow rel=%s local_path=%s", rel, shadow)
        except ValueError:
            return

    def read_text_rel(self, rel: str) -> str | None:
        scope, key = self._scope_key(rel)
        content = self._provider.get_text(scope, key)
        if content is not None:
            self._log_op("read", rel, size=len(content.encode("utf-8")))
        else:
            self._warn_stale_local_shadow(rel)
            log.debug("storage_op operation=read_miss rel=%s backend=%s", rel, self.backend_for_rel(rel))
        return content

    def write_text_rel(self, rel: str, content: str) -> None:
        scope, key = self._scope_key(rel)
        self._provider.put_text(scope, key, content)
        self._log_op("write", rel, size=len(content.encode("utf-8")))

    def read_bytes_rel(self, rel: str) -> bytes | None:
        scope, key = self._scope_key(rel)
        data = self._provider.get_bytes(scope, key)
        if data is not None:
            self._log_op("read_bytes", rel, size=len(data))
        else:
            self._warn_stale_local_shadow(rel)
            log.debug("storage_op operation=read_bytes_miss rel=%s backend=%s", rel, self.backend_for_rel(rel))
        return data

    def write_bytes_rel(
        self,
        rel: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        scope, key = self._scope_key(rel)
        self._provider.put_bytes(scope, key, data, content_type=content_type)
        self._log_op("write_bytes", rel, size=len(data))

    def read_json_rel(self, rel: str) -> dict[str, Any] | None:
        scope, key = self._scope_key(rel)
        return self._provider.get_json(scope, key)

    def atomic_write_json_rel(self, rel: str, payload: dict[str, Any]) -> None:
        scope, key = self._scope_key(rel)
        self._provider.atomic_put_json(scope, key, payload)

    def exists_rel(self, rel: str) -> bool:
        scope, key = self._scope_key(rel)
        return self._provider.exists(scope, key)

    def path_exists(self, path: pathlib.Path) -> bool:
        """Return True when the object exists on its authoritative storage tier (strict, no shadow fallback)."""
        rel = self.rel_from_path(path)
        if rel is None:
            return pathlib.Path(path).is_file()
        try:
            exists = self.exists_rel(rel)
        except ValueError:
            log.debug("path_exists: unknown scope for %s", rel)
            return False
        if exists:
            return True
        self._warn_stale_local_shadow(rel)
        return False

    def list_entries(self, rel_dir: str) -> list[str]:
        """List immediate children under a DATA_DIR-relative directory (S3-aware)."""
        norm = str(rel_dir or "").strip().replace("\\", "/").strip("/")
        if not norm:
            children: list[str] = []
            for scope in sorted(ALL_SCOPES):
                if self._provider.list_keys(scope, ""):
                    children.append(f"{scope}/")
            return children
        parts = norm.split("/", 1)
        scope = parts[0].lower()
        prefix = f"{parts[1]}/" if len(parts) > 1 else ""
        try:
            keys = self._provider.list_keys(scope, prefix)
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

    def delete_rel(self, rel: str) -> None:
        scope, key = self._scope_key(rel)
        self._provider.delete(scope, key)
        self._log_op("delete", rel)

    def delete_prefix(self, rel_dir: str) -> int:
        """Delete all objects under a DATA_DIR-relative directory prefix. Returns delete count."""
        norm = str(rel_dir or "").strip().replace("\\", "/").strip("/")
        if not norm:
            return 0
        parts = norm.split("/", 1)
        scope = parts[0].lower()
        prefix = f"{parts[1]}/" if len(parts) > 1 else ""
        try:
            keys = self._provider.list_keys(scope, prefix)
        except ValueError:
            return 0
        deleted = 0
        for key in keys:
            self._provider.delete(scope, key)
            deleted += 1
        if deleted:
            log.info("storage_op operation=delete_prefix rel_dir=%s backend=%s count=%d", norm, scope, deleted)
        return deleted

    def read_text(self, path: pathlib.Path) -> str | None:
        rel = self.rel_from_path(path)
        if rel is None:
            return None
        try:
            return self.read_text_rel(rel)
        except ValueError:
            return None

    def read_bytes(self, path: pathlib.Path) -> bytes | None:
        rel = self.rel_from_path(path)
        if rel is None:
            return None
        try:
            return self.read_bytes_rel(rel)
        except ValueError:
            return None

    def write_text(self, path: pathlib.Path, content: str) -> None:
        rel = self.rel_from_path(path)
        if rel is None:
            raise ValueError(f"Path {path} is outside drive_root {self.drive_root}")
        self.write_text_rel(rel, content)

    def write_bytes(
        self,
        path: pathlib.Path,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        rel = self.rel_from_path(path)
        if rel is None:
            raise ValueError(f"Path {path} is outside drive_root {self.drive_root}")
        self.write_bytes_rel(rel, data, content_type=content_type)

    def read_json_dict(self, path: pathlib.Path) -> dict[str, Any] | None:
        rel = self.rel_from_path(path)
        if rel is None:
            return None
        try:
            return self.read_json_rel(rel)
        except ValueError:
            return None

    def atomic_write_json(self, path: pathlib.Path, payload: dict[str, Any]) -> None:
        rel = self.rel_from_path(path)
        if rel is None:
            raise ValueError(f"Path {path} is outside drive_root {self.drive_root}")
        self.atomic_write_json_rel(rel, payload)

    def local_path(self, path: pathlib.Path) -> pathlib.Path | None:
        """Return local filesystem path when using FileStorageProvider."""
        rel = self.rel_from_path(path)
        if rel is None:
            return None
        try:
            scope, key = self._scope_key(rel)
        except ValueError:
            return None
        local = self._local_file_provider()
        if local is not None and is_local_append_jsonl_scope(scope, key):
            return local.local_path(scope, key)
        if isinstance(self._provider, ScopedHybridProvider):
            resolved = self._provider.local_path(scope, key)
            if resolved is not None:
                return resolved
        return None

    def append_jsonl(self, path: pathlib.Path, event: dict[str, Any]) -> bool:
        rel = self.rel_from_path(path)
        if rel is None:
            return False
        try:
            scope, key = self._scope_key(rel)
        except ValueError:
            return False
        if not is_local_append_jsonl_scope(scope, key):
            log.warning("append_jsonl unsupported path %s", rel)
            return False
        local = self._local_file_provider()
        if local is not None and str(key).endswith(".jsonl"):
            journal_path = local.local_path(scope, key)
            line = json.dumps(event, ensure_ascii=False) + "\n"
            try:
                journal_path.parent.mkdir(parents=True, exist_ok=True)
                with journal_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                log.debug(
                    "storage_op operation=append_jsonl rel=%s backend=file size=%d",
                    rel,
                    len(line.encode("utf-8")),
                )
                return True
            except OSError:
                log.warning("append_jsonl failed for %s", journal_path, exc_info=True)
                return False
        stream = pathlib.PurePosixPath(key).stem
        return self._provider.append_event(scope, stream, event)

    def read_jsonl_tail(self, path: pathlib.Path, max_entries: int = 100) -> list[dict[str, Any]]:
        rel = self.rel_from_path(path)
        if rel is None:
            return []
        try:
            scope, key = self._scope_key(rel)
        except ValueError:
            return []

        local = self._local_file_provider()
        if local is not None and (scope == SCOPE_LOGS or is_local_append_jsonl_scope(scope, key)):
            jsonl_path = local.local_path(scope, key)
            if not jsonl_path.is_file():
                return []
            return _read_jsonl_file_tail(jsonl_path, max_entries)

        stream = pathlib.PurePosixPath(key).stem
        prefix = f"{stream}/events/"
        keys = [k for k in self._provider.list_keys(scope, prefix) if k.endswith(".json")]
        keys.sort()
        tail_keys = keys[-max_entries:] if max_entries < len(keys) else keys
        entries: list[dict[str, Any]] = []
        for obj_key in tail_keys:
            raw = self._provider.get_text(scope, obj_key)
            if not raw:
                continue
            for line in raw.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries


def _read_jsonl_file_tail(path: pathlib.Path, max_entries: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
    except OSError:
        return []
    tail = lines[-max_entries:] if max_entries < len(lines) else lines
    entries: list[dict[str, Any]] = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def resolve_drive_root(path: pathlib.Path | Any) -> pathlib.Path:
    """Find OUROBOROS_DATA_DIR root from a path under state/memory/logs."""
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
    from ouroboros.storage.blob.factory import create_storage_provider, resolve_storage_backend

    root = resolve_drive_root(drive_root)
    backend = resolve_storage_backend()
    provider = create_storage_provider(data_dir=root, backend=backend)
    return DriveStorage(root, provider)


def state_write_lock() -> threading.Lock:
    return _STATE_LOCK
