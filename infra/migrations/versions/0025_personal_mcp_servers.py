from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_SERVERS = "personal_mcp_servers"
_TOKENS = "personal_mcp_user_tokens"


def _present() -> set[str]:
    return {_SERVERS, _TOKENS} & set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    present = _present()
    if present:
        raise RuntimeError(f"0025 preflight: {', '.join(sorted(present))} already exists; this revision has partly run")
    op.create_table(
        _SERVERS,
        *[
            sa.Column(name, sa.Text, nullable=False)
            for name in ("id", "owner_id", "agent_id", "server_id", "entry", "created_at", "updated_at")
        ],
        sa.Column("static_token", sa.Text),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_personal_mcp_servers"),
        sa.UniqueConstraint("owner_id", "agent_id", "server_id", name="uq_personal_mcp_servers_owner_agent_server"),
    )
    op.create_table(
        _TOKENS,
        *[
            sa.Column(name, sa.Text, nullable=False)
            for name in ("id", "personal_server_id", "agent_id", "owner_id", "origin", "token", "expires_at")
        ],
        sa.PrimaryKeyConstraint("id", name="pk_personal_mcp_user_tokens"),
        sa.UniqueConstraint(
            "personal_server_id", "owner_id", "agent_id", name="uq_personal_mcp_user_tokens_server_owner_agent"
        ),
        sa.ForeignKeyConstraint(
            ["personal_server_id"],
            [f"{_SERVERS}.id"],
            name="fk_personal_mcp_user_tokens_server",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_personal_mcp_user_tokens_owner_agent", _TOKENS, ["owner_id", "agent_id"])


def downgrade() -> None:
    present = _present()
    if _TOKENS in present:
        op.drop_table(_TOKENS)
    if _SERVERS in present:
        op.drop_table(_SERVERS)
