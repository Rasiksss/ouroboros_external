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

from supervisor.messaging.base import InboundMessage

SENT_MESSAGE_ID_MEMORY = 256


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
    _lookup_slots: threading.BoundedSemaphore
    _sent_message_ids: deque[str]
    _foreign_message_ids: deque[str]
    _reply_lookup_warned: bool

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
