"""The seam a run reports through: the interfaces, the messages, and the transport.

Every case here is a property the seam has to keep when the transport becomes a broker
and the consumer becomes another process. None of them is about what a handler does with
a message — that is the handlers' own tests.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from ouroboros.agent_events.inmemory import (
    MAX_PENDING_EVENTS,
    InMemoryAgentEventConsumer,
    InMemoryAgentEventPublisher,
    create_agent_event_transport,
)
from ouroboros.agent_events.interfaces import (
    AgentEvent,
    IAgentEventConsumer,
    IAgentEventPublisher,
    OutboundText,
    TaskHeartbeat,
)
from tests.helpers.agent_event_conformance import AgentEventConformance
from tests.helpers.shared import TEST_SCOPE

#: Every kind on the seam, so a kind added later is checked by all of these at once.
_KINDS = tuple(kind for kind in AgentEvent.__subclasses__() if dataclasses.is_dataclass(kind))

#: Names that would make a message unserializable if one showed up in an annotation.
_LIVE_OBJECT_NAMES = ("Queue", "Callable", "Store", "Agent", "Connection", "Bridge", "Publisher", "Consumer")


def test_every_kind_is_covered_by_this_module() -> None:
    """A guard on the guard: an empty kind list would make every case below vacuous."""
    assert len(_KINDS) >= 10


# --- the two interfaces -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("interface", "missing"),
    [(IAgentEventPublisher, "publish"), (IAgentEventConsumer, "poll")],
)
def test_an_incomplete_implementation_is_rejected(interface, missing) -> None:
    incomplete = type("Incomplete", (interface,), {})

    with pytest.raises(TypeError, match=missing):
        incomplete()


# --- the messages -----------------------------------------------------------------------


@pytest.mark.parametrize("kind", _KINDS, ids=lambda kind: kind.__name__)
def test_a_message_cannot_be_constructed_without_a_scope(kind) -> None:
    with pytest.raises(TypeError):
        kind()


@pytest.mark.parametrize("kind", _KINDS, ids=lambda kind: kind.__name__)
def test_identity_is_declared_once_on_the_base(kind) -> None:
    own_fields = {field.name for field in dataclasses.fields(kind)} - {
        field.name for field in dataclasses.fields(AgentEvent)
    }

    assert "scope" in {field.name for field in dataclasses.fields(AgentEvent)}
    assert not own_fields & {"scope", "chat_id", "owner_id", "agent_id", "surface_id"}


@pytest.mark.parametrize("kind", _KINDS, ids=lambda kind: kind.__name__)
def test_every_message_is_serializable(kind) -> None:
    """Each field is a value a transport can carry: no handle, no callable, no store."""
    event = kind(scope=TEST_SCOPE)

    for field in dataclasses.fields(kind):
        annotation = str(field.type)
        assert not any(name in annotation for name in _LIVE_OBJECT_NAMES), f"{kind.__name__}.{field.name}"
        if field.name == "scope":
            continue
        json.dumps(getattr(event, field.name))


@pytest.mark.parametrize("kind", _KINDS, ids=lambda kind: kind.__name__)
def test_every_message_is_frozen(kind) -> None:
    event = kind(scope=TEST_SCOPE)

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.scope = TEST_SCOPE  # type: ignore[misc]


def test_a_new_report_kind_needs_no_interface_change() -> None:
    """A kind declared here, in a test, crosses the seam without either interface moving."""

    @dataclasses.dataclass(frozen=True)
    class SomethingNew(AgentEvent):
        detail: str = ""

    publisher, consumer = create_agent_event_transport()
    publisher.publish(SomethingNew(scope=TEST_SCOPE, detail="hello"))

    assert consumer.poll() == SomethingNew(scope=TEST_SCOPE, detail="hello")


def test_even_the_publisher_that_delivers_nothing_refuses_an_unwireable_value() -> None:
    """Delivering nothing is not accepting anything.

    A producer whose free-form mapping holds a path would otherwise pass every test that
    runs against this publisher and fail on the first real transport — the exact failure
    the check exists to prevent, and the reason the seam asks every publisher for it.
    """
    import pathlib

    from ouroboros.agent_events.interfaces import NullAgentEventPublisher

    with pytest.raises(TypeError):
        NullAgentEventPublisher().publish(OutboundText(scope=TEST_SCOPE, transport={"hint": pathlib.Path("/tmp/x")}))
    NullAgentEventPublisher().publish(OutboundText(scope=TEST_SCOPE, transport={"target_uid": "alice"}))


def test_a_droppable_kind_says_so_itself() -> None:
    assert TaskHeartbeat.droppable is True
    assert OutboundText.droppable is False
    # Declared on the kind rather than on the base's default for every kind.
    assert AgentEvent.droppable is False


# --- the transport ----------------------------------------------------------------------


class TestInProcessTransport(AgentEventConformance):
    """The in-process backend, held to the contract every backend behind the seam keeps."""

    #: Lowered by `shrink_capacity` for the cases about the bound; a fresh instance per case.
    capacity = MAX_PENDING_EVENTS

    def make_transport(self):
        return create_agent_event_transport(maxsize=self.capacity)

    def shrink_capacity(self, monkeypatch) -> int:
        """The bound is the queue's own, so the case builds a smaller queue."""
        del monkeypatch
        self.capacity = 1
        return self.capacity

    def preserves_order_within_one_scope(self) -> bool:
        """One FIFO queue in one process: nothing can reorder it."""
        return True


@pytest.mark.parametrize(
    ("module", "allowed"),
    [
        ("ouroboros/agent_events/inmemory.py", ["ouroboros.agent_events.codec", "ouroboros.agent_events.interfaces"]),
        (
            "ouroboros/agent_events/redis_transport.py",
            [
                "ouroboros.agent_events.codec",
                "ouroboros.agent_events.interfaces",
                # The reply shapes, shared with the message gateway rather than copied. A
                # contract, which is the one thing below this seam a backend may reach for.
                "ouroboros.contracts.stream_replies",
            ],
        ),
        (
            "ouroboros/agent_events/kafka_transport.py",
            ["ouroboros.agent_events.codec", "ouroboros.agent_events.interfaces", "ouroboros.contracts.stream_replies"],
        ),
    ],
)
def test_the_transport_does_not_reach_the_business_layer(module: str, allowed: list[str]) -> None:
    import ast
    import pathlib

    imported: list[str] = []
    for node in ast.walk(ast.parse(pathlib.Path(module).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert not [name for name in imported if name.startswith(("ouroboros.agent.", "supervisor"))]
    assert sorted(name for name in imported if name.startswith("ouroboros.")) == allowed


def test_the_two_halves_share_one_transport() -> None:
    publisher, consumer = create_agent_event_transport()

    assert isinstance(publisher, InMemoryAgentEventPublisher)
    assert isinstance(consumer, InMemoryAgentEventConsumer)
    publisher.publish(TaskHeartbeat(scope=TEST_SCOPE, task_id="t-1"))
    assert consumer.poll() is not None
