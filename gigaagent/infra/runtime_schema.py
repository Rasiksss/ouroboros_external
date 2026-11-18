"""Read-only contract for the schema deployed by AIFA's Liquibase changelog."""

from sqlalchemy import text
from sqlalchemy.engine import Engine

REQUIRED_SCHEMA_VERSION = 34


class RuntimeSchemaOutdatedError(RuntimeError):
    pass


def assert_runtime_schema(engine: Engine) -> None:
    with engine.connect() as conn:
        present = conn.execute(text("SELECT to_regclass('gigaagent_schema_version')")).scalar_one()
        if present is None:
            raise RuntimeSchemaOutdatedError(
                "GigaAgent schema is not deployed in the configured search_path. "
                "Apply aifa/liquibase/changelog.sql with the deployment DDL role before starting pods."
            )
        version = conn.execute(
            text("SELECT version FROM gigaagent_schema_version WHERE component = 'gigaagent'")
        ).scalar_one_or_none()
    if version != REQUIRED_SCHEMA_VERSION:
        raise RuntimeSchemaOutdatedError(
            f"GigaAgent requires schema {REQUIRED_SCHEMA_VERSION}, found {version}. "
            "Deploy the matching AIFA Liquibase changelog; application pods never migrate the database."
        )
