from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from infra.migrations.runner import alembic_config, assert_schema_at_head, upgrade_head
from infra.runtime_database import create_runtime_engine
from ouroboros.storage.db_url import (
    canonicalize_runtime_db_url,
    canonicalize_runtime_db_urls,
    collect_runtime_db_urls,
    escape_percent_for_alembic_ini,
    postgres_failover_creator,
    primary_runtime_db_url,
    runtime_db_config_fingerprint,
)
from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.parametrize import parametrize_from_dict

#: Past the connect budget with room for a loaded runner.
_SILENT_DATABASE_WAIT_SEC = 20.0


def _settings(**kwargs: object) -> AppSettings:
    return AppSettings.model_validate(kwargs)


def test_escape_percent_for_alembic_ini_doubles_percent_signs() -> None:
    raw = "postgresql+psycopg://user:pa%25ss@localhost:5432/ouroboros"
    escaped = escape_percent_for_alembic_ini(raw)
    assert escaped == "postgresql+psycopg://user:pa%%25ss@localhost:5432/ouroboros"


def test_alembic_config_accepts_percent_in_password() -> None:
    raw = "postgresql+psycopg://user:pa%25ss@localhost:5432/ouroboros"
    cfg = alembic_config(raw)
    assert cfg.get_main_option("sqlalchemy.url") == raw


def test_canonicalize_runtime_db_url_percent_encodes_password() -> None:
    raw = "postgresql+psycopg://user:pa%ss@localhost:5432/ouroboros"
    canonical = canonicalize_runtime_db_url(raw)
    assert canonical == (
        "postgresql+psycopg://user:pa%25ss@localhost:5432/ouroboros"
        "?connect_timeout=5&target_session_attrs=read-write&tcp_user_timeout=15000"
    )


def test_canonicalize_runtime_db_url_preserves_ssl_and_schema_options() -> None:
    raw = (
        "postgresql+psycopg://user:pass@localhost:5432/ouroboros"
        "?sslmode=require&options=-csearch_path%3Dmyschema&connect_timeout=10"
    )
    canonical = canonicalize_runtime_db_url(raw)
    assert "sslmode=require" in canonical
    assert "options=-csearch_path%3Dmyschema" in canonical
    assert "connect_timeout=10" in canonical
    assert "target_session_attrs=read-write" in canonical


@parametrize_from_dict(
    {
        "session target": {"option": "target_session_attrs=any", "default": "target_session_attrs=read-write"},
        "connect budget": {"option": "connect_timeout=30", "default": "connect_timeout=5"},
        "unacknowledged budget": {"option": "tcp_user_timeout=60000", "default": "tcp_user_timeout=15000"},
    }
)
def test_canonicalize_runtime_db_url_respects_an_explicit_option(option: str, default: str) -> None:
    canonical = canonicalize_runtime_db_url(f"postgresql+psycopg://user:pass@localhost:5432/ouroboros?{option}")
    assert option in canonical
    assert default not in canonical


def test_a_silent_database_cannot_hold_a_runtime_connect() -> None:
    with ThreadPoolExecutor(max_workers=1) as attempts, socket.create_server(("127.0.0.1", 0)) as silent:
        url = canonicalize_runtime_db_url(f"postgresql+psycopg://u:p@127.0.0.1:{silent.getsockname()[1]}/db")
        attempt = attempts.submit(create_runtime_engine(url).connect)
        with pytest.raises(OperationalError):
            attempt.result(timeout=_SILENT_DATABASE_WAIT_SEC)


def test_collect_runtime_db_urls_from_numbered_keys() -> None:
    settings = _settings(
        OUROBOROS_RUNTIME_DB_URL_1="postgresql+psycopg://u:p@pg1:5432/db",
        OUROBOROS_RUNTIME_DB_URL_2="postgresql+psycopg://u:p@pg2:5432/db",
    )
    assert collect_runtime_db_urls(settings) == [
        "postgresql+psycopg://u:p@pg1:5432/db",
        "postgresql+psycopg://u:p@pg2:5432/db",
    ]


