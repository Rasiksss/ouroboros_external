"""Queue-based bridge between ingress transports and the supervisor."""

from __future__ import annotations

import base64
import logging
import pathlib
import queue
import re
import threading
import uuid
from collections.abc import Callable
from typing import Any

from ouroboros.contracts.chat_id_policy import is_internal_chat_id
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_bus import CHAT_OUTBOUND, CHAT_PHOTO, CHAT_TYPING, publish_event
from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import (
    delete_attachment_metadata,
    public_attachment,
    store_attachment_bytes,
)
from ouroboros.storage.chat_store import append_chat_message, clear_owner_chat_history, conversation_id_for_chat
from ouroboros.storage.runtime_models import ActorType, is_dialogs_source
from ouroboros.storage.settings.schema import SettingsLike
from ouroboros.storage.settings.store import load_settings
from ouroboros.utils import utc_now_iso
from supervisor.state import current_session_id, load_state

log = logging.getLogger(__name__)
CLEAN_HISTORY_RESPONSE = "История очищена"
_outbound_sender: Callable[..., Any] | None = None


def _coerce_chat_id(value: Any, default: Any = 1) -> Any:
    if value in (None, ""):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return str(value)


def _is_numeric_chat_id(value: Any) -> bool:
    try:
        int(str(value).strip())
        return True
    except (TypeError, ValueError):
        return False


def _transport_conversation_id(source: str, chat_id: Any) -> str:
    """Dialogs delivers by chat, so its conversation is the chat itself.

    Other routes have no transport-level conversation: the store falls back to
    the session scope. Keeping this here keeps the store transport-neutral.
    """
    return conversation_id_for_chat(source="dialogs", chat_id=chat_id) if is_dialogs_source(source) else ""


def clear_chat_history(data_dir: pathlib.Path, *, preserve_message_id: str = "") -> None:
    clear_owner_chat_history(
        pathlib.Path(data_dir),
        preserve_message_id=preserve_message_id,
    )


def _runtime_bridge() -> LocalChatBridge:
    from supervisor.runtime_context import require_runtime

    bridge = require_runtime().bridge
    if bridge is None:
        raise RuntimeError("Supervisor bridge is not initialized")
    return bridge


def _try_drive_root() -> pathlib.Path | None:
    from supervisor.runtime_context import try_get_runtime

    runtime = try_get_runtime()
    return pathlib.Path(runtime.drive_root) if runtime is not None else None


def _persist_outbound_attachment(
    *,
    chat_id: Any,
    data: bytes,
    filename: str,
    mime: str,
    caption: str,
    source: str,
    transport: dict[str, Any] | None,
) -> dict[str, Any] | None:
    drive_root = _try_drive_root()
    if drive_root is None:
        return None
    attachment_id = uuid.uuid4().hex
    message_id = uuid.uuid4().hex
    try:
        provider = create_storage_provider(data_dir=drive_root)
        metadata = store_attachment_bytes(
            provider,
            attachment_id=attachment_id,
            filename=filename,
            mime=mime,
            data=data,
            source=source,
            status="ready",
            message_id=message_id,
        )
    except Exception:
        log.warning("Failed to store outbound attachment", exc_info=True)
        return None
    try:
        state = load_state()
        persisted = append_chat_message(
            drive_root,
            actor_type="agent",
            chat_id=chat_id,
            user_id=state.owner_id if state.owner_id not in (None, "") else chat_id,
            text=caption,
            session_id=str(state.session_id or ""),
            source=source,
            client_message_id=message_id,
            message_type="attachment",
            conversation_id=_transport_conversation_id(source, chat_id),
            transport=transport,
            extra_metadata={"attachments": [metadata]},
        )
        if persisted is None:
            raise RuntimeError("outbound attachment message insert conflict")
        return dict(public_attachment(metadata))
    except Exception:
        log.warning("Failed to persist outbound attachment", exc_info=True)
        storage_key = str(metadata.get("storage_key") or "")
        try:
            if storage_key:
                provider.delete(SCOPE_UPLOADS, storage_key)
            delete_attachment_metadata(provider, attachment_id)
        except Exception:
            log.warning("Failed to roll back outbound attachment", exc_info=True)
        return None


