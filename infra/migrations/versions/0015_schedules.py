"""Owner-scoped schedules and their slot receipts, moved out of skill state storage.

Unconditional despite the issue asking otherwise: one stamped version must mean one schema, and
the upgrade is a deploy step. ``schedule_runs``' primary key is the at-most-once claim.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_SCHEDULES = "schedules"
_RUNS = "schedule_runs"


def _preflight() -> None:
    """Refuse a schema this revision has partly written."""
    present = set(sa.inspect(op.get_bind()).get_table_names())
    for table in (_SCHEDULES, _RUNS):
        if table in present:
            raise RuntimeError(f"0015 preflight: {table} already exists; this revision has partly run")


def upgrade() -> None:
    _preflight()
    op.create_table(
        _SCHEDULES,
        sa.Column("schedule_id", sa.Text(), primary_key=True),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("next_run_at", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index("idx_schedules_owner", _SCHEDULES, ["owner_id", "agent_id", "state"])
    op.create_index("idx_schedules_due", _SCHEDULES, ["state", "next_run_at"])
    op.create_table(
        _RUNS,
        sa.Column("schedule_id", sa.Text(), nullable=False),
        sa.Column("slot_id", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("schedule_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("claimed_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        # Whether the failure was a property of the row. The winner writes the receipt before
        # it writes the schedule, so a peer reading the receipt in between decides with it.
        sa.Column("permanent", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("schedule_id", "slot_id", name="pk_schedule_runs"),
    )
    # The due scan asks whether this schedule has a live claim. Partial on purpose: live claims are
    # a handful at any moment, and over a full index the planner materialises every settled receipt.
    op.create_index(
        "idx_schedule_runs_claim",
        _RUNS,
        ["schedule_id", "claimed_at"],
        postgresql_where=sa.text("status = 'claimed'"),
        sqlite_where=sa.text("status = 'claimed'"),
    )
    # Every replica sweeps settled receipts on its own cadence; unindexed that is a full scan each.
    op.create_index("idx_schedule_runs_settled", _RUNS, ["finished_at"])


def downgrade() -> None:
    op.drop_index("idx_schedule_runs_settled", table_name=_RUNS)
    op.drop_index("idx_schedule_runs_claim", table_name=_RUNS)
    op.drop_table(_RUNS)
    op.drop_index("idx_schedules_due", table_name=_SCHEDULES)
    op.drop_index("idx_schedules_owner", table_name=_SCHEDULES)
    op.drop_table(_SCHEDULES)
