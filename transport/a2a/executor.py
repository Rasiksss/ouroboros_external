"""A2A agent executor: one JSON-RPC request in, one agent answer out.

Submits through the seam's inbox and waits for the answer on the transport's own
outbox reader, so nothing here knows what the supervisor does with the message —
in a multi-instance deployment the task may run somewhere else entirely.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import threading
import uuid
from typing import Any

from background_memory.infra.events import announce_chat_message_persisted
from background_memory.interfaces import IMemoryEventPublisher
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.chat_id_policy import A2A_CHAT_ID_CEILING
from ouroboros.contracts.owner_scope import configured_agent_id, configured_owner_id
from ouroboros.observability import aef_tracing
from ouroboros.storage.chat_store import append_chat_message
from transport.a2a.delivery import TRANSPORT_NAME, A2AResponseRouter
from transport.a2a.interfaces import ISupervisorReadiness
from transport.interfaces import IEnvelopeBuilder, IMessageGateway, InboundEnvelope
from transport.journal import DEFAULT_SWEEP_LIMIT, UNPROCESSED_METADATA, mark_processed, replay_unprocessed

try:
    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.events import EventQueue

    from transport.a2a.proto import (
        Message,
        TaskStatusUpdateEvent,
        get_message_text,
        new_task_event,
        new_text_artifact_update_event,
        new_text_status_update_event,
        task_state_value,
    )

    _A2A_AVAILABLE = True
except ImportError:
    _A2A_AVAILABLE = False
    AgentExecutor = object  # type: ignore[assignment,misc]
    RequestContext = EventQueue = None  # type: ignore[assignment,misc]
    Message = None  # type: ignore[assignment,misc]
    TaskStatusUpdateEvent = None  # type: ignore[assignment,misc]

log = logging.getLogger("a2a-server")

#: Keeps derived ids below the skill bridge's window, which is what the ceiling names.
_A2A_CHAT_ID_OFFSET = -A2A_CHAT_ID_CEILING
#: Fits JSON number precision; a hash collision would merge conversation history.
_A2A_CHAT_ID_MODULUS = 2**52


def derive_a2a_chat_id(context_id: str) -> int:
    """Stable chat id for an A2A context, or a task when no context is available."""
    seed = str(context_id or "") or uuid.uuid4().hex
    digest = int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16)
    return -(digest % _A2A_CHAT_ID_MODULUS) - _A2A_CHAT_ID_OFFSET


def a2a_delivery_id(*, message_id: str, task_id: str, text: str) -> str:
    """Preserve the caller's unique message id, with a fallback for id-less messages."""
    explicit = str(message_id or "").strip()
    if explicit:
        return explicit
    return f"{task_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"


def _reply_correlation(delivery_id: str, text: str) -> str:
    """A conflicting payload must not consume the accepted message's answer."""
    return f"{delivery_id}:{hashlib.sha256(text.encode()).hexdigest()}"


