from __future__ import annotations

import json
from typing import Any

from sqlalchemy import RowMapping

from ouroboros.storage.message_field_crypto import decrypt_field
from ouroboros.storage.runtime_models import ConsolidationLease, ContextSummary, ConversationMessage

_RETIRED_KEY = "conversation_id"


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def load_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _overlay_axes(payload: dict[str, Any], mapping: RowMapping, *axes: str) -> dict[str, Any]:
    payload.pop(_RETIRED_KEY, None)
    payload.update({axis: mapping[axis] for axis in axes})
    return payload


def to_message(mapping: RowMapping) -> ConversationMessage:
    content = decrypt_field(mapping["content"])
    metadata_json = decrypt_field(mapping["metadata_json"])
    payload = load_json(decrypt_field(mapping["payload_json"]))
    if payload:
        payload.setdefault("metadata", load_json(metadata_json))
        return ConversationMessage.model_validate(
            _overlay_axes(payload, mapping, "owner_id", "agent_id", "chat_id", "dedup_key")
        )
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
        metadata=load_json(metadata_json),
    )


def to_context_summary(mapping: RowMapping) -> ContextSummary:
    payload = load_json(decrypt_field(mapping["payload_json"]))
    if payload:
        payload.setdefault("metadata", load_json(decrypt_field(mapping["metadata_json"])))
        return ContextSummary.model_validate(_overlay_axes(payload, mapping, "owner_id", "agent_id", "chat_id"))
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
        metadata=load_json(decrypt_field(mapping["metadata_json"])),
    )


def to_consolidation_lease(mapping: RowMapping) -> ConsolidationLease:
    return ConsolidationLease(
        owner_id=mapping["owner_id"],
        agent_id=mapping["agent_id"],
        holder_id=mapping["holder_id"] or "",
        lease_expires_at=mapping["lease_expires_at"] or "",
        consecutive_failures=int(mapping["consecutive_failures"] or 0),
        last_failure_at=mapping["last_failure_at"] or "",
        updated_at=mapping["updated_at"] or "",
    )


__all__ = ["dump_json", "load_json", "to_consolidation_lease", "to_context_summary", "to_message"]
