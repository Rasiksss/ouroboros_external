from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundAttachment:
    filename: str
    mime: str
    size: int
    temp_path: str


@dataclass(frozen=True)
class InboundMessage:
    chat_id: Any
    user_id: Any
    text: str
    caption: str = ""
    image_data: Any = None
    raw_command: str = ""
    source_message_id: str = ""
    attachments: tuple[InboundAttachment, ...] = ()
    history_only: bool = False
    sender_session_id: str = ""
    sender_label: str = ""
    conversation_id: str = ""
    ts: str = ""  # transport-side ISO UTC timestamp; empty means "now"
