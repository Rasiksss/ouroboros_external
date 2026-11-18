from __future__ import annotations

import logging
import queue
import tempfile
import threading
import time
from contextvars import copy_context
from pathlib import Path
from typing import Any

from new_core.identity import OwnerScope
from ouroboros.contracts.attachments import StoredFile
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.observability import aef_tracing
from ouroboros.storage.attachment_validation import attachment_max_bytes
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.protocol import ObjectTooLargeError
from ouroboros.storage.chat_attachments import get_attachment
from ouroboros.storage.file_catalog import FileFailure, FileTransferPending, FileUnavailable
from ouroboros.storage.file_transfers import get_transfer_limits
from security_config.attachments import IMAGE_SUFFIXES
from transport.dialogs.state import (
    DialogsState,
    outbound_http_headers,
    outbound_metadata,
    sdk_mid,
    serialize_mid,
)

log = logging.getLogger(__name__)

MAX_DIALOGS_MESSAGE_CHARS = 3800
_DIALOGS_MEDIA_SEND_ATTEMPTS = 2
_DIALOGS_OUTBOUND_TIMEOUT_SEC = 120.0


class _DialogsDeliveryUnknown(TimeoutError):
    """The SDK call outlived our wait, so the remote outcome is ambiguous."""


def _sdk_message_ids(values: list[Any]) -> list[Any]:
    try:
        # Optional SDK: headless deployments may not install Dialogs.
        from dialog_bot_sdk.entities.definitions import UUID

        result: list[Any] = []
        for value in values:
            if isinstance(value, UUID):
                result.append(value)
                continue
            msb, separator, lsb = str(value).partition(":")
            if not separator:
                raise ValueError("Dialogs reply message ID is not a serialized UUID")
            result.append(UUID(int(msb), int(lsb)))
        return result
    except (ImportError, TypeError, ValueError) as exc:
        log.warning("Dialogs reply unavailable; sending without reply: %s", exc)
        return []


def _split_dialogs_message(text: str, limit: int = MAX_DIALOGS_MESSAGE_CHARS) -> list[str]:
    clean_text = str(text or "")
    if len(clean_text) <= limit:
        return [clean_text]

    chunks: list[str] = []
    remaining = clean_text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = 0
        for separator in ("\n\n", "\n", ". ", "! ", "? ", "; ", ": ", " "):
            idx = window.rfind(separator)
            if idx > limit // 2:
                split_at = idx + len(separator)
                break
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    if remaining:
        chunks.append(remaining)
    return chunks


