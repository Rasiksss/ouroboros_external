"""Re-key A2A history after draining ingress, execution/event queues and jobs.

Stop transport, supervisor/runtime and background writers; keep the message encryption key.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import batched

import sqlalchemy as sa
from alembic import op
from pydantic import JsonValue
from sqlalchemy.engine.row import RowMapping
from sqlalchemy.sql import Executable

from ouroboros.storage.message_field_crypto import decrypt_field, encrypt_field

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)
_PAGE_SIZE = 500
_CHAT_OFFSET = 4000
_CHAT_MODULUS = 2**52
JsonObject = dict[str, JsonValue]


@dataclass(frozen=True)
class _Context:
    owner_id: str
    agent_id: str
    old_chat: str
    task_id: str
    context_id: str

    @property
    def chat_id(self) -> str:
        return _chat_id(self.context_id or self.task_id)


def _chat_id(value: str) -> str:
    return str(-(int(hashlib.sha256(value.encode()).hexdigest()[:16], 16) % _CHAT_MODULUS) - _CHAT_OFFSET)


def _identity(values: list[str]) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(values, ensure_ascii=True, separators=(",", ":"))).hex


def _mapping(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise RuntimeError("0033: stored A2A metadata must be an object")
    return value


def _object(value: str) -> JsonObject:
    return _mapping(json.loads(decrypt_field(value) or "{}"))


def _encoded(value: JsonObject) -> str:
    return encrypt_field(json.dumps(value, ensure_ascii=False))


def _execute_batches(statement: Executable, values: Iterable[JsonObject]) -> None:
    for batch in batched(values, _PAGE_SIZE):
        op.get_bind().execute(statement, list(batch))


def _rows(query: sa.TextClause) -> Iterator[RowMapping]:
    after = ""
    while rows := op.get_bind().execute(query, {"after": after, "limit": _PAGE_SIZE}).mappings().all():
        yield from rows
        after = rows[-1]["cursor"]


_MESSAGES = sa.text(
    "SELECT message_id AS cursor, message_id, owner_id, agent_id, chat_id, actor_type, "
    "source_message_id, dedup_key, metadata_json, payload_json FROM messages WHERE source='a2a' "
    "AND message_id>:after ORDER BY message_id LIMIT :limit"
)
_TASKS = sa.text(
    "SELECT task_id AS cursor, task_id, owner_id, agent_id, chat_id, task_type, correlation_id, "
    "job_id, input_json, metadata_json, payload_json FROM tasks "
    "WHERE source='a2a' AND task_id>:after ORDER BY task_id LIMIT :limit"
)


def _contexts() -> dict[tuple[str, str, str], _Context]:
    protocol = {
        row["task_id"]: _mapping(_object(row["metadata_json"])["a2a_payload"]).get("contextId", "")
        for row in _rows(_TASKS)
        if row["task_type"] == "a2a" and "a2a_payload" in _object(row["metadata_json"])
    }
    contexts: dict[tuple[str, str, str], _Context] = {}
    unresolved: set[tuple[str, str, str]] = set()
    current: set[tuple[str, str, str]] = set()

    def remember(row: RowMapping, task_id: str, correlation_id: JsonValue, context_id: JsonValue) -> None:
        key = (row["owner_id"], row["agent_id"], row["chat_id"])
        if correlation_id:
            current.add(key)
            return
        if not task_id or row["chat_id"] != _chat_id(task_id.strip()):
            return
        if context_id is None:
            context_id = protocol.get(task_id)
        if context_id is None:
            unresolved.add(key)
            return
        if not isinstance(context_id, str):
            raise RuntimeError("0033: stored A2A contextId must be a string")
        context = _Context(*key, task_id, context_id)
        if key in contexts and contexts[key] != context:
            raise RuntimeError("0033: conflicting A2A contexts for one legacy chat")
        contexts[key] = context

    for row in _rows(_TASKS):
        if row["task_type"] != "a2a":
            inputs = _object(row["input_json"])
            transport = _mapping(inputs.get("transport") or {})
            remember(row, row["correlation_id"], transport.get("correlation_id"), transport.get("a2a_context_id"))
    for row in _rows(_MESSAGES):
        if row["actor_type"] == "owner":
            metadata = _object(row["metadata_json"])
            transport = _mapping(metadata.get("transport") or {})
            remember(
                row,
                str(metadata.get("task_id") or ""),
                transport.get("correlation_id"),
                transport.get("a2a_context_id"),
            )
            if not transport.get("correlation_id"):
                unresolved.add((row["owner_id"], row["agent_id"], row["chat_id"]))
    if unresolved - contexts.keys():
        raise RuntimeError("0033: cannot recover contextId for legacy A2A history; restore the missing task metadata")
    if current & contexts.keys():
        raise RuntimeError("0033: legacy and context-scoped A2A records share a chat; resolve their ownership first")
    return contexts


def _create_mapping(contexts: dict[tuple[str, str, str], _Context]) -> sa.Table:
    mapping = op.create_table(
        "_a2a_context_rekey",
        *(sa.Column(column, sa.Text, nullable=False) for column in ("owner_id", "agent_id", "old_chat", "chat_id")),
        sa.PrimaryKeyConstraint("owner_id", "agent_id", "old_chat"),
        prefixes=["TEMPORARY"],
        postgresql_on_commit="DROP",
    )
    _execute_batches(
        mapping.insert(),
        (
            {"owner_id": c.owner_id, "agent_id": c.agent_id, "old_chat": c.old_chat, "chat_id": c.chat_id}
            for c in contexts.values()
        ),
    )
    # Include existing target histories, unless that key itself moves elsewhere in a chain or swap.
    op.get_bind().execute(
        sa.text(
            "INSERT INTO _a2a_context_rekey SELECT DISTINCT target.owner_id, target.agent_id, target.chat_id, target.chat_id "
            "FROM _a2a_context_rekey target WHERE NOT EXISTS (SELECT 1 FROM _a2a_context_rekey source "
            "WHERE (source.owner_id, source.agent_id, source.old_chat)=(target.owner_id, target.agent_id, target.chat_id))"
        )
    )
    op.create_index("_a2a_context_target", mapping.name, ["owner_id", "agent_id", "chat_id"])
    op.get_bind().execute(sa.text("ANALYZE _a2a_context_rekey"))
    return mapping


def _check_idle() -> None:
    attempts = sa.text(
        "SELECT current.task_id AS cursor, current.status, current.detached, current.terminal_published_at, "
        "current.terminal_publish_claim, current.metadata_json, NOT EXISTS (SELECT 1 FROM tasks newer "
        "WHERE newer.owner_id=current.owner_id AND newer.job_id=current.job_id "
        "AND newer.lineage_ordinal>current.lineage_ordinal) AS latest FROM tasks current "
        "JOIN _a2a_context_rekey mapping ON (current.owner_id, current.agent_id, current.chat_id)="
        "(mapping.owner_id, mapping.agent_id, mapping.old_chat) "
        "WHERE current.source='a2a' AND current.task_type<>'a2a' AND current.task_id>:after "
        "ORDER BY current.task_id LIMIT :limit"
    )
    for row in _rows(attempts):
        pending = row["status"] not in ("completed", "cancelled", "failed", "rejected_duplicate", "budget_exceeded")
        if row["detached"] and row["latest"]:
            pending |= not row["terminal_published_at"] or (
                row["status"] == "completed" and not _object(row["metadata_json"]).get("terminal_memory_done")
            )
        if pending or (row["latest"] and row["terminal_publish_claim"]):
            raise RuntimeError("0033: finish A2A tasks, terminal publication and memory work before migrating")
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM tool_confirmations parked JOIN _a2a_context_rekey mapping "
                "ON (parked.owner_id, parked.agent_id, parked.chat_id)=(mapping.owner_id, mapping.agent_id, mapping.old_chat) "
                "WHERE parked.surface_id='a2a' AND (parked.status='pending' OR parked.run_state_json<>'') LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("0033: drain parked A2A tasks before migrating")


def _migrate_summaries() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM context_summaries summary JOIN _a2a_context_rekey mapping "
            "ON (summary.owner_id, summary.agent_id, summary.chat_id)=(mapping.owner_id, mapping.agent_id, mapping.old_chat) "
            "GROUP BY mapping.owner_id, mapping.agent_id, mapping.chat_id HAVING count(*)>1 LIMIT 1"
        )
    ).first():
        raise RuntimeError("0033: multiple A2A summaries need an explicit rebuild plan before merging")
    summaries = sa.text(
        "SELECT summary.summary_id AS cursor, summary.summary_id, summary.source_offsets_ref, summary.payload_json, "
        "mapping.chat_id AS target_chat, anchor.message_id AS anchor_id, EXISTS ("
        "SELECT 1 FROM _a2a_context_rekey peer JOIN messages added "
        "ON (added.owner_id, added.agent_id, added.chat_id)=(peer.owner_id, peer.agent_id, peer.old_chat) "
        "WHERE (peer.owner_id, peer.agent_id, peer.chat_id)=(mapping.owner_id, mapping.agent_id, mapping.chat_id) "
        "AND added.chat_id<>summary.chat_id AND added.chat_history_visible "
        "AND (added.created_at, added.message_id)<=(anchor.created_at, anchor.message_id)) AS overlaps "
        "FROM context_summaries summary JOIN _a2a_context_rekey mapping "
        "ON (summary.owner_id, summary.agent_id, summary.chat_id)=(mapping.owner_id, mapping.agent_id, mapping.old_chat) "
        "LEFT JOIN messages anchor ON (anchor.owner_id, anchor.agent_id, anchor.chat_id, anchor.message_id)="
        "(summary.owner_id, summary.agent_id, summary.chat_id, summary.source_offsets_ref) AND anchor.chat_history_visible "
        "WHERE summary.summary_id>:after ORDER BY summary.summary_id LIMIT :limit"
    )
    updates: list[JsonObject] = []
    for summary in _rows(summaries):
        payload = _object(summary["payload_json"])
        if payload.get("source_offsets_ref", summary["source_offsets_ref"]) != summary["source_offsets_ref"]:
            raise RuntimeError("0033: resolve the inconsistent A2A summary anchor before migrating")
        if summary["anchor_id"] is None:
            raise RuntimeError("0033: restore the missing A2A summary anchor before migrating")
        if summary["overlaps"]:
            raise RuntimeError("0033: overlapping A2A history and summary need an explicit rebuild plan before merging")
        if payload:
            payload["chat_id"] = summary["target_chat"]
        updates.append({"chat": summary["target_chat"], "payload": _encoded(payload), "id": summary["summary_id"]})
    if updates:
        # Free unique chat keys before assigning chains or swaps of the same keys.
        bind.execute(
            sa.text("UPDATE context_summaries SET chat_id=:prefix || summary_id WHERE summary_id=ANY(:ids)"),
            {"prefix": "_a2a_context_0033_", "ids": [row["id"] for row in updates]},
        )
        _execute_batches(
            sa.text("UPDATE context_summaries SET chat_id=:chat, payload_json=:payload WHERE summary_id=:id"), updates
        )


def _move_scope(value: JsonObject, context: _Context) -> None:
    if "chat_id" in value:
        value["chat_id"] = context.chat_id
    for key in ("scope", "handoff_scope"):
        nested = value.get(key)
        if isinstance(nested, dict):
            value[key] = {**nested, "chat_id": context.chat_id}


def _migrate_messages(contexts: dict[tuple[str, str, str], _Context]) -> dict[tuple[str, str, str], str]:
    jobs: dict[tuple[str, str, str], str] = {}
    update = sa.text(
        "UPDATE messages SET chat_id=:chat, dedup_key=:dedup, metadata_json=:metadata, "
        "payload_json=:payload WHERE message_id=:id"
    )
    for rows in batched(_rows(_MESSAGES), _PAGE_SIZE):
        updates: list[JsonObject] = []
        for row in rows:
            context = contexts.get((row["owner_id"], row["agent_id"], row["chat_id"]))
            if context is None:
                continue
            metadata, payload = _object(row["metadata_json"]), _object(row["payload_json"])
            _move_scope(metadata, context)
            if row["actor_type"] == "owner" or str(metadata.get("user_id")) == context.old_chat:
                metadata["user_id"] = context.chat_id
            metadata["transport"] = {
                **_mapping(metadata.get("transport") or {}),
                "a2a_context_id": context.context_id,
                "correlation_id": context.task_id,
            }
            if row["actor_type"] == "owner":
                metadata["task_id"] = context.task_id
            dedup = row["dedup_key"]
            delivery_id = str(row["source_message_id"] or "").strip()
            if delivery_id:
                inner = _identity(
                    [
                        row["actor_type"],
                        "a2a",
                        context.chat_id,
                        str(metadata.get("user_id")),
                        str(metadata.get("sender_session_id") or ""),
                        delivery_id,
                    ]
                )
                dedup = _identity([context.owner_id, context.agent_id, inner])
                if row["actor_type"] == "owner":
                    old_job, new_job = (
                        _identity([context.owner_id, context.agent_id, "a2a", chat, chat, "", delivery_id])
                        for chat in (context.old_chat, context.chat_id)
                    )
                    jobs[(context.owner_id, context.agent_id, old_job)] = new_job
            if payload:
                payload.update(chat_id=context.chat_id, dedup_key=dedup, metadata=metadata)
            updates.append(
                {
                    "id": row["message_id"],
                    "chat": context.chat_id,
                    "dedup": dedup,
                    "metadata": _encoded(metadata),
                    "payload": _encoded(payload),
                },
            )
        _execute_batches(update, updates)
    return jobs


def _migrate_tasks(contexts: dict[tuple[str, str, str], _Context], jobs: dict[tuple[str, str, str], str]) -> None:
    update = sa.text(
        "UPDATE tasks SET chat_id=:chat, job_id=:job, input_json=:inputs, payload_json=:payload WHERE task_id=:id"
    )
    for rows in batched(_rows(_TASKS), _PAGE_SIZE):
        updates: list[JsonObject] = []
        for row in rows:
            context = contexts.get((row["owner_id"], row["agent_id"], row["chat_id"]))
            if context is None or row["task_type"] == "a2a":
                continue
            inputs, payload = _object(row["input_json"]), _object(row["payload_json"])
            job_id = jobs.get((row["owner_id"], row["agent_id"], row["job_id"]), row["job_id"])
            _move_scope(inputs, context)
            inputs["job_id"] = job_id
            inputs["transport"] = {
                **_mapping(inputs.get("transport") or {}),
                "a2a_context_id": context.context_id,
                "correlation_id": context.task_id,
            }
            if payload:
                payload.update(chat_id=context.chat_id, job_id=job_id, input=inputs)
            updates.append(
                {
                    "id": row["task_id"],
                    "chat": context.chat_id,
                    "job": job_id,
                    "inputs": _encoded(inputs),
                    "payload": _encoded(payload),
                },
            )
        _execute_batches(update, updates)


def _migrate_related(mapping: sa.Table, jobs: dict[tuple[str, str, str], str]) -> None:
    bind = op.get_bind()
    for name in ("files", "scratchpad_blocks", "tool_confirmations", "task_episodes", "tool_invocations"):
        table = sa.table(
            name, *(sa.column(column, sa.Text) for column in ("owner_id", "agent_id", "surface_id", "chat_id"))
        )
        statement = (
            table.update()
            .where(
                table.c.owner_id == mapping.c.owner_id,
                table.c.agent_id == mapping.c.agent_id,
                table.c.surface_id == "a2a",
                table.c.chat_id == mapping.c.old_chat,
            )
            .values(chat_id=mapping.c.chat_id)
        )
        bind.execute(statement)
    if jobs:
        keys: list[JsonObject] = [
            {"owner": owner, "agent": agent, "old": old, "new": new} for (owner, agent, old), new in jobs.items()
        ]
        _execute_batches(
            sa.text(
                "UPDATE scratchpad_blocks SET job_id=:new WHERE owner_id=:owner AND agent_id=:agent AND job_id=:old"
            ),
            keys,
        )
        _execute_batches(
            sa.text("UPDATE task_episodes SET task_id=:new WHERE owner_id=:owner AND agent_id=:agent AND task_id=:old"),
            keys,
        )


def upgrade() -> None:
    contexts = _contexts()
    if not contexts:
        return
    mapping = _create_mapping(contexts)
    _check_idle()
    _migrate_summaries()
    jobs = _migrate_messages(contexts)
    _migrate_tasks(contexts, jobs)
    _migrate_related(mapping, jobs)
    log.info("0033 migrated %d A2A chats and %d admission keys", len(contexts), len(jobs))


def downgrade() -> None:
    raise RuntimeError("0033 cannot split merged A2A histories; restore the pre-upgrade backup")
