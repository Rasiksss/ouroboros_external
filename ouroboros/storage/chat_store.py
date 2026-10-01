"""Chat message persistence via the runtime metadata store (DB SSOT)."""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import uuid
from typing import Any

from ouroboros.contracts.chat_id_policy import is_derived_a2a_chat_id
from ouroboros.runtime_time import parse_local_bound, runtime_time_config
from ouroboros.storage.runtime_models import (
    ActorType,
    ConversationMessage,
    normalize_task_source,
)
from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store
from ouroboros.utils import utc_now_iso

_CHAT_HISTORY_LOCK = threading.RLock()
SHARED_CHAT_SUMMARY_ID = "shared-chat"
SHARED_CHAT_SUMMARY_CONVERSATION_ID = "global"


def conversation_id_for_chat(*, source: str, chat_id: Any) -> str:
    return f"{source or 'chat'}:{chat_id}"


def conversation_id_for_a2a(chat_id: Any) -> str:
    """One remote task is one conversation, so a single exchange can be exported or deleted.

    Empty for anything else, which is how the caller falls back to the session.
    """
    if not is_derived_a2a_chat_id(chat_id):
        return ""
    return conversation_id_for_chat(source="a2a", chat_id=chat_id)


def resolve_conversation_id(*, conversation_id: str, session_id: str, source: str, chat_id: Any) -> str:
    """The conversation a stored message belongs to.

    ``conversation_id`` is the stable logical-chat key (retrieval scope); the
    ingress/egress that knows the transport supplies it explicitly — Dialogs
    passes ``dialogs:<chat_id>``. ``session_id`` is the runtime epoch (usage
    scope, rotated on restart) and only anchors session-scoped chats such as
    the browser. The store itself stays transport-neutral.
    """
    explicit = str(conversation_id or "").strip()
    if explicit:
        return explicit
    # Ahead of the session id: A2A rows belong to the remote task that produced
    # them, and the session outlives every one of them.
    a2a = conversation_id_for_a2a(chat_id)
    if a2a:
        return a2a
    return str(session_id or "").strip() or conversation_id_for_chat(source=source or "chat", chat_id=chat_id)


def _message_storage_id(
    *,
    client_message_id: str,
    actor_type: ActorType,
    source: str,
    chat_id: Any,
    user_id: Any,
    sender_session_id: str,
) -> str:
    raw_id = str(client_message_id or "").strip()
    if not raw_id:
        return uuid.uuid4().hex
    scope = json.dumps(
        [
            str(actor_type),
            source,
            str(chat_id),
            str(user_id),
            str(sender_session_id or ""),
            raw_id,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, scope).hex


def _matches_legacy_message_scope(
    message: ConversationMessage,
    *,
    actor_type: ActorType,
    source: str,
    chat_id: Any,
    user_id: Any,
    sender_session_id: str,
) -> bool:
    metadata = message.metadata or {}
    return (
        message.actor_type == actor_type
        and message.source == source
        and str(metadata.get("chat_id")) == str(chat_id)
        and str(metadata.get("user_id")) == str(user_id)
        and str(metadata.get("sender_session_id") or "") == str(sender_session_id or "")
    )


def get_chat_message_by_client_id(
    drive_root: pathlib.Path,
    *,
    actor_type: ActorType,
    source: str,
    chat_id: Any,
    user_id: Any,
    client_message_id: str,
    sender_session_id: str = "",
) -> ConversationMessage | None:
    raw_id = str(client_message_id or "").strip()
    if not raw_id:
        return None
    store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
    source_value = normalize_task_source(source)
    message_id = _message_storage_id(
        client_message_id=raw_id,
        actor_type=actor_type,
        source=source_value,
        chat_id=chat_id,
        user_id=user_id,
        sender_session_id=sender_session_id,
    )
    for candidate_id in (message_id, raw_id):
        message = store.get_message(candidate_id)
        if message is not None and _matches_legacy_message_scope(
            message,
            actor_type=actor_type,
            source=source_value,
            chat_id=chat_id,
            user_id=user_id,
            sender_session_id=sender_session_id,
        ):
            return message
    return None


def mark_chat_message_processed(drive_root: pathlib.Path, message_id: str) -> None:
    store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
    message = store.get_message(message_id)
    if message is None:
        raise RuntimeError(f"inbound chat message disappeared before acknowledgement: {message_id}")
    metadata = dict(message.metadata or {})
    metadata["inbound_processed"] = True
    if not store.update_message_metadata(message_id, metadata):
        raise RuntimeError(f"failed to acknowledge inbound chat message: {message_id}")


def append_chat_message(
    drive_root: pathlib.Path,
    *,
    actor_type: ActorType,
    chat_id: Any,
    user_id: Any,
    text: str,
    session_id: str = "",
    ts: str | None = None,
    fmt: str = "",
    source: str = "",
    sender_label: str = "",
    sender_session_id: str = "",
    client_message_id: str = "",
    transport: dict[str, Any] | None = None,
    task_id: str = "",
    conversation_id: str = "",
    is_progress: bool = False,
    message_type: str = "",
    extra_metadata: dict[str, Any] | None = None,
) -> ConversationMessage | None:
    with _CHAT_HISTORY_LOCK:
        store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
        ts_value = ts or utc_now_iso()
        source_value = normalize_task_source(source)
        resolved_conversation_id = resolve_conversation_id(
            conversation_id=conversation_id,
            session_id=session_id,
            source=source,
            chat_id=chat_id,
        )
        store.get_or_create_conversation(
            resolved_conversation_id,
            session_id=session_id,
            source=source_value,
        )
        metadata: dict[str, Any] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "format": fmt,
            "sender_label": sender_label,
            "sender_session_id": sender_session_id,
            "transport": dict(transport or {}),
            "task_id": str(task_id or ""),
        }
        if is_progress:
            metadata["is_progress"] = True
            metadata["type"] = message_type or "send_message"
            metadata["content"] = text
        elif message_type:
            metadata["type"] = message_type
        if extra_metadata:
            metadata.update(extra_metadata)
        raw_client_message_id = str(client_message_id or "").strip()
        if (
            raw_client_message_id
            and get_chat_message_by_client_id(
                drive_root,
                actor_type=actor_type,
                source=source_value,
                chat_id=chat_id,
                user_id=user_id,
                client_message_id=raw_client_message_id,
                sender_session_id=sender_session_id,
            )
            is not None
        ):
            return None
        message_id = _message_storage_id(
            client_message_id=raw_client_message_id,
            actor_type=actor_type,
            source=source_value,
            chat_id=chat_id,
            user_id=user_id,
            sender_session_id=sender_session_id,
        )
        return store.append_message(
            ConversationMessage(
                message_id=message_id,
                conversation_id=resolved_conversation_id,
                session_id=session_id,
                source=source_value,
                source_message_id=raw_client_message_id,
                actor_type=actor_type,
                channel=str(source or "chat"),
                content=text,
                created_at=ts_value,
                metadata=metadata,
            )
        )


