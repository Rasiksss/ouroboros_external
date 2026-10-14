"""The agent-event wire format: what survives the hop, and what must not.

The consuming side dispatches on the message type and reads the scope as an `OwnerScope`,
so a decode that hands back an equal collection of JSON values is a report silently
mishandled rather than a loud failure. Each case here is one way that happens.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

import pytest

from new_core.identity import OwnerScope
from ouroboros.agent_events.codec import (
    KIND_FIELD,
    PAYLOAD_FIELD,
    UnknownAgentEventKind,
    decode_agent_event,
    encode_agent_event,
    ensure_wire_safe,
    wire_kinds,
)
from ouroboros.agent_events.interfaces import AgentEvent, LiveLogLine, OutboundText
from tests.helpers.shared import TEST_SCOPE

_KINDS = tuple(kind for kind in AgentEvent.__subclasses__() if dataclasses.is_dataclass(kind))

#: Every kind that crosses the wire, pinned by name. A rename is then a visible diff in a
#: test — which is where a decision about what older instances can still read belongs —
#: rather than a message a running deployment quietly drops.
_WIRE_KIND_NAMES = {
    "FileSendRequested",
    "LiveLogLine",
    "LlmUsageReported",
    "OutboundText",
    "OwnerMessageInjected",
    "PhotoSendRequested",
    "SkillLifecycle",
    "SubtaskRequested",
    "TaskCancellationRequested",
    "TaskFinished",
    "TaskHeartbeat",
    "TaskMetrics",
    "TypingStarted",
    "TypingStopped",
}


def _away_from_default(annotation: Any) -> Any:
    """One value per declared type, none of them a dataclass default."""
    text = str(annotation)
    if "dict" in text:
        return {"key": ["a", 1, True, None]}
    if "bool" in text:
        return True
    if "float" in text:
        return 1.5
    if "int" in text:
        return 7
    return "a value no default has"


def _populated(kind: type[AgentEvent]) -> AgentEvent:
    """One report of ``kind`` with every field set away from its default.

    Every field, because a dataclass default makes a decode that dropped a field compare
    equal to one that used the default anyway.
    """
    values = {field.name: _away_from_default(field.type) for field in dataclasses.fields(kind) if field.name != "scope"}
    return kind(scope=TEST_SCOPE, **values)


def test_the_set_of_wire_kinds_is_pinned() -> None:
    assert set(wire_kinds()) == _WIRE_KIND_NAMES
    assert set(wire_kinds()) == {kind.__name__ for kind in _KINDS}


@pytest.mark.parametrize("kind", _KINDS, ids=lambda kind: kind.__name__)
def test_every_kind_round_trips_as_itself(kind: type[AgentEvent]) -> None:
    event = _populated(kind)

    decoded = decode_agent_event(encode_agent_event(event))

    assert type(decoded) is kind
    assert decoded == event
    assert isinstance(decoded.scope, OwnerScope)


def test_the_kind_travels_beside_the_payload_rather_than_inside_it() -> None:
    """A reader looking at `redis-cli` sees the kind without parsing JSON."""
    entry = encode_agent_event(OutboundText(scope=TEST_SCOPE, text="hi"))

    assert entry[KIND_FIELD] == "OutboundText"
    assert set(entry) == {KIND_FIELD, PAYLOAD_FIELD}
    assert "kind" not in entry[PAYLOAD_FIELD], "the kind has one home, not two"


def test_the_scope_returns_as_an_owner_scope() -> None:
    entry = encode_agent_event(OutboundText(scope=TEST_SCOPE, text="hi"))

    decoded = decode_agent_event(entry)

    assert isinstance(decoded.scope, OwnerScope)
    assert decoded.scope == TEST_SCOPE


@pytest.mark.parametrize(
    "scope",
    [
        {"owner_id": "global", "agent_id": "main", "surface_id": "browser"},  # an axis missing
        {**TEST_SCOPE.to_dict(), "conversation_id": "c-1"},  # one axis too many
    ],
)
def test_a_scope_that_is_not_the_four_axes_fails_to_decode(scope: dict[str, str]) -> None:
    """Validation re-runs on the receiving side: the one hop where nobody else is watching."""
    entry = _entry("OutboundText", {"scope": scope, "text": "hi"})

    with pytest.raises(ValueError):
        decode_agent_event(entry)


def test_a_field_a_reader_does_not_know_does_not_stop_delivery() -> None:
    """A newer instance publishing to an older one; instances upgrade one at a time."""
    entry = _entry("OutboundText", {"scope": TEST_SCOPE.to_dict(), "text": "hi", "invented_later": "x"})

    decoded = decode_agent_event(entry)

    assert decoded == OutboundText(scope=TEST_SCOPE, text="hi")


def test_a_known_field_absent_from_the_payload_takes_its_default() -> None:
    entry = _entry("OutboundText", {"scope": TEST_SCOPE.to_dict()})

    decoded = decode_agent_event(entry)

    assert decoded.text == ""
    assert decoded.is_progress is False
    assert decoded.transport == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_progress", "false"),  # every answer becomes a progress tick
        ("transport", "target_uid=alice"),  # a mapping read as a string iterates as characters
        ("task_id", 7),
    ],
)
def test_a_field_of_the_wrong_type_fails_to_decode(field: str, value: Any) -> None:
    entry = _entry("OutboundText", {"scope": TEST_SCOPE.to_dict(), field: value})

    with pytest.raises(TypeError):
        decode_agent_event(entry)


def test_a_kind_the_reader_does_not_know_is_its_own_failure() -> None:
    """Named, so a consumer can tell it from a producer's malformed payload."""
    entry = _entry("SomethingTheReaderHasNeverHeardOf", {"scope": TEST_SCOPE.to_dict()})

    with pytest.raises(UnknownAgentEventKind):
        decode_agent_event(entry)


