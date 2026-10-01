from __future__ import annotations

import logging
import queue
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.storage.chat_attachments import MAX_OUTBOUND_BYTES
from supervisor.messaging.dialogs.state import DialogsState, sdk_mid, serialize_mid

log = logging.getLogger(__name__)

MAX_DIALOGS_MESSAGE_CHARS = 3800
_DIALOGS_MEDIA_SEND_ATTEMPTS = 2
_DIALOGS_OUTBOUND_TIMEOUT_SEC = 120.0


class _DialogsDeliveryUnknown(TimeoutError):
    """The SDK call outlived our wait, so the remote outcome is ambiguous."""


def _sdk_message_ids(values: list[Any]) -> list[Any]:
    try:
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
            result.put(outcome)

        worker = threading.Thread(target=_run, name="dialogs-outbound", daemon=True)
        try:
            worker.start()
        except Exception:
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
                anchor = self._call_with_timeout(fetch, mid, operation="progress anchor fetch")
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
            )
            return True
        except Exception as exc:
            log.warning("Dialogs SDK typing failed action=%s error=%s", action, exc)
            return False

    def send_photo(
        self,
        chat_id: Any,
        image_bytes: bytes,
        caption: str = "",
        mime: str = "image/png",
        reply_to: list[Any] | None = None,
        target_uid: Any = None,
    ) -> tuple[bool, str]:
        filename = "image" + self._image_suffix(mime)
        return self.send_image(
            chat_id,
            image_bytes,
            filename=filename,
            caption=caption,
            mime=mime,
            reply_to=reply_to,
            target_uid=target_uid,
        )

    def send_image(
        self,
        chat_id: Any,
        image_bytes: bytes,
        *,
        filename: str,
        caption: str = "",
        mime: str = "image/png",
        reply_to: list[Any] | None = None,
        target_uid: Any = None,
    ) -> tuple[bool, str]:
        if len(image_bytes) > MAX_OUTBOUND_BYTES:
            return False, "image exceeds 50 MB limit"
        if self._bot is None:
            return False, "dialogs sdk bot not initialized"
        peer = self._resolve_out_peer(chat_id)
        if peer is None:
            return False, f"dialogs peer is not available for chat_id={chat_id!r}"
        messaging = getattr(self._bot, "messaging", None)
        method = getattr(messaging, "send_image_sync", None)
        if not callable(method):
            return False, "dialogs sdk image send method is not available"
        try:
            media_kwargs = self._media_kwargs(filename, caption, reply_to, target_uid)
        except (ImportError, TypeError, ValueError) as exc:
            return False, str(exc)
        suffix = Path(filename).suffix or self._image_suffix(mime)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(image_bytes)
            temp_path = handle.name
        try:
            return self._call_with_timeout(
                self._send_media_path,
                method,
                peer,
                temp_path,
                operation="image send",
                on_finish=lambda: Path(temp_path).unlink(missing_ok=True),
                kwargs=media_kwargs,
                media_kind="image",
            )
        except _DialogsDeliveryUnknown:
            return False, DELIVERY_UNKNOWN
        except Exception as exc:
            return False, str(exc)

    def send_file(
        self,
        chat_id: Any,
        file_bytes: bytes,
        *,
        filename: str,
        mime: str = "application/octet-stream",
        caption: str = "",
        reply_to: list[Any] | None = None,
        target_uid: Any = None,
        task_id: str = "",
    ) -> tuple[bool, str]:
        del mime
        if len(file_bytes) > MAX_OUTBOUND_BYTES:
            return False, "attachment exceeds 50 MB limit"
        if self._bot is None:
            return False, "dialogs sdk bot not initialized"
        peer = self._resolve_out_peer(chat_id)
        if peer is None:
            return False, f"dialogs peer is not available for chat_id={chat_id!r}"
        messaging = getattr(self._bot, "messaging", None)
        method = getattr(messaging, "send_file_sync", None)
        if not callable(method):
            return False, "dialogs sdk file send method is not available"
        try:
            media_kwargs = self._media_kwargs(filename, caption, reply_to, target_uid)
        except (ImportError, TypeError, ValueError) as exc:
            return False, str(exc)
        suffix = Path(filename).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(file_bytes)
            temp_path = handle.name
        try:
            return self._call_with_timeout(
                self._send_media_path,
                method,
                peer,
                temp_path,
                operation="file send",
                on_finish=lambda: Path(temp_path).unlink(missing_ok=True),
                kwargs=media_kwargs,
                media_kind="file",
                task_id=task_id,
                filename=filename,
            )
        except _DialogsDeliveryUnknown:
            return False, DELIVERY_UNKNOWN
        except Exception as exc:
            return False, str(exc)

    def _send_media_path(
        self,
        method: Any,
        peer: Any,
        path: str,
        *,
        kwargs: dict[str, Any],
        media_kind: str,
        task_id: str = "",
        filename: str = "",
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
                response = method(peer, path, **kwargs)
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
            except TimeoutError as exc:
                if media_kind == "file":
                    log.warning(
                        "event=dialogs_file_send_failed stage=dialogs_send_file_sync task_id=%s "
                        "filename=%s attempt=%s exception_type=%s message=delivery_unknown duration=%.3f",
                        task_id,
                        safe_name,
                        attempt,
                        type(exc).__name__,
                        time.perf_counter() - started_at,
                    )
                else:
                    log.warning(
                        "event=dialogs_%s_send_failed stage=dialogs_send_%s_sync attempt=%s "
                        "exception_type=%s message=delivery_unknown duration=%.3f",
                        media_kind,
                        media_kind,
                        attempt,
                        type(exc).__name__,
                        time.perf_counter() - started_at,
                    )
                return False, DELIVERY_UNKNOWN
            except Exception as exc:
                last_error = str(exc)
                if media_kind == "file":
                    log.warning(
                        "event=dialogs_file_send_failed stage=dialogs_send_file_sync task_id=%s filename=%s "
                        "attempt=%s exception_type=%s message=sdk_call_failed duration=%.3f",
                        task_id,
                        safe_name,
                        attempt,
                        type(exc).__name__,
                        time.perf_counter() - started_at,
                    )
                else:
                    log.warning(
                        "event=dialogs_%s_send_failed stage=dialogs_send_%s_sync attempt=%s "
                        "exception_type=%s message=sdk_call_failed duration=%.3f",
                        media_kind,
                        media_kind,
                        attempt,
                        type(exc).__name__,
                        time.perf_counter() - started_at,
                    )
        return False, last_error or f"dialogs sdk {media_kind} send failed"

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
        return kwargs

    @staticmethod
    def _image_suffix(mime: str) -> str:
        return {
            "image/gif": ".gif",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }.get(str(mime or "").lower(), ".png")

    def _remember_sent_message(self, response: Any) -> str:
        message_id = serialize_mid(response)
        if message_id:
            self._sent_message_ids.append(message_id)
        return message_id

    @staticmethod
    def _response_id(response: Any) -> str:
        return serialize_mid(response) or "ok"
