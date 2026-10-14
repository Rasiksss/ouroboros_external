"""Owner decisions on tool calls that must not run unasked, and the state of the run parked on one
(`docs/ARCHITECTURE.md` §Owner confirmations)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_TABLE = "tool_confirmations"
_OPEN_INDEX = "uq_tool_confirmations_open"
_TASK_INDEX = "idx_tool_confirmations_task"
_OPEN_PREDICATE = "status = 'pending'"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("confirmation_id", sa.Text(), primary_key=True),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("surface_id", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("call_ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("round_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("args_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("run_state_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("applied_at", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index(
        _OPEN_INDEX,
        _TABLE,
        ["owner_id", "agent_id", "surface_id", "chat_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN_PREDICATE),
        sqlite_where=sa.text(_OPEN_PREDICATE),
    )
    op.create_index(_TASK_INDEX, _TABLE, ["task_id", "round_idx"])


def downgrade() -> None:
    # Guarded: pysqlite commits DDL outside the transaction, so a refusal below leaves this drop done.
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(_TABLE)
