from __future__ import annotations

import ast
import asyncio
import pathlib
import re

import pytest
from sqlalchemy import create_engine, text

import config_registry.user_settings.infra.repositories.repo as repo_module
from config_registry.user_settings.infra.repositories.repo import (
    SqlAsyncUserSettingsStore,
    SqlUserSettingsRepo,
    SqlUserSettingsStore,
)
from infra.async_engine import AsyncEngineFactory
from ouroboros.storage.runtime_engine import resolve_runtime_engine

OWNER = "alice"
OTHER_OWNER = "bob"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _rows(runtime_db_url: str, sql: str, **params):
    engine = create_engine(runtime_db_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).mappings().all()
    finally:
        engine.dispose()


def _overrides(runtime_db_url: str, owner_id: str, agent_id: str) -> dict[str, str]:
    """The rows of one pair, read outside the repository, since the repository only writes."""
    rows = _rows(
        runtime_db_url,
        "SELECT key, value FROM user_settings WHERE owner_id = :owner_id AND agent_id = :agent_id",
        owner_id=owner_id,
        agent_id=agent_id,
    )
    return {str(row["key"]): str(row["value"]) for row in rows}


def test_every_statement_the_repository_issues_names_the_owner_and_the_agent():
    tree = ast.parse(pathlib.Path(repo_module.__file__).read_text(encoding="utf-8"))
    statements = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.search(r"\b(FROM|INTO)\s+user_settings\b", node.value)
    ]
    assert statements, "the module issues no statements at all"
    assert all(":owner_id" in statement and ":agent_id" in statement for statement in statements), statements


def test_an_override_is_written_and_read_back(repo, runtime_db_url):
    _run(repo.set_override(OWNER, "main", "OUROBOROS_TIMEZONE", "Asia/Tokyo"))
    assert _overrides(runtime_db_url, OWNER, "main") == {"OUROBOROS_TIMEZONE": "Asia/Tokyo"}
    rows = _rows(runtime_db_url, "SELECT agent_id, value FROM user_settings WHERE owner_id = :o", o=OWNER)
    assert [(row["agent_id"], row["value"]) for row in rows] == [("main", "Asia/Tokyo")]


def test_setting_the_same_key_twice_replaces_the_row(repo, runtime_db_url):
    _run(repo.set_override(OWNER, "main", "OUROBOROS_MODEL", "first"))
    _run(repo.set_override(OWNER, "main", "OUROBOROS_MODEL", "second"))
    rows = _rows(runtime_db_url, "SELECT value FROM user_settings WHERE owner_id = :o", o=OWNER)
    assert [row["value"] for row in rows] == ["second"]


def test_two_agents_of_one_owner_are_read_apart(repo, runtime_db_url):
    _run(repo.set_override(OWNER, "main", "OUROBOROS_MODEL", "mine"))
    _run(repo.set_override(OWNER, "sub-1", "OUROBOROS_MODEL", "other"))

    assert _overrides(runtime_db_url, OWNER, "main") == {"OUROBOROS_MODEL": "mine"}
    assert _overrides(runtime_db_url, OWNER, "sub-1") == {"OUROBOROS_MODEL": "other"}


def test_another_owners_rows_are_unreachable_under_the_same_agent_id(repo, runtime_db_url):
    _run(repo.set_override(OWNER, "main", "OUROBOROS_MODEL", "mine"))
    _run(repo.set_override(OTHER_OWNER, "main", "OUROBOROS_MODEL", "theirs"))

    assert _overrides(runtime_db_url, OWNER, "main") == {"OUROBOROS_MODEL": "mine"}
    assert _overrides(runtime_db_url, OTHER_OWNER, "main") == {"OUROBOROS_MODEL": "theirs"}


def test_clearing_reports_whether_a_row_was_there(repo, runtime_db_url):
    _run(repo.set_override(OWNER, "main", "OUROBOROS_TIMEZONE", "Asia/Tokyo"))
    assert _run(repo.clear_override(OWNER, "main", "OUROBOROS_TIMEZONE")) is True
    assert _run(repo.clear_override(OWNER, "main", "OUROBOROS_TIMEZONE")) is False
    assert _overrides(runtime_db_url, OWNER, "main") == {}


def test_clearing_one_agents_override_leaves_the_other_agent_alone(repo, runtime_db_url):
    _run(repo.set_override(OWNER, "main", "OUROBOROS_MODEL", "mine"))
    _run(repo.set_override(OWNER, "sub-1", "OUROBOROS_MODEL", "other"))

    assert _run(repo.clear_override(OWNER, "main", "OUROBOROS_MODEL")) is True

    assert _overrides(runtime_db_url, OWNER, "sub-1") == {"OUROBOROS_MODEL": "other"}


