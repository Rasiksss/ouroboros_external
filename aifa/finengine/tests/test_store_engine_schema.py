import pytest
from sqlalchemy import event

from finengine.store import engine


SCHEMA = "aif-aef-agnbf-af-aifaouroboros-finengine-db"
DATABASE_URL = "postgresql+psycopg://app:unused@invalid.example/aif_aef_metadb_dev"


@pytest.mark.parametrize("schema", [SCHEMA, "dev_2", "MixedCase", "_dev", "a" * 63])
def test_schema_names_allow_hyphens_and_existing_names(schema):
    assert engine.проверить_имя(schema) == schema


@pytest.mark.parametrize(
    "schema",
    ["", "1dev", "a" * 64, "dev\n", "dev public", "dev,public", 'dev"', "dev;DROP SCHEMA public", "dev\\x"],
)
def test_invalid_schema_names_are_rejected(schema):
    with pytest.raises(ValueError):
        engine.проверить_имя(schema)


@pytest.mark.parametrize("schema", [SCHEMA, "dev_2", "MixedCase"])
def test_postgres_driver_receives_quoted_schema_without_connecting(monkeypatch, schema):
    monkeypatch.setattr(engine.config.settings, "pg_schema", schema)
    connect_args = {"sslmode": "verify-full"}
    db = engine.движок(DATABASE_URL, connect_args=connect_args)
    captured = {}

    class ConnectionCaptured(Exception):
        pass

    @event.listens_for(db, "do_connect")
    def capture_connection(dialect, connection_record, args, kwargs):
        captured.update(kwargs)
        raise ConnectionCaptured

    try:
        with pytest.raises(ConnectionCaptured):
            db.connect()
    finally:
        db.dispose()

    assert captured["options"] == f'-csearch_path="{schema}"'
    assert captured["sslmode"] == "verify-full"
    assert connect_args == {"sslmode": "verify-full"}


def test_sqlite_does_not_receive_postgres_schema_options(monkeypatch):
    monkeypatch.setattr(engine.config.settings, "pg_schema", SCHEMA)
    db = engine.движок("sqlite://")
    try:
        with db.connect() as connection:
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    finally:
        db.dispose()


@pytest.mark.parametrize("from_connect_args", [False, True])
def test_schema_keeps_existing_driver_options(monkeypatch, from_connect_args):
    monkeypatch.setattr(engine.config.settings, "pg_schema", SCHEMA)
    from sqlalchemy.engine import URL
    url = URL.create("postgresql+psycopg", host="db", query={
        "sslmode": "verify-full", "options": "-cstatement_timeout=5000",
    }).render_as_string(hide_password=False)
    kwargs = {"connect_args": {"options": "-clock_timeout=2000"}} if from_connect_args else {}
    captured = {}
    monkeypatch.setattr(engine, "_create_engine", lambda url, **kw: captured.update(kw))
    engine.движок(url, **kwargs)
    option = "-clock_timeout=2000" if from_connect_args else "-cstatement_timeout=5000"
    assert captured["connect_args"]["options"] == f'{option} -csearch_path="{SCHEMA}"'
