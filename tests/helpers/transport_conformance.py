"""Conformance tests for ``transport.interfaces``: ``IOutbox`` and ``IInbox``.

Written against the interfaces, not an implementation: every semantic that only
matters for a broker — offsets, redelivery, ``SHARED`` versus ``BROADCAST``, a
poll that actually blocks — would otherwise be untested by construction, because
the in-process backend is the only one that ships with the seam. A broker-backed
gateway, and the test fakes, are expected to pass this same suite unchanged.

To run it against an implementation, subclass the conformance class in a test
module and implement its factory::

    class TestMyOutbox(OutboxConformance):
        def make_outbox(self):
            return MyOutbox()

The class names here deliberately lack a ``Test`` prefix so pytest collects the
cases only through a subclass that can actually build the thing under test.
"""

from __future__ import annotations

import pathlib
import threading
import time

import pytest

from new_core.identity import OwnerScope
from tests.helpers.scope import scope_for
from transport.interfaces import (
    AttachmentRef,
    IInbox,
    InboundEnvelope,
    IOutbox,
    IOutboxReader,
    OutboundKind,
    OutboundMessage,
    SubscriptionMode,
)

# Enough for a reader thread to reach its blocking poll, short enough that a
# broken implementation fails the suite quickly rather than hanging it.
POLL_TIMEOUT_SEC = 0.5


def message(text: str, *, chat_id: int | str = 1, source: str = "browser", **kwargs) -> OutboundMessage:
    kwargs.setdefault("scope", scope_for(source, chat_id))
    return OutboundMessage(source=source, text=text, **kwargs)


def envelope(text: str, *, chat_id: int | str = 1, source: str = "browser", **kwargs) -> InboundEnvelope:
    return InboundEnvelope(scope=scope_for(source, chat_id), user_id=chat_id, text=text, **kwargs)


def full_message(chat_id: int | str = 4242) -> OutboundMessage:
    """One outbound payload with every field set away from its default.

    Every field, because a dataclass default can make a decode that dropped a
    field compare equal to a payload that used the default anyway.
    """
    return OutboundMessage(
        scope=scope_for("browser", chat_id),
        source="browser",
        kind=OutboundKind.PHOTO,
        text="the answer",
        action="typing",
        caption="a photo of it",
        is_progress=True,
        markdown=True,
        task_id="task-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        ts="2026-08-25T10:00:00Z",
        transport={"reply_to": 17, "route": {"tags": ["a", "b"], "retries": 2, "quiet": False}},
        attachment=AttachmentRef(attachment_id="att-1", filename="cat.png", mime="image/png", size=1024),
    )


def full_envelope(chat_id: int | str = 4242) -> InboundEnvelope:
    """One inbound payload with every field set away from its default."""
    return InboundEnvelope(
        scope=scope_for("browser", chat_id),
        user_id=7,
        text="hello",
        sender_label="Anton",
        sender_session_id="sess-1",
        client_message_id="cmid-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        transport={"source_message_id": "42"},
        image_base64="Zm9v",
        image_mime="image/png",
        image_caption="a caption",
        attachment_ids=["att-1", "att-2"],
        attachments=[{"attachment_id": "att-1", "size": 3}],
        suppress_chat_log=True,
        confirmation_id="ab" * 16,
        confirmation_decision="approved",
    )


