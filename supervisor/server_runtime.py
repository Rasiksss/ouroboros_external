from __future__ import annotations

import logging
import pathlib
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ouroboros.observability import aef_tracing
from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import delete_attachment_metadata, store_attachment_path
from ouroboros.storage.chat_store import (
    append_chat_message,
    get_chat_message_by_client_id,
    mark_chat_message_processed,
)
from ouroboros.storage.runtime_models import OwnerRuntimeState, is_dialogs_source
from ouroboros.storage.runtime_store import get_runtime_store
from ouroboros.storage.settings.schema import AppSettings
from supervisor.commands import handle_slash_command
from supervisor.message_bus import CLEAN_HISTORY_RESPONSE, clear_chat_history
from supervisor.messaging.base import InboundMessage
from supervisor.messaging.dialogs import DialogsProvider
from supervisor.messaging.dialogs.provider import _dialogs_bot_token
from supervisor.orchestrator.envelope import InboundEnvelope
from supervisor.orchestrator.intent import TaskIntentKind
from supervisor.queue import _queue_lock
from supervisor.runtime_commands import is_clean_history_command, is_runtime_command
from supervisor.state import owner_state_transaction

log = logging.getLogger(__name__)


def _delivery_result(value: object) -> tuple[bool, str] | None:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], bool) and isinstance(value[1], str):
        return value
    return None


def dialogs_feature_enabled(settings: AppSettings) -> bool:
    return bool(str(settings.DIALOGS_GRPC_ENDPOINT or "").strip() and _dialogs_bot_token(settings))


def ensure_dialogs_runtime_transport_provider(transport: Any):
    if getattr(transport, "closed", False):
        return None
    if not dialogs_feature_enabled(transport.settings):
        transport.provider = None
        transport.provider_active = False
        return None

    provider = getattr(transport, "provider", None)
    if provider is not None:
        transport.provider_active = True
        return provider

    with transport._provider_lock:
        if getattr(transport, "closed", False):
            return None
        provider = getattr(transport, "provider", None)
        if provider is not None:
            transport.provider_active = True
            return provider

        recovering = bool(getattr(transport, "startup_error", None))
        if recovering and not transport._recovery_active:
            transport._recovery_active = True
            log.info("event=dialogs_transport_recovery_started")
        try:
            provider = transport.dialogs_provider_factory(transport.settings)
            provider.connect()
            provider.authenticate()
        except Exception as exc:
            transport.provider = None
            transport.provider_active = False
            transport.startup_error = type(exc).__name__
            if not recovering:
                log.error(
                    "event=dialogs_transport_unavailable exception_type=%s",
                    type(exc).__name__,
                )
            return None

        transport.provider = provider
        transport.provider_active = True
        transport.startup_error = None
        if recovering:
            log.info("event=dialogs_transport_restored")
        else:
            log.info("event=dialogs_transport_started")
        transport._recovery_active = False
        return provider


