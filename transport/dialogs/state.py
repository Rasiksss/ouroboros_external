"""Shared state and peer-routing contract for the Dialogs mixins.

``DialogsInbound`` and ``DialogsOutbound`` are mixins: they read provider state
and call provider helpers that only ``DialogsProvider`` defines. Without a
common base each mixin had to be checked against nothing at all, so the shared
names were inferred from whatever the mixin body happened to do with them --
which is why ``_updates_thread`` came out as ``Thread`` in one file and
``Thread | None`` in the one that assigns it.

This base declares that shared surface in one place. The attributes are bare
annotations, so nothing is created at runtime and ``DialogsProvider.__init__``
stays the only writer. The methods raise: ``DialogsProvider`` precedes this
class in every MRO that matters, so reaching one of these bodies means a mixin
was used without the provider, and failing loudly beats a silent no-op.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any, Protocol

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.owner_scope import configured_agent_id, configured_owner_id
from ouroboros.observability import aef_tracing
from transport.dialogs.messages import TRANSPORT_NAME, InboundMessage

SENT_MESSAGE_ID_MEMORY = 256
#: The two header names this agent puts on the wire, spelled for the carrier that takes them
#: here: gRPC rejects a metadata key that is not lowercase.
_TRACE_METADATA_KEY = aef_tracing.TRACE_HEADER_NAME.lower()
_AGENT_METADATA_KEY = aef_tracing.AGENT_HEADER_NAME.lower()


class DialogsPeer(Protocol):
    """A routable SDK peer (``update.peer``, ``group.peer``, ``Peer(id, type)``).

    Peers are handed back to the SDK unchanged (``send_message_sync``,
    ``load_message_history_sync``, ...), so this is a structural contract over
    whatever object the SDK produced — never a wrapper.
    """

    id: Any
    type: Any


class DialogsSenderPeer(Protocol):
    """A message sender. May arrive as a user-outpeer: ``uid`` instead of
    ``id`` and no ``type``, which is why accessors read it via ``getattr``."""

    id: Any


def outbound_metadata() -> dict[str, Any]:
    """The ``metadata=`` kwarg the SDK forwards as gRPC metadata, or nothing.

    Carries the pair every outbound request of this agent carries — which operation
    (``x-trace-id``) and which agent (``x-agent-id``) — because the environment trace reads both
    off the request itself, not off this process.

    Both directions of the conversation use it: what this agent sends and what it asks the
    server for — a history import, a sender's name, who sent the message a user replied to —
    are all requests this process makes, and a read that fails is exactly the kind of thing an
    operator reconstructs from a trace.

    Read per call and never merged into the bot, for the reason every other long-lived client
    here computes it per request: one bot serves every operation, so a value fixed at
    construction would attribute every later call to the first one. A key whose value is unknown
    is left out rather than sent empty — an absent header reads as "nobody said", an empty one
    passes validation and means nothing.
    """
    metadata = {
        key: value
        for key, value in (
            (_TRACE_METADATA_KEY, aef_tracing.current_trace_id()),
            (_AGENT_METADATA_KEY, aef_tracing.current_agent_id()),
        )
        if value
    }
    return {"metadata": metadata} if metadata else {}


def connection_metadata() -> dict[str, str]:
    """Bot-level gRPC metadata: the floor under every SDK call this transport does not stamp.

    The SDK sends these on any RPC that carries no per-call ``metadata`` of its own — the token
    auth and device registration it performs while connecting, and anything the library does on
    its own schedule. Those calls exist before any operation does, so without a floor they are
    the only traffic this process makes that names neither the agent nor a chain.

    The operation identifier is **minted for the connection and never inherited**: it names this
    bot's own traffic rather than whichever task happened to be running when the bot was built,
    which for a client that outlives every operation is the frozen-header defect. Per-call
    metadata overrides it key by key — the SDK prefers what the call passes — so a send made
    inside an operation still carries that operation's identifier, not this one.
    """
    metadata = {_TRACE_METADATA_KEY: aef_tracing.accept_trace_id("", source=f"{TRANSPORT_NAME} connection")}
    agent_id = aef_tracing.current_agent_id()
    if agent_id:
        metadata[_AGENT_METADATA_KEY] = agent_id
    return metadata


def outbound_http_headers() -> dict[str, Any]:
    """:func:`outbound_metadata` for the SDK's other channel.

    File download and upload are plain HTTP rather than gRPC, and the SDK keeps the two apart:
    ``metadata`` reaches a stub method, ``headers`` reaches the file transfer. Same identifier,
    same rule about when it is absent — only the carrier and the spelling of the name differ,
    and an HTTP header is the canonical ``X-Trace-Id`` rather than the lowercased key gRPC
    requires.
    """
    headers = aef_tracing.with_agent_header(aef_tracing.with_trace_header({}))
    return {"headers": headers} if headers else {}


def sdk_mid(value: Any) -> Any:
    for attr in ("message_id", "mid"):
        candidate = getattr(value, attr, None)
        if candidate is not None:
            return candidate
    if getattr(value, "msb", None) is not None and getattr(value, "lsb", None) is not None:
        return value
    return None


def serialize_mid(value: Any) -> str:
    mid = sdk_mid(value)
    if mid is None:
        return ""
    msb = getattr(mid, "msb", None)
    lsb = getattr(mid, "lsb", None)
    if msb is not None and lsb is not None:
        return f"{int(msb)}:{int(lsb)}"
    return str(mid)


class DialogsState:
    """State and helpers the Dialogs mixins expect their provider to supply."""

    settings: Any

    # Connection and peer routing.
    _bot: Any
    _bot_lock: threading.RLock
    _group_peer: DialogsPeer | None
    _out_peer_by_chat_id: dict[str, DialogsPeer]

    # Inbound queues and update-loop lifecycle.
    _incoming: queue.Queue[InboundMessage]
    _incoming_lock: threading.Lock
    _cursor: int
    _handlers_registered: bool
    _updates_thread: threading.Thread | None
    _updates_stop_event: threading.Event
    _last_updates_error: str
    _updates_trace_metadata: bool

    # Attachment download workers and dedup bookkeeping.
    _seen_file_updates: set[str]
    _seen_file_update_order: deque[str]
    _history_recovery_lock: threading.Lock
    _history_recovery_state: dict[str, str]  # peer_key -> "recovering" | "recovered"
    _history_recovery_failed_at: dict[str, float]
    _sender_labels: dict[str, str]
    _sender_label_retry_at: dict[str, float]
    _file_jobs: queue.Queue[Any]
    _file_workers: list[threading.Thread]
    _file_workers_lock: threading.Lock
    _download_slots: threading.BoundedSemaphore

    # Outbound pacing and progress-message tracking.
    _progress_messages: dict[str, Any]
    _progress_fallbacks: set[str]
    _outbound_slots: threading.BoundedSemaphore
    _outbound_workers: set[threading.Thread]
    _lookup_slots: threading.BoundedSemaphore
    _sent_message_ids: deque[str]
    _foreign_message_ids: deque[str]
    _reply_lookup_warned: bool
    _media_trace_headers: bool

    def send_text(
        self,
        chat_id: Any,
        text: str,
        fmt: str = "",
        *,
        is_progress: bool = False,
        task_id: str = "",
        reply_to: list[Any] | None = None,
        target_uid: Any = None,
    ) -> tuple[bool, str]:
        raise NotImplementedError

    def _call_with_timeout(self, callback: Any, *args: Any, operation: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _ensure_bot(self) -> Any:
        raise NotImplementedError

    def _register_handlers(self) -> None:
        raise NotImplementedError

    def _startup_config_summary(self) -> str:
        raise NotImplementedError

    def _extract_text(self, update: Any) -> str:
        raise NotImplementedError

    def _group_id(self) -> int | None:
        raise NotImplementedError

    def _peer_key(self, peer: DialogsPeer | None) -> str:
        raise NotImplementedError

    def _sender_key(self, peer: DialogsSenderPeer | None) -> str:
        raise NotImplementedError

    def _resolve_out_peer(self, chat_id: Any) -> DialogsPeer | None:
        raise NotImplementedError

    def _remember_out_peer(self, chat_id: str, peer: DialogsPeer) -> None:
        raise NotImplementedError

    def _remember_group_peer(self, peer: DialogsPeer) -> None:
        raise NotImplementedError

    def _group_filter_enabled(self) -> bool:
        raise NotImplementedError

    def _user_filter_enabled(self) -> bool:
        raise NotImplementedError

    def _is_configured_group_peer(self, peer: DialogsPeer | None) -> bool:
        raise NotImplementedError

    def _is_group_peer(self, peer: DialogsPeer | None) -> bool:
        raise NotImplementedError

    def _is_configured_user_sender(self, sender_peer: DialogsSenderPeer | None) -> bool:
        raise NotImplementedError

    def _is_self_sender(self, sender_peer: DialogsSenderPeer | None) -> bool:
        raise NotImplementedError

    def _self_sender_key(self) -> str:
        raise NotImplementedError


def dialogs_scope(chat_id: str) -> OwnerScope:
    return OwnerScope(
        owner_id=configured_owner_id(), agent_id=configured_agent_id(), surface_id=SurfaceId.DIALOGS, chat_id=chat_id
    )
