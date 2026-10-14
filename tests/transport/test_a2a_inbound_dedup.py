"""A2A over the seam: submission dedups, the journal replays, the answer comes back.

Without ``client_message_id`` the storage id falls back to ``uuid4``, so a
resubmitted message (at-least-once delivery) lands on a second record. The id is the
delivery id, not the task id: a task carries as many messages as the conversation has
turns, and keying on the task would drop every one after the first as a duplicate.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import event

from ouroboros.observability import aef_tracing
from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.fake_gateway import FakeGateway, RecordingMemoryEvents, SettlingReader
from tests.helpers.shared import TEST_SCOPE, scope_with
from transport.a2a.delivery import A2AResponseRouter
from transport.a2a.executor import (
    OuroborosA2AExecutor,
    a2a_delivery_id,
    derive_a2a_chat_id,
    resubmit_unprocessed,
)
from transport.a2a.interfaces import ISupervisorReadiness
from transport.interfaces import OutboundMessage


class _ReadyNow(ISupervisorReadiness):
    """The supervisor is already accepting submissions."""

    async def wait_ready(self, *, timeout_sec: float) -> None:
        return None


class _TaskStore:
    """What the executor asks the store, without the SDK's persistence."""

    async def get(self, task_id, context=None):
        del task_id, context
        return None


def _executor(gateway, drive_root) -> OuroborosA2AExecutor:
    return OuroborosA2AExecutor(
        max_concurrent=3,
        task_store=_TaskStore(),
        gateway=gateway,
        router=A2AResponseRouter(gateway.outbox),
        readiness=_ReadyNow(),
        session_id="sess-test",
        memory_events=RecordingMemoryEvents(),
        drive_root=drive_root,
    )


def _dispatch(
    tmp_path,
    *,
    task_id: str,
    message_id: str = "",
    text: str = "hello",
    answer: str | None = "answer",
):
    """Run one A2A request end to end over the seam, returning its envelope."""
    runtime_persistence.initialize()
    gateway = FakeGateway()
    executor = _executor(gateway, tmp_path)

    original_submit = gateway.inbox.submit

    def _submit_and_answer(envelope):
        original_submit(envelope)
        if answer is None:
            return
        # Stand in for the supervisor: the answer arrives on the outbox.
        reader = SettlingReader()
        answered = OutboundMessage(
            scope=envelope.scope, source="a2a", text=answer, correlation_id=envelope.correlation_id
        )
        executor.router.handle(reader, reader.deliver(answered))

    gateway.inbox.submit = _submit_and_answer  # type: ignore[method-assign]

    result = asyncio.run(executor._dispatch_to_supervisor(text, task_id, "ctx-1", message_id))
    return gateway.submitted[-1], result


def _a2a_scope(context_id: str = "ctx-1"):
    """The conversation shared by tasks in an A2A context."""
    return scope_with(TEST_SCOPE, surface_id="a2a", chat_id=str(derive_a2a_chat_id(context_id)))


def _owner_rows(drive_root):
    rows = list_owner_messages(drive_root, scope=_a2a_scope())
    return [message for message in rows if message.actor_type == "owner"]


def test_a2a_envelope_carries_the_delivery_id_as_client_message_id(tmp_path):
    engine = runtime_persistence.initialize().engine
    legacy_reads = []

    def record_read(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT") and parameters.get("chat_id") == str(derive_a2a_chat_id("task-42")):
            legacy_reads.append(statement)

    event.listen(engine, "before_cursor_execute", record_read)
    try:
        envelope, answer = _dispatch(tmp_path, task_id="task-42", message_id="message-42")
    finally:
        event.remove(engine, "before_cursor_execute", record_read)

    assert legacy_reads == []
    assert envelope.client_message_id == "message-42"
    assert _owner_rows(tmp_path)[0].source_message_id == "message-42"
    assert envelope.correlation_id == _owner_rows(tmp_path)[0].metadata["transport"]["correlation_id"]
    assert envelope.source == "a2a"
    assert answer == "answer"


def test_the_envelope_carries_the_trace_id_of_the_inbound_span(tmp_path):
    """Left out, it is resolved on the supervisor thread — empty contextvar, fresh uuid."""
    with aef_tracing.trace_scope(uuid.uuid4().hex) as trace_id:
        envelope, _answer = _dispatch(tmp_path, task_id="task-trace")

    assert trace_id
    assert envelope.trace_id == trace_id


def test_two_submissions_of_one_task_store_one_message(tmp_path):
    """At-least-once resubmission of the same envelope yields one record."""
    envelope, _answer = _dispatch(tmp_path, task_id="task-42")

    def _append():
        return append_chat_message(
            tmp_path,
            scope=envelope.scope,
            actor_type="owner",
            chat_id=envelope.chat_id,
            user_id=envelope.user_id,
            text=envelope.text,
            source=envelope.source,
            client_message_id=envelope.client_message_id,
        )

    first = _append()
    second = _append()

    assert first is None  # the dispatch already wrote it
    assert second is None
    assert len(_owner_rows(tmp_path)) == 1


def test_a_handed_over_request_is_marked_processed(tmp_path):
    """The journal marker is what tells the sweep this row needs no replay."""
    _dispatch(tmp_path, task_id="task-done")

    (row,) = _owner_rows(tmp_path)
    assert row.metadata["inbound_processed"] is True


def test_a_failed_submission_leaves_its_row_for_the_sweep(tmp_path):
    """A transport may not delete a persisted message to compensate for a failed submit."""
    runtime_persistence.initialize()
    gateway = FakeGateway()
    gateway.inbox.fail_with(RuntimeError("queue is down"))
    executor = _executor(gateway, tmp_path)

    with pytest.raises(RuntimeError):
        asyncio.run(executor._dispatch_to_supervisor("remember this", "task-lost", "ctx-1"))

    (row,) = _owner_rows(tmp_path)
    assert row.content == "remember this"
    assert row.metadata["inbound_processed"] is False


def test_the_sweep_resubmits_what_was_never_handed_over(tmp_path):
    """The replayed request reaches the agent even though its caller is gone."""
    runtime_persistence.initialize()
    failing = FakeGateway()
    failing.inbox.fail_with(RuntimeError("queue is down"))
    with pytest.raises(RuntimeError):
        asyncio.run(_executor(failing, tmp_path)._dispatch_to_supervisor("remember this", "task-lost", "ctx-1"))

    restarted = FakeGateway()
    assert resubmit_unprocessed(restarted, tmp_path) == 1

    (envelope,) = restarted.submitted
    assert envelope.source == "a2a"
    assert envelope.text == "remember this"
    assert envelope.chat_id == str(derive_a2a_chat_id("ctx-1"))
    assert envelope.correlation_id == _owner_rows(tmp_path)[0].metadata["transport"]["correlation_id"]
    assert envelope.transport == {
        "a2a_context_id": "ctx-1",
        "a2a_task_id": "task-lost",
        "correlation_id": envelope.correlation_id,
    }
    assert envelope.client_message_id == a2a_delivery_id(message_id="", task_id="task-lost", text="remember this")
    # Replayed once: the marker moves, so the next sweep finds nothing.
    assert resubmit_unprocessed(restarted, tmp_path) == 0


def test_the_sweep_leaves_handed_over_requests_alone(tmp_path):
    _dispatch(tmp_path, task_id="task-done")

    assert resubmit_unprocessed(FakeGateway(), tmp_path) == 0
