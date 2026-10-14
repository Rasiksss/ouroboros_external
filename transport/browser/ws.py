"""Browser WebSocket /ws handler."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import pathlib
import threading
import uuid
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Any, TypeVar, cast

import anyio
from pydantic import JsonValue, TypeAdapter
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.websockets import WebSocket, WebSocketDisconnect

from infra.async_engine import async_engine_factory
from infra.security_context import security_context, security_context_collector
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.attachments import AttachmentMetadata
from ouroboros.contracts.chat_commands import answered_slash_command, runtime_slash_command
from ouroboros.contracts.tool_confirmation import CONFIRMATION_TRANSPORT_KEY
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_bus import SKILL_LIFECYCLE, get_global_event_bus
from ouroboros.policy.safety import UNCHECKED_WARNING_SUFFIX, check_safety
from ouroboros.storage.attachment_validation import attachment_max_bytes
from ouroboros.storage.blob.async_backend import AsyncBlob
from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.chat_store import InboundMessageStore, InboundRefused, build_chat_message
from ouroboros.storage.conversations.message_write import amark_message_processed
from ouroboros.storage.file_catalog import FileCatalog, FileError, FileFailure, FileTransferPending
from ouroboros.storage.file_models import attachment_metadata
from ouroboros.storage.file_transfers import get_transfer_limits
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.settings.paths import load_path_settings
from ouroboros.storage.settings.store import load_settings
from ouroboros.utils import set_log_sink, utc_now_iso
from transport.browser.contracts import (
    COMMAND_CHECK_UNAVAILABLE,
    COMMAND_NOT_ALLOWED,
    WS_MESSAGE_TYPES,
    BrowserEcho,
    BrowserOutbound,
    ChatOutbound,
    ConfirmationRequestOutbound,
    ExtensionLifecycleOutbound,
    LogOutbound,
    PhotoOutbound,
    browser_attachment,
)
from transport.browser.identity import (
    BROWSER_CHAT_ID,
    CHAT_HEADER_NAME,
    OWNER_HEADER_NAME,
    TRANSPORT_NAME,
    IdentityRefused,
    browser_identity,
    trusted_chat_id,
    trusted_owner_id,
)
from transport.inbound_limits import oversized_message_refusal
from transport.interfaces import (
    DeliveredMessage,
    IEnvelopeBuilder,
    IMessageGateway,
    InboundEnvelope,
    IOutboxReader,
    OutboundKind,
    OutboundMessage,
    SubscriptionMode,
    routed_chat_id,
)
from transport.journal import (
    DEFAULT_SWEEP_LIMIT,
    replay_unprocessed,
    stored_attachments,
)
from transport.reader_loop import READER_JOIN_TIMEOUT_MARGIN_SEC, run_reader_loop

log = logging.getLogger(__name__)

#: What one scheduled coroutine returns; `_schedule` hands its future back unchanged.
_T = TypeVar("_T")

#: Marks an outbound message as the echo of a user's own line and carries the
#: facts its frame needs beyond text and timestamp. The echo travels the outbox
#: like assistant output, because the connections it must reach are not only the
#: ones this process holds.
ECHO_TRANSPORT_KEY = "browser_echo"
_BROWSER_ECHO_ADAPTER = TypeAdapter(BrowserEcho)
#: Where a connection keeps its writer lock. Namespaced because it is set on an
#: object this module does not own.
_SEND_LOCK_ATTR = "_ouroboros_send_lock"
#: RFC 6455 "policy violation": the handshake presented no identity this port accepts.
_IDENTITY_REFUSED_CLOSE_CODE = 1008
_SECURITY_CONTEXT_UNSAVED_CLOSE_CODE = 1011
_SECURITY_CONTEXT_UNSAVED_REASON = "the caller's security context could not be recorded"
_HEARTBEAT_INTERVAL_SEC = 30.0
_OUTBOX_POLL_TIMEOUT_SEC = 0.5
_ADMISSION_RETRY_BACKOFF_SEC = 0.5
#: One connection's own deadline for taking a frame, covering both waiting for its
#: turn on that socket and the send itself. Small on purpose: a socket that has not
#: taken a frame in this long is not slow, it is wedged, and every other owner on
#: the instance is paying for it.
_CLIENT_SEND_TIMEOUT_SEC = 2.0
#: How long a payload may take to resolve — an object-storage read plus base64 —
#: before the reader abandons it. Larger than a send because it is a network read
#: of a whole blob; bounded because the reader holds the in-flight slot throughout.
_PAYLOAD_RESOLVE_TIMEOUT_SEC = 5.0
#: The hop onto the event loop and back. The fan-out itself is concurrent, so the
#: bound below is one connection's deadline plus this — not one per connection.
_HANDOVER_MARGIN_SEC = 1.0
#: How long the reader waits for a frame to reach every connection. Composed of its
#: parts rather than picked, so changing one cannot leave the whole inconsistent.
_DELIVERY_TIMEOUT_SEC = _CLIENT_SEND_TIMEOUT_SEC + _HANDOVER_MARGIN_SEC
#: The whole budget one message can hold the subscription's single in-flight slot:
#: resolve the payload, then hand it to every connection. Independent of how many
#: connections this instance holds, which is the property a multi-owner process needs.
_MESSAGE_BUDGET_SEC = _PAYLOAD_RESOLVE_TIMEOUT_SEC + _DELIVERY_TIMEOUT_SEC
#: Long enough for the reader to finish the poll it is in plus the message it took.
#: Shorter would release the subscription under a reader still holding a message,
#: which the backend requeues behind the delivery that afterwards succeeds.
_READER_JOIN_TIMEOUT_SEC = _OUTBOX_POLL_TIMEOUT_SEC + _MESSAGE_BUDGET_SEC + READER_JOIN_TIMEOUT_MARGIN_SEC
#: Attempts at one frame before it is dropped. The ladder now fires only for a
#: handover that never started, so the worst case is three budgets of silence for
#: a loop that is not running work — and a loop that never recovers still releases
#: the slot instead of holding it against every message behind it.
_BROADCAST_ATTEMPT_LIMIT = 3
#: How long an operator waits for their command to be judged. Seconds, because the
#: person who sent it is holding a socket — the safety backend's own bound is
#: minutes, sized for an agent loop, and inheriting it was an accident of calling
#: it in line.
_POLICY_CHECK_TIMEOUT_SEC = 15.0
#: How many policy checks may be in flight on this instance. An abandoned check keeps
#: its slot until the backend gives up on its own, so this is what turns "a thread per
#: command frame" into a refusal — the frame is not operator-gated, and any connection
#: can send it.
_POLICY_CHECK_SLOTS = threading.BoundedSemaphore(2)


class _TooManyChecksInFlight(RuntimeError):
    """Raised when no policy-check slot is free; answered as an unavailable check."""


class DeliveryOutcome(str, Enum):
    """What a delivery attempt actually did — the reader settles on this.

    Only ``NOT_HANDED_OVER`` is worth another attempt: it means the runtime that
    performs sends never ran the delivery, which is the condition the blocking
    wait exists for. Everything else ran, and offering it again would spend every
    other owner's budget on the same outcome.
    """

    DELIVERED = "delivered"
    NO_RECIPIENT = "no_recipient"
    UNRESOLVED = "unresolved"
    FAILED = "failed"
    NOT_HANDED_OVER = "not_handed_over"


def connection_send_lock(websocket: WebSocket) -> asyncio.Lock:
    """The lock every writer to this connection takes, kept on the connection itself.

    Held for the duration of one send, so two writers never interleave: an
    outbound delivery, the heartbeat, an error frame answered on the connection's
    own task and the close that ends it all reach the same channel from different
    tasks, and two coroutines inside one ASGI ``send`` corrupt the frame stream
    rather than merely reordering it.

    On the socket rather than on ``BrowserClient`` because the socket is what the
    writers have in common: several of them never see the pairing, and a lock only
    half the writers take is not a lock.
    """
    lock: asyncio.Lock | None = getattr(websocket, _SEND_LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(websocket, _SEND_LOCK_ATTR, lock)
    return lock


@dataclass(frozen=True)
class BrowserClient:
    """One live connection and the identity it presented at its handshake.

    The scope is held here rather than rebuilt per frame: a frame that could supply an
    axis is a sender naming its own owner, which is the property this pairing removes.
    """

    websocket: WebSocket
    scope: OwnerScope

    @property
    def send_lock(self) -> asyncio.Lock:
        """The connection's one writer lock — see ``connection_send_lock``."""
        return connection_send_lock(self.websocket)

    @property
    def chat_id(self) -> int | str:
        """The conversation this connection is in, through the seam's one conversion."""
        return routed_chat_id(self.scope)


