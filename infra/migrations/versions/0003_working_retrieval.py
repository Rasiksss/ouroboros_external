"""Working retrieval tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column("tool_invocation_id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("conversation_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=""),
        sa.Column("args_redacted_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("result_summary_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("result_index_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("search_terms_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("payload_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("sensitivity", sa.Text(), nullable=False, server_default="normal"),
        sa.Column("retention_class", sa.Text(), nullable=False, server_default="summary"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("idx_tool_invocations_task", "tool_invocations", ["task_id", "created_at"])
    op.create_index(
        "idx_tool_invocations_conversation",
        "tool_invocations",
        ["conversation_id", "created_at"],
    )
    op.create_index("idx_tool_invocations_tool", "tool_invocations", ["tool_name", "created_at"])
    op.create_index("idx_tool_invocations_expires", "tool_invocations", ["expires_at"])

    op.create_table(
        "task_episodes",
        sa.Column("episode_id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("conversation_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("user_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("answer_preview", sa.Text(), nullable=False, server_default=""),
        sa.Column("tools_used_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("domains_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("entities_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("refs_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("search_terms_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("idx_task_episodes_task", "task_episodes", ["task_id", "created_at"])
    op.create_index(
        "idx_task_episodes_conversation",
        "task_episodes",
        ["conversation_id", "created_at"],
    )
    op.create_index("idx_task_episodes_expires", "task_episodes", ["expires_at"])


def downgrade() -> None:
    op.drop_table("task_episodes")
    op.drop_table("tool_invocations")
