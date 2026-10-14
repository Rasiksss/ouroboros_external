from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from infra.migrations.runner import alembic_config, upgrade_head
from tests.helpers.migrations import upgrade_to
from tests.helpers.revision_schema import column_names, schema_version, table_names

_INSERT = text(
    "INSERT INTO agents (owner_id, agent_id, name, description, is_active, created_at) VALUES (:owner_id, :agent_id, :name, '', true, '2026-09-01T00:00:00+00:00')"
)
#: The 0013 shape, for the one test that stops there: the column 0016 drops is NOT NULL below it.
_INSERT_WITH_KIND = text(
    "INSERT INTO agents (owner_id, agent_id, name, description, agent_type, is_active, created_at) "
    "VALUES (:owner_id, :agent_id, '', '', 'CORE_AGENT', true, '2026-09-01T00:00:00+00:00')"
)


def _engine(blank_schema):
    engine = blank_schema()
    upgrade_head(engine)
    return engine


def _insert(engine, **row) -> None:
    with engine.begin() as conn:
        conn.execute(_INSERT, {"name": "", **row})


def _columns(engine) -> set[str]:
    return column_names(engine, "agents")


def test_the_migration_is_reachable_from_head(blank_schema) -> None:
    engine = _engine(blank_schema)
    assert _columns(engine) == {"owner_id", "agent_id", "name", "description", "prompt", "is_active", "created_at"}


def test_the_table_carries_no_kind(blank_schema) -> None:
    assert "agent_type" not in _columns(_engine(blank_schema))


def test_an_agent_id_is_unique_within_its_owner_and_free_across_owners(blank_schema) -> None:
    engine = _engine(blank_schema)
    _insert(engine, owner_id="owner-a", agent_id="main")
    _insert(engine, owner_id="owner-b", agent_id="main")
    with pytest.raises(IntegrityError):
        _insert(engine, owner_id="owner-a", agent_id="main")


def test_the_reserved_id_is_one_row_per_owner_and_a_sub_agent_takes_another(blank_schema) -> None:
    """The primary key is what now says "one default agent per owner"."""
    engine = _engine(blank_schema)
    _insert(engine, owner_id="owner-a", agent_id="main")
    _insert(engine, owner_id="owner-a", agent_id="0f9a")
    with engine.connect() as conn:
        ids = {row[0] for row in conn.execute(text("SELECT agent_id FROM agents WHERE owner_id = 'owner-a'"))}
    assert ids == {"main", "0f9a"}


def test_the_table_goes_up_and_comes_back_down(blank_schema) -> None:
    engine = blank_schema()
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "0013")
    cfg.attributes.pop("connection", None)
    with engine.begin() as conn:
        conn.execute(_INSERT_WITH_KIND, {"owner_id": "owner-a", "agent_id": "main"})
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "0011")
    cfg.attributes.pop("connection", None)
    tables = table_names(engine)
    assert schema_version(engine) == "0011"
    assert "agents" not in tables
    assert "tasks" in tables


def test_the_kind_comes_back_derived_from_the_reserved_id(blank_schema) -> None:
    engine = blank_schema()
    upgrade_to(engine, "0018")
    _insert(engine, owner_id="owner-a", agent_id="main")
    _insert(engine, owner_id="owner-a", agent_id="0f9a")
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "0014")
    cfg.attributes.pop("connection", None)
    with engine.connect() as conn:
        kinds = dict(conn.execute(text("SELECT agent_id, agent_type FROM agents WHERE owner_id = 'owner-a'")).all())
    assert kinds == {"main": "CORE_AGENT", "0f9a": "SUB_AGENT"}
