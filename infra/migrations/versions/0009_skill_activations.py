"""Skills an owner's agent activated by reading their SKILL.md.

One row per skill and ownership pair: a repeated read moves ``activated_at`` forward rather
than adding a row. The manifest text is not stored; a restore re-reads it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_activations",
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("skill_name", sa.Text(), nullable=False),
        sa.Column("activated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "agent_id", "skill_name"),
    )


def downgrade() -> None:
    op.drop_table("skill_activations")