class BrowserWebSocketHub:
    """Own browser clients, bridge wiring, and background subscriptions."""

    def __init__(self, *, heartbeat_interval_sec: float | None = None) -> None:
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._clients: list[BrowserClient] = []
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._lifecycle_sub_id: str | None = None
        self._gateway: IMessageGateway | None = None
        self._drive_root: pathlib.Path | None = None
        self._reader: Any | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stopping = threading.Event()
        self.file_blob: AsyncBlob | None = None
        #: Tasks nobody awaits — a dropped connection's close, and a send that was
        #: abandoned at its deadline. Held so each has an owner and an error sink.
        self._side_tasks: set[asyncio.Task[Any]] = set()

    def has_clients(self) -> bool:
        with self._lock:
            return bool(self._clients)

    def add_client(self, client: BrowserClient) -> int:
        with self._lock:
            self._clients.append(client)
            return len(self._clients)

    def remove_client(self, client: BrowserClient) -> int:
        with self._lock:
            with suppress(ValueError):
                self._clients.remove(client)
            return len(self._clients)

    def clients_for(self, *, owner_id: str, agent_id: str, chat_id: int | str) -> list[BrowserClient]:
        chat = str(chat_id)
        with self._lock:
            return [
                client
                for client in self._clients
                if client.scope.owner_id == owner_id
                and client.scope.agent_id == agent_id
                and client.scope.chat_id == chat
            ]

    async def close_all(
        self,
        *,
        code: int = 1012,
        reason: str = "Server restarting",
    ) -> None:
        with self._lock:
            clients = list(self._clients)

        async def close(client: BrowserClient) -> None:
            try:
                async with client.send_lock:
                    await client.websocket.close(code=code, reason=reason)
            except Exception:
                log.debug("Failed to close browser WebSocket", exc_info=True)

        tasks = [asyncio.create_task(close(client)) for client in clients]
        for task in tasks:
            self._adopt(task)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_CLIENT_SEND_TIMEOUT_SEC)
            if pending:
                for task in pending:
                    task.cancel()
                raise TimeoutError("Browser connections did not close within the shutdown budget")

    async def broadcast(self, message: BrowserOutbound) -> None:
        """Broadcast ownerless frames; owner content must use deliver."""
        with self._lock:
            clients = list(self._clients)
        await self._send(clients, message)

    async def deliver(self, message: BrowserOutbound, *, owner_id: str, agent_id: str, chat_id: int | str) -> int:
        return await self._send(self.clients_for(owner_id=owner_id, agent_id=agent_id, chat_id=chat_id), message)

    async def _send(self, clients: list[BrowserClient], message: BrowserOutbound) -> int:
        if not clients:
            return 0
        data = await anyio.to_thread.run_sync(lambda: json.dumps(message, ensure_ascii=False, default=str))
        message_type = str(message.get("type", "unknown"))
        results = await asyncio.gather(
            *(self._send_one(client, data, message_type) for client in clients),
            return_exceptions=True,
        )
        return sum(1 for result in results if result is True)

    async def _send_one(self, client: BrowserClient, data: str, message_type: str) -> bool:
        """One connection's own attempt, under its own deadline.

        The send is not cancelled at the deadline: cancelling inside an ASGI
        ``send`` can leave a half-written frame on the wire. It is handed to the
        drop instead, which ends it once no other writer is behind it.
        """
        send = asyncio.ensure_future(self._locked_send(client, data))
        self._adopt(send)
        done, _pending = await asyncio.wait({send}, timeout=_CLIENT_SEND_TIMEOUT_SEC)
        if not done:
            log.info(
                "WebSocket did not take msg type=%s within %.1fs; dropping client",
                message_type,
                _CLIENT_SEND_TIMEOUT_SEC,
            )
            self._drop_client(client, still_writing=send)
            return False
        error = send.exception()
        if error is None:
            return True
        log.info(
            "WebSocket send failed for msg type=%s; dropping client (%s)",
            message_type,
            type(error).__name__,
        )
        self._drop_client(client)
        return False

    @staticmethod
    async def _locked_send(client: BrowserClient, data: str) -> None:
        async with client.send_lock:
            await client.websocket.send_text(data)

    def _drop_client(self, client: BrowserClient, *, still_writing: asyncio.Future[None] | None = None) -> None:
        """Take an unresponsive connection off the roster and close it.

        Immediately, rather than leaving it to the connection's own handler: the
        point is that the *next* message must not pay this connection's deadline
        again. Nothing durable is lost — the conversation is in owner history and
        the client re-reads it when it reconnects.

        ``still_writing`` is the send that was abandoned at its deadline, if any;
        the close has to reckon with it rather than write past it.
        """
        self.remove_client(client)
        self._adopt(asyncio.create_task(self._close_dropped(client, still_writing), name="browser-ws-drop"))

    def _adopt(self, task: asyncio.Task[Any]) -> None:
        """Hold a task nobody awaits, so it has an owner and its error is collected."""
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)
        task.add_done_callback(self._collect_error)

    @staticmethod
    def _collect_error(task: asyncio.Task[Any]) -> None:
        """Retrieve the exception of an abandoned task, so it is logged here and not at GC."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.debug("A detached browser task failed (%s)", type(error).__name__)

    @staticmethod
    async def _close_dropped(client: BrowserClient, still_writing: asyncio.Future[None] | None) -> None:
        """End the connection we gave up on, and whatever was still writing to it.

        The close writes a frame, so it must not run while the abandoned send is
        still inside ``send``: that would be the two concurrent writers the lock
        exists to prevent. A send that was merely slow finishes here and the close
        follows it; one that is wedged in the socket is cancelled, because the
        connection is being destroyed and there is no frame stream left to
        protect — and a task nothing ever ends would outlive the process.

        Closing a wedged socket can block as readily as writing to it, so the
        close has the same deadline. Cancelling *that* is safe for the same
        reason: what it would truncate is the goodbye of a connection nobody is
        reading any more.
        """
        if still_writing is not None:
            await asyncio.wait({still_writing}, timeout=_CLIENT_SEND_TIMEOUT_SEC)
            if not still_writing.done():
                still_writing.cancel()
        with suppress(Exception):
            await asyncio.wait_for(
                client.websocket.close(code=1011, reason="Connection stopped draining"),
                _CLIENT_SEND_TIMEOUT_SEC,
            )

    def broadcast_sync(self, message: BrowserOutbound) -> None:
        future = self._schedule(self.broadcast(message))
        if future is not None:
            future.add_done_callback(partial(self._log_broadcast_result, str(message.get("type") or "unknown")))

    def deliver_blocking(
        self,
        message: BrowserOutbound,
        *,
        owner_id: str,
        agent_id: str,
        chat_id: int | str,
        timeout: float = _DELIVERY_TIMEOUT_SEC,
    ) -> DeliveryOutcome:
        """Called by the reader thread, never by its destination event loop."""
        started = threading.Event()
        future = self._schedule(
            self._deliver_started(message, owner_id=owner_id, agent_id=agent_id, chat_id=chat_id, started=started)
        )
        if future is None:
            return DeliveryOutcome.NOT_HANDED_OVER
        try:
            taken = future.result(timeout)
        except FutureTimeoutError:
            future.cancel()
            if not started.is_set():
                log.warning("Browser delivery never ran for msg type=%s", message.get("type"))
                return DeliveryOutcome.NOT_HANDED_OVER
            # It started and overran: the loop itself is blocked, and redelivering
            # into a blocked loop only spends the next message's budget too.
            log.error("Browser delivery overran its budget for msg type=%s", message.get("type"))
            return DeliveryOutcome.FAILED
        except Exception:
            log.error("Browser delivery failed for msg type=%s", message.get("type"), exc_info=True)
            return DeliveryOutcome.FAILED
        return DeliveryOutcome.DELIVERED if taken else DeliveryOutcome.NO_RECIPIENT

    async def _deliver_started(
        self,
        message: BrowserOutbound,
        *,
        owner_id: str,
        agent_id: str,
        chat_id: int | str,
        started: threading.Event,
    ) -> int:
        # First statement, so an expired budget can be told from a loop that
        # accepted the coroutine and never ran it.
        started.set()
        return await self.deliver(message, owner_id=owner_id, agent_id=agent_id, chat_id=chat_id)

    def _schedule(self, coro: Coroutine[Any, Any, _T]) -> Future[_T] | None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            log.debug("Browser event loop stopped before broadcast", exc_info=True)
            coro.close()
            return None

    @staticmethod
    def _log_broadcast_result(message_type: str, future: Future[None]) -> None:
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            log.warning("Browser broadcast failed for msg type=%s (%s)", message_type, type(error).__name__)

    # -- outbound ---------------------------------------------------------

    def attach_gateway(self, gateway: IMessageGateway, *, drive_root: pathlib.Path | None = None) -> None:
        """Open the transport's own subscription and start reading it.

        ``BROADCAST`` because delivery needs the WebSocket this process is
        holding: every instance sees every message, and only the one with a live
        client for the chat delivers it.
        """
        root = pathlib.Path(drive_root) if drive_root is not None else None
        if gateway is self._gateway and root == self._drive_root:
            return
        self._detach_gateway()
        self._gateway = gateway
        self._drive_root = root
        self._reader = gateway.outbox.reader(name=TRANSPORT_NAME, mode=SubscriptionMode.BROADCAST)
        self._reader_stopping.clear()
        self._reader_thread = threading.Thread(
            target=self._read_outbox,
            name="browser-outbox-reader",
            daemon=True,
        )
        self._reader_thread.start()
        # A runtime log line carries no owner, so under several owners there is no
        # connection it correctly belongs to. Switched off rather than routed: a
        # deployment that declared its identities separate should not also have to know
        # which diagnostic channel republishes one owner's output to another.
        if browser_identity.resolver.delivers_unattributed_output:
            set_log_sink(self._broadcast_log)

    def _detach_gateway(self) -> None:
        # Stop and join before releasing the subscription: a message still being
        # delivered when the reader is released would be requeued behind the
        # delivery that then succeeds, and arrive twice.
        self._reader_stopping.set()
        thread = self._reader_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_READER_JOIN_TIMEOUT_SEC)
        if thread is not None and thread.is_alive():
            raise TimeoutError("Browser reader did not stop within the shutdown budget")
        reader = self._reader
        if reader is not None:
            reader.close()
        self._reader = None
        self._reader_thread = None
        self._gateway = None
        set_log_sink(None)

    def _read_outbox(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            run_reader_loop(
                reader,
                self._reader_stopping,
                self.handle_outbound,
                name="Browser",
                poll_timeout_sec=_OUTBOX_POLL_TIMEOUT_SEC,
            )
        finally:
            # Leaving the subscription open would make it a write-only sink:
            # nothing drains it and every publish still fans in.
            if not self._reader_stopping.is_set():
                with suppress(Exception):
                    reader.close()

    def handle_outbound(self, reader: IOutboxReader, delivered: DeliveredMessage) -> None:
        message = delivered.message
        owner_id = message.scope.owner_id
        agent_id = message.scope.agent_id
        chat_id = message.chat_id
        if not self._is_mine(message) or not self.clients_for(owner_id=owner_id, agent_id=agent_id, chat_id=chat_id):
            reader.ack(delivered)
            return
        try:
            frame = self._frame(message)
        except FileTransferPending:
            reader.defer(delivered, delay=_ADMISSION_RETRY_BACKOFF_SEC)
            return
        outcome = (
            DeliveryOutcome.UNRESOLVED
            if frame is None
            else self.deliver_blocking(frame, owner_id=owner_id, agent_id=agent_id, chat_id=chat_id)
        )
        if outcome is DeliveryOutcome.UNRESOLVED:
            # Terminal: a blob that did not arrive within the budget will not
            # arrive faster on attempt two, and every attempt is charged to every
            # owner waiting behind the slot.
            log.error(
                "Dropping a browser message whose payload could not be resolved chat_id=%s kind=%s",
                message.chat_id,
                message.kind.value,
            )
            reader.ack(delivered)
            return
        if outcome is not DeliveryOutcome.NOT_HANDED_OVER:
            reader.ack(delivered)
            return
        attempts = delivered.attempts + 1
        if attempts >= _BROADCAST_ATTEMPT_LIMIT:
            log.error(
                "Dropping a browser message after %d failed broadcasts chat_id=%s kind=%s",
                attempts,
                message.chat_id,
                message.kind.value,
            )
            reader.ack(delivered)
            return
        log.warning("Browser delivery never ran chat_id=%s; retrying", message.chat_id)
        reader.nack(delivered)

    def _is_mine(self, message: OutboundMessage) -> bool:
        """Whether this transport delivers the message, source-wise."""
        return message.source == TRANSPORT_NAME

    @staticmethod
    def _echo_frame(message: OutboundMessage, echo: BrowserEcho) -> ChatOutbound:
        return {
            "type": "chat",
            "role": "user",
            "content": message.text,
            "ts": message.ts,
            "source": message.source,
            "sender_session_id": str(echo.get("sender_session_id") or ""),
            "client_message_id": str(echo.get("client_message_id") or ""),
            "trace_id": message.trace_id,
            "attachments": list(echo.get("attachments") or []),
        }

    @staticmethod
    def _confirmation_frame(message: OutboundMessage, request: dict[str, JsonValue]) -> ConfirmationRequestOutbound:
        return {
            "type": "confirmation_request",
            "confirmation_id": str(request.get("confirmation_id") or ""),
            "task_id": str(request.get("task_id") or ""),
            "tool": str(request.get("tool") or ""),
            "arguments_preview": str(request.get("arguments_preview") or ""),
            "arguments_truncated": bool(request.get("arguments_truncated")),
            "text": message.text,
            "ts": message.ts or utc_now_iso(),
            "source": message.source,
        }

    @staticmethod
    def _lifecycle_payload(data: dict[str, JsonValue]) -> ExtensionLifecycleOutbound:
        return {
            "type": "extension_lifecycle",
            "skill": str(data.get("skill") or ""),
            "action": str(data.get("action") or data.get("type") or ""),
            "status": str(data.get("status") or ""),
            "reason": str(data.get("reason") or ""),
            "data": dict(data),
        }

    def _frame(self, message: OutboundMessage) -> BrowserOutbound | None:
        if message.kind is OutboundKind.ACTION:
            return {"type": "typing", "action": message.action, "source": message.source}
        if message.kind is OutboundKind.TEXT:
            echo = message.transport.get(ECHO_TRANSPORT_KEY)
            if isinstance(echo, dict):
                return self._echo_frame(message, _BROWSER_ECHO_ADAPTER.validate_python(echo))
            confirmation = message.transport.get(CONFIRMATION_TRANSPORT_KEY)
            if isinstance(confirmation, dict):
                return self._confirmation_frame(message, confirmation)
            return {
                "type": "chat",
                "role": "assistant",
                "content": message.text,
                "markdown": message.markdown,
                "is_progress": message.is_progress,
                "ts": message.ts or utc_now_iso(),
                "task_id": message.task_id,
                "source": message.source,
                "transport": dict(message.transport),
                "trace_id": message.trace_id,
            }
        return self._media_frame(message)

    def _media_frame(self, message: OutboundMessage) -> ChatOutbound | PhotoOutbound | None:
        if self._loop is None:
            return None
        with aef_tracing.trace_scope(message.trace_id or aef_tracing.accept_trace_id("", source="browser outbound")):
            future = asyncio.run_coroutine_threadsafe(self._build_media_frame(message), self._loop)
        try:
            return future.result(_PAYLOAD_RESOLVE_TIMEOUT_SEC)
        except FileTransferPending:
            raise
        except FutureTimeoutError:
            future.cancel()
            log.warning("Browser attachment resolution timed out")
            return None
        except (FileError, OSError, SQLAlchemyError):
            log.warning("Browser attachment resolution failed", exc_info=True)
            return None

    async def _build_media_frame(self, message: OutboundMessage) -> ChatOutbound | PhotoOutbound | None:
        """Media travels by reference; the bytes are fetched here, at delivery."""
        task = asyncio.current_task()
        if task is not None:
            self._adopt(task)
        reference = message.attachment
        if reference is None or not reference.attachment_id:
            log.warning("Browser drops a %s with no attachment reference", message.kind.value)
            return None
        async with file_transaction() as conn:
            file = await conn.run_sync(lambda c: FileCatalog(c, message.scope).file(reference.attachment_id))
        if file.status != "ready":
            return None
        metadata = attachment_metadata(file)
        attachment = browser_attachment(metadata)
        if message.kind is OutboundKind.PHOTO:
            image_base64 = await self._attachment_base64(message.scope, metadata)
            if image_base64 is None:
                return None
            return {
                "type": "photo",
                "role": "assistant",
                # `PhotoOutbound` requires it and clients render from it; the URL
                # is the alternative, not the replacement.
                "image_base64": image_base64,
                "mime": reference.mime or attachment.get("mime", ""),
                "caption": message.caption,
                "ts": message.ts or utc_now_iso(),
                "source": message.source,
                "attachments": [attachment],
                "url": attachment.get("url", ""),
            }
        return {
            "type": "chat",
            "role": "assistant",
            "content": message.caption,
            "attachments": [attachment],
            "ts": message.ts or utc_now_iso(),
            "source": message.source,
        }

    async def _attachment_base64(self, owner_scope: OwnerScope, metadata: AttachmentMetadata) -> str | None:
        blob = self.file_blob
        if blob is None:
            return None
        limit = attachment_max_bytes(metadata["mime"])
        if metadata["size"] > limit:
            return None
        encoded: list[str] = []
        remainder, received = b"", 0
        async with (
            get_transfer_limits().async_slot(owner_scope, async_engine_factory.engine, outgoing=True),
            blob.open(owner_scope, "uploads", metadata["storage_key"]) as chunks,
        ):
            async for chunk in chunks:
                received += len(chunk)
                if received > limit:
                    raise FileError("image exceeds limit", FileFailure.TOO_LARGE)
                chunk = remainder + chunk
                whole = len(chunk) - len(chunk) % 3
                encoded.append(
                    await anyio.to_thread.run_sync(lambda data: base64.b64encode(data).decode("ascii"), chunk[:whole])
                )
                remainder = chunk[whole:]
        encoded.append(base64.b64encode(remainder).decode("ascii"))
        return await anyio.to_thread.run_sync(lambda: "".join(encoded))

    def _broadcast_log(self, event: dict[str, JsonValue]) -> None:
        self.broadcast_sync(cast(LogOutbound, event) if event.get("type") == "log" else {"type": "log", "data": event})

    async def start(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        gateway: IMessageGateway | None = None,
        drive_root: pathlib.Path | None = None,
    ) -> None:
        settings = load_settings()
        if min(settings.OUROBOROS_FILE_TRANSFERS, settings.OUROBOROS_FILE_OWNER_TRANSFERS) < 2:
            raise ValueError("browser file transfers require at least two process and owner slots")
        self._loop = loop
        if gateway is not None:
            # Off the loop, symmetrically with `stop`: opening the subscription is a
            # blocking broker call, and `attach_gateway` first joins the reader thread.
            await asyncio.to_thread(self.attach_gateway, gateway, drive_root=drive_root)
        # Extension lifecycle events carry no owner either, and fan out the same way.
        if self._lifecycle_sub_id is None and browser_identity.resolver.delivers_unattributed_output:
            self._lifecycle_sub_id = get_global_event_bus().subscribe(
                "browser-ingress",
                SKILL_LIFECYCLE,
                lambda data: self.broadcast_sync(self._lifecycle_payload(data)),
            )
        if self._heartbeat_task is None or self._heartbeat_task.done():
            interval = (
                self._heartbeat_interval_sec if self._heartbeat_interval_sec is not None else _HEARTBEAT_INTERVAL_SEC
            )
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(interval),
                name="browser-ws-heartbeat",
            )

    async def _heartbeat_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            if self.has_clients():
                await self.broadcast({"type": "heartbeat", "ts": utc_now_iso()})

    async def stop(self) -> None:
        self._reader_stopping.set()
        await self.close_all()
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._lifecycle_sub_id is not None:
            get_global_event_bus().unsubscribe(self._lifecycle_sub_id)
            self._lifecycle_sub_id = None
        # Off the loop: `_detach_gateway` joins the reader thread, and the reader
        # may be waiting on this very loop to send its last frame.
        await asyncio.to_thread(self._detach_gateway)
        # The sockets are closed by now, so an abandoned send is finishing anyway;
        # cancelling is the backstop for one that is not.
        detached = list(self._side_tasks)
        for task in detached:
            task.cancel()
        if detached:
            _, pending = await asyncio.wait(detached, timeout=_READER_JOIN_TIMEOUT_SEC)
            if pending:
                raise TimeoutError("Browser tasks did not stop within the shutdown budget")
        with self._lock:
            self._clients.clear()
        self._loop = None

    def reset_for_tests(self) -> None:
        task = self._heartbeat_task
        if task is not None and not task.done():
            task.cancel()
        self._heartbeat_task = None
        if self._lifecycle_sub_id is not None:
            get_global_event_bus().unsubscribe(self._lifecycle_sub_id)
            self._lifecycle_sub_id = None
        self._detach_gateway()
        with self._lock:
            self._clients.clear()
        self._loop = None


_browser_hub = BrowserWebSocketHub()


def get_browser_ws_hub() -> BrowserWebSocketHub:
    return _browser_hub


def reset_browser_ws_state_for_tests() -> None:
    _browser_hub.reset_for_tests()


def _drive_root_from_websocket(websocket: WebSocket) -> pathlib.Path:
    if hasattr(websocket.app, "state") and hasattr(websocket.app.state, "drive_root"):
        return pathlib.Path(websocket.app.state.drive_root)
    return load_path_settings().data_dir


def _handshake_trace_id(websocket: WebSocket) -> str:
    """The identifier the client put on the handshake, read once when the socket is accepted.

    Read per message instead, the same fixed headers would re-log the same rejection for every
    frame the client sends — one bad header on an ingress that is not authenticated, unbounded
    log volume. A client that sends the header at all is asking for connection scope, so a
    malformed one is repaired to a single minted identifier for the connection; a client that
    sends none gets ``""`` and mints per message.
    """
    headers = getattr(websocket, "headers", {}) or {}
    for key, value in headers.items():
        if str(key).casefold() == aef_tracing.TRACE_HEADER_NAME.casefold():
            return aef_tracing.accept_trace_id(value, source="browser")
    return ""


def _browser_trace_id(message: dict[str, Any], connection_trace_id: str) -> str:
    """The body first, then the connection's handshake identifier: a client can supply it either way.

    The body value goes through the boundary acceptor, so a client sending a malformed id per
    message is logged rather than silently replaced; the handshake value already has.
    """
    requested = str(message.get("trace_id") or "")
    if requested:
        return aef_tracing.accept_trace_id(requested, source="browser")
    return connection_trace_id or aef_tracing.ensure_trace_id()


async def _ensure_background_tasks(websocket: WebSocket) -> None:
    """Attach on the first connection, for a hub the composition root never started."""
    await _browser_hub.start(
        asyncio.get_running_loop(),
        gateway=_gateway_from_websocket(websocket),
        drive_root=_drive_root_from_websocket(websocket),
    )


async def _send_ws_error(
    websocket: WebSocket,
    message: str,
    *,
    code: str = "invalid_request",
    retryable: bool = False,
) -> None:
    detail = str(message or "invalid request")
    payload = json.dumps(
        {
            "type": "error",
            "error": detail,
            "code": code,
            "message": detail,
            "retryable": retryable,
            "ok": False,
        }
    )
    async with connection_send_lock(websocket):
        await websocket.send_text(payload)


def check_operator_command_policy(cmd: str, scope: OwnerScope) -> tuple[bool, str]:
    """Policy gate for WS operator commands via ``policy/safety.py``.

    Only an *unqualified* allowance passes. The verdict is a ``(bool, str)`` pair
    in which three outcomes collapse into ``True``: examined and clean, flagged
    but permitted, and never examined at all because the backend is not
    configured. Only the first is a statement that this command is safe, and only
    the first is silent — so the message is the difference, and the transport
    stops discarding it.

    The alternative was matching on the warning text, which is a fail-open
    sanitizer keyed on wording: it reopens the hole the day the string changes.
    """
    text = str(cmd or "").strip()
    if not text:
        return False, "empty command"
    if runtime_slash_command(text):
        return True, ""
    if text.startswith("/"):
        return True, ""
    allowed, message = check_safety("run_shell", {"cmd": text}, scope=scope)
    if allowed and not str(message or "").strip():
        return True, ""
    return False, message or "command blocked by policy"


def _names_an_unavailable_check(message: str) -> bool:
    """Whether this refusal is "nobody checked" rather than "the check said no".

    Keyed on the suffix the policy module puts on every allowance it gave *without
    examining anything* — an unconfigured backend and an unreachable local runtime
    both — rather than on one of the two wordings. Both refusals are refusals either
    way; this only chooses which code the client is told, so a marker that drifts
    costs a less precise code and never a forwarded command.
    """
    return UNCHECKED_WARNING_SUFFIX in str(message or "")


async def _checked_off_the_loop(cmd: str, scope: OwnerScope) -> tuple[bool, str]:
    """Run the policy check on a thread owned by this call, under our own bound.

    Not on ``asyncio.to_thread``: the loop's default executor is where this
    transport's storage and attachment hops run, so a check stuck for minutes would
    starve every other owner's message. Not with the backend's own timeout either —
    that one is sized for an agent loop a model is waiting on.

    And not on a ``ThreadPoolExecutor``: its workers are registered for an
    interpreter-exit join, so ``shutdown(wait=False)`` does not abandon anything —
    an overrun check would hold process exit for the backend's own five minutes. A
    daemon thread is what actually lets go.

    The slots are what keep abandonment bounded: an abandoned check still occupies
    one until the backend returns, so a stream of commands is refused rather than
    turned into a thread per frame. The transport bounds its payload resolutions the
    same way, for the same reason.
    """
    if not _POLICY_CHECK_SLOTS.acquire(blocking=False):
        raise _TooManyChecksInFlight
    loop = asyncio.get_running_loop()
    verdict: asyncio.Future[tuple[bool, str]] = loop.create_future()
    call = contextvars.copy_context()

    def _settle(outcome: tuple[bool, str] | None, error: BaseException | None) -> None:
        if verdict.done():
            # The wait expired and the caller has moved on; this verdict is discarded.
            return
        if error is not None:
            verdict.set_exception(error)
        else:
            verdict.set_result(outcome)  # type: ignore[arg-type]

    def _run() -> None:
        try:
            outcome = call.run(check_operator_command_policy, cmd, scope)
        except BaseException as error:
            _hand_back(loop, _settle, None, error)
        else:
            _hand_back(loop, _settle, outcome, None)
        finally:
            _POLICY_CHECK_SLOTS.release()

    threading.Thread(target=_run, name="browser-policy", daemon=True).start()
    return await asyncio.wait_for(verdict, _POLICY_CHECK_TIMEOUT_SEC)


def _hand_back(
    loop: asyncio.AbstractEventLoop,
    settle: Callable[[tuple[bool, str] | None, BaseException | None], None],
    outcome: tuple[bool, str] | None,
    error: BaseException | None,
) -> None:
    """Cross back to the loop, or drop the verdict if it is gone."""
    try:
        loop.call_soon_threadsafe(settle, outcome, error)
    except RuntimeError:
        # The loop closed while the check ran: nobody is waiting for this any more.
        log.debug("A policy verdict arrived after its event loop closed")


def _policy_error_text(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return "command blocked by policy"
    first_line = text.splitlines()[0].strip()
    if "SAFETY_VIOLATION" in first_line:
        return first_line
    return first_line[:500]


def _gateway_from_websocket(websocket: WebSocket) -> IMessageGateway | None:
    return getattr(getattr(websocket.app, "state", None), "message_gateway", None)


def _session_id_from_websocket(websocket: WebSocket) -> str:
    """The conversation the message belongs to, supplied by the composition root."""
    return str(getattr(getattr(websocket.app, "state", None), "chat_session_id", "") or "")


async def _send_initializing(websocket: WebSocket) -> None:
    payload = json.dumps(
        {
            "type": "chat",
            "role": "assistant",
            "content": "⚠️ System is still initializing. Please wait a moment and try again.",
            "ts": utc_now_iso(),
        }
    )
    async with connection_send_lock(websocket):
        await websocket.send_text(payload)


async def _handle_inbound_command(
    client: BrowserClient,
    msg: dict[str, Any],
    trace_id: str,
) -> None:
    """An operator command is an ordinary message; only the policy gate is ours.

    A refusal is the transport's own answer and must reach the client on its own
    connection, so it stays synchronous. What the command *is* — and what running
    it means — the supervisor decides.
    """
    cmd = str(msg.get("cmd") or "").strip()
    if not cmd:
        return

    try:
        allowed, policy_message = await _checked_off_the_loop(cmd, client.scope)
    except asyncio.TimeoutError:
        # The bound is the transport's, not the backend's: an operator is a human
        # on a socket, and the backend's is minutes long.
        log.warning("The operator command policy check did not answer within %.0fs", _POLICY_CHECK_TIMEOUT_SEC)
        await _send_ws_error(
            client.websocket,
            "the command could not be checked: the policy check did not answer",
            code="command_check_unavailable",
        )
        return
    except _TooManyChecksInFlight:
        log.warning("Refusing an operator command: every policy-check slot is in use")
        await _send_ws_error(
            client.websocket,
            "the command could not be checked: too many checks are already running",
            code=COMMAND_CHECK_UNAVAILABLE,
        )
        return
    except Exception:
        # A refusal, not a closed connection: the handler's generic guard would
        # end the socket over a check that failed.
        log.warning("The operator command policy check failed", exc_info=True)
        await _send_ws_error(
            client.websocket,
            "the command could not be checked: the policy check failed",
            code=COMMAND_CHECK_UNAVAILABLE,
        )
        return
    if not allowed:
        unchecked = _names_an_unavailable_check(policy_message)
        if unchecked:
            log.warning("Refusing an operator command that could not be checked: %s", policy_message)
        await _send_ws_error(
            client.websocket,
            _policy_error_text(policy_message),
            code=COMMAND_CHECK_UNAVAILABLE if unchecked else COMMAND_NOT_ALLOWED,
        )
        return

    await _handle_inbound_chat(
        client,
        {
            "content": cmd,
            "sender_session_id": str(msg.get("sender_session_id") or ""),
            "client_message_id": str(msg.get("client_message_id") or uuid.uuid4().hex),
        },
        # A command is an ordinary message on the way in, and carries the identifier its own
        # frame supplied. Passed rather than left in the synthetic payload above: that payload
        # is a rewrite of the client's frame, and a field dropped from a rewrite mints silently.
        trace_id,
    )


class _EnvelopeBuilder(IEnvelopeBuilder):
    """The browser's own half of the journal sweep: row to envelope."""

    def build(self, record: ConversationMessage) -> InboundEnvelope:
        metadata = record.metadata or {}
        attachments, attachment_ids = stored_attachments(record)
        return InboundEnvelope(
            # The row records the conversation it was written under; replaying it into
            # the constant chat would move one owner's message into another's.
            scope=OwnerScope(
                owner_id=record.owner_id,
                agent_id=record.agent_id,
                surface_id=SurfaceId(TRANSPORT_NAME),
                chat_id=str(record.chat_id or BROWSER_CHAT_ID),
            ),
            user_id=record.chat_id or BROWSER_CHAT_ID,
            text=record.content,
            sender_session_id=str(metadata.get("sender_session_id") or ""),
            client_message_id=str(record.source_message_id or ""),
            attachment_ids=attachment_ids,
            attachments=attachments,
            detached=bool(metadata.get("detached")),
            trace_id=str(metadata.get("trace_id") or ""),
            transport={"source_message_id": str(record.source_message_id or "")},
        )


