"""Queue-based bridge between ingress transports and the supervisor."""

from __future__ import annotations

import logging
import pathlib
import re
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from background_memory.infra.events import announce_chat_message_persisted, memory_event_publisher_factory
from new_core.identity import OwnerScope
from new_core.tools.interfaces import ToolOutbound
from ouroboros.contracts.attachments import AttachmentMetadata, PublicAttachment, StoredFile, attachment_status
from ouroboros.contracts.chat_id_policy import is_internal_chat_id
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_bus import CHAT_OUTBOUND, CHAT_PHOTO, CHAT_TYPING, publish_event
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import get_attachment
from ouroboros.storage.chat_store import append_chat_message, get_chat_message_by_client_id
from ouroboros.storage.runtime_backend import store_deadline
from ouroboros.storage.runtime_models import ActorType, ConversationMessage, TaskRecord
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.utils import emit_ui_log, utc_now_iso
from security_config.attachments import IMAGE_SUFFIXES
from supervisor.runtime_context import require_runtime
from supervisor.state import get_state_repository
from transport.interfaces import AttachmentRef, IOutbox, OutboundKind, OutboundMessage, routed_chat_id

log = logging.getLogger(__name__)


def _try_drive_root() -> pathlib.Path | None:
    from supervisor.runtime_context import try_get_runtime

    runtime = try_get_runtime()
    return pathlib.Path(runtime.drive_root) if runtime is not None else None


def get_bridge() -> LocalChatBridge:

    bridge = require_runtime().bridge
    if bridge is None:
        raise RuntimeError("Supervisor bridge is not initialized")
    return bridge


