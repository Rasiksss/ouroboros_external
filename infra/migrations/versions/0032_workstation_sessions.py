"""Share encrypted interactive Workstation credentials between runtimes."""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workstation_sessions",
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("token_encrypted", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "agent_id", "chat_id"),
    )


def downgrade() -> None:
    op.drop_table("workstation_sessions")
