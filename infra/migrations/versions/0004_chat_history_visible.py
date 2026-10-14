"""Chat history visibility marker."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "chat_history_visible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_index(
        "idx_messages_chat_history_visible_created",
        "messages",
        ["chat_history_visible", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_messages_chat_history_visible_created", table_name="messages")
    op.drop_column("messages", "chat_history_visible")
