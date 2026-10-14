"""The conversation becomes a key: ``chat_id`` on the dialogue shapes and on ``schedules``, ``chats``,
no agent kind, and the inbound journal marker — openspec changes ``drain-the-inbound-journal`` and
``key-schedules-by-chat``.

Five parts of one schema step. The first two are inseparable: the ``chats`` backfill reads the very
column the dialogue shapes gain, and a conversation record for a history nothing keys by chat
describes an isolation the rows do not have. There is no state between them a deployment could
usefully stop at.

**The key column.** The backfill reads each message's conversation out of ``metadata_json`` in a
read-only pass that finishes before the first ``ALTER``. A blob that cannot be *decrypted* aborts
the revision against an untouched schema; a plaintext blob that names no conversation, or will not
parse, parks on the empty chat, is retained and is counted — the shape ``0006_core_memory.py``
already uses, and for its reason: the row reader answers ``{}`` for such a blob too. Every existing
``context_summaries`` row is parked unconditionally, so each conversation's first consolidation
after the upgrade re-reads from its own oldest reachable message and pays for it once in tokens.

**The record.** ``chats`` is keyed on ``(owner_id, agent_id, chat_id)`` — the sentence ``agents``
writes one axis up, and the one ``messages`` and ``context_summaries`` are keyed by once the key
column above is filled. Its backfill takes ids only: the two places a title could come from at migration time
are message content, which is ciphertext wherever chat encryption is on, and a model, which a
revision must never call. It reads ``messages.source`` as well, because the registry is the browser
surface's alone — a dialogs peer id or a negative agent-to-agent id imported here is a conversation
the list shows and the rename route refuses, and one the memory tier would then spend model calls
naming.

**The kind.** ``agents`` loses ``agent_type`` and the three constraints that read it. The kind was
two values where one identity was meant: the owner's default agent is the row on the reserved id and
nothing else, so the id already said everything the kind said. What the constraints enforced survives
as the primary key: one row per ``(owner_id, agent_id)`` still means one default agent per owner, on
the id the writer reserves. The table is rebuilt rather than altered in place, because SQLite drops a
column by recreating the table from its own reflection and does not reflect CHECK constraints, so an
in-place drop would leave them behind or lose them silently depending on the dialect. It rides here
rather than in a revision of its own because both halves above already stop the runtimes, and a
deployment that has taken one outage should not take a second for a table rebuild that answers the
same product decision.

The kind's reversal loses nothing: the old constraints made it a function of the id, so
``'CORE_AGENT'`` on ``main`` and ``'SUB_AGENT'`` everywhere else restores exactly what any row that
satisfied them could have held.

**The journal marker.** ``messages.inbound_unprocessed`` materialises what the crash-replay sweep
used to read out of ``metadata_json`` row by row. It rides here for the same reason the kind does,
and it shares the key column's single read: one decrypting pass over the largest table answers both
"which conversation is this" and "did the journal ever hand this row over", so the marker's backfill
costs no second pass. Its predicate is the runtime's, term for term — an owner-authored row on a
named conversation whose metadata says ``inbound_processed`` is false and which is not
``history_only``.

**The schedules.** ``schedules`` gains the same key column, filled from the delivery scope each row
already stores, and ``idx_schedules_owner`` becomes ``idx_schedules_owner_chat`` over
``(owner_id, agent_id, chat_id, state)`` — the narrowed listing's own order of keys. It rides here
because it is the same sentence as the first part: the conversation becomes a key, and a scheduler
whose tool-facing statements still reached pair-wide would hand one participant another's reminders
in a schema that had just separated their messages. Its read follows the same split as the key
column's — a blob that will not *decrypt* aborts the revision, one that decrypts but names no
delivery scope parks its row on the empty chat with no next run and needing attention, and is
counted. Such a row could never have fired: dispatch refuses a row whose stored scope will not read.

**Not safe for a rolling deploy.** An instance on the previous code inserts messages and schedules
with no chat, reads pair-wide across conversations this revision has just separated, and writes rows
no sweep will see. Apply with the runtimes stopped, in the deployment step that already owns schema
upgrades. The downgrade is re-entrant: pysqlite commits DDL outside the transaction, so a refusal
partway leaves the drops before it committed while ``alembic_version`` still reads ``0016``, and
running the same downgrade again is the recovery. A failed *upgrade* has none: the preflight refuses
a schema this revision has partly written, so the exit is a restore or hand-dropping what it names.

Every literal is written out here rather than imported: a revision is history the moment it is
applied, so it carries its own copy of what it needs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import sqlalchemy as sa
from alembic import op

from ouroboros.storage.message_field_crypto import decrypt_field

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_MESSAGES = "messages"
_SUMMARIES = "context_summaries"
_AGENTS = "agents"
_AGENTS_REBUILD = "agents_rebuild"
_AGENT_KIND = "agent_type"
_DEFAULT_AGENT_ID = "main"
_CORE_AGENT = "CORE_AGENT"
_SUB_AGENT = "SUB_AGENT"
_AGENT_COLUMNS = "owner_id, agent_id, name, description, is_active, created_at"
_CHATS = "chats"
_BROWSER = "browser"
#: The spelling `0001` allowed for the same surface; a row carrying it is a browser conversation.
_LEGACY_BROWSER = "web"
_PROVISIONAL = "provisional"
_TITLE_MODEL = "model"
_TITLE_OWNER = "owner"
_CHAT = "chat_id"
_DEDUP = "dedup_key"
_DEDUP_INDEX = "uq_messages_dedup_key"
_PARKED_CHAT_ID = ""
_MARKER = "inbound_unprocessed"
_MARKER_INDEX = "idx_messages_inbound_unprocessed"
_OWNER = "owner"
#: How many rows one read or write round trip carries. Bounds both the blobs held at once
#: and the number of statements, on a table that is the largest one this schema has.
_PAGE = 1000

_SUMMARY_UNIQUE_BEFORE = "uq_context_summaries_owner_agent"
_SUMMARY_UNIQUE_AFTER = "uq_context_summaries_owner_agent_chat"

_SCHEDULES = "schedules"
_PARKED_STATE = "needs_attention"
_SCHEDULE_INDEX_BEFORE = "idx_schedules_owner"
_SCHEDULE_INDEX_AFTER = "idx_schedules_owner_chat"

_VISIBLE_INDEX_BEFORE = "idx_messages_owner_agent_visible_created"
_CREATED_INDEX_BEFORE = "idx_messages_owner_agent_created"
_VISIBLE_INDEX_AFTER = "idx_messages_owner_agent_chat_visible_created"
_CREATED_INDEX_AFTER = "idx_messages_owner_agent_chat_created"


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _preflight() -> None:
    inspector = sa.inspect(op.get_bind())
    present = set(inspector.get_table_names())
    missing = [name for name in (_MESSAGES, _SUMMARIES, _AGENTS, _SCHEDULES) if name not in present]
    if missing:
        raise RuntimeError(f"0016 preflight: expected tables missing from the schema 0015 leaves behind: {missing}")
    for table in (_MESSAGES, _SUMMARIES, _SCHEDULES):
        if _CHAT in _columns(table):
            raise RuntimeError(f"0016 preflight: {table}.{_CHAT} already exists; this revision has partly run")
    schedule_indexes = {index["name"] for index in inspector.get_indexes(_SCHEDULES)}
    if _SCHEDULE_INDEX_BEFORE not in schedule_indexes:
        raise RuntimeError(f"0016 preflight: {_SCHEDULE_INDEX_BEFORE} is absent; this is not an 0015 schema")
    if _SCHEDULE_INDEX_AFTER in schedule_indexes:
        raise RuntimeError(f"0016 preflight: {_SCHEDULE_INDEX_AFTER} already exists; this revision has partly run")
    if _CHATS in present:
        raise RuntimeError(f"0016 preflight: {_CHATS} already exists; this revision has partly run")
    message_columns = _columns(_MESSAGES)
    if _MARKER in message_columns:
        raise RuntimeError(f"0016 preflight: {_MESSAGES}.{_MARKER} already exists; this revision has partly run")
    if _DEDUP not in message_columns:
        raise RuntimeError(f"0016 preflight: {_MESSAGES}.{_DEDUP} is absent; this is not an 0015 schema")
    absent = [
        name
        for name in ("source", "actor_type", "metadata_json", "chat_history_visible")
        if name not in message_columns
    ]
    if absent:
        raise RuntimeError(f"0016 preflight: columns the 0015 schema leaves behind are absent: {absent}")
    if _AGENT_KIND not in _columns(_AGENTS):
        raise RuntimeError(f"0016 preflight: {_AGENTS}.{_AGENT_KIND} is absent; this is not an 0015 schema")
    indexes = {index["name"] for index in inspector.get_indexes(_MESSAGES)}
    if _DEDUP_INDEX not in indexes:
        raise RuntimeError(f"0016 preflight: {_DEDUP_INDEX} is absent; this is not an 0015 schema")
    if _MARKER_INDEX in indexes:
        raise RuntimeError(f"0016 preflight: {_MARKER_INDEX} already exists; this revision has partly run")
    # Checked here rather than where they are dropped: the drop runs after the column and the
    # whole backfill, and on SQLite a failure there leaves a schema whose retry this preflight
    # then refuses, with `alembic_version` still at 0015.
    missing_indexes = [name for name in (_VISIBLE_INDEX_BEFORE, _CREATED_INDEX_BEFORE) if name not in indexes]
    if missing_indexes:
        raise RuntimeError(f"0016 preflight: the message indexes 0008 creates are absent: {missing_indexes}")
    if _SUMMARY_UNIQUE_BEFORE not in {
        constraint["name"] for constraint in inspector.get_unique_constraints(_SUMMARIES)
    }:
        raise RuntimeError(f"0016 preflight: {_SUMMARY_UNIQUE_BEFORE} is absent; this is not an 0015 schema")


def _stored_metadata(stored: Any, dialect: str) -> dict[str, Any]:
    text = decrypt_field(str(stored or ""), dialect_name=dialect).strip()
    try:
        parsed = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stored_chat(metadata: dict[str, Any]) -> str:
    chat = metadata.get(_CHAT)
    return _PARKED_CHAT_ID if chat is None or str(chat) == "" else str(chat)


def _never_handed_over(metadata: dict[str, Any]) -> bool:
    return metadata.get("inbound_processed") is False and not metadata.get("history_only")


def _read_messages() -> tuple[list[dict[str, str]], list[str]]:
    # Paged by the primary key rather than one `fetchall`: the values live inside an encrypted
    # blob, so the read cannot be set-based, but only the derived chat strings and stranded ids
    # are kept — the blobs themselves live for one page. Every page is read before the first
    # ALTER, so a failure while computing leaves a database this revision can still be run
    # against; one pass answers both halves, because both read the same blob.
    bind = op.get_bind()
    dialect = bind.dialect.name
    query = sa.text(
        f"SELECT message_id, actor_type, metadata_json FROM {_MESSAGES} "
        "WHERE message_id > :after ORDER BY message_id LIMIT :page"
    )
    chats: list[dict[str, str]] = []
    stranded: list[str] = []
    after = ""
    while True:
        rows = bind.execute(query, {"after": after, "page": _PAGE}).fetchall()
        if not rows:
            return chats, stranded
        for message_id, actor_type, metadata_json in rows:
            metadata = _stored_metadata(metadata_json, dialect)
            chat = _stored_chat(metadata)
            chats.append({"message_id": str(message_id), _CHAT: chat})
            if str(actor_type) == _OWNER and chat != _PARKED_CHAT_ID and _never_handed_over(metadata):
                stranded.append(str(message_id))
        after = str(rows[-1][0])


def _stored_delivery_chat(payload: Any, dialect: str) -> str:
    stored = decrypt_field(str(payload or ""), dialect_name=dialect).strip()
    try:
        parsed = json.loads(stored or "{}")
    except (TypeError, ValueError):
        return _PARKED_CHAT_ID
    delivery = parsed.get("delivery") if isinstance(parsed, dict) else None
    scope = delivery.get("scope") if isinstance(delivery, dict) else None
    chat = scope.get(_CHAT) if isinstance(scope, dict) else None
    return _PARKED_CHAT_ID if chat is None or str(chat) == "" else str(chat)


def _read_schedules() -> tuple[list[dict[str, str]], list[str]]:
    # The same shape as the message read above, and for the same reasons.
    bind = op.get_bind()
    dialect = bind.dialect.name
    query = sa.text(
        f"SELECT schedule_id, payload_json FROM {_SCHEDULES} "
        "WHERE schedule_id > :after ORDER BY schedule_id LIMIT :page"
    )
    keyed: list[dict[str, str]] = []
    parked: list[str] = []
    after = ""
    while True:
        rows = bind.execute(query, {"after": after, "page": _PAGE}).fetchall()
        if not rows:
            return keyed, parked
        for schedule_id, payload_json in rows:
            chat = _stored_delivery_chat(payload_json, dialect)
            if chat == _PARKED_CHAT_ID:
                parked.append(str(schedule_id))
            else:
                keyed.append({"schedule_id": str(schedule_id), _CHAT: chat})
        after = str(rows[-1][0])


def _write_schedule_chats(keyed: list[dict[str, str]], parked: list[str]) -> None:
    bind = op.get_bind()
    keying = sa.text(f"UPDATE {_SCHEDULES} SET {_CHAT}=:{_CHAT} WHERE schedule_id=:schedule_id")
    for start in range(0, len(keyed), _PAGE):
        bind.execute(keying, keyed[start : start + _PAGE])
    log.info("0016 gave %d schedule row(s) their conversation", len(keyed))

    parking = sa.text(f"UPDATE {_SCHEDULES} SET state=:state, next_run_at=:cleared WHERE schedule_id=:schedule_id")
    for start in range(0, len(parked), _PAGE):
        bind.execute(
            parking,
            [{"state": _PARKED_STATE, "cleared": "", "schedule_id": row} for row in parked[start : start + _PAGE]],
        )
    log.info("0016 parked %d schedule row(s)", len(parked))
    if parked:
        log.warning(
            "0016: parked %d schedule row(s) whose payload names no delivery conversation; each needs "
            "attention and has no next run, and could never have fired",
            len(parked),
        )


def _key_the_schedules(keyed: list[dict[str, str]], parked: list[str]) -> None:
    op.add_column(_SCHEDULES, sa.Column(_CHAT, sa.Text(), nullable=False, server_default=_PARKED_CHAT_ID))
    _write_schedule_chats(keyed, parked)
    op.drop_index(_SCHEDULE_INDEX_BEFORE, table_name=_SCHEDULES)
    op.create_index(_SCHEDULE_INDEX_AFTER, _SCHEDULES, ["owner_id", "agent_id", _CHAT, "state"])


def _unkey_the_schedules() -> None:
    # Guarded like the marker's drop: pysqlite leaves a committed drop behind a later refusal.
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_SCHEDULES)}
    if _SCHEDULE_INDEX_AFTER in indexes:
        op.drop_index(_SCHEDULE_INDEX_AFTER, table_name=_SCHEDULES)
    if _SCHEDULE_INDEX_BEFORE not in indexes:
        op.create_index(_SCHEDULE_INDEX_BEFORE, _SCHEDULES, ["owner_id", "agent_id", "state"])
    if _CHAT in _columns(_SCHEDULES):
        op.drop_column(_SCHEDULES, _CHAT)


def _write_message_chats(chats: list[dict[str, str]]) -> None:
    bind = op.get_bind()
    named = [row for row in chats if row[_CHAT] != _PARKED_CHAT_ID]
    statement = sa.text(f"UPDATE {_MESSAGES} SET {_CHAT}=:{_CHAT} WHERE message_id=:message_id")
    for start in range(0, len(named), _PAGE):
        bind.execute(statement, named[start : start + _PAGE])
    log.info("0016 gave %d message row(s) their conversation", len(named))
    parked = len(chats) - len(named)
    if parked:
        log.warning(
            "0016: parked %d message row(s) whose metadata names no conversation; they are retained, match no "
            "minted scope, and are removed only by the pair-wide erasure",
            parked,
        )


def _write_stranded(stranded: list[str]) -> None:
    bind = op.get_bind()
    statement = sa.text(f"UPDATE {_MESSAGES} SET {_MARKER}=:marker WHERE message_id=:message_id")
    for start in range(0, len(stranded), _PAGE):
        bind.execute(statement, [{"marker": True, "message_id": row} for row in stranded[start : start + _PAGE]])
    log.info("0016 backfilled the inbound journal marker over %d stranded message row(s)", len(stranded))
    if stranded:
        log.warning(
            "0016 marked %d message row(s) the inbound journal never handed over; the next sweep replays them",
            len(stranded),
        )


def upgrade() -> None:
    _preflight()
    chats, stranded = _read_messages()
    keyed_schedules, parked_schedules = _read_schedules()

    op.add_column(_MESSAGES, sa.Column(_CHAT, sa.Text(), nullable=False, server_default=_PARKED_CHAT_ID))
    _write_message_chats(chats)

    op.add_column(_SUMMARIES, sa.Column(_CHAT, sa.Text(), nullable=False, server_default=_PARKED_CHAT_ID))
    summaries = int(op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {_SUMMARIES}")).scalar() or 0)
    if summaries:
        log.warning(
            "0016: parked %d committed summary row(s); each conversation's next consolidation rebuilds its own "
            "from that conversation's oldest reachable message",
            summaries,
        )

    op.drop_index(_VISIBLE_INDEX_BEFORE, table_name=_MESSAGES)
    op.drop_index(_CREATED_INDEX_BEFORE, table_name=_MESSAGES)
    op.create_index(
        _VISIBLE_INDEX_AFTER,
        _MESSAGES,
        ["owner_id", "agent_id", _CHAT, "chat_history_visible", "created_at"],
    )
    op.create_index(_CREATED_INDEX_AFTER, _MESSAGES, ["owner_id", "agent_id", _CHAT, "created_at"])

    with op.batch_alter_table(_SUMMARIES) as batch:
        batch.drop_constraint(_SUMMARY_UNIQUE_BEFORE, type_="unique")
        batch.create_unique_constraint(_SUMMARY_UNIQUE_AFTER, ["owner_id", "agent_id", _CHAT])

    _create_chats()
    _key_the_schedules(keyed_schedules, parked_schedules)
    _drop_the_agent_kind()
    _add_the_journal_marker(stranded)


def _add_the_journal_marker(stranded: list[str]) -> None:
    op.add_column(_MESSAGES, sa.Column(_MARKER, sa.Boolean(), nullable=False, server_default=sa.false()))
    _write_stranded(stranded)
    partial = sa.text(_MARKER)
    op.create_index(
        _MARKER_INDEX,
        _MESSAGES,
        ["owner_id", "agent_id", "source", "created_at"],
        postgresql_where=partial,
        sqlite_where=partial,
    )


def _drop_the_journal_marker() -> None:
    # Guarded: pysqlite commits DDL outside the transaction, so the summary refusal below can
    # leave these drops already committed for the retry to run into.
    inspector = sa.inspect(op.get_bind())
    if _MARKER_INDEX in {index["name"] for index in inspector.get_indexes(_MESSAGES)}:
        op.drop_index(_MARKER_INDEX, table_name=_MESSAGES)
    if _MARKER in _columns(_MESSAGES):
        op.drop_column(_MESSAGES, _MARKER)


def _drop_any_stale_agents_rebuild() -> None:
    # SQLite commits DDL outside the transaction, so a refusal after the rebuild table exists can
    # leave this scratch table behind for the retry to trip on.
    if sa.inspect(op.get_bind()).has_table(_AGENTS_REBUILD):
        op.drop_table(_AGENTS_REBUILD)


def _drop_the_agent_kind() -> None:
    _drop_any_stale_agents_rebuild()
    op.create_table(
        _AGENTS_REBUILD,
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("owner_id", "agent_id"),
    )
    op.execute(sa.text(f"INSERT INTO {_AGENTS_REBUILD} ({_AGENT_COLUMNS}) SELECT {_AGENT_COLUMNS} FROM {_AGENTS}"))
    op.drop_table(_AGENTS)
    op.rename_table(_AGENTS_REBUILD, _AGENTS)


def _restore_the_agent_kind() -> None:
    _drop_any_stale_agents_rebuild()
    op.create_table(
        _AGENTS_REBUILD,
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(_AGENT_KIND, sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("owner_id", "agent_id"),
        sa.CheckConstraint(f"{_AGENT_KIND} IN ('{_CORE_AGENT}', '{_SUB_AGENT}')", name="ck_agents_kind"),
        sa.CheckConstraint(
            f"{_AGENT_KIND} <> '{_CORE_AGENT}' OR agent_id = '{_DEFAULT_AGENT_ID}'",
            name="ck_agents_core_holds_the_reserved_id",
        ),
        sa.CheckConstraint(
            f"agent_id <> '{_DEFAULT_AGENT_ID}' OR {_AGENT_KIND} = '{_CORE_AGENT}'",
            name="ck_agents_reserved_id_is_the_core",
        ),
    )
    op.execute(
        sa.text(
            f"INSERT INTO {_AGENTS_REBUILD} ({_AGENT_COLUMNS}, {_AGENT_KIND}) "
            f"SELECT {_AGENT_COLUMNS}, "
            f"CASE WHEN agent_id = '{_DEFAULT_AGENT_ID}' THEN '{_CORE_AGENT}' ELSE '{_SUB_AGENT}' END "
            f"FROM {_AGENTS}"
        )
    )
    op.drop_table(_AGENTS)
    op.rename_table(_AGENTS_REBUILD, _AGENTS)


def _create_chats() -> None:
    op.create_table(
        _CHATS,
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column(_CHAT, sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("title_source", sa.Text(), nullable=False, server_default=_PROVISIONAL),
        sa.Column("title_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("owner_id", "agent_id", _CHAT),
        sa.CheckConstraint(
            f"title_source IN ('{_PROVISIONAL}', '{_TITLE_MODEL}', '{_TITLE_OWNER}')",
            name="ck_chats_title_source",
        ),
        sa.CheckConstraint(f"{_CHAT} <> ''", name="ck_chats_chat_id_present"),
    )
    # Ids only, so every other column takes the server default declared above. Writing them out
    # would put a boolean literal in the SELECT list, and PostgreSQL has no assignment cast from
    # integer to boolean — a spelling SQLite accepts and the fleet's database rejects.
    recorded = op.get_bind().execute(
        sa.text(
            f"INSERT INTO {_CHATS} (owner_id, agent_id, {_CHAT}) "
            f"SELECT DISTINCT owner_id, agent_id, {_CHAT} FROM {_MESSAGES} "
            f"WHERE {_CHAT} <> :parked AND source IN (:browser, :legacy)"
        ),
        {"parked": _PARKED_CHAT_ID, "browser": _BROWSER, "legacy": _LEGACY_BROWSER},
    )
    log.info("0016 recorded %d browser conversation(s)", recorded.rowcount)


def downgrade() -> None:
    bind = op.get_bind()
    _drop_the_journal_marker()

    # A pair that had a summary before the upgrade holds a parked row, and would hold two the
    # moment one of its conversations commits — refusing every downgrade from then on. The
    # parked row is what this revision's own upgrade made, so unwinding it is not choosing
    # between two real summaries. Only where it actually collides: a pair with nothing else
    # keeps it, and the downgrade puts it back where the old uniqueness expects one row.
    unparked = bind.execute(
        sa.text(
            f"DELETE FROM {_SUMMARIES} WHERE {_CHAT}=:parked AND EXISTS ("
            f"SELECT 1 FROM {_SUMMARIES} AS other WHERE other.owner_id={_SUMMARIES}.owner_id "
            f"AND other.agent_id={_SUMMARIES}.agent_id AND other.{_CHAT}<>:parked)"
        ),
        {"parked": _PARKED_CHAT_ID},
    )
    if unparked.rowcount:
        log.warning("0016 downgrade: removed %d summary row(s) this revision parked", unparked.rowcount)

    collisions = bind.execute(
        sa.text(
            f"SELECT owner_id, agent_id, COUNT(*) FROM {_SUMMARIES} GROUP BY owner_id, agent_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if collisions:
        pairs = ", ".join(f"{owner}/{agent} ({count})" for owner, agent, count in collisions)
        raise RuntimeError(
            "0016 downgrade: these ownership pairs hold more than one committed summary and the pair-wide "
            f"uniqueness cannot readmit them: {pairs}. Delete the rows to lose, in "
            "context_summaries, and run the downgrade again; each conversation rebuilds its summary "
            "from its own retained messages."
        )

    _restore_the_agent_kind()

    with op.batch_alter_table(_SUMMARIES) as batch:
        batch.drop_constraint(_SUMMARY_UNIQUE_AFTER, type_="unique")
        batch.create_unique_constraint(_SUMMARY_UNIQUE_BEFORE, ["owner_id", "agent_id"])
    op.drop_column(_SUMMARIES, _CHAT)

    op.drop_index(_VISIBLE_INDEX_AFTER, table_name=_MESSAGES)
    op.drop_index(_CREATED_INDEX_AFTER, table_name=_MESSAGES)
    op.create_index(_VISIBLE_INDEX_BEFORE, _MESSAGES, ["owner_id", "agent_id", "chat_history_visible", "created_at"])
    op.create_index(_CREATED_INDEX_BEFORE, _MESSAGES, ["owner_id", "agent_id", "created_at"])
    op.drop_column(_MESSAGES, _CHAT)

    _unkey_the_schedules()

    # Last, after the one refusal above has already passed: pysqlite does not roll back DDL, so a
    # table dropped ahead of a refusal is one a retry cannot put back.
    op.drop_table(_CHATS)