def test_a_payload_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_agent_event({KIND_FIELD: "OutboundText", PAYLOAD_FIELD: '"just a string"'})


@pytest.mark.parametrize(
    "event",
    [
        OutboundText(scope=TEST_SCOPE, transport={"hint": pathlib.Path("/tmp/x")}),
        LiveLogLine(scope=TEST_SCOPE, data={"when": {"nested": {1, 2}}}),
        OutboundText(scope=TEST_SCOPE, transport={"ratio": float("nan")}),
    ],
    ids=["a path", "a set", "a non-finite float"],
)
def test_a_value_no_wire_format_carries_is_refused_with_its_path(event: AgentEvent) -> None:
    """The error names the field and the path within it, so the producer is identifiable."""
    with pytest.raises(TypeError) as refusal:
        ensure_wire_safe(event)

    assert str(refusal.value).startswith(("transport", "data"))


@pytest.mark.parametrize("kind", _KINDS, ids=lambda kind: kind.__name__)
def test_every_open_shape_on_every_kind_is_checked(kind: type[AgentEvent]) -> None:
    """Derived from the declared types, so a kind that gains a mapping is covered by this.

    A hand-written tuple of field names would be a second source of truth for the set of
    open shapes, and nothing would say the day a kind gained one that the check missed it.
    """
    mappings = [field.name for field in dataclasses.fields(kind) if "dict" in str(field.type)]
    if not mappings:
        ensure_wire_safe(kind(scope=TEST_SCOPE))
        return

    for name in mappings:
        event = kind(scope=TEST_SCOPE, **{name: {"hint": pathlib.Path("/tmp/x")}})
        with pytest.raises(TypeError):
            ensure_wire_safe(event)


def _entry(kind: str, payload: dict[str, Any]) -> dict[str, str]:
    """One wire entry built by hand, as a peer instance would have written it."""
    import json

    return {KIND_FIELD: kind, PAYLOAD_FIELD: json.dumps(payload)}
