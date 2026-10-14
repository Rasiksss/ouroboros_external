"""A2A reads everything on its subscription and delivers only what it is holding.

Everything it does not deliver is acknowledged and dropped — never negatively
acknowledged, which would hold the offset and stall the messages behind it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros.agent.task_pipeline import emit_task_results
from ouroboros.agent_events.interfaces import OutboundText
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.service.messaging import get_tools
from supervisor.events import dispatch_event
from supervisor.message_bus import send_with_budget
from supervisor.runtime_context import reset_runtime_for_tests
from tests.helpers.fake_gateway import FakeGateway, SettlingReader
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.scope import scope_for
from tests.helpers.shared import make_tool_ctx, scope_with
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from tests.helpers.transport_conformance import POLL_TIMEOUT_SEC
from transport.a2a.delivery import A2AResponseRouter
from transport.a2a.executor import derive_a2a_chat_id
from transport.interfaces import (
    AttachmentRef,
    DeliveredMessage,
    IOutboxReader,
    OutboundKind,
    OutboundMessage,
    SubscriptionMode,
)


def _delivered(message: OutboundMessage) -> tuple[SettlingReader, DeliveredMessage]:
    """One delivery plus the reader the router must settle it through."""
    reader = SettlingReader()
    return reader, reader.deliver(message)


def _router() -> A2AResponseRouter:
    return A2AResponseRouter(FakeGateway().outbox)


def test_a_progress_message_is_acknowledged_and_dropped():
    router = _router()
    chat_id = derive_a2a_chat_id("task-1")
    loop = asyncio.new_event_loop()
    try:
        pending = router.register(scope_for("a2a", chat_id), "task-1", loop)
        reader, delivered = _delivered(
            OutboundMessage(
                scope=scope_for("a2a", chat_id),
                source="a2a",
                correlation_id="task-1",
                text="thinking",
                is_progress=True,
            )
        )

        router.handle(reader, delivered)

        assert reader.settled == ["ack"]
        assert not pending._event.is_set()
    finally:
        loop.close()


def test_an_orphaned_final_is_acknowledged_and_dropped(caplog):
    """Another instance holds the request, or it has already timed out.

    A dropped answer is the one outcome nobody can see from outside, so it says so.
    """
    router = _router()
    reader, delivered = _delivered(
        OutboundMessage(
            scope=scope_for("a2a", derive_a2a_chat_id("task-2")),
            source="a2a",
            correlation_id="task-1",
            text="late answer",
        )
    )

    with caplog.at_level(logging.INFO, logger="a2a-server"):
        router.handle(reader, delivered)

    assert reader.settled == ["ack"]
    assert "no pending request" in caplog.text


def test_a_soft_timeout_notice_is_not_the_answer():
    """The queue's "still running" notice is published as progress, and progress is dropped.

    Completing on it would hand the JSON-RPC caller a status line as the agent's
    answer, and the real answer would arrive to no pending request at all.
    """
    router = _router()
    chat_id = derive_a2a_chat_id("task-slow")
    loop = asyncio.new_event_loop()
    try:
        pending = router.register(scope_for("a2a", chat_id), "task-1", loop)
        reader, delivered = _delivered(
            OutboundMessage(
                scope=scope_for("a2a", chat_id),
                source="a2a",
                correlation_id="task-1",
                text="⏱️ Task task-slow running for 600s. Continuing.",
                is_progress=True,
            )
        )

        router.handle(reader, delivered)

        assert reader.settled == ["ack"]
        assert not pending._event.is_set()

        # The answer that follows it is the one the caller gets.
        answer_reader, answer = _delivered(
            OutboundMessage(scope=scope_for("a2a", chat_id), source="a2a", correlation_id="task-1", text="the answer")
        )
        router.handle(answer_reader, answer)
        assert loop.run_until_complete(pending.wait(POLL_TIMEOUT_SEC)) == "the answer"
    finally:
        loop.close()


def test_only_the_first_answer_reaches_the_caller(caplog):
    """A second final message for one chat is an orphan, not an overwrite."""
    router = _router()
    chat_id = derive_a2a_chat_id("task-twice")
    loop = asyncio.new_event_loop()
    try:
        pending = router.register(scope_for("a2a", chat_id), "task-1", loop)
        first_reader, first = _delivered(
            OutboundMessage(scope=scope_for("a2a", chat_id), source="a2a", correlation_id="task-1", text="the answer")
        )
        second_reader, second = _delivered(
            OutboundMessage(scope=scope_for("a2a", chat_id), source="a2a", correlation_id="task-1", text="a later one")
        )

        router.handle(first_reader, first)
        with caplog.at_level(logging.INFO, logger="a2a-server"):
            router.handle(second_reader, second)
            assert loop.run_until_complete(pending.wait(POLL_TIMEOUT_SEC)) == "the answer"

        assert pending._text == "the answer"
        assert second_reader.settled == ["ack"]
        assert "no pending request" in caplog.text
    finally:
        loop.close()


def test_a_proactive_tool_message_does_not_replace_the_final_answer(tmp_path):
    runtime_persistence.initialize()
    gateway = FakeGateway()
    runtime = bootstrap_supervisor_for_tests(tmp_path, gateway=gateway)
    ctx = SimpleNamespace(send_with_budget=send_with_budget, state_repo=runtime.state_repo)
    scope = scope_for("a2a", derive_a2a_chat_id("ctx-1"))
    transport = {"a2a_context_id": "ctx-1", "correlation_id": "task-1"}
    tool_ctx = make_tool_ctx(
        scope=scope, transport=transport, task_id="internal-job-1", repo_dir=tmp_path, drive_root=tmp_path
    )
    send_user_message = next(tool.handler for tool in get_tools() if tool.name == "send_user_message")
    router = A2AResponseRouter(gateway.outbox)
    reader = gateway.outbox.reader(name="a2a-test", mode=SubscriptionMode.BROADCAST)
    loop = asyncio.new_event_loop()
    try:
        pending = router.register(scope, "task-1", loop)
        send_user_message(tool_ctx, text="Still checking the employees.")
        emit_task_results(
            env=SimpleNamespace(drive_root=tmp_path),
            llm=None,
            pending_events=tool_ctx.pending_events,
            task={"id": "internal-job-1", "type": "task", "source": "a2a", "transport": transport},
            text="The employees and their grades.",
            usage={},
            llm_trace={},
            start_time=time.time(),
            drive_logs=tmp_path,
            scope=scope,
            execution_memory_repository=runtime.execution_memory_repository,
            audit_event_repository=runtime.state_repo.audit_event_repository,
        )
        for event in tool_ctx.pending_events:
            if isinstance(event, OutboundText):
                dispatch_event(event, ctx)
                delivered = reader.poll(0)
                assert delivered is not None
                router.handle(reader, delivered)

        assert loop.run_until_complete(pending.wait(POLL_TIMEOUT_SEC)) == "The employees and their grades."
    finally:
        reader.close()
        loop.close()
        reset_runtime_for_tests()


def test_media_is_acknowledged_and_dropped():
    """The channel is a JSON-RPC response and cannot carry media."""
    router = _router()
    chat_id = derive_a2a_chat_id("task-3")
    loop = asyncio.new_event_loop()
    try:
        pending = router.register(scope_for("a2a", chat_id), "task-1", loop)
        reader, delivered = _delivered(
            OutboundMessage(
                scope=scope_for("a2a", chat_id),
                source="a2a",
                kind=OutboundKind.PHOTO,
                caption="chart",
                attachment=AttachmentRef(attachment_id="att-1", filename="chart.png", mime="image/png"),
            )
        )

        router.handle(reader, delivered)

        assert reader.settled == ["ack"]
        # Every path through `handle` acks, so the ack alone does not say the
        # message was dropped: the waiting request must still be waiting.
        assert not pending._event.is_set()
        assert pending._text == ""
    finally:
        loop.close()


@pytest.mark.parametrize("instances", [1, 2])
def test_concurrent_tasks_in_one_context_receive_their_own_answers(instances):
    async def scenario() -> list[str]:
        gateway = FakeGateway()
        routers = [A2AResponseRouter(gateway.outbox, name=f"a2a-{i}") for i in range(instances)]
        scope = scope_for("a2a", derive_a2a_chat_id("ctx-shared"))
        for router in routers:
            router.start()
        try:
            pending = [
                routers[i % instances].register(scope, f"task-{i}", asyncio.get_running_loop()) for i in range(2)
            ]
            for i in reversed(range(2)):
                gateway.answer(
                    scope.chat_id,
                    f"answer-{i}",
                    source="a2a",
                    scope=scope,
                    correlation_id=f"task-{i}",
                    transport={"correlation_id": "stale-task"},
                )
            return await asyncio.gather(*(request.wait(POLL_TIMEOUT_SEC * 4) for request in pending))
        finally:
            for router in routers:
                router.stop()

    assert asyncio.run(scenario()) == ["answer-0", "answer-1"]


def test_a_released_request_no_longer_receives_its_answer():
    """The timeout path: the entry is gone, so a late final is dropped."""

    async def scenario() -> None:
        router = _router()
        chat_id = derive_a2a_chat_id("task-5")
        pending = router.register(scope_for("a2a", chat_id), "task-1", asyncio.get_running_loop())
        with pytest.raises(asyncio.TimeoutError):
            await pending.wait(0.05)
        retry = router.register(scope_for("a2a", chat_id), "task-1", asyncio.get_running_loop())
        router.release(scope_for("a2a", chat_id), "task-1", pending)

        reader, delivered = _delivered(
            OutboundMessage(scope=scope_for("a2a", chat_id), source="a2a", correlation_id="task-1", text="too late")
        )
        router.handle(reader, delivered)

        assert reader.settled == ["ack"]
        assert await retry.wait(POLL_TIMEOUT_SEC) == "too late"
        assert not pending._event.is_set()

    asyncio.run(scenario())


def test_dropping_a_message_does_not_stall_the_next_one():
    gateway = FakeGateway()
    router = A2AResponseRouter(gateway.outbox)
    reader = gateway.outbox.reader(name="a2a", mode=SubscriptionMode.BROADCAST)
    try:
        chat_id = derive_a2a_chat_id("task-6")
        loop = asyncio.new_event_loop()
        pending = router.register(scope_for("a2a", chat_id), "task-1", loop)

        gateway.outbox.publish(
            OutboundMessage(
                scope=scope_for("a2a", chat_id),
                source="a2a",
                correlation_id="task-1",
                text="progress",
                is_progress=True,
            )
        )
        gateway.outbox.publish(
            OutboundMessage(scope=scope_for("a2a", chat_id), source="a2a", correlation_id="task-1", text="the answer")
        )

        for _ in range(2):
            delivered = reader.poll(POLL_TIMEOUT_SEC)
            assert delivered is not None
            router.handle(reader, delivered)

        loop.run_until_complete(asyncio.sleep(0))
        assert pending._text == "the answer"
        loop.close()
    finally:
        reader.close()


def test_the_reader_runs_on_its_own_thread_and_stops_cleanly():
    gateway = FakeGateway()
    router = A2AResponseRouter(gateway.outbox)
    router.start()
    try:
        assert any(thread.name == "a2a-outbox-reader" for thread in threading.enumerate())
    finally:
        router.stop()

    assert not any(thread.name == "a2a-outbox-reader" and thread.is_alive() for thread in threading.enumerate())


def test_a_failing_poll_stops_the_shared_reader_loop(caplog):
    """The loop is ``transport.reader_loop``'s, so the failure it reports is that one's."""

    class _BrokenReader(IOutboxReader):
        def poll(self, timeout: float = 0.0) -> DeliveredMessage | None:
            raise RuntimeError("backend is gone")

        def ack(self, delivered: DeliveredMessage) -> None:
            raise AssertionError("nothing was delivered")

        def nack(self, delivered: DeliveredMessage) -> None:
            raise AssertionError("nothing was delivered")

        def defer(self, delivered: DeliveredMessage, *, delay: float) -> None:
            raise AssertionError("nothing was delivered")

        def close(self) -> None:
            return None

    router = _router()
    router._reader = _BrokenReader()
    with caplog.at_level(logging.ERROR):
        router._run()

    reported = next(record for record in caplog.records if "outbox reader failed" in record.getMessage())
    assert reported.name == "transport.reader_loop"
    assert "A2A" in reported.getMessage()


def test_a_reader_that_gave_up_releases_its_subscription():
    """The shared loop leaves closing to its caller, and a stopped reader must close.

    A subscription with no reader is one every publish still fans into, up to the
    backlog limit.
    """

    def _boom(_timeout: float = 0.0):
        raise RuntimeError("backend is gone")

    gateway = FakeGateway()
    router = A2AResponseRouter(gateway.outbox)
    reader = gateway.outbox.reader(name="a2a", mode=SubscriptionMode.BROADCAST)
    reader.poll = _boom  # type: ignore[method-assign]
    router._reader = reader

    router._run()

    gateway.answer(derive_a2a_chat_id("task-1"), "after the reader died", source="a2a")
    assert gateway.outbox._outbox._broadcast == [], "a subscription nothing drains is a write-only sink"


def test_a2a_chat_ids_stay_clear_of_the_skill_bridge_window():
    from ouroboros.contracts.chat_id_policy import A2A_CHAT_ID_CEILING, is_derived_a2a_chat_id

    ids = [derive_a2a_chat_id(f"task-{index}") for index in range(50)]

    assert all(is_derived_a2a_chat_id(value) for value in ids)
    assert all(value <= A2A_CHAT_ID_CEILING for value in ids)
    assert len(set(ids)) == len(ids)


@parametrize_from_dict(
    {
        "owner": {"axis": {"owner_id": "other"}, "source": "a2a", "task_id": "task-1"},
        "agent": {"axis": {"agent_id": "other"}, "source": "a2a", "task_id": "task-1"},
        "surface": {"axis": {"surface_id": "browser"}, "source": "a2a", "task_id": "task-1"},
        "chat": {"axis": {"chat_id": "opaque-browser-chat"}, "source": "a2a", "task_id": "task-1"},
        "source": {"axis": {}, "source": "browser", "task_id": "task-1"},
        "task": {"axis": {}, "source": "a2a", "task_id": "other"},
        "no correlation": {"axis": {}, "source": "a2a", "task_id": ""},
    }
)
def test_an_unmatched_answer_is_dropped_without_consuming_the_waiter(axis, source, task_id):
    router = _router()
    chat_id = derive_a2a_chat_id("task-1")
    scope = scope_for("a2a", chat_id)
    loop = asyncio.new_event_loop()
    try:
        pending = router.register(scope, "task-1", loop)
        reader, delivered = _delivered(
            OutboundMessage(
                scope=scope_with(scope, **axis),
                source=source,
                correlation_id=task_id,
                transport={"correlation_id": "task-1"},
                text="unmatched answer",
            )
        )

        router.handle(reader, delivered)

        assert reader.settled == ["ack"]
        assert not pending._event.is_set()
        answer = OutboundMessage(
            scope=scope,
            trace_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
            source="a2a",
            correlation_id="task-1",
            text="the answer",
        )
        router.handle(reader, reader.deliver(answer))
        assert loop.run_until_complete(pending.wait(POLL_TIMEOUT_SEC)) == "the answer"
    finally:
        loop.close()
