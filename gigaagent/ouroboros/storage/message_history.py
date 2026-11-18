from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import RowMapping

from ouroboros.storage.file_catalog import MessageAttachments, merge_message_attachments
from ouroboros.storage.message_field_crypto import decrypt_field
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.runtime_serialization import load_json


def message_from_row(row: RowMapping, *, dialect_name: str) -> ConversationMessage:
    content = decrypt_field(row["content"], dialect_name=dialect_name)
    metadata_json = decrypt_field(row["metadata_json"], dialect_name=dialect_name)
    payload_json = decrypt_field(row["payload_json"], dialect_name=dialect_name)
    payload = load_json(payload_json)
    if payload:
        payload.setdefault("metadata", load_json(metadata_json))
        return ConversationMessage.model_validate(
            {**payload, **{key: row[key] for key in ("owner_id", "agent_id", "chat_id", "dedup_key")}}
        )
    return ConversationMessage(
        message_id=row["message_id"],
        dedup_key=row["dedup_key"],
        owner_id=row["owner_id"],
        agent_id=row["agent_id"],
        chat_id=row["chat_id"],
        session_id=row["session_id"],
        source=row["source"],
        source_message_id=row["source_message_id"],
        actor_type=row["actor_type"],
        channel=row["channel"],
        content=content,
        content_ref=row["content_ref"],
        content_hash=row["content_hash"],
        created_at=row["created_at"],
        metadata=load_json(metadata_json),
    )


@dataclass(frozen=True)
class HistoryWindow:
    rows: list[RowMapping]
    dialect_name: str
    more: bool
    attachments: MessageAttachments | None = None
    open_confirmation_id: str | None = None

    def messages(self) -> list[ConversationMessage]:
        messages = [message_from_row(row, dialect_name=self.dialect_name) for row in self.rows]
        return merge_message_attachments(messages, self.attachments) if self.attachments is not None else messages