class LocalChatBridge(ToolOutbound):
    """The publish boundary for everything bound for a user, whoever produced it.

    Not a transport: it delivers nothing. Each method persists what belongs in
    history, puts one ``OutboundMessage`` on the outbox, and mirrors the event
    onto the observability bus. Which channel the message reaches is decided by
    whichever transport reads it.

    The outbox and the drive root arrive through the constructor, from the
    composition root that owns the gateway: the bridge names one half of the seam
    and never reaches for a runtime to find either. The same object is handed to
    each run as its ``ToolOutbound``, which is the half of it a tool is allowed to
    see — and a boundary that resolved anything from a process-local registry
    would be a boundary that starts failing on the day the run moves out.
    """

    def __init__(self, outbox: IOutbox, drive_root: pathlib.Path | None = None):
        self._outbox = outbox
        #: Where an outbound attachment is persisted before its reference is published.
        #: ``None`` for a bridge built without one, which then publishes without history.
        self._drive_root = pathlib.Path(drive_root) if drive_root is not None else None

    def _publish(self, message: OutboundMessage, before_publish: Callable[[], float | None] | None = None) -> bool:
        """Put one supervisor-produced message on the outbox; True when it took it.

        A publish that fails is logged rather than raised — an undelivered reply
        must not take down the turn that produced it — and the caller decides
        what to report. At error level, because publishing is all-or-nothing: a
        failure here is not a degraded delivery but a message no transport will
        ever see.
        """

        deadline = time.monotonic() + 5

        def budget() -> float:
            remaining = before_publish() if before_publish is not None else None
            remaining = min(
                remaining if remaining is not None else float("inf"),
                min(store_deadline.get() or deadline, deadline) - time.monotonic(),
            )
            if remaining <= 0:
                raise TimeoutError("outbound publication budget exhausted")
            return remaining

        try:
            if before_publish is not None:
                before_publish()
            self._outbox.publish(message, before_publish=budget if before_publish is not None else None)
        except Exception:
            log.error("Failed to publish outbound message for chat %s", message.chat_id, exc_info=True)
            return False
        return True

    def send_message(
        self,
        text: str,
        parse_mode: str = "",
        ts: str | None = None,
        is_progress: bool = False,
        task_id: str = "",
        source: str = "",
        trace_id: str = "",
        transport: dict[str, Any] | None = None,
        *,
        scope: OwnerScope,
        message_id: str = "",
        before_publish: Callable[[], float | None] | None = None,
    ) -> tuple[bool, str]:
        """Publish one text message and mirror it onto the observability bus.

        The chat comes from ``scope`` and from nowhere else: a chat id beside a scope is one
        fact carried twice, and two parameters can name different conversations. ``source``
        stays its own argument — it is the delivery routing key, a different question.
        """
        chat_id = routed_chat_id(scope)
        source = _require_source(source)
        clean_text = self._strip_markdown(text) if not parse_mode else text
        message_ts = ts or utc_now_iso()
        outbound_transport = dict(transport or {})
        message_trace_id = str(trace_id or aef_tracing.current_trace_id())
        published = self._publish(
            OutboundMessage(
                scope=scope,
                message_id=message_id,
                source=source,
                text=clean_text,
                is_progress=bool(is_progress),
                markdown=bool(parse_mode),
                task_id=str(task_id or ""),
                trace_id=message_trace_id,
                correlation_id=str(outbound_transport.get("correlation_id") or ""),
                ts=message_ts,
                transport=outbound_transport,
            ),
            before_publish,
        )
        if not published:
            # No observability mirror either: that event is read as evidence the
            # message was sent. A history row already written stays — the agent
            # did say it, and the next turn's context reads that row.
            return False, "failed to publish message"
        if not is_internal_chat_id(chat_id):
            publish_event(
                CHAT_OUTBOUND,
                {
                    "chat_id": chat_id,
                    "text": clean_text,
                    "markdown": bool(parse_mode),
                    "is_progress": bool(is_progress),
                    "ts": message_ts,
                    "task_id": str(task_id or ""),
                    "source": source,
                    "transport": outbound_transport,
                    "trace_id": message_trace_id,
                },
            )
        return True, "ok"

    def send_chat_action(
        self,
        scope: OwnerScope,
        action: str = "typing",
        *,
        source: str = "",
    ) -> bool:
        """Publish a typing indicator for the conversation ``scope`` names.

        Takes the scope rather than a bare chat for the same reason the other publishes
        do: a transport holding two connections has to know whose indicator this is.
        """
        source = _require_source(source)
        chat_id = routed_chat_id(scope)
        if is_internal_chat_id(chat_id):
            return True
        if not self._publish(
            OutboundMessage(
                scope=scope,
                source=source,
                kind=OutboundKind.ACTION,
                action=str(action or ""),
            )
        ):
            return False
        publish_event(
            CHAT_TYPING,
            {
                "chat_id": chat_id,
                "action": str(action or ""),
                "source": source,
                "transport": {},
            },
        )
        return True

    def send_image(
        self,
        media: StoredFile,
        caption: str = "",
        mime: str = "image/png",
        source: str = "",
        filename: str = "",
        transport: dict[str, Any] | None = None,
        *,
        scope: OwnerScope,
    ) -> tuple[bool, str]:
        """Publish a saved image reference and mirror its event."""
        chat_id = routed_chat_id(scope)
        source = _require_source(source)
        if is_internal_chat_id(chat_id):
            return True, "ok"
        if not filename:
            suffix = IMAGE_SUFFIXES.get(str(mime or "").lower(), ".png")
            filename = f"image{suffix}"
        photo_transport = dict(transport or {})
        message_ts = utc_now_iso()
        published, detail = self.send_file(
            media,
            scope=scope,
            filename=filename,
            mime=mime,
            caption=caption,
            source=source,
            transport=photo_transport,
            kind=OutboundKind.PHOTO,
            ts=message_ts,
        )
        if not published:
            return False, detail
        publish_event(
            CHAT_PHOTO,
            {
                "chat_id": chat_id,
                "transport": photo_transport,
                "caption": str(caption or ""),
                "image_base64": "",
                "attachment": {"id": media.file_id},
                "owner_id": scope.owner_id,
                "agent_id": scope.agent_id,
                "mime": str(mime or ""),
                "source": source,
                "ts": message_ts,
            },
        )
        return True, "queued"

    def send_file(
        self,
        media: StoredFile,
        *,
        scope: OwnerScope,
        filename: str,
        mime: str = "application/octet-stream",
        caption: str = "",
        source: str = "",
        transport: dict[str, Any] | None = None,
        task_id: str = "",
        kind: OutboundKind = OutboundKind.FILE,
        ts: str | None = None,
    ) -> tuple[bool, str]:
        """Persist history, then publish a stored file reference; report acceptance."""
        chat_id = routed_chat_id(scope)
        source = _require_source(source)
        if is_internal_chat_id(chat_id):
            return True, "ok"
        attachment = self._persist_outbound_attachment(
            scope=scope,
            data=media,
            caption=caption,
            source=source,
            transport=transport,
        )
        if attachment is None:
            return False, "failed to persist attachment"
        published = self._publish(
            OutboundMessage(
                scope=scope,
                source=source,
                kind=kind,
                caption=str(caption or ""),
                task_id=str(task_id or ""),
                trace_id=aef_tracing.current_trace_id(),
                ts=ts or utc_now_iso(),
                transport=dict(transport or {}),
                attachment=attachment,
            )
        )
        if not published:
            return False, "failed to publish attachment"
        return True, "queued"

    def send_terminal_attachments(self, record: TaskRecord, before_publish: Callable[[], None]) -> bool:
        scope = OwnerScope.from_dict(record.input.get("scope") or record.stored_scope())
        delivery_repository = runtime_persistence.current.result_repo
        delivery = record.terminal_delivery
        complete = True
        for item in record.terminal_attachments:
            key = item["id"]
            if delivery.get(key) in {"accepted", "unavailable"}:
                continue
            disposition = "retryable"
            try:
                before_publish()
                provider = get_storage_provider()
                attachment = get_attachment(scope, key)
                if attachment is None or attachment_status(attachment) in {
                    "expired",
                    "failed",
                    "missing",
                    "deleting",
                    "deleted",
                }:
                    disposition = "unavailable"
                elif attachment_status(attachment) == "ready":
                    before_publish()
                    if not provider.exists(scope, SCOPE_UPLOADS, attachment["storage_key"]):
                        disposition = "unavailable"
                    elif is_internal_chat_id(routed_chat_id(scope)) or self._publish(
                        OutboundMessage(
                            scope=scope,
                            source=record.source,
                            task_id=record.job_id,
                            message_id=f"durable-job-terminal:{record.owner_id}:{record.job_id}:attachment:{key}",
                            kind=OutboundKind.PHOTO if item["kind"] == "image" else OutboundKind.FILE,
                            attachment=self._attachment_ref(item),
                            caption=item.get("caption", ""),
                            trace_id=aef_tracing.current_trace_id(),
                            ts=record.finished_at,
                            transport=dict(record.input.get("transport") or {}),
                        ),
                        before_publish,
                    ):
                        disposition = "accepted"
            except FileNotFoundError:
                disposition = "unavailable"
            except Exception:
                log.warning("Terminal attachment handoff failed for %s", key, exc_info=True)
            before_publish()
            if not delivery_repository.record_terminal_delivery(
                record.task_id, claim=str(record.terminal_publish_claim or ""), item=key, disposition=disposition
            ):
                return False
            complete = complete and disposition != "retryable"
        return complete

    def push_log(self, event: dict[str, Any]) -> None:
        """Send one runtime log event to whichever UI is watching.

        The same sink `record_event` writes to, so a task heartbeat and a
        recorded event reach the browser by one path instead of two.
        """
        emit_ui_log({"type": "log", "data": event})

    def _persist_outbound_attachment(
        self,
        *,
        scope: OwnerScope,
        data: StoredFile,
        caption: str,
        source: str,
        transport: dict[str, Any] | None,
    ) -> AttachmentRef | None:
        """Link a ready file to its outbound history row."""
        drive_root = self._drive_root
        if drive_root is None:
            return None
        chat_id = routed_chat_id(scope)
        attachment_id = data.file_id
        message_id = uuid.uuid4().hex
        try:
            metadata = get_attachment(scope, attachment_id)
            if metadata is None or attachment_status(metadata) != "ready":
                return None
            persisted = append_chat_message(
                drive_root,
                scope=scope,
                actor_type="agent",
                chat_id=chat_id,
                user_id=scope.owner_id,
                text=caption,
                session_id=_owner_session_id(scope),
                source=source,
                client_message_id=message_id,
                message_type="attachment",
                transport=transport,
                extra_metadata={"attachments": [metadata]},
            )
            if persisted is None:
                raise RuntimeError("outbound attachment message insert conflict")
            return self._attachment_ref(next(item for item in persisted.attachments if item["id"] == attachment_id))
        except Exception:
            log.warning("Failed to persist outbound attachment", exc_info=True)
            return None

    @staticmethod
    def _attachment_ref(attachment: PublicAttachment | AttachmentMetadata | None) -> AttachmentRef | None:
        if not attachment:
            return None
        return AttachmentRef(
            attachment_id=str(attachment.get("id") or ""),
            filename=str(attachment.get("filename") or ""),
            mime=str(attachment.get("mime") or ""),
            size=int(attachment.get("size") or 0),
        )

    @staticmethod
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


