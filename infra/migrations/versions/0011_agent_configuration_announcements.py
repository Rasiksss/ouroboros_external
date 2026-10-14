"""Which agent system configurations this deployment has already announced.

The announcement describes the image and its system prompt — one fact per deployment, not
one per replica — so eight processes starting the same build must publish it once. The right
to publish is claimed here rather than counted anywhere: the primary key on the announced
configuration's hash *is* the once-per-deployment rule, and a second announcer for one
configuration is refused by the database rather than by a check somebody can skip.

Keyed by the hash of what is announced rather than by a fixed row id, which is the shape
``deployment_keyspace`` uses. A fixed id would announce once for the life of the database and
never again, so a rolling deploy that changed the version or the prompt filename would be
invisible on the bus. Hashing what is announced makes "the configuration changed" and
"announce again" the same fact, with no version bookkeeping.

No backfill. An empty table reads as "nothing announced yet", which for the first start after
the upgrade means one announcement — the correct outcome, and the only one available: no
process can know what a predecessor published.

A row is deleted when the publish that claimed it failed, so the next start tries again. That
is the only delete; nothing prunes the table otherwise, and it grows by one row per distinct
configuration a deployment has ever run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_ANNOUNCEMENTS = "agent_configuration_announcements"


def upgrade() -> None:
    op.create_table(
        _ANNOUNCEMENTS,
        # The announced content's hash, with the event's timestamp left out of it: processes
        # building the same event at different instants have to derive the same value, or the
        # key would separate them instead of joining them.
        sa.Column("config_hash", sa.Text(), nullable=False),
        # Which component won the claim. Diagnostic only — it names who published, so a bus
        # with no event has a process to look at — and no reader decides anything on it.
        sa.Column("component", sa.Text(), nullable=False, server_default=""),
        # ISO, like every other time column in this store.
        sa.Column("announced_at", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("config_hash"),
    )


def downgrade() -> None:
    """Symmetric: nothing else reads the table, so dropping it loses only the claims.

    The cost is one duplicate announcement per configuration if the deployment is rolled
    forward again — the same cost the first start after the upgrade pays.
    """
    op.drop_table(_ANNOUNCEMENTS)
