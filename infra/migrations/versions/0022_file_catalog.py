from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_messages_file_owner", "messages", ["owner_id", "agent_id", "message_id"], unique=True)
    op.create_table(
        "folders",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("owner_id", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("parent_id", sa.Text),
        sa.Column("name", sa.Text, nullable=False),
        sa.UniqueConstraint("owner_id", "agent_id", "id"),
        sa.ForeignKeyConstraint(
            ["owner_id", "agent_id", "parent_id"], ["folders.owner_id", "folders.agent_id", "folders.id"]
        ),
    )
    op.create_index("idx_folders_page", "folders", ["owner_id", "agent_id", "parent_id", "name", "id"])
    op.create_index(
        "uq_folders_name", "folders", ["owner_id", "agent_id", sa.text("coalesce(parent_id, '')"), "name"], unique=True
    )
    op.create_table(
        "files",
        sa.Column("id", sa.Text, primary_key=True),
        *[
            sa.Column(name, sa.Text, nullable=False)
            for name in (
                "owner_id",
                "agent_id",
                "name",
                "storage_scope",
                "storage_key",
                "mime",
                "origin",
                "surface_id",
                "chat_id",
                "fingerprint",
                "producer_task",
                "artifact_path",
                "status",
                "created_at",
                "updated_at",
            )
        ],
        *[sa.Column(name, sa.Text) for name in ("folder_id", "expires_at", "claim_token", "claim_until")],
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("validation_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("artifact_sequence", sa.BigInteger, nullable=False, server_default="0"),
        sa.UniqueConstraint("owner_id", "agent_id", "id"),
        sa.UniqueConstraint("owner_id", "agent_id", "storage_scope", "storage_key"),
        sa.ForeignKeyConstraint(
            ["owner_id", "agent_id", "folder_id"], ["folders.owner_id", "folders.agent_id", "folders.id"]
        ),
        sa.CheckConstraint("status IN ('staging','draft','ready','deleting','deleted')"),
        sa.CheckConstraint("size >= 0"),
    )
    op.create_index("idx_files_page", "files", ["owner_id", "agent_id", "folder_id", "status", "name", "id"])
    op.create_index("idx_files_cleanup", "files", ["status", "expires_at", "claim_until", "id"])
    op.create_index(
        "idx_files_artifact",
        "files",
        ["owner_id", "agent_id", "producer_task", "artifact_path", "artifact_sequence", "id"],
    )
    op.create_table(
        "message_files",
        sa.Column("file_id", sa.Text, primary_key=True),
        sa.Column("message_id", sa.Text, primary_key=True),
        sa.Column("owner_id", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(["owner_id", "agent_id", "file_id"], ["files.owner_id", "files.agent_id", "files.id"]),
        sa.ForeignKeyConstraint(
            ["owner_id", "agent_id", "message_id"],
            ["messages.owner_id", "messages.agent_id", "messages.message_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_message_files_message", "message_files", ["message_id", "file_id"])
    op.create_table(
        "file_transfer_leases",
        sa.Column("token", sa.Text, primary_key=True),
        sa.Column("owner_id", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("process_id", sa.Text, nullable=False),
        sa.Column("outgoing", sa.Boolean, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_file_transfers_owner", "file_transfer_leases", ["owner_id", "agent_id", "expires_at"])
    op.create_index("idx_file_transfers_process", "file_transfer_leases", ["process_id", "expires_at"])
    op.create_index("idx_file_transfers_expiry", "file_transfer_leases", ["expires_at"])


def downgrade() -> None:
    raise RuntimeError("0022 requires a backup restore or a reverse migration preserving file metadata and links")
