from sqlalchemy.engine import Row

from ouroboros.storage.message_field_crypto import decrypt_field
from ouroboros.storage.runtime_models import ConversationMessage, ToolConfirmation
from ouroboros.storage.runtime_serialization import load_json as _load_json


def message_from_row(row: Row[tuple[object, ...]]) -> ConversationMessage:
    mapping = row._mapping
    content = decrypt_field(mapping["content"])
    metadata_json = decrypt_field(mapping["metadata_json"])
    payload_json = decrypt_field(mapping["payload_json"])
    payload = _load_json(payload_json)
    if isinstance(payload, dict) and payload:
        payload.setdefault("metadata", _load_json(metadata_json))
        payload.pop("conversation_id", None)
        payload.update({axis: mapping[axis] for axis in ("owner_id", "agent_id", "chat_id", "dedup_key")})
        return ConversationMessage.model_validate(payload)
    return ConversationMessage(
        message_id=mapping["message_id"],
        dedup_key=mapping["dedup_key"],
        owner_id=mapping["owner_id"],
        agent_id=mapping["agent_id"],
        chat_id=mapping["chat_id"],
        session_id=mapping["session_id"],
        source=mapping["source"],
        source_message_id=mapping["source_message_id"],
        actor_type=mapping["actor_type"],
        channel=mapping["channel"],
        content=content,
        content_ref=mapping["content_ref"],
        content_hash=mapping["content_hash"],
        created_at=mapping["created_at"],
        metadata=_load_json(metadata_json),
    )


def tool_confirmation_from_row(row: Row[tuple[object, ...]]) -> ToolConfirmation:
    mapping = row._mapping
    return ToolConfirmation(
        confirmation_id=mapping["confirmation_id"],
        owner_id=mapping["owner_id"],
        agent_id=mapping["agent_id"],
        surface_id=mapping["surface_id"],
        chat_id=mapping["chat_id"],
        task_id=mapping["task_id"] or "",
        tool_name=mapping["tool_name"] or "",
        call_ordinal=int(mapping["call_ordinal"] or 0),
        round_idx=int(mapping["round_idx"] or 0),
        args_hash=mapping["args_hash"] or "",
        status=mapping["status"],
        created_at=mapping["created_at"] or "",
        decided_at=mapping["decided_at"] or "",
    )
