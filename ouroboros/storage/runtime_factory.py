"""Factory for the process-local runtime metadata store."""

from __future__ import annotations

import pathlib
from typing import Any

from sqlalchemy.engine import URL

from ouroboros.storage.db_url import canonicalize_runtime_db_url, canonicalize_runtime_db_urls
from ouroboros.storage.runtime_backend import RuntimeStore
from ouroboros.storage.sql_runtime_store import SqlRuntimeStore, create_runtime_engine


def runtime_db_path(drive_root: Any) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / "runtime.sqlite3"


def sqlite_url_for_path(db_path: pathlib.Path) -> str:
    db_path = pathlib.Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return URL.create(drivername="sqlite", database=str(db_path.resolve())).render_as_string(hide_password=False)


def resolve_runtime_db_urls(drive_root: Any, *, configured_urls: list[str] | None = None) -> list[str]:
    urls = canonicalize_runtime_db_urls(list(configured_urls or []))
    if urls:
        return urls
    return [sqlite_url_for_path(runtime_db_path(drive_root))]


def resolve_runtime_db_url(
    drive_root: Any, *, configured_url: str = "", configured_urls: list[str] | None = None
) -> str:
    if configured_urls is not None:
        return resolve_runtime_db_urls(drive_root, configured_urls=configured_urls)[0]
    url = canonicalize_runtime_db_url(str(configured_url or ""))
    if url:
        return url
    return sqlite_url_for_path(runtime_db_path(drive_root))


def create_runtime_store(
    drive_root: Any,
    *,
    configured_url: str = "",
    configured_urls: list[str] | None = None,
) -> RuntimeStore:
    if configured_urls is not None:
        db_urls = resolve_runtime_db_urls(drive_root, configured_urls=configured_urls)
    else:
        single = resolve_runtime_db_url(drive_root, configured_url=configured_url)
        db_urls = [single]
    engine = create_runtime_engine(db_urls)
    return SqlRuntimeStore(engine, db_url=db_urls[0], db_urls=db_urls)
