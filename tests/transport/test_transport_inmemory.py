"""The in-process gateway backend, held to the seam's conformance suite."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.runtime_sessions import sql_runtime_session_repository
from tests.helpers.scope import scope_for
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor import supervisor_heartbeat
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.transport_conformance import (
    POLL_TIMEOUT_SEC,
    InboxConformance,
    OutboxConformance,
    message,
)
from tests.helpers.usage import sql_usage_repository
from transport.infra.inmemory import (
    InProcessInbox,
    InProcessMessageGateway,
    InProcessOutbox,
)
from transport.interfaces import IInbox, IMessageGateway, InboundEnvelope, IOutbox, SubscriptionMode, routed_chat_id


class TestInProcessOutbox(OutboxConformance):
    def make_outbox(self) -> InProcessOutbox:
        return InProcessOutbox()

    def supports_shared_subscriptions(self) -> bool:
        return True

    def break_publish(self, outbox: InProcessOutbox) -> bool:
        """Fail the append the way a backend whose write is refused would."""

        def _refuse(_message: object) -> None:
            raise RuntimeError("the outbox is not taking messages")

        outbox._offer_to_all = _refuse  # type: ignore[method-assign]
        return True

    def shrink_retention(self, outbox: InProcessOutbox, monkeypatch) -> int:
        """The bound is a module constant here, so the case can reach it cheaply."""
        del outbox
        monkeypatch.setattr("transport.infra.inmemory._MAX_PENDING_PER_SUBSCRIPTION", 3)
        return 3


class TestInProcessInbox(InboxConformance):
    def make_inbox(self) -> InProcessInbox:
        return InProcessInbox()

    def replacement_consumer(self, inbox: InProcessInbox) -> None:
        """Declared, not inferred: this queue dies with the consumer that stopped."""
        del inbox
        return None


def test_the_gateway_implements_the_seam():
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())

    assert isinstance(gateway, IMessageGateway)
    assert isinstance(gateway.inbox, IInbox)
    assert isinstance(gateway.outbox, IOutbox)


def test_the_directions_are_independently_substitutable():
    """Each direction is exactly the one its caller supplied — the gateway substitutes nothing."""
    inbox = InProcessInbox()
    outbox = InProcessOutbox()
    gateway = InProcessMessageGateway(inbox=inbox, outbox=outbox)

    assert gateway.inbox is inbox
    assert gateway.outbox is outbox


def test_both_directions_must_be_passed_explicitly():
    with pytest.raises(TypeError):
        InProcessMessageGateway()

    with pytest.raises(TypeError):
        InProcessMessageGateway(outbox=InProcessOutbox())


def test_submitting_returns_and_the_supervisor_polls_the_envelope():
    inbox = InProcessInbox()
    envelope = InboundEnvelope(scope=scope_for("browser", 1), user_id=1, text="hello")

    assert inbox.submit(envelope) is None
    assert inbox.poll(POLL_TIMEOUT_SEC) is envelope
    assert inbox.poll(0.05) is None


def test_the_inbox_refuses_anything_that_is_not_an_envelope():
    with pytest.raises(TypeError):
        InProcessInbox().submit({"chat_id": 1, "text": "hello"})


def test_the_outbox_refuses_anything_that_is_not_an_outbound_message():
    with pytest.raises(TypeError):
        InProcessOutbox().publish({"chat_id": 1, "text": "hello"})


def test_a_reader_requires_the_transport_name():
    with pytest.raises(ValueError, match="transport's name"):
        InProcessOutbox().reader(name="", mode=SubscriptionMode.BROADCAST)


def test_a_broadcast_reader_drops_what_it_never_acknowledged():
    """Its message belonged to a connection that died with the reader."""
    outbox = InProcessOutbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    outbox.publish(message("for a closed websocket"))
    assert reader.poll(POLL_TIMEOUT_SEC) is not None
    reader.close()

    reopened = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        assert reopened.poll(POLL_TIMEOUT_SEC) is None
    finally:
        reopened.close()


def test_the_drop_report_names_the_consumer_and_is_rate_limited(monkeypatch, caplog):
    """Drops are the operator's only signal; one line per lost message is no signal."""
    import logging

    monkeypatch.setattr("transport.infra.inmemory._MAX_PENDING_PER_SUBSCRIPTION", 2)
    outbox = InProcessOutbox()
    reader = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    try:
        with caplog.at_level(logging.ERROR, logger="transport.infra.inmemory"):
            for index in range(12):
                assert outbox.publish(message(f"m{index}", source="dialogs")) is None

        reports = [record for record in caplog.records if "is full" in record.getMessage()]
        assert len(reports) == 1, "a report per dropped message is not a diagnostic"
        assert "'dialogs'" in reports[0].getMessage(), "the report must name the consumer that is behind"
    finally:
        reader.close()