def init(
    drive_root,
    total_budget_limit: float,
    budget_report_every: int,
    chat_bridge: LocalChatBridge,
) -> None:
    """Deprecated: use build_supervisor_runtime / bootstrap_supervisor_for_tests."""
    from supervisor.bootstrap import build_supervisor_runtime
    from supervisor.runtime_context import set_runtime, try_get_runtime

    runtime = try_get_runtime()
    if runtime is None:
        runtime = build_supervisor_runtime(
            settings={
                "OUROBOROS_SOFT_TIMEOUT_SEC": 10,
                "OUROBOROS_HARD_TIMEOUT_SEC": 20,
            },
            drive_root=pathlib.Path(drive_root),
            repo_dir=pathlib.Path(drive_root),
            bridge=chat_bridge,
        )
        set_runtime(runtime)
        return
    del total_budget_limit, budget_report_every
    runtime.bridge = chat_bridge


def get_bridge() -> LocalChatBridge:
    return _runtime_bridge()


def try_get_bridge() -> LocalChatBridge | None:
    from supervisor.runtime_context import try_get_bridge as _try_get_bridge

    bridge = _try_get_bridge()
    return bridge if bridge is not None else None


def __getattr__(name: str) -> Any:
    if name == "DATA_DIR":
        return _try_drive_root()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class LocalChatBridge:
    """Local Queue-backed message bus."""

    def __init__(self, settings: dict[str, Any] | None = None):
        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._outbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._log_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        self._update_counter = 0
        self._browser_sink: Callable[[dict[str, Any]], None] | None = None
        # A2A response subscriptions: {subscription_id: (chat_id, callback)}
        self._response_subs: dict[str, tuple] = {}
        self._response_subs_lock = threading.Lock()
        self._chat_transports: dict[Any, dict[str, Any]] = {}
        if settings:
            self.configure_from_settings(settings)

    def broadcast(self, payload: dict[str, Any]) -> None:
        """Broadcast to browser WebSocket clients when ingress bridge is attached."""
        chat_id = payload.get("chat_id")
        if is_internal_chat_id(chat_id):
            return
        if self._browser_sink:
            self._browser_sink(payload)

    def set_browser_sink(
        self,
        sink: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """Attach or detach the browser event sink."""
        self._browser_sink = sink

    def get_updates(self, offset: int, timeout: int = 10) -> list[dict[str, Any]]:
        """Block on inbox and return supervisor-style updates."""
        try:
            raw_msg = self._inbox.get(timeout=timeout)
            if isinstance(raw_msg, str):
                msg = {
                    "chat_id": 1,
                    "user_id": 1,
                    "text": raw_msg,
                    "source": "ingress",
                    "sender_label": "",
                }
            else:
                msg = dict(raw_msg or {})

            chat_id_value = _coerce_chat_id(msg.get("chat_id"), 1)
            user_id_value = _coerce_chat_id(msg.get("user_id"), 1)
            message: dict[str, Any] = {
                "chat": {"id": chat_id_value},
                "from": {"id": user_id_value},
                "text": str(msg.get("text") or ""),
                "source": str(msg.get("source") or "ingress"),
            }
            if isinstance(msg.get("transport"), dict) and msg.get("transport") and chat_id_value != 1:
                self._chat_transports[chat_id_value] = dict(msg.get("transport") or {})
            else:
                self._chat_transports.pop(chat_id_value, None)
            for key in (
                "sender_label",
                "sender_session_id",
                "client_message_id",
                "transport",
                "image_base64",
                "image_mime",
                "image_caption",
                "suppress_chat_log",
                "task_constraint",
            ):
                value = msg.get(key)
                if value not in (None, "", 0):
                    message[key] = value

            self._update_counter = max(offset, self._update_counter + 1)
            return [
                {
                    "update_id": self._update_counter,
                    "message": message,
                }
            ]
        except queue.Empty:
            return []

    def configure_from_settings(self, settings: SettingsLike) -> None:
        """Compatibility no-op; chat bridges are skills now."""
        return None

    def _restart_telegram_polling(self) -> None:
        return None

    def subscribe_response(self, chat_id: int, callback) -> str:
        """Subscribe to responses for a chat_id."""
        sub_id = uuid.uuid4().hex
        with self._response_subs_lock:
            self._response_subs[sub_id] = (chat_id, callback)
        return sub_id

    def unsubscribe_response(self, subscription_id: str) -> None:
        """Remove a response subscription."""
        with self._response_subs_lock:
            self._response_subs.pop(subscription_id, None)

    def shutdown(self) -> None:
        return None

    def enqueue_local_message(
        self,
        text: str,
        *,
        chat_id: Any = 1,
        user_id: Any = 1,
        source: str = "ingress",
        sender_label: str = "",
        sender_session_id: str = "",
        client_message_id: str = "",
        transport: dict[str, Any] | None = None,
        image_base64: str = "",
        image_mime: str = "",
        image_caption: str = "",
        suppress_chat_log: bool = False,
        task_constraint: dict[str, Any] | None = None,
        trace_id: str = "",
    ) -> None:
        clean_text = str(text or "").strip()
        caption_text = str(image_caption or "").strip()
        image_b64 = str(image_base64 or "").strip()
        if not clean_text and caption_text:
            clean_text = caption_text
        if not clean_text and not image_b64:
            return
        self._inbox.put(
            {
                "chat_id": _coerce_chat_id(chat_id, 1),
                "user_id": _coerce_chat_id(user_id, 1),
                "text": clean_text,
                "source": str(source or "ingress"),
                "sender_label": str(sender_label or ""),
                "sender_session_id": str(sender_session_id or ""),
                "client_message_id": str(client_message_id or ""),
                "transport": dict(transport or {}),
                "image_base64": image_b64,
                "image_mime": str(image_mime or ""),
                "image_caption": caption_text,
                "suppress_chat_log": bool(suppress_chat_log),
                "task_constraint": dict(task_constraint or {}),
                "trace_id": str(trace_id or ""),
            }
        )

    def send_message(
        self,
        chat_id: Any,
        text: str,
        parse_mode: str = "",
        ts: str | None = None,
        is_progress: bool = False,
        task_id: str = "",
        source: str = "",
        trace_id: str = "",
        transport: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Send text to A2A subscribers and observability event stream."""
        clean_text = _strip_markdown(text) if not parse_mode else text
        message_ts = ts or utc_now_iso()
        normalized_chat_id = _coerce_chat_id(chat_id, 0)
        outbound_transport = dict(transport or self._chat_transports.get(normalized_chat_id, {}) or {})
        msg: dict[str, Any] = {
            "type": "text",
            "content": clean_text,
            "markdown": bool(parse_mode),
            "is_progress": bool(is_progress),
            "ts": message_ts,
            "task_id": str(task_id or ""),
            "source": source,
            "trace_id": str(trace_id or aef_tracing.current_trace_id()),
        }
        self._outbox.put(msg)
        with self._response_subs_lock:
            subs = [(sid, cb) for sid, (cid, cb) in self._response_subs.items() if cid == chat_id and not is_progress]
        for sid, cb in subs:
            try:
                cb(clean_text)
            except Exception:
                log.debug("A2A response callback error for sub %s", sid, exc_info=True)
        if self._browser_sink and source == "browser" and not is_internal_chat_id(chat_id):
            self._browser_sink(
                {
                    "type": "chat",
                    "role": "assistant",
                    "content": clean_text,
                    "markdown": bool(parse_mode),
                    "is_progress": bool(is_progress),
                    "ts": message_ts,
                    "task_id": str(task_id or ""),
                    "source": source,
                    "transport": outbound_transport,
                    "trace_id": str(trace_id or aef_tracing.current_trace_id()),
                }
            )
        if _is_numeric_chat_id(chat_id) and not is_internal_chat_id(chat_id):
            publish_event(
                CHAT_OUTBOUND,
                {
                    "chat_id": normalized_chat_id,
                    "text": clean_text,
                    "markdown": bool(parse_mode),
                    "is_progress": bool(is_progress),
                    "ts": message_ts,
                    "task_id": str(task_id or ""),
                    "source": source,
                    "transport": outbound_transport,
                    "trace_id": str(trace_id or aef_tracing.current_trace_id()),
                },
            )
        return True, "ok"

    def send_chat_action(
        self,
        chat_id: Any,
        action: str = "typing",
        *,
        source: str = "",
    ) -> bool:
        """Send typing indicator to event subscribers."""
        if is_internal_chat_id(chat_id):
            return True
        self._outbox.put(
            {
                "type": "action",
                "content": action,
            }
        )
        if self._browser_sink and source == "browser":
            self._browser_sink({"type": "typing", "action": action, "source": source})
        normalized_chat_id = _coerce_chat_id(chat_id, 0)
        typing_transport = dict(self._chat_transports.get(normalized_chat_id, {}) or {})
        if _is_numeric_chat_id(chat_id):
            publish_event(
                CHAT_TYPING,
                {
                    "chat_id": normalized_chat_id,
                    "action": str(action or ""),
                    "source": source,
                    "transport": typing_transport,
                },
            )
        return True

    def send_photo(
        self,
        chat_id: Any,
        photo_bytes: bytes,
        caption: str = "",
        mime: str = "image/png",
        source: str = "",
        filename: str = "",
        transport: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Send photo to event subscribers."""
        if is_internal_chat_id(chat_id):
            return True, "ok"
        b64_str = base64.b64encode(photo_bytes).decode("ascii")
        if not filename:
            suffix = {
                "image/gif": ".gif",
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
            }.get(str(mime or "").lower(), ".png")
            filename = f"image{suffix}"
        normalized_chat_id = _coerce_chat_id(chat_id, 0)
        photo_transport = dict(transport or self._chat_transports.get(normalized_chat_id, {}) or {})
        persistence_required = _try_drive_root() is not None
        attachment = _persist_outbound_attachment(
            chat_id=chat_id,
            data=photo_bytes,
            filename=pathlib.PurePosixPath(filename).name,
            mime=mime,
            caption=caption,
            source=source,
            transport=photo_transport,
        )
        if persistence_required and attachment is None:
            return False, "failed to persist attachment"
        msg: dict[str, Any] = {
            "type": "photo",
            "role": "assistant",
            "image_base64": b64_str,
            "mime": mime,
            "caption": caption,
            "ts": utc_now_iso(),
            "source": source,
        }
        if attachment:
            msg["attachments"] = [attachment]
            msg["url"] = attachment["url"]
        self._outbox.put(msg)
        if self._browser_sink and source == "browser":
            self._browser_sink(msg)
        if _is_numeric_chat_id(chat_id):
            publish_event(
                CHAT_PHOTO,
                {
                    "chat_id": normalized_chat_id,
                    "transport": photo_transport,
                    "caption": str(caption or ""),
                    "image_base64": b64_str,
                    "mime": str(mime or ""),
                    "source": source,
                    "ts": msg["ts"],
                },
            )
        return True, "ok"

    def send_image(
        self,
        chat_id: Any,
        image_bytes: bytes,
        *,
        filename: str,
        caption: str = "",
        mime: str = "image/png",
        source: str = "",
        transport: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        return self.send_photo(
            chat_id,
            image_bytes,
            caption=caption,
            mime=mime,
            source=source,
            filename=filename,
            transport=transport,
        )

    def send_file(
        self,
        chat_id: Any,
        file_bytes: bytes,
        *,
        filename: str,
        mime: str = "application/octet-stream",
        caption: str = "",
        source: str = "",
        transport: dict[str, Any] | None = None,
        task_id: str = "",
    ) -> tuple[bool, str]:
        del task_id
        if is_internal_chat_id(chat_id):
            return True, "ok"
        attachment = _persist_outbound_attachment(
            chat_id=chat_id,
            data=file_bytes,
            filename=pathlib.PurePosixPath(filename).name,
            mime=mime,
            caption=caption,
            source=source,
            transport=transport,
        )
        if attachment is None:
            return False, "failed to persist attachment"
        msg = {
            "type": "chat",
            "role": "assistant",
            "content": caption,
            "attachments": [attachment],
            "ts": utc_now_iso(),
            "source": source,
        }
        self._outbox.put(msg)
        if self._browser_sink and source == "browser":
            self._browser_sink(msg)
        return True, "ok"

    def push_log(self, event: dict[str, Any]) -> None:
        """Stream runtime log events to browser WebSocket clients."""
        try:
            self._log_queue.put_nowait(event)
        except queue.Full:
            try:
                self._log_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._log_queue.put_nowait(event)
            except queue.Full:
                pass
        if self._browser_sink:
            self._browser_sink({"type": "log", "data": event})

    def ui_poll_logs(self) -> list[dict[str, Any]]:
        """Drain pending log events for browser UI compatibility."""
        batch: list[dict[str, Any]] = []
        for _ in range(50):
            try:
                batch.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        return batch


def _strip_markdown(text: str) -> str:
    """Best-effort markdown-to-plain-text fallback."""
    text = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\*\-]\s+", "• ", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = text.replace("`", "")
    return text


def _send_markdown(
    chat_id: Any,
    text: str,
    ts: str | None = None,
    is_progress: bool = False,
    task_id: str = "",
    source: str = "",
    trace_id: str = "",
    transport: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Send markdown text through the bridge."""
    bridge = get_bridge()
    if not text:
        return False, "empty"
    return bridge.send_message(
        chat_id,
        text,
        parse_mode="markdown",
        ts=ts,
        is_progress=is_progress,
        task_id=task_id,
        source=source,
        trace_id=trace_id,
        transport=transport,
    )


def log_chat(
    actor_type: ActorType,
    chat_id: Any,
    user_id: Any,
    text: str,
    ts: str | None = None,
    fmt: str = "",
    source: str = "",
    sender_label: str = "",
    sender_session_id: str = "",
    client_message_id: str = "",
    transport: dict[str, Any] | None = None,
    task_id: str = "",
    conversation_id: str = "",
    extra_metadata: dict[str, Any] | None = None,
) -> Any:
    """Persist one chat message and return it, or ``None`` when nothing was written."""
    drive_root = _try_drive_root()
    if drive_root is None:
        return None
    try:
        session_id = current_session_id()
        persisted = append_chat_message(
            drive_root,
            actor_type=actor_type,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            session_id=session_id,
            ts=ts,
            fmt=fmt,
            source=source,
            sender_label=sender_label,
            sender_session_id=sender_session_id,
            client_message_id=client_message_id,
            transport=transport,
            task_id=task_id,
            conversation_id=conversation_id or _transport_conversation_id(source, chat_id),
            extra_metadata=extra_metadata,
        )
        # Every persisted row counts toward the backlog, so every one asks. Scheduling after
        # a failed run is left to this path, and an owner message has to be able to trigger it.
        if persisted is not None:
            try:
                from supervisor.queue import get_task_queue

                get_task_queue(drive_root).schedule_context_consolidation(load_settings())
            except Exception:
                log.warning("Failed to schedule durable context consolidation", exc_info=True)
        return persisted
    except Exception:
        # Losing a chat row silently is how history quietly stops matching what happened.
        log.warning("Failed to persist chat message", exc_info=True)
        return None


def _send_dialog_text(
    chat_id: Any,
    text: str,
    *,
    fmt: str,
    ts: str,
    is_progress: bool,
    task_id: str,
    source: str,
    trace_id: str,
    transport: dict[str, Any] | None,
) -> tuple[bool, str]:
    if fmt == "markdown":
        return _send_markdown(
            chat_id,
            text,
            ts=ts,
            is_progress=is_progress,
            task_id=task_id,
            source=source,
            trace_id=trace_id,
            transport=transport,
        )

    bridge = get_bridge()
    return bridge.send_message(
        chat_id,
        text,
        ts=ts,
        is_progress=is_progress,
        task_id=task_id,
        source=source,
        trace_id=trace_id,
        transport=transport,
    )


def _add_aef_output_response(controller: Any, *, http_code: int) -> None:
    try:
        controller.add_response(
            headers=aef_tracing.with_trace_header({}),
            body=aef_tracing.mask_trace_payload({"status": http_code}),
            http_code=http_code,
        )
    except Exception:
        log.debug("Failed to add AEF dialogs output response", exc_info=True)


def send_with_budget(
    chat_id: Any,
    text: str,
    log_text: str | None = None,
    fmt: str = "",
    is_progress: bool = False,
    task_id: str = "",
    ts: str | None = None,
    source: str = "",
    trace_id: str = "",
    transport: dict[str, Any] | None = None,
) -> None:
    with aef_tracing.trace_scope(trace_id):
        st = load_state()
        aef_tracing.set_session_id(str(st.session_id or ""))
        owner_id = st.owner_id if st.owner_id not in (None, "") else 0
        _text = str(text or "")
        msg_ts = ts or utc_now_iso()

        drive_root = _try_drive_root()
        if is_progress and drive_root is not None:
            try:
                append_chat_message(
                    drive_root,
                    actor_type="agent",
                    chat_id=chat_id,
                    user_id=owner_id,
                    text=text if log_text is None else log_text,
                    session_id=str(st.session_id or ""),
                    ts=msg_ts,
                    fmt=fmt,
                    source=source,
                    task_id=task_id,
                    is_progress=True,
                    conversation_id=_transport_conversation_id(source, chat_id),
                    transport=transport,
                )
            except Exception:
                log.debug("Failed to persist progress chat message", exc_info=True)
        else:
            log_chat(
                "agent",
                chat_id,
                owner_id,
                text if log_text is None else log_text,
                ts=msg_ts,
                fmt=fmt,
                source=source,
                task_id=task_id,
                transport=transport,
            )

        if _text.strip() in ("", "\u200b"):
            return
        if not is_progress and not is_internal_chat_id(chat_id):
            with aef_tracing.output_request(
                headers=aef_tracing.with_trace_header({}),
                body=aef_tracing.mask_trace_payload({"chat_id": chat_id, "task_id": task_id}),
                path="/dialogs/sendMessage",
                method="POST",
            ) as output_span:
                try:
                    ok, _err = _send_dialog_text(
                        chat_id,
                        _text,
                        fmt=fmt,
                        ts=msg_ts,
                        is_progress=is_progress,
                        task_id=task_id,
                        source=source,
                        trace_id=aef_tracing.current_trace_id(),
                        transport=transport,
                    )
                except Exception:
                    _add_aef_output_response(output_span, http_code=500)
                    raise
                _add_aef_output_response(output_span, http_code=200 if ok else 500)
            return

        _send_dialog_text(
            chat_id,
            _text,
            fmt=fmt,
            ts=msg_ts,
            is_progress=is_progress,
            task_id=task_id,
            source=source,
            trace_id=aef_tracing.current_trace_id(),
            transport=transport,
        )


def configure_outbound_sender(sender: Callable[..., Any] | None) -> None:
    """Set the source-aware runtime sender used by host APIs."""
    global _outbound_sender
    _outbound_sender = sender


def send_outbound_message(chat_id: Any, text: str, **kwargs: Any) -> None:
    """Send through the active runtime fanout, falling back to the local bus."""
    sender = _outbound_sender
    source = str(kwargs.get("source") or "").strip().lower()
    if sender is None and is_dialogs_source(source):
        raise RuntimeError("source-aware outbound transport is not ready")
    sender = sender or send_with_budget
    sender(chat_id, text, **kwargs)
