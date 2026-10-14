"""Rows are keyed by ownership axes, not by a glued conversation key.

Two claims, one revision, because they are one sentence: identity in this database is
``OwnerScope``.

Core memory and scratchpad blocks were keyed by ``owner_id`` alone, which was sound only
while one owner meant one agent. They take ``agent_id`` as the second half of their key: a
name identifies one memory *per agent*, and two agents of one owner keep separate
scratchpads. Surface and chat deliberately stay out there — memory belongs to an agent, not
to a conversation.

The five runtime tables were keyed by ``conversation_id``, a denormalized
``f"{surface}:{chat}"`` string with no owner axis and no agent axis. ``messages``,
``context_summaries`` and ``tasks`` take the ownership pair; ``task_episodes`` and
``tool_invocations`` take all four axes, because their read is chat-narrow. The
``conversations`` table, which never had a reader, is dropped. ``context_summaries``
additionally stops being one row under a constant id: uniqueness moves onto
``(owner_id, agent_id)`` so a second agent can commit at all. Rows that outlive the upgrade
are carried over rather than reset: episodes and invocations split their glued key into
axes, and a task still waiting to run is given the scope its claim now reads.

This revision is rewritten rather than followed by an ``0009``: it has not been applied in
any environment, so the head stays ``0008`` and the claim stays one revision. Any database
already stamped ``0008`` from the older lineage will never run this — alembic sees head and
does nothing — and must be recreated. The preflight below is what turns that into a loud
failure rather than a half-applied schema.

Every literal is written out here rather than imported: a revision is history the moment it
is applied, so it carries its own copy of what it needs.
"""

from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_DEFAULT_OWNER_ID = "global"
_DEFAULT_AGENT_ID = "main"
#: Where a legacy key that names no surface is parked. No minted scope can carry an empty
#: chat, so a parked row matches no read — exactly as unreachable as it already was.
_PARKED_SURFACE_ID = "skill-bridge"
_PARKED_CHAT_ID = ""

#: Every spelling a stored key's left part may use, including the read-only legacy ones.
_KNOWN_SURFACES = {
    "dialogs": "dialogs",
    "browser": "browser",
    "a2a": "a2a",
    "skill-bridge": "skill-bridge",
    "sberchat": "dialogs",
    "web": "browser",
    "internal": "skill-bridge",
    "unknown": "skill-bridge",
}

#: The prefix the agent wrote when it held a chat but not the transport behind it. Every
#: episode and invocation key is ``f"chat:{chat_id}"``, so reading it is what keeps working
#: memory across the upgrade instead of parking all of it.
_KEYLESS_SURFACE = "chat"

#: Sources that name no transport, so the chat id says more than they do.
_UNNAMED_SOURCES = {"", "unknown", "system"}

#: The chat-id windows in force while those keys were written: the allocators never
#: overlapped, so an id that names no surface still belongs to exactly one.
_A2A_CHAT_ID_CEILING = -4000
_SKILL_BRIDGE_CHAT_ID_MIN = -3999
_SKILL_BRIDGE_CHAT_ID_MAX = -1
_WEB_UI_CHAT_ID = 1

_MEMORY = "memory"
_MEMORY_INDEX = "idx_memory_owner_status_updated"
_MEMORY_UNIQUE_BEFORE = "uq_memory_owner_name"
_MEMORY_UNIQUE_AFTER = "uq_memory_owner_agent_name"

_SCRATCHPAD = "scratchpad_blocks"
_SCRATCHPAD_INDEX = "idx_scratchpad_blocks_owner_status_id"

_SUMMARY_UNIQUE = "uq_context_summaries_owner_agent"

