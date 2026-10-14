"""The seam's wire format: one codec, used by every backend.

The seam promises that a broker-backed gateway replaces the in-process queues
without any transport changing (``transport/interfaces.py``). That promise rests
on this module: a payload leaves one process and arrives in another **as the
types it declares**, not as an equal collection of JSON values. Readers dispatch
on ``OutboundKind`` by identity, read ``chat_id`` off an ``OwnerScope`` property
and read the attachment by attribute, so a decode that hands back plain strings
and mappings is a message silently dropped rather than a loud failure.

Two rules the encode/decode pair keeps:

* **Decode reconstructs through the constructors**, so ``OwnerScope`` validation,
  ``OutboundKind`` membership and the empty-source refusal all re-run on the
  receiving side — the one hop where nobody else is watching.
* **The chat axis is never converted here.** It crosses the wire as the string it
  is in the scope; ``routed_chat_id`` stays the only place it becomes an integer, and
  it becomes one only where the spelling is a canonical decimal.

``ensure_wire_safe`` is the other half: the free-form dicts are open shapes, so a
producer can put a ``Path`` in one and pass every in-process test. Backends call
it from ``submit``/``publish`` and refuse the payload there, where the call site
still names the producer.

What is generic — the unknown-key filter, the declared-type check, the JSON-safety
walk and the container copy — lives in ``ouroboros/contracts/wire.py``, because the
agent-event seam serializes the same way and two copies of a fail-closed check drift
silently. What stays here is what names a seam type.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from typing import Any

from new_core.identity import OwnerScope
from ouroboros.contracts.wire import (
    check_declared_types,
    check_json_safe,
    copied,
    known_fields,
)
from transport.interfaces import (
    AttachmentRef,
    InboundEnvelope,
    OutboundKind,
    OutboundMessage,
)

#: The free-form fields on each payload: open shapes whose contents are checked
#: at hand-over rather than at construction, because they stay mutable on a
#: frozen dataclass.
_INBOUND_FREE_FORM = ("transport", "attachments")
_OUTBOUND_FREE_FORM = ("transport",)


def encode_outbound(message: OutboundMessage) -> dict[str, Any]:
    """The wire form of one published payload."""
    return {field.name: _encode_value(getattr(message, field.name)) for field in fields(message)}


def encode_inbound(envelope: InboundEnvelope) -> dict[str, Any]:
    """The wire form of one submitted envelope."""
    return {field.name: _encode_value(getattr(envelope, field.name)) for field in fields(envelope)}


def decode_outbound(payload: Mapping[str, Any]) -> OutboundMessage:
    """Rebuild a published payload from its wire form, running its validation."""
    known = known_fields(OutboundMessage, payload)
    if "scope" in known:
        known["scope"] = OwnerScope.from_dict(known["scope"])
    if "kind" in known:
        known["kind"] = OutboundKind(known["kind"])
    if "attachment" in known:
        known["attachment"] = _decode_attachment(known["attachment"])
    check_declared_types(OutboundMessage, known)
    return OutboundMessage(**known)


def decode_inbound(payload: Mapping[str, Any]) -> InboundEnvelope:
    """Rebuild a submitted envelope from its wire form, running its validation."""
    known = known_fields(InboundEnvelope, payload)
    if "scope" in known:
        known["scope"] = OwnerScope.from_dict(known["scope"])
    check_declared_types(InboundEnvelope, known)
    return InboundEnvelope(**known)


def ensure_wire_safe(payload: InboundEnvelope | OutboundMessage) -> None:
    """Raise unless every free-form field holds only values a wire format carries.

    Called by a backend from ``submit``/``publish``. Never coerces: a stringified
    value would deliver a wrong answer instead of raising, and the producer that
    put it there is only identifiable from the hand-over call site.
    """
    names = _INBOUND_FREE_FORM if isinstance(payload, InboundEnvelope) else _OUTBOUND_FREE_FORM
    for name in names:
        check_json_safe(getattr(payload, name), name)


def _encode_value(value: Any) -> Any:
    """One field's wire form; the declared types are the only special cases."""
    if isinstance(value, OwnerScope):
        return value.to_dict()
    if isinstance(value, OutboundKind):
        return value.value
    if isinstance(value, AttachmentRef):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    return copied(value)


def _decode_attachment(value: Any) -> AttachmentRef | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"attachment must decode to a mapping, got {type(value).__name__}")
    known = known_fields(AttachmentRef, value)
    check_declared_types(AttachmentRef, known)
    return AttachmentRef(**known)


__all__ = [
    "decode_inbound",
    "decode_outbound",
    "encode_inbound",
    "encode_outbound",
    "ensure_wire_safe",
]
