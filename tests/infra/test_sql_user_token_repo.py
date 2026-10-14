from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from infra.async_engine import create_async_runtime_engine
from infra.migrations.runner import upgrade_head
from infra.user_tokens.interface import UserTokenTarget, UserTokenTargetKind
from infra.user_tokens.sql import SqlUserTokenRepo, SqlUserTokenStore
from tests.helpers.parametrize import parametrize_from_dict

SECRET = "personal-s3cr3t-token"
KEY = SecretStr(base64.b64encode(b"k" * 64).decode("ascii"))
SERVER = "hr"
OWNER = "alice-owner-id"
AGENT = "main"
MCP = UserTokenTarget(UserTokenTargetKind.MCP_SERVER, SERVER)
A2A = UserTokenTarget(UserTokenTargetKind.A2A_AGENT, SERVER)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _in(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _mcp(server_id: str) -> UserTokenTarget:
    return UserTokenTarget(UserTokenTargetKind.MCP_SERVER, server_id)


def _a2a(remote_agent_id: str) -> UserTokenTarget:
    return UserTokenTarget(UserTokenTargetKind.A2A_AGENT, remote_agent_id)


@pytest.fixture
def db_url(blank_schema) -> str:
    engine = blank_schema()
    upgrade_head(engine)
    return str(engine.url.render_as_string(hide_password=False))


@pytest.fixture
def async_engine(db_url):
    engine = create_async_runtime_engine(db_url)
    yield engine
    _run(engine.dispose())


@pytest.fixture
def repo(async_engine) -> SqlUserTokenRepo:
    return SqlUserTokenRepo(async_engine, encryption_key=KEY)


@pytest.fixture
def store(async_engine) -> SqlUserTokenStore:
    return SqlUserTokenStore(async_engine, encryption_key=KEY)


def _rows(db_url: str, table: str, id_column: str) -> list[dict]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT id, {id_column}, owner_id, agent_id, token, expires_at FROM {table}"))
            return [dict(row) for row in result.mappings().all()]
    finally:
        engine.dispose()


def test_a_saved_token_loads_through_the_store(repo, store) -> None:
    _run(repo.save(MCP, OWNER, AGENT, SECRET, _in(600)))

    assert _run(store.load(MCP, OWNER, AGENT)) == SECRET


def test_a_second_save_replaces_the_token_and_its_expiry_in_one_row(repo, store, db_url) -> None:
    _run(repo.save(MCP, OWNER, AGENT, SECRET, _in(600)))
    later = _in(7200)

    _run(repo.save(MCP, OWNER, AGENT, "rotated-token", later))

    rows = _rows(db_url, "mcp_user_tokens", "mcp_server_id")
    assert len(rows) == 1
    assert rows[0]["expires_at"] == SqlUserTokenStore._timestamp(later)
    assert _run(store.load(MCP, OWNER, AGENT)) == "rotated-token"


def test_an_a2a_save_replaces_the_token_of_its_target_owner_and_agent(repo, store, db_url) -> None:
    _run(repo.save(A2A, OWNER, AGENT, SECRET, _in(600)))
    later = _in(7200)

    _run(repo.save(A2A, OWNER, AGENT, "rotated-token", later))

    rows = _rows(db_url, "a2a_user_tokens", "a2a_agent_id")
    assert [(row["a2a_agent_id"], row["owner_id"], row["agent_id"]) for row in rows] == [(SERVER, OWNER, AGENT)]
    assert rows[0]["expires_at"] == SqlUserTokenStore._timestamp(later)
    assert _run(store.load(A2A, OWNER, AGENT)) == "rotated-token"
    assert _rows(db_url, "mcp_user_tokens", "mcp_server_id") == []


def test_a_saved_row_holds_no_clear_token(repo, db_url) -> None:
    _run(repo.save(MCP, OWNER, AGENT, SECRET, _in(600)))

    rows = _rows(db_url, "mcp_user_tokens", "mcp_server_id")

    assert [(row["mcp_server_id"], row["owner_id"], row["agent_id"]) for row in rows] == [(SERVER, OWNER, AGENT)]
    assert SECRET not in " ".join(str(column) for column in rows[0].values())


def test_a_clear_reports_whether_a_row_was_removed(repo, store) -> None:
    _run(repo.save(MCP, OWNER, AGENT, SECRET, _in(600)))

    assert _run(repo.clear(MCP, OWNER, AGENT)) is True
    assert _run(repo.clear(MCP, OWNER, AGENT)) is False
    assert _run(store.load(MCP, OWNER, AGENT)) is None


def test_clearing_an_a2a_token_leaves_an_mcp_token_with_the_same_id(repo, store) -> None:
    _run(repo.save(MCP, OWNER, AGENT, "mcp-token-x", _in(600)))
    _run(repo.save(A2A, OWNER, AGENT, "a2a-token-y", _in(600)))

    assert _run(repo.clear(A2A, OWNER, AGENT)) is True

    assert _run(store.load(MCP, OWNER, AGENT)) == "mcp-token-x"
    assert _run(store.load(A2A, OWNER, AGENT)) is None
    assert _run(repo.clear(A2A, OWNER, AGENT)) is False


@parametrize_from_dict(
    {
        "another server": {"cleared": (_mcp("finance"), OWNER, AGENT)},
        "another owner": {"cleared": (MCP, "bob-owner-id", AGENT)},
        "another agent": {"cleared": (MCP, OWNER, "second")},
        "an a2a agent with the same id": {"cleared": (A2A, OWNER, AGENT)},
    }
)
def test_a_clear_of_another_server_owner_or_agent_removes_nothing(repo, store, cleared) -> None:
    _run(repo.save(MCP, OWNER, AGENT, SECRET, _in(600)))

    assert _run(repo.clear(*cleared)) is False
    assert _run(store.load(MCP, OWNER, AGENT)) == SECRET


def test_expirations_name_the_unexpired_servers_of_one_owner_and_agent(repo) -> None:
    hr_expiry = _in(600)
    finance_expiry = _in(3600)
    _run(repo.save(MCP, OWNER, AGENT, SECRET, hr_expiry))
    _run(repo.save(_mcp("finance"), OWNER, AGENT, SECRET, finance_expiry))
    _run(repo.save(_mcp("calendar"), OWNER, AGENT, SECRET, datetime.now(UTC) - timedelta(seconds=1)))
    _run(repo.save(_mcp("mail"), "bob-owner-id", AGENT, SECRET, _in(600)))
    _run(repo.save(_mcp("wiki"), OWNER, "second", SECRET, _in(600)))

    assert _run(repo.expirations(UserTokenTargetKind.MCP_SERVER, OWNER, AGENT)) == {
        SERVER: hr_expiry,
        "finance": finance_expiry,
    }


def test_expirations_are_scoped_to_one_target_kind(repo) -> None:
    hr_expiry = _in(600)
    research_expiry = _in(3600)
    _run(repo.save(MCP, OWNER, AGENT, SECRET, hr_expiry))
    _run(repo.save(_a2a("research"), OWNER, AGENT, SECRET, research_expiry))
    _run(repo.save(_a2a("expired"), OWNER, AGENT, SECRET, datetime.now(UTC) - timedelta(seconds=1)))
    _run(repo.save(_a2a("theirs"), "bob-owner-id", AGENT, SECRET, _in(600)))

    assert _run(repo.expirations(UserTokenTargetKind.MCP_SERVER, OWNER, AGENT)) == {SERVER: hr_expiry}
    assert _run(repo.expirations(UserTokenTargetKind.A2A_AGENT, OWNER, AGENT)) == {"research": research_expiry}


def test_expirations_open_no_ciphertext(async_engine, db_url) -> None:
    _run(SqlUserTokenRepo(async_engine, encryption_key=KEY).save(MCP, OWNER, AGENT, SECRET, _in(600)))
    other_key = SecretStr(base64.b64encode(b"q" * 64).decode("ascii"))
    other = SqlUserTokenRepo(async_engine, encryption_key=other_key)

    assert set(_run(other.expirations(UserTokenTargetKind.MCP_SERVER, OWNER, AGENT))) == {SERVER}


@parametrize_from_dict(
    {
        "from another server": {"source": (_mcp("finance"), OWNER, AGENT)},
        "from another owner": {"source": (MCP, "bob-owner-id", AGENT)},
        "from another agent": {"source": (MCP, OWNER, "second")},
    }
)
def test_a_saved_ciphertext_moved_to_another_row_does_not_open(repo, store, db_url, source) -> None:
    _run(repo.save(*source, "their-token", _in(600)))
    _run(repo.save(MCP, OWNER, AGENT, SECRET, _in(600)))
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE mcp_user_tokens SET token = (SELECT token FROM mcp_user_tokens "
                    "WHERE mcp_server_id = :server AND owner_id = :owner AND agent_id = :agent) "
                    "WHERE mcp_server_id = :target_server AND owner_id = :target_owner AND agent_id = :target_agent"
                ),
                {
                    "server": source[0].target_id,
                    "owner": source[1],
                    "agent": source[2],
                    "target_server": SERVER,
                    "target_owner": OWNER,
                    "target_agent": AGENT,
                },
            )
    finally:
        engine.dispose()

    with pytest.raises(InvalidTag):
        _run(store.load(MCP, OWNER, AGENT))


@parametrize_from_dict(
    {
        "empty key": {"raw": ""},
        "not base64": {"raw": "not-base64-at-all"},
        "short key": {"raw": base64.b64encode(b"k" * 16).decode("ascii")},
        "a chat encryption key": {"raw": base64.b64encode(b"k" * 32).decode("ascii")},
    }
)
def test_a_repo_refuses_a_key_that_is_not_64_base64_bytes(async_engine, raw) -> None:
    with pytest.raises(ValueError, match="OUROBOROS_CREDENTIALS_ENCRYPTION_KEY") as refused:
        SqlUserTokenRepo(async_engine, encryption_key=SecretStr(raw))

    if raw:
        assert raw not in str(refused.value)


def test_a_save_refuses_an_expiry_without_a_timezone(repo, db_url) -> None:
    with pytest.raises(ValueError, match="timezone"):
        _run(repo.save(MCP, OWNER, AGENT, SECRET, datetime.now()))

    assert _rows(db_url, "mcp_user_tokens", "mcp_server_id") == []
