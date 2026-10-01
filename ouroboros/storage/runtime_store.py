"""Runtime metadata store facade (SQLite file or PostgreSQL via env URL)."""

from __future__ import annotations

import threading
from typing import Any

from ouroboros.storage.db_url import collect_runtime_db_urls, runtime_db_config_fingerprint
from ouroboros.storage.runtime_backend import RuntimeStore
from ouroboros.storage.runtime_factory import create_runtime_store, resolve_runtime_db_urls, runtime_db_path
from ouroboros.storage.runtime_serialization import dump_json as _dump_json, load_json as _load_json

_STORE_REF: RuntimeStore | None = None
_STORE_REF_LOCK = threading.Lock()
_STORE_REF_FINGERPRINT: str | None = None


def _configured_db_urls() -> list[str]:
    from ouroboros.storage.settings.store import load_settings

    return collect_runtime_db_urls(load_settings())


def init_runtime_store(drive_root: Any) -> RuntimeStore:
    """Initialize and register the process-local runtime store."""
    global _STORE_REF, _STORE_REF_FINGERPRINT
    configured_urls = _configured_db_urls()
    with _STORE_REF_LOCK:
        _STORE_REF = create_runtime_store(drive_root, configured_urls=configured_urls)
        _STORE_REF_FINGERPRINT = runtime_db_config_fingerprint(
            resolve_runtime_db_urls(drive_root, configured_urls=configured_urls)
        )
        return _STORE_REF


def get_runtime_store(drive_root: Any | None = None) -> RuntimeStore | None:
    """Return the registered store, lazily creating it when drive_root is known."""
    global _STORE_REF, _STORE_REF_FINGERPRINT
    if _STORE_REF is not None and drive_root is None:
        return _STORE_REF
    if drive_root is None:
        return None
    configured_urls = _configured_db_urls()
    wanted = runtime_db_config_fingerprint(resolve_runtime_db_urls(drive_root, configured_urls=configured_urls))
    if _STORE_REF is not None and wanted == _STORE_REF_FINGERPRINT:
        return _STORE_REF
    return init_runtime_store(drive_root)


__all__ = [
    "RuntimeStore",
    "_dump_json",
    "_load_json",
    "get_runtime_store",
    "init_runtime_store",
    "runtime_db_path",
]