@dataclass
class DialogsRuntimeTransport:
    settings: AppSettings
    bridge: Any
    logger: Any
    dialogs_provider_factory: Callable[[AppSettings], Any] = DialogsProvider
    provider: Any = None
    provider_active: bool = False
    startup_error: str | None = None
    dialogs_input_thread: threading.Thread | None = None
    closed: bool = False
    _recovery_active: bool = False
    _provider_lock: threading.Lock = field(default_factory=threading.Lock)

    def close(self) -> None:
        with self._provider_lock:
            if self.closed:
                return
            self.closed = True
            was_active = self.provider_active or self.provider is not None
            self.provider_active = False
            provider = self.provider
        thread = self.dialogs_input_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        if was_active:
            log.info("event=dialogs_transport_stopped")

    def send_with_budget(
        self,
        chat_id: Any,
        text: str,
        log_text: str | None = None,
        force_budget: bool = False,
        fmt: str = "",
        is_progress: bool = False,
        task_id: str = "",
        ts: str | None = None,
        source: str = "",
        trace_id: str = "",
        transport: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        del log_text, force_budget, ts, source, trace_id
        provider = ensure_dialogs_runtime_transport_provider(self)
        if provider is None:
            return False, "dialogs transport disabled"
        metadata = dict(transport or {})
        send_options: dict[str, Any] = {}
        if self.settings.DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED:
            send_options.update(is_progress=is_progress, task_id=task_id)
        source_message_id = str(metadata.get("source_message_id") or "").strip()
        if self.settings.DIALOGS_REPLY_ENABLED and source_message_id:
            send_options["reply_to"] = [source_message_id]
        target_uid = metadata.get("target_uid")
        if self.settings.DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED and target_uid not in (None, ""):
            send_options["target_uid"] = target_uid
        return provider.send_text(chat_id, text, fmt=fmt, **send_options)

    def send_typing(
        self,
        chat_id: Any,
        action: str = "typing",
        *,
        source: str = "",
    ) -> Any:
        del source
        provider = ensure_dialogs_runtime_transport_provider(self)
        if provider is None:
            return None
        send_typing = getattr(provider, "send_typing", None)
        return send_typing(chat_id, action) if callable(send_typing) else None

    def send_photo(
        self,
        chat_id: Any,
        image_bytes: bytes,
        caption: str = "",
        mime: str = "image/png",
        *,
        source: str = "",
        transport: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        del source
        provider = ensure_dialogs_runtime_transport_provider(self)
        if provider is None:
            return False, "dialogs transport disabled"
        return provider.send_photo(
            chat_id,
            image_bytes,
            caption=caption,
            mime=mime,
            **self._media_options(transport),
        )

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
        del source
        provider = ensure_dialogs_runtime_transport_provider(self)
        if provider is None:
            return False, "dialogs transport disabled"
        return provider.send_image(
            chat_id,
            image_bytes,
            filename=filename,
            caption=caption,
            mime=mime,
            **self._media_options(transport),
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
        del source
        provider = ensure_dialogs_runtime_transport_provider(self)
        if provider is None:
            return False, "dialogs transport disabled"
        return provider.send_file(
            chat_id,
            file_bytes,
            filename=filename,
            mime=mime,
            caption=caption,
            **self._media_options(transport),
            **({"task_id": task_id} if task_id else {}),
        )

    def _media_options(self, transport: dict[str, Any] | None) -> dict[str, Any]:
        metadata = dict(transport or {})
        options: dict[str, Any] = {}
        source_message_id = str(metadata.get("source_message_id") or "").strip()
        if self.settings.DIALOGS_REPLY_ENABLED and source_message_id:
            options["reply_to"] = [source_message_id]
        target_uid = metadata.get("target_uid")
        if self.settings.DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED and target_uid not in (None, ""):
            options["target_uid"] = target_uid
        return options


def build_server_runtime_transport(
    *,
    settings: AppSettings,
    bridge,
    logger,
    dialogs_provider_factory=DialogsProvider,
):
    transport = DialogsRuntimeTransport(
        settings=settings,
        bridge=bridge,
        logger=logger,
        dialogs_provider_factory=dialogs_provider_factory,
    )
    ensure_dialogs_runtime_transport_provider(transport)
    return transport


def attach_dialogs_transport_fanout(
    event_ctx: Any,
    runtime_transport: Any,
    load_state: Callable[[], OwnerRuntimeState],
) -> Any:
    base_send_with_budget = event_ctx.send_with_budget
    base_send_typing = getattr(event_ctx, "send_typing", None)
    base_send_photo = getattr(event_ctx, "send_photo", None)
    bridge = getattr(event_ctx, "bridge", None)
    base_send_image = getattr(event_ctx, "send_image", None) or getattr(bridge, "send_image", None)
    base_send_file = getattr(event_ctx, "send_file", None) or getattr(bridge, "send_file", None)

    def _dialogs_should_receive(chat_id: Any, source: str = "") -> bool:
        if source:
            return is_dialogs_source(source)
        provider = getattr(runtime_transport, "provider", None)
        can_send = getattr(provider, "can_send_to_chat_id", None)
        if callable(can_send):
            try:
                if can_send(chat_id):
                    return True
            except Exception:
                return False
        try:
            st = load_state()
        except Exception:
            return False
        return (
            st.owner_chat_source == "dialogs"
            and st.owner_chat_id not in (None, "")
            and str(st.owner_chat_id) == str(chat_id)
        )

    def _send_with_dialogs_fanout(chat_id: Any, *args: Any, **kwargs: Any) -> None:
        if _dialogs_should_receive(chat_id, str(kwargs.get("source") or "")):
            ok, error = runtime_transport.send_with_budget(chat_id, *args, **kwargs)
            if not ok:
                raise RuntimeError(error or "dialogs transport delivery failed")
        base_send_with_budget(chat_id, *args, **kwargs)

    def _send_typing_with_dialogs_fanout(
        chat_id: Any,
        action: str = "typing",
        *,
        source: str = "",
    ) -> None:
        if callable(base_send_typing):
            base_send_typing(chat_id, action, source=source)
        if _dialogs_should_receive(chat_id, source):
            runtime_transport.send_typing(chat_id, action, source=source)

    def _send_media(
        kind: str,
        chat_id: Any,
        data: bytes,
        *,
        filename: str = "",
        caption: str = "",
        mime: str,
        source: str,
        transport: dict[str, Any] | None,
        task_id: str = "",
    ) -> tuple[bool, str]:
        common_kwargs: dict[str, Any] = {
            "caption": caption,
            "mime": mime,
            "source": source,
            "transport": transport,
        }
        if kind != "photo":
            common_kwargs["filename"] = filename
        external = getattr(runtime_transport, f"send_{kind}")
        local = {"photo": base_send_photo, "image": base_send_image, "file": base_send_file}[kind]
        external_result = (True, "ok")
        delivered_externally = False
        if _dialogs_should_receive(chat_id, source):
            external_kwargs = dict(common_kwargs)
            if kind == "file" and task_id:
                external_kwargs["task_id"] = task_id
            result = external(chat_id, data, **external_kwargs)
            delivery = _delivery_result(result)
            if delivery is None:
                return False, "invalid external transport result"
            ok, error = delivery
            if not ok:
                return False, str(error or f"dialogs {kind} delivery failed")
            external_result = (True, str(error or "ok"))
            delivered_externally = True
        if not callable(local):
            return external_result
        try:
            result = local(chat_id, data, **common_kwargs)
        except Exception:
            if not delivered_externally:
                raise
            log.error("Local %s persistence failed after Dialogs delivery", kind, exc_info=True)
            return external_result
        delivery = _delivery_result(result)
        if delivery is None:
            return external_result if delivered_externally else (False, "invalid local transport result")
        if delivered_externally and not delivery[0]:
            log.error("Local %s persistence failed after Dialogs delivery: %s", kind, delivery[1])
            return external_result
        return delivery

    def _send_photo_with_dialogs_fanout(
        chat_id: Any,
        image_bytes: bytes,
        caption: str = "",
        mime: str = "image/png",
        *,
        source: str = "",
        transport: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        return _send_media(
            "photo",
            chat_id,
            image_bytes,
            caption=caption,
            mime=mime,
            source=source,
            transport=transport,
        )

    def _send_image_with_dialogs_fanout(
        chat_id: Any,
        image_bytes: bytes,
        *,
        filename: str,
        caption: str = "",
        mime: str = "image/png",
        source: str = "",
        transport: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        return _send_media(
            "image",
            chat_id,
            image_bytes,
            filename=filename,
            caption=caption,
            mime=mime,
            source=source,
            transport=transport,
        )

    def _send_file_with_dialogs_fanout(
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
        return _send_media(
            "file",
            chat_id,
            file_bytes,
            filename=filename,
            caption=caption,
            mime=mime,
            source=source,
            transport=transport,
            task_id=task_id,
        )

    event_ctx.send_with_budget = _send_with_dialogs_fanout
    event_ctx.send_typing = _send_typing_with_dialogs_fanout
    event_ctx.send_photo = _send_photo_with_dialogs_fanout
    event_ctx.send_image = _send_image_with_dialogs_fanout
    event_ctx.send_file = _send_file_with_dialogs_fanout
    event_ctx.runtime_transport = runtime_transport
    from supervisor.message_bus import configure_outbound_sender

    configure_outbound_sender(event_ctx.send_with_budget)
    return event_ctx


def start_dialogs_runtime_input_thread(
    *,
    runtime_transport,
    dialogs_cursor_ref: dict[str, Any],
    ctx: Any,
    logger,
    stop_event,
):
    if runtime_transport is None:
        return None

    def _run():
        startup_backoff_sec = 1.0
        pending_message: InboundMessage | None = None
        pending_cursor: Any = None
        try:
            while not stop_event.is_set() and not getattr(runtime_transport, "closed", False):
                provider = ensure_dialogs_runtime_transport_provider(runtime_transport)
                if provider is None:
                    if stop_event.wait(startup_backoff_sec):
                        return
                    startup_backoff_sec = min(startup_backoff_sec * 2, 30.0)
                    continue

                try:
                    if pending_message is None:
                        cursor = dialogs_cursor_ref.get("value", 0)
                        pending_message, pending_cursor = provider.recv_update(cursor, timeout_sec=1)
                        if pending_message is None:
                            startup_backoff_sec = 1.0
                            continue
                    if stop_event.is_set() or getattr(runtime_transport, "closed", False):
                        return
                    handled = handle_runtime_inbound(
                        source="dialogs",
                        message=pending_message,
                        cursor=pending_cursor,
                        ctx=ctx,
                    )
                    if not handled:
                        raise RuntimeError("Dialogs inbound message was not handled")
                    dialogs_cursor_ref["value"] = pending_cursor
                    pending_message = None
                    pending_cursor = None
                    startup_backoff_sec = 1.0
                except Exception as exc:
                    if logger is not None:
                        logger.warning(
                            "Dialogs input loop failed exception_type=%s error=%s",
                            type(exc).__name__,
                            exc,
                            exc_info=True,
                        )
                    if stop_event.wait(0.25):
                        return
        finally:
            if pending_message is not None:
                _delete_inbound_temp_files(pending_message)

    thread = threading.Thread(target=_run, name="dialogs-input-loop", daemon=True)
    thread.start()
    return thread


def persist_dialogs_cursor_state(state: OwnerRuntimeState, cursor: Any) -> None:
    if cursor is None:
        return

    provider_state = state.provider_state
    if not isinstance(provider_state, dict):
        provider_state = {}
        state.provider_state = provider_state

    dialogs_state = provider_state.get("dialogs")
    if not isinstance(dialogs_state, dict):
        dialogs_state = {"seq": 0}
        provider_state["dialogs"] = dialogs_state

    try:
        dialogs_state["seq"] = int(cursor)
    except (TypeError, ValueError):
        return


def handle_runtime_inbound(*, source: str, message: InboundMessage, cursor: Any, ctx: Any) -> bool:
    with owner_state_transaction():
        st = ctx.load_state()
        if source == "dialogs":
            owner_id = st.owner_id
            owner_chat_id = st.owner_chat_id
            local_placeholder = owner_id == 1 and owner_chat_id == 1
            if owner_id is None or local_placeholder:
                st.owner_id = message.user_id
                st.owner_chat_id = message.chat_id
            st.owner_chat_source = "dialogs"
        ctx.save_state(st)
    attachment_ids: list[str] = []
    attachments: list[dict[str, Any]] = []
    acknowledged = False
    try:
        existing = get_chat_message_by_client_id(
            pathlib.Path(ctx.DRIVE_ROOT),
            actor_type="owner",
            source=source,
            chat_id=message.chat_id,
            user_id=message.user_id,
            client_message_id=message.source_message_id,
        )
        raw_attachments = (existing.metadata or {}).get("attachments") if existing else None
        if isinstance(raw_attachments, list):
            attachments = [dict(item) for item in raw_attachments if isinstance(item, dict)]
            attachment_ids = [str(item.get("id")) for item in attachments if item.get("id")]
        else:
            attachment_ids, attachments = _persist_inbound_attachments(
                message=message,
                source=source,
                ctx=ctx,
            )
        handled = _dispatch_runtime_inbound(
            message=message,
            text=(message.text or ""),
            ctx=ctx,
            source=source,
            attachment_ids=attachment_ids,
            attachments=attachments,
        )
        if handled:
            with owner_state_transaction():
                latest = ctx.load_state()
                if source == "dialogs":
                    persist_dialogs_cursor_state(latest, cursor)
                latest.last_owner_message_at = datetime.now(timezone.utc).isoformat()
                ctx.save_state(latest)
            acknowledged = True
        return handled
    finally:
        if acknowledged:
            _delete_inbound_temp_files(message)


def _delete_inbound_temp_files(message: InboundMessage) -> None:
    for attachment in message.attachments:
        pathlib.Path(attachment.temp_path).unlink(missing_ok=True)


def _persist_inbound_attachments(
    *,
    message: InboundMessage,
    source: str,
    ctx: Any,
) -> tuple[list[str], list[dict[str, Any]]]:
    if not message.attachments:
        return [], []
    provider = create_storage_provider(data_dir=ctx.DRIVE_ROOT)
    message_id = str(message.source_message_id or uuid.uuid4().hex)
    attachment_ids: list[str] = []
    metadata_items: list[dict[str, Any]] = []
    try:
        for inbound in message.attachments:
            attachment_id = uuid.uuid4().hex
            metadata = store_attachment_path(
                provider,
                attachment_id=attachment_id,
                filename=inbound.filename,
                mime=inbound.mime,
                path=pathlib.Path(inbound.temp_path),
                source=source,
                status="ready",
                message_id=message_id,
            )
            attachment_ids.append(attachment_id)
            metadata_items.append(dict(metadata))
    except Exception:
        _rollback_inbound_attachments(ctx, metadata_items)
        raise
    return attachment_ids, metadata_items


def _rollback_inbound_attachments(ctx: Any, attachments: list[dict[str, Any]]) -> bool:
    provider = create_storage_provider(data_dir=ctx.DRIVE_ROOT)
    succeeded = True
    for metadata in attachments:
        attachment_id = str(metadata.get("id") or "")
        storage_key = str(metadata.get("storage_key") or "")
        try:
            if storage_key:
                provider.delete(SCOPE_UPLOADS, storage_key)
            if attachment_id:
                delete_attachment_metadata(provider, attachment_id)
        except Exception:
            succeeded = False
            log.warning("Failed to roll back inbound attachment %s", attachment_id, exc_info=True)
    return succeeded


def _aef_session_id_from_ctx(ctx: Any) -> str:
    load_state = getattr(ctx, "load_state", None)
    if not callable(load_state):
        return ""
    try:
        return str(load_state().session_id or "").strip()
    except Exception:
        log.debug("Failed to load AEF session id from runtime context", exc_info=True)
        return ""


def _aef_input_path(source: str) -> str:
    normalized = str(source or "").strip().lower()
    if normalized == "sberchat":
        return "/sberchat"
    return "/dialogs"


def _add_aef_input_response(controller: Any, *, http_code: int) -> None:
    try:
        controller.add_response(
            headers=aef_tracing.with_trace_header({}),
            body=aef_tracing.mask_trace_payload({"status": http_code}),
            http_code=http_code,
        )
    except Exception:
        log.debug("Failed to add AEF dialogs input response", exc_info=True)


def _dispatch_runtime_inbound(
    *,
    message: InboundMessage,
    text: str,
    ctx: Any,
    source: str = "dialogs",
    attachment_ids: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    normalized = str(text or "")
    if not normalized and attachment_ids:
        normalized = "(file attached)"
    if not normalized:
        return True

    aef_tracing.set_session_id(_aef_session_id_from_ctx(ctx))
    with (
        aef_tracing.trace_scope(),
        aef_tracing.input_request(
            headers=aef_tracing.with_trace_header({}),
            body=aef_tracing.mask_trace_payload({"source": source}),
            path=_aef_input_path(source),
            method="POST",
        ) as input_span,
    ):
        try:
            handled = _dispatch_runtime_inbound_inner(
                message=message,
                text=normalized,
                ctx=ctx,
                source=source,
                attachment_ids=attachment_ids,
                attachments=attachments,
            )
        except Exception:
            _add_aef_input_response(input_span, http_code=500)
            raise
        _add_aef_input_response(input_span, http_code=202 if handled else 500)
        return handled


def _dispatch_runtime_inbound_inner(
    *,
    message: InboundMessage,
    text: str,
    ctx: Any,
    source: str = "dialogs",
    attachment_ids: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    chat_id = message.chat_id
    user_id = message.user_id
    normalized = str(text or "")
    stored_attachments = list(attachments or [])
    source_message_id = str(message.source_message_id or "")
    orchestrator = getattr(ctx, "orchestrator", None)
    transport = {"source_message_id": source_message_id, "target_uid": user_id}
    is_command = is_runtime_command(normalized)

    if stored_attachments and is_command and not message.history_only:
        if not _rollback_inbound_attachments(ctx, stored_attachments):
            raise RuntimeError("failed to roll back attachments for rejected command")
        filenames = ", ".join(str(item.get("filename") or "attachment") for item in stored_attachments)
        ctx.send_with_budget(
            chat_id,
            f"Команды нельзя отправлять вместе с файлами: {filenames}",  # noqa: RUF001
            source=source,
            transport=transport,
        )
        return True

    try:
        session_id = str(ctx.load_state().session_id or "")
    except Exception:
        session_id = ""
    persisted = get_chat_message_by_client_id(
        pathlib.Path(ctx.DRIVE_ROOT),
        actor_type="owner",
        source=source,
        chat_id=chat_id,
        user_id=user_id,
        client_message_id=source_message_id,
        sender_session_id=str(getattr(message, "sender_session_id", "") or ""),
    )
    if persisted is not None and bool((persisted.metadata or {}).get("inbound_processed")):
        return True
    if persisted is not None:
        raw_attachments = (persisted.metadata or {}).get("attachments")
        if isinstance(raw_attachments, list):
            stored_attachments = [dict(item) for item in raw_attachments if isinstance(item, dict)]
            attachment_ids = [str(item.get("id")) for item in stored_attachments if item.get("id")]
    elif not message.image_data or orchestrator is None:
        extra_metadata: dict[str, Any] = {"inbound_processed": False}
        if message.history_only:
            extra_metadata["history_only"] = True
        if stored_attachments:
            extra_metadata["attachments"] = stored_attachments
        try:
            persisted = append_chat_message(
                pathlib.Path(ctx.DRIVE_ROOT),
                actor_type="owner",
                chat_id=chat_id,
                user_id=user_id,
                text=normalized,
                session_id=session_id,
                ts=str(getattr(message, "ts", "") or "") or None,
                source=source,
                sender_label=message.sender_label,
                sender_session_id=str(getattr(message, "sender_session_id", "") or ""),
                client_message_id=source_message_id,
                conversation_id=message.conversation_id,
                transport=transport,
                extra_metadata=extra_metadata,
            )
        except Exception:
            if stored_attachments:
                _rollback_inbound_attachments(ctx, stored_attachments)
            raise
        if persisted is None:
            persisted = get_chat_message_by_client_id(
                pathlib.Path(ctx.DRIVE_ROOT),
                actor_type="owner",
                source=source,
                chat_id=chat_id,
                user_id=user_id,
                client_message_id=source_message_id,
                sender_session_id=str(getattr(message, "sender_session_id", "") or ""),
            )
            if persisted is None:
                raise RuntimeError("duplicate inbound message disappeared from history")
            if bool((persisted.metadata or {}).get("inbound_processed")):
                return True

    persisted_message_id = persisted.message_id if persisted is not None else ""

    if message.history_only:
        if persisted_message_id:
            mark_chat_message_processed(pathlib.Path(ctx.DRIVE_ROOT), persisted_message_id)
        return True

    if is_clean_history_command(normalized):
        store = getattr(ctx, "store", None) or get_runtime_store(pathlib.Path(ctx.DRIVE_ROOT))
        if store is None:
            raise RuntimeError("runtime store is unavailable")
        with _queue_lock:
            active = store.list_tasks(
                statuses=["queued", "scheduled", "interrupted", "running"],
                limit=1,
            )
            if not active:
                clear_chat_history(
                    ctx.DRIVE_ROOT,
                    preserve_message_id=persisted_message_id,
                )
        if active:
            ctx.send_with_budget(
                chat_id,
                "Нельзя очистить историю, пока есть активные задачи.",
                source=source,
                transport=transport,
            )
        else:
            bridge = getattr(ctx, "bridge", None)
            broadcast = getattr(bridge, "broadcast", None)
            if callable(broadcast):
                broadcast({"type": "chat_history_cleared", "ts": datetime.now(timezone.utc).isoformat()})
            ctx.send_with_budget(
                chat_id,
                CLEAN_HISTORY_RESPONSE,
                source=source,
                transport=transport,
            )
        if persisted_message_id:
            mark_chat_message_processed(pathlib.Path(ctx.DRIVE_ROOT), persisted_message_id)
        return True

    if orchestrator is not None:
        envelope = InboundEnvelope.from_chat(
            chat_id=chat_id,
            user_id=user_id,
            text=normalized,
            source=source,
            sender_label=message.sender_label,
            image_data=message.image_data,
            conversation_id=message.conversation_id,
            client_message_id=source_message_id,
            attachment_ids=list(attachment_ids or []),
            attachments=stored_attachments,
            transport=transport,
        )
        result = orchestrator.submit(envelope)
        for attachment in stored_attachments:
            log.info(
                "event=inbound_attachment_bound task_id=%s filename=%s size=%s source=%s",
                getattr(result, "task_id", None) or source_message_id,
                pathlib.PurePosixPath(str(attachment.get("filename") or "attachment")).name,
                attachment.get("size", 0),
                source,
            )
        if not persisted_message_id and source_message_id:
            persisted = get_chat_message_by_client_id(
                pathlib.Path(ctx.DRIVE_ROOT),
                actor_type="owner",
                source=source,
                chat_id=chat_id,
                user_id=user_id,
                client_message_id=source_message_id,
            )
            persisted_message_id = persisted.message_id if persisted is not None else ""
        if result.intent == TaskIntentKind.SLASH_COMMAND:
            if persisted_message_id:
                mark_chat_message_processed(pathlib.Path(ctx.DRIVE_ROOT), persisted_message_id)
            handle_slash_command(
                result.slash_command,
                chat_id=chat_id,
                ctx=ctx,
                source=source,
                transport=transport,
            )
            return True
        if persisted_message_id:
            mark_chat_message_processed(pathlib.Path(ctx.DRIVE_ROOT), persisted_message_id)
        return True

    handle_chat_direct = getattr(ctx, "handle_chat_direct", None)
    if callable(handle_chat_direct):
        handle_chat_direct(chat_id, normalized, message.image_data)
    if persisted_message_id:
        mark_chat_message_processed(pathlib.Path(ctx.DRIVE_ROOT), persisted_message_id)
    return True
