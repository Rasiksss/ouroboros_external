"""Format ConversationMessage rows for prompts and operator-facing history."""

from __future__ import annotations

import re

from ouroboros.storage.runtime_models import ConversationMessage


def _meta(message: ConversationMessage) -> dict:
    return dict(message.metadata or {})


def format_attachment_descriptors(message: ConversationMessage) -> str:
    descriptors: list[str] = []
    for value in (_meta(message).get("attachments") or [])[:10]:
        if not isinstance(value, dict) or str(value.get("status") or "ready") != "ready":
            continue
        attachment_id = str(value.get("id") or "")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", attachment_id) is None:
            continue
        filename = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value.get("filename") or "attachment"))[:200]
        descriptors.append(f"{filename} [attachment_id={attachment_id}]")
    return "\n📎 Attachments: " + "; ".join(descriptors) if descriptors else ""


def format_chat_history_line(message: ConversationMessage) -> str:
    ts = str(message.created_at or "")[:16]
    text = message.display_text()
    if message.actor_type == "agent":
        return f"→ [{ts}] {text}"
    if message.actor_type == "system":
        entry_type = str(_meta(message).get("type") or "").strip() or "system"
        return f"📋 [{ts}] [{entry_type}] {text}"
    username = _meta(message).get("sender_label") or "User"
    return f"← [{ts}] [{username}] {text}"


def format_chat_summary_line(message: ConversationMessage) -> str:
    ts_full = str(message.created_at or "")
    ts_hhmm = ts_full[11:16] if len(ts_full) >= 16 else ""
    text = message.display_text()
    if message.actor_type == "agent":
        return f"→ {ts_hhmm} {text}"
    if message.actor_type == "system":
        entry_type = str(_meta(message).get("type") or "").strip() or "system"
        return f"📋 {ts_hhmm} [{entry_type}] {text}"
    username = _meta(message).get("sender_label") or "User"
    return f"← {ts_hhmm} [{username}] {text}"


def format_block_entry(message: ConversationMessage) -> str:
    ts_raw = str(message.created_at or "")
    ts = ts_raw[:10] + " " + ts_raw[11:16] if len(ts_raw) >= 16 else ts_raw
    if message.actor_type == "agent":
        direction_prefix = "-> "
        author = "ГигаАгент"
    elif message.actor_type == "system":
        direction_prefix = "[system] "
        author = "ГигаАгент"
    else:
        direction_prefix = ""
        author = _meta(message).get("sender_label") or "User"
    return f"[{ts}] {direction_prefix}{author}: {message.display_text()}"


def format_messages_for_block(messages: list[ConversationMessage]) -> str:
    return "\n\n".join(format_block_entry(message) for message in messages)


def summarize_chat(messages: list[ConversationMessage]) -> str:
    """Render the trailing chat window as one summary line per message."""
    if not messages:
        return ""
    return "\n".join(format_chat_summary_line(message) for message in messages[-1000:])
