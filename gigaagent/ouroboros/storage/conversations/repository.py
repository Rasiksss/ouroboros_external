import uuid
from contextlib import AbstractContextManager

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from infra.sql_transaction import SqlTransactionManager
from new_core.identity import OwnerScope
from ouroboros.contracts.chat_id_policy import is_internal_chat_id
from ouroboros.storage import task_transitions
from ouroboros.storage.conversations.interfaces import ConversationMetadata, IConversationRepository
from ouroboros.storage.conversations.message_write import (
    _chat_history_visible,
    _identity_metadata,
    append_message_row,
    encode_message,
)
from ouroboros.storage.file_catalog import hydrate_attachments
from ouroboros.storage.message_field_crypto import decrypt_field, encrypt_field
from ouroboros.storage.runtime_backend import MessageAnchorMissingError
from ouroboros.storage.runtime_models import ContextSummary, ConversationMessage
from ouroboros.storage.runtime_rows import message_from_row
from ouroboros.storage.runtime_serialization import dump_json as _dump_json, load_json as _load_json
from ouroboros.utils import utc_now_iso

_OWNED = "owner_id=:owner_id AND agent_id=:agent_id"
_CHATTED = f"{_OWNED} AND chat_id=:chat_id"


def _owned_params(scope: OwnerScope) -> dict[str, str]:
    return {"owner_id": scope.owner_id, "agent_id": scope.agent_id}


def _chat_params(scope: OwnerScope) -> dict[str, str]:
    return {**_owned_params(scope), "chat_id": scope.chat_id}


def _inbound_unprocessed(message: ConversationMessage) -> bool:
    metadata = message.metadata
    return (
        metadata.get("inbound_processed") is False
        and not metadata.get("history_only")
        and message.actor_type == "owner"
        and bool(message.chat_id)
    )


