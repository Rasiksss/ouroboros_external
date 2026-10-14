"""Bound retention of processed task input receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_inputs",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")
        ),
    )
    op.create_index(
        "idx_task_inputs_retention",
        "task_inputs",
        ["updated_at", "sequence"],
        postgresql_where=sa.text("state IN ('committed','cancelled')"),
    )


def downgrade() -> None:
    op.drop_index("idx_task_inputs_retention", table_name="task_inputs")
    op.drop_column("task_inputs", "updated_at")
