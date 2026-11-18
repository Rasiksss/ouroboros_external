"""Connection-bound task locking and attempt authority shared during repository migration."""

import datetime
from typing import TypedDict

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

_OWNER_ADVISORY_SQL = "SELECT pg_advisory_xact_lock(hashtext('durable-background-jobs'), hashtext(:owner_id))"


class LeaseParams(TypedDict):
    now: str
    cutoff: str
    claim_token: str


def serialize_owner(conn: Connection, owner_id: str) -> None:
    conn.execute(text(_OWNER_ADVISORY_SQL), {"owner_id": owner_id})


def database_now(conn: Connection) -> datetime.datetime:
    value = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    return value if isinstance(value, datetime.datetime) else datetime.datetime.fromisoformat(value)


def lease_params(conn: Connection, claim_token: str = "") -> LeaseParams:
    # task_results imports the runtime factory that composes this module.
    from ouroboros.storage.task_results import HEARTBEAT_STALE_SEC

    now = database_now(conn)
    return {
        "now": now.isoformat(),
        "cutoff": (now - datetime.timedelta(seconds=HEARTBEAT_STALE_SEC)).isoformat(),
        "claim_token": claim_token,
    }


def task_for_transition(conn: Connection, task_id: str) -> Row[tuple[object, ...]] | None:
    row = conn.execute(text("SELECT * FROM tasks WHERE task_id=:task_id"), {"task_id": str(task_id)}).fetchone()
    if row is not None:
        serialize_owner(conn, str(row._mapping["owner_id"]))
        # Lock before evaluating lease time: PostgreSQL can wait after an UPDATE's WHERE.
        row = conn.execute(
            text("SELECT * FROM tasks WHERE task_id=:task_id FOR UPDATE"), {"task_id": str(task_id)}
        ).fetchone()
    return row


def terminal_status_binds(prefix: str = "terminal") -> tuple[str, dict[str, str]]:
    # task_results imports the runtime factory that composes this module.
    from ouroboros.storage.task_results import TERMINAL_STATUSES

    params = {f"{prefix}_{index}": value for index, value in enumerate(sorted(TERMINAL_STATUSES))}
    return ", ".join(f":{name}" for name in params), params
