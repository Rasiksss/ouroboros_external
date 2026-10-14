"""Durable inputs for a running foreground task."""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("input_open", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("tasks", sa.Column("input_claim", sa.Text(), nullable=False, server_default=""))
    op.create_table(
        "task_inputs",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("input_id", sa.Text(), nullable=False, unique=True),
        sa.Column("client_input_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("surface_id", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("operation_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("dispatched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False, server_default=""),
        sa.CheckConstraint("state IN ('pending','editing','committed','cancelled')", name="ck_task_inputs_state"),
        sa.UniqueConstraint(
            "owner_id", "agent_id", "surface_id", "chat_id", "client_input_id", name="uq_task_inputs_client"
        ),
    )
    op.create_index("idx_task_inputs_task_sequence", "task_inputs", ["task_id", "sequence"])
    op.create_index(
        "idx_task_inputs_scope_sequence", "task_inputs", ["owner_id", "agent_id", "surface_id", "chat_id", "sequence"]
    )

    op.create_index(
        "uq_task_inputs_editing",
        "task_inputs",
        ["owner_id", "agent_id", "surface_id", "chat_id"],
        unique=True,
        postgresql_where=sa.text("state='editing'"),
    )
    op.create_index(
        "idx_task_inputs_active",
        "task_inputs",
        ["owner_id", "agent_id", "surface_id", "chat_id", "sequence"],
        postgresql_where=sa.text("state IN ('pending','editing')"),
    )

    op.create_index(
        "idx_task_inputs_dispatch_message", "task_inputs", ["message_id"], postgresql_where=sa.text("dispatched")
    )


def downgrade() -> None:
    raise RuntimeError("Preserve task_inputs on binary rollback; dropping it would lose accepted messages")
