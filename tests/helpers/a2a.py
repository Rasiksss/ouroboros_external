"""A2A test helpers extracted from test_a2a_protocol."""

from __future__ import annotations

from typing import Any

from tests.helpers.fake_gateway import FakeGateway, RecordingMemoryEvents
from transport.a2a.delivery import A2AResponseRouter
from transport.a2a.executor import OuroborosA2AExecutor
from transport.a2a.interfaces import ISupervisorReadiness
from transport.a2a.proto import new_user_text_message


class _ReadyNow(ISupervisorReadiness):
    async def wait_ready(self, *, timeout_sec: float) -> None:
        return None


class StubTaskStore:
    def __init__(self, tasks=None):
        self.tasks = dict(tasks or {})

    async def get(self, task_id, context=None):
        del context
        return self.tasks.get(task_id)


def make_executor(drive_root, *, max_concurrent=3, gateway=None, task_store=None, router=None):
    gateway = gateway if gateway is not None else FakeGateway()
    return OuroborosA2AExecutor(
        max_concurrent=max_concurrent,
        task_store=task_store if task_store is not None else StubTaskStore(),
        gateway=gateway,
        router=router if router is not None else A2AResponseRouter(gateway.outbox),
        readiness=_ReadyNow(),
        session_id="sess-test",
        memory_events=RecordingMemoryEvents(),
        drive_root=drive_root,
    )


def make_a2a_context(text="hello", task_id="t1", context_id="c1"):
    """Build a minimal RequestContext-like object for testing."""
    from a2a.types.a2a_pb2 import Message, Role

    msg = new_user_text_message(text) if text else Message(role=Role.ROLE_USER, message_id="m1")

    class FakeContext:
        task_id: str
        context_id: str
        message: Any
        current_task: Any

    ctx = FakeContext()
    ctx.task_id = task_id
    ctx.context_id = context_id
    ctx.message = msg
    ctx.current_task = None
    return ctx


def make_event_queue():
    """A simple event collector implementing enqueue_event."""

    events: list = []

    class FakeEventQueue:
        async def enqueue_event(self, event):
            events.append(event)

    return FakeEventQueue(), events
