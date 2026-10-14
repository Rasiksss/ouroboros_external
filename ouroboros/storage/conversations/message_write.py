import uuid
from dataclasses import dataclass

import anyio
from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from new_core.identity import OwnerScope
from ouroboros.contracts.attachments import ATTACHMENT_IDS_ADAPTER
from ouroboros.storage.dialect import insert_message_sql
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.file_models import attachment_metadata
from ouroboros.storage.message_field_crypto import encrypt_field
from ouroboros.storage.message_history import message_from_row
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.runtime_serialization import dump_json as _dump_json
from ouroboros.utils import utc_now_iso


def _chat_history_visible(metadata: dict[str, object]) -> bool:
    return not (metadata.get("is_progress") or metadata.get("delivery_only"))


def _inbound_unprocessed(message: ConversationMessage) -> bool:
    metadata = message.metadata
    return (
        metadata.get("inbound_processed") is False
        and not metadata.get("history_only")
        and message.actor_type == "owner"
        and bool(message.chat_id)
    )


_MESSAGE_BY_ID = text("SELECT * FROM messages WHERE message_id=:id")
_MESSAGE_PROCESSED = text(
    "UPDATE messages SET metadata_json=:metadata, payload_json=:payload, inbound_unprocessed=false WHERE message_id=:id"
)


def _processed_message_values(row: RowMapping, dialect_name: str) -> dict[str, str]:
    message = message_from_row(row, dialect_name=dialect_name)
    message.metadata["inbound_processed"] = True
    return {
        "id": message.message_id,
        "metadata": encrypt_field(_dump_json(message.metadata)),
        "payload": encrypt_field(_dump_json(message.model_dump(mode="json"))),
    }


def mark_message_processed(conn: Connection, message_id: str) -> None:
    row = conn.execute(_MESSAGE_BY_ID, {"id": message_id}).mappings().one()
    conn.execute(_MESSAGE_PROCESSED, _processed_message_values(row, conn.dialect.name))


async def amark_message_processed(conn: AsyncConnection, message_id: str) -> None:
    row = (await conn.execute(_MESSAGE_BY_ID, {"id": message_id})).mappings().one()
    values = await anyio.to_thread.run_sync(_processed_message_values, row, conn.dialect.name)
    await conn.execute(_MESSAGE_PROCESSED, values)


@dataclass(frozen=True)
class EncodedMessage:
    attachment_ids: list[str]
    content: str
    metadata: str
    payload: str


def _identity_metadata(metadata: dict[str, object]) -> dict[str, object]:
    metadata = dict(metadata)
    if metadata.get("attachments"):
        metadata["attachments"] = [
            {"id": item.id} for item in ATTACHMENT_IDS_ADAPTER.validate_python(metadata["attachments"])
        ]
    return metadata


def encode_message(message: ConversationMessage) -> EncodedMessage:
    if not message.created_at:
        message.created_at = utc_now_iso()
    if not message.message_id:
        message.message_id = uuid.uuid4().hex
    metadata = _identity_metadata(message.metadata)
    ids = [item.id for item in ATTACHMENT_IDS_ADAPTER.validate_python(metadata.get("attachments") or [])]
    payload = message.model_dump(mode="json")
    payload["metadata"] = metadata
    return EncodedMessage(
        ids,
        encrypt_field(message.content),
        encrypt_field(_dump_json(metadata)),
        encrypt_field(_dump_json(payload)),
    )


def append_message_row(
    conn: Connection,
    message: ConversationMessage,
    *,
    scope: OwnerScope,
    encoded: EncodedMessage,
) -> ConversationMessage | None:
    if (message.owner_id, message.agent_id, message.chat_id) != (scope.owner_id, scope.agent_id, scope.chat_id):
        raise ValueError("message and attachment scope differ")
    ids = encoded.attachment_ids
    with FileCatalog(conn, scope) as catalog:
        if ids:
            catalog.lock()
        insert_result = conn.execute(
            text(insert_message_sql()),
            {
                "message_id": message.message_id,
                "dedup_key": message.dedup_key,
                "owner_id": message.owner_id,
                "agent_id": message.agent_id,
                "chat_id": message.chat_id,
                "session_id": message.session_id,
                "source": message.source,
                "source_message_id": message.source_message_id,
                "actor_type": message.actor_type,
                "channel": message.channel,
                "content": encoded.content,
                "content_ref": message.content_ref,
                "content_hash": message.content_hash,
                "created_at": message.created_at,
                "metadata_json": encoded.metadata,
                "payload_json": encoded.payload,
                "chat_history_visible": _chat_history_visible(message.metadata),
                "inbound_unprocessed": _inbound_unprocessed(message),
            },
        )
        if insert_result.rowcount == 0:
            return None
        if ids:
            files = catalog.bind(
                ids,
                message.message_id,
                historical=message.actor_type == "agent" and message.metadata.get("terminal_publication") is True,
            )
            by_id = {file.id: attachment_metadata(file) for file in files}
            message.metadata["attachments"] = [
                {**by_id[file_id], "message_id": message.source_message_id} for file_id in dict.fromkeys(ids)
            ]
        return message
