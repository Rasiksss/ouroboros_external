"""Persist root-owned plans and acceptance history."""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_plans",
        sa.Column("root_task_id", sa.Text(), sa.ForeignKey("tasks.task_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("surface_id", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("state_encrypted", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("task_plans")
