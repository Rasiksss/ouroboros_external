from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from pydantic import SecretStr
from sqlalchemy import create_engine, event, inspect, text

from infra.async_engine import create_async_runtime_engine
from infra.migrations.runner import upgrade_head
from infra.security_context.interface import (
    SecurityContext,
    SecurityParameter,
    SecurityParameterKey,
    SecurityParameterType,
)
from infra.security_context.sql import SqlSecurityContextStore
from new_core.identity.owner_scope import OwnerScope
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.postgres import postgres_urls
from tests.helpers.revision_schema import index_names
from tests.helpers.security_context import context_of, parameters_of, sealed_under_the_chat_key

SECRET = "Bearer s3cr3t-value"
KEY = SecretStr(base64.b64encode(b"k" * 64).decode("ascii"))
OTHER_KEY = SecretStr(base64.b64encode(b"q" * 64).decode("ascii"))
OWNER = "alice-owner-id"
TOKEN = OwnerScope(owner_id=OWNER, agent_id="a1", surface_id="browser", chat_id="c1").security_token
OTHER_TOKEN = OwnerScope(owner_id="bob-owner-id", agent_id="a1", surface_id="browser", chat_id="c1").security_token
_RACE_ROUNDS = 12


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _header(name: str) -> SecurityParameterKey:
    return SecurityParameterKey(SecurityParameterType.HEADER, name)


def _cookie(name: str) -> SecurityParameterKey:
    return SecurityParameterKey(SecurityParameterType.COOKIE, name)


@pytest.fixture
def schema_engine(blank_schema):
    engine = blank_schema()
    upgrade_head(engine)
    return engine


@pytest.fixture
def db_url(schema_engine) -> str:
    return str(schema_engine.url.render_as_string(hide_password=False))


@pytest.fixture
def async_engine(db_url):
    engine = create_async_runtime_engine(db_url)
    yield engine
    _run(engine.dispose())


@pytest.fixture
def store(async_engine) -> SqlSecurityContextStore:
    return SqlSecurityContextStore(async_engine, encryption_key=KEY)


def _execute(db_url: str, sql: str, **params) -> list[dict]:
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return [dict(row) for row in result.mappings().all()] if result.returns_rows else []
    finally:
        engine.dispose()


def _rows(db_url: str) -> list[dict]:
    return _execute(
        db_url,
        "SELECT security_token, param_type, param_name, param_value, expires_at FROM security_contexts "
        "ORDER BY security_token, param_type, param_name",
    )


def _expire(db_url: str, token: str) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="microseconds")
    _execute(
        db_url, "UPDATE security_contexts SET expires_at = :past WHERE security_token = :token", past=past, token=token
    )


def test_the_table_is_keyed_by_token_type_and_name_and_indexed_by_expiry(schema_engine) -> None:
    primary_key = inspect(schema_engine).get_pk_constraint("security_contexts")["constrained_columns"]

    assert primary_key == ["security_token", "param_type", "param_name"]
    assert "idx_security_contexts_expires" in index_names(schema_engine, "security_contexts")


def _move_ciphertext(db_url: str, source: tuple[str, str, str], target: tuple[str, str, str]) -> None:
    _execute(
        db_url,
        "UPDATE security_contexts SET param_value = (SELECT param_value FROM security_contexts "
        "WHERE security_token = :source_token AND param_type = :source_type AND param_name = :source_name) "
        "WHERE security_token = :target_token AND param_type = :target_type AND param_name = :target_name",
        source_token=source[0],
        source_type=source[1],
        source_name=source[2],
        target_token=target[0],
        target_type=target[1],
        target_name=target[2],
    )


def test_a_context_round_trips_its_requested_headers_and_cookies(store) -> None:
    headers = {"authorization": SECRET, "x-sudir-token": "t2"}
    cookies = {"sudir": "abc", "theme": "dark"}
    context = context_of(headers=headers, cookies=cookies)

    _run(store.save(TOKEN, parameters_of(headers=headers, cookies=cookies)))

    assert _run(store.load(TOKEN, list(context.parameters))) == context


def test_a_load_returns_only_the_requested_keys(store) -> None:
    _run(
        store.save(
            TOKEN,
            parameters_of(
                headers={"authorization": SECRET, "x-sudir-token": "t2"}, cookies={"sudir": "abc", "theme": "dark"}
            ),
        )
    )

    loaded = _run(store.load(TOKEN, [_header("x-sudir-token"), _cookie("theme"), _cookie("absent")]))

    assert loaded == context_of(headers={"x-sudir-token": "t2"}, cookies={"theme": "dark"})


