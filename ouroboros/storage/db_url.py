"""Database URL helpers for SQLAlchemy, Alembic, and psycopg."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import psycopg
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from ouroboros.storage.settings.schema import AppSettings

RUNTIME_DB_URL_KEY = "OUROBOROS_RUNTIME_DB_URL"
RUNTIME_DB_URL_INDEXED_RE = re.compile(r"^OUROBOROS_RUNTIME_DB_URL_(\d+)$")
DEFAULT_RUNTIME_DB_URL_MAX = 16
ABSOLUTE_RUNTIME_DB_URL_MAX = 64


def escape_percent_for_alembic_ini(url: str) -> str:
    """Double ``%`` so Alembic's ConfigParser does not treat URL bytes as interpolation."""
    return url.replace("%", "%%")


def _with_read_write_target(url: str) -> str:
    parsed = make_url(url)
    query = dict(parsed.query)
    if "target_session_attrs" not in query:
        parsed = parsed.update_query_dict({"target_session_attrs": "read-write"})
    return parsed.render_as_string(hide_password=False)


def canonicalize_runtime_db_url(url: str) -> str:
    """Normalize a configured runtime DB URL for SQLAlchemy/psycopg consumption."""
    stripped = str(url or "").strip()
    if not stripped:
        return ""
    if stripped.startswith("sqlite"):
        return stripped
    return _with_read_write_target(stripped)


def canonicalize_runtime_db_urls(urls: list[str]) -> list[str]:
    return [canonicalize_runtime_db_url(url) for url in urls if str(url or "").strip()]


def runtime_db_config_fingerprint(urls: list[str]) -> str:
    return "|".join(urls)


def primary_runtime_db_url(urls: list[str]) -> str:
    """First URL for Alembic offline mode and other single-URL consumers."""
    return urls[0] if urls else ""


def _runtime_db_url_max(settings: AppSettings) -> int:
    raw = getattr(settings, "OUROBOROS_RUNTIME_DB_URL_MAX", DEFAULT_RUNTIME_DB_URL_MAX)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_RUNTIME_DB_URL_MAX
    return max(1, min(value, ABSOLUTE_RUNTIME_DB_URL_MAX))


def _indexed_runtime_db_urls(settings: AppSettings) -> dict[int, str]:
    indexed: dict[int, str] = {}
    extras = settings.__pydantic_extra__ or {}
    for key, value in extras.items():
        match = RUNTIME_DB_URL_INDEXED_RE.match(str(key))
        if not match:
            continue
        text = str(value or "").strip()
        if text:
            indexed[int(match.group(1))] = text
    return indexed


def collect_runtime_db_urls(settings: AppSettings) -> list[str]:
    """Collect configured runtime DB URLs from numbered keys or legacy single URL."""
    max_urls = _runtime_db_url_max(settings)
    indexed = _indexed_runtime_db_urls(settings)
    if indexed:
        urls: list[str] = []
        for index in range(1, max_urls + 1):
            url = indexed.get(index)
            if not url:
                break
            urls.append(url)
        if any(url.startswith("sqlite") for url in urls) and len(urls) > 1:
            raise ValueError("SQLite runtime DB supports only a single connection URL")
        return urls

    legacy = str(settings.OUROBOROS_RUNTIME_DB_URL or "").strip()
    if legacy:
        return [legacy]
    return []


def postgres_failover_creator(urls: list[str]) -> Callable[[], Any]:
    """Return a DBAPI connect callable that fails over across PostgreSQL URLs."""
    if len(urls) < 2:
        raise ValueError("postgres_failover_creator requires at least two URLs")
    dialect = PGDialect_psycopg()
    lock = threading.Lock()
    last_good_index = 0

    def connect() -> Any:
        nonlocal last_good_index
        with lock:
            start = last_good_index
        last_exc: BaseException | None = None
        count = len(urls)
        for offset in range(count):
            index = (start + offset) % count
            url = urls[index]
            try:
                _args, connect_kwargs = dialect.create_connect_args(make_url(url))
                connection = psycopg.connect(**connect_kwargs)
            except Exception as exc:
                last_exc = exc
                continue
            with lock:
                last_good_index = index
            return connection
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no PostgreSQL URLs configured for failover")

    return connect
