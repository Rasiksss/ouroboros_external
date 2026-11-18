"""The agent-event seam's wire format: one codec, used by every serializing backend.

The seam promises that a broker-backed transport replaces the in-process queue without a
call site changing (``ouroboros/agent_events/interfaces.py``). That promise rests on this
module: a report leaves one process and arrives in another **as the kind it was published
as**, with every field as its declared type rather than as an equal collection of JSON
values. The consuming side dispatches on ``type(event)``
(``supervisor/events.py::dispatch_event``), reads ``scope`` as an ``OwnerScope`` at its
first routing decision and reads ``transport`` as a mapping, so a decode that hands back
plain values is a report silently mishandled rather than a loud failure.

Three rules, the first two copied from ``transport/codec.py`` because they are the same
two bugs on a second seam:

* **Decode reconstructs through the constructor**, so ``OwnerScope`` validation re-runs on
  the receiving side — the one hop where nobody else is watching.
* **A field that decodes to the wrong runtime type is refused**, not delivered: a wire
  ``"false"`` where ``is_progress`` is declared ``bool`` turns every answer into a progress
  tick.
* **The kind travels beside the payload rather than inside it**, because a stream entry is
  a field/value map and a reader looking at ``redis-cli`` should see the kind without
  parsing JSON. It has one home, not two.

The name-to-kind mapping is derived from ``AgentEvent.__subclasses__()`` so the kind set
has a single source in ``interfaces.py``; a test pins the exact set of names, which is what
makes a class rename a visible diff rather than a message an older reader silently drops.

``ensure_wire_safe`` is the other half: the free-form mappings are open shapes, so a
producer can put a ``Path`` in one and pass every in-process test. Every publisher calls
it — not only the one that serializes — so a producer fails in the process that put the
value there rather than on the day a wire is underneath.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any

from new_core.identity import OwnerScope
from ouroboros.agent_events.interfaces import AgentEvent
from ouroboros.contracts.wire import (
    check_declared_types,
    check_json_safe,
    copied,
    declared_types,
    known_fields,
)

#: The stream entry's two fields: the kind a reader dispatches on, and the payload.
KIND_FIELD = "kind"
PAYLOAD_FIELD = "payload"


class UnknownAgentEventKind(LookupError):
    """A kind this build does not know — an older reader, a newer publisher.

    Its own type rather than a decode failure, because a consumer answers the two
    differently: an unknown kind is recorded and skipped, while a malformed payload is a
    producer bug worth an error and a traceback.
    """


def wire_kinds() -> dict[str, type[AgentEvent]]:
    """Every kind on the seam, keyed by the name that crosses the wire.

    Built on each call rather than cached at import: a kind declared after this module
    loaded — a test's own, a later import — must encode as itself rather than as a kind
    this mapping never learned. Small enough that the cost is one dict comprehension.
    """
    return {kind.__name__: kind for kind in AgentEvent.__subclasses__() if dataclasses.is_dataclass(kind)}


def encode_agent_event(event: AgentEvent) -> dict[str, str]:
    """The wire form of one report: the kind, and the payload as JSON."""
    payload = {field.name: _encode_value(getattr(event, field.name)) for field in dataclasses.fields(event)}
    return {KIND_FIELD: type(event).__name__, PAYLOAD_FIELD: json.dumps(payload)}


def decode_agent_event(entry: Mapping[str, Any]) -> AgentEvent:
    """Rebuild one report from its wire form, running the kind's own validation.

    Raises ``UnknownAgentEventKind`` for a kind this build does not know, and ``TypeError``
    or ``ValueError`` for a payload that does not decode as the kind declares.
    """
    name = str(entry.get(KIND_FIELD) or "")
    kind = wire_kinds().get(name)
    if kind is None:
        raise UnknownAgentEventKind(name)
    payload = json.loads(entry.get(PAYLOAD_FIELD) or "null")
    if not isinstance(payload, Mapping):
        raise TypeError(f"{name} payload must decode to a mapping, got {type(payload).__name__}")
    known = known_fields(kind, payload)
    if "scope" in known:
        known["scope"] = OwnerScope.from_dict(known["scope"])
    check_declared_types(kind, known)
    return kind(**known)


def ensure_wire_safe(event: AgentEvent) -> None:
    """Raise unless every free-form mapping holds only values a wire format carries.

    Called by a publisher from ``publish``. Never coerces: a stringified value would
    deliver a wrong report instead of raising, and the producer that put it there is only
    identifiable from the hand-over call site.
    """
    for name in _free_form_fields(type(event)):
        check_json_safe(getattr(event, name), name)


def _free_form_fields(kind: Any) -> tuple[str, ...]:
    """The kind's fields whose declared type is a mapping — its open shapes.

    Derived rather than listed: a hand-written tuple is a second source of truth for the
    set of open shapes, and the day a kind gains one nothing would say the check missed it.

    Not memoized on the kind, though the walk it does is: a cache keyed on the class would
    hold the last reference to it, and a kind that cannot die never leaves
    ``AgentEvent.__subclasses__()`` — which is what ``wire_kinds`` reads. The scan below is
    a genexp over the ten fields ``declared_types`` already resolved.
    """
    return tuple(
        name
        for name, classes in declared_types(kind).items()
        if any(isinstance(cls, type) and issubclass(cls, Mapping) for cls in classes)
    )


def _encode_value(value: Any) -> Any:
    """One field's wire form; ``scope`` is the only declared type needing a conversion."""
    if isinstance(value, OwnerScope):
        return value.to_dict()
    return copied(value)


__all__ = [
    "KIND_FIELD",
    "PAYLOAD_FIELD",
    "UnknownAgentEventKind",
    "decode_agent_event",
    "encode_agent_event",
    "ensure_wire_safe",
    "wire_kinds",
]
