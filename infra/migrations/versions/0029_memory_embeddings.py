"""Derived memory vectors; ordinary PostgreSQL does not need the optional extension."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory", sa.Column("embedding", postgresql.ARRAY(sa.REAL()), nullable=True))
    op.add_column("memory", sa.Column("embedding_key", sa.Text(), nullable=False, server_default=""))
    op.add_column("memory", sa.Column("embedding_dimensions", sa.SmallInteger(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("memory", "embedding_dimensions")
    op.drop_column("memory", "embedding_key")
    op.drop_column("memory", "embedding")
