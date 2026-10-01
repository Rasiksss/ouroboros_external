"""Regression: the incoming A2A server already serves long work without holding the caller.

Adapted from the spike that settled the scope of the outbound change. The real stack runs —
the production app builder, ``DefaultRequestHandler``, ``OuroborosA2AExecutor``, a real task
store — with only ``_dispatch_to_supervisor`` replaced by a body the test holds open.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest

# Requires a2a-sdk[http-server]==1.0.3 (see pyproject.toml).
pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types.a2a_pb2 import CancelTaskRequest, GetTaskRequest, SendMessageRequest

from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor, a2a_delivery_id, derive_a2a_chat_id
from ouroboros.ingress.a2a_proto import (
    build_agent_card_proto,
    new_user_text_message,
    task_state_name,
)
from ouroboros.ingress.a2a_server import _build_starlette_app
from ouroboros.ingress.a2a_task_store import FileTaskStore

_SERVICE_URL = "http://a2a-ingress.test/"
#: Ceiling on every wait here. Only an ingress that never answers should reach it.
_PATIENCE_SEC = 30.0


class _GatedExecutor(OuroborosA2AExecutor):
    """The production executor with the supervisor replaced by a body the test releases.

    A body on a timer turns each assertion into a race against the clock: a loaded runner can
    let a 1.5s body finish before a cancel that was sent long before it even arrives, and the
    cancel then lands behind an already queued result. A body held on a gate ends when the test
    says so, so the interleaving is the same on every machine.

    The ingress write is kept: it is what the real dispatch does once the envelope is accepted.
    """

    def __init__(self, *, task_store) -> None:
        super().__init__(max_concurrent=3, task_store=task_store)
        self._release = asyncio.Event()
        self.bodies_started: list[str] = []
        self.bodies_finished: list[str] = []

    async def _dispatch_to_supervisor(self, text, event_queue, task_id, context_id, message_id="") -> str:
        self._record_incoming_message(
            text=text,
            task_id=task_id,
            context_id=context_id,
            chat_id=derive_a2a_chat_id(task_id),
            delivery_id=a2a_delivery_id(message_id=message_id, task_id=task_id, text=text),
        )
        self.bodies_started.append(task_id)
        await asyncio.wait_for(self._release.wait(), timeout=_PATIENCE_SEC)
        self.bodies_finished.append(task_id)
        return f"done: {text}"

    def finish_bodies(self) -> None:
        """Lets every body that is still running return its result."""
        self._release.set()


@asynccontextmanager
async def _ingress(tmp_path):
    """The real ingress app, reachable in-process, plus an SDK client pointed at it."""
    store = FileTaskStore(tmp_path / "data", ttl_hours=24)
    executor = _GatedExecutor(task_store=store)
    card = build_agent_card_proto(
        name="Ingress Under Test",
        description="",
        version="1.0.0",
        service_url=_SERVICE_URL,
        skills=[],
    )
    handler = DefaultRequestHandler(agent_executor=executor, task_store=store, agent_card=card)
    app = _build_starlette_app(card, handler)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=_SERVICE_URL) as http_client:
        factory = ClientFactory(ClientConfig(httpx_client=http_client, streaming=False, polling=True))
        try:
            yield factory.create(card), executor
        finally:
            # Teardown never waits on a body the test forgot to release.
            executor.finish_bodies()


async def _submit(client, text: str):
    async for response in client.send_message(SendMessageRequest(message=new_user_text_message(text))):
        return response
    raise AssertionError("send_message yielded nothing")


async def _until(condition, *, what: str) -> None:
    """Waits for a condition instead of for a duration — a slow machine only makes it later."""
    deadline = time.monotonic() + _PATIENCE_SEC
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def _poll_until(client, task_id: str, state: str, seen: list[str] | None = None):
    """Polls one task until it reports ``state``, recording the states it passed through."""
    deadline = time.monotonic() + _PATIENCE_SEC
    while True:
        task = await client.get_task(GetTaskRequest(id=task_id))
        current = task_state_name(task.status.state)
        if seen is not None and (not seen or seen[-1] != current):
            seen.append(current)
        if current == state:
            return task
        assert time.monotonic() < deadline, f"task {task_id} never reached {state!r}, last state was {current!r}"
        await asyncio.sleep(0.01)


def _artifact_text(task) -> str:
    return "\n".join(part.text for artifact in task.artifacts for part in artifact.parts if part.text)


def test_return_immediately_answers_long_before_the_body_does(tmp_path):
    async def scenario():
        async with _ingress(tmp_path) as (client, executor):
            response = await asyncio.wait_for(_submit(client, "long job"), timeout=_PATIENCE_SEC)

            assert response.HasField("task")
            assert task_state_name(response.task.status.state) in {"submitted", "working"}
            # The whole point: the answer is already here while the body is still held open.
            assert executor.bodies_finished == []
            await _until(
                lambda: executor.bodies_started == [response.task.id],
                what="the body of the submitted task to start",
            )

    asyncio.run(scenario())


def test_polling_sees_working_then_the_finished_artifact(tmp_path):
    async def scenario():
        async with _ingress(tmp_path) as (client, executor):
            response = await _submit(client, "long job")
            task_id = response.task.id

            seen: list[str] = []
            await _poll_until(client, task_id, "working", seen)
            executor.finish_bodies()
            await _poll_until(client, task_id, "completed", seen)

            assert seen[-2:] == ["working", "completed"], seen
            # The artifact outlives the connection the request was made on.
            finished = await client.get_task(GetTaskRequest(id=task_id))
            assert "done: long job" in _artifact_text(finished)

    asyncio.run(scenario())


def test_a_completed_task_left_its_request_in_history(tmp_path, monkeypatch):
    """The whole point of writing at ingress: the request outlives the call."""
    import supervisor.runtime_context as runtime_context
    import supervisor.state as supervisor_state
    from ouroboros.storage.chat_store import list_chat_history_messages
    from ouroboros.storage.runtime_models import OwnerRuntimeState
    from ouroboros.storage.runtime_store import init_runtime_store

    init_runtime_store(tmp_path)

    class _Runtime:
        drive_root = tmp_path

    state = OwnerRuntimeState(session_id="s-ingress")

    class _StateRepo:
        def load(self):
            return state

        def session_id(self):
            return str(state.session_id)

    _Runtime.state_repo = _StateRepo()
    monkeypatch.setattr(runtime_context, "try_get_runtime", lambda: _Runtime())
    monkeypatch.setattr(supervisor_state, "load_state", lambda: state)

    async def scenario():
        async with _ingress(tmp_path) as (client, executor):
            response = await _submit(client, "remember this")
            task_id = response.task.id

            executor.finish_bodies()
            await _poll_until(client, task_id, "completed")

        recorded = list_chat_history_messages(tmp_path, limit=10)
        assert [message.content for message in recorded] == ["remember this"]
        assert recorded[0].metadata["task_id"] == task_id

    asyncio.run(scenario())


def test_cancel_stops_the_body_from_completing(tmp_path):
    async def scenario():
        async with _ingress(tmp_path) as (client, executor):
            cancelled = await _submit(client, "cancel me")
            survivor = await _submit(client, "let me finish")

            # The cancel has to reach a body that is running, or it proves nothing about cancelling.
            await _until(
                lambda: set(executor.bodies_started) == {cancelled.task.id, survivor.task.id},
                what="both bodies to start",
            )
            await client.cancel_task(CancelTaskRequest(id=cancelled.task.id))

            executor.finish_bodies()
            other = await _poll_until(client, survivor.task.id, "completed")

            stopped = await client.get_task(GetTaskRequest(id=cancelled.task.id))
            assert task_state_name(stopped.status.state) == "canceled"
            assert _artifact_text(stopped) == ""
            assert cancelled.task.id not in executor.bodies_finished
            # The parallel task did reach its result, so the difference is the cancel.
            assert "done: let me finish" in _artifact_text(other)

    asyncio.run(scenario())
