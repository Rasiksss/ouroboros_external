"""Durable logical jobs on the existing task queue."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_TASKS = "tasks"
_SCRATCHPAD = "scratchpad_blocks"
_OLD_ONE_ROOT = "uq_tasks_one_running_root_per_owner"
_FOREGROUND_ONE_ROOT = "uq_tasks_one_running_foreground_root_per_owner_agent"
_OWNER_JOB = "idx_tasks_owner_job"
_TERMINAL = "idx_tasks_terminal_publication"
_SCRATCHPAD_OLD = "idx_scratchpad_blocks_owner_status_id"
_SCRATCHPAD_JOB = "idx_scratchpad_blocks_owner_job_status_id"


def _where(sql: str) -> dict[str, Any]:
    clause = sa.text(sql)
    return {"postgresql_where": clause, "sqlite_where": clause}


def upgrade() -> None:
    with op.batch_alter_table(_TASKS) as batch:
        batch.add_column(sa.Column("job_id", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("lineage_ordinal", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("detached", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("terminal_published_at", sa.Text(), nullable=True))
        batch.add_column(sa.Column("terminal_publish_claim", sa.Text(), nullable=True))
        batch.add_column(sa.Column("terminal_publish_after", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE tasks SET job_id=task_id WHERE job_id=''"))

    with op.batch_alter_table(_SCRATCHPAD) as batch:
        batch.add_column(sa.Column("job_id", sa.Text(), nullable=False, server_default=""))

    op.drop_index(_OLD_ONE_ROOT, table_name=_TASKS)
    op.create_index(
        _OWNER_JOB, _TASKS, ["owner_id", "job_id", "lineage_ordinal"], unique=True, **_where("job_id <> ''")
    )
    op.create_index(
        _TERMINAL,
        _TASKS,
        ["terminal_publish_after", "finished_at"],
        **_where(
            "detached AND terminal_published_at IS NULL "
            "AND status IN ('completed','cancelled','failed','rejected_duplicate','budget_exceeded')"
        ),
    )
    op.create_index(
        _FOREGROUND_ONE_ROOT,
        _TASKS,
        ["owner_id", "agent_id"],
        unique=True,
        **_where("status = 'running' AND parent_task_id = '' AND task_type = 'task' AND NOT detached"),
    )
    op.drop_index(_SCRATCHPAD_OLD, table_name=_SCRATCHPAD)
    op.create_index(
        _SCRATCHPAD_JOB,
        _SCRATCHPAD,
        ["owner_id", "agent_id", "job_id", "status", "id"],
    )


def downgrade() -> None:
    raise RuntimeError("0014 durable jobs schema downgrade is unsupported; binary rollback must retain schema 0014")
