from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

_TABLES = {
    "personal_mcp_servers": "uq_personal_mcp_servers_owner_agent_creation",
    "personal_a2a_agents": "uq_personal_a2a_agents_owner_agent_creation",
}
_COLUMNS = ("creation_key", "creation_fingerprint", "discovery_revision", "snapshot", "lease_holder", "lease_until")


def _present(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)} & set(_COLUMNS)


def upgrade() -> None:
    for table in _TABLES:
        present = _present(table)
        if present:
            raise RuntimeError(
                f"0028 preflight: {table} already has {', '.join(sorted(present))}; this revision has partly run"
            )
    for table, creation_constraint in _TABLES.items():
        op.add_column(table, sa.Column("creation_key", sa.Text))
        op.add_column(table, sa.Column("creation_fingerprint", sa.Text))
        op.add_column(table, sa.Column("discovery_revision", sa.Integer))
        op.add_column(table, sa.Column("snapshot", sa.Text))
        op.add_column(table, sa.Column("lease_holder", sa.Text))
        op.add_column(table, sa.Column("lease_until", sa.DateTime(timezone=True)))
        op.create_unique_constraint(creation_constraint, table, ["owner_id", "agent_id", "creation_key"])


def downgrade() -> None:
    for table, creation_constraint in _TABLES.items():
        present = _present(table)
        if "creation_key" in present:
            op.drop_constraint(creation_constraint, table, type_="unique")
        for column in reversed(_COLUMNS):
            if column in present:
                op.drop_column(table, column)