def clear_owner_chat_history(
    drive_root: pathlib.Path,
    *,
    preserve_message_id: str = "",
) -> None:
    """Clear owner-visible chat metadata and context files on the volume."""
    with _CHAT_HISTORY_LOCK:
        from ouroboros.storage.blob.drive import get_drive_storage

        root = pathlib.Path(drive_root)
        store = get_runtime_store(root) or init_runtime_store(root)
        store.delete_owner_messages(
            preserve_message_ids=(preserve_message_id,) if preserve_message_id else (),
            before_message_id=preserve_message_id,
        )
        get_drive_storage(root).delete_prefix("memory/contexts")


def list_owner_messages(
    drive_root: pathlib.Path,
    *,
    limit: int = 10000,
    after_message_id: str | None = None,
    include_progress: bool = False,
    newest_first: bool = False,
) -> list[ConversationMessage]:
    store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
    return store.list_owner_messages(
        limit=limit,
        after_message_id=after_message_id or None,
        include_progress=include_progress,
        newest_first=newest_first,
    )


def list_chat_history_messages(
    drive_root: pathlib.Path,
    *,
    limit: int = 100,
    offset: int = 0,
    date_from: str = "",
    date_to: str = "",
) -> list[ConversationMessage]:
    store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
    time_config = runtime_time_config()
    return store.list_chat_history_messages(
        limit=max(0, int(limit)),
        offset=max(0, int(offset)),
        created_from=parse_local_bound(
            date_from,
            field_name="date_from",
            config=time_config,
        ),
        created_to=parse_local_bound(
            date_to,
            field_name="date_to",
            config=time_config,
        ),
    )


def chat_watermark_signature(drive_root: pathlib.Path) -> dict[str, Any]:
    """Return a small signature for the current owner chat generation in DB."""
    store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
    messages = store.list_owner_messages(limit=1, include_progress=True)
    if not messages:
        return {}
    first = messages[0]
    count = store.count_messages()
    return {
        "first_message_id": first.message_id,
        "first_created_at": first.created_at,
        "message_count": count,
    }


log = logging.getLogger(__name__)


CHAT_HISTORY_MAX_COUNT = 200


def render_chat_history(
    drive_root: pathlib.Path,
    *,
    count: int = 100,
    offset: int = 0,
    date_from: str = "",
    date_to: str = "",
) -> str:
    """Render owner-visible chat history for the ``chat_history`` tool.

    Errors are answered rather than raised: the tool shows the reason to the
    agent instead of failing the turn.
    """
    try:
        from ouroboros.storage.conversation_format import format_attachment_descriptors, format_chat_history_line

        clean_count = min(max(int(count or 100), 1), CHAT_HISTORY_MAX_COUNT)
        clean_offset = max(int(offset or 0), 0)
        messages = list_chat_history_messages(
            drive_root,
            limit=clean_count,
            offset=clean_offset,
            date_from=date_from,
            date_to=date_to,
        )

        if not messages:
            return "(no messages matching query)"

        messages = list(reversed(messages))
        lines = [format_chat_history_line(message) + format_attachment_descriptors(message) for message in messages]
        return f"Showing {len(messages)} messages:\n\n" + "\n".join(lines)
    except Exception as e:
        return f"(error reading history: {e})"


def list_recent_owner_messages(
    drive_root: pathlib.Path, limit: int = 100, *, after_message_id: str = ""
) -> list[ConversationMessage]:
    """Return the most recent owner chat messages, oldest-first.

    The tail is bounded by the store rather than by pulling every message and
    slicing in Python, which is what the facade this replaces did twice per
    prompt build. The newest-first query is the right one to bound: its
    ``chat_history_visible`` predicate is the indexed form of exactly the
    owner-message filter — not a progress message — so the two owner-chat reads
    see one set of messages.
    """
    try:
        store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
        messages = store.list_chat_history_messages(
            limit=max(int(limit or 0), 0),
            after_message_id=after_message_id or None,
        )
        return list(reversed(messages))
    except Exception:
        log.warning("Failed to read chat tail from runtime store", exc_info=True)
        return []


__all__ = [
    "CHAT_HISTORY_MAX_COUNT",
    "SHARED_CHAT_SUMMARY_CONVERSATION_ID",
    "SHARED_CHAT_SUMMARY_ID",
    "append_chat_message",
    "chat_watermark_signature",
    "clear_owner_chat_history",
    "conversation_id_for_chat",
    "list_chat_history_messages",
    "list_owner_messages",
    "list_recent_owner_messages",
    "render_chat_history",
]
