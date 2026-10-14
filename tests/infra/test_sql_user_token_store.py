from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from infra.async_engine import create_async_runtime_engine
from infra.migrations.runner import upgrade_head
from infra.user_tokens.interface import UserTokenTarget, UserTokenTargetKind
from infra.user_tokens.sql import SqlUserTokenStore
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import sealed_under_the_chat_key
from tests.helpers.user_tokens import seed_user_token

SECRET = "personal-s3cr3t-token"
KEY = SecretStr(base64.b64encode(b"k" * 64).decode("ascii"))
OTHER_KEY = SecretStr(base64.b64encode(b"q" * 64).decode("ascii"))
SERVER = "hr"
OWNER = "alice-owner-id"
AGENT = "main"
MCP = UserTokenTarget(UserTokenTargetKind.MCP_SERVER, SERVER)
A2A = UserTokenTarget(UserTokenTargetKind.A2A_AGENT, SERVER)
TABLES = {UserTokenTargetKind.MCP_SERVER: "mcp_user_tokens", UserTokenTargetKind.A2A_AGENT: "a2a_user_tokens"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _later() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=600)


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
def store(async_engine) -> SqlUserTokenStore:
    return SqlUserTokenStore(async_engine, encryption_key=KEY)


def _seed(db_url: str, *, key: SecretStr = KEY, **overrides) -> str:
    row = {
        "target": MCP,
        "owner_id": OWNER,
        "agent_id": AGENT,
        "token": SECRET,
        "expires_at": _later(),
    }
    row.update(overrides)
    return seed_user_token(db_url, key, **row)


def _execute(db_url: str, sql: str, **params) -> list[dict]:
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return [dict(row) for row in result.mappings().all()] if result.returns_rows else []
    finally:
        engine.dispose()


def test_a_target_is_one_of_two_kinds() -> None:
    assert {kind.name: kind.value for kind in UserTokenTargetKind} == {
        "MCP_SERVER": "mcp-server",
        "A2A_AGENT": "a2a-agent",
    }
    assert UserTokenTarget(UserTokenTargetKind.MCP_SERVER, "hr") != UserTokenTarget(UserTokenTargetKind.A2A_AGENT, "hr")


def test_the_table_is_keyed_by_id_and_unique_per_server_owner_and_agent(db_url) -> None:
    engine = create_engine(db_url)
    try:
        inspector = inspect(engine)
        primary_key = inspector.get_pk_constraint("mcp_user_tokens")["constrained_columns"]
        unique = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("mcp_user_tokens")
        }
    finally:
        engine.dispose()

    assert primary_key == ["id"]
    assert unique == {"uq_mcp_user_tokens_server_owner_agent": ["mcp_server_id", "owner_id", "agent_id"]}


def test_a_second_row_for_one_server_owner_and_agent_is_refused(db_url) -> None:
    _seed(db_url)

    with pytest.raises(IntegrityError):
        _seed(db_url, token="another-token")


def test_a2a_user_tokens_refuses_a_second_row_for_one_target_owner_and_agent(db_url) -> None:
    _seed(db_url, target=A2A)

    with pytest.raises(IntegrityError, match="a2a_user_tokens"):
        _seed(db_url, target=A2A, token="another-token")


@parametrize_from_dict({"an mcp server": {"target": MCP}, "an a2a agent": {"target": A2A}})
def test_a_stored_token_loads_for_either_kind(store, db_url, target) -> None:
    _seed(db_url, target=target)

    assert _run(store.load(target, OWNER, AGENT)) == SECRET


def test_an_mcp_and_an_a2a_target_with_one_id_hold_separate_tokens(store, db_url) -> None:
    _seed(db_url, target=MCP, token="mcp-token-x")
    _seed(db_url, target=A2A, token="a2a-token-y")

    assert _run(store.load(MCP, OWNER, AGENT)) == "mcp-token-x"
    assert _run(store.load(A2A, OWNER, AGENT)) == "a2a-token-y"


@parametrize_from_dict(
    {
        "another server": {"stored": MCP, "requested": (UserTokenTarget(MCP.kind, "finance"), OWNER, AGENT)},
        "another owner": {"stored": MCP, "requested": (MCP, "bob-owner-id", AGENT)},
        "another agent": {"stored": MCP, "requested": (MCP, OWNER, "second")},
        "another remote agent": {"stored": A2A, "requested": (UserTokenTarget(A2A.kind, "finance"), OWNER, AGENT)},
        "another owner of a remote agent": {"stored": A2A, "requested": (A2A, "bob-owner-id", AGENT)},
        "another agent of a remote agent": {"stored": A2A, "requested": (A2A, OWNER, "second")},
    }
)
def test_a_token_of_another_server_owner_or_agent_loads_nothing(store, db_url, stored, requested) -> None:
    _seed(db_url, target=stored)

    assert _run(store.load(*requested)) is None