def log_chat(
    actor_type: ActorType,
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
    *,
    scope: OwnerScope,
    extra_metadata: dict[str, Any] | None = None,
) -> ConversationMessage | None:
    """Persist one chat message and return it, or ``None`` when nothing was written.

    The chat is read off ``scope``: the row's identity and the conversation its metadata
    names are one fact, so they come from one value.
    """
    chat_id = routed_chat_id(scope)
    drive_root = _try_drive_root()
    if drive_root is None:
        return None
    try:
        session_id = _owner_session_id(scope)
        persisted = append_chat_message(
            drive_root,
            scope=scope,
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
            extra_metadata=extra_metadata,
        )
        if persisted is None and client_message_id:
            persisted = get_chat_message_by_client_id(
                drive_root,
                scope=scope,
                actor_type=actor_type,
                source=source,
                chat_id=chat_id,
                user_id=user_id,
                client_message_id=client_message_id,
                sender_session_id=sender_session_id,
            )
        if persisted is not None and not persisted.metadata.get("delivery_only"):
            # A module function has no collaborator to be handed, which is the one case
            # the factory exists for. The scope is this function's own parameter — the
            # same value the write used, passed through rather than minted again.
            announce_chat_message_persisted(
                memory_event_publisher_factory.memory_publisher, scope, str(persisted.message_id or "")
            )
        return persisted
    except Exception:
        # Losing a chat row silently is how history quietly stops matching what happened.
        log.warning("Failed to persist chat message", exc_info=True)
        return None


