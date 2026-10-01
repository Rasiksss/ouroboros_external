"""Baseline runtime metadata schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("owner_external_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("auth_context_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.Text(), primary_key=True),
        sa.Column("session_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("message_log_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("retention_policy", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("message_id", sa.Text(), primary_key=True),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("source_message_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("idx_messages_conversation_created", "messages", ["conversation_id", "created_at"])
    op.create_index("idx_messages_session_created", "messages", ["session_id", "created_at"])
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.Text(), primary_key=True),
        sa.Column("session_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("conversation_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("correlation_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=False, server_default="task"),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("input_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("input_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("result", sa.Text(), nullable=False, server_default=""),
        sa.Column("result_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_code", sa.Text(), nullable=False, server_default=""),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("worker_id", sa.Integer(), nullable=True),
        sa.Column("queued_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("heartbeat_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("finished_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("idx_tasks_claim", "tasks", ["status", "priority", "created_at", "task_id"])
    op.create_index("idx_tasks_conversation", "tasks", ["conversation_id", "created_at"])
    op.create_index("idx_tasks_correlation", "tasks", ["correlation_id"])
    op.create_table(
        "llm_usage",
        sa.Column("usage_id", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("session_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("idx_llm_usage_task", "llm_usage", ["task_id", "created_at"])
    op.create_index("idx_llm_usage_session", "llm_usage", ["session_id", "created_at"])
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("timestamp", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor_type", sa.Text(), nullable=False, server_default=""),
        sa.Column("resource_type", sa.Text(), nullable=False, server_default=""),
        sa.Column("resource_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("idx_audit_events_type", "audit_events", ["event_type", "timestamp"])
    op.create_index("idx_audit_events_resource", "audit_events", ["resource_type", "resource_id", "timestamp"])
    op.create_table(
        "context_summaries",
        sa.Column("summary_id", sa.Text(), primary_key=True),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("source_offsets_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_budget", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index(
        "idx_context_summaries_conversation",
        "context_summaries",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("context_summaries")
    op.drop_table("audit_events")
    op.drop_table("llm_usage")
    op.drop_table("tasks")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("sessions")
