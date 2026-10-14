"""The agent registry, and ``user_settings`` keyed by the agent as well as the owner.

``user_settings`` is rebuilt rather than altered in place: SQLite cannot change a primary
key, so the new shape is created beside the old one, the rows are copied under the core
agent's reserved id, and the old table is dropped. The reversal keeps only the core agent's
rows, since the old key has nowhere to put a sub-agent's.

Every literal is written out here rather than imported: a revision is history the moment it
is applied, so it carries its own copy of what it needs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None
_AGENTS = "agents"
_SETTINGS = "user_settings"
_SETTINGS_REBUILD = "user_settings_rebuild"
_CORE_AGENT_ID = "main"
_CORE_AGENT = "CORE_AGENT"
_SUB_AGENT = "SUB_AGENT"


def upgrade() -> None:
    op.create_table(
        _AGENTS,
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("agent_type", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("owner_id", "agent_id"),
        sa.CheckConstraint(f"agent_type IN ('{_CORE_AGENT}', '{_SUB_AGENT}')", name="ck_agents_kind"),
        sa.CheckConstraint(
            f"agent_type <> '{_CORE_AGENT}' OR agent_id = '{_CORE_AGENT_ID}'",
            name="ck_agents_core_holds_the_reserved_id",
        ),
        sa.CheckConstraint(
            f"agent_id <> '{_CORE_AGENT_ID}' OR agent_type = '{_CORE_AGENT}'", name="ck_agents_reserved_id_is_the_core"
        ),
    )
    op.create_table(
        _SETTINGS_REBUILD,
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "agent_id", "key"),
    )
    op.execute(
        sa.text(
            f"INSERT INTO {_SETTINGS_REBUILD} (owner_id, agent_id, key, value) "
            f"SELECT owner_id, '{_CORE_AGENT_ID}', key, value FROM {_SETTINGS}"
        )
    )
    op.drop_table(_SETTINGS)
    op.rename_table(_SETTINGS_REBUILD, _SETTINGS)


def downgrade() -> None:
    op.create_table(
        _SETTINGS_REBUILD,
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "key"),
    )
    op.execute(
        sa.text(
            f"INSERT INTO {_SETTINGS_REBUILD} (owner_id, key, value) "
            f"SELECT owner_id, key, value FROM {_SETTINGS} WHERE agent_id = '{_CORE_AGENT_ID}'"
        )
    )
    op.drop_table(_SETTINGS)
    op.rename_table(_SETTINGS_REBUILD, _SETTINGS)
    op.drop_table(_AGENTS)