def _require_source(source: str) -> str:
    """The publish boundary refuses a message with no source.

    Delivery is decided by the reading transport, so defaulting an absent source
    silently routes the message to the wrong channel — or to none at all.
    """
    text = str(source or "").strip()
    if not text:
        raise ValueError("outbound message requires an explicit source")
    return text


def _owner_session_id(scope: OwnerScope) -> str:
    """The session id of the owner this message belongs to.

    Read from that owner's own runtime state row, never from process state: the row used to
    be one per deployment, so every message an instance wrote carried whichever session the
    last owner to speak had. The scope is already required at this door and already decides
    the chat; the session comes from the same place.
    """

    try:
        return str(get_state_repository().for_scope(scope).session_id() or "")
    except Exception:
        log.debug("Failed to read the session id for %s", scope.owner_id, exc_info=True)
        return ""


def send_with_budget(
    text: str,
    log_text: str | None = None,
    fmt: str = "",
    is_progress: bool = False,
    task_id: str = "",
    ts: str | None = None,
    *,
    scope: OwnerScope,
    source: str,
    trace_id: str = "",
    transport: dict[str, Any] | None = None,
    client_message_id: str = "",
    publication: TaskRecord | None = None,
    before_publish: Callable[[], None] | None = None,
    persist_only: bool = False,
    repeat_accepted: bool = False,
    delivery_only: bool = False,
) -> bool:
    """Publish one outbound message.

    ``scope`` and ``source`` are both keyword-only and both required, and they answer
    different questions: the scope says whose conversation this message belongs to — the
    chat included — while the source says which transport carries it. A message with no
    source reaches nobody or everybody; a message with no scope would be stored under
    whichever owner the process happens to be configured with.
    """
    if before_publish is not None:
        before_publish()
    if publication is not None and publication.terminal_delivery.get("text") == "accepted":
        return True
    message_handoff = publication is None or publication.status == "rejected_duplicate"
    chat_id = routed_chat_id(scope)
    source = _require_source(source)
    with aef_tracing.trace_scope(trace_id):
        # Identity comes from the message, not from the process: the owner is the scope's,
        # and the session is that owner's own.
        session_id = _owner_session_id(scope)
        aef_tracing.set_session_id(session_id)
        owner_id = scope.owner_id
        _text = str(text or "")
        msg_ts = ts or utc_now_iso()

        drive_root = _try_drive_root()
        persisted = None
        if is_progress and drive_root is not None:
            try:
                append_chat_message(
                    drive_root,
                    scope=scope,
                    actor_type="agent",
                    chat_id=chat_id,
                    user_id=owner_id,
                    text=text if log_text is None else log_text,
                    session_id=session_id,
                    ts=msg_ts,
                    fmt=fmt,
                    source=source,
                    task_id=task_id,
                    is_progress=True,
                    transport=transport,
                )
            except Exception:
                log.debug("Failed to persist progress chat message", exc_info=True)
        else:
            persisted = log_chat(
                "agent",
                owner_id,
                text if log_text is None else log_text,
                ts=msg_ts,
                fmt=fmt,
                source=source,
                task_id=task_id,
                client_message_id=client_message_id,
                transport=transport,
                scope=scope,
                extra_metadata={
                    **({"delivery_only": True} if delivery_only else {}),
                    **(
                        {
                            "attachments": publication.terminal_attachments,
                            "terminal_publication": True,
                        }
                        if publication
                        else {}
                    ),
                    **(
                        {
                            "handoff_scope": scope.to_dict(),
                            "trace_id": aef_tracing.current_trace_id(),
                        }
                        if message_handoff and client_message_id
                        else {}
                    ),
                },
            )
            if (
                persisted is not None
                and message_handoff
                and client_message_id
                and "handoff_scope" not in persisted.metadata
            ):
                metadata = {
                    "handoff_scope": scope.to_dict(),
                    "trace_id": aef_tracing.current_trace_id(),
                    **persisted.metadata,
                }
                if not runtime_persistence.current.conversations_repo.update_message_metadata(
                    persisted.message_id, metadata, expected_metadata=persisted.metadata
                ):
                    return False
                persisted.metadata = metadata
            if persisted is not None and message_handoff and client_message_id and repeat_accepted:
                metadata = {
                    **persisted.metadata,
                    # A prior publisher must not acknowledge this new delivery request.
                    "handoff_claim": uuid.uuid4().hex,
                    "handoff_accepted": False,
                    "handoff_until": 0,
                    "transport": dict(transport or {}),
                }
                if not runtime_persistence.current.conversations_repo.update_message_metadata(
                    persisted.message_id, metadata, expected_metadata=persisted.metadata
                ):
                    return False
                persisted.metadata = metadata
            if persist_only:
                return persisted is not None and bool(client_message_id)
            if client_message_id:
                if persisted is None:
                    if publication is not None:
                        raise RuntimeError("terminal history unavailable")
                    return False
                if message_handoff:
                    persisted = runtime_persistence.current.conversations_repo.claim_message_handoff(
                        persisted.message_id, scope=scope, claim=uuid.uuid4().hex
                    )
                if persisted is None:
                    return False
                if persisted.metadata.get("handoff_accepted") and publication is None and not repeat_accepted:
                    return True
                _text = persisted.content if repeat_accepted or not persisted.metadata.get("handoff_accepted") else ""
                msg_ts = persisted.created_at
                transport = (
                    dict(transport or {}) if repeat_accepted else dict(persisted.metadata.get("transport") or {})
                )

        dispatch_guard: Callable[[], float | None] | None = before_publish
        if (
            message_handoff
            and client_message_id
            and persisted is not None
            and not persisted.metadata.get("handoff_accepted")
            and _text
        ):

            def handoff_guard() -> float:
                if before_publish is not None:
                    before_publish()
                remaining = runtime_persistence.current.conversations_repo.message_handoff_remaining(
                    persisted.message_id, scope=scope, claim=str(persisted.metadata.get("handoff_claim") or "")
                )
                if remaining <= 0:
                    raise TimeoutError("message handoff authority lost")
                return remaining

            dispatch_guard = handoff_guard

        span = (
            aef_tracing.output_request(
                headers=aef_tracing.with_trace_header({}),
                body=aef_tracing.mask_trace_payload({"chat_id": chat_id, "task_id": task_id}),
                path="/dialogs/sendMessage",
                method="POST",
            )
            if not is_progress and not is_internal_chat_id(chat_id)
            else nullcontext()
        )
        with span as output_span:
            try:
                ok, _err = (
                    (True, "")
                    if _text.strip() in ("", "\u200b")
                    else get_bridge().send_message(
                        _text,
                        scope=scope,
                        parse_mode="markdown" if fmt == "markdown" else "",
                        ts=msg_ts,
                        is_progress=is_progress,
                        task_id=task_id,
                        source=source,
                        trace_id=aef_tracing.current_trace_id(),
                        transport=transport,
                        message_id=persisted.message_id if client_message_id and persisted else "",
                        before_publish=dispatch_guard,
                    )
                )
            except Exception:
                aef_tracing.add_response(output_span, http_code=500)
                raise
            aef_tracing.add_response(output_span, http_code=200 if ok else 500)
        if (
            message_handoff
            and client_message_id
            and persisted is not None
            and not persisted.metadata.get("handoff_accepted")
        ):
            confirmed = runtime_persistence.current.conversations_repo.update_message_metadata(
                persisted.message_id,
                {**persisted.metadata, "handoff_accepted": bool(ok), "handoff_until": 0},
                expected_metadata=persisted.metadata,
            )
            ok = bool(ok and confirmed)
        if publication is not None:
            if before_publish is not None:
                before_publish()
            return runtime_persistence.current.result_repo.record_terminal_delivery(
                publication.task_id,
                claim=str(publication.terminal_publish_claim or ""),
                item="text",
                disposition="accepted" if ok else "retryable",
            ) and bool(ok)
        return bool(ok)
