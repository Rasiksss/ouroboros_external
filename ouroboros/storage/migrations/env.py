"""Alembic migration environment for the runtime metadata store."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from ouroboros.storage.db_url import (
    canonicalize_runtime_db_urls,
    collect_runtime_db_urls,
    escape_percent_for_alembic_ini,
    primary_runtime_db_url,
)
from ouroboros.storage.runtime_factory import resolve_runtime_db_urls
from ouroboros.storage.sql_runtime_store import create_runtime_engine

config = context.config

# Skip logging reconfiguration for programmatic runs (runner.upgrade_head):
# fileConfig replaces root handlers, hijacking the running app's logging setup.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _configured_db_urls() -> list[str]:
    from ouroboros.storage.settings.store import load_settings

    return collect_runtime_db_urls(load_settings())


_env_db_urls = canonicalize_runtime_db_urls(_configured_db_urls())
if _env_db_urls:
    config.set_main_option(
        "sqlalchemy.url",
        escape_percent_for_alembic_ini(primary_runtime_db_url(_env_db_urls)),
    )

target_metadata = None


def run_migrations_offline() -> None:
    url = primary_runtime_db_url(_env_db_urls) or config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    injected = config.attributes.get("connection")
    if injected is not None:
        context.configure(connection=injected, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    db_urls = resolve_runtime_db_urls("", configured_urls=_configured_db_urls())
    connectable = create_runtime_engine(db_urls, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
