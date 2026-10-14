from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_tasks_chat_status", "tasks", ["owner_id", "agent_id", "surface_id", "chat_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_tasks_chat_status", table_name="tasks")
