"""One PostgreSQL schema per worker process, migrated once and emptied between tests."""

from __future__ import annotations

import os
import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine, create_engine, make_url

RUNTIME_DB_URL_PREFIX = "OUROBOROS_RUNTIME_DB_URL"
_VERSION_TABLE = "alembic_version"

_ABSENT_DATABASE = (
    "the suites run against PostgreSQL and nothing else: set OUROBOROS_RUNTIME_DB_URL "
    "(or OUROBOROS_RUNTIME_DB_URL_1) to a reachable server before running them. "
    "`docker compose -f docker/docker-compose.test.yml up -d` starts one on localhost:5433."
)

_schema = ""
_base_url = ""
_engine: Engine | None = None
_tables: tuple[str, ...] = ()
_sequences: tuple[str, ...] = ()


def server_url() -> str:
    """The configured server, with no schema selected — for a test that needs one of its own."""
    if not _base_url:
        raise RuntimeError(_ABSENT_DATABASE)
    return _base_url


def scoped_url(schema: str) -> str:
    return (
        make_url(server_url())
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )


def _configured_url() -> str:
    for key in (RUNTIME_DB_URL_PREFIX, f"{RUNTIME_DB_URL_PREFIX}_1"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            if not value.startswith("postgresql"):
                raise RuntimeError(f"{key} is not a PostgreSQL URL. {_ABSENT_DATABASE}")
            return value
    raise RuntimeError(_ABSENT_DATABASE)


def _worker_suffix() -> str:
    raw = str(os.environ.get("PYTEST_XDIST_WORKER") or "main").lower()
    return re.sub(r"[^a-z0-9]", "", raw) or "main"


def worker_schema() -> str:
    return _schema


def install_worker_schema(suite: str) -> None:
    global _schema, _base_url, _engine, _tables, _sequences
    if _engine is not None:
        return

    from infra.migrations.runner import upgrade_head

    base = make_url(_configured_url())
    _base_url = base.render_as_string(hide_password=False)
    _schema = f"t_{suite}_{_worker_suffix()}"

    admin = create_engine(base.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{_schema}" CASCADE'))
            conn.execute(text(f'CREATE SCHEMA "{_schema}"'))
    finally:
        admin.dispose()

    scoped = scoped_url(_schema)
    for key in [name for name in os.environ if name.startswith(RUNTIME_DB_URL_PREFIX)]:
        del os.environ[key]
    os.environ[f"{RUNTIME_DB_URL_PREFIX}_1"] = scoped

    engine = create_engine(scoped, pool_size=1, max_overflow=0, pool_pre_ping=True)
    upgrade_head(engine)
    inspector = inspect(engine)
    _tables = tuple(
        name
        for name, _ in reversed(inspector.get_sorted_table_and_fkc_names(schema=_schema))
        if name and name != _VERSION_TABLE
    )
    _sequences = tuple(sorted(inspector.get_sequence_names(schema=_schema)))
    _engine = engine


def reset_worker_schema() -> None:
    """Empty every table the migrations created, keeping the version stamp.

    ``DELETE`` rather than ``TRUNCATE``: on tables that are already empty it is an order
    of magnitude cheaper, takes no heavy lock and does not fsync. The version row stays,
    because ``assert_schema_at_head`` refuses a schema whose stamp reads zero.
    """
    if _engine is None:
        return
    with _engine.begin() as conn:
        for table in _tables:
            conn.execute(text(f'DELETE FROM "{_schema}"."{table}"'))
        for sequence in _sequences:
            conn.execute(text(f'ALTER SEQUENCE "{_schema}"."{sequence}" RESTART'))


def dispose_worker_schema() -> None:
    global _engine
    engine, _engine = _engine, None
    if engine is not None:
        engine.dispose()


__all__ = [
    "RUNTIME_DB_URL_PREFIX",
    "dispose_worker_schema",
    "install_worker_schema",
    "reset_worker_schema",
    "scoped_url",
    "server_url",
    "worker_schema",
]
