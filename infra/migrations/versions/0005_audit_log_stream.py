"""Audit events log_stream column."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column(
            "log_stream",
            sa.Text(),
            nullable=False,
            server_default="events",
        ),
    )
    op.create_index("idx_audit_events_stream", "audit_events", ["log_stream", "timestamp"])


def downgrade() -> None:
    op.drop_index("idx_audit_events_stream", table_name="audit_events")
    op.drop_column("audit_events", "log_stream")