#: What ``0007`` leaves behind. Checked read-only before any DDL: a schema from another
#: lineage half-migrated is worse than one that refused to migrate.
_EXPECTED_TABLES = (
    "sessions",
    "conversations",
    "messages",
    "tasks",
    "context_summaries",
    "task_episodes",
    "tool_invocations",
    "memory",
    "scratchpad_blocks",
    "user_settings",
)
_EXPECTED_CONVERSATION_KEYED = ("messages", "tasks", "context_summaries", "task_episodes", "tool_invocations")


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _preflight() -> None:
    """Refuse a schema that is not the one ``0007`` produces."""
    inspector = sa.inspect(op.get_bind())
    present = set(inspector.get_table_names())
    missing = [name for name in _EXPECTED_TABLES if name not in present]
    if missing:
        raise RuntimeError(f"0008 preflight: expected tables missing from the schema 0007 leaves behind: {missing}")
    for table in _EXPECTED_CONVERSATION_KEYED:
        columns = _columns(table)
        if "conversation_id" not in columns:
            raise RuntimeError(f"0008 preflight: {table}.conversation_id is absent; this is not an 0007 schema")
        if "owner_id" in columns:
            raise RuntimeError(f"0008 preflight: {table}.owner_id already exists; this revision has partly run")
    if "agent_id" in _columns(_MEMORY):
        raise RuntimeError("0008 preflight: memory.agent_id already exists; this revision has partly run")
    # Every pre-rekey summary lands on the same owner and agent, so more than one row is the
    # collision the new constraint would refuse. Checked before the first ALTER: pysqlite does
    # not roll back DDL, and aborting mid-way leaves a schema no retry can get past.
    summaries = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM context_summaries")).scalar()
    if int(summaries or 0) > 1:
        raise RuntimeError(
            "0008 preflight: context_summaries holds several rows; the committed summary cannot be made "
            "one row per agent without choosing which to lose"
        )


def _add_owner_axes(table: str) -> None:
    op.add_column(table, sa.Column("owner_id", sa.Text(), nullable=False, server_default=_DEFAULT_OWNER_ID))
    op.add_column(table, sa.Column("agent_id", sa.Text(), nullable=False, server_default=_DEFAULT_AGENT_ID))


def _add_scope_axes(table: str) -> None:
    _add_owner_axes(table)
    op.add_column(table, sa.Column("surface_id", sa.Text(), nullable=False, server_default=_PARKED_SURFACE_ID))
    op.add_column(table, sa.Column("chat_id", sa.Text(), nullable=False, server_default=_PARKED_CHAT_ID))


def _surface_for_chat_id(chat: str) -> str:
    """The surface a chat id belonged to, for a key whose prefix names none."""
    try:
        value = int(chat)
    except ValueError:
        return "dialogs"
    if value <= _A2A_CHAT_ID_CEILING:
        return "a2a"
    if _SKILL_BRIDGE_CHAT_ID_MIN <= value <= _SKILL_BRIDGE_CHAT_ID_MAX:
        return "skill-bridge"
    if value == _WEB_UI_CHAT_ID:
        return "browser"
    return "dialogs"


def _split_legacy_key(value: str) -> tuple[str, str] | None:
    """``surface:chat`` as axes, or ``None`` when the left part names no known surface."""
    head, separator, tail = str(value or "").partition(":")
    if not separator or not tail:
        return None
    prefix = head.strip().lower()
    if prefix == _KEYLESS_SURFACE:
        return _surface_for_chat_id(tail), tail
    surface = _KNOWN_SURFACES.get(prefix)
    if surface is None:
        return None
    return surface, tail


def _backfill_scope_axes(table: str, id_column: str) -> None:
    """Split the stored key into axes; park what does not split, and never delete a row."""
    conn = op.get_bind()
    rows = conn.execute(sa.text(f"SELECT {id_column}, conversation_id FROM {table}")).fetchall()
    parked = 0
    for row in rows:
        axes = _split_legacy_key(str(row._mapping["conversation_id"] or ""))
        if axes is None:
            parked += 1
            continue
        surface, chat = axes
        conn.execute(
            sa.text(f"UPDATE {table} SET surface_id=:surface_id, chat_id=:chat_id WHERE {id_column}=:row_id"),
            {"surface_id": surface, "chat_id": chat, "row_id": str(row._mapping[id_column])},
        )
    if parked:
        log.warning(
            "0008: parked %d %s row(s) whose stored key names no known surface; they match no scope and expire "
            "on their own retention",
            parked,
            table,
        )


