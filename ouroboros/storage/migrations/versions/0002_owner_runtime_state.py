"""Owner runtime state table (replaces state/state.json)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_runtime_state",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("owner_runtime_state")
