"""Local filesystem StorageProvider under OUROBOROS_DATA_DIR."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shutil
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, BinaryIO

from ouroboros.platform_layer import file_lock_exclusive, file_unlock
from ouroboros.storage.blob.keys import normalize_storage_key
from ouroboros.storage.blob.protocol import ObjectTooLargeError
from ouroboros.storage.blob.scopes import ALL_SCOPES

log = logging.getLogger(__name__)

_APPEND_LOCKS: dict[str, threading.Lock] = {}
_APPEND_LOCKS_GUARD = threading.Lock()
_INTERNAL_TEMP_FILE_RE = re.compile(r"^\..+\.(?:create|replace|tmp)\.[0-9a-f]{8}$")


def _iter_files(
    directory: pathlib.Path,
    root: pathlib.Path,
    start_after: str,
) -> Iterator[pathlib.Path]:
    try:
        entries = sorted(
            directory.iterdir(),
            key=lambda path: f"{path.name}/" if not path.is_symlink() and path.is_dir() else path.name,
        )
    except FileNotFoundError:
        return
    for path in entries:
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            if relative > start_after and not _INTERNAL_TEMP_FILE_RE.fullmatch(path.name):
                yield path
            continue
        if path.is_symlink() or not path.is_dir():
            continue
        prefix = f"{relative}/"
        if start_after and prefix < start_after and not start_after.startswith(prefix):
            continue
        yield from _iter_files(path, root, start_after)


def _append_lock_for(path: pathlib.Path) -> threading.Lock:
    key = str(path.resolve())
    with _APPEND_LOCKS_GUARD:
        lock = _APPEND_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _APPEND_LOCKS[key] = lock
        return lock


def _write_json_temp(path: pathlib.Path, value: dict[str, Any], marker: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{marker}.{uuid.uuid4().hex[:8]}")
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        try:
            remaining = memoryview(content)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("write made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _fsync_directory(path: pathlib.Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_file_lock(path: pathlib.Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock_for(path), path.open("a+b") as handle:
        file_lock_exclusive(handle.fileno())
        try:
            yield
        finally:
            file_unlock(handle.fileno())


class FileStorageProvider:
    """Maps scope/key to ``{data_dir}/{scope}/{key}``."""

    def __init__(self, data_dir: pathlib.Path) -> None:
        self.data_dir = pathlib.Path(data_dir).resolve()

    def _path(self, scope: str, key: str) -> pathlib.Path:
        scope_norm = str(scope or "").strip().lower()
        if scope_norm not in ALL_SCOPES:
            raise ValueError(f"Unknown storage scope: {scope!r}")
        rel = normalize_storage_key(key)
        return (self.data_dir / scope_norm / rel).resolve()

    def exists(self, scope: str, key: str) -> bool:
        return self._path(scope, key).is_file()

    def put_text(self, scope: str, key: str, content: str, *, content_type: str = "text/plain") -> None:
        del content_type
        path = self._path(scope, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def get_text(self, scope: str, key: str) -> str | None:
        path = self._path(scope, key)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def put_bytes(
        self,
        scope: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        del content_type
        path = self._path(scope, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def put_fileobj(
        self,
        scope: str,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        del content_type
        path = self._path(scope, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex[:8]}")
        try:
            with tmp.open("wb") as destination:
                shutil.copyfileobj(fileobj, destination, length=64 * 1024)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def get_bytes(self, scope: str, key: str) -> bytes | None:
        path = self._path(scope, key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def copy_to_path(
        self,
        scope: str,
        key: str,
        destination: pathlib.Path,
        *,
        max_bytes: int,
    ) -> int:
        source = self._path(scope, key)
        if not source.is_file():
            raise FileNotFoundError(key)
        target = pathlib.Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with source.open("rb") as src, target.open("wb") as dst:
                for chunk in iter(lambda: src.read(64 * 1024), b""):
                    written += len(chunk)
                    if written > max_bytes:
                        raise ObjectTooLargeError(f"object exceeds {max_bytes} bytes")
                    dst.write(chunk)
            return written
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def put_json(self, scope: str, key: str, value: dict[str, Any]) -> None:
        self.atomic_put_json(scope, key, value)

    def get_json(self, scope: str, key: str) -> dict[str, Any] | None:
        raw = self.get_text(scope, key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            log.warning("Failed to parse JSON at %s/%s", scope, key, exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    def create_json_if_absent(
        self,
        scope: str,
        key: str,
        value: dict[str, Any],
    ) -> bool:
        path = self._path(scope, key)
        tmp = _write_json_temp(path, value, "create")
        try:
            with _exclusive_file_lock(self.data_dir / ".storage-json.lock"):
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    return False
                tmp.unlink()
                _fsync_directory(path.parent)
                return True
        finally:
            tmp.unlink(missing_ok=True)

    def delete_json_if_matches(
        self,
        scope: str,
        key: str,
        expected: dict[str, Any],
    ) -> bool:
        path = self._path(scope, key)
        with _exclusive_file_lock(self.data_dir / ".storage-json.lock"):
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, ValueError):
                return False
            if current != expected:
                return False
            path.unlink()
            _fsync_directory(path.parent)
            return True

    def replace_json_if_matches(
        self,
        scope: str,
        key: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool:
        path = self._path(scope, key)
        tmp = _write_json_temp(path, replacement, "replace")
        try:
            with _exclusive_file_lock(self.data_dir / ".storage-json.lock"):
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError, ValueError):
                    return False
                if current != expected:
                    return False
                os.replace(tmp, path)
                _fsync_directory(path.parent)
                return True
        finally:
            tmp.unlink(missing_ok=True)

    def append_event(self, scope: str, stream: str, event: dict[str, Any]) -> bool:
        stream_name = str(stream or "").strip() or "default"
        if scope == "logs" and stream_name.split(".jsonl")[0] in {"events", "tools", "supervisor"}:
            log.warning("append_event unsupported for runtime %s stream; use runtime DB audit_events", stream_name)
            return False
        if scope == "logs" and stream_name.endswith(".jsonl"):
            rel_key = stream_name
        elif scope == "logs":
            rel_key = f"{stream_name}.jsonl"
        else:
            rel_key = f"{stream_name}/events.jsonl"
        path = self._path(scope, rel_key)
        line = json.dumps(event, ensure_ascii=False) + "\n"
        lock = _append_lock_for(path)
        with lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                return True
            except OSError:
                log.warning("append_event failed for %s", path, exc_info=True)
                return False

    def list_keys(
        self,
        scope: str,
        prefix: str = "",
        *,
        limit: int | None = None,
        start_after: str = "",
    ) -> list[str]:
        if limit is not None and limit <= 0:
            return []
        root = self.data_dir / str(scope).strip().lower()
        if not root.is_dir():
            return []
        norm_prefix = normalize_storage_key(prefix) if prefix else ""
        norm_start_after = normalize_storage_key(start_after) if start_after else ""
        search_root = root / norm_prefix if norm_prefix else root
        if not search_root.exists():
            return []
        if search_root.is_file():
            rel = search_root.relative_to(root).as_posix()
            return [rel] if rel > norm_start_after else []
        out: list[str] = []
        for path in _iter_files(search_root, root, norm_start_after):
            out.append(path.relative_to(root).as_posix())
            if limit is not None and len(out) >= limit:
                break
        return out

    def delete(self, scope: str, key: str) -> None:
        path = self._path(scope, key)
        with _exclusive_file_lock(self.data_dir / ".storage-json.lock"):
            try:
                path.unlink()
            except FileNotFoundError:
                return
            _fsync_directory(path.parent)

    def atomic_put_json(self, scope: str, key: str, value: dict[str, Any]) -> None:
        """Atomic JSON write (temp file + replace)."""
        path = self._path(scope, key)
        tmp = _write_json_temp(path, value, "tmp")
        try:
            with _exclusive_file_lock(self.data_dir / ".storage-json.lock"):
                os.replace(tmp, path)
                _fsync_directory(path.parent)
        finally:
            tmp.unlink(missing_ok=True)

    def local_path(self, scope: str, key: str) -> pathlib.Path:
        """Expose resolved path for legacy callers (file backend only)."""
        return self._path(scope, key)