_ENVELOPE_BUILDER = _EnvelopeBuilder()


def resubmit_unprocessed(
    gateway: IMessageGateway,
    drive_root: pathlib.Path,
    *,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> int:
    """Submit again every message persisted but never handed over."""
    return replay_unprocessed(
        gateway,
        drive_root,
        source=TRANSPORT_NAME,
        build=_ENVELOPE_BUILDER,
        limit=limit,
    )


async def _handle_inbound_chat(
    client: BrowserClient,
    msg: dict[str, Any],
    trace_id: str,
) -> None:
    """Persist and hand off a frame under its connection identity."""
    websocket = client.websocket
    scope = client.scope
    chat_id = client.chat_id
    content = str(msg.get("content") or "").strip()
    detached = msg.get("detached", False)
    if not isinstance(detached, bool):
        await _send_ws_error(websocket, "detached must be a boolean", code="invalid_detached")
        return
    raw_attachment_ids = msg.get("attachment_ids")
    attachment_ids: list[str] = []
    if "attachment_ids" in msg:
        if not isinstance(raw_attachment_ids, list) or any(
            not isinstance(value, str) or not value.strip() for value in raw_attachment_ids
        ):
            await _send_ws_error(
                websocket,
                "attachment_ids must be an array of non-empty strings",
                code="invalid_attachment_ids",
            )
            return
        attachment_ids = [value.strip() for value in raw_attachment_ids]
        if len(set(attachment_ids)) != len(attachment_ids):
            await _send_ws_error(
                websocket,
                "attachment_ids must contain unique values",
                code="invalid_attachment_ids",
            )
            return
    if not content and not attachment_ids:
        return
    if len(attachment_ids) > 10:
        await _send_ws_error(websocket, "too many attachments", code="too_many_attachments")
        return
    # The same predicate the supervisor classifies with: a command never becomes a task,
    # so an attachment bound to one would have no run to reach.
    if attachment_ids and answered_slash_command(content):
        await _send_ws_error(
            websocket,
            "commands cannot include attachments",
            code="command_with_attachments",
        )
        return

    sender_session_id = str(msg.get("sender_session_id") or "")
    client_message_id = str(msg.get("client_message_id") or uuid.uuid4().hex)
    ts = utc_now_iso()
    gateway = _gateway_from_websocket(websocket)
    if gateway is None:
        await _send_initializing(websocket)
        return

    session_id = _session_id_from_websocket(websocket)

    message = build_chat_message(
        scope,
        actor_type="owner",
        chat_id=chat_id,
        user_id=chat_id,
        text=content,
        source=TRANSPORT_NAME,
        session_id=session_id,
        sender_session_id=sender_session_id,
        client_message_id=client_message_id,
        ts=ts,
        extra_metadata={
            "inbound_processed": False,
            "detached": detached,
            "trace_id": trace_id,
            "attachments": [{"id": file_id} for file_id in attachment_ids],
        },
    )

    try:
        messages: InboundMessageStore = websocket.app.state.inbound_messages
        persisted = await messages.persist(scope, message)
    except FileError as error:
        await _send_ws_error(
            websocket, str(error), code=error.code if isinstance(error, InboundRefused) else "attachment_bind_failed"
        )
        return
    except (SQLAlchemyError, RuntimeError, TimeoutError):
        log.warning("Failed to persist browser chat message", exc_info=True)
        await _send_ws_error(websocket, "failed to persist message", code="message_persistence_failed", retryable=True)
        return
    internal_attachments, _ = stored_attachments(persisted)
    stored_trace = str(persisted.metadata.get("trace_id") or trace_id)
    echoed = True
    try:
        await gateway.apublish(
            OutboundMessage(
                scope=scope,
                source=TRANSPORT_NAME,
                text=content,
                ts=persisted.created_at,
                trace_id=stored_trace,
                transport={
                    ECHO_TRANSPORT_KEY: {
                        "sender_session_id": sender_session_id,
                        "client_message_id": client_message_id,
                        "attachments": [browser_attachment(value) for value in internal_attachments],
                    }
                },
            )
        )
    except (RedisError, RuntimeError, OSError):
        log.warning("Failed to echo browser message", exc_info=True)
        echoed = False
    if not persisted.metadata.get("inbound_processed"):
        try:
            await gateway.asubmit(_ENVELOPE_BUILDER.build(persisted))
        except (RedisError, RuntimeError, OSError):
            log.warning("Failed to submit browser message", exc_info=True)
            await _send_ws_error(websocket, "failed to submit message", code="message_submit_failed", retryable=True)
            return
        try:
            async with file_transaction() as conn:
                await amark_message_processed(conn, persisted.message_id)
        except (SQLAlchemyError, TimeoutError):
            log.warning("Failed to acknowledge browser journal message", exc_info=True)
    if not echoed:
        await _send_ws_error(websocket, "failed to echo message", code="message_echo_failed", retryable=True)


async def ws_endpoint(websocket: WebSocket) -> None:
    """Accept browser WebSocket connections and dispatch inbound chat frames.

    Identity is settled before the accept, so a refusal is a failed handshake rather than
    an accepted socket that immediately closes — which is what a proxy and a test both
    need to see, and what keeps a refused caller from ever having a connection to send on.
    The hub is attached before the accept too, and after every refusal: a socket accepted
    while it is still attaching belongs to an owner who can publish into a subscription
    that does not exist yet.
    """
    headers = getattr(websocket, "headers", {})
    try:
        scope = await browser_identity.resolver.scope_for(headers)
    except IdentityRefused as refusal:
        log.info("Browser WebSocket handshake refused: %s", refusal.reason)
        await websocket.close(code=_IDENTITY_REFUSED_CLOSE_CODE, reason=refusal.reason)
        return
    collector = security_context_collector.collector
    store = security_context.store
    try:
        await store.save(scope.security_token, collector.collect(headers.items()))
    except Exception as exc:
        log.warning("Browser WebSocket handshake refused: the security context was not saved: %s", type(exc).__name__)
        await websocket.close(code=_SECURITY_CONTEXT_UNSAVED_CLOSE_CODE, reason=_SECURITY_CONTEXT_UNSAVED_REASON)
        return
    await _ensure_background_tasks(websocket)
    await websocket.accept()
    connection_trace_id = _handshake_trace_id(websocket)
    client = BrowserClient(websocket=websocket, scope=scope)
    total = _browser_hub.add_client(client)
    log.info("Browser WebSocket client connected (total: %d)", total)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await _send_ws_error(websocket, "invalid json", code="invalid_json")
                continue
            if not isinstance(msg, dict):
                await _send_ws_error(
                    websocket,
                    "invalid message payload",
                    code="invalid_message_payload",
                )
                continue

            msg_type = str(msg.get("type", "") or "")
            if msg_type in {"chat", "command"}:
                # One scope per frame, opened here rather than inside the handlers: both persist
                # a row and bind attachments to object storage *in band*, so without a scope
                # those writes reach the wire with no header and the handler's own warnings lose
                # the field. Per frame and not per connection — a socket carries many independent
                # operations, and one scope at accept would attribute them all to whoever opened it.
                trace_id = _browser_trace_id(msg, connection_trace_id)
                with aef_tracing.trace_scope(trace_id):
                    content = str(msg.get("content" if msg_type == "chat" else "cmd") or "").strip()
                    length_refusal = oversized_message_refusal(
                        content, load_settings().OUROBOROS_MAX_INBOUND_MESSAGE_CHARS
                    )
                    if length_refusal:
                        await _send_ws_error(websocket, length_refusal, code="message_too_long")
                        continue
                    if msg_type == "chat":
                        await _handle_inbound_chat(client, msg, trace_id)
                    else:
                        await _handle_inbound_command(client, msg, trace_id)
                continue
            if msg_type:
                await _send_ws_error(
                    websocket,
                    f"unknown message type: {msg_type}",
                    code="unknown_message_type",
                )
                continue
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("Browser WebSocket error: %s", exc)
    finally:
        total = _browser_hub.remove_client(client)
        log.info("Browser WebSocket client disconnected (total: %d)", total)


__all__ = [
    "BROWSER_CHAT_ID",
    "CHAT_HEADER_NAME",
    "OWNER_HEADER_NAME",
    "WS_MESSAGE_TYPES",
    "BrowserClient",
    "BrowserWebSocketHub",
    "IdentityRefused",
    "check_operator_command_policy",
    "get_browser_ws_hub",
    "reset_browser_ws_state_for_tests",
    "trusted_chat_id",
    "trusted_owner_id",
    "ws_endpoint",
]
