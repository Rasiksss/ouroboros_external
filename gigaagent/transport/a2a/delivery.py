"""Routing outbox messages back to the A2A request that is waiting for them.

A2A answers a connection this process is holding, so it reads the outbox on a
``BROADCAST`` subscription: every instance sees every message, and the one
holding the request delivers it while the others acknowledge and drop it. That is
also why the subscription starts at the present — a message nobody is waiting for
belongs to a request that died with its process.

The reader runs on a thread because the seam is synchronous; results cross into
the asyncio loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress

from new_core.identity.owner_scope import OwnerScope
from transport.interfaces import (
    DeliveredMessage,
    IOutbox,
    IOutboxReader,
    OutboundKind,
    OutboundMessage,
    SubscriptionMode,
)
from transport.reader_loop import READER_JOIN_TIMEOUT_MARGIN_SEC, run_reader_loop

log = logging.getLogger("a2a-server")

TRANSPORT_NAME = "a2a"
_POLL_TIMEOUT_SEC = 0.5
#: Long enough for the reader to finish the poll it is in plus one delivery.
_READER_JOIN_TIMEOUT_SEC = _POLL_TIMEOUT_SEC + READER_JOIN_TIMEOUT_MARGIN_SEC


class PendingRequest:
    """One connection waiting for its delivery's answer."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._text = ""

    def complete(self, text: str) -> None:
        """Complete on the registry's event loop."""
        self._text = text
        self._event.set()

    async def wait(self, timeout_sec: float) -> str:
        await asyncio.wait_for(self._event.wait(), timeout=timeout_sec)
        return self._text or "(no response)"


class A2AResponseRouter:
    """The outbox reader with an event-loop-owned registry of requests per delivery."""

    def __init__(self, outbox: IOutbox, *, name: str = TRANSPORT_NAME) -> None:
        self._outbox = outbox
        self._name = name
        self._pending: dict[tuple[OwnerScope, str], list[PendingRequest]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: IOutboxReader | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._reader = self._outbox.reader(name=self._name, mode=SubscriptionMode.BROADCAST)
        self._thread = threading.Thread(target=self._run, name="a2a-outbox-reader", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """Tell the reader to finish, without waiting for it.

        For a caller on the event loop: the join belongs off it, and the serving
        task does it as it unwinds.
        """
        self._stopping.set()

    def stop(self) -> None:
        # Join before releasing the subscription, so no delivery is in flight
        # when the reader is released.
        self._stopping.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_READER_JOIN_TIMEOUT_SEC)
        reader = self._reader
        if reader is not None:
            reader.close()
        self._thread = None
        self._reader = None

    # -- registry ----------------------------------------------------------

    def register(self, scope: OwnerScope, correlation_id: str, loop: asyncio.AbstractEventLoop) -> PendingRequest:
        if self._loop is not None and self._loop is not loop and not self._loop.is_closed():
            raise RuntimeError("A2A pending requests must belong to one event loop")
        self._loop = loop
        pending = PendingRequest()
        self._pending.setdefault((scope, correlation_id), []).append(pending)
        return pending

    def release(self, scope: OwnerScope, correlation_id: str, pending: PendingRequest) -> None:
        """Remove only this connection; retries may still be waiting for the same result."""
        key = (scope, correlation_id)
        requests = self._pending.get(key, [])
        if pending in requests:
            requests.remove(pending)
        if not requests:
            self._pending.pop(key, None)

    # -- reading -----------------------------------------------------------

    def _run(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            run_reader_loop(
                reader,
                self._stopping,
                self.handle,
                name="A2A",
                poll_timeout_sec=_POLL_TIMEOUT_SEC,
            )
        finally:
            # Only when the loop gave up on its own: leaving the subscription
            # open would make it a write-only sink every publish still fans into.
            # A requested stop leaves the close to `stop`, which joins first.
            if not self._stopping.is_set():
                with suppress(Exception):
                    reader.close()

    def handle(self, reader: IOutboxReader, delivered: DeliveredMessage) -> None:
        """Deliver a final answer to its waiting request; acknowledge every message."""
        message = delivered.message
        if message.source != TRANSPORT_NAME or message.is_progress:
            reader.ack(delivered)
            return
        if message.kind is not OutboundKind.TEXT:
            # The channel is a JSON-RPC response; it cannot carry media.
            log.debug("A2A drops %s for chat %s", message.kind.value, message.chat_id)
            reader.ack(delivered)
            return
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._deliver, message)
            except RuntimeError:
                log.debug("A2A request loop closed before its answer arrived", exc_info=True)
        else:
            log.info("A2A drops an answer with no pending request")
        reader.ack(delivered)

    def _deliver(self, message: OutboundMessage) -> None:
        requests = self._pending.pop((message.scope, message.correlation_id), [])
        if not requests:
            log.info("A2A drops an answer for delivery %s with no pending request", message.correlation_id)
        for pending in requests:
            pending.complete(message.text)


__all__ = [
    "TRANSPORT_NAME",
    "A2AResponseRouter",
    "PendingRequest",
]
