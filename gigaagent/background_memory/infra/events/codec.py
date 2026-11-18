"""The memory seam's wire format: one codec, used by every serializing backend.

Beside the transports rather than in ``background_memory/interfaces.py``, because the seam
names no wire format — the same place ``transport/codec.py`` and
``ouroboros/agent_events/codec.py`` hold theirs.

The wire form is one mapping: a ``kind`` tag beside the dataclass fields. The tag is what
lets a reader rebuild the concrete kind, since ``IMemoryEventConsumer.poll`` is typed to
the base and a bare mapping would leave it guessing. It travels *inside* the mapping rather
than as a second stream field — unlike the agent-event codec, whose stream an operator
watches live in ``redis-cli`` — so that encode and decode are inverses over a single value
and a backend chooses for itself how many fields it writes.

Three rules, all of them the type-generic ones ``ouroboros/contracts/wire.py`` owns:

* **Decode reconstructs through the constructors**, so ``OwnerScope`` validation re-runs on
  the receiving side and ``MemoryTaskRef`` arrives as the declared type rather than as an
  equal mapping.
* **A key this build does not declare is dropped**, because instances are upgraded one at a
  time and a rolling deploy must not become dropped events.
* **A field that decodes to the wrong runtime type is refused**: a ``recorded_at`` that
  arrives as a string is a wrong object rather than a failure.

**The registry is written out kind by kind and is never reflected.**
``ConsolidationContinuationEvent`` is deliberately absent: it exists only as a ``FollowUp``
payload the loop delivers itself, and a registry derived from ``MemoryEvent.__subclasses__()``
would enrol it automatically and quietly undo that rule. Encoding one raises, which is what
makes "runtime-internal events never cross the transport" an enforcement point rather than a
convention.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    MemoryDistillationEvent,
    MemoryEvent,
    MemoryTaskRef,
)
from new_core.identity import OwnerScope

# The type-generic half of every seam codec. Not a scope mint and not a settings read:
# `background_memory` may name this module and no other under `ouroboros.contracts`.
from ouroboros.contracts.wire import (
    check_declared_types,
    check_json_safe,
    copied,
    declared_types,
    known_fields,
)

#: The wire key naming the concrete kind, beside the event's own fields.
KIND_FIELD = "kind"

#: Tag to kind, written out rather than derived. Adding a kind here is the deliberate act
#: that gives it a wire form; a kind that belongs to the loop alone is left out.
WIRE_KINDS: dict[str, type[MemoryEvent]] = {
    "MemoryDistillationEvent": MemoryDistillationEvent,
    "ChatMessagePersistedEvent": ChatMessagePersistedEvent,
}


class UnknownMemoryEventKind(LookupError):
    """A kind tag this build does not know — an older reader, a newer publisher.

    Its own type rather than a decode failure, because a consumer answers the two
    differently: an unknown kind is recorded and skipped, while a malformed payload is a
    producer bug worth a traceback.
    """


def encode_memory_event(event: MemoryEvent) -> dict[str, Any]:
    """The wire form of one event, or a ``TypeError`` for one that may not cross.

    Raises for a kind with no wire form, and for a free-form field holding a value no wire
    format carries — both at the hand-over, where the call site still names the producer.
    """
    tag = _wire_tag(type(event))
    _check_free_form(event)
    payload = {field.name: _encode_value(getattr(event, field.name)) for field in dataclasses.fields(event)}
    return {KIND_FIELD: tag, **payload}


def decode_memory_event(payload: Mapping[str, Any]) -> MemoryEvent:
    """Rebuild one event from its wire form, running the kind's own validation.

    Raises ``UnknownMemoryEventKind`` for a kind this build does not know, and ``TypeError``
    or ``ValueError`` for a payload that does not decode as the kind declares.
    """
    tag = str(payload.get(KIND_FIELD) or "")
    kind = WIRE_KINDS.get(tag)
    if kind is None:
        raise UnknownMemoryEventKind(tag)
    known = known_fields(kind, payload)
    if "scope" in known:
        known["scope"] = OwnerScope.from_dict(known["scope"])
    if isinstance(known.get("task"), Mapping):
        task = known_fields(MemoryTaskRef, known["task"])
        check_declared_types(MemoryTaskRef, task)
        known["task"] = MemoryTaskRef(**task)
    check_declared_types(kind, known)
    return kind(**known)


def _wire_tag(kind: type[MemoryEvent]) -> str:
    """The tag ``kind`` crosses as, or a refusal naming the kind that may not cross."""
    for tag, registered in WIRE_KINDS.items():
        if registered is kind:
            return tag
    raise TypeError(
        f"{kind.__name__} has no wire form: it is a runtime-internal kind the loop delivers "
        "itself, and a transport that carried one could drop or replay a half-finished run"
    )


def _check_free_form(event: MemoryEvent) -> None:
    """Raise unless every open shape on ``event`` holds only what a wire format carries.

    Derived from the declared types rather than listing ``llm_trace``: the day a kind gains
    a second open shape, nothing would say the check had missed it. Derived here rather
    than in ``ouroboros/contracts/wire.py`` because that module deliberately names no
    payload type — which fields are free-form is each codec's own knowledge.
    """
    for name, classes in declared_types(type(event)).items():
        if any(isinstance(cls, type) and issubclass(cls, Mapping) for cls in classes):
            check_json_safe(getattr(event, name), name)


def _encode_value(value: Any) -> Any:
    """One field's wire form; the two declared types needing a conversion, and a copy."""
    if isinstance(value, OwnerScope):
        return value.to_dict()
    if isinstance(value, MemoryTaskRef):
        return dataclasses.asdict(value)
    return copied(value)


__all__ = [
    "KIND_FIELD",
    "WIRE_KINDS",
    "UnknownMemoryEventKind",
    "decode_memory_event",
    "encode_memory_event",
]
