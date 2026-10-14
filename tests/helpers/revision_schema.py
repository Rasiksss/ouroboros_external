from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from tests.helpers.schema_harness import scoped_url, server_url, worker_schema


def _revision_schema_prefix() -> str:
    return f"rev_{worker_schema()}_"


@pytest.fixture
def blank_schema():
    engines: list[Engine] = []
    schemas: list[str] = []
    admin = create_engine(server_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        orphans = [
            str(row[0])
            for row in conn.execute(
                text("SELECT nspname FROM pg_namespace WHERE starts_with(nspname, :prefix)"),
                {"prefix": _revision_schema_prefix()},
            )
        ]
        for orphan in orphans:
            conn.execute(text(f'DROP SCHEMA "{orphan}" CASCADE'))

    def make() -> Engine:
        name = f"{_revision_schema_prefix()}{uuid.uuid4().hex}"
        with admin.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{name}"'))
        schemas.append(name)
        engine = create_engine(scoped_url(name))
        engines.append(engine)
        with engine.connect() as conn:
            selected = conn.execute(text("SELECT current_schema()")).scalar_one()
        if selected != name:
            raise RuntimeError(f"the connection selected {selected!r}, not the schema built for this test ({name!r})")
        return engine

    try:
        yield make
    finally:
        for engine in engines:
            engine.dispose()
        with admin.connect() as conn:
            for name in schemas:
                conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        admin.dispose()


def table_names(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def column_names(bind, table: str) -> set[str]:
    return {column["name"] for column in inspect(bind).get_columns(table)}


def index_names(bind, table: str) -> set[str]:
    return {index["name"] for index in inspect(bind).get_indexes(table) if index.get("name")}


def index_columns(bind, table: str, index: str) -> list[str]:
    for found in inspect(bind).get_indexes(table):
        if found.get("name") == index:
            return [str(column) for column in found.get("column_names") or []]
    return []


def primary_key_columns(bind, table: str) -> set[str]:
    return set(inspect(bind).get_pk_constraint(table).get("constrained_columns") or [])


def unique_constraint_names(bind, table: str) -> set[str]:
    return {constraint["name"] for constraint in inspect(bind).get_unique_constraints(table) if constraint.get("name")}


def unique_constraint_columns(bind, table: str) -> set[frozenset[str]]:
    return {
        frozenset(constraint.get("column_names") or []) for constraint in inspect(bind).get_unique_constraints(table)
    }


def constraint_names(bind, table: str) -> set[str]:
    inspector = inspect(bind)
    names = {constraint["name"] for constraint in inspector.get_unique_constraints(table)}
    names |= {constraint["name"] for constraint in inspector.get_check_constraints(table)}
    primary = inspector.get_pk_constraint(table).get("name")
    if primary:
        names.add(primary)
    return {name for name in names if name}


def index_definition(engine: Engine, index: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname=:name "
                    "AND schemaname = ANY (current_schemas(false))"
                ),
                {"name": index},
            ).scalar_one()
        )


def query_plan(engine: Engine, statement: str, parameters: object = None) -> str:
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        rows = conn.exec_driver_sql(f"EXPLAIN {statement}", parameters)
        return " ".join(str(row[0]) for row in rows)


def schema_version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


__all__ = [
    "blank_schema",
    "column_names",
    "constraint_names",
    "index_columns",
    "index_definition",
    "index_names",
    "primary_key_columns",
    "query_plan",
    "schema_version",
    "table_names",
    "unique_constraint_columns",
    "unique_constraint_names",
]
