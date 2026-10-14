"""``0013`` re-keys ``user_settings`` by the agent and carries the rows over under the default agent."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from infra.migrations.runner import alembic_config, upgrade_head

_INSERT_OWNER_ROW = text("INSERT INTO user_settings (owner_id, key, value) VALUES (:owner_id, :key, :value)")
_INSERT_ROW = text(
    "INSERT INTO user_settings (owner_id, agent_id, key, value) VALUES (:owner_id, :agent_id, :key, :value)"
)


def _config(engine):
    return alembic_config(str(engine.url.render_as_string(hide_password=False)))


def _migrate(engine, revision: str, *, down: bool = False) -> None:
    cfg = _config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(cfg, revision)


def _shape(engine, table: str) -> tuple[set[str], list[str]]:
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns(table)}
    return columns, list(inspector.get_pk_constraint(table)["constrained_columns"])


@pytest.fixture
def engine(blank_schema):
    return blank_schema()


def test_the_table_is_keyed_by_the_owner_the_agent_and_the_key(engine) -> None:
    upgrade_head(engine)
    columns, primary_key = _shape(engine, "user_settings")
    assert columns == {"owner_id", "agent_id", "key", "value"}
    assert primary_key == ["owner_id", "agent_id", "key"]
    assert "agent_settings" not in inspect(engine).get_table_names()


def test_an_owner_row_is_carried_over_under_the_default_agent(engine) -> None:
    _migrate(engine, "0011")
    with engine.begin() as conn:
        conn.execute(_INSERT_OWNER_ROW, {"owner_id": "alice", "key": "OUROBOROS_TIMEZONE", "value": "Asia/Tokyo"})

    _migrate(engine, "0013")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT owner_id, agent_id, key, value FROM user_settings")).all()
    assert rows == [("alice", "main", "OUROBOROS_TIMEZONE", "Asia/Tokyo")]


def test_one_row_per_owner_agent_and_key(engine) -> None:
    upgrade_head(engine)
    row = {"owner_id": "alice", "agent_id": "main", "key": "OUROBOROS_MODEL", "value": "a"}
    with engine.begin() as conn:
        conn.execute(_INSERT_ROW, row)
        conn.execute(_INSERT_ROW, {**row, "agent_id": "sub-1"})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(_INSERT_ROW, {**row, "value": "b"})


def test_the_reversal_keeps_the_default_agents_rows_and_drops_the_registry(engine) -> None:
    _migrate(engine, "0013")
    with engine.begin() as conn:
        conn.execute(
            _INSERT_ROW, {"owner_id": "alice", "agent_id": "main", "key": "OUROBOROS_LOCALE", "value": "en-US"}
        )
        conn.execute(_INSERT_ROW, {"owner_id": "alice", "agent_id": "sub-1", "key": "OUROBOROS_LOCALE", "value": "de"})

    _migrate(engine, "0011", down=True)

    assert "agents" not in inspect(engine).get_table_names()
    assert _shape(engine, "user_settings") == ({"owner_id", "key", "value"}, ["owner_id", "key"])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT owner_id, key, value FROM user_settings")).all() == [
            ("alice", "OUROBOROS_LOCALE", "en-US")
        ]
