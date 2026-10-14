"""Apply Alembic migrations against a SQLAlchemy engine."""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from infra.sql_connection import sql_connection
from ouroboros.storage.db_url import escape_percent_for_alembic_ini
from ouroboros.storage.task_results import TERMINAL_STATUSES

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def alembic_config(db_url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", escape_percent_for_alembic_ini(db_url))
    cfg.attributes["configure_logger"] = False
    return cfg


#: The command that brings a database up. Named once, so a refusal can print the thing an
#: operator actually types.
UPGRADE_COMMAND = "uv run alembic -c alembic.ini upgrade head"


class RuntimeSchemaOutdatedError(RuntimeError):
    """The database is below the schema this code needs, and this process will not upgrade it."""


def upgrade_head(engine: Engine) -> None:
    """Apply pending migrations in deployment, never at runtime startup."""
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def assert_binary_rollback_safe(connection: Connection) -> None:
    """Run on the upgraded image before replacing it, after flag-off and consumer drain."""
    from ouroboros.storage.settings.store import load_settings

    revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    if (
        schema_version_of(revision) >= 33
        and connection.execute(
            text(
                "SELECT 1 FROM messages WHERE source='a2a' "
                "UNION ALL SELECT 1 FROM tasks WHERE source='a2a' AND task_type<>'a2a' LIMIT 1"
            )
        ).first()
    ):
        raise RuntimeError(
            "binary rollback blocked: context-scoped A2A data requires the A2A context migration; "
            "restore the pre-upgrade backup before starting an older image"
        )
    if load_settings().OUROBOROS_DURABLE_JOBS_ENABLED:
        raise RuntimeError("binary rollback requires OUROBOROS_DURABLE_JOBS_ENABLED=false")
    if connection.execute(text("SELECT 1 FROM scratchpad_blocks WHERE job_id<>'' LIMIT 1")).first():
        raise RuntimeError("binary rollback blocked: job scratchpad cleanup pending")
    # A stored run state is a round the older image cannot resume, whether its record is still parked
    # or already queued after the answer; the sweep drops the state once the task ends.
    terminal = {f"terminal_{index}": status for index, status in enumerate(sorted(TERMINAL_STATUSES))}
    if (
        inspect(connection).has_table("tool_confirmations")
        and connection.execute(
            text(
                "SELECT 1 FROM tasks JOIN tool_confirmations ON tool_confirmations.task_id=tasks.task_id "
                f"WHERE tasks.status NOT IN ({', '.join(f':{key}' for key in terminal)}) "
                "AND tool_confirmations.run_state_json<>'' LIMIT 1"
            ),
            terminal,
        ).first()
    ):
        raise RuntimeError("binary rollback blocked: a run is parked on, or resuming from, an owner confirmation")
    for row in connection.execute(
        text(
            "SELECT * FROM tasks WHERE detached AND NOT EXISTS (SELECT 1 FROM tasks newer "
            "WHERE newer.owner_id=tasks.owner_id AND newer.job_id=tasks.job_id "
            "AND newer.lineage_ordinal>tasks.lineage_ordinal)"
        )
    ):
        metadata = json.loads(row.metadata_json)
        delivery = metadata.get("terminal_delivery", {})
        if (
            row.status not in TERMINAL_STATUSES
            or not row.terminal_published_at
            or row.terminal_publish_claim is not None
            or row.terminal_publish_after is not None
            or delivery.get("text") != "accepted"
            or any(
                delivery.get(item["id"]) not in {"accepted", "unavailable"}
                for item in metadata.get("terminal_attachments", [])
            )
            or (row.status == "completed" and metadata.get("terminal_memory_done") is not True)
        ):
            raise RuntimeError("binary rollback blocked: detached execution, publication or memory pending")


def schema_version_of(revision: str) -> int:
    """The numeric version inside a revision id, the way the store reports its own."""
    digits = "".join(ch for ch in str(revision or "") if ch.isdigit())
    return int(digits or "0")


def head_schema_version() -> int:
    """The schema version the code in this checkout expects the database to be at."""
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_current_head()
    return schema_version_of(head or "")


def schema_version(engine: Engine) -> int:
    """Read the stamped schema revision; connection failures propagate."""
    with sql_connection(engine) as conn:
        if not inspect(conn).has_table("alembic_version"):
            return 0
        row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
    return schema_version_of(str(row[0])) if row is not None else 0


def assert_schema_at_head(engine: Engine) -> None:
    """Require the build's schema, or a drained database safe for binary rollback."""
    required = head_schema_version()
    current = schema_version(engine)
    if current < required:
        raise RuntimeSchemaOutdatedError(
            f"runtime database schema is at version {current}, this build requires {required}. "
            f"Bring the schema up before starting a runtime: {UPGRADE_COMMAND}"
        )
    if current > required:
        with engine.connect() as connection:
            assert_binary_rollback_safe(connection)
