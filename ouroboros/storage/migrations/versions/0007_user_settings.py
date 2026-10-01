"""Per-user setting overrides.

Sparse by design: a row exists only for a key the owner changed, and its absence
means the instance value applies.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_settings",
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("user_settings")
