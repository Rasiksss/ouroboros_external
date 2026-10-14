"""Conformance tests for ``ouroboros.agent_events.interfaces``: the publisher and consumer.

Written against the interfaces, not an implementation: the properties here are the ones the
seam has to keep when the transport becomes a broker and the consumer becomes another
process, and every backend behind the seam is expected to pass this same suite.

To run it against an implementation, subclass it in a test module and implement its
factory::

    class TestMyTransport(AgentEventConformance):
        def make_transport(self):
            return MyPublisher(...), MyConsumer(...)

The class name here deliberately lacks a ``Test`` prefix so pytest collects the cases only
through a subclass that can actually build the thing under test.
"""

from __future__ import annotations

import pathlib
import threading
import time

import pytest

from ouroboros.agent_events.interfaces import (
    AgentEvent,
    IAgentEventConsumer,
    IAgentEventPublisher,
    OutboundText,
    TaskHeartbeat,
)
from tests.helpers.shared import TEST_SCOPE, scope_with

# Enough for a publishing thread to reach its blocked publish, short enough that a broken
# implementation fails the suite quickly rather than hanging it.
POLL_TIMEOUT_SEC = 0.5


class AgentEventConformance:
    """The contract every backend behind the agent-event seam must satisfy."""

    def make_transport(self) -> tuple[IAgentEventPublisher, IAgentEventConsumer]:
        raise NotImplementedError("conformance subclasses must build a publisher and a consumer")

    def shrink_capacity(self, monkeypatch: pytest.MonkeyPatch) -> int | None:
        """Lower the bound the next transport is built with, or ``None`` if it cannot.

        Without it the back-pressure cases would have to publish a backend's whole bound to
        reach it, which turns the suite into a load test and is vacuous on any backend whose
        bound is large.
        """
        del monkeypatch
        return None

    def preserves_order_within_one_scope(self) -> bool:
        """Whether this backend delivers one scope's messages in publication order.

        Declared, never inferred, which is why this raises instead of defaulting: a backend
        that delivers to competing consumers across processes cannot preserve it and must
        say so — but a backend that can order by the partition key must not inherit the
        exemption by forgetting to answer.
        """
        raise NotImplementedError("conformance subclasses must declare whether they preserve per-scope order")

    # -- what a transport does with nothing in it --------------------------

    def test_an_empty_transport_returns_nothing_immediately(self) -> None:
        _publisher, consumer = self.make_transport()

        started = time.monotonic()
        assert consumer.poll() is None
        assert time.monotonic() - started < POLL_TIMEOUT_SEC

    def test_a_waiting_poll_returns_nothing_after_its_timeout(self) -> None:
        """The timeout is what saves a polling loop from needing its own sleep."""
        _publisher, consumer = self.make_transport()

        started = time.monotonic()
        assert consumer.poll(timeout=POLL_TIMEOUT_SEC) is None
        elapsed = time.monotonic() - started

        assert elapsed >= POLL_TIMEOUT_SEC * 0.5
        assert elapsed < POLL_TIMEOUT_SEC * 8

    # -- identity and order ------------------------------------------------

    def test_one_scopes_messages_arrive_in_order(self) -> None:
        publisher, consumer = self.make_transport()
        if not self.preserves_order_within_one_scope():
            pytest.skip(f"{type(publisher).__name__} declares that it does not preserve per-scope delivery order")
        published = [OutboundText(scope=TEST_SCOPE, text=str(index)) for index in range(20)]

        for event in published:
            publisher.publish(event)

        assert [consumer.poll(POLL_TIMEOUT_SEC) for _ in published] == published

    def test_every_published_message_is_delivered(self) -> None:
        """Order is what a multi-reader backend may deviate on; delivery is not.

        Without this the only case publishing more than one report is the ordering one, and
        a backend that declares the deviation would have its whole drain untested — a poll
        that lost or skipped an entry would pass the suite.
        """
        publisher, consumer = self.make_transport()
        published = [OutboundText(scope=TEST_SCOPE, text=str(index)) for index in range(20)]

        for event in published:
            publisher.publish(event)

        delivered: list[AgentEvent] = []
        while (event := consumer.poll(POLL_TIMEOUT_SEC)) is not None:
            delivered.append(event)

        assert sorted(item.text for item in delivered) == sorted(item.text for item in published)

    def test_the_scope_is_the_ordering_key(self) -> None:
        """Asked what it orders by, the transport reads the base type and names no kind."""
        publisher, consumer = self.make_transport()
        other = scope_with(TEST_SCOPE, owner_id="someone-else")

        for event in (OutboundText(scope=TEST_SCOPE), TaskHeartbeat(scope=other)):
            assert publisher.partition_key(event) is event.scope
            assert consumer.partition_key(event) is event.scope

    # -- what the report is when it arrives --------------------------------

    def test_a_published_message_round_trips_intact(self) -> None:
        """Equality, never identity: a backend spanning processes cannot hand back the object."""
        publisher, consumer = self.make_transport()
        event = OutboundText(scope=TEST_SCOPE, text="answer", transport={"target_uid": "alice"})

        publisher.publish(event)
        polled = consumer.poll(POLL_TIMEOUT_SEC)

        assert isinstance(polled, OutboundText)
        assert polled == event
        assert polled.transport == {"target_uid": "alice"}

    def test_the_transport_refuses_a_value_no_wire_format_carries(self) -> None:
        """On every backend, not the serializing one alone.

        A producer whose free-form mapping holds a path must fail in the process that put it
        there, not on the day a wire is underneath.
        """
        publisher, consumer = self.make_transport()
        unsafe = OutboundText(scope=TEST_SCOPE, text="hi", transport={"hint": pathlib.Path("/tmp/x")})

        with pytest.raises(TypeError):
            publisher.publish(unsafe)

        assert consumer.poll() is None, "a refused report must not be queued"

    # -- the bound ---------------------------------------------------------

    def test_a_full_transport_blocks_rather_than_dropping_an_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bound = self.shrink_capacity(monkeypatch)
        if bound is None:
            pytest.skip(f"{type(self).__name__} cannot lower its capacity")
        publisher, consumer = self.make_transport()
        for index in range(bound):
            publisher.publish(OutboundText(scope=TEST_SCOPE, text=f"first-{index}"))
        delivered = threading.Event()

        def _publish_the_answer() -> None:
            publisher.publish(OutboundText(scope=TEST_SCOPE, text="the answer"))
            delivered.set()

        run = threading.Thread(target=_publish_the_answer, name="conformance-agent-event-publish", daemon=True)
        run.start()
        try:
            assert not delivered.wait(timeout=POLL_TIMEOUT_SEC), (
                "a full transport must hold the run, not drop its answer"
            )
            drained = [consumer.poll(POLL_TIMEOUT_SEC) for _ in range(bound)]
            assert [event for event in drained if event is not None]
            assert delivered.wait(timeout=POLL_TIMEOUT_SEC * 20)

            assert consumer.poll(POLL_TIMEOUT_SEC).text == "the answer"
        finally:
            run.join(timeout=POLL_TIMEOUT_SEC * 4)

    def test_a_full_transport_drops_a_droppable_kind_without_blocking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bound = self.shrink_capacity(monkeypatch)
        if bound is None:
            pytest.skip(f"{type(self).__name__} cannot lower its capacity")
        publisher, consumer = self.make_transport()
        for index in range(bound):
            publisher.publish(OutboundText(scope=TEST_SCOPE, text=f"the answer {index}"))

        publisher.publish(TaskHeartbeat(scope=TEST_SCOPE, task_id="t-1"))  # must not block

        polled: list[AgentEvent] = []
        while (event := consumer.poll(POLL_TIMEOUT_SEC if not polled else 0.0)) is not None:
            polled.append(event)
        assert [type(event) for event in polled] == [OutboundText] * bound, "the beat must never have been queued"


__all__ = ["POLL_TIMEOUT_SEC", "AgentEventConformance"]
