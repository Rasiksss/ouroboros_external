from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_AGENTS = "personal_a2a_agents"
_TOKENS = "personal_a2a_user_tokens"


def _present() -> set[str]:
    return {_AGENTS, _TOKENS} & set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    present = _present()
    if present:
        raise RuntimeError(f"0027 preflight: {', '.join(sorted(present))} already exists; this revision has partly run")
    op.create_table(
        _AGENTS,
        *[
            sa.Column(name, sa.Text, nullable=False)
            for name in ("id", "owner_id", "agent_id", "remote_agent_id", "entry", "created_at", "updated_at")
        ],
        sa.Column("static_token", sa.Text),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_personal_a2a_agents"),
        sa.UniqueConstraint(
            "owner_id", "agent_id", "remote_agent_id", name="uq_personal_a2a_agents_owner_agent_remote"
        ),
    )
    op.create_table(
        _TOKENS,
        *[
            sa.Column(name, sa.Text, nullable=False)
            for name in ("id", "personal_agent_id", "agent_id", "owner_id", "origin", "token", "expires_at")
        ],
        sa.PrimaryKeyConstraint("id", name="pk_personal_a2a_user_tokens"),
        sa.UniqueConstraint(
            "personal_agent_id", "owner_id", "agent_id", name="uq_personal_a2a_user_tokens_agent_owner_agent"
        ),
        sa.ForeignKeyConstraint(
            ["personal_agent_id"],
            [f"{_AGENTS}.id"],
            name="fk_personal_a2a_user_tokens_agent",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_personal_a2a_user_tokens_owner_agent", _TOKENS, ["owner_id", "agent_id"])


def downgrade() -> None:
    present = _present()
    if _TOKENS in present:
        op.drop_table(_TOKENS)
    if _AGENTS in present:
        op.drop_table(_AGENTS)