class DialogsOutbound(DialogsState):
    """Outbound Dialogs text, presence, and media delivery."""

    def _call_with_timeout(
        self,
        callback: Any,
        *args: Any,
        operation: str,
        on_finish: Any = None,
        slots: threading.BoundedSemaphore | None = None,
        timeout_sec: float | None = None,
        **kwargs: Any,
    ) -> Any:
        # A timed-out SDK call keeps running and holds its slot until it
        # returns, so callers whose work is optional (reply lookups) pass
        # their own small pool to keep hangs away from outbound capacity.
        if slots is None:
            slots = self._outbound_slots
        if timeout_sec is None:
            timeout_sec = _DIALOGS_OUTBOUND_TIMEOUT_SEC
        if not slots.acquire(blocking=False):
            if callable(on_finish):
                on_finish()
            raise TimeoutError(f"Dialogs {operation} capacity is exhausted")
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def _run() -> None:
            try:
                outcome = (True, callback(*args, **kwargs))
            except BaseException as exc:
                outcome = (False, exc)
            finally:
                if callable(on_finish):
                    try:
                        on_finish()
                    except Exception:
                        log.warning("Dialogs %s cleanup failed", operation, exc_info=True)
                slots.release()
                with self._incoming_lock:
                    self._outbound_workers.discard(threading.current_thread())
            result.put(outcome)

        # The thread exists only so the wait above can be bounded; the SDK call on it is the
        # caller's own work — a reply to a task's message, a lookup for the update being
        # handled — so it runs in the caller's context rather than starting bare. Without the
        # copy every call routed through here, and every log record it writes, loses the
        # operation: a thread does not inherit context variables, and `with_trace_header`
        # omits rather than raises, so nothing fails to announce it.
        worker = threading.Thread(target=copy_context().run, args=(_run,), name="dialogs-outbound", daemon=True)
        try:
            with self._incoming_lock:
                if self._updates_stop_event.is_set():
                    raise RuntimeError("Dialogs provider is stopping")
                self._outbound_workers.add(worker)
                worker.start()
        except Exception:
            with self._incoming_lock:
                self._outbound_workers.discard(worker)
            slots.release()
            if callable(on_finish):
                on_finish()
            raise
        try:
            ok, value = result.get(timeout=timeout_sec)
        except queue.Empty as exc:
            raise _DialogsDeliveryUnknown(f"Dialogs {operation} timed out") from exc
        if not ok:
            raise value
        return value

    def can_send_to_chat_id(self, chat_id: Any) -> bool:
        return self._resolve_out_peer(chat_id) is not None

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
        del fmt
        if self._bot is None:
            return False, "dialogs sdk bot not initialized"

        peer = self._resolve_out_peer(chat_id)
        if peer is None:
            return False, f"dialogs peer is not available for chat_id={chat_id!r}"

        clean_text = str(text or "")
        if not clean_text:
            return True, "empty"

        messaging = getattr(self._bot, "messaging", None)
        chunks = _split_dialogs_message(clean_text)
        progress_key = f"{chat_id}:{task_id}" if task_id else ""
        anchor_key = (
            progress_key
            if self.settings.DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED and is_progress and len(chunks) == 1
            else ""
        )
        if anchor_key:
            previous = self._progress_messages.get(anchor_key)
            update = getattr(messaging, "update_message_sync", None)
            if previous is not None and callable(update):
                try:
                    self._call_with_timeout(
                        update,
                        previous,
                        chunks[0],
                        operation="progress update",
                        **outbound_metadata(),
                    )
                    return True, self._response_id(previous)
                except TimeoutError as exc:
                    return False, str(exc)
                except Exception as exc:
                    self._log_progress_fallback(anchor_key, f"update failed exception_type={type(exc).__name__}")
        elif progress_key:
            self._forget_progress(progress_key)

        reply = getattr(messaging, "reply_sync", None)
        try:
            message_kwargs = self._text_kwargs(target_uid)
            reply_ids = (
                _sdk_message_ids(list(reply_to or [])) if self.settings.DIALOGS_REPLY_ENABLED and reply_to else []
            )
        except (ImportError, TypeError, ValueError) as exc:
            return False, str(exc)
        methods: tuple[tuple[str, Any], ...]
        if reply_ids and callable(reply):
            methods = (("reply_sync", reply),)
        else:
            methods = tuple(
                (method_name, method)
                for method_name in ("send_message_sync",)
                if callable(method := getattr(messaging, method_name, None))
            )
        for method_name, method in methods:
            message_ids: list[str] = []
            first_response: Any = None
            try:
                for index, chunk in enumerate(chunks):
                    if method_name == "reply_sync" and index == 0:
                        response = self._call_with_timeout(
                            method,
                            peer,
                            reply_ids,
                            chunk,
                            operation="message send",
                            **message_kwargs,
                        )
                    else:
                        send = getattr(messaging, "send_message_sync", None)
                        if not callable(send):
                            raise RuntimeError("dialogs sdk messaging send method is not available")
                        response = self._call_with_timeout(
                            send,
                            peer,
                            chunk,
                            operation="message send",
                            **message_kwargs,
                        )
                    if first_response is None:
                        first_response = response
                    message_id = self._remember_sent_message(response)
                    if message_id:
                        message_ids.append(message_id)
            except Exception as exc:
                log.warning("Dialogs SDK send failed method=%s error=%s", method_name, exc)
                return False, str(exc)
            if anchor_key:
                self._remember_progress_anchor(anchor_key, first_response)
            if message_ids:
                return True, ",".join(message_ids)
            return True, f"ok ({len(chunks)} parts)" if len(chunks) > 1 else "ok"

        return False, "dialogs sdk messaging send method is not available"

    def _remember_progress_anchor(self, anchor_key: str, response: Any) -> None:
        messaging = getattr(self._bot, "messaging", None)
        mid = sdk_mid(response)
        fetch = getattr(messaging, "get_message_by_id_sync", None)
        if not callable(getattr(messaging, "update_message_sync", None)):
            reason = "update_message_sync missing"
        elif mid is None:
            reason = f"send returned no message id response_type={type(response).__name__}"
        elif not callable(fetch):
            reason = "get_message_by_id_sync missing"
        else:
            try:
                anchor = self._call_with_timeout(
                    fetch,
                    mid,
                    operation="progress anchor fetch",
                    **outbound_metadata(),
                )
            except Exception as exc:
                reason = f"anchor fetch failed exception_type={type(exc).__name__}"
            else:
                if anchor is not None:
                    self._progress_messages[anchor_key] = anchor
                    return
                reason = "anchor fetch returned nothing"
        self._progress_messages.pop(anchor_key, None)
        self._log_progress_fallback(anchor_key, reason)

    def _log_progress_fallback(self, anchor_key: str, reason: str) -> None:
        if anchor_key in self._progress_fallbacks:
            return
        self._progress_fallbacks.add(anchor_key)
        log.warning(
            "Dialogs progress updates unavailable key=%s reason=%s; sending new messages",
            anchor_key,
            reason,
        )

    def _forget_progress(self, progress_key: str) -> None:
        self._progress_messages.pop(progress_key, None)
        self._progress_fallbacks.discard(progress_key)

    def _text_kwargs(self, target_uid: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.settings.DIALOGS_FORWARDING_DISABLED:
            kwargs["is_forward_banned"] = True
        if self.settings.DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED and target_uid not in (None, ""):
            kwargs["uid"] = int(target_uid)
        kwargs.update(outbound_metadata())
        return kwargs

    def send_typing(self, chat_id: Any, action: str = "typing") -> bool:
        if not self.settings.DIALOGS_TYPING_ENABLED or self._bot is None:
            return False
        peer = self._resolve_out_peer(chat_id)
        if peer is None:
            return False
        try:
            from dialog_bot_sdk.entities.presence import TypingType

            presence = getattr(self._bot, "presence", None)
            method_name = "stop_typing_sync" if action in {"cancel", "stop"} else "start_typing_sync"
            method = getattr(presence, method_name, None)
            if not callable(method):
                return False
            self._call_with_timeout(
                method,
                peer,
                TypingType.TYPINGTYPE_TEXT,
                operation="presence update",
                **outbound_metadata(),
            )
            return True
        except Exception as exc:
            log.warning("Dialogs SDK typing failed action=%s error=%s", action, exc)
            return False

    def send_image(
        self,
        chat_id: Any,
        media: StoredFile,
        *,
        filename: str | None,
        caption: str = "",
        mime: str | None = "image/png",
        reply_to: list[Any] | None = None,
        target_uid: Any = None,
        scope: OwnerScope,
    ) -> tuple[bool, str]:
        return self._send_media(
            chat_id,
            media,
            filename=filename,
            mime=mime,
            caption=caption,
            reply_to=reply_to,
            target_uid=target_uid,
            media_kind="image",
            scope=scope,
        )

    def send_file(
        self,
        chat_id: Any,
        media: StoredFile,
        *,
        filename: str | None,
        mime: str | None = "application/octet-stream",
        caption: str = "",
        reply_to: list[Any] | None = None,
        target_uid: Any = None,
        task_id: str = "",
        scope: OwnerScope,
    ) -> tuple[bool, str]:
        return self._send_media(
            chat_id,
            media,
            filename=filename,
            mime=mime,
            caption=caption,
            reply_to=reply_to,
            target_uid=target_uid,
            media_kind="file",
            task_id=task_id,
            scope=scope,
        )

    def _send_media(
        self,
        chat_id: Any,
        media: StoredFile,
        *,
        filename: str | None,
        mime: str | None,
        caption: str,
        reply_to: list[Any] | None,
        target_uid: Any,
        media_kind: str,
        scope: OwnerScope,
        task_id: str = "",
    ) -> tuple[bool, str]:
        if self._bot is None:
            return False, "dialogs sdk bot not initialized"
        peer = self._resolve_out_peer(chat_id)
        if peer is None:
            return False, f"dialogs peer is not available for chat_id={chat_id!r}"
        messaging = getattr(self._bot, "messaging", None)
        method = getattr(messaging, f"send_{media_kind}_sync", None)
        if not callable(method):
            return False, f"dialogs sdk {media_kind} send method is not available"
        deadline = time.monotonic() + _DIALOGS_OUTBOUND_TIMEOUT_SEC
        outcome: tuple[bool, str] | None = None
        phase_lock = threading.Lock()
        sdk_started = abandoned = False
        directory = tempfile.TemporaryDirectory(prefix="dialogs-send-")

        def send() -> tuple[bool, str]:
            nonlocal outcome, sdk_started
            with get_transfer_limits().slot(scope):
                storage = get_storage_provider()
                metadata = get_attachment(scope, media.file_id)
                if metadata is None or metadata["status"] != "ready":
                    raise FileUnavailable("attachment unavailable")
                limit = attachment_max_bytes(metadata["mime"])
                if metadata["size"] > limit:
                    raise ObjectTooLargeError("attachment exceeds the current size limit")
                name = metadata["filename"] if filename is None else filename
                content_type = metadata["mime"] if mime is None else mime
                suffix = Path(name).suffix or (
                    IMAGE_SUFFIXES.get(content_type.lower(), ".png") if media_kind == "image" else ""
                )
                path = Path(directory.name) / ("attachment" + suffix)
                storage.copy_to_path(scope, "uploads", metadata["storage_key"], path, max_bytes=limit)
            kwargs = self._media_kwargs(name, caption, reply_to, target_uid)
            with phase_lock:
                if abandoned or time.monotonic() >= deadline:
                    raise TimeoutError("attachment resolution deadline expired")
                sdk_started = True
            outcome = self._send_prepared_media(
                method,
                peer,
                str(path),
                kwargs=kwargs,
                media_kind=media_kind,
                task_id=task_id,
                filename=name,
            )
            return outcome

        try:
            return self._call_with_timeout(send, operation=f"{media_kind} send", on_finish=directory.cleanup)
        except FileTransferPending:
            raise
        except _DialogsDeliveryUnknown as error:
            with phase_lock:
                abandoned = True
                if not sdk_started:
                    raise FileTransferPending(
                        "attachment preparation timed out", FileFailure.TRANSFER_IN_PROGRESS
                    ) from error
            return False, DELIVERY_UNKNOWN
        except Exception as exc:
            if outcome is not None:
                log.warning("Dialogs %s cleanup failed after SDK result", media_kind, exc_info=True)
                return outcome
            if isinstance(exc, (FileUnavailable, FileNotFoundError, ObjectTooLargeError)):
                raise FileUnavailable(f"attachment resolution failed: {type(exc).__name__}") from exc
            return False, str(exc)

    def _send_prepared_media(
        self,
        method: Any,
        peer: Any,
        path: str,
        *,
        kwargs: dict[str, Any],
        media_kind: str,
        task_id: str,
        filename: str,
    ) -> tuple[bool, str]:
        last_error = ""
        safe_name = Path(filename or path).name
        for attempt in range(1, _DIALOGS_MEDIA_SEND_ATTEMPTS + 1):
            started_at = time.perf_counter()
            if media_kind == "file":
                log.info(
                    "event=dialogs_file_send_started stage=dialogs_send_file_sync task_id=%s filename=%s attempt=%s",
                    task_id,
                    safe_name,
                    attempt,
                )
            try:
                response = self._call_media_method(method, peer, path, kwargs)
                self._remember_sent_message(response)
                response_id = self._response_id(response)
                if media_kind == "file":
                    log.info(
                        "event=dialogs_file_send_accepted stage=dialogs_send_file_sync task_id=%s filename=%s "
                        "attempt=%s duration=%.3f response_id=%s",
                        task_id,
                        safe_name,
                        attempt,
                        time.perf_counter() - started_at,
                        response_id,
                    )
                return True, response_id
            except Exception as exc:
                last_error = str(exc)
                log.warning(
                    "event=dialogs_%s_send_failed stage=dialogs_send_%s_sync attempt=%s "
                    "exception_type=%s message=%s duration=%.3f task_id=%s filename=%s",
                    media_kind,
                    media_kind,
                    attempt,
                    type(exc).__name__,
                    "delivery_unknown" if isinstance(exc, TimeoutError) else "sdk_call_failed",
                    time.perf_counter() - started_at,
                    task_id,
                    safe_name,
                )
                if isinstance(exc, TimeoutError):
                    return False, DELIVERY_UNKNOWN
        return False, last_error or f"dialogs sdk {media_kind} send failed"

    def _call_media_method(self, method: Any, peer: Any, path: str, kwargs: dict[str, Any]) -> Any:
        """Send the media, and let an SDK that does not take ``headers=`` cost the header.

        The `dialog_bot_sdk` is not in this tree, so both keywords this transport passes are taken
        on trust — but they fail differently and the mitigation follows the blast radius. A
        rejected ``metadata=`` surfaces, like everywhere else in this transport. A rejected
        ``headers=`` would fail every attachment and image this agent sends, and a chat that can no
        longer deliver a file is a worse outage than an upload the environment trace cannot
        correlate: the keyword is dropped for the rest of the process, once and out loud, and the
        same attempt continues without it rather than being spent.

        Narrowed to the interpreter's own signature rejection, which is raised before the SDK runs
        anything — unlike the bot build, this call has a side effect a caller can see twice, so a
        `TypeError` from *inside* a send that may already have uploaded is left to the send loop,
        which bounds its own retries, rather than being re-sent a second time from here.
        """
        if self._media_trace_headers:
            try:
                return method(peer, path, **kwargs)
            except TypeError as exc:
                text = str(exc)
                if "headers" not in kwargs or "unexpected keyword argument" not in text or "headers" not in text:
                    raise
                self._media_trace_headers = False
                log.warning(
                    "Dialogs SDK rejected media trace headers; attachment uploads carry no %s "
                    "for the rest of this process",
                    aef_tracing.TRACE_HEADER_NAME,
                    exc_info=True,
                )
        return method(peer, path, **{key: value for key, value in kwargs.items() if key != "headers"})

    def _media_kwargs(
        self,
        filename: str,
        caption: str,
        reply_to: list[Any] | None,
        target_uid: Any,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"name": Path(filename).name}
        if caption:
            kwargs["text"] = caption
        if self.settings.DIALOGS_FORWARDING_DISABLED:
            kwargs["is_forward_banned"] = True
        if self.settings.DIALOGS_REPLY_ENABLED and reply_to:
            reply_ids = _sdk_message_ids(list(reply_to))
            if reply_ids:
                kwargs["reply"] = reply_ids
        if self.settings.DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED and target_uid not in (None, ""):
            kwargs["uid"] = int(target_uid)
        kwargs.update(outbound_metadata())
        # A media send is two channels, not one: the SDK asks the server over gRPC and then puts
        # the bytes themselves on an HTTP transfer, which takes `headers=` rather than the gRPC
        # `metadata=` beside it — the same split the download already follows. Passing only the
        # metadata left the upload, the leg that actually carries the file and the slow one, as
        # the outbound request of this transport naming no operation.
        kwargs.update(outbound_http_headers())
        return kwargs

    def _remember_sent_message(self, response: Any) -> str:
        message_id = serialize_mid(response)
        if message_id:
            self._sent_message_ids.append(message_id)
        return message_id

    @staticmethod
    def _response_id(response: Any) -> str:
        return serialize_mid(response) or "ok"