class OuroborosA2AExecutor(AgentExecutor):
    """Bridges A2A protocol to Ouroboros supervisor."""

    def __init__(
        self,
        *,
        max_concurrent: int,
        task_store: Any,
        gateway: IMessageGateway,
        router: A2AResponseRouter,
        readiness: ISupervisorReadiness,
        session_id: str,
        memory_events: IMemoryEventPublisher,
        drive_root: pathlib.Path,
    ):
        self._semaphore = threading.Semaphore(max_concurrent)
        self._active_tasks: set[str] = set()
        self._active_lock = threading.Lock()
        self._task_store = task_store
        self._gateway = gateway
        self.router = router
        self._readiness = readiness
        self._session_id = str(session_id or "")
        self._memory_events = memory_events
        self._drive_root = pathlib.Path(drive_root)

    async def _ensure_task_on_queue(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
    ) -> None:
        """Enqueue initial Task before TaskStatusUpdateEvent (a2a-sdk requirement)."""
        if getattr(context, "current_task", None) is not None:
            return
        if await self._stored_task(context, task_id) is not None:
            return
        await event_queue.enqueue_event(
            new_task_event(task_id, context_id, "submitted"),
        )

    async def _stored_task(self, context: RequestContext, task_id: str) -> Any | None:
        if not task_id:
            return None
        try:
            return await self._task_store.get(task_id, getattr(context, "call_context", None))
        except Exception:
            log.debug("A2A task store lookup failed for %s", task_id, exc_info=True)
            return None

    async def _answer_with_running_status(
        self,
        event_queue: EventQueue,
        *,
        task_id: str,
        context_id: str,
    ) -> bool:
        """Report a task *this process* is running instead of submitting it twice.

        A client retrying with the same task id wants the state of the work it
        already asked for; resubmitting would enqueue the same request twice.
        Keyed on the in-memory set, not on the stored state: a persisted
        non-terminal task nobody here owns was left behind by a process that is
        gone, and answering its retry with a status would make that task id
        permanently unanswerable.
        """
        with self._active_lock:
            if task_id not in self._active_tasks:
                return False
        log.info("A2A task %s is already running here; answering with its status", task_id)
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=task_state_value("working"),
                final=False,
            )
        )
        return True

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        # SDK 1.0.3 generates a fresh context for a task-only continuation.
        current_task = getattr(context, "current_task", None)
        context_id = (current_task.context_id if current_task is not None else context.context_id) or ""

        text = self._extract_text(context.message)
        message_id = str(getattr(context.message, "message_id", "") or "") if context.message is not None else ""
        request_body = {"task_id": task_id, "context_id": context_id, "message": text}

        # A2A is an acceptor: the caller's operation identifier arrives on the request headers,
        # which the per-request call context carries here as data. An absent call context is a
        # request with no carrier, not an error, so it mints the way an absent header does.
        call_context = getattr(context, "call_context", None)
        inbound_headers = getattr(call_context, "state", {}).get("headers") if call_context else None

        with (
            aef_tracing.trace_scope(aef_tracing.accept_trace_id_from_headers(inbound_headers, source="a2a")),
            aef_tracing.input_request(
                headers=aef_tracing.with_trace_header({}),
                body=aef_tracing.mask_trace_payload(request_body),
                path="/a2a",
                method="POST",
            ) as input_span,
        ):
            if not text:
                await self._ensure_task_on_queue(
                    context,
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                )
                await event_queue.enqueue_event(
                    new_text_status_update_event(
                        task_id,
                        context_id,
                        task_state_value("failed"),
                        "Empty message received",
                    )
                )
                aef_tracing.add_response(
                    input_span,
                    body={
                        "task_id": task_id,
                        "context_id": context_id,
                        "status": "failed",
                        "error": "Empty message received",
                    },
                    http_code=400,
                )
                return

            if await self._answer_with_running_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
            ):
                aef_tracing.add_response(
                    input_span,
                    body={"task_id": task_id, "context_id": context_id, "status": "running"},
                    http_code=200,
                )
                return

            if not self._semaphore.acquire(blocking=False):
                await self._ensure_task_on_queue(
                    context,
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                )
                await event_queue.enqueue_event(
                    new_text_status_update_event(
                        task_id,
                        context_id,
                        task_state_value("rejected"),
                        "Too many concurrent tasks",
                    )
                )
                aef_tracing.add_response(
                    input_span,
                    body={
                        "task_id": task_id,
                        "context_id": context_id,
                        "status": "rejected",
                        "error": "Too many concurrent tasks",
                    },
                    http_code=429,
                )
                return

            with self._active_lock:
                self._active_tasks.add(task_id)
            try:
                await self._ensure_task_on_queue(
                    context,
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                )
                await event_queue.enqueue_event(
                    new_text_status_update_event(
                        task_id=task_id,
                        context_id=context_id,
                        state=task_state_value("working"),
                        final=False,
                    )
                )

                response_text = await self._dispatch_to_supervisor(
                    text,
                    task_id,
                    context_id,
                    message_id,
                )

                with self._active_lock:
                    still_active = task_id in self._active_tasks

                if not still_active:
                    log.debug(
                        "A2A task %s was canceled during dispatch — skipping completion event",
                        task_id,
                    )
                    aef_tracing.add_response(
                        input_span,
                        body={"task_id": task_id, "context_id": context_id, "status": "canceled"},
                        http_code=499,
                    )
                    return

                await event_queue.enqueue_event(
                    new_text_artifact_update_event(
                        task_id,
                        context_id,
                        name="response",
                        text=response_text,
                        last_chunk=True,
                    )
                )
                await event_queue.enqueue_event(
                    new_text_status_update_event(
                        task_id=task_id,
                        context_id=context_id,
                        state=task_state_value("completed"),
                        final=True,
                    )
                )
                aef_tracing.add_response(
                    input_span,
                    body={
                        "task_id": task_id,
                        "context_id": context_id,
                        "status": "completed",
                        "response": response_text,
                    },
                    http_code=200,
                )
            except Exception as exc:
                log.error("A2A task %s failed: %s", task_id, exc, exc_info=True)
                with self._active_lock:
                    still_active = task_id in self._active_tasks
                if not still_active:
                    aef_tracing.add_response(
                        input_span,
                        body={"task_id": task_id, "context_id": context_id, "status": "canceled"},
                        http_code=499,
                    )
                    return
                await self._ensure_task_on_queue(
                    context,
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                )
                await event_queue.enqueue_event(
                    new_text_status_update_event(
                        task_id,
                        context_id,
                        task_state_value("failed"),
                        f"Task failed: {exc}",
                    )
                )
                aef_tracing.add_response(
                    input_span,
                    body={
                        "task_id": task_id,
                        "context_id": context_id,
                        "status": "failed",
                        "error": str(exc),
                    },
                    http_code=500,
                )
            finally:
                self._semaphore.release()
                with self._active_lock:
                    self._active_tasks.discard(task_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        with self._active_lock:
            self._active_tasks.discard(task_id)
        await self._ensure_task_on_queue(
            context,
            event_queue,
            task_id=task_id,
            context_id=context_id,
        )
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=task_state_value("canceled"),
                final=True,
            )
        )

    async def _dispatch_to_supervisor(
        self,
        text: str,
        task_id: str,
        context_id: str,
        message_id: str = "",
    ) -> str:
        from ouroboros.storage.settings.store import load_settings

        try:
            startup_timeout = float(load_settings().OUROBOROS_SUPERVISOR_STARTUP_TIMEOUT_SEC or 300)
        except (TypeError, ValueError):
            startup_timeout = 300.0
        await self._readiness.wait_ready(timeout_sec=startup_timeout)

        delivery_id = a2a_delivery_id(message_id=message_id, task_id=task_id, text=text)
        correlation_id = _reply_correlation(delivery_id, text)
        chat_id = derive_a2a_chat_id(context_id or task_id)
        envelope = InboundEnvelope(
            scope=OwnerScope(
                owner_id=configured_owner_id(),
                agent_id=configured_agent_id(),
                surface_id=SurfaceId(TRANSPORT_NAME),
                chat_id=str(chat_id),
            ),
            user_id=chat_id,
            text=text,
            correlation_id=correlation_id,
            client_message_id=delivery_id,
            trace_id=aef_tracing.current_trace_id(),
            transport={"a2a_context_id": context_id, "a2a_task_id": task_id, "correlation_id": correlation_id},
        )
        # Register before submission so even an immediate answer finds its waiter.
        pending = self.router.register(envelope.scope, envelope.correlation_id, asyncio.get_running_loop())
        try:
            # Persist before submission so the worker sees this turn and failed submits are replayable.
            recorded_id = ""
            try:
                recorded_id = await asyncio.to_thread(self._record_incoming_message, envelope)
            except Exception:
                log.warning(
                    "Failed to persist incoming A2A message for task %s",
                    task_id,
                    exc_info=True,
                )

            # The seam answers only whether the message was accepted for delivery: what
            # it becomes is decided on the supervisor side, possibly on another instance.
            await asyncio.to_thread(self._gateway.inbox.submit, envelope)
            if recorded_id:
                await asyncio.to_thread(mark_processed, self._drive_root, recorded_id)

            try:
                timeout_sec = int(load_settings().OUROBOROS_HARD_TIMEOUT_SEC or 1800)
            except (TypeError, ValueError):
                timeout_sec = 1800
            try:
                return await pending.wait(timeout_sec)
            except asyncio.TimeoutError:
                raise TimeoutError(f"Agent did not respond within {timeout_sec}s")
        finally:
            # Releasing here is what makes a late answer a drop rather than a
            # delivery to a request that has already failed.
            self.router.release(envelope.scope, envelope.correlation_id, pending)

    def _record_incoming_message(self, envelope: InboundEnvelope) -> str:
        """Persist the request under the envelope's scope and delivery id before submission."""
        persisted = append_chat_message(
            self._drive_root,
            scope=envelope.scope,
            actor_type="owner",
            chat_id=envelope.chat_id,
            user_id=envelope.user_id,
            text=envelope.text,
            session_id=self._session_id,
            source=envelope.source,
            # Without it the inbound render prints `[User]` for a remote peer.
            sender_label=f"a2a:{envelope.transport['a2a_context_id'] or envelope.transport['a2a_task_id']}",
            client_message_id=envelope.client_message_id,
            transport=envelope.transport,
            task_id=str(envelope.transport["a2a_task_id"]),
            extra_metadata=dict(UNPROCESSED_METADATA),
        )
        if persisted is None:
            # A redelivery: the row is already there under the same delivery id.
            return ""
        announce_chat_message_persisted(self._memory_events, envelope.scope, str(persisted.message_id or ""))
        return str(persisted.message_id or "")

    @staticmethod
    def _extract_text(message: Message | None) -> str:
        if message is None:
            return ""
        return get_message_text(message).strip()


