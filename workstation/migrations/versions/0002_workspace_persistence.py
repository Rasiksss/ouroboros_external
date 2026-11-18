"""workspace persistence

Revision ID: 0002_workspace_persistence
Revises: 0001_session_lifecycle
Create Date: 2026-06-17 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_workspace_persistence"
down_revision = "0001_session_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("workspace_persistence_scope_id", sa.String(length=128), nullable=True))
    op.add_column("sessions", sa.Column("workspace_persistence_root", sa.String(length=64), nullable=True))
    op.add_column("sessions", sa.Column("workspace_restored_revision_id", sa.String(length=32), nullable=True))
    op.add_column("sessions", sa.Column("workspace_snapshot_revision_id", sa.String(length=32), nullable=True))
    op.create_index(
        "ix_sessions_workspace_persistence_scope_id",
        "sessions",
        ["workspace_persistence_scope_id"],
    )

    op.create_table(
        "workspace_scopes",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("latest_revision_id", sa.String(length=32), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "workspace_revisions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("created_from_session_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("root", sa.String(length=64), nullable=False),
        sa.Column("manifest_object_key", sa.String(length=1024), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scope_id"], ["workspace_scopes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workspace_revisions_scope_id", "workspace_revisions", ["scope_id"])
    op.create_index(
        "ix_workspace_revisions_created_from_session_id",
        "workspace_revisions",
        ["created_from_session_id"],
    )
    op.create_index("ix_workspace_revisions_created_at", "workspace_revisions", ["created_at"])

    op.create_table(
        "workspace_files",
        sa.Column("revision_id", sa.String(length=32), nullable=False),
        sa.Column("path", sa.String(length=2048), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("mode", sa.Integer(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["revision_id"], ["workspace_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("revision_id", "path"),
    )
    op.create_index("ix_workspace_files_sha256", "workspace_files", ["sha256"])


def downgrade() -> None:
    op.drop_index("ix_workspace_files_sha256", table_name="workspace_files")
    op.drop_table("workspace_files")
    op.drop_index("ix_workspace_revisions_created_at", table_name="workspace_revisions")
    op.drop_index("ix_workspace_revisions_created_from_session_id", table_name="workspace_revisions")
    op.drop_index("ix_workspace_revisions_scope_id", table_name="workspace_revisions")
    op.drop_table("workspace_revisions")
    op.drop_table("workspace_scopes")
    op.drop_index("ix_sessions_workspace_persistence_scope_id", table_name="sessions")
    op.drop_column("sessions", "workspace_snapshot_revision_id")
    op.drop_column("sessions", "workspace_restored_revision_id")
    op.drop_column("sessions", "workspace_persistence_root")
    op.drop_column("sessions", "workspace_persistence_scope_id")
