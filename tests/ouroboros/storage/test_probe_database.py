from __future__ import annotations

import asyncio

import psycopg
import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from ouroboros.storage.probe_database import PROBE_WAIT_SEC, create_probe_database
from tests.helpers.postgres import postgres_urls


def test_the_handshake_budget_is_added_beside_the_deployments_own_options() -> None:
    database = create_probe_database(
        "postgresql+psycopg://user:pass@pg1:5432/ouroboros?options=-csearch_path%3Dmyschema"
    )
    try:
        assert database.engine.url.drivername == "postgresql+psycopg"
        assert dict(database.engine.url.query) == {
            "options": "-csearch_path=myschema",
            "connect_timeout": str(PROBE_WAIT_SEC),
        }
    finally:
        asyncio.get_event_loop().run_until_complete(database.dispose())


def test_a_probe_keeps_no_connection_for_the_next_one() -> None:
    database = create_probe_database(postgres_urls()[0])
    dialled: list[object] = []

    @event.listens_for(database.engine.sync_engine, "connect")
    def _count(dbapi_connection: object, _record: object) -> None:
        dialled.append(dbapi_connection)

    async def probe_twice() -> None:
        try:
            await database.dial()
            await database.dial()
        finally:
            await database.dispose()

    asyncio.get_event_loop().run_until_complete(probe_twice())

    assert len(dialled) == 2


def test_every_node_of_a_failover_set_is_dialled_once_within_the_budget(monkeypatch) -> None:
    dialled: list[dict[str, str]] = []

    async def refuse(**kwargs: str) -> None:
        dialled.append(kwargs)
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", refuse)
    database = create_probe_database(
        [
            "postgresql+psycopg://user:pass@pg1:5432/ouroboros",
            "postgresql+psycopg://user:pass@pg2:5432/ouroboros?options=-csearch_path%3Dmy",
        ]
    )

    async def dial() -> None:
        try:
            await database.dial()
        finally:
            await database.dispose()

    with pytest.raises(SQLAlchemyError):
        asyncio.get_event_loop().run_until_complete(dial())

    assert [node["host"] for node in dialled] == ["pg1", "pg2"]
    assert {node["connect_timeout"] for node in dialled} == {str(PROBE_WAIT_SEC)}
    assert dialled[1]["options"] == "-csearch_path=my"
    assert database.deadline_sec == PROBE_WAIT_SEC * 3, "the walk of both nodes, and then one answer"
