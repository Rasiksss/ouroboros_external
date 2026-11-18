"""Chat message persistence via the runtime metadata store (DB SSOT)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import pathlib
import threading
import uuid
from typing import Any

import anyio
from pydantic import JsonValue
from sqlalchemy import bindparam, text as sql_text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from config_registry.chat_registry.domain import chat_title
from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryStore
from new_core.identity import OwnerScope
from ouroboros.contracts.chat_id_policy import is_internal_chat_id
from ouroboros.runtime_time import parse_local_bound, runtime_time_config
from ouroboros.storage.conversation_format import format_attachment_descriptors, format_chat_history_line
from ouroboros.storage.conversations.message_write import (
    EncodedMessage,
    append_message_row,
    encode_message,
    mark_message_processed,
)
from ouroboros.storage.file_catalog import (
    FileCatalog,
    FileError,
    FileFailure,
    hydrate_attachments,
    load_message_attachments,
)
from ouroboros.storage.message_history import HistoryWindow, message_from_row
from ouroboros.storage.runtime_backend import MessageAnchorMissingError
from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.storage.runtime_models import (
    ActorType,
    ConversationMessage,
    normalize_task_source,
)
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.utils import utc_now_iso

_CHAT_HISTORY_LOCK = threading.RLock()


def inner_message_key(
    *,
    client_message_id: str,
    actor_type: ActorType,
    source: str,
    chat_id: Any,
    user_id: Any,
    sender_session_id: str,
) -> str:
    """Preserve the pre-ownership key used by migrated messages."""
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


def message_dedup_key(scope: OwnerScope, inner: str) -> str:
    """Fold ownership over the legacy identity, matching migration 0010."""
    material = json.dumps(
        [scope.owner_id, scope.agent_id, str(inner or "")],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, material).hex


def matches_message_scope(
    message: ConversationMessage,
    *,
    scope: OwnerScope,
    actor_type: ActorType,
    source: str,
    chat_id: Any,
    user_id: Any,
    sender_session_id: str,
) -> bool:
    """Check every sender axis, including those absent from a legacy raw-client-id key."""
    metadata = message.metadata or {}
    return (
        message.owner_id == scope.owner_id
        and message.agent_id == scope.agent_id
        and message.actor_type == actor_type
        and message.source == source
        and message.chat_id == str(chat_id)
        and str(metadata.get("user_id")) == str(user_id)
        and str(metadata.get("sender_session_id") or "") == str(sender_session_id or "")
    )


def get_chat_message_by_client_id(
    drive_root: pathlib.Path,
    *,
    scope: OwnerScope,
    actor_type: ActorType,
    source: str,
    chat_id: Any,
    user_id: Any,
    client_message_id: str,
    sender_session_id: str = "",
) -> ConversationMessage | None:
    """The row this owner stored for ``client_message_id``, or ``None``.

    A client message id is unique within its owner and not across owners — a chat id is
    owner-relative and the id itself is whatever the client sent — so the lookup is scoped
    on both sides: the key folds the ownership, and the read filters on it.
    """
    raw_id = str(client_message_id or "").strip()
    if not raw_id:
        return None
    store = runtime_persistence.current.conversations_repo
    source_value = normalize_task_source(source)
    inner = inner_message_key(
        client_message_id=raw_id,
        actor_type=actor_type,
        source=source_value,
        chat_id=chat_id,
        user_id=user_id,
        sender_session_id=sender_session_id,
    )
    for candidate in (inner, raw_id):
        message = store.get_message_by_dedup_key(message_dedup_key(scope, candidate), scope=scope)
        if message is not None and matches_message_scope(
            message,
            scope=scope,
            actor_type=actor_type,
            source=source_value,
            chat_id=chat_id,
            user_id=user_id,
            sender_session_id=sender_session_id,
        ):
            return message
    return None


def mark_chat_message_processed(drive_root: pathlib.Path, message_id: str) -> None:

    with resolve_runtime_engine("message journal").begin() as conn:
        mark_message_processed(conn, message_id)


def build_chat_message(
    scope: OwnerScope,
    *,
    actor_type: ActorType,
    chat_id: int | str,
    user_id: int | str,
    text: str,
    session_id: str = "",
    ts: str | None = None,
    fmt: str = "",
    source: str = "",
    sender_label: str = "",
    sender_session_id: str = "",
    client_message_id: str = "",
    transport: dict[str, JsonValue] | None = None,
    task_id: str = "",
    is_progress: bool = False,
    message_type: str = "",
    extra_metadata: dict[str, JsonValue] | None = None,
) -> ConversationMessage:
    ts_value = ts or utc_now_iso()
    source_value = normalize_task_source(source)
    metadata: dict[str, JsonValue] = {
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
    inner = inner_message_key(
        client_message_id=raw_client_message_id,
        actor_type=actor_type,
        source=source_value,
        chat_id=chat_id,
        user_id=user_id,
        sender_session_id=sender_session_id,
    )
    return ConversationMessage(
        message_id=uuid.uuid4().hex,
        dedup_key=message_dedup_key(scope, inner),
        owner_id=scope.owner_id,
        agent_id=scope.agent_id,
        chat_id=scope.chat_id,
        session_id=session_id,
        source=source_value,
        source_message_id=raw_client_message_id,
        actor_type=actor_type,
        channel=str(source or "chat"),
        content=text,
        created_at=ts_value,
        metadata=metadata,
    )


def append_chat_message(
    drive_root: pathlib.Path,
    *,
    scope: OwnerScope,
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
    is_progress: bool = False,
    message_type: str = "",
    extra_metadata: dict[str, Any] | None = None,
) -> ConversationMessage | None:
    """Persist one chat row under ``scope``.

    The row's ``chat_id`` column comes from ``scope`` and from nothing else. The ``chat_id``
    parameter survives as the transport's own typed value inside ``metadata`` — Dialogs keeps
    an ``int`` — and a caller passing one that disagrees with its scope files the row under
    one conversation and renders it as another.
    """
    with _CHAT_HISTORY_LOCK:
        source_value = normalize_task_source(source)
        raw_client_message_id = str(client_message_id or "").strip()
        if (
            raw_client_message_id
            and get_chat_message_by_client_id(
                drive_root,
                scope=scope,
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
        return runtime_persistence.current.conversations_repo.append_message(
            scope=scope,
            message=build_chat_message(
                scope,
                actor_type=actor_type,
                chat_id=chat_id,
                user_id=user_id,
                text=text,
                session_id=session_id,
                ts=ts,
                fmt=fmt,
                source=source,
                sender_label=sender_label,
                sender_session_id=sender_session_id,
                client_message_id=client_message_id,
                transport=transport,
                task_id=task_id,
                is_progress=is_progress,
                message_type=message_type,
                extra_metadata=extra_metadata,
            ),
        )


def list_owner_messages(
    drive_root: pathlib.Path,
    *,
    scope: OwnerScope,
    limit: int = 10000,
    actor_type: str | None = None,
    source: str | None = None,
    after_message_id: str | None = None,
    include_progress: bool = False,
    newest_first: bool = False,
) -> list[ConversationMessage]:
    """One conversation's owner-visible messages, oldest first unless ``newest_first`` bounds
    the window. The chat comes off ``scope``; a read across an owner's conversations is
    ``IConversationRepository.list_pair_messages`` and is named for it.

    ``limit`` cuts from the oldest end by default, which is wrong for anything
    looking for recent rows: a store with more messages than the limit never
    reaches them. ``newest_first`` takes the newest ``limit`` rows and still
    returns them in the order they were said. ``actor_type`` and ``source``
    narrow the window before the limit rather than after it.
    """
    store = runtime_persistence.current.conversations_repo
    return store.list_owner_messages(
        scope=scope,
        limit=limit,
        actor_type=actor_type,
        source=source,
        after_message_id=after_message_id or None,
        include_progress=include_progress,
        newest_first=newest_first,
    )


def list_chat_history_messages(
    drive_root: pathlib.Path,
    *,
    scope: OwnerScope,
    limit: int = 100,
    offset: int = 0,
    date_from: str = "",
    date_to: str = "",
    through_message_id: str = "",
    message_ids: list[str] | None = None,
) -> list[ConversationMessage]:
    store = runtime_persistence.current.conversations_repo
    time_config = runtime_time_config(scope)
    return store.list_chat_history_messages(
        scope=scope,
        through_message_id=through_message_id or None,
        message_ids=message_ids,
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


def chat_watermark_signature(drive_root: pathlib.Path, *, scope: OwnerScope) -> dict[str, Any]:
    """Return a small signature for the current owner chat generation in DB."""
    store = runtime_persistence.current.conversations_repo
    messages = store.list_owner_messages(scope=scope, limit=1, include_progress=True)
    if not messages:
        return {}
    first = messages[0]
    count = store.count_messages(scope=scope)
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
    scope: OwnerScope,
    count: int = 100,
    offset: int = 0,
    date_from: str = "",
    date_to: str = "",
    through_message_id: str = "",
    message_ids: list[str] | None = None,
) -> str:
    """Render owner-visible chat history for the ``chat_history`` tool.

    Errors are answered rather than raised: the tool shows the reason to the
    agent instead of failing the turn.
    """
    try:
        clean_count = min(max(int(count or 100), 1), CHAT_HISTORY_MAX_COUNT)
        clean_offset = max(int(offset or 0), 0)
        messages = list_chat_history_messages(
            drive_root,
            scope=scope,
            message_ids=message_ids,
            limit=clean_count,
            offset=clean_offset,
            date_from=date_from,
            date_to=date_to,
            through_message_id=through_message_id,
        )

        if not messages:
            return "(no messages matching query)"

        messages = list(reversed(messages))
        lines = [format_chat_history_line(message) + format_attachment_descriptors(message) for message in messages]
        return f"Showing {len(messages)} messages:\n\n" + "\n".join(lines)
    except Exception as e:
        return f"(error reading history: {e})"


def list_recent_owner_messages(
    drive_root: pathlib.Path,
    limit: int = 100,
    *,
    scope: OwnerScope,
    after_message_id: str = "",
    through_message_id: str = "",
    message_ids: list[str] | None = None,
) -> list[ConversationMessage]:
    """Return the most recent owner chat messages, oldest-first.

    The tail is bounded by the store rather than by pulling every message and
    slicing in Python, which is what the facade this replaces did twice per
    prompt build. The newest-first query is the right one to bound: its
    ``chat_history_visible`` predicate is the indexed form of exactly the
    owner-message filter — neither progress nor delivery-only receipts — so the two owner-chat reads
    see one set of messages.
    """
    try:
        store = runtime_persistence.current.conversations_repo
        messages = store.list_chat_history_messages(
            scope=scope,
            message_ids=message_ids,
            limit=max(int(limit or 0), 0),
            after_message_id=after_message_id or None,
            through_message_id=through_message_id or None,
        )
        return list(reversed(messages))
    except MessageAnchorMissingError:
        raise
    except Exception:
        log.warning("Failed to read chat tail from runtime store", exc_info=True)
        if after_message_id or through_message_id:
            raise
        return []


__all__ = [
    "CHAT_HISTORY_MAX_COUNT",
    "append_chat_message",
    "chat_watermark_signature",
    "list_chat_history_messages",
    "list_owner_messages",
    "list_recent_owner_messages",
    "render_chat_history",
]


class InboundRefused(FileError):
    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason, FileFailure.CONFLICT)
        self.code = code


class InboundMessageStore:
    def __init__(self, engine: AsyncEngine, registry: SqlChatRegistryStore, *, timeout: float) -> None:
        self.engine, self.registry, self.timeout = engine, registry, timeout

    async def persist(self, scope: OwnerScope, message: ConversationMessage) -> ConversationMessage:
        encoded = await anyio.to_thread.run_sync(encode_message, message)
        with anyio.CancelScope(shield=True):
            async with asyncio.timeout(self.timeout), self.engine.begin() as connection:
                persisted = await self._persist_inbound_message(connection, scope, message, encoded)
                if not persisted.metadata.get("inbound_processed"):
                    attachments = persisted.attachments
                    title = chat_title(persisted.content) or chat_title(
                        attachments[0].get("filename", "") if attachments else ""
                    )
                    chat = await connection.run_sync(
                        lambda conn: self.registry.ensure_chat_in_transaction(
                            conn, scope, provisional_title=title or "New conversation"
                        )
                    )
                    if not chat.is_active:
                        raise InboundRefused("this conversation was retired", "chat_retired")
        return persisted

    async def _persist_inbound_message(
        self, connection: AsyncConnection, scope: OwnerScope, message: ConversationMessage, encoded: EncodedMessage
    ) -> ConversationMessage:
        def lock_catalog(conn: Connection) -> None:
            with FileCatalog(conn, scope) as catalog:
                catalog.lock()

        if encoded.attachment_ids:
            await connection.run_sync(lock_catalog)
        metadata = message.metadata
        keys = [message.dedup_key, message_dedup_key(scope, message.source_message_id)]
        requested_ids = encoded.attachment_ids
        stored = await self._inbound_message(connection, scope, keys, message)
        if stored is None:
            stored = await connection.run_sync(lambda c: append_message_row(c, message, scope=scope, encoded=encoded))
            if stored is None:
                stored = await self._inbound_message(connection, scope, keys, message)
            if stored is None:
                raise RuntimeError("inbound insert returned no row")
        attachments = stored.attachments
        if (
            stored.content != message.content
            or [item["id"] for item in attachments] != requested_ids
            or bool(stored.metadata.get("detached")) != bool(metadata.get("detached"))
        ):
            raise InboundRefused("duplicate client_message_id has different payload", "message_conflict")
        return stored

    async def _inbound_message(
        self, connection: AsyncConnection, scope: OwnerScope, keys: list[str], message: ConversationMessage
    ) -> ConversationMessage | None:
        metadata = message.metadata
        result = await connection.execute(
            sql_text(
                "SELECT * FROM messages WHERE owner_id=:owner AND agent_id=:agent AND chat_id=:chat AND dedup_key IN :keys"
            ).bindparams(bindparam("keys", expanding=True)),
            {"owner": scope.owner_id, "agent": scope.agent_id, "chat": scope.chat_id, "keys": keys},
        )
        rows = result.mappings().all()
        candidates = await anyio.to_thread.run_sync(
            lambda: {row["dedup_key"]: message_from_row(row, dialect_name=connection.dialect.name) for row in rows}
        )
        stored = next(
            (
                candidate
                for key in keys
                if (candidate := candidates.get(key)) is not None
                and matches_message_scope(
                    candidate,
                    scope=scope,
                    actor_type="owner",
                    source=str(scope.surface_id),
                    chat_id=scope.chat_id,
                    user_id=metadata["user_id"],
                    sender_session_id=metadata["sender_session_id"],
                )
            ),
            None,
        )
        if stored is not None:
            return (await connection.run_sync(lambda c: hydrate_attachments(c, [stored])))[0]
        return None


def load_chat_history(conn: Connection, scope: OwnerScope, *, limit: int, before_ts: str = "") -> HistoryWindow:
    if not 1 <= limit <= 2000:
        raise FileError("invalid limit", FileFailure.INVALID_INPUT)
    params: dict[str, str | int | tuple[str, ...]] = {
        "owner": scope.owner_id,
        "agent": scope.agent_id,
        "chat": scope.chat_id,
        "surface": str(scope.surface_id),
        "before": before_ts,
        "limit": limit + 1,
    }
    predicate = "owner_id=:owner AND agent_id=:agent AND chat_id=:chat"
    if is_internal_chat_id(scope.chat_id):
        predicate += " AND chat_history_visible"
    projection = (
        "messages.*, (SELECT confirmation_id FROM tool_confirmations "
        "WHERE owner_id=:owner AND agent_id=:agent AND surface_id=:surface "
        "AND chat_id=:chat AND status='pending') AS open_confirmation_id"
    )
    bound = "(:before='' OR created_at<:before)"
    if before_ts.startswith("cursor:"):
        try:
            stamp, message_id = base64.urlsafe_b64decode(before_ts[7:]).decode().split("\0")
        except (ValueError, UnicodeError) as exc:
            raise FileError("invalid history cursor", FileFailure.INVALID_INPUT) from exc
        params.update(before=stamp, before_id=message_id)
        bound = "(created_at,message_id)<(:before,:before_id)"
    fetched = (
        conn.execute(
            sql_text(
                f"SELECT {projection} FROM messages WHERE {predicate} AND {bound} "
                "ORDER BY created_at DESC,message_id DESC LIMIT :limit"
            ),
            params,
        )
        .mappings()
        .all()
    )
    rows, more = list(reversed(fetched[:limit])), len(fetched) > limit
    return HistoryWindow(
        rows,
        conn.dialect.name,
        more,
        load_message_attachments(conn, [row["message_id"] for row in rows]),
        rows[0]["open_confirmation_id"] if rows else None,
    )


def history_cursor(message: ConversationMessage) -> str:
    return "cursor:" + base64.urlsafe_b64encode((message.created_at + "\0" + message.message_id).encode()).decode()
