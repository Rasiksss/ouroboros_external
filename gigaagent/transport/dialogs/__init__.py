"""The dialogs (SberChat SDK) transport."""

from transport.dialogs.messages import InboundAttachment, InboundMessage
from transport.dialogs.provider import DialogsProvider
from transport.dialogs.transport import (
    TRANSPORT_NAME,
    DialogsTransport,
    build_dialogs_transport,
    dialogs_feature_enabled,
)

__all__ = [
    "TRANSPORT_NAME",
    "DialogsProvider",
    "DialogsTransport",
    "InboundAttachment",
    "InboundMessage",
    "build_dialogs_transport",
    "dialogs_feature_enabled",
]