def test_an_expired_token_loads_nothing(store, db_url) -> None:
    _seed(db_url, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    assert _run(store.load(MCP, OWNER, AGENT)) is None


def test_a_token_expired_in_another_timezone_loads_nothing(store, db_url) -> None:
    moscow = timezone(timedelta(hours=3))
    _seed(db_url, expires_at=(datetime.now(UTC) - timedelta(minutes=30)).astimezone(moscow))

    assert _run(store.load(MCP, OWNER, AGENT)) is None
    assert _execute(db_url, "SELECT expires_at FROM mcp_user_tokens")[0]["expires_at"].endswith("+00:00")


def test_a_timestamp_without_a_timezone_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        SqlUserTokenStore._timestamp(datetime.now())


def test_a_row_holds_no_clear_token(db_url) -> None:
    _seed(db_url)

    rows = _execute(db_url, "SELECT id, mcp_server_id, agent_id, owner_id, token, expires_at FROM mcp_user_tokens")

    assert [(row["mcp_server_id"], row["owner_id"], row["agent_id"]) for row in rows] == [(SERVER, OWNER, AGENT)]
    assert SECRET not in " ".join(str(column) for column in rows[0].values())


def test_an_a2a_row_holds_no_token_in_the_clear(db_url) -> None:
    _seed(db_url, target=A2A)

    rows = _execute(db_url, "SELECT id, a2a_agent_id, agent_id, owner_id, token, expires_at FROM a2a_user_tokens")

    assert [(row["a2a_agent_id"], row["owner_id"], row["agent_id"]) for row in rows] == [(SERVER, OWNER, AGENT)]
    assert SECRET not in " ".join(str(column) for column in rows[0].values())
    assert _execute(db_url, "SELECT id FROM mcp_user_tokens") == []


@parametrize_from_dict(
    {
        "from another server": {"source": {"target": UserTokenTarget(MCP.kind, "finance")}},
        "from another owner": {"source": {"owner_id": "bob-owner-id"}},
        "from another agent": {"source": {"agent_id": "second"}},
    }
)
def test_a_moved_ciphertext_does_not_open(store, db_url, source) -> None:
    source_id = _seed(db_url, token="their-token", **source)
    target_id = _seed(db_url)
    _execute(
        db_url,
        "UPDATE mcp_user_tokens SET token = (SELECT token FROM mcp_user_tokens WHERE id = :source_id) "
        "WHERE id = :target_id",
        source_id=source_id,
        target_id=target_id,
    )

    with pytest.raises(InvalidTag):
        _run(store.load(MCP, OWNER, AGENT))


@parametrize_from_dict(
    {
        "from an mcp server to an a2a agent": {"source": MCP, "destination": A2A},
        "from an a2a agent to an mcp server": {"source": A2A, "destination": MCP},
    }
)
def test_a_ciphertext_moved_between_target_kinds_does_not_open(store, db_url, source, destination) -> None:
    source_id = _seed(db_url, target=source, token="their-token")
    destination_id = _seed(db_url, target=destination)
    _execute(
        db_url,
        f"UPDATE {TABLES[destination.kind]} SET token = (SELECT token FROM {TABLES[source.kind]} WHERE id = :source_id) "
        "WHERE id = :destination_id",
        source_id=source_id,
        destination_id=destination_id,
    )

    with pytest.raises(InvalidTag):
        _run(store.load(destination, OWNER, AGENT))


def test_an_mcp_token_sealed_under_a_chat_encryption_key_does_not_open(store, db_url) -> None:
    _execute(
        db_url,
        "INSERT INTO mcp_user_tokens (id, mcp_server_id, agent_id, owner_id, token, expires_at) "
        "VALUES ('sealed-under-the-chat-key', :server, :agent, :owner, :token, :expires_at)",
        server=SERVER,
        agent=AGENT,
        owner=OWNER,
        token=sealed_under_the_chat_key(SECRET, associated_data=json.dumps([SERVER, OWNER, AGENT]).encode("utf-8")),
        expires_at=SqlUserTokenStore._timestamp(_later()),
    )

    with pytest.raises(InvalidTag):
        _run(store.load(MCP, OWNER, AGENT))


def test_a_token_sealed_under_another_key_raises_rather_than_loading_nothing(store, db_url) -> None:
    _seed(db_url, key=OTHER_KEY)

    with pytest.raises(InvalidTag):
        _run(store.load(MCP, OWNER, AGENT))


@parametrize_from_dict(
    {
        "empty key": {"raw": ""},
        "not base64": {"raw": "not-base64-at-all"},
        "short key": {"raw": base64.b64encode(b"k" * 16).decode("ascii")},
        "a chat encryption key": {"raw": base64.b64encode(b"k" * 32).decode("ascii")},
        "48 bytes": {"raw": base64.b64encode(b"k" * 48).decode("ascii")},
    }
)
def test_a_store_refuses_a_key_that_is_not_64_base64_bytes(async_engine, raw) -> None:
    with pytest.raises(ValueError, match="OUROBOROS_CREDENTIALS_ENCRYPTION_KEY") as refused:
        SqlUserTokenStore(async_engine, encryption_key=SecretStr(raw))

    if raw:
        assert raw not in str(refused.value)