class InboxConformance:
    """The contract every ``IInbox`` implementation must satisfy."""

    def make_inbox(self) -> IInbox:
        raise NotImplementedError("conformance subclasses must build an inbox")

    def replacement_consumer(self, inbox: IInbox) -> IInbox | None:
        """A fresh consumer over the same backend, or ``None`` if there can be none.

        Declared, never inferred, which is why this raises instead of defaulting:
        a backend that can span processes must not inherit the skip by forgetting
        to override it. A backend whose queue dies with its consumer returns
        ``None`` and the redelivery case skips naming it; a deleted case would
        leave the first cross-process implementation untested by construction.
        """
        del inbox
        raise NotImplementedError("conformance subclasses must declare whether a replacement consumer can exist")

    def test_the_next_poll_settles_the_previous_envelope(self) -> None:
        """Settlement is `poll`'s second job: there is no method that could do it."""
        inbox = self.make_inbox()
        inbox.submit(envelope("first", client_message_id="cmid-1"))
        inbox.submit(envelope("second", client_message_id="   "))

        first = inbox.poll(POLL_TIMEOUT_SEC)
        assert first is not None and first.client_message_id == "cmid-1"
        second = inbox.poll(POLL_TIMEOUT_SEC)
        assert (
            second is not None
            and second.client_message_id.strip()
            and second.client_message_id != first.client_message_id
        )

        assert inbox.poll(POLL_TIMEOUT_SEC) is None, "a settled envelope must never come back"

    def test_an_envelope_a_consumer_never_settled_reaches_its_replacement(self) -> None:
        inbox = self.make_inbox()
        submitted = envelope("taken but not handled", client_message_id="")
        inbox.submit(submitted)
        taken = inbox.poll(POLL_TIMEOUT_SEC)
        assert taken is not None and taken.client_message_id
        assert taken.client_message_id == submitted.client_message_id

        replacement = self.replacement_consumer(inbox)
        if replacement is None:
            pytest.skip(f"{type(inbox).__name__} cannot outlive its consumer, so there is nobody to redeliver to")

        redelivered = replacement.poll(POLL_TIMEOUT_SEC)
        assert redelivered is not None
        assert redelivered.client_message_id == taken.client_message_id

    def test_a_zero_timeout_does_not_block(self) -> None:
        """The drain takes everything after its first message with `poll(0.0)`."""
        inbox = self.make_inbox()

        started = time.monotonic()
        assert inbox.poll(0.0) is None
        assert time.monotonic() - started < POLL_TIMEOUT_SEC, "a zero timeout must not wait"

        inbox.submit(envelope("available now", client_message_id="cmid-1"))
        polled = inbox.poll(0.0)
        assert polled is not None and polled.client_message_id == "cmid-1"

    def test_submissions_are_polled_in_the_order_they_arrived(self) -> None:
        inbox = self.make_inbox()
        first, second = envelope("first"), envelope("second")

        inbox.submit(first)
        inbox.submit(second)

        # By value, not by identity: the in-process backend hands back the object
        # it was given and a backend spanning processes cannot.
        assert inbox.poll(POLL_TIMEOUT_SEC) == first
        assert inbox.poll(POLL_TIMEOUT_SEC) == second
        assert inbox.poll() is None

    def test_polling_an_empty_inbox_waits_out_the_timeout_and_returns_none(self) -> None:
        """The supervisor loop paces its tick on this: without the wait it spins."""
        inbox = self.make_inbox()

        started = time.monotonic()
        assert inbox.poll(POLL_TIMEOUT_SEC) is None
        elapsed = time.monotonic() - started

        assert elapsed >= POLL_TIMEOUT_SEC * 0.5
        assert elapsed < POLL_TIMEOUT_SEC * 4

    def test_a_blocked_poll_returns_as_soon_as_something_is_submitted(self) -> None:
        inbox = self.make_inbox()
        polled: list[object] = []
        done = threading.Event()

        def _poll() -> None:
            polled.append(inbox.poll(POLL_TIMEOUT_SEC * 20))
            done.set()

        thread = threading.Thread(target=_poll, name="conformance-inbox-poll", daemon=True)
        thread.start()
        submitted = envelope("wake up")
        inbox.submit(submitted)

        assert done.wait(POLL_TIMEOUT_SEC * 4), "a blocked poll must return when a message arrives"
        assert polled == [submitted]
        thread.join(timeout=POLL_TIMEOUT_SEC)

    def test_the_inbox_refuses_anything_that_is_not_an_envelope(self) -> None:
        """A transport submitting a dict would otherwise pass its whole test file."""
        inbox = self.make_inbox()

        with pytest.raises(TypeError):
            inbox.submit({"chat_id": 1, "text": "hello"})  # type: ignore[arg-type]

    def test_a_fully_populated_envelope_arrives_as_its_declared_types(self) -> None:
        """What crosses the seam is the declared type, not an equal blob of JSON."""
        inbox = self.make_inbox()
        submitted = full_envelope()

        inbox.submit(submitted)
        polled = inbox.poll(POLL_TIMEOUT_SEC)

        assert polled == submitted
        assert isinstance(polled.scope, OwnerScope)
        assert polled.chat_id == submitted.chat_id
        assert polled.source == submitted.source
        assert polled.attachments == submitted.attachments

    @pytest.mark.parametrize("free_form", ["transport", "attachments"])
    def test_the_inbox_refuses_a_value_no_wire_format_carries(self, free_form: str) -> None:
        """Refused where it is handed over, on every backend rather than the broker alone.

        Every open shape, not just `transport`: a backend that checks one of them
        would pass this suite and fail on the wire for the other two.
        """
        inbox = self.make_inbox()
        envelope_with_path = full_envelope()
        container = getattr(envelope_with_path, free_form)
        target = container[0] if isinstance(container, list) else container
        target["hint"] = pathlib.Path("/tmp/x")

        with pytest.raises(TypeError):
            inbox.submit(envelope_with_path)
        assert inbox.poll() is None


