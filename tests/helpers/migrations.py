"""Migration test helpers extracted from test_runtime_migrations."""

from __future__ import annotations

import json

from alembic import command
from sqlalchemy import inspect, text

from infra.migrations.runner import alembic_config


def schema_snapshot(engine, *, data: bool = False) -> str:
    inspector = inspect(engine)
    result: dict = {}
    with engine.connect() as conn:
        for table in sorted(inspector.get_table_names()):
            result[table] = {
                kind: getattr(inspector, f"get_{kind}")(table)
                for kind in (
                    "columns",
                    "indexes",
                    "pk_constraint",
                    "unique_constraints",
                    "foreign_keys",
                    "check_constraints",
                )
            }
            if data:
                result[table]["rows"] = sorted(map(tuple, conn.execute(text(f'SELECT * FROM "{table}"'))), key=repr)
    return json.dumps(result, default=str, sort_keys=True)


def upgrade_to(engine, revision: str) -> None:
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)
    cfg.attributes.pop("connection", None)


def downgrade_to(engine, revision: str) -> None:
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, revision)
    cfg.attributes.pop("connection", None)
