"""The memory seam's wire format: the round trip, the refusals, and the missing kind.

The registry is what makes ``ConsolidationContinuationEvent`` unpublishable rather than
merely unpublished, so the cases below pin both halves of that: the kinds that have a wire
form, and the one that has none.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from background_memory import interfaces as memory_interfaces
from background_memory.infra.events import codec as codec_mod
from background_memory.infra.events.codec import (
    KIND_FIELD,
    WIRE_KINDS,
    UnknownMemoryEventKind,
    decode_memory_event,
    encode_memory_event,
)
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    ConsolidationContinuationEvent,
    MemoryDistillationEvent,
    MemoryEvent,
    MemoryTaskRef,
)
from new_core.identity import OwnerScope
from tests.helpers.shared import TEST_SCOPE


def _event(task_id: str = "src1") -> MemoryDistillationEvent:
    return MemoryDistillationEvent(
        scope=TEST_SCOPE,
        task=MemoryTaskRef(job_id="job1", task_id=task_id, task_type="task", text="do it", recorded_at=1.5),
        response_text="A" * 200,
        llm_trace={"tool_calls": []},
    )


# -- the round trip -------------------------------------------------------


def test_an_event_round_trips_as_its_own_kind() -> None:
    """Equality plus the declared types: a decode handing back mappings is a wrong object."""
    event = _event()

    decoded = decode_memory_event(encode_memory_event(event))

    assert decoded == event
    assert isinstance(decoded, MemoryDistillationEvent)
    assert isinstance(decoded.scope, OwnerScope)
    assert isinstance(decoded.task, MemoryTaskRef)
    assert decoded.task.recorded_at == 1.5
    assert decoded.task.job_id == "job1"
    assert isinstance(decoded.task.recorded_at, float)


def test_the_other_published_kind_round_trips_too() -> None:
    event = ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id="message-0001")

    decoded = decode_memory_event(encode_memory_event(event))

    assert decoded == event
    assert isinstance(decoded, ChatMessagePersistedEvent)


def test_the_free_form_trace_survives_intact_and_is_not_aliased() -> None:
    """``llm_trace`` is the one open shape; a decoded payload must not point into the wire."""
    event = _event()
    event.llm_trace["tool_calls"].append({"name": "recall", "ok": True})

    wire = encode_memory_event(event)
    decoded = decode_memory_event(wire)

    assert isinstance(decoded, MemoryDistillationEvent)
    assert decoded.llm_trace == {"tool_calls": [{"name": "recall", "ok": True}]}
    assert decoded.llm_trace is not wire["llm_trace"]


# -- the kind with no wire form -------------------------------------------


def test_encoding_the_runtime_internal_kind_raises() -> None:
    """The enforcement point for "runtime-internal events never cross the transport"."""
    with pytest.raises(TypeError, match="ConsolidationContinuationEvent"):
        encode_memory_event(ConsolidationContinuationEvent(scope=TEST_SCOPE))


def test_the_registry_is_written_out_rather_than_reflected() -> None:
    """Reflection would enrol the continuation automatically and undo the rule above.

    Parsed rather than asserted on the value alone: a dict built from
    ``MemoryEvent.__subclasses__()`` would satisfy any equality check written here on the
    day it is written and enrol the next kind silently.
    """
    tree = ast.parse(pathlib.Path(codec_mod.__file__).read_text(encoding="utf-8"))
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "WIRE_KINDS"
        and isinstance(node.value, ast.Dict)
    ]

    assert assignments, "the registry must be a dict literal, kind by kind"
    assert all(isinstance(key, ast.Constant) for key in assignments[0].value.keys)
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "__subclasses__"]
    assert set(WIRE_KINDS) == {"MemoryDistillationEvent", "ChatMessagePersistedEvent"}


def test_every_declared_kind_but_the_continuation_has_a_wire_form() -> None:
    """A kind added to the seam and registered nowhere would be dropped at the publisher."""
    declared = {
        kind
        for name in memory_interfaces.__all__
        if isinstance(kind := getattr(memory_interfaces, name), type)
        and issubclass(kind, MemoryEvent)
        and kind is not MemoryEvent
    }

    assert declared - set(WIRE_KINDS.values()) == {ConsolidationContinuationEvent}


# -- what a reader does with a payload it cannot use ----------------------


def test_an_unknown_key_is_dropped_rather_than_refused() -> None:
    """An older reader against a newer publisher: a rolling deploy must not lose events."""
    wire = encode_memory_event(_event())
    wire["a_field_this_build_never_declared"] = "whatever"

    decoded = decode_memory_event(wire)

    assert decoded == _event()


def test_a_field_of_the_wrong_declared_type_is_refused() -> None:
    """A ``recorded_at`` that arrives as a string is a wrong object, not a failure."""
    wire = encode_memory_event(_event())
    wire["task"]["recorded_at"] = "1.5"

    with pytest.raises(TypeError, match="recorded_at"):
        decode_memory_event(wire)


def test_a_top_level_field_of_the_wrong_declared_type_is_refused() -> None:
    wire = encode_memory_event(ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id="m1"))
    wire["message_id"] = 17

    with pytest.raises(TypeError, match="message_id"):
        decode_memory_event(wire)


def test_a_kind_tag_this_build_does_not_know_raises_its_own_error() -> None:
    """Its own type: a consumer skips an unknown kind and errors on a malformed payload."""
    wire = encode_memory_event(_event())
    wire[KIND_FIELD] = "SomethingTheReaderHasNeverHeardOf"

    with pytest.raises(UnknownMemoryEventKind):
        decode_memory_event(wire)


def test_a_scope_that_does_not_validate_is_refused_on_the_receiving_side() -> None:
    """Decode reconstructs through ``OwnerScope.from_dict``, so validation re-runs here."""
    wire = encode_memory_event(_event())
    wire["scope"] = {**wire["scope"], "owner_id": ""}

    with pytest.raises((TypeError, ValueError)):
        decode_memory_event(wire)


# -- the free-form field is checked with the shared walk ------------------


def test_a_value_no_wire_format_carries_is_refused_at_encode() -> None:
    """The shared walk, not ``json.dumps``: a tuple and a non-string key are refusals here."""
    event = MemoryDistillationEvent(scope=TEST_SCOPE, llm_trace={"path": pathlib.Path("/tmp/x")})

    with pytest.raises(TypeError, match="llm_trace"):
        encode_memory_event(event)


def test_a_tuple_in_the_free_form_field_is_refused_rather_than_coerced() -> None:
    """``json.dumps`` would write it as a list and return an event unequal to the published one."""
    with pytest.raises(TypeError):
        encode_memory_event(MemoryDistillationEvent(scope=TEST_SCOPE, llm_trace={"rounds": (1, 2)}))


def test_a_non_string_key_in_the_free_form_field_is_refused() -> None:
    with pytest.raises(TypeError):
        encode_memory_event(MemoryDistillationEvent(scope=TEST_SCOPE, llm_trace={1: "one"}))


# -- what the codec may not reach ----------------------------------------


def test_the_codec_reaches_no_handler_and_no_runtime() -> None:
    """The same check the in-process sibling is held to."""
    imported: list[str] = []
    for node in ast.walk(ast.parse(pathlib.Path(codec_mod.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert not [name for name in imported if "handlers" in name or "runtime" in name], imported
    assert not [name for name in imported if name == "redis" or name.endswith(".redis_transport")], (
        "the codec is the wire format of every serializing backend, so it names none of them"
    )
    assert not [
        name for name in imported if name == "kafka" or name.startswith("kafka.") or name.endswith(".kafka_transport")
    ], "the codec is the wire format of every serializing backend, so it names none of them"