def test_a_name_cannot_be_opened_in_two_modes():
    """A name is one consumer identity, and a consumer has one mode."""
    outbox = InProcessOutbox()
    shared = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    try:
        with pytest.raises(ValueError, match="already open as shared"):
            outbox.reader(name="dialogs", mode=SubscriptionMode.BROADCAST)
    finally:
        shared.close()

    # Still bound after the last reader released it: an instance that restarts
    # keeps its mode rather than racing a peer into changing it.
    with pytest.raises(ValueError, match="already open as shared"):
        outbox.reader(name="dialogs", mode=SubscriptionMode.BROADCAST)

    # The claim lapses when no instance holds the name. In one process that is
    # reached exactly when the process ends, so a fresh outbox is unclaimed.
    fresh = InProcessOutbox().reader(name="dialogs", mode=SubscriptionMode.BROADCAST)
    fresh.close()


def test_a_broadcast_reader_dropped_without_close_takes_its_subscription_with_it():
    """`close` is the orderly release, not the only one.

    A reader nobody holds is the one-process form of an instance that did not
    come back: nothing can poll that subscription again, and every publish would
    keep feeding it.
    """
    outbox = InProcessOutbox()
    outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)  # dropped, never closed

    for index in range(5):
        outbox.publish(message(f"m{index}"))

    assert outbox._broadcast == [], "a subscription whose reader is gone must not be fed"


def test_releasing_an_abandoned_subscription_says_what_went_with_it(caplog):
    """The reap is a transport bug in one process; silence would hide it."""
    import logging

    outbox = InProcessOutbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    outbox.publish(message("never read"))
    del reader

    with caplog.at_level(logging.WARNING, logger="transport.infra.inmemory"):
        outbox.publish(message("the one that reaps"))

    assert "'browser'" in caplog.text
    assert "1 pending message(s)" in caplog.text


def test_a_broadcast_reader_still_held_keeps_receiving():
    """The failure path of the same mechanism: a live reader is not reaped."""
    outbox = InProcessOutbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    try:
        outbox.publish(message("for a live socket"))

        delivered = reader.poll(POLL_TIMEOUT_SEC)
        assert delivered is not None
        assert delivered.message.text == "for a live socket"
        assert len(outbox._broadcast) == 1
        reader.ack(delivered)
    finally:
        reader.close()


def test_a_subscription_exists_as_soon_as_its_reader_is_constructed():
    """A transport publishes into the seam before the reader thread is started."""
    outbox = InProcessOutbox()
    reader = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    try:
        outbox.publish(message("published before anyone polls", source="dialogs"))

        delivered = reader.poll(POLL_TIMEOUT_SEC)
        assert delivered is not None
        assert delivered.message.text == "published before anyone polls"
        reader.ack(delivered)
    finally:
        reader.close()


def test_one_publish_reaches_every_transport_that_is_listening():
    outbox = InProcessOutbox()
    browser = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    a2a = outbox.reader(name="a2a", mode=SubscriptionMode.BROADCAST)
    dialogs = outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    try:
        outbox.publish(message("one answer", chat_id=7, source="dialogs"))

        for reader in (browser, a2a, dialogs):
            delivered = reader.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None
            assert delivered.message.text == "one answer"
            reader.ack(delivered)
    finally:
        for reader in (browser, a2a, dialogs):
            reader.close()


