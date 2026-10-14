"""The transport-test fake is held to the same contract as production.

A fake that quietly disagrees about redelivery or stalling would let a transport
test pass against behavior the real gateway does not have.
"""

from __future__ import annotations

import pytest

from tests.helpers.fake_gateway import FakeGateway, FakeInbox, RecordingOutbox
from tests.helpers.scope import scope_for
from tests.helpers.transport_conformance import (
    POLL_TIMEOUT_SEC,
    InboxConformance,
    OutboxConformance,
)
from transport.interfaces import IInbox, IMessageGateway, InboundEnvelope, IOutbox, SubscriptionMode


class TestRecordingOutbox(OutboxConformance):
    def make_outbox(self) -> RecordingOutbox:
        return RecordingOutbox()

    def supports_shared_subscriptions(self) -> bool:
        return True

    def break_publish(self, outbox: RecordingOutbox) -> bool:
        """The fake wraps the real outbox, so it can fail the same append."""

        def _refuse(_message: object) -> None:
            raise RuntimeError("the outbox is not taking messages")

        outbox._outbox._offer_to_all = _refuse  # type: ignore[method-assign]
        return True

    def shrink_retention(self, outbox: RecordingOutbox, monkeypatch) -> int:
        """Same bound as the outbox it wraps, so the case is not vacuous here either."""
        del outbox
        monkeypatch.setattr("transport.infra.inmemory._MAX_PENDING_PER_SUBSCRIPTION", 3)
        return 3


class TestFakeInbox(InboxConformance):
    def make_inbox(self) -> FakeInbox:
        return FakeInbox()

    def replacement_consumer(self, inbox: FakeInbox) -> None:
        """The fake is in-process too: nothing outlives the consumer that stopped."""
        del inbox
        return None


def test_the_fake_implements_the_seam():
    gateway = FakeGateway()

    assert isinstance(gateway, IMessageGateway)
    assert isinstance(gateway.inbox, IInbox)
    assert isinstance(gateway.outbox, IOutbox)


def test_a_transport_submission_is_visible_to_the_test():
    gateway = FakeGateway()
    envelope = InboundEnvelope(scope=scope_for("browser", 1), user_id=1, text="hello")

    gateway.inbox.submit(envelope)

    assert gateway.submitted == [envelope]


def test_polling_the_fake_inbox_leaves_the_record_of_what_was_submitted():
    """The fake reads in order without consuming, so assertions still see everything."""
    gateway = FakeGateway()
    first = InboundEnvelope(scope=scope_for("browser", 1), user_id=1, text="first")
    second = InboundEnvelope(scope=scope_for("browser", 1), user_id=1, text="second")

    gateway.inbox.submit(first)
    gateway.inbox.submit(second)

    assert gateway.inbox.poll() is first
    assert gateway.inbox.poll(POLL_TIMEOUT_SEC) is second
    assert gateway.inbox.poll() is None
    assert gateway.submitted == [first, second]


def test_a_refusing_inbox_raises_so_the_caller_can_retry():
    gateway = FakeGateway()
    gateway.inbox.fail_with(RuntimeError("queue unavailable"))

    with pytest.raises(RuntimeError, match="queue unavailable"):
        gateway.inbox.submit(InboundEnvelope(scope=scope_for("browser", 1), text="hello"))

    gateway.inbox.fail_with(None)
    gateway.inbox.submit(InboundEnvelope(scope=scope_for("browser", 1), text="hello"))
    assert len(gateway.submitted) == 1


def test_the_test_can_answer_as_the_supervisor():
    gateway = FakeGateway()
    reader = gateway.outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        published = gateway.answer(1, "the answer", source="browser")

        delivered = reader.poll(POLL_TIMEOUT_SEC)
        assert delivered is not None
        assert delivered.message == published
        assert gateway.published == [published]
        reader.ack(delivered)
    finally:
        reader.close()