class _A2AEnvelopeBuilder(IEnvelopeBuilder):
    """Rebuilds the envelope of a stored A2A request, for the journal sweep."""

    def build(self, record: Any) -> InboundEnvelope:
        metadata = record.metadata or {}
        task_id = str(metadata.get("task_id") or "")
        chat_id = int(record.chat_id or derive_a2a_chat_id(task_id))
        correlation_id = _reply_correlation(str(record.source_message_id or ""), record.content)
        return InboundEnvelope(
            scope=OwnerScope(
                owner_id=record.owner_id,
                agent_id=record.agent_id,
                surface_id=SurfaceId(TRANSPORT_NAME),
                chat_id=str(chat_id),
            ),
            user_id=chat_id,
            text=record.content,
            sender_label=str(metadata.get("sender_label") or ""),
            client_message_id=str(record.source_message_id or ""),
            correlation_id=correlation_id,
            transport={**dict(metadata.get("transport") or {}), "correlation_id": correlation_id},
        )


def resubmit_unprocessed(
    gateway: IMessageGateway,
    drive_root: pathlib.Path,
    *,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> int:
    """Submit again every A2A request persisted but never handed over.

    The JSON-RPC caller died with the process that took the request, so the answer
    reaches the orphan branch of the router and is dropped — the point of the replay
    is that the peer's message still reaches the agent and lands in history.
    """
    return replay_unprocessed(
        gateway,
        drive_root,
        source=TRANSPORT_NAME,
        build=_A2AEnvelopeBuilder(),
        limit=limit,
    )
