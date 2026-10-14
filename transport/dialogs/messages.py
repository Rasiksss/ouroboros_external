from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: This transport's name, and the boundary label its ingress originates operations under.
TRANSPORT_NAME = "dialogs"


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
    ts: str = ""  # transport-side ISO UTC timestamp; empty means "now"
    #: The operation this update belongs to, established by the SDK handler that received it.
    #: Not a header a caller sent — this protocol has none — but the identifier this transport
    #: originated, carried to the consumer thread, which cannot see the handler's context.
    #: Empty means no ingress frame ran (a journal replay), and the consumer originates then.
    trace_id: str = ""
