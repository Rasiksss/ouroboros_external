"""Core-memory tables: one unified ``memory`` table plus scratchpad working memory.

``memory`` replaces three legacy stores at once — the ``memory/global/*`` fact
blobs, the ``memory/knowledge/*.md`` topic files, and the ``memory/identity.md``
document — because they were always the same shape: a named markdown body under
an owner, written by upsert, surfaced through a rendered index. Each row carries
the ``kind`` its source implies. ``scratchpad_blocks`` replaces
``memory/scratchpad_blocks.json`` and stays its own table: it is short-term
working memory with no natural key and its own FIFO discipline.

One thing here is not core memory: ``ChatHistoryVisibilityBackfill`` repairs
``messages.chat_history_visible``, which 0004 added with a ``true`` default and
never backfilled. It rides along in this revision because 0004 is already
applied everywhere, so a correction placed there would never run again.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)


class MemoryMigration:
    """Creates the unified ``memory`` table."""

    table_name = "memory"
    index_name = "idx_memory_owner_status_updated"

    def create_table(self) -> None:
        op.create_table(
            self.table_name,
            # Surrogate key: no current kind needs it — every one is named — but
            # it costs one column and keeps the table able to hold an unnamed,
            # ordered kind later without a key redesign.
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("owner_id", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("source", sa.Text(), nullable=False, server_default=""),
            sa.Column("first_recorded_at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("last_recalled_at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("recall_count", sa.Integer(), nullable=False, server_default="0"),
            # Unique on (owner_id, name) and not (owner_id, kind, name): a name
            # identifies exactly one memory, so memory_read needs no second
            # parameter to disambiguate.
            sa.UniqueConstraint("owner_id", "name", name="uq_memory_owner_name"),
            # The status vocabulary is closed, which is what lets the prompt
            # listing filter on it in SQL: a value outside the pair would be
            # visible to the fail-open normalizer and invisible to the query,
            # and the two readers disagreeing is worse than either answer.
            sa.CheckConstraint("status IN ('active', 'archived')", name="ck_memory_status"),
        )
        # Shaped for the one hot query — the prompt listing, which asks for one
        # owner's active headers newest-first with name breaking ties. ``kind``
        # is deliberately absent: no statement in the package puts it in a
        # predicate, so indexing it bought write amplification and nothing else.
        op.create_index(
            self.index_name,
            self.table_name,
            ["owner_id", "status", sa.text("updated_at DESC"), "name"],
        )

    def drop_table(self) -> None:
        op.drop_index(self.index_name, table_name=self.table_name)
        op.drop_table(self.table_name)


class ScratchpadBlocksMigration:
    """Creates ``scratchpad_blocks``."""

    table_name = "scratchpad_blocks"
    index_name = "idx_scratchpad_blocks_owner_status_id"

    def create_table(self) -> None:
        op.create_table(
            self.table_name,
            # The id is the block order key (design D6/D7).
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("owner_id", sa.Text(), nullable=False),
            sa.Column("ts", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False, server_default="task"),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        )
        op.create_index(self.index_name, self.table_name, ["owner_id", "status", "id"])

    def drop_table(self) -> None:
        op.drop_index(self.index_name, table_name=self.table_name)
        op.drop_table(self.table_name)


# ------------------------------------------------- chat history visibility


def _is_a2a_chat_id(chat_id: Any) -> bool:
    """Frozen copy of ``ouroboros.contracts.chat_id_policy.is_a2a_chat_id``.

    Every negative chat id is synthetic agent-to-agent traffic — legacy A2A
    minted unbounded negatives, so the range is open-ended. Only the types a
    JSON object can hold are considered, and a ``float`` is declined rather than
    truncated, exactly as the original declines it: a chat id that arrived as
    ``-1001.0`` was visible to the readers of the day and stays visible here.
    """
    if isinstance(chat_id, bool):
        # int(True) is 1. Spelled out because ``bool`` is an ``int`` subclass
        # and would otherwise take the branch below.
        return False
    if isinstance(chat_id, int):
        return chat_id < 0
    if isinstance(chat_id, str):
        try:
            return int(chat_id) < 0
        except ValueError:
            return False
    return False


class ChatHistoryVisibilityBackfill:
    """Recomputes ``messages.chat_history_visible`` for the rows 0004 defaulted.

    0004 added the column with ``server_default=true`` and copied nothing into
    it, so every row that predates it reads as owner-visible — including
    progress messages and synthetic A2A traffic, which the readers of the day
    excluded by inspecting ``metadata`` instead. That was invisible while the
    recent-chat path still filtered on metadata; it stopped being invisible when
    ``list_recent_owner_messages`` moved onto the indexed column, which puts
    pre-0004 progress and A2A messages into the prompt's Recent chat and into
    ``_recent_chat_task_ids``.

    Only ``true -> false`` is reachable: 0004 defaulted every legacy row to
    ``true`` and every row written since computed the value at insert, so no row
    that belongs in chat history is currently marked hidden. Scanning just the
    visible rows is therefore both complete and cheap enough to re-run.
    """

    table_name = "messages"

    #: Rows per scan page. Large enough that a busy installation is a handful of
    #: round trips, small enough that no page materializes a whole table at once.
    page_size = 1000

    def backfill(self) -> int:
        """Hide every legacy progress and A2A row. Returns how many were hidden.

        Paging is by ``message_id`` rather than ``OFFSET`` because the scan
        predicate is the column being written: an offset would skip rows as
        earlier pages drop out of it. Every row a page updates sits behind the
        cursor, so a keyset walk sees each row exactly once.
        """
        conn = op.get_bind()
        dialect = conn.dialect.name
        hidden = 0
        cursor = ""
        while True:
            rows = conn.execute(
                sa.text(
                    f"SELECT message_id, metadata_json FROM {self.table_name} "
                    "WHERE chat_history_visible = :visible AND message_id > :cursor "
                    "ORDER BY message_id LIMIT :page"
                ),
                {"visible": True, "cursor": cursor, "page": self.page_size},
            ).fetchall()
            if not rows:
                break
            cursor = str(rows[-1][0])
            updates = [
                {"message_id": str(message_id), "visible": False}
                for message_id, metadata_json in rows
                if not self._visible(metadata_json, dialect)
            ]
            if updates:
                conn.execute(
                    sa.text(
                        f"UPDATE {self.table_name} SET chat_history_visible = :visible WHERE message_id = :message_id"
                    ),
                    updates,
                )
                hidden += len(updates)
        if hidden:
            log.info("chat history backfill hid %d legacy progress/A2A messages", hidden)
        return hidden

    @classmethod
    def _visible(cls, stored: Any, dialect: str) -> bool:
        """Frozen copy of ``_chat_history_visible``, over one row's stored metadata."""
        metadata = cls._metadata(stored, dialect)
        return not bool(metadata.get("is_progress")) and not _is_a2a_chat_id(metadata.get("chat_id"))

    @staticmethod
    def _metadata(stored: Any, dialect: str) -> dict[str, Any]:
        """Parse one row's metadata the way ``_row_to_message`` parsed it.

        A decryption failure propagates and aborts the migration: a row whose
        metadata cannot be read is a row whose visibility cannot be decided, and
        defaulting it to visible is the exact bug this exists to fix.
        Unparseable *plaintext* is a different case — the reader answered ``{}``
        for it, so the old filter called such a row visible, and leaving it
        visible keeps this backfill a no-op wherever the two agreed.
        """
        from ouroboros.storage.message_field_crypto import decrypt_field

        text = decrypt_field(str(stored or ""), dialect_name=dialect)
        try:
            parsed = json.loads(text or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


def upgrade() -> None:
    MemoryMigration().create_table()
    ScratchpadBlocksMigration().create_table()

    ChatHistoryVisibilityBackfill().backfill()


def downgrade() -> None:
    # The chat-history repair is deliberately not reverted. Its "before" state
    # is a default that was wrong for every legacy row, and the rows written
    # since the upgrade carry values this revision never touched — restoring
    # either would put progress and A2A traffic back into owner chat history.
    # 0004's own downgrade drops the column outright, which is the real undo.
    ScratchpadBlocksMigration().drop_table()
    MemoryMigration().drop_table()