@pytest.fixture
def runtime_repo():
    """A writer on the same database the synchronous reader is composed against."""
    url = str(resolve_runtime_engine("user settings").url.render_as_string(hide_password=False))
    factory = AsyncEngineFactory()
    factory.initialize(url)
    yield SqlUserSettingsRepo(factory.engine)
    asyncio.get_event_loop().run_until_complete(factory.dispose())


def test_the_two_engines_agree_on_what_was_written(runtime_repo):
    """A value written through the async engine is read through the synchronous store."""
    _run(runtime_repo.set_override(OWNER, "main", "OUROBOROS_TIMEZONE", "Asia/Vladivostok"))
    _run(runtime_repo.set_override(OWNER, "main", "OUROBOROS_MODEL", "written-async"))

    store = SqlUserSettingsStore()
    assert store.get_setting_value(OWNER, "main", "OUROBOROS_TIMEZONE") == "Asia/Vladivostok"
    assert store.get_setting_value(OWNER, "main", "OUROBOROS_MODEL") == "written-async"

    assert _run(runtime_repo.clear_override(OWNER, "main", "OUROBOROS_MODEL")) is True
    assert store.get_setting_value(OWNER, "main", "OUROBOROS_MODEL") is None
    assert store.get_setting_value(OWNER, "main", "OUROBOROS_TIMEZONE") == "Asia/Vladivostok"


def test_the_synchronous_read_reaches_one_owner_and_one_agent(runtime_repo):
    _run(runtime_repo.set_override(OWNER, "main", "OUROBOROS_MODEL", "mine"))
    _run(runtime_repo.set_override(OWNER, "main", "OUROBOROS_TIMEZONE", "Asia/Tokyo"))
    _run(runtime_repo.set_override(OWNER, "sub-1", "OUROBOROS_MODEL", "other-agent"))
    _run(runtime_repo.set_override(OTHER_OWNER, "main", "OUROBOROS_MODEL", "theirs"))

    store = SqlUserSettingsStore()

    assert store.get_setting_value(OWNER, "main", "OUROBOROS_MODEL") == "mine"
    assert store.get_setting_value(OWNER, "main", "OUROBOROS_TIMEZONE") == "Asia/Tokyo"
    assert store.get_setting_value(OWNER, "sub-1", "OUROBOROS_MODEL") == "other-agent"
    assert store.get_setting_value(OTHER_OWNER, "main", "OUROBOROS_MODEL") == "theirs"
    assert store.get_setting_value(OWNER, "sub-2", "OUROBOROS_MODEL") is None


def test_the_async_store_reads_one_key_of_one_pair(runtime_repo):
    """The per-key path an async accessor takes: one key, this pair's row and no other's."""
    from infra.async_engine import async_engine_factory

    async_engine_factory.initialize(
        str(resolve_runtime_engine("user settings").url.render_as_string(hide_password=False))
    )
    _run(runtime_repo.set_override(OWNER, "main", "OUROBOROS_MODEL", "mine"))
    _run(runtime_repo.set_override(OWNER, "sub-1", "OUROBOROS_MODEL", "other-agent"))
    _run(runtime_repo.set_override(OTHER_OWNER, "main", "OUROBOROS_MODEL", "theirs"))

    store = SqlAsyncUserSettingsStore()

    assert _run(store.get_setting_value(OWNER, "main", "OUROBOROS_MODEL")) == "mine"
    assert _run(store.get_setting_value(OWNER, "sub-1", "OUROBOROS_MODEL")) == "other-agent"
    assert _run(store.get_setting_value(OTHER_OWNER, "main", "OUROBOROS_MODEL")) == "theirs"
    assert _run(store.get_setting_value(OWNER, "main", "OUROBOROS_TIMEZONE")) is None


def test_the_async_store_reads_the_rows_of_one_pair_in_one_scan(runtime_repo):
    from infra.async_engine import async_engine_factory

    async_engine_factory.initialize(
        str(resolve_runtime_engine("user settings").url.render_as_string(hide_password=False))
    )
    _run(runtime_repo.set_override(OWNER, "main", "OUROBOROS_MODEL", "mine"))
    _run(runtime_repo.set_override(OWNER, "main", "OUROBOROS_TIMEZONE", "Asia/Tokyo"))
    _run(runtime_repo.set_override(OWNER, "sub-1", "OUROBOROS_MODEL", "other-agent"))
    _run(runtime_repo.set_override(OTHER_OWNER, "main", "OUROBOROS_MODEL", "theirs"))

    store = SqlAsyncUserSettingsStore()

    assert _run(store.get_setting_values(OWNER, "main")) == {
        "OUROBOROS_MODEL": "mine",
        "OUROBOROS_TIMEZONE": "Asia/Tokyo",
    }
    assert _run(store.get_setting_values(OWNER, "sub-1")) == {"OUROBOROS_MODEL": "other-agent"}
    assert _run(store.get_setting_values(OTHER_OWNER, "main")) == {"OUROBOROS_MODEL": "theirs"}
    assert _run(store.get_setting_values(OWNER, "sub-2")) == {}