def test_a_load_with_no_keys_queries_nothing(store, async_engine) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))
    statements: list[str] = []

    def listener(conn, cursor, sql, *args) -> None:
        statements.append(sql)

    event.listen(async_engine.sync_engine, "before_cursor_execute", listener)
    try:
        loaded = _run(store.load(TOKEN, []))
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", listener)

    assert loaded == SecurityContext({})
    assert statements == []


def test_a_save_removes_a_name_the_newer_context_lacks(store) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"x-sudir-token": "first", "authorization": SECRET})))
    _run(store.save(TOKEN, parameters_of(headers={"x-sudir-token": "second"})))

    loaded = _run(store.load(TOKEN, [_header("x-sudir-token"), _header("authorization")]))

    assert loaded == context_of(headers={"x-sudir-token": "second"})


def test_a_save_replaces_rows_a_concurrent_save_of_the_token_committed_before_its_insert(store, async_engine) -> None:
    raced: list[str] = []

    def commit_racing_rows(conn, cursor, sql, parameters, context, executemany) -> None:
        if raced or not sql.lstrip().upper().startswith("INSERT INTO SECURITY_CONTEXTS"):
            return
        raced.append(sql)
        expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="microseconds")
        for name in ("x-sudir-token", "authorization"):
            cursor.execute(
                "INSERT INTO security_contexts (security_token, param_type, param_name, param_value, expires_at) "
                "VALUES (%s, 'header', %s, 'racing', %s)",
                (TOKEN, name, expires_at),
            )

    event.listen(async_engine.sync_engine, "before_cursor_execute", commit_racing_rows)
    try:
        _run(store.save(TOKEN, parameters_of(headers={"x-sudir-token": "mine"})))
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", commit_racing_rows)

    assert raced
    loaded = _run(store.load(TOKEN, [_header("x-sudir-token"), _header("authorization")]))
    assert loaded == context_of(headers={"x-sudir-token": "mine"})


class _RecordingConnection:
    def __init__(self, statements: list[tuple[str, object]]) -> None:
        self._statements = statements

    async def execute(self, statement, parameters=None) -> None:
        self._statements.append((str(statement), parameters))


class _RecordingEngine:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[_RecordingConnection]:
        yield _RecordingConnection(self.statements)


def test_a_save_locks_the_token_in_its_own_namespace() -> None:
    engine = _RecordingEngine()
    store = SqlSecurityContextStore(engine, encryption_key=KEY)

    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))

    statement, parameters = engine.statements[0]
    assert "pg_advisory_xact_lock(hashtext('security-contexts'), hashtext(:token))" in statement
    assert parameters == {"token": TOKEN}


def test_a_save_takes_the_advisory_lock_before_its_write(store, async_engine) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))
    statements: list[str] = []

    def listener(conn, cursor, sql, *args) -> None:
        statements.append(sql)

    event.listen(async_engine.sync_engine, "before_cursor_execute", listener)
    try:
        _run(store.save(TOKEN, parameters_of(headers={"x-sudir-token": "t2"})))
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", listener)

    assert "pg_advisory_xact_lock" in statements[0]
    assert statements[1].lstrip().upper().startswith(("INSERT", "DELETE"))


def test_concurrent_saves_of_one_token_leave_one_saves_set_on_postgres() -> None:
    urls = postgres_urls()
    upgraded = create_engine(urls[0])
    upgrade_head(upgraded)
    upgraded.dispose()
    token = f"security-context-race-{uuid.uuid4().hex}"
    engine = create_async_runtime_engine(urls[0])
    store = SqlSecurityContextStore(engine, encryption_key=KEY)
    one = parameters_of(headers={"x-sudir-token": "solo"})
    both = parameters_of(headers={"x-sudir-token": "pair"}, cookies={"sudir": "pair"})

    async def race() -> None:
        await asyncio.gather(store.save(token, one), store.save(token, both))

    try:
        for _ in range(_RACE_ROUNDS):
            _run(store.save(token, both))
            _run(race())
            loaded = _run(store.load(token, [_header("x-sudir-token"), _cookie("sudir")]))
            assert loaded in (
                context_of(headers={"x-sudir-token": "solo"}),
                context_of(headers={"x-sudir-token": "pair"}, cookies={"sudir": "pair"}),
            )
    finally:
        try:
            _run(store.save(token, ()))
        finally:
            _run(engine.dispose())


