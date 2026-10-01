"""
A2A — Agent Executor.

Bridges incoming A2A messages to the Ouroboros supervisor via
handle_chat_direct(). Collects responses through LocalChatBridge
subscription mechanism.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import threading
import uuid
from typing import Any

from ouroboros.contracts.chat_id_policy import A2A_CHAT_ID_CEILING
from ouroboros.observability import aef_tracing

try:
    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.events import EventQueue

    from ouroboros.ingress.a2a_proto import (
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
#: The widest range that still survives a round trip through JSON in a browser, where these
#: ids reach ``/api/chat/history``. What a collision costs is bounded by what is *live*: the
#: chat id keys a response subscription that lasts one request, and at most A2A_MAX_CONCURRENT
#: of those exist at a time. Across all history it merges two conversations.
_A2A_CHAT_ID_MODULUS = 2**52


def derive_a2a_chat_id(task_id: str) -> int:
    """Chat id for an A2A task: stable per task id, so a retry reuses its conversation."""
    seed = str(task_id or "").strip() or uuid.uuid4().hex
    digest = int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16)
    return -(digest % _A2A_CHAT_ID_MODULUS) - _A2A_CHAT_ID_OFFSET


def a2a_delivery_id(*, message_id: str, task_id: str, text: str) -> str:
    """The one id a single delivery is known by, in history and in the envelope alike.

    It is the *message* id, not the task id: a task carries as many messages as the
    conversation has turns, and keying on the task would drop every one after the first as a
    duplicate of it. A caller that sends no message id gets one derived from the task and the
    text, so a redelivery still deduplicates.
    """
    explicit = str(message_id or "").strip()
    if explicit:
        return explicit
    return f"{task_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"


def _add_aef_input_response(
    controller,
    *,
    body: dict,
    http_code: int,
) -> None:
    try:
        controller.add_response(
            headers=aef_tracing.with_trace_header({}),
            body=aef_tracing.mask_trace_payload(body),
            http_code=http_code,
        )
    except Exception:
        log.debug("Failed to add AEF A2A input response", exc_info=True)


class OuroborosA2AExecutor(AgentExecutor):
    """Bridges A2A protocol to Ouroboros supervisor."""

    def __init__(self, max_concurrent: int = 3, task_store: Any | None = None):
        self._semaphore = threading.Semaphore(max_concurrent)
        self._active_tasks: set[str] = set()
        self._active_lock = threading.Lock()
        self._task_store = task_store

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
        if self._task_store is not None and task_id:
            try:
                existing = await self._task_store.get(
                    task_id,
                    getattr(context, "call_context", None),
                )
            except Exception:
                log.debug("A2A task store lookup failed for %s", task_id, exc_info=True)
                existing = None
            if existing is not None:
                return
        await event_queue.enqueue_event(
            new_task_event(task_id, context_id, "submitted"),
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        context_id = context.context_id or ""

        text = self._extract_text(context.message)
        message_id = str(getattr(context.message, "message_id", "") or "") if context.message is not None else ""
        request_body = {"task_id": task_id, "context_id": context_id, "message": text}

        with (
            aef_tracing.trace_scope(),
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
                _add_aef_input_response(
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
                _add_aef_input_response(
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
                    event_queue,
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
                    _add_aef_input_response(
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
                _add_aef_input_response(
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
                    _add_aef_input_response(
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
                _add_aef_input_response(
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
        event_queue: EventQueue,
        task_id: str,
        context_id: str,
        message_id: str = "",
    ) -> str:
        from ouroboros.app.supervisor_readiness import wait_for_supervisor_ready
        from supervisor.message_bus import try_get_bridge
        from supervisor.orchestrator.envelope import InboundEnvelope
        from supervisor.orchestrator.orchestrator import SubmissionCommitUnknown
        from supervisor.runtime_context import try_get_orchestrator

        try:
            from ouroboros.storage.settings.store import load_settings

            startup_timeout = float(load_settings().OUROBOROS_SUPERVISOR_STARTUP_TIMEOUT_SEC or 300)
        except (TypeError, ValueError):
            startup_timeout = 300.0
        await wait_for_supervisor_ready(timeout_sec=startup_timeout, require_bridge=True)

        bridge = try_get_bridge()
        if bridge is None:
            raise RuntimeError("Supervisor not ready — bridge not initialized yet")
        orchestrator = try_get_orchestrator()
        if orchestrator is None:
            raise RuntimeError("Supervisor orchestrator is not initialized")
        chat_id = derive_a2a_chat_id(task_id)
        delivery_id = a2a_delivery_id(message_id=message_id, task_id=task_id, text=text)
        response_event = asyncio.Event()
        response_holder: dict[str, str] = {}
        loop = asyncio.get_running_loop()

        def on_response(resp_text: str) -> None:
            response_holder["text"] = resp_text
            loop.call_soon_threadsafe(response_event.set)

        sub_id = bridge.subscribe_response(chat_id, on_response)
        try:
            envelope = InboundEnvelope.from_chat(
                chat_id=chat_id,
                user_id=chat_id,
                text=text,
                source="a2a",
                correlation_id=task_id,
                client_message_id=delivery_id,
            )

            # Before the submit, not after: the worker assembles its prompt from history, and
            # a request written afterwards can miss the very turn it started. A submit that
            # then fails *and says so* takes the row back out, so a refused request leaves none.
            recorded_id = ""
            try:
                recorded_id = await asyncio.to_thread(
                    self._record_incoming_message,
                    text=text,
                    task_id=task_id,
                    context_id=context_id,
                    chat_id=chat_id,
                    delivery_id=delivery_id,
                )
            except Exception:
                log.warning(
                    "Failed to persist incoming A2A message for task %s",
                    task_id,
                    exc_info=True,
                )

            def _submit() -> None:
                orchestrator.submit(envelope)

            try:
                await asyncio.to_thread(_submit)
            except SubmissionCommitUnknown:
                # Only a *confirmed* refusal takes the row back out. Here the task may well be
                # in the queue and only its commit record unreadable, so removing the request
                # would lose one the agent is about to answer. The row stays, and the delivery
                # id reconciles it: a redelivery derives the same task id, finds that task, and
                # adds neither a second row nor a second task.
                raise
            except Exception:
                # Not BaseException: cancelling ``to_thread`` abandons the await, not the
                # thread, which can still commit afterwards — the same unknown, kept the same way.
                if recorded_id:
                    await asyncio.to_thread(self._forget_incoming_message, recorded_id)
                raise

            try:
                timeout_sec = int(load_settings().OUROBOROS_HARD_TIMEOUT_SEC or 1800)
            except (TypeError, ValueError):
                timeout_sec = 1800
            try:
                await asyncio.wait_for(response_event.wait(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                raise TimeoutError(f"Agent did not respond within {timeout_sec}s")

            return response_holder.get("text", "(no response)")
        finally:
            bridge.unsubscribe_response(sub_id)

    @staticmethod
    def _record_incoming_message(
        *,
        text: str,
        task_id: str,
        context_id: str,
        chat_id: int,
        delivery_id: str,
    ) -> str:
        """Write the request to chat history, through the same door as every reply.

        ``delivery_id`` is passed in rather than derived here: the same id has to key this row
        and the envelope, or history deduplicates a redelivery while the supervisor queues it
        twice. :func:`a2a_delivery_id` is where it comes from.
        """
        from supervisor.message_bus import log_chat

        persisted = log_chat(
            "owner",
            chat_id,
            chat_id,
            text,
            source="a2a",
            sender_label=f"a2a:{context_id or task_id}",
            client_message_id=delivery_id,
            transport={"a2a_context_id": context_id},
            task_id=task_id,
        )
        return str(getattr(persisted, "message_id", "") or "")

    @staticmethod
    def _forget_incoming_message(message_id: str) -> None:
        """Undo the pre-submit write for a request the supervisor never accepted."""
        from supervisor.runtime_context import try_get_runtime

        try:
            runtime = try_get_runtime()
            if runtime is None:
                return
            from ouroboros.storage.runtime_store import get_runtime_store

            store = get_runtime_store(pathlib.Path(runtime.drive_root))
            if store is not None:
                store.delete_message(message_id)
        except Exception:
            log.warning("Failed to remove the unaccepted A2A message %s", message_id, exc_info=True)

    @staticmethod
    def _extract_text(message: Message | None) -> str:
        if message is None:
            return ""
        return get_message_text(message).strip()