class OutboxConformance:
    """The contract every ``IOutbox`` implementation must satisfy."""

    def make_outbox(self) -> IOutbox:
        raise NotImplementedError("conformance subclasses must build an outbox")

    def break_publish(self, outbox: IOutbox) -> bool:
        """Make the next ``publish`` on ``outbox`` fail; ``False`` if it cannot.

        The hook exists so a backend that cannot simulate a failed append skips
        the atomicity case *conditionally*. An implementation that can simulate
        it must, or the one contract a bus can violate goes untested. What it
        cannot prove is structure: that a backend is not several independent
        writes is a review obligation on the implementation, because a black-box
        suite can only break the write the author chose to break.
        """
        del outbox
        return False

    def shrink_retention(self, outbox: IOutbox, monkeypatch: pytest.MonkeyPatch) -> int | None:
        """Lower this outbox's retention bound and return it, or ``None`` if it cannot.

        Without it the retention cases would have to publish a backend's whole
        bound to reach a drop, which turns the suite into a load test and is
        vacuous on any backend whose bound is large.
        """
        del outbox, monkeypatch
        return None

    def supports_shared_subscriptions(self) -> bool:
        raise NotImplementedError("conformance subclasses must declare whether the backend opens shared subscriptions")

    def _single_reader_mode(self) -> SubscriptionMode:
        if self.supports_shared_subscriptions():
            return SubscriptionMode.SHARED
        return SubscriptionMode.BROADCAST

    def _second_subscription(self, outbox: IOutbox) -> IOutboxReader:
        if self.supports_shared_subscriptions():
            return outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        return outbox.reader(name="a2a", mode=SubscriptionMode.BROADCAST)

    def _require_shared_subscriptions(self) -> None:
        if not self.supports_shared_subscriptions():
            pytest.skip(
                f"{type(self).__name__} declares supports_shared_subscriptions() False: "
                "the backend opens no SHARED reader to compete or hand over"
            )

    # -- fan-out and exclusivity ------------------------------------------

    def test_every_broadcast_reader_sees_the_message(self) -> None:
        outbox = self.make_outbox()
        first = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        second = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        try:
            outbox.publish(message("fan out"))

            assert first.poll(POLL_TIMEOUT_SEC).message.text == "fan out"
            assert second.poll(POLL_TIMEOUT_SEC).message.text == "fan out"
        finally:
            first.close()
            second.close()

    def test_exactly_one_shared_reader_sees_the_message(self) -> None:
        self._require_shared_subscriptions()
        outbox = self.make_outbox()
        first = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        second = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        try:
            outbox.publish(message("once"))

            polled = [(reader, reader.poll(POLL_TIMEOUT_SEC)) for reader in (first, second)]
            received = [(reader, item) for reader, item in polled if item is not None]
            assert len(received) == 1
            winner, delivered = received[0]
            assert delivered.message.text == "once"
            winner.ack(delivered)
        finally:
            first.close()
            second.close()

    def test_a_broadcast_reader_starts_at_the_present(self) -> None:
        outbox = self.make_outbox()
        outbox.publish(message("before the reader existed"))
        reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        try:
            assert reader.poll(POLL_TIMEOUT_SEC) is None

            outbox.publish(message("after"))
            delivered = reader.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None
            assert delivered.message.text == "after"
            reader.ack(delivered)
        finally:
            reader.close()

    def test_a_broadcast_reader_receives_what_was_published_before_its_first_poll(self) -> None:
        outbox = self.make_outbox()
        reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        try:
            outbox.publish(message("published before the first poll"))

            delivered = reader.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None
            assert delivered.message.text == "published before the first poll"
            reader.ack(delivered)
        finally:
            reader.close()

    def test_a_backend_without_shared_subscriptions_refuses_to_open_one(self) -> None:
        if self.supports_shared_subscriptions():
            pytest.skip(
                f"{type(self).__name__} declares supports_shared_subscriptions() True: there is no refusal to pin"
            )
        outbox = self.make_outbox()

        with pytest.raises(ValueError, match="shared"):
            outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)

        reader = outbox.reader(name="dialogs", mode=SubscriptionMode.BROADCAST)
        try:
            outbox.publish(message("once", source="dialogs"))

            delivered = reader.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None
            assert delivered.message.text == "once"
            reader.ack(delivered)
            assert reader.poll(POLL_TIMEOUT_SEC) is None
        finally:
            reader.close()

    def test_a_competing_reader_resumes_an_unacknowledged_close(self) -> None:
        """One shared reader dies mid-delivery; the message is another's to finish."""
        self._require_shared_subscriptions()
        outbox = self.make_outbox()
        first = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        second = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        try:
            outbox.publish(message("answered but not sent"))
            delivered = first.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None
            first.close()  # crash: read, never acknowledged

            redelivered = second.poll(POLL_TIMEOUT_SEC)
            assert redelivered is not None
            assert redelivered.message.text == "answered but not sent"
            second.ack(redelivered)
        finally:
            second.close()

    def test_closing_a_reader_leaves_another_readers_delivery_alone(self) -> None:
        """The in-flight slot belongs to the reader that took it, not to the subscription.

        Two shared readers, and the one holding nothing closes. Taking the other
        reader's message back would deliver the same answer to the user twice.
        """
        self._require_shared_subscriptions()
        outbox = self.make_outbox()
        holder = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        idle = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        try:
            outbox.publish(message("once"))
            delivered = holder.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None

            idle.close()
            holder.ack(delivered)

            assert holder.poll(POLL_TIMEOUT_SEC) is None, "an acknowledged message must not come back"
        finally:
            holder.close()

    # -- acknowledgement ---------------------------------------------------

    def test_an_unacknowledged_message_stalls_its_successors(self) -> None:
        outbox = self.make_outbox()
        reader = outbox.reader(name="dialogs", mode=self._single_reader_mode())
        try:
            outbox.publish(message("first"))
            outbox.publish(message("second"))

            first = reader.poll(POLL_TIMEOUT_SEC)
            assert first is not None
            assert first.message.text == "first"
            assert reader.poll(POLL_TIMEOUT_SEC) is None

            reader.ack(first)
            second = reader.poll(POLL_TIMEOUT_SEC)
            assert second is not None
            assert second.message.text == "second"
            reader.ack(second)
        finally:
            reader.close()

    def test_a_negatively_acknowledged_message_is_redelivered(self) -> None:
        outbox = self.make_outbox()
        reader = outbox.reader(name="dialogs", mode=self._single_reader_mode())
        try:
            outbox.publish(message("delivery failed"))

            delivered = reader.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None
            reader.nack(delivered)

            again = reader.poll(POLL_TIMEOUT_SEC)
            assert again is not None
            assert again.message.text == "delivery failed"
            reader.ack(again)
        finally:
            reader.close()

    def test_deferred_owner_does_not_block_other_owners_or_lose_its_order(self) -> None:
        outbox = self.make_outbox()
        reader = outbox.reader(name="dialogs", mode=self._single_reader_mode())
        first_owner = OwnerScope(owner_id="first", agent_id="agent", surface_id="browser", chat_id="chat")
        other_owner = OwnerScope(owner_id="other", agent_id="agent", surface_id="browser", chat_id="chat")
        try:
            outbox.publish(message("photo", scope=first_owner))
            outbox.publish(message("after photo", scope=first_owner))
            outbox.publish(message("other owner's text", scope=other_owner))
            photo = reader.poll(POLL_TIMEOUT_SEC)
            assert photo is not None
            reader.nack(photo)
            photo = reader.poll(POLL_TIMEOUT_SEC)
            assert photo is not None
            reader.defer(photo, delay=1.0)

            other = reader.poll(0)
            assert other is not None
            assert other.message.text == "other owner's text"
            assert other.attempts == 0
            reader.ack(other)
            assert reader.poll(0) is None

            resumed = reader.poll(2.0)
            assert resumed is not None
            assert resumed.message.text == "photo"
            assert resumed.attempts == 1
            reader.ack(resumed)
            after = reader.poll(POLL_TIMEOUT_SEC)
            assert after is not None
            assert after.message.text == "after photo"
            reader.ack(after)
        finally:
            reader.close()

    def test_deferred_owner_survives_shared_reader_replacement(self) -> None:
        self._require_shared_subscriptions()
        outbox = self.make_outbox()
        first = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        second = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        try:
            owner = OwnerScope(owner_id="first", agent_id="a", surface_id="browser", chat_id="c")
            outbox.publish(message("waiting", scope=owner))
            outbox.publish(message("following", scope=owner))
            outbox.publish(message("ready", scope=owner.model_copy(update={"owner_id": "other"})))
            waiting = first.poll(POLL_TIMEOUT_SEC)
            assert waiting is not None
            first.defer(waiting, delay=1.0)
            first.close()

            ready = second.poll(0)
            assert ready is not None
            assert ready.message.text == "ready"
            second.ack(ready)
            first.ack(waiting)
            first.defer(waiting, delay=60)
            resumed = second.poll(2.0)
            assert resumed is not None
            assert resumed.message.text == "waiting"
            assert resumed.attempts == 0
            second.ack(resumed)
            following = second.poll(POLL_TIMEOUT_SEC)
            assert following is not None
            assert following.message.text == "following"
            second.ack(following)
        finally:
            first.close()
            second.close()

    def test_an_acknowledged_drop_does_not_stall_what_follows(self) -> None:
        """ "Not mine" is ack plus a drop, and the queue keeps moving."""
        outbox = self.make_outbox()
        reader = outbox.reader(name="dialogs", mode=self._single_reader_mode())
        try:
            outbox.publish(message("not mine", chat_id=99))
            outbox.publish(message("mine", chat_id=1))

            dropped = reader.poll(POLL_TIMEOUT_SEC)
            assert dropped is not None
            reader.ack(dropped)  # delivered nothing

            delivered = reader.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None
            assert delivered.message.text == "mine"
            reader.ack(delivered)
        finally:
            reader.close()

    # -- what the payload is when it arrives -------------------------------

    def test_a_fully_populated_message_arrives_as_its_declared_types(self) -> None:
        """Readers dispatch on the kind by identity and read the attachment by attribute."""
        outbox = self.make_outbox()
        reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        published = full_message()
        try:
            outbox.publish(published)

            delivered = reader.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None
            assert delivered.message == published
            assert delivered.message.kind is OutboundKind.PHOTO
            assert delivered.message.kind.value == "photo"
            assert isinstance(delivered.message.scope, OwnerScope)
            assert delivered.message.chat_id == published.chat_id
            assert isinstance(delivered.message.attachment, AttachmentRef)
            assert delivered.message.attachment.attachment_id == "att-1"
            assert delivered.message.transport == published.transport
            reader.ack(delivered)
        finally:
            reader.close()

    def test_the_outbox_refuses_a_value_no_wire_format_carries(self) -> None:
        outbox = self.make_outbox()
        reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        message_with_path = full_message()
        message_with_path.transport["hint"] = pathlib.Path("/tmp/x")
        try:
            with pytest.raises(TypeError):
                outbox.publish(message_with_path)

            assert reader.poll() is None, "a refused payload must not be queued"
        finally:
            reader.close()

    # -- publishing is all-or-nothing --------------------------------------

    def test_a_publish_that_raises_reached_nobody(self) -> None:
        """No repair exists for a partial fan-out, so the state must be unreachable."""
        outbox = self.make_outbox()
        first = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        second = self._second_subscription(outbox)
        try:
            if not self.break_publish(outbox):
                pytest.skip(f"{type(outbox).__name__} cannot simulate a failed append")

            with pytest.raises(Exception):
                outbox.publish(message("never taken"))

            assert first.poll(POLL_TIMEOUT_SEC) is None
            assert second.poll(POLL_TIMEOUT_SEC) is None
        finally:
            first.close()
            second.close()

    def test_a_publish_that_returns_reached_every_open_subscription(self) -> None:
        outbox = self.make_outbox()
        first = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        second = self._second_subscription(outbox)
        try:

            def expired():
                raise TimeoutError("authority expired")

            with pytest.raises(TimeoutError, match="authority expired"):
                outbox.publish(message("refused"), before_publish=expired)
            assert outbox.publish(message("to everyone", source="dialogs")) is None

            for reader in (first, second):
                delivered = reader.poll(POLL_TIMEOUT_SEC)
                assert delivered is not None
                assert delivered.message.text == "to everyone"
                reader.ack(delivered)
        finally:
            first.close()
            second.close()

    def test_an_overflowing_outbox_drops_the_oldest_not_the_newest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bound is on the outbox, and what it discards is the stale end."""
        outbox = self.make_outbox()
        stalled = outbox.reader(name="dialogs", mode=self._single_reader_mode())
        try:
            bound = self.shrink_retention(outbox, monkeypatch)
            if bound is None:
                pytest.skip(f"{type(outbox).__name__} cannot lower its retention bound")

            for index in range(bound + 2):
                # A publish that failed on another consumer's slowness would fail
                # an unrelated producer's turn, so a drop is not a refusal.
                assert outbox.publish(message(f"m{index}", source="dialogs")) is None

            delivered = []
            while True:
                item = stalled.poll(POLL_TIMEOUT_SEC if not delivered else 0.0)
                if item is None:
                    break
                delivered.append(item.message.text)
                stalled.ack(item)

            assert delivered, "a stalled consumer that resumes must still receive something"
            assert delivered[-1] == f"m{bound + 1}", "the newest message must survive the drop"
            assert "m0" not in delivered, "the oldest is what a full backlog discards"
        finally:
            stalled.close()

    # -- synchronous seam --------------------------------------------------

    def test_polling_an_empty_outbox_returns_after_the_timeout(self) -> None:
        outbox = self.make_outbox()
        reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        try:
            started = time.monotonic()
            assert reader.poll(POLL_TIMEOUT_SEC) is None
            elapsed = time.monotonic() - started

            assert elapsed >= POLL_TIMEOUT_SEC * 0.5
            assert elapsed < POLL_TIMEOUT_SEC * 4
        finally:
            reader.close()

    def test_close_releases_a_blocked_poll(self) -> None:
        outbox = self.make_outbox()
        reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
        blocking = threading.Event()
        returned = threading.Event()
        result: list[object] = []

        def _poll() -> None:
            blocking.set()
            result.append(reader.poll(POLL_TIMEOUT_SEC * 20))
            returned.set()

        thread = threading.Thread(target=_poll, name="conformance-poll", daemon=True)
        thread.start()
        # Waiting for the thread to reach its poll rather than sleeping a guessed
        # interval: closing first makes `poll` return at its top-of-loop guard,
        # and the test would pass without ever interrupting anything.
        assert blocking.wait(POLL_TIMEOUT_SEC * 4)
        started = time.monotonic()
        reader.close()

        assert returned.wait(POLL_TIMEOUT_SEC * 4), "close must interrupt a blocked poll"
        assert result == [None]
        assert time.monotonic() - started < POLL_TIMEOUT_SEC * 4, "close must not wait out the poll timeout"
        thread.join(timeout=POLL_TIMEOUT_SEC)

    # -- settling is bound to its delivery ---------------------------------

    def test_a_stale_receipt_cannot_settle_another_readers_message(self) -> None:
        """A shared subscription outlives its readers, so receipts must not cross.

        The dialogs transport is mid-delivery when its reader closes: the message
        goes back to the head of the durable queue, a competing reader takes it,
        and only then does the old delivery finish. Settling it on the reader it
        came from must not clear the new reader's in-flight slot, and its `nack`
        must not requeue a copy.
        """
        self._require_shared_subscriptions()
        outbox = self.make_outbox()
        first = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        second = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
        outbox.publish(message("only"))
        delivered = first.poll(POLL_TIMEOUT_SEC)
        assert delivered is not None
        first.close()

        try:
            redelivered = second.poll(POLL_TIMEOUT_SEC)
            assert redelivered is not None
            assert redelivered.message.text == "only"

            first.ack(delivered)
            first.nack(delivered)

            second.ack(redelivered)
            assert second.poll(POLL_TIMEOUT_SEC) is None, "a stale receipt must not requeue a duplicate"
        finally:
            second.close()