def test_an_empty_save_erases_the_tokens_parameters(store, db_url) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET}, cookies={"sudir": "abc"})))

    _run(store.save(TOKEN, ()))

    assert _run(store.load(TOKEN, [_header("authorization"), _cookie("sudir")])) == SecurityContext({})
    assert _rows(db_url) == []


def test_another_token_loads_nothing(store) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))

    assert _run(store.load(OTHER_TOKEN, [_header("authorization")])) == SecurityContext({})


def test_a_row_stores_the_name_and_type_in_the_clear_and_neither_the_value_nor_the_owner(store, db_url) -> None:
    cookie_value = "sudir-cookie-plaintext"
    _run(store.save(TOKEN, parameters_of(headers={"x-sudir-token": SECRET}, cookies={"sudir": cookie_value})))

    rows = _rows(db_url)

    assert [(row["security_token"], row["param_type"], row["param_name"]) for row in rows] == [
        (TOKEN, "cookie", "sudir"),
        (TOKEN, "header", "x-sudir-token"),
    ]
    for row in rows:
        for plaintext in (SECRET, "s3cr3t", cookie_value, OWNER):
            assert plaintext not in " ".join(str(column) for column in row.values())


def test_two_saves_of_one_value_store_different_ciphertexts(store, db_url) -> None:
    parameters = parameters_of(headers={"authorization": SECRET})

    _run(store.save(TOKEN, parameters))
    first = _rows(db_url)[0]["param_value"]
    _run(store.save(TOKEN, parameters))
    second = _rows(db_url)[0]["param_value"]

    assert first != second


def test_every_row_expires_its_own_ttl_after_the_save(store, db_url) -> None:
    before = datetime.now(UTC)
    _run(
        store.save(
            TOKEN,
            (
                *parameters_of(headers={"authorization": SECRET}, ttl_sec=60),
                *parameters_of(cookies={"sudir": "c"}, ttl_sec=600),
            ),
        )
    )
    after = datetime.now(UTC)

    expiries = {
        (row["param_type"], row["param_name"]): datetime.fromisoformat(row["expires_at"]) for row in _rows(db_url)
    }

    assert set(expiries) == {("header", "authorization"), ("cookie", "sudir")}
    for key, ttl_sec in ((("header", "authorization"), 60), (("cookie", "sudir"), 600)):
        assert before + timedelta(seconds=ttl_sec) <= expiries[key] <= after + timedelta(seconds=ttl_sec)


def test_a_save_with_two_parameters_of_one_key_touches_no_row(store, db_url) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"x-sudir-token": "kept"})))
    kept = _rows(db_url)
    duplicated = (
        SecurityParameter(_header("X-Sudir-Token"), SECRET, 600),
        SecurityParameter(_header("x-sudir-token"), "other-secret", 600),
    )

    with pytest.raises(ValueError, match="header:x-sudir-token") as refused:
        _run(store.save(TOKEN, duplicated))

    assert SECRET not in str(refused.value)
    assert "other-secret" not in str(refused.value)
    assert _rows(db_url) == kept


def test_an_expired_context_loads_nothing(store, db_url) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))
    _expire(db_url, TOKEN)

    assert _run(store.load(TOKEN, [_header("authorization")])) == SecurityContext({})


def test_an_expired_row_stays_stored_until_its_token_saves_again(store, db_url) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))
    _expire(db_url, TOKEN)

    assert [row["security_token"] for row in _rows(db_url)] == [TOKEN]


def test_a_save_removes_an_expired_row_of_a_name_the_newer_context_lacks(store, db_url) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET, "x-sudir-token": "t2"})))
    _expire(db_url, TOKEN)

    _run(store.save(TOKEN, parameters_of(headers={"x-sudir-token": "t3"})))

    assert [(row["param_type"], row["param_name"]) for row in _rows(db_url)] == [("header", "x-sudir-token")]


def test_a_save_after_expiry_serves_the_new_context(store, db_url) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": "old"})))
    _expire(db_url, TOKEN)

    _run(store.save(TOKEN, parameters_of(headers={"authorization": "new"})))

    assert _run(store.load(TOKEN, [_header("authorization")])) == context_of(headers={"authorization": "new"})


