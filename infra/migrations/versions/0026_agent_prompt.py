"""Owner-provided agent instructions; downgrade discards them."""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("prompt", sa.Text(), nullable=False, server_default=""))
    op.create_check_constraint("ck_agents_prompt_length", "agents", "char_length(prompt) <= 8000")


def downgrade() -> None:
    op.drop_constraint("ck_agents_prompt_length", "agents", type_="check")
    op.drop_column("agents", "prompt")