def _rekey_messages() -> None:
    _add_owner_axes("messages")
    op.drop_index("idx_messages_conversation_created", table_name="messages")
    # The owner view and the history read always filter visibility; the progress-inclusive
    # read does not, and an index leading with visibility cannot serve it without a sort.
    op.drop_index("idx_messages_chat_history_visible_created", table_name="messages")
    op.create_index(
        "idx_messages_owner_agent_visible_created",
        "messages",
        ["owner_id", "agent_id", "chat_history_visible", "created_at"],
    )
    op.create_index("idx_messages_owner_agent_created", "messages", ["owner_id", "agent_id", "created_at"])
    op.drop_column("messages", "conversation_id")


def _rekey_context_summaries() -> None:
    _add_owner_axes("context_summaries")
    with op.batch_alter_table("context_summaries") as batch:
        batch.create_unique_constraint(_SUMMARY_UNIQUE, ["owner_id", "agent_id"])
    op.drop_index("idx_context_summaries_conversation", table_name="context_summaries")
    op.drop_column("context_summaries", "conversation_id")


#: Statuses a task can still be claimed from. A terminal row is never handed to an agent,
#: so the scope it would have run under no longer matters.
_CLAIMABLE_STATUSES = ("requested", "scheduled", "queued", "running", "waiting_confirmation", "interrupted")


def _task_scope_axes(*, conversation_id: str, source: str, chat_id: str) -> dict[str, str] | None:
    """The scope a stored task would be claimed under, or ``None`` when it has no chat."""
    legacy = _split_legacy_key(conversation_id)
    if legacy:
        surface, chat = legacy
    else:
        chat = chat_id
        if not chat:
            return None
        named = source.strip().lower()
        surface = (_KNOWN_SURFACES.get(named) if named not in _UNNAMED_SOURCES else None) or _surface_for_chat_id(chat)
    return {
        "owner_id": _DEFAULT_OWNER_ID,
        "agent_id": _DEFAULT_AGENT_ID,
        "surface_id": surface,
        "chat_id": chat,
    }


def _backfill_task_scopes() -> None:
    """Give every still-claimable task the scope its claim now requires.

    Identity is read from ``input["scope"]`` and from nothing else, and a row queued before
    the upgrade has no such key: without this it is claimed and then dies, on every attempt.
    Both blobs are written, since the reader prefers the payload and falls back to the input.
    A row nothing can be derived for is failed rather than left to crash a worker.
    """
    conn = op.get_bind()
    statuses = ", ".join(f"'{status}'" for status in _CLAIMABLE_STATUSES)
    rows = conn.execute(
        sa.text(
            "SELECT task_id, source, chat_id, conversation_id, input_json, payload_json "
            f"FROM tasks WHERE status IN ({statuses})"
        )
    ).fetchall()
    unreadable = 0
    stranded: list[str] = []
    backfilled = 0
    for row in rows:
        mapping = row._mapping
        task_id = str(mapping["task_id"])
        try:
            stored_input = json.loads(mapping["input_json"] or "{}")
            stored_payload = json.loads(mapping["payload_json"] or "{}")
        except (TypeError, ValueError):
            # A ``context_consolidation`` row keeps both blobs encrypted. The consolidation
            # lease recreates its own work, so there is nothing here to rescue.
            unreadable += 1
            continue
        if not isinstance(stored_input, dict) or not isinstance(stored_payload, dict):
            unreadable += 1
            continue
        if stored_input.get("scope"):
            continue
        axes = _task_scope_axes(
            conversation_id=str(mapping["conversation_id"] or ""),
            source=str(mapping["source"] or ""),
            chat_id=str(mapping["chat_id"] or ""),
        )
        if axes is None:
            stranded.append(task_id)
            continue
        stored_input["scope"] = axes
        payload_input = stored_payload.get("input")
        if isinstance(payload_input, dict):
            payload_input["scope"] = axes
        conn.execute(
            sa.text("UPDATE tasks SET input_json=:input_json, payload_json=:payload_json WHERE task_id=:task_id"),
            {
                "input_json": json.dumps(stored_input, ensure_ascii=False),
                "payload_json": json.dumps(stored_payload, ensure_ascii=False),
                "task_id": task_id,
            },
        )
        backfilled += 1
    if stranded:
        conn.execute(
            sa.text(
                "UPDATE tasks SET status='failed', error_code='owner_scope_rekey', "
                "result='Task predates the ownership rekey and names no chat to run under.' "
                "WHERE task_id IN :task_ids"
            ).bindparams(sa.bindparam("task_ids", expanding=True)),
            {"task_ids": stranded},
        )
    if backfilled or unreadable or stranded:
        log.warning(
            "0008: gave %d pending task(s) their scope; %d unreadable, %d failed for want of a chat",
            backfilled,
            unreadable,
            len(stranded),
        )