def test_a_blocked_reader_wakes_as_soon_as_something_is_published():
    outbox = InProcessOutbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    received: list[str] = []
    done = threading.Event()

    def _read() -> None:
        delivered = reader.poll(POLL_TIMEOUT_SEC * 20)
        if delivered is not None:
            received.append(delivered.message.text)
            reader.ack(delivered)
        done.set()

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    outbox.publish(message("wake up"))

    assert done.wait(POLL_TIMEOUT_SEC * 4)
    assert received == ["wake up"]
    reader.close()
    thread.join(timeout=POLL_TIMEOUT_SEC)


def _loop_context(store, state_repo, submitted, commands):
    from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
    from supervisor.orchestrator.orchestrator import SubmitResult

    def _submit(envelope):
        submitted.append(envelope)
        if envelope.effective_text().startswith("/"):
            return SubmitResult(
                intent=TaskIntentKind.SLASH_COMMAND,
                mode=SubmissionMode.NONE,
                slash_command=envelope.effective_text(),
            )
        return SubmitResult(intent=TaskIntentKind.AGENT_CHAT, mode=SubmissionMode.QUEUE, task_id="t-1")

    return SimpleNamespace(
        tasks_repo=store.tasks_repo,
        state_repo=state_repo,
        orchestrator=SimpleNamespace(submit=_submit),
        send_with_budget=lambda text, **kwargs: commands.append((routed_chat_id(kwargs["scope"]), text)),
        executor=SimpleNamespace(get_slot=lambda _worker_id: None),
    )


def _supervisor_loop(tmp_path, submitted, commands, *, inbox):
    from supervisor.runtime import SupervisorLoop
    from supervisor.state import RuntimeStateRepository

    store = runtime_persistence.initialize()
    state_repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=sql_owner_state_repository(),
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    state_repo.sync_runtime_session(sql_runtime_session_repository(), source="dialogs")
    ctx = _loop_context(store, state_repo, submitted, commands)
    return SupervisorLoop(
        ctx=ctx,
        stop_event=threading.Event(),
        inbox=inbox,
        heartbeat=supervisor_heartbeat(),
    )


def test_the_loop_consumes_what_a_transport_submitted(tmp_path):
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    submitted: list[InboundEnvelope] = []
    loop = _supervisor_loop(tmp_path, submitted, [], inbox=gateway.inbox)

    gateway.inbox.submit(InboundEnvelope(scope=scope_for("browser", 7), user_id=7, text="hello"))
    handled = loop.process_inbox()

    assert handled == 1
    assert [envelope.text for envelope in submitted] == ["hello"]
    assert loop.ctx.state_repo.load().owner_id == 7


def test_the_loop_takes_the_same_path_for_two_transports(tmp_path):
    """Equivalent messages from two transports produce the same supervisor steps."""
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    submitted: list[InboundEnvelope] = []
    loop = _supervisor_loop(tmp_path, submitted, [], inbox=gateway.inbox)

    for source in ("browser", "dialogs"):
        gateway.inbox.submit(InboundEnvelope(scope=scope_for(source, 7), user_id=7, text="same question"))
    loop.process_inbox()

    assert [envelope.source for envelope in submitted] == ["browser", "dialogs"]
    assert [envelope.effective_text() for envelope in submitted] == ["same question"] * 2


def test_a_slash_command_from_the_inbox_runs_in_the_supervisor(tmp_path):
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    submitted: list[InboundEnvelope] = []
    commands: list[tuple] = []
    loop = _supervisor_loop(tmp_path, submitted, commands, inbox=gateway.inbox)

    gateway.inbox.submit(InboundEnvelope(scope=scope_for("browser", 7), user_id=7, text="/stop"))
    loop.process_inbox()

    assert commands == [(7, "Активной задачи нет.")]


def test_draining_the_inbox_is_bounded(tmp_path):
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    submitted: list[InboundEnvelope] = []
    loop = _supervisor_loop(tmp_path, submitted, [], inbox=gateway.inbox)

    for index in range(5):
        gateway.inbox.submit(InboundEnvelope(scope=scope_for("browser", 7), user_id=7, text=f"m{index}"))

    assert loop.process_inbox(limit=2) == 2
    assert loop.process_inbox() == 3


def test_a_loop_with_an_empty_inbox_handles_nothing(tmp_path):
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    loop = _supervisor_loop(tmp_path, [], [], inbox=gateway.inbox)

    assert loop.process_inbox(timeout=0.0) == 0
