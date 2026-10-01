"""Normalized inbound message envelope from ingress transports."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ouroboros.observability import aef_tracing
from ouroboros.storage.chat_store import conversation_id_for_chat
from ouroboros.storage.runtime_models import RuntimeModel, TaskSource, is_dialogs_source, normalize_task_source


def _ingress_conversation_id(conversation_id: Any, source: str, chat_id: Any) -> str:
    """The ingress knows the transport, so it keys Dialogs chats here; the
    store stays transport-neutral and falls back to the session scope."""
    explicit = str(conversation_id or "").strip()
    if explicit:
        return explicit
    return conversation_id_for_chat(source="dialogs", chat_id=chat_id) if is_dialogs_source(source) else ""


class InboundEnvelope(RuntimeModel):
    chat_id: int | str
    user_id: int | str | None = None
    text: str = ""
    source: TaskSource = "unknown"
    sender_label: str = ""
    sender_session_id: str = ""
    client_message_id: str = ""
    correlation_id: str = ""
    conversation_id: str = ""
    trace_id: str = ""
    transport: dict[str, Any] = Field(default_factory=dict)
    task_constraint: dict[str, Any] = Field(default_factory=dict)
    image_base64: str = ""
    image_mime: str = "image/jpeg"
    image_caption: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    suppress_chat_log: bool = False

    @classmethod
    def from_bridge_message(cls, msg: dict[str, Any]) -> InboundEnvelope:
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        transport_raw = msg.get("transport")
        transport = dict(transport_raw) if isinstance(transport_raw, dict) else {}
        transport_headers = transport.get("headers")
        headers = transport_headers if isinstance(transport_headers, dict) else {}
        trace_id = aef_tracing.trace_id_from_headers(headers) or aef_tracing.trace_id_from_headers(transport)
        if not trace_id:
            raw_trace_id = msg.get("trace_id")
            trace_id = (
                aef_tracing.ensure_trace_id(raw_trace_id)
                if raw_trace_id
                else aef_tracing.ensure_trace_id(aef_tracing.current_trace_id())
            )
        constraint_raw = msg.get("task_constraint")
        constraint = dict(constraint_raw) if isinstance(constraint_raw, dict) else {}
        raw_chat_id = chat.get("id")
        if raw_chat_id in (None, ""):
            raw_chat_id = 1
        raw_user_id = sender.get("id")
        if raw_user_id in (None, ""):
            raw_user_id = raw_chat_id
        source = normalize_task_source(str(msg.get("source") or "web"))
        return cls(
            chat_id=int(str(raw_chat_id)),
            user_id=int(str(raw_user_id)),
            text=str(msg.get("text") or ""),
            source=source,
            sender_label=str(msg.get("sender_label") or ""),
            sender_session_id=str(msg.get("sender_session_id") or ""),
            client_message_id=str(msg.get("client_message_id") or ""),
            conversation_id=_ingress_conversation_id(msg.get("conversation_id"), source, int(str(raw_chat_id))),
            trace_id=trace_id,
            transport=transport,
            task_constraint=constraint,
            image_base64=str(msg.get("image_base64") or ""),
            image_mime=str(msg.get("image_mime") or "image/jpeg"),
            image_caption=str(msg.get("image_caption") or ""),
            attachment_ids=[str(value) for value in (msg.get("attachment_ids") or []) if str(value).strip()],
            attachments=[dict(value) for value in (msg.get("attachments") or []) if isinstance(value, dict)],
            suppress_chat_log=bool(msg.get("suppress_chat_log")),
        )

    @classmethod
    def from_chat(
        cls,
        *,
        chat_id: Any,
        user_id: Any = None,
        text: str = "",
        source: TaskSource | str = "dialogs",
        sender_label: str = "",
        task_constraint: dict[str, Any] | None = None,
        image_data: tuple[str, str] | tuple[str, str, str] | None = None,
        correlation_id: str = "",
        conversation_id: str = "",
        client_message_id: str = "",
        trace_id: str = "",
        suppress_chat_log: bool = False,
        attachment_ids: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        transport: dict[str, Any] | None = None,
    ) -> InboundEnvelope:
        image_base64 = ""
        image_mime = "image/jpeg"
        image_caption = ""
        if image_data:
            image_base64 = image_data[0]
            image_mime = image_data[1]
            if len(image_data) > 2:
                image_caption = str(image_data[2] or "")
        normalized_source = normalize_task_source(source)
        return cls(
            chat_id=chat_id,
            user_id=user_id if user_id is not None else chat_id,
            text=str(text or ""),
            source=normalized_source,
            sender_label=str(sender_label or ""),
            task_constraint=dict(task_constraint or {}),
            image_base64=image_base64,
            image_mime=image_mime,
            image_caption=image_caption,
            correlation_id=str(correlation_id or ""),
            conversation_id=_ingress_conversation_id(conversation_id, normalized_source, chat_id),
            client_message_id=str(client_message_id or ""),
            trace_id=aef_tracing.ensure_trace_id(trace_id or aef_tracing.current_trace_id()),
            attachment_ids=list(attachment_ids or []),
            attachments=[dict(value) for value in (attachments or [])],
            transport=dict(transport or {}),
            suppress_chat_log=suppress_chat_log,
        )

    def image_tuple(self) -> tuple[str, str, str] | None:
        if not self.image_base64:
            return None
        return (self.image_base64, self.image_mime, self.image_caption)

    def effective_text(self) -> str:
        text = str(self.text or "").strip()
        if text:
            return text
        caption = str(self.image_caption or "").strip()
        if caption:
            return caption
        if self.image_base64:
            return "(image attached)"
        if self.attachment_ids:
            return "(file attached)"
        return ""


__all__ = ["InboundEnvelope"]