def _rekey_tasks() -> None:
    _add_owner_axes("tasks")
    _backfill_task_scopes()
    # Nothing reads tasks by owner, so the dropped index gets no replacement.
    op.drop_index("idx_tasks_conversation", table_name="tasks")
    op.drop_column("tasks", "conversation_id")


def _rekey_task_episodes() -> None:
    _add_scope_axes("task_episodes")
    _backfill_scope_axes("task_episodes", "episode_id")
    op.drop_index("idx_task_episodes_conversation", table_name="task_episodes")
    op.create_index(
        "idx_task_episodes_scope_created",
        "task_episodes",
        ["owner_id", "agent_id", "surface_id", "chat_id", "created_at"],
    )
    op.drop_column("task_episodes", "conversation_id")


def _rekey_tool_invocations() -> None:
    _add_scope_axes("tool_invocations")
    _backfill_scope_axes("tool_invocations", "tool_invocation_id")
    # No replacement: the only read is by task, and ``idx_tool_invocations_task`` serves it.
    op.drop_index("idx_tool_invocations_conversation", table_name="tool_invocations")
    op.drop_column("tool_invocations", "conversation_id")


def _agent_scope_core_memory() -> None:
    op.add_column(_MEMORY, sa.Column("agent_id", sa.Text(), nullable=False, server_default=_DEFAULT_AGENT_ID))
    # SQLite cannot ALTER a table-level constraint, so this rebuilds the table there and
    # issues plain ALTERs on PostgreSQL. One code path, both dialects.
    with op.batch_alter_table(_MEMORY) as batch:
        batch.drop_constraint(_MEMORY_UNIQUE_BEFORE, type_="unique")
        batch.create_unique_constraint(_MEMORY_UNIQUE_AFTER, ["owner_id", "agent_id", "name"])
    # Strictly after the rebuild. SQLite's index reflection does not carry a per-column
    # sort direction, so an index recreated by the batch block comes back without its
    # ``DESC`` — the listing then pays a temp b-tree for its ORDER BY on every prompt
    # build. Recreating it here is what keeps the descending key.
    op.drop_index(_MEMORY_INDEX, table_name=_MEMORY)
    op.create_index(
        _MEMORY_INDEX,
        _MEMORY,
        ["owner_id", "agent_id", "status", sa.text("updated_at DESC"), "name"],
    )

    op.add_column(_SCRATCHPAD, sa.Column("agent_id", sa.Text(), nullable=False, server_default=_DEFAULT_AGENT_ID))
    op.drop_index(_SCRATCHPAD_INDEX, table_name=_SCRATCHPAD)
    op.create_index(_SCRATCHPAD_INDEX, _SCRATCHPAD, ["owner_id", "agent_id", "status", "id"])


