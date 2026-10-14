"""The dialogs (SberChat SDK) transport.

Everything the SDK conversation needs lives here: the provider lifecycle, the
update loop, inbound attachment persistence, the update cursor, AEF input
tracing, and outbound delivery. The supervisor sees an ``InboundEnvelope`` on the
inbox and an ``OutboundMessage`` on the outbox, and nothing else.

Outbound uses a ``SHARED`` subscription. Dialogs delivery is a plain API call
that works from any instance, so competing consumers give the per-chat claim for
free, and a reply produced but not yet sent survives a restart instead of being
dropped with the process.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext, suppress
from typing import Any

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.attachments import AttachmentMetadata, StoredFile
from ouroboros.contracts.chat_commands import answered_slash_command
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.observability import aef_tracing
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.chat_attachments import store_attachment_fileobj
from ouroboros.storage.chat_store import append_chat_message, get_chat_message_by_client_id
from ouroboros.storage.file_catalog import FileTransferPending, FileUnavailable
from ouroboros.storage.runtime_models import ConversationMessage, is_dialogs_source
from ouroboros.storage.settings.schema import AppSettings
from transport.dialogs.cursor import DialogsCursor
from transport.dialogs.interfaces import IDialogsCursorRepository
from transport.dialogs.messages import TRANSPORT_NAME, InboundMessage
from transport.dialogs.provider import DialogsProvider, _dialogs_bot_token
from transport.dialogs.state import dialogs_scope
from transport.interfaces import (
    DeliveredMessage,
    IEnvelopeBuilder,
    IMessageGateway,
    InboundEnvelope,
    IOutboxReader,
    OutboundKind,
    OutboundMessage,
    SubscriptionMode,
)
from transport.journal import (
    DEFAULT_SWEEP_LIMIT,
    UNPROCESSED_METADATA,
    mark_processed,
    replay_unprocessed,
    stored_attachments,
)
from transport.reader_loop import READER_JOIN_TIMEOUT_MARGIN_SEC, run_reader_loop

log = logging.getLogger(__name__)

COMMAND_WITH_FILES_REFUSAL = "Команды нельзя отправлять вместе с файлами: "  # noqa: RUF001
UNAVAILABLE_FILES_REFUSAL = "Вложения больше недоступны. Отправьте сообщение с файлами ещё раз: "  # noqa: RUF001

_OUTBOX_POLL_TIMEOUT_SEC = 0.5
_INPUT_POLL_TIMEOUT_SEC = 1
_INPUT_RETRY_DELAY_SEC = 0.25
_STARTUP_BACKOFF_MAX_SEC = 30.0
#: A `nack` puts the message back at the head of its subscription, so an endless
#: retry would stall every dialogs message behind it. After this many attempts
#: the message is acknowledged and dropped with an error.
_DELIVERY_ATTEMPT_LIMIT = 3
_DELIVERY_RETRY_BACKOFF_SEC = 1.0
#: Long enough for the reader to finish the poll it is in plus the backoff of a
#: delivery it is retrying.
_READER_JOIN_TIMEOUT_SEC = _OUTBOX_POLL_TIMEOUT_SEC + _DELIVERY_RETRY_BACKOFF_SEC + READER_JOIN_TIMEOUT_MARGIN_SEC
#: How long `close` waits for the provider lock. `ensure_provider` holds it
#: across `connect`/`authenticate`, so an unresponsive endpoint would otherwise
#: hang process shutdown for as long as the network takes.
_PROVIDER_CLOSE_LOCK_TIMEOUT_SEC = 2.0


def dialogs_feature_enabled(settings: AppSettings) -> bool:
    return bool(str(settings.DIALOGS_GRPC_ENDPOINT or "").strip() and _dialogs_bot_token(settings))


class DialogsTransport:
    """Owns the SDK conversation and both directions of the seam."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        gateway: IMessageGateway,
        storage: StorageProvider,
        drive_root: pathlib.Path,
        session_id: str,
        provider_factory: Callable[[AppSettings], DialogsProvider],
        cursor_repository: IDialogsCursorRepository,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.drive_root = pathlib.Path(drive_root)
        self.provider_factory = provider_factory
        self.provider: DialogsProvider | None = None
        self.startup_error: str | None = None
        self.closed = False
        self.cursor = DialogsCursor(cursor_repository)
        self._session_id = str(session_id or "")
        self._provider_lock = threading.Lock()
        self._recovery_active = False
        self._stop_event = threading.Event()
        self._input_thread: threading.Thread | None = None
        # Subscribed here rather than in `start`: `publish` fans into the
        # subscriptions that exist, so everything the supervisor sends between
        # its own start and this transport's would land nowhere.
        self._reader: IOutboxReader | None = gateway.outbox.reader(
            name=TRANSPORT_NAME,
            mode=SubscriptionMode.SHARED,
        )
        self._reader_thread: threading.Thread | None = None
        self._storage = storage

    @property
    def name(self) -> str:
        return TRANSPORT_NAME

    @property
    def provider_active(self) -> bool:
        return self.provider is not None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Connect the SDK and start both loops; the subscription is already open."""
        if self.closed:
            raise RuntimeError("a closed dialogs transport cannot be restarted")
        self.ensure_provider()
        if self._reader_thread is None:
            self._reader_thread = threading.Thread(
                target=self._read_outbox,
                name="dialogs-outbox-reader",
                daemon=True,
            )
            self._reader_thread.start()
        if self._input_thread is None:
            self._input_thread = threading.Thread(
                target=self._read_updates,
                name="dialogs-input-loop",
                daemon=True,
            )
            self._input_thread.start()

    def stop(self) -> None:
        self.close()

    def close(self) -> None:
        if self.closed and self._input_thread is None and self._reader_thread is None and self.provider is None:
            return
        # Signalled before the lock is touched: `ensure_provider` holds it across
        # network round-trips, and a shutdown that waited for the lock would
        # never reach the flag that lets the call inside it give up.
        self.closed = True
        self._stop_event.set()
        was_active = self.provider is not None
        # Join before releasing the subscription: a message still being
        # delivered when the reader is released is requeued behind the delivery
        # that then succeeds, and the user receives it twice.
        for thread in (self._input_thread, self._reader_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=_READER_JOIN_TIMEOUT_SEC)
        if any(thread is not None and thread.is_alive() for thread in (self._input_thread, self._reader_thread)):
            raise TimeoutError("Dialogs consumers did not stop within the shutdown budget")
        reader = self._reader
        if reader is not None:
            reader.close()
        self._reader = None
        self._reader_thread = None
        self._input_thread = None
        provider = None
        if self._provider_lock.acquire(timeout=_PROVIDER_CLOSE_LOCK_TIMEOUT_SEC):
            try:
                provider = self.provider
            finally:
                self._provider_lock.release()
        else:
            raise TimeoutError("Dialogs provider did not stop within the shutdown budget")
        close_provider = getattr(provider, "close", None)
        if callable(close_provider):
            close_provider()
        self.provider = None
        if was_active:
            log.info("event=dialogs_transport_stopped")

    def ensure_provider(self) -> DialogsProvider | None:
        """Connect on first use, and reconnect after a failure, logging transitions."""
        if self.closed or self._stop_event.is_set():
            return None
        if not dialogs_feature_enabled(self.settings):
            self.provider = None
            return None

        provider = self.provider
        if provider is not None:
            return provider

        with self._provider_lock:
            if self.closed or self._stop_event.is_set():
                return None
            provider = self.provider
            if provider is not None:
                return provider

            recovering = bool(self.startup_error)
            if recovering and not self._recovery_active:
                self._recovery_active = True
                log.info("event=dialogs_transport_recovery_started")
            try:
                provider = self.provider_factory(self.settings)
                provider.connect()
                provider.authenticate()
            except Exception as exc:
                self.provider = None
                self.startup_error = type(exc).__name__
                if not recovering:
                    log.error("event=dialogs_transport_unavailable exception_type=%s", type(exc).__name__)
                return None

            self.provider = provider
            self.startup_error = None
            if recovering:
                log.info("event=dialogs_transport_restored")
            else:
                log.info("event=dialogs_transport_started")
            self._recovery_active = False
            return provider

    # -- inbound ----------------------------------------------------------

    def _read_updates(self) -> None:
        """Pull one update at a time, retrying it until it is handled.

        The cursor advances only after a message is committed, so a failure
        replays the same update rather than skipping it, and the staged temp
        files stay on disk for that replay.
        """
        startup_backoff_sec = 1.0
        pending_message: InboundMessage | None = None
        pending_cursor: Any = None
        try:
            while not self._stop_event.is_set() and not self.closed:
                provider = self.ensure_provider()
                if provider is None:
                    if self._stop_event.wait(startup_backoff_sec):
                        return
                    startup_backoff_sec = min(startup_backoff_sec * 2, _STARTUP_BACKOFF_MAX_SEC)
                    continue
                try:
                    if pending_message is None:
                        pending_message, pending_cursor = provider.recv_update(
                            self.cursor.value,
                            timeout_sec=_INPUT_POLL_TIMEOUT_SEC,
                        )
                        if pending_message is None:
                            startup_backoff_sec = 1.0
                            continue
                    if self._stop_event.is_set() or self.closed:
                        return
                    self.handle_inbound(pending_message)
                    self.cursor.commit(pending_cursor)
                    _delete_temp_files(pending_message)
                    pending_message = None
                    pending_cursor = None
                    startup_backoff_sec = 1.0
                except Exception as exc:
                    log.warning(
                        "Dialogs input loop failed exception_type=%s error=%s",
                        type(exc).__name__,
                        exc,
                        exc_info=True,
                    )
                    if self._stop_event.wait(_INPUT_RETRY_DELAY_SEC):
                        return
        finally:
            if pending_message is not None:
                _delete_temp_files(pending_message)

    def handle_inbound(self, message: InboundMessage) -> None:
        """Handle the update under its ingress trace, originating one only when absent."""
        with aef_tracing.trace_scope(aef_tracing.ensure_trace_id(message.trace_id)):
            self._handle_inbound(message)

    def _handle_inbound(self, message: InboundMessage) -> None:
        """Persist accepted input before submission, leaving failures retryable."""
        text = str(message.text or "")
        transport = {
            "source_message_id": str(message.source_message_id or ""),
            "target_uid": message.user_id,
        }
        # Only the commands the runtime answers itself: they never become a task, so an
        # attachment bound to one would have no run to reach.
        if message.attachments and answered_slash_command(text) and not message.history_only:
            self._refuse_files(message, COMMAND_WITH_FILES_REFUSAL, transport=transport)
            return

        persisted = self._stored_message(message)
        try:
            attachment_ids, attachments = self._inbound_attachments(message, persisted)
        except FileUnavailable:
            self._refuse_files(message, UNAVAILABLE_FILES_REFUSAL, transport=transport)
            return
        if not text and attachment_ids:
            text = "(file attached)"
        if not text:
            return

        session_id = self._session_id
        aef_tracing.set_session_id(session_id)
        with aef_tracing.input_request(
            headers=aef_tracing.with_trace_header({}),
            body=aef_tracing.mask_trace_payload({"source": TRANSPORT_NAME}),
            path="/dialogs",
            method="POST",
        ) as input_span:
            try:
                self._commit_inbound(
                    message,
                    text=text,
                    transport=transport,
                    attachment_ids=attachment_ids,
                    attachments=attachments,
                    persisted=persisted,
                    session_id=session_id,
                )
            except Exception:
                aef_tracing.add_response(input_span, http_code=500)
                raise
            aef_tracing.add_response(input_span, http_code=202)

    def _commit_inbound(
        self,
        message: InboundMessage,
        *,
        text: str,
        transport: dict[str, Any],
        attachment_ids: list[str],
        attachments: list[AttachmentMetadata],
        persisted: ConversationMessage | None,
        session_id: str,
    ) -> None:
        source_message_id = str(message.source_message_id or "")
        sender_session_id = message.sender_session_id

        scope = self._scope_for_chat(message.chat_id)
        if persisted is not None and bool((persisted.metadata or {}).get("inbound_processed")):
            return
        if persisted is None:
            extra_metadata: dict[str, Any] = dict(UNPROCESSED_METADATA)
            if message.history_only:
                extra_metadata["inbound_processed"] = True
                extra_metadata["history_only"] = True
            if attachments:
                extra_metadata["attachments"] = attachments
            persisted = append_chat_message(
                self.drive_root,
                scope=scope,
                actor_type="owner",
                chat_id=message.chat_id,
                user_id=message.user_id,
                text=text,
                session_id=session_id,
                # The moment the user spoke, not the moment we got round to it:
                # a replayed backlog would otherwise land in history out of order.
                ts=str(message.ts or "") or None,
                source=TRANSPORT_NAME,
                sender_label=message.sender_label,
                sender_session_id=sender_session_id,
                client_message_id=source_message_id,
                transport=transport,
                extra_metadata=extra_metadata,
            )
            if persisted is None:
                # Losing the insert race is not a failure: the row exists under
                # the same client id, so re-read it and carry on.
                persisted = self._stored_message(message)
                if persisted is None:
                    raise RuntimeError("inbound message insert reported no row and none exists")
                if bool((persisted.metadata or {}).get("inbound_processed")):
                    return

        message_id = persisted.message_id if persisted is not None else ""
        if message.history_only:
            # Replayed history is persisted, never submitted: it is this
            # transport catching up, not the user speaking.
            return

        self.gateway.inbox.submit(
            InboundEnvelope(
                scope=scope,
                user_id=message.user_id,
                text=text,
                sender_label=message.sender_label,
                sender_session_id=sender_session_id,
                client_message_id=source_message_id,
                trace_id=aef_tracing.current_trace_id(),
                transport=transport,
                attachment_ids=list(attachment_ids),
                attachments=list(attachments),
            )
        )
        for attachment in attachments:
            log.info(
                "event=inbound_attachment_bound message_id=%s filename=%s size=%s source=%s",
                source_message_id,
                pathlib.PurePosixPath(str(attachment.get("filename") or "attachment")).name,
                attachment.get("size", 0),
                TRANSPORT_NAME,
            )
        if message_id:
            mark_processed(self.drive_root, message_id)

    def _stored_message(self, message: InboundMessage) -> ConversationMessage | None:
        return get_chat_message_by_client_id(
            self.drive_root,
            scope=self._scope_for_chat(message.chat_id),
            actor_type="owner",
            source=TRANSPORT_NAME,
            chat_id=message.chat_id,
            user_id=message.user_id,
            client_message_id=str(message.source_message_id or ""),
            sender_session_id=message.sender_session_id,
        )

    def _scope_for_chat(self, chat_id: Any) -> OwnerScope:
        """The scope this transport mints for one chat — the only place it is built."""
        return dialogs_scope(str(chat_id))

    def _inbound_attachments(
        self,
        message: InboundMessage,
        persisted: ConversationMessage | None,
    ) -> tuple[list[str], list[AttachmentMetadata]]:
        """Reuse what a previous attempt stored, or store the temp files now."""
        if persisted is not None:
            attachments, attachment_ids = stored_attachments(persisted)
            if attachments:
                return attachment_ids, attachments
        if not message.attachments:
            return [], []
        scope = self._scope_for_chat(message.chat_id)
        client_message_id = str(message.source_message_id or uuid.uuid4().hex)
        stored_ids: list[str] = []
        stored: list[AttachmentMetadata] = []
        for index, inbound in enumerate(message.attachments):
            attachment_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                json.dumps([scope.owner_id, scope.agent_id, scope.chat_id, client_message_id, index]),
            ).hex
            with pathlib.Path(inbound.temp_path).open("rb") as fileobj:
                metadata = store_attachment_fileobj(
                    scope,
                    self._storage,
                    attachment_id=attachment_id,
                    filename=inbound.filename,
                    mime=inbound.mime,
                    fileobj=fileobj,
                    size=pathlib.Path(inbound.temp_path).stat().st_size,
                    source=TRANSPORT_NAME,
                    status="draft",
                )
            stored_ids.append(attachment_id)
            stored.append(metadata)
        return stored_ids, stored

    def _refuse_files(
        self,
        message: InboundMessage,
        reason: str,
        *,
        transport: dict[str, Any],
    ) -> None:
        """Keep an undelivered refusal retryable."""
        filenames = ", ".join(item.filename for item in message.attachments)
        ok, error = self._send_text(
            message.chat_id,
            f"{reason}{filenames}",
            transport=transport,
        )
        if not ok:
            log.warning("Dialogs refusal undelivered chat_id=%s error=%s", message.chat_id, error)
            raise RuntimeError("failed to deliver the attachment refusal")

    def resubmit_unprocessed(self, *, limit: int = DEFAULT_SWEEP_LIMIT) -> int:
        """Submit again every message persisted but never handed over."""
        return replay_unprocessed(
            self.gateway,
            self.drive_root,
            source=TRANSPORT_NAME,
            build=_DialogsEnvelopeBuilder(),
            limit=limit,
        )

    # -- outbound ---------------------------------------------------------

    def _read_outbox(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            run_reader_loop(
                reader,
                self._stop_event,
                self.handle_outbound,
                name="Dialogs",
                poll_timeout_sec=_OUTBOX_POLL_TIMEOUT_SEC,
            )
        finally:
            # Only when the loop gave up on its own: the subscription is durable,
            # so leaving it open with nothing draining it grows it to the drop
            # limit. A requested stop leaves the close to `close`, which joins first.
            if not self._stop_event.is_set():
                with suppress(Exception):
                    reader.close()

    def handle_outbound(self, reader: IOutboxReader, delivered: DeliveredMessage) -> None:
        message = delivered.message
        if not self._is_mine(message):
            reader.ack(delivered)
            return
        with _delivery_operation(message):
            return self._deliver_settled(reader, delivered)

    def _deliver_settled(self, reader: IOutboxReader, delivered: DeliveredMessage) -> None:
        message = delivered.message
        provider = self.ensure_provider()
        if provider is None:
            # No provider is a blip, a shutdown or a disabled feature — never
            # evidence about the peer, so the message keeps its place in the
            # durable subscription instead of being acknowledged away.
            return self._retry_delivery(reader, delivered, error="dialogs provider unavailable")
        if not _can_send(provider, message.chat_id):
            # A live provider that does not know this peer: nobody here can ever
            # deliver it, and retrying would stall the subscription.
            log.warning(
                "Dropping a dialogs message for an unreachable chat chat_id=%s kind=%s",
                message.chat_id,
                message.kind.value,
            )
            reader.ack(delivered)
            return
        try:
            ok, error = self._deliver(provider, message)
        except FileTransferPending:
            reader.defer(delivered, delay=_DELIVERY_RETRY_BACKOFF_SEC)
            return
        except FileUnavailable as exc:
            log.warning(
                "Dropping unresolved dialogs media chat_id=%s task_id=%s attachment_id=%s error=%s",
                message.chat_id,
                message.task_id,
                message.attachment.attachment_id if message.attachment else "",
                exc,
            )
            reader.ack(delivered)
            return
        except Exception:
            log.warning("Dialogs delivery raised for chat %s", message.chat_id, exc_info=True)
            ok, error = False, "delivery raised"
        if ok:
            reader.ack(delivered)
            return
        return self._retry_delivery(reader, delivered, error=error)

    def _retry_delivery(
        self,
        reader: IOutboxReader,
        delivered: DeliveredMessage,
        *,
        error: str,
    ) -> None:
        """Hand the message back for another attempt, or drop it on the record."""
        message = delivered.message
        attempts = delivered.attempts + 1
        if attempts >= _DELIVERY_ATTEMPT_LIMIT:
            attachment = message.attachment
            log.error(
                "Giving up on a dialogs message after %d attempts chat_id=%s kind=%s "
                "task_id=%s attachment_id=%s error=%s",
                attempts,
                message.chat_id,
                message.kind.value,
                message.task_id,
                attachment.attachment_id if attachment is not None else "",
                error,
            )
            reader.ack(delivered)
            return
        log.warning("Dialogs delivery failed chat_id=%s error=%s; retrying", message.chat_id, error)
        self._stop_event.wait(_DELIVERY_RETRY_BACKOFF_SEC)
        reader.nack(delivered)

    def _is_mine(self, message: OutboundMessage) -> bool:
        # `sberchat` is the older spelling of this transport, still carried by stored
        # task records, so `is_dialogs_source` is what decides what is ours to deliver.
        return is_dialogs_source(message.source)

    def _deliver(self, provider: DialogsProvider, message: OutboundMessage) -> tuple[bool, str]:
        if message.kind is OutboundKind.ACTION:
            send_typing = getattr(provider, "send_typing", None)
            if callable(send_typing):
                send_typing(message.chat_id, message.action or "typing")
            return True, "ok"
        if message.kind is OutboundKind.TEXT:
            return self._send_text(
                message.chat_id,
                message.text,
                transport=message.transport,
                fmt="markdown" if message.markdown else "",
                is_progress=message.is_progress,
                task_id=message.task_id,
                provider=provider,
            )
        return self._send_media(provider, message)

    def _send_text(
        self,
        chat_id: Any,
        text: str,
        *,
        transport: dict[str, Any] | None = None,
        fmt: str = "",
        is_progress: bool = False,
        task_id: str = "",
        provider: DialogsProvider | None = None,
    ) -> tuple[bool, str]:
        provider = provider if provider is not None else self.ensure_provider()
        if provider is None:
            return False, "dialogs transport disabled"
        options = self._reply_options(transport)
        if self.settings.DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED:
            options.update(is_progress=is_progress, task_id=task_id)
        return provider.send_text(chat_id, text, fmt=fmt, **options)

    def _send_media(self, provider: DialogsProvider, message: OutboundMessage) -> tuple[bool, str]:
        """Media travels by reference; the bytes are fetched here, at delivery."""
        reference = message.attachment
        if reference is None or not reference.attachment_id:
            raise FileUnavailable("outbound media has no attachment reference")
        options = self._reply_options(message.transport)
        send = provider.send_image if message.kind is OutboundKind.PHOTO else provider.send_file
        if message.kind is not OutboundKind.PHOTO:
            options["task_id"] = message.task_id
        result = send(
            message.chat_id,
            StoredFile(reference.attachment_id),
            filename=None,
            mime=None,
            caption=message.caption,
            scope=message.scope,
            **options,
        )
        if result == (False, DELIVERY_UNKNOWN):
            raise FileUnavailable("media delivery deadline expired")
        return result

    def _reply_options(self, transport: dict[str, Any] | None) -> dict[str, Any]:
        metadata = dict(transport or {})
        options: dict[str, Any] = {}
        source_message_id = str(metadata.get("source_message_id") or "").strip()
        if self.settings.DIALOGS_REPLY_ENABLED and source_message_id:
            options["reply_to"] = [source_message_id]
        target_uid = metadata.get("target_uid")
        if self.settings.DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED and target_uid not in (None, ""):
            options["target_uid"] = target_uid
        return options


def _delivery_operation(message: OutboundMessage) -> AbstractContextManager[Any]:
    """The operation this delivery belongs to, re-established on the reader thread.

    The outbox is a boundary, so the identifier crossed it as data — stamped on the message
    when it was published (`supervisor/message_bus.py`) — and is picked back up here, where the
    reader thread has a context of its own and inherits nothing. Without this every SDK call the
    delivery makes, and every line it logs, would run under no operation: the answer to a task
    would reach the chat uncorrelated with the task that wrote it.

    A message that names no operation is delivered outside one. Minting here would put an
    identifier on the wire that names nothing an operator could follow back, which is worse than
    an absent one because it looks like a correlation.
    """
    return aef_tracing.trace_scope(message.trace_id) if message.trace_id else nullcontext()


def _can_send(provider: DialogsProvider, chat_id: Any) -> bool:
    can_send = getattr(provider, "can_send_to_chat_id", None)
    if not callable(can_send):
        return True
    try:
        return bool(can_send(chat_id))
    except Exception:
        log.debug("Dialogs peer lookup failed for chat %s", chat_id, exc_info=True)
        return False


class _DialogsEnvelopeBuilder(IEnvelopeBuilder):
    """The one part of the journal sweep that is this transport's own."""

    def build(self, record: ConversationMessage) -> InboundEnvelope:
        metadata = record.metadata or {}
        attachments, attachment_ids = stored_attachments(record)
        transport = metadata.get("transport")
        user_id = metadata.get("user_id")
        return InboundEnvelope(
            scope=OwnerScope(
                owner_id=record.owner_id,
                agent_id=record.agent_id,
                surface_id=SurfaceId(TRANSPORT_NAME),
                chat_id=str(record.chat_id),
            ),
            user_id=record.chat_id if user_id is None else user_id,
            text=record.content,
            sender_label=str(metadata.get("sender_label") or ""),
            sender_session_id=str(metadata.get("sender_session_id") or ""),
            client_message_id=str(record.source_message_id or ""),
            transport=dict(transport) if isinstance(transport, dict) else {},
            attachment_ids=attachment_ids,
            attachments=attachments,
        )


def _delete_temp_files(message: InboundMessage) -> None:
    """Release what the message staged. Tied to acknowledgement, not to receipt."""
    for attachment in message.attachments:
        pathlib.Path(attachment.temp_path).unlink(missing_ok=True)


def build_dialogs_transport(
    *,
    settings: AppSettings,
    gateway: IMessageGateway,
    storage: StorageProvider,
    drive_root: pathlib.Path,
    session_id: str,
    provider_factory: Callable[[AppSettings], DialogsProvider],
    cursor_repository: IDialogsCursorRepository,
) -> DialogsTransport:
    """The composition root's entry point; returns a transport, started or not."""
    transport = DialogsTransport(
        settings=settings,
        gateway=gateway,
        storage=storage,
        drive_root=drive_root,
        session_id=session_id,
        provider_factory=provider_factory,
        cursor_repository=cursor_repository,
    )
    transport.ensure_provider()
    return transport


__all__ = [
    "TRANSPORT_NAME",
    "DialogsTransport",
    "build_dialogs_transport",
    "dialogs_feature_enabled",
]