class SqlConversationRepository(IConversationRepository):
    def __init__(self, transaction_manager: SqlTransactionManager) -> None:
        self.transaction_manager = transaction_manager

    @property
    def transaction(self) -> AbstractContextManager[None]:
        return self.transaction_manager.transaction

    def append_message(self, message: ConversationMessage, *, scope: OwnerScope) -> ConversationMessage | None:
        encoded = encode_message(message)
        with self.transaction, self.transaction_manager.connection() as conn:
            return append_message_row(conn, message, scope=scope, encoded=encoded)

    def _append_anchor_clause(
        self,
        clauses: list[str],
        params: dict[str, str | int | bool],
        *,
        after_message_id: str,
        scope: OwnerScope,
    ) -> None:
        with self.transaction_manager.connection() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT created_at, message_id FROM messages
                    WHERE message_id=:message_id
                      AND owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id
                    """
                ),
                {"message_id": str(after_message_id), **_chat_params(scope)},
            ).fetchone()
        if row is None:
            raise MessageAnchorMissingError(after_message_id)
        clauses.append(
            "(created_at > :anchor_created_at OR (created_at = :anchor_created_at AND message_id > :anchor_message_id))"
        )
        params["anchor_created_at"] = str(row._mapping["created_at"])
        params["anchor_message_id"] = str(row._mapping["message_id"])

    def list_messages(
        self,
        *,
        scope: OwnerScope,
        limit: int = 500,
        offset: int = 0,
        actor_type: str | None = None,
        source: str | None = None,
        after_message_id: str | None = None,
        include_progress: bool = True,
        newest_first: bool = False,
    ) -> list[ConversationMessage]:
        clauses: list[str] = ["owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id"]
        params: dict[str, str | int | bool] = {"limit": int(limit), "offset": int(offset), **_chat_params(scope)}
        if not include_progress:
            # Filter before LIMIT so progress cannot crowd out dialogue.
            clauses.append("chat_history_visible = :visible")
            params["visible"] = True
        if actor_type:
            clauses.append("actor_type = :actor_type")
            params["actor_type"] = actor_type
        if source:
            clauses.append("source = :source")
            params["source"] = source
        if after_message_id:
            self._append_anchor_clause(clauses, params, after_message_id=after_message_id, scope=scope)
        where = " AND ".join(clauses)
        order = "DESC" if newest_first else "ASC"
        with self.transaction_manager.connection() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM messages WHERE {where}
                    ORDER BY created_at {order}, message_id {order}
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).fetchall()
            if newest_first:
                rows = list(reversed(rows))
            return hydrate_attachments(conn, [message_from_row(row) for row in rows])

    def list_owner_messages(
        self,
        *,
        scope: OwnerScope,
        limit: int = 10000,
        actor_type: str | None = None,
        source: str | None = None,
        after_message_id: str | None = None,
        include_progress: bool = False,
        newest_first: bool = False,
    ) -> list[ConversationMessage]:
        """Read the owner view, excluding progress produced for remote peers."""
        return self.list_messages(
            scope=scope,
            limit=limit,
            actor_type=actor_type,
            source=source,
            after_message_id=after_message_id,
            include_progress=include_progress and not is_internal_chat_id(scope.chat_id),
            newest_first=newest_first,
        )

    def list_pair_messages(
        self,
        *,
        owner_id: str,
        agent_id: str,
        limit: int = 500,
        actor_type: str | None = None,
        source: str | None = None,
        include_progress: bool = False,
        newest_first: bool = False,
        unprocessed_only: bool = False,
    ) -> list[ConversationMessage]:
        """Every conversation of one ownership pair — the one deliberate cross-chat message read."""
        clauses: list[str] = ["owner_id=:owner_id AND agent_id=:agent_id"]
        params: dict[str, str | int | bool] = {
            "limit": int(limit),
            "owner_id": str(owner_id),
            "agent_id": str(agent_id),
        }
        if not include_progress:
            clauses.append("chat_history_visible = :visible")
            params["visible"] = True
        if actor_type:
            clauses.append("actor_type = :actor_type")
            params["actor_type"] = actor_type
        if source:
            clauses.append("source = :source")
            params["source"] = source
        if unprocessed_only:
            clauses.append("inbound_unprocessed")
            clauses.append("chat_id <> ''")
        where = " AND ".join(clauses)
        order = "DESC" if newest_first else "ASC"
        with self.transaction_manager.connection() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM messages WHERE {where}
                    ORDER BY created_at {order}, message_id {order}
                    LIMIT :limit
                    """
                ),
                params,
            ).fetchall()
            if newest_first:
                rows = list(reversed(rows))
            return hydrate_attachments(conn, [message_from_row(row) for row in rows])

    def list_owner_agent_pairs(
        self, *, limit: int = 1000, start_after: tuple[str, str] | None = None
    ) -> list[tuple[str, str]]:
        """Every owner/agent pair that has said something, for deployment-wide maintenance."""
        params: dict[str, str | int | bool] = {"limit": max(0, int(limit))}
        resume = ""
        if start_after is not None:
            resume = "WHERE owner_id >= :after_owner AND (owner_id > :after_owner OR agent_id > :after_agent)"
            params["after_owner"] = str(start_after[0])
            params["after_agent"] = str(start_after[1])
        with self.transaction_manager.connection() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT DISTINCT owner_id, agent_id FROM messages
                    {resume}
                    ORDER BY owner_id, agent_id
                    LIMIT :limit
                    """
                ),
                params,
            ).fetchall()
        return [(str(row[0] or ""), str(row[1] or "")) for row in rows]

    def list_chat_history_messages(
        self,
        *,
        scope: OwnerScope,
        limit: int = 100,
        offset: int = 0,
        created_from: str | None = None,
        created_to: str | None = None,
        after_message_id: str | None = None,
        through_message_id: str | None = None,
        oldest_first: bool = False,
        message_ids: list[str] | None = None,
    ) -> list[ConversationMessage]:
        clean_limit = max(0, int(limit))
        clauses = ["owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id", "chat_history_visible = :visible"]
        params: dict[str, str | int | bool] = {
            "visible": True,
            "limit": clean_limit,
            "offset": max(0, int(offset)),
            **_chat_params(scope),
        }
        if created_from:
            clauses.append("created_at >= :created_from")
            params["created_from"] = str(created_from)
        if created_to:
            clauses.append("created_at < :created_to")
            params["created_to"] = str(created_to)
        with self.transaction_manager.connection() as conn:
            for anchor_id, key, time_relation, id_relation in (
                (after_message_id, "after", ">", ">"),
                (through_message_id, "through", "<", "<="),
            ):
                if not anchor_id:
                    continue
                anchor = conn.execute(
                    text(
                        f"SELECT created_at, message_id FROM messages WHERE message_id=:anchor AND {_CHATTED} "
                        "AND chat_history_visible=:visible"
                    ),
                    {"anchor": anchor_id, "visible": True, **_chat_params(scope)},
                ).fetchone()
                if anchor is None:
                    raise MessageAnchorMissingError(anchor_id)
                clauses.append(
                    f"(created_at {time_relation} :{key}_created_at OR "
                    f"(created_at = :{key}_created_at AND message_id {id_relation} :{key}_message_id))"
                )
                params[f"{key}_created_at"] = anchor.created_at
                params[f"{key}_message_id"] = anchor.message_id
            if (
                after_message_id
                and through_message_id
                and (params["after_created_at"], params["after_message_id"])
                > (params["through_created_at"], params["through_message_id"])
            ):
                raise MessageAnchorMissingError("reversed history interval")
            if message_ids is not None:
                clauses.append(
                    "message_id IN (" + ", ".join(f":frozen_{i}" for i in range(len(message_ids))) + ")"
                    if message_ids
                    else "1=0"
                )
                params.update({f"frozen_{i}": value for i, value in enumerate(message_ids)})
            where = " AND ".join(clauses)
            order = "ASC" if oldest_first else "DESC"
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM messages
                    WHERE {where}
                    ORDER BY created_at {order}, message_id {order}
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).fetchall()
            return hydrate_attachments(conn, [message_from_row(row) for row in rows])

    def get_message(self, message_id: str) -> ConversationMessage | None:
        """A row by its surrogate primary key, and therefore across every owner and chat."""
        with self.transaction_manager.connection() as conn:
            row = conn.execute(
                text("SELECT * FROM messages WHERE message_id=:message_id"),
                {"message_id": str(message_id)},
            ).fetchone()
            return hydrate_attachments(conn, [message_from_row(row)])[0] if row else None

    def get_message_by_dedup_key(self, dedup_key: str, *, scope: OwnerScope) -> ConversationMessage | None:
        """One conversation's row for that identity; the predicate is carried, not assumed."""
        with self.transaction_manager.connection() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM messages
                    WHERE dedup_key=:dedup_key
                      AND owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id
                    """
                ),
                {"dedup_key": str(dedup_key), **_chat_params(scope)},
            ).fetchone()
            return hydrate_attachments(conn, [message_from_row(row)])[0] if row else None

    def claim_message_handoff(self, message_id: str, *, scope: OwnerScope, claim: str) -> ConversationMessage | None:
        """Serialize senders on the history row; expired ambiguous handoffs retain its identity."""
        with self.transaction, self.transaction_manager.connection() as conn:
            row = conn.execute(
                text(f"SELECT * FROM messages WHERE message_id=:id AND {_OWNED} FOR UPDATE"),
                {"id": message_id, **_owned_params(scope)},
            ).fetchone()
            if row is None:
                return None
            message = message_from_row(row)
            now = float(conn.execute(text("SELECT EXTRACT(EPOCH FROM clock_timestamp())")).scalar_one())
            if message.metadata.get("handoff_accepted"):
                return message
            if float(message.metadata.get("handoff_until") or 0) > now:
                return None
            message.metadata.update(handoff_claim=claim, handoff_until=now + 60)
            conn.execute(
                text("UPDATE messages SET metadata_json=:metadata, payload_json=:payload WHERE message_id=:id"),
                {
                    "id": message_id,
                    "metadata": encrypt_field(_dump_json(message.metadata)),
                    "payload": encrypt_field(_dump_json(message.model_dump(mode="json"))),
                },
            )
            return message

    def message_handoff_remaining(self, message_id: str, *, scope: OwnerScope, claim: str) -> float:
        with self.transaction_manager.connection() as conn:
            row = conn.execute(
                text(f"SELECT * FROM messages WHERE message_id=:id AND {_OWNED}"),
                {"id": message_id, **_owned_params(scope)},
            ).fetchone()
            if row is None:
                return 0.0
            metadata = message_from_row(row).metadata
            if not claim or metadata.get("handoff_claim") != claim or metadata.get("handoff_accepted"):
                return 0.0
            return max(0.0, float(metadata.get("handoff_until") or 0) - task_transitions.database_now(conn).timestamp())

    def list_message_handoffs(self, *, after_id: str, limit: int) -> list[ConversationMessage]:
        with self.transaction_manager.connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM messages WHERE (actor_type='agent' OR "
                    "(actor_type='owner' AND inbound_unprocessed)) AND message_id>:after "
                    "ORDER BY message_id LIMIT :limit"
                ),
                {"after": after_id, "limit": max(1, min(limit, 100))},
            ).fetchall()
            return hydrate_attachments(conn, [message_from_row(row) for row in rows])

    def update_message_metadata(
        self, message_id: str, metadata: ConversationMetadata, *, expected_metadata: ConversationMetadata | None = None
    ) -> bool:
        """Replace metadata; a handoff confirmation must still own the exact claimed snapshot."""
        with self.transaction, self.transaction_manager.connection() as conn:
            row = conn.execute(text("SELECT * FROM messages WHERE message_id=:id"), {"id": message_id}).fetchone()
            if row is None:
                return False
            message = message_from_row(row)
            if expected_metadata is not None and _identity_metadata(message.metadata) != _identity_metadata(
                expected_metadata
            ):
                return False
            message.metadata = _identity_metadata(metadata)
            result = conn.execute(
                text(
                    "UPDATE messages SET metadata_json=:metadata, payload_json=:payload, "
                    "chat_history_visible=:visible, inbound_unprocessed=:unprocessed WHERE message_id=:id "
                    "AND (NOT :conditional OR metadata_json=:seen)"
                ),
                {
                    "id": message_id,
                    "conditional": expected_metadata is not None,
                    "seen": row._mapping["metadata_json"],
                    "metadata": encrypt_field(_dump_json(message.metadata)),
                    "payload": encrypt_field(_dump_json(message.model_dump(mode="json"))),
                    "visible": _chat_history_visible(message.metadata),
                    "unprocessed": _inbound_unprocessed(message),
                },
            )
        return bool(result.rowcount)

    def count_messages(
        self,
        *,
        scope: OwnerScope,
        after_message_id: str | None = None,
    ) -> int:
        return len(
            self.list_messages(
                scope=scope,
                limit=1_000_000,
                after_message_id=after_message_id,
            )
        )

    def delete_message(self, message_id: str) -> bool:
        """One row by its surrogate primary key. Cross-owner for ``get_message``'s reason."""
        with self.transaction, self.transaction_manager.connection() as conn:
            result = conn.execute(
                text("DELETE FROM messages WHERE message_id=:message_id"),
                {"message_id": str(message_id)},
            )
        return bool(result.rowcount)

    def upsert_context_summary(self, summary: ContextSummary) -> ContextSummary:
        """One statement: an upsert on the owner, the agent and the chat, from values the caller holds."""
        now = utc_now_iso()
        if not summary.created_at:
            summary.created_at = now
        if not summary.summary_id:
            summary.summary_id = uuid.uuid4().hex
        payload = summary.model_dump(mode="json")
        with self.transaction, self.transaction_manager.connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO context_summaries (
                        summary_id, owner_id, agent_id, chat_id, source_offsets_ref, summary_ref,
                        token_budget, summary_hash, created_at, metadata_json, payload_json
                    ) VALUES (
                        :summary_id, :owner_id, :agent_id, :chat_id, :source_offsets_ref, :summary_ref,
                        :token_budget, :summary_hash, :created_at, :metadata_json, :payload_json
                    )
                    ON CONFLICT(owner_id, agent_id, chat_id) DO UPDATE SET
                        source_offsets_ref=excluded.source_offsets_ref,
                        summary_ref=excluded.summary_ref,
                        token_budget=excluded.token_budget,
                        summary_hash=excluded.summary_hash,
                        created_at=excluded.created_at,
                        metadata_json=excluded.metadata_json,
                        payload_json=excluded.payload_json
                    """
                ),
                {
                    "summary_id": summary.summary_id,
                    "owner_id": summary.owner_id,
                    "agent_id": summary.agent_id,
                    "chat_id": summary.chat_id,
                    "source_offsets_ref": summary.source_offsets_ref,
                    "summary_ref": encrypt_field(summary.summary_ref),
                    "token_budget": int(summary.token_budget),
                    "summary_hash": summary.summary_hash,
                    "created_at": summary.created_at,
                    "metadata_json": encrypt_field(_dump_json(summary.metadata)),
                    "payload_json": encrypt_field(_dump_json(payload)),
                },
            )
        return summary

    def get_latest_context_summary(self, scope: OwnerScope) -> ContextSummary | None:
        with self.transaction_manager.connection() as conn:
            return self._read_latest_context_summary(conn, scope)

    def _read_latest_context_summary(self, conn: Connection, scope: OwnerScope) -> ContextSummary | None:
        row = conn.execute(
            text(
                f"""
                SELECT * FROM context_summaries
                WHERE {_CHATTED}
                ORDER BY created_at DESC, summary_id DESC
                LIMIT 1
                """
            ),
            _chat_params(scope),
        ).fetchone()
        return self._row_to_context_summary(row) if row else None

    def _row_to_context_summary(self, row: Row[tuple[object, ...]]) -> ContextSummary:
        mapping = row._mapping
        payload = _load_json(decrypt_field(mapping["payload_json"]))
        if isinstance(payload, dict) and payload:
            payload.setdefault("metadata", _load_json(decrypt_field(mapping["metadata_json"])))
            payload.pop("conversation_id", None)
            payload.update({axis: mapping[axis] for axis in ("owner_id", "agent_id", "chat_id")})
            return ContextSummary.model_validate(payload)
        return ContextSummary(
            summary_id=mapping["summary_id"],
            owner_id=mapping["owner_id"],
            agent_id=mapping["agent_id"],
            chat_id=mapping["chat_id"],
            source_offsets_ref=mapping["source_offsets_ref"],
            summary_ref=decrypt_field(mapping["summary_ref"]),
            token_budget=int(mapping["token_budget"] or 0),
            summary_hash=mapping["summary_hash"],
            created_at=mapping["created_at"],
            metadata=_load_json(decrypt_field(mapping["metadata_json"])),
        )