def upgrade() -> None:
    _preflight()
    _rekey_messages()
    _rekey_context_summaries()
    _rekey_tasks()
    _rekey_task_episodes()
    _rekey_tool_invocations()
    op.drop_table("conversations")
    _agent_scope_core_memory()


def downgrade() -> None:
    """Restore the columns and re-glue what can be re-glued.

    What cannot be restored: the ``conversations`` table comes back empty, because its rows
    were never referenced and nothing records what they held. ``messages``, ``tasks`` and
    ``context_summaries`` get an empty ``conversation_id``, because the axes they kept do not
    contain the surface or the chat the old key was glued from. Episodes and invocations do
    re-glue, except for rows that were parked, which come back with an empty key — which is
    what an unparseable key was worth in the first place.

    Narrowing the core-memory key can genuinely fail: two agents of one owner may hold the
    same memory name, and there is no correct way to pick a survivor. It is left to raise —
    a merge this revision invented would lose a memory silently.
    """
    with op.batch_alter_table(_MEMORY) as batch:
        batch.drop_constraint(_MEMORY_UNIQUE_AFTER, type_="unique")
        batch.create_unique_constraint(_MEMORY_UNIQUE_BEFORE, ["owner_id", "name"])
    op.drop_index(_MEMORY_INDEX, table_name=_MEMORY)
    op.create_index(
        _MEMORY_INDEX,
        _MEMORY,
        ["owner_id", "status", sa.text("updated_at DESC"), "name"],
    )
    op.drop_column(_MEMORY, "agent_id")

    op.drop_index(_SCRATCHPAD_INDEX, table_name=_SCRATCHPAD)
    op.create_index(_SCRATCHPAD_INDEX, _SCRATCHPAD, ["owner_id", "status", "id"])
    op.drop_column(_SCRATCHPAD, "agent_id")

    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.Text(), primary_key=True),
        sa.Column("session_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("message_log_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("retention_policy", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )

    _restore_scope_keyed("tool_invocations")
    op.create_index("idx_tool_invocations_conversation", "tool_invocations", ["conversation_id", "created_at"])

    # Strictly before the columns go: SQLite refuses to drop a column an index still names.
    op.drop_index("idx_task_episodes_scope_created", table_name="task_episodes")
    _restore_scope_keyed("task_episodes")
    op.create_index("idx_task_episodes_conversation", "task_episodes", ["conversation_id", "created_at"])

    _restore_owner_keyed("tasks")
    op.create_index("idx_tasks_conversation", "tasks", ["conversation_id", "created_at"])

    with op.batch_alter_table("context_summaries") as batch:
        batch.drop_constraint(_SUMMARY_UNIQUE, type_="unique")
    _restore_owner_keyed("context_summaries")
    op.create_index("idx_context_summaries_conversation", "context_summaries", ["conversation_id", "created_at"])

    op.drop_index("idx_messages_owner_agent_created", table_name="messages")
    op.drop_index("idx_messages_owner_agent_visible_created", table_name="messages")
    _restore_owner_keyed("messages")
    op.create_index("idx_messages_conversation_created", "messages", ["conversation_id", "created_at"])
    op.create_index("idx_messages_chat_history_visible_created", "messages", ["chat_history_visible", "created_at"])


def _restore_owner_keyed(table: str) -> None:
    op.add_column(table, sa.Column("conversation_id", sa.Text(), nullable=False, server_default=""))
    op.drop_column(table, "agent_id")
    op.drop_column(table, "owner_id")


def _restore_scope_keyed(table: str) -> None:
    op.add_column(table, sa.Column("conversation_id", sa.Text(), nullable=False, server_default=""))
    op.execute(
        sa.text(
            f"UPDATE {table} SET conversation_id = surface_id || ':' || chat_id WHERE chat_id != '{_PARKED_CHAT_ID}'"
        )
    )
    op.drop_column(table, "chat_id")
    op.drop_column(table, "surface_id")
    op.drop_column(table, "agent_id")
    op.drop_column(table, "owner_id")
