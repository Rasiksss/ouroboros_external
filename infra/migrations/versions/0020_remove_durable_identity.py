"""Irreversibly remove durable identity; see docs/ARCHITECTURE.md data layout."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    memory = sa.table("memory", sa.column("kind", sa.Text()))
    op.execute(memory.delete().where(memory.c.kind == "identity"))


def downgrade() -> None:
    """Deleted self-descriptions cannot be reconstructed."""
