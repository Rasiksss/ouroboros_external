from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_SECURITY_CONTEXTS = "security_contexts"
_USER_TOKENS = "mcp_user_tokens"


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    present = sorted({_SECURITY_CONTEXTS, _USER_TOKENS} & _existing_tables())
    if present:
        raise RuntimeError(f"0018 preflight: {', '.join(present)} already exists; this revision has partly run")
    op.create_table(
        _SECURITY_CONTEXTS,
        sa.Column("security_token", sa.Text(), nullable=False),
        sa.Column("param_type", sa.Text(), nullable=False),
        sa.Column("param_name", sa.Text(), nullable=False),
        sa.Column("param_value", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("security_token", "param_type", "param_name", name="pk_security_contexts"),
    )
    op.create_index("idx_security_contexts_expires", _SECURITY_CONTEXTS, ["expires_at"])
    op.create_table(
        _USER_TOKENS,
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("mcp_server_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_mcp_user_tokens"),
        sa.UniqueConstraint("mcp_server_id", "owner_id", "agent_id", name="uq_mcp_user_tokens_server_owner_agent"),
    )


def downgrade() -> None:
    existing = _existing_tables()
    if _USER_TOKENS in existing:
        op.drop_table(_USER_TOKENS)
    if _SECURITY_CONTEXTS in existing:
        op.drop_index("idx_security_contexts_expires", table_name=_SECURITY_CONTEXTS)
        op.drop_table(_SECURITY_CONTEXTS)
