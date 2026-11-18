"""PostgreSQL structure belongs to deployment, never to application pods."""

from sqlalchemy import inspect


def ensure_store_schema(metadata, engine) -> None:
    # SQLite remains a disposable local/test backend. Production is PostgreSQL.
    if engine.dialect.name == "sqlite":
        metadata.create_all(engine)
        return
    inspector = inspect(engine)
    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name, schema=table.schema):
            raise RuntimeError(
                f"Missing table {table.name}; apply aifa/liquibase/changelog.sql "
                "with the deployment DDL role before starting application pods."
            )
        columns = {c["name"] for c in inspector.get_columns(table.name, schema=table.schema)}
        missing = sorted(set(table.columns.keys()) - columns)
        if missing:
            raise RuntimeError(
                f"Table {table.name} lacks columns {', '.join(missing)}; "
                "apply the AIFA Liquibase changelog before starting pods."
            )