def test_a_save_leaves_the_expired_rows_of_other_tokens_alone(store, db_url) -> None:
    _run(store.save("stale-token", parameters_of(headers={"authorization": SECRET}, cookies={"sudir": "abc"})))
    _run(store.save("live-token", parameters_of(headers={"authorization": SECRET})))
    _expire(db_url, "stale-token")
    before = _rows(db_url)

    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))

    assert [row for row in _rows(db_url) if row["security_token"] != TOKEN] == before


@parametrize_from_dict(
    {
        "another token": {
            "saves": {
                OTHER_TOKEN: parameters_of(headers={"authorization": SECRET}),
                TOKEN: parameters_of(headers={"authorization": "mine"}),
            },
            "source": (OTHER_TOKEN, "header", "authorization"),
            "target": (TOKEN, "header", "authorization"),
            "requested": _header("authorization"),
        },
        "another name": {
            "saves": {TOKEN: parameters_of(headers={"authorization": SECRET, "x-sudir-token": "mine"})},
            "source": (TOKEN, "header", "authorization"),
            "target": (TOKEN, "header", "x-sudir-token"),
            "requested": _header("x-sudir-token"),
        },
        "header to cookie": {
            "saves": {TOKEN: parameters_of(headers={"sudir": SECRET}, cookies={"sudir": "mine"})},
            "source": (TOKEN, "header", "sudir"),
            "target": (TOKEN, "cookie", "sudir"),
            "requested": _cookie("sudir"),
        },
    }
)
def test_a_moved_ciphertext_does_not_open(store, db_url, saves, source, target, requested) -> None:
    for token, parameters in saves.items():
        _run(store.save(token, parameters))
    _move_ciphertext(db_url, source, target)

    with pytest.raises(InvalidTag):
        _run(store.load(TOKEN, [requested]))


def test_an_unrequested_row_that_cannot_be_opened_does_not_break_a_load(store, db_url) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET, "x-sudir-token": "t"})))
    _move_ciphertext(db_url, (TOKEN, "header", "authorization"), (TOKEN, "header", "x-sudir-token"))

    assert _run(store.load(TOKEN, [_header("authorization")])) == context_of(headers={"authorization": SECRET})


def test_a_cookie_name_is_matched_case_sensitively(store) -> None:
    _run(store.save(TOKEN, parameters_of(cookies={"Sudir": "abc"})))

    assert _run(store.load(TOKEN, [_cookie("sudir")])) == SecurityContext({})
    assert _run(store.load(TOKEN, [_cookie("Sudir")])) == context_of(cookies={"Sudir": "abc"})


def test_a_context_sealed_under_another_key_raises_rather_than_loading_nothing(async_engine) -> None:
    writer = SqlSecurityContextStore(async_engine, encryption_key=OTHER_KEY)
    reader = SqlSecurityContextStore(async_engine, encryption_key=KEY)
    _run(writer.save(TOKEN, parameters_of(headers={"authorization": SECRET})))

    with pytest.raises(InvalidTag):
        _run(reader.load(TOKEN, [_header("authorization")]))


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
        SqlSecurityContextStore(async_engine, encryption_key=SecretStr(raw))

    if raw:
        assert raw not in str(refused.value)


def test_a_store_takes_a_key_broken_over_lines(async_engine) -> None:
    encoded = KEY.get_secret_value()
    store = SqlSecurityContextStore(async_engine, encryption_key=SecretStr(f"{encoded[:64]}\n{encoded[64:]}\n"))

    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))

    assert _run(store.load(TOKEN, [_header("authorization")])) == context_of(headers={"authorization": SECRET})


def test_a_stored_value_is_aes_256_siv_with_its_nonce_in_front(store, db_url) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))
    blob = base64.urlsafe_b64decode(_rows(db_url)[0]["param_value"])
    associated_data = [json.dumps([TOKEN, "header", "authorization"]).encode("utf-8"), blob[:16]]

    assert AESSIV(b"k" * 64).decrypt(blob[16:], associated_data) == SECRET.encode("utf-8")


def test_a_value_sealed_under_a_chat_encryption_key_does_not_open(store, db_url) -> None:
    _run(store.save(TOKEN, parameters_of(headers={"authorization": SECRET})))
    aad = json.dumps([TOKEN, "header", "authorization"]).encode("utf-8")
    _execute(
        db_url,
        "UPDATE security_contexts SET param_value = :value",
        value=sealed_under_the_chat_key(SECRET, associated_data=aad),
    )

    with pytest.raises(InvalidTag):
        _run(store.load(TOKEN, [_header("authorization")]))
