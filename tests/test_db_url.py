from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from ouroboros.storage.db_url import (
    canonicalize_runtime_db_url,
    canonicalize_runtime_db_urls,
    collect_runtime_db_urls,
    escape_percent_for_alembic_ini,
    postgres_failover_creator,
    primary_runtime_db_url,
    runtime_db_config_fingerprint,
)
from ouroboros.storage.migrations.runner import alembic_config, upgrade_head
from ouroboros.storage.runtime_factory import resolve_runtime_db_url, resolve_runtime_db_urls, sqlite_url_for_path
from ouroboros.storage.settings.schema import AppSettings


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
    assert canonical == ("postgresql+psycopg://user:pa%25ss@localhost:5432/ouroboros?target_session_attrs=read-write")


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


def test_canonicalize_runtime_db_url_respects_explicit_target_session_attrs() -> None:
    raw = "postgresql+psycopg://user:pass@localhost:5432/ouroboros?target_session_attrs=any"
    canonical = canonicalize_runtime_db_url(raw)
    assert "target_session_attrs=any" in canonical
    assert "target_session_attrs=read-write" not in canonical


def test_resolve_runtime_db_url_canonicalizes_postgres_url(tmp_path: pathlib.Path) -> None:
    raw = "postgresql+psycopg://user:pa%ss@localhost:5432/ouroboros"
    resolved = resolve_runtime_db_url(tmp_path, configured_url=raw)
    assert resolved == ("postgresql+psycopg://user:pa%25ss@localhost:5432/ouroboros?target_session_attrs=read-write")


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


def test_collect_runtime_db_urls_rejects_multiple_sqlite_urls() -> None:
    settings = _settings(
        OUROBOROS_RUNTIME_DB_URL_1="sqlite:///a.db",
        OUROBOROS_RUNTIME_DB_URL_2="sqlite:///b.db",
    )
    with pytest.raises(ValueError, match="SQLite runtime DB supports only a single"):
        collect_runtime_db_urls(settings)


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


def test_resolve_runtime_db_urls_canonicalizes_each_url(tmp_path: pathlib.Path) -> None:
    resolved = resolve_runtime_db_urls(
        tmp_path,
        configured_urls=["postgresql+psycopg://u:p@pg1:5432/db"],
    )
    assert resolved == [
        "postgresql+psycopg://u:p@pg1:5432/db?target_session_attrs=read-write",
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


def test_alembic_upgrade_head_with_percent_password_in_sqlite_path(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "state" / "runtime%25.sqlite3"
    url = sqlite_url_for_path(db_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})

    upgrade_head(engine)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == "0007"