def test_collect_runtime_db_urls_stops_at_first_gap() -> None:
    settings = _settings(
        OUROBOROS_RUNTIME_DB_URL_1="postgresql+psycopg://u:p@pg1:5432/db",
        OUROBOROS_RUNTIME_DB_URL_3="postgresql+psycopg://u:p@pg3:5432/db",
    )
    assert collect_runtime_db_urls(settings) == ["postgresql+psycopg://u:p@pg1:5432/db"]


def test_collect_runtime_db_urls_falls_back_to_legacy_url() -> None:
    settings = _settings(OUROBOROS_RUNTIME_DB_URL="postgresql+psycopg://u:p@pg:5432/db")
    assert collect_runtime_db_urls(settings) == ["postgresql+psycopg://u:p@pg:5432/db"]


def test_collect_runtime_db_urls_prefers_numbered_over_legacy() -> None:
    settings = _settings(
        OUROBOROS_RUNTIME_DB_URL="postgresql+psycopg://u:p@legacy:5432/db",
        OUROBOROS_RUNTIME_DB_URL_1="postgresql+psycopg://u:p@pg1:5432/db",
    )
    assert collect_runtime_db_urls(settings) == ["postgresql+psycopg://u:p@pg1:5432/db"]


def test_collect_runtime_db_urls_respects_max_limit() -> None:
    settings = _settings(
        OUROBOROS_RUNTIME_DB_URL_MAX=2,
        OUROBOROS_RUNTIME_DB_URL_1="postgresql+psycopg://u:p@pg1:5432/db",
        OUROBOROS_RUNTIME_DB_URL_2="postgresql+psycopg://u:p@pg2:5432/db",
        OUROBOROS_RUNTIME_DB_URL_3="postgresql+psycopg://u:p@pg3:5432/db",
    )
    assert collect_runtime_db_urls(settings) == [
        "postgresql+psycopg://u:p@pg1:5432/db",
        "postgresql+psycopg://u:p@pg2:5432/db",
    ]


def test_runtime_db_config_fingerprint_joins_urls() -> None:
    urls = canonicalize_runtime_db_urls(
        [
            "postgresql+psycopg://u:p@pg1:5432/db",
            "postgresql+psycopg://u:p@pg2:5432/db",
        ]
    )
    assert runtime_db_config_fingerprint(urls) == f"{urls[0]}|{urls[1]}"


def test_primary_runtime_db_url_returns_first_url_for_alembic() -> None:
    urls = canonicalize_runtime_db_urls(
        [
            "postgresql+psycopg://u:p@pg1:5432/db",
            "postgresql+psycopg://u:p@pg2:5432/db",
        ]
    )
    primary = primary_runtime_db_url(urls)
    assert primary == urls[0]
    assert "|" not in primary
    cfg = alembic_config(escape_percent_for_alembic_ini(primary))
    stored = cfg.get_main_option("sqlalchemy.url")
    assert stored == primary
    assert "pg1" in stored
    assert "pg2" not in stored


def test_canonicalize_runtime_db_urls_canonicalizes_each_url() -> None:
    resolved = canonicalize_runtime_db_urls(["postgresql+psycopg://u:p@pg1:5432/db"])
    assert resolved == [
        "postgresql+psycopg://u:p@pg1:5432/db?connect_timeout=5&target_session_attrs=read-write&tcp_user_timeout=15000",
    ]


def test_postgres_failover_creator_uses_second_url_after_first_fails() -> None:
    urls = canonicalize_runtime_db_urls(
        [
            "postgresql+psycopg://u:p@bad:5432/db",
            "postgresql+psycopg://u:p@good:5432/db",
        ]
    )
    creator = postgres_failover_creator(urls)
    sentinel = object()

    with patch("ouroboros.storage.db_url.psycopg.connect", side_effect=[OSError("down"), sentinel]) as connect:
        assert creator() is sentinel
    assert connect.call_count == 2


def test_alembic_upgrade_head_survives_a_percent_in_the_url() -> None:
    import uuid

    from tests.helpers.schema_harness import scoped_url, server_url

    schema = f"pct%{uuid.uuid4().hex[:8]}"
    admin = create_engine(server_url(), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(scoped_url(schema))
        try:
            upgrade_head(engine)
            assert_schema_at_head(engine)
        finally:
            engine.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
