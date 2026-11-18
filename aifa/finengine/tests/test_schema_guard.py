from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, Table

from finengine.store import schema_guard


@pytest.mark.parametrize(
    "present,columns,ok", [(False, [], False), (True, [], False), (True, [{"name": "id"}], True)]
)
def test_postgres_schema_validation_never_creates_tables(monkeypatch, present, columns, ok):
    metadata = MetaData()
    Table("sample", metadata, Column("id", Integer))
    monkeypatch.setattr(metadata, "create_all", lambda *a, **k: pytest.fail("DDL must not run"))
    inspector = SimpleNamespace(
        has_table=lambda *a, **k: present, get_columns=lambda *a, **k: columns
    )
    monkeypatch.setattr(schema_guard, "inspect", lambda e: inspector)
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    if ok:
        schema_guard.ensure_store_schema(metadata, engine)
    else:
        with pytest.raises(RuntimeError, match="Liquibase|liquibase"):
            schema_guard.ensure_store_schema(metadata, engine)
