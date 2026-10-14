from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_A2A_USER_TOKENS = "a2a_user_tokens"
_A2A_OWNER_AGENT = "idx_a2a_user_tokens_owner_agent"


def _present() -> bool:
    return _A2A_USER_TOKENS in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _present():
        raise RuntimeError(f"0019 preflight: {_A2A_USER_TOKENS} already exists; this revision has partly run")
    op.create_table(
        _A2A_USER_TOKENS,
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("a2a_agent_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_a2a_user_tokens"),
        sa.UniqueConstraint("a2a_agent_id", "owner_id", "agent_id", name="uq_a2a_user_tokens_remote_owner_agent"),
    )
    op.create_index(_A2A_OWNER_AGENT, _A2A_USER_TOKENS, ["owner_id", "agent_id"])


def downgrade() -> None:
    if _present():
        op.drop_table(_A2A_USER_TOKENS)
