from __future__ import annotations

import itertools
import logging
import os
import queue
import re
import select
import tempfile
import threading
import time
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ouroboros.contracts.chat_commands import answered_slash_command, is_confirm_command
from ouroboros.observability import aef_tracing
from ouroboros.storage.attachment_validation import (
    AttachmentError,
    attachment_max_bytes,
    attachment_mime_for_suffix,
    validate_attachment_path,
)
from ouroboros.storage.file_catalog import FileTransferPending
from ouroboros.storage.file_transfers import get_transfer_limits
from transport.dialogs.messages import TRANSPORT_NAME, InboundAttachment, InboundMessage
from transport.dialogs.state import (
    DialogsPeer,
    DialogsState,
    dialogs_scope,
    outbound_http_headers,
    outbound_metadata,
    serialize_mid,
)
from transport.inbound_limits import oversized_message_refusal

log = logging.getLogger(__name__)


def _originate_trace_id() -> str:
    """Start an operation for one inbound update, at the frame the SDK hands it over.

    Dialogs originates rather than accepts: a gRPC bot update stream has no header frame to
    carry a caller's identifier, and the sender is a person in a chat client rather than a
    calling system. Minting through the acceptor makes the origination a named act instead of
    a default argument doing invisible work — and it happens here, not at submission, so the
    download and the DLP check that run first belong to the operation they are part of.
    """
    return aef_tracing.accept_trace_id("", source=TRANSPORT_NAME)


@dataclass(frozen=True)
class _FileJob:
    """One queued file update, and the operation it arrived under.

    The worker runs on a thread of its own, where the handler's context variable is not
    readable, so the identifier travels on the job rather than being re-derived there.
    """

    update: Any
    trace_id: str


_DIALOGS_FILE_QUEUE_SIZE = 32
_DIALOGS_INCOMING_QUEUE_SIZE = 256
_DIALOGS_FILE_WORKERS = 2
_DIALOGS_FILE_PROCESS_ATTEMPTS = 3
_DIALOGS_DOWNLOAD_TIMEOUT_SEC = 120.0
_DIALOGS_DOWNLOAD_CHUNK_SIZE = 64 * 1024
_DIALOGS_HISTORY_RETRY_DELAY_SEC = 60.0
_SENDER_LABEL_RETRY_SEC = 300.0
_REPLY_LOOKUPS_PER_MESSAGE = 2
_REPLY_LOOKUP_TIMEOUT_SEC = 10.0
_MENTION_SEPARATORS = ",.:;!?"


class DialogsInbound(DialogsState):
    """Inbound Dialogs handlers, DLP downloads, and bounded queues."""

    def recv_update(self, cursor: Any, timeout_sec: int = 0) -> tuple[InboundMessage | None, Any]:
        self._ensure_updates_thread()
        try:
            self._cursor = max(int(self._cursor or 0), int(cursor or 0))
        except Exception:
            pass

        timeout = max(float(timeout_sec or 0), 0.25)
        try:
            message = self._incoming.get(timeout=timeout)
        except queue.Empty:
            return None, self._cursor

        self._cursor += 1
        return message, self._cursor

    def _register_handlers(self) -> None:
        if self._handlers_registered:
            return
        bot = self._ensure_bot()

        try:
            from dialog_bot_sdk.entities.messaging import MessageContentType, MessageHandler
            from dialog_bot_sdk.entities.peers import PeerType
        except Exception as exc:
            raise RuntimeError("dialog-bot-sdk messaging entities are not available") from exc

        handlers = [
            self._build_message_handler(
                MessageHandler,
                self._on_text_message,
                MessageContentType.TEXT_MESSAGE,
                peer_type,
            )
            for peer_type in self._handler_peer_types(PeerType)
        ]
        bot.messaging.message_handler(handlers)
        self._register_file_handlers(bot)
        self._handlers_registered = True

    def _register_file_handlers(self, bot: Any) -> int:
        try:
            # Optional SDK: load its extension types only when Dialogs is enabled.
            from dialog_bot_sdk.entities.sequence_and_updates import CustomUpdateHandler, CustomUpdateType
        except ImportError:
            log.info("Dialogs SDK custom file update handlers are unavailable")
            return 0
        register = getattr(getattr(bot, "updates", None), "custom_update_handler", None)
        if not callable(register):
            log.warning("Dialogs SDK custom_update_handler method is unavailable")
            return 0
        handlers = [
            CustomUpdateHandler(self._on_file_passed, CustomUpdateType.UpdateFilePassed),
            CustomUpdateHandler(self._on_file_blocked, CustomUpdateType.UpdateFileBlocked),
        ]
        register(handlers)
        return len(handlers)

    def _handler_peer_types(self, peer_type_cls: Any) -> list[Any]:
        if self._group_filter_enabled():
            group_type = getattr(peer_type_cls, "PEERTYPE_GROUP", None)
            return [group_type if group_type is not None else 2]

        peer_types: list[Any] = []
        seen: set[str] = set()
        for attr_name in ("PEERTYPE_GROUP", "PEERTYPE_PRIVATE", "PEERTYPE_ENCRYPTEDPRIVATE"):
            value = getattr(peer_type_cls, attr_name, None)
            if value is None:
                continue
            key = str(value)
            if key in seen:
                continue
            seen.add(key)
            peer_types.append(value)
        return peer_types or [None]

    def _build_message_handler(self, handler_cls, callback, content_type, peer_type=None):
        if peer_type is None:
            return handler_cls(callback, content_type)
        try:
            return handler_cls(callback, content_type, peer_type=peer_type)
        except TypeError:
            try:
                return handler_cls(callback, content_type, peer_type)
            except TypeError:
                return handler_cls(callback, content_type)

    def _ensure_updates_thread(self) -> None:
        if self._bot is None:
            return
        if self._updates_thread is not None and self._updates_thread.is_alive():
            return

        self._updates_stop_event.clear()
        self._updates_thread = threading.Thread(
            target=self._updates_loop,
            name="dialogs-sdk-updates",
            daemon=True,
        )
        self._updates_thread.start()

    def _updates_loop(self) -> None:
        while not self._updates_stop_event.is_set():
            bot = self._bot
            if bot is None:
                if self._updates_stop_event.wait(5.0):
                    return
                continue
            try:
                self._open_updates_stream(bot)
                if self._last_updates_error:
                    log.info("event=dialogs_transport_restored")
                    for peer in list(self._out_peer_by_chat_id.values()):
                        self._run_history_recovery(peer, force=True)
                self._last_updates_error = ""
            except Exception as exc:
                if not self._last_updates_error:
                    log.warning(
                        "event=dialogs_transport_unavailable exception_type=%s",
                        type(exc).__name__,
                    )
                    log.info("event=dialogs_transport_recovery_started")
                else:
                    log.debug(
                        "Dialogs SDK updates loop remains unavailable exception_type=%s",
                        type(exc).__name__,
                    )
                self._last_updates_error = type(exc).__name__
                bot.updates.stop()
            if self._updates_stop_event.wait(5.0):
                return

    def _open_updates_stream(self, bot: Any) -> None:
        """Open the update subscription under an identifier minted for this connection.

        The open is an outbound gRPC call like every other one this transport makes, and so is
        the difference fetch a reconnect triggers, so it carries `x-trace-id`. Metadata travels
        once, in the headers frame: the identifier names the subscription, never an individual
        update, which arrives as data rather than as a request this process made.

        Minted and never inherited, the rule an MCP session follows for the same reason: a
        subscription that outlives every operation must not be labelled with whichever one
        happened to open it. The scope ends with the call, which is what keeps that true in the
        other direction — an arriving update originates its own (`_on_text_message`,
        `_on_file_passed` mint explicitly rather than inheriting), and the history import a
        reconnect triggers runs outside this scope, so a recovered message is still given an
        identifier per message instead of the stream's.
        """
        kwargs: dict[str, Any] = {"do_read_message": True, "do_register_commands": True}
        with aef_tracing.trace_scope(_originate_trace_id()):
            if self._updates_trace_metadata:
                try:
                    bot.updates.on_updates(**kwargs, **outbound_metadata())
                    return
                except TypeError:
                    # The one call whose failure leaves no evidence: the loop above reads any
                    # exception as an outage and retries every 5s forever, so an SDK that does
                    # not take the kwarg would cost the whole inbound path rather than the
                    # header. It costs the header, once, out loud, and the stream opens below.
                    self._updates_trace_metadata = False
                    log.warning(
                        "Dialogs SDK on_updates rejected trace metadata; the update stream "
                        "carries no %s for the rest of this process",
                        aef_tracing.TRACE_HEADER_NAME,
                        exc_info=True,
                    )
            bot.updates.on_updates(**kwargs)

    def _on_text_message(self, message: Any) -> None:
        try:
            with aef_tracing.trace_scope(_originate_trace_id()):
                self._handle_text_message(message)
        except Exception:
            log.exception("Dialogs SDK message handler failed")

    def _handle_text_message(self, message: Any) -> None:
        inbound, reason = self._normalize_sdk_update_message(message)
        log.info(
            "Dialogs SDK message handled=%s reason=%s chat_id=%s user_id=%s text_len=%s",
            inbound is not None,
            reason,
            getattr(inbound, "chat_id", ""),
            getattr(inbound, "user_id", ""),
            len(getattr(inbound, "text", "") or ""),
        )
        if inbound is None:
            return
        if not self._queue_inbound_once(inbound, wait_for_capacity=True):
            log.warning("Dialogs live message was not queued during shutdown")
            return
        self._recover_peer_history_once(getattr(message, "peer", None))

    def _recover_peer_history_once(self, peer: DialogsPeer | None) -> None:
        self._run_history_recovery(peer, force=False)

    def _run_history_recovery(self, peer: DialogsPeer | None, *, force: bool) -> None:
        """Import peer history unless it is imported, in flight, or cooling down.

        A peer counts as recovered only after a fully successful import; a
        failed one retries after a delay. Recovery failures never escape this
        method — they must not be mistaken for transport failures upstream.
        """
        if not self.settings.DIALOGS_HISTORY_RECOVERY_ENABLED or self._bot is None:
            return
        peer_key = self._peer_key(peer)
        if not peer_key:
            return
        with self._history_recovery_lock:
            state = self._history_recovery_state.get(peer_key)
            if state == "recovering" or (state == "recovered" and not force):
                return
            failed_at = self._history_recovery_failed_at.get(peer_key, 0.0)
            if not force and time.monotonic() - failed_at < _DIALOGS_HISTORY_RETRY_DELAY_SEC:
                return
            self._history_recovery_state[peer_key] = "recovering"
        try:
            recovered = self._recover_peer_history(peer)
        except Exception:
            log.warning("Dialogs history recovery failed peer=%s", peer_key, exc_info=True)
            recovered = False
        with self._history_recovery_lock:
            if recovered:
                self._history_recovery_state[peer_key] = "recovered"
                self._history_recovery_failed_at.pop(peer_key, None)
            else:
                self._history_recovery_state.pop(peer_key, None)
                self._history_recovery_failed_at[peer_key] = time.monotonic()

    def _recover_peer_history(self, peer: DialogsPeer | None) -> bool:
        if not self.settings.DIALOGS_HISTORY_RECOVERY_ENABLED or self._bot is None:
            return False
        peer_key = self._peer_key(peer)
        if not peer_key:
            return False
        loader = getattr(getattr(self._bot, "messaging", None), "load_message_history_sync", None)
        if not callable(loader):
            log.warning("Dialogs SDK load_message_history_sync method is unavailable")
            return False
        limit = int(self.settings.DIALOGS_HISTORY_RECOVERY_LIMIT)
        try:
            history = (
                self._call_with_timeout(
                    loader,
                    peer,
                    date=0,
                    limit=limit,
                    operation="history recovery",
                    **outbound_metadata(),
                )
                or ()
            )
        except Exception as exc:
            log.warning("Dialogs SDK history recovery failed peer=%s error=%s", peer_key, exc)
            return False
        window = sorted(itertools.islice(history, limit), key=self._update_date)
        queued = 0
        drops: dict[str, int] = {}
        for update in window:
            try:
                inbound, reason = self._normalize_sdk_update_message(update, history_only=True)
            except Exception:
                log.warning("Dialogs history update normalization failed peer=%s", peer_key, exc_info=True)
                drops["normalize_error"] = drops.get("normalize_error", 0) + 1
                continue
            if inbound is None:
                drops[reason] = drops.get(reason, 0) + 1
                continue
            if not self._queue_inbound_once(inbound, wait_for_capacity=True):
                return False
            queued += 1
        log.info(
            "event=dialogs_history_imported peer=%s limit=%s loaded=%s queued=%s drops=%s dates=%s",
            peer_key,
            limit,
            len(window),
            queued,
            drops,
            f"{self._update_date(window[0])}..{self._update_date(window[-1])}" if window else "",
        )
        return True

    def _queue_inbound_once(
        self,
        inbound: InboundMessage,
        *,
        wait_for_capacity: bool = False,
    ) -> bool:
        if not str(inbound.source_message_id or "").strip():
            log.warning("Dialogs inbound message has no stable message id")
            return False
        while True:
            with self._incoming_lock:
                if self._updates_stop_event.is_set():
                    return False
                try:
                    self._incoming.put_nowait(inbound)
                    return True
                except queue.Full:
                    pass
            if not wait_for_capacity:
                log.warning("Dialogs inbound queue is full")
                return False
            if self._updates_stop_event.wait(0.25):
                return False

    def _ensure_file_workers(self) -> None:
        with self._file_workers_lock:
            if any(worker.is_alive() for worker in self._file_workers):
                return
            self._file_workers = []
            for index in range(_DIALOGS_FILE_WORKERS):
                worker = threading.Thread(
                    target=self._file_worker_loop,
                    name=f"dialogs-file-{index + 1}",
                    daemon=True,
                )
                worker.start()
                self._file_workers.append(worker)

    def _file_worker_loop(self) -> None:
        while not self._updates_stop_event.is_set():
            try:
                job = self._file_jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            update = job.update
            try:
                # The whole leg — every download attempt, the DLP check, and the rejection
                # that may replace them — under the operation the update arrived on. This is
                # the slow part and the part that fails, so it is the part worth correlating.
                with aef_tracing.trace_scope(job.trace_id):
                    for attempt in range(1, _DIALOGS_FILE_PROCESS_ATTEMPTS + 1):
                        try:
                            self._process_file_passed(update, attempt=attempt)
                            break
                        except Exception as exc:
                            retryable = (
                                exc.retryable
                                if isinstance(exc, AttachmentError)
                                else isinstance(exc, FileTransferPending) or not isinstance(exc, ValueError)
                            )
                            if not retryable:
                                log.warning("Dialogs queued file rejected: %s", exc)
                                break
                            if attempt >= _DIALOGS_FILE_PROCESS_ATTEMPTS:
                                log.warning(
                                    "Dialogs queued file handler exhausted retries exception_type=%s "
                                    "code=%s reason=%s filename=%s attempts=%s",
                                    type(exc).__name__,
                                    exc.code if isinstance(exc, AttachmentError) else "file_processing_failed",
                                    exc.reason if isinstance(exc, AttachmentError) else "",
                                    exc.filename if isinstance(exc, AttachmentError) else "",
                                    attempt,
                                )
                                self._report_file_processing_failure(update)
                                break
                            log.debug(
                                "Dialogs queued file handler retrying attempt=%s/%s exception_type=%s",
                                attempt,
                                _DIALOGS_FILE_PROCESS_ATTEMPTS,
                                type(exc).__name__,
                            )
                            if self._updates_stop_event.wait(0.25 * (2 ** (attempt - 1))):
                                break
            finally:
                self._file_jobs.task_done()

    def _report_file_processing_failure(self, update: Any) -> None:
        identity, _reason = self._normalize_inbound_identity(update)
        document = self._extract_document(update)
        if identity is not None and document is not None:
            self._send_file_rejection(
                update,
                document,
                "could not process the file; please send it again",
                identity=identity,
            )

    @staticmethod
    def _document_size(document: Any) -> int:
        for attr in ("size", "file_size", "fileSize", "length"):
            value = getattr(document, attr, None)
            if value in (None, ""):
                continue
            try:
                return max(0, int(str(value)))
            except (TypeError, ValueError):
                continue
        return 0

    def _on_file_passed(self, update: Any) -> None:
        with aef_tracing.trace_scope(_originate_trace_id()):
            self._handle_file_passed(update)

    def _handle_file_passed(self, update: Any) -> None:
        identity, reason = self._normalize_inbound_identity(update)
        if identity is None:
            log.info("Dialogs DLP-passed file ignored reason=%s", reason)
            return
        document = self._extract_document(update)
        if document is None or not self._mark_file_update(update, document, "passed", identity=identity):
            return
        filename = self._safe_document_name(getattr(document, "name", ""))
        message_id = re.sub(r"[^A-Za-z0-9:._-]", "_", self._source_message_id(update))[:128]
        mime = re.sub(r"[^a-z0-9.+/-]", "_", self._document_mime(document, filename))[:100]
        log.info(
            "event=dialogs_file_received message_id=%s filename=%s declared_mime=%s declared_size=%s source=dialogs",
            message_id,
            filename,
            mime,
            self._document_size(document),
        )
        if not self._is_addressed(update):
            self._queue_file_context(update, identity, document, filename)
            return
        caption = self._strip_bot_mention(str(getattr(document, "caption", "") or "")).strip()
        if self._refuse_oversized(caption, update, identity):
            return
        limit = attachment_max_bytes(self._document_mime(document, filename))
        if self._document_size(document) > limit:
            log.warning(
                "event=dialogs_file_validation_failed filename=%s message_id=%s "
                "error_code=attachment_too_large reason=size_limit",
                filename,
                message_id,
            )
            self._send_file_rejection(
                update,
                document,
                self._size_limit_reason(limit),
                identity=identity,
            )
            return
        self._ensure_file_workers()
        while not self._updates_stop_event.is_set():
            try:
                self._file_jobs.put(_FileJob(update, aef_tracing.current_trace_id()), timeout=0.25)
                return
            except queue.Full:
                continue
        log.warning("Dialogs file update was not queued during shutdown")

    def _download_document(self, downloader: Any, path: Path, document: Any, max_bytes: int, *, chat_id: str) -> None:
        download_slots = self._download_slots
        if not download_slots.acquire(blocking=False):
            raise RuntimeError("Dialogs download capacity is exhausted")
        fifo_path: Path | None = None
        reader_fd: int | None = None
        thread_started = False
        try:
            fifo_path = path.with_name(f".{path.name}.download")
            result: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)
            timed_out = threading.Event()
            # Read here rather than on the download thread: this is the frame that belongs to
            # the arriving file, and the transfer is HTTP, so it takes the header rather than
            # the gRPC metadata every other SDK call in this transport carries.
            trace_headers = outbound_http_headers()

            def _call() -> None:
                error: BaseException | None = None
                try:
                    with get_transfer_limits().slot(dialogs_scope(chat_id)):
                        downloader(str(fifo_path), document, **trace_headers)
                except BaseException as exc:
                    error = exc
                finally:
                    download_slots.release()
                    if timed_out.is_set():
                        path.unlink(missing_ok=True)
                    result.put(error)

            path.unlink(missing_ok=True)
            fifo_path.unlink(missing_ok=True)
            os.mkfifo(fifo_path, 0o600)
            reader_fd = os.open(fifo_path, os.O_RDWR | os.O_NONBLOCK)
            # The download runs on a thread only so the timeout above can be enforced; it is
            # the same operation, so it carries the caller's context rather than starting
            # bare — the rule `tool-runtimes` already states for a thread placement.
            thread = threading.Thread(target=copy_context().run, args=(_call,), name="dialogs-download", daemon=True)
            deadline = time.monotonic() + _DIALOGS_DOWNLOAD_TIMEOUT_SEC
            thread.start()
            thread_started = True
            written = 0
            with path.open("wb") as output:
                while True:
                    if self._updates_stop_event.is_set():
                        timed_out.set()
                        raise RuntimeError("Dialogs file download stopped")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 and thread.is_alive():
                        timed_out.set()
                        raise TimeoutError("Dialogs file download timed out")
                    readable, _, _ = select.select([reader_fd], [], [], max(0.0, min(0.1, remaining)))
                    if readable:
                        chunk = os.read(reader_fd, _DIALOGS_DOWNLOAD_CHUNK_SIZE)
                        if chunk:
                            if written + len(chunk) > max_bytes:
                                raise AttachmentError(
                                    "attachment_too_large",
                                    self._size_limit_reason(max_bytes),
                                )
                            output.write(chunk)
                            written += len(chunk)
                            continue
                    if thread.is_alive():
                        continue
                    while True:
                        try:
                            chunk = os.read(reader_fd, _DIALOGS_DOWNLOAD_CHUNK_SIZE)
                        except BlockingIOError:
                            break
                        if not chunk:
                            break
                        if written + len(chunk) > max_bytes:
                            raise AttachmentError(
                                "attachment_too_large",
                                self._size_limit_reason(max_bytes),
                            )
                        output.write(chunk)
                        written += len(chunk)
                    break
            thread.join(timeout=0.1)
            error = result.get_nowait()
            if error is not None:
                raise error
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            if not thread_started:
                download_slots.release()
            if reader_fd is not None:
                os.close(reader_fd)
            if fifo_path is not None:
                fifo_path.unlink(missing_ok=True)

    def _process_file_passed(self, update: Any, *, attempt: int = 1) -> None:
        temp_path: Path | None = None
        try:
            identity, reason = self._normalize_inbound_identity(update)
            if identity is None:
                log.info("Dialogs DLP-passed file ignored reason=%s", reason)
                return
            document = self._extract_document(update)
            if document is None:
                return
            filename = self._safe_document_name(getattr(document, "name", ""))
            caption = self._strip_bot_mention(str(getattr(document, "caption", "") or "")).strip()
            file_id = getattr(document, "file_id", 0)
            access_hash = getattr(document, "access_hash", 0)
            if not file_id or not access_hash:
                log.warning("Dialogs DLP-passed file has no download credentials")
                return

            mime = self._document_mime(document, filename)
            suffix = Path(filename).suffix[:16]
            with tempfile.NamedTemporaryFile(delete=False, prefix="dialogs-", suffix=suffix) as handle:
                temp_path = Path(handle.name)
            started_at = time.monotonic()
            message_id = re.sub(r"[^A-Za-z0-9:._-]", "_", self._source_message_id(update))[:128]
            log.info(
                "event=dialogs_file_download_started filename=%s message_id=%s attempt=%s stage=download_file_sync",
                filename,
                message_id,
                attempt,
            )
            try:
                downloader = getattr(
                    getattr(getattr(self._bot, "internal", None), "downloading", None),
                    "download_file_sync",
                    None,
                )
                if not callable(downloader):
                    raise RuntimeError("Dialogs SDK download_file_sync method is unavailable")
                self._download_document(
                    downloader, temp_path, document, attachment_max_bytes(mime), chat_id=identity[0]
                )
            except AttachmentError as exc:
                if exc.retryable:
                    raise
                log.warning(
                    "event=dialogs_file_validation_failed filename=%s message_id=%s "
                    "error_code=%s reason=validation_rejected",
                    filename,
                    message_id,
                    exc.code,
                )
                self._send_file_rejection(
                    update,
                    document,
                    exc.reason,
                    identity=identity,
                )
                return
            except Exception as exc:
                log.warning(
                    "event=dialogs_file_download_failed filename=%s message_id=%s attempt=%s stage=download_file_sync exception_type=%s message=download_failed duration=%.3f",
                    filename,
                    message_id,
                    attempt,
                    type(exc).__name__,
                    time.monotonic() - started_at,
                )
                raise
            log.info(
                "event=dialogs_file_download_completed filename=%s message_id=%s actual_size=%s duration=%.3f",
                filename,
                message_id,
                temp_path.stat().st_size,
                time.monotonic() - started_at,
            )
            if self._updates_stop_event.is_set():
                return

            try:
                validated = validate_attachment_path(temp_path, filename, mime)
            except AttachmentError as exc:
                if exc.retryable:
                    raise
                log.warning(
                    "event=dialogs_file_validation_failed filename=%s message_id=%s "
                    "error_code=%s reason=validation_rejected",
                    filename,
                    message_id,
                    exc.code,
                )
                self._send_file_rejection(
                    update,
                    document,
                    exc.reason,
                    identity=identity,
                )
                return
            log.info(
                "event=dialogs_file_validation_passed filename=%s message_id=%s reason=accepted", filename, message_id
            )
            if self._updates_stop_event.is_set():
                return
            queued = self._queue_inbound_once(
                self._file_inbound(
                    update,
                    identity,
                    filename,
                    caption,
                    attachments=(
                        InboundAttachment(
                            filename=filename,
                            mime=validated.mime,
                            size=validated.size,
                            temp_path=str(temp_path),
                        ),
                    ),
                ),
                wait_for_capacity=True,
            )
            if queued:
                temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _queue_file_context(
        self,
        update: Any,
        identity: tuple[str, str, str],
        document: Any,
        filename: str,
    ) -> None:
        """Keep an unaddressed group file as history context: no download, no reply."""
        caption = self._strip_bot_mention(str(getattr(document, "caption", "") or "")).strip()
        if self._refuse_oversized(caption, update, identity, history_only=True):
            return
        self._queue_inbound_once(
            self._file_inbound(update, identity, filename, caption, history_only=True),
            wait_for_capacity=True,
        )

    def _file_inbound(
        self,
        update: Any,
        identity: tuple[str, str, str],
        filename: str,
        caption: str,
        *,
        attachments: tuple[InboundAttachment, ...] = (),
        history_only: bool = False,
    ) -> InboundMessage:
        chat_id, user_id, _accepted_reason = identity
        return InboundMessage(
            chat_id=chat_id,
            user_id=user_id,
            trace_id=aef_tracing.current_trace_id(),
            text=caption or f"Вложение: {filename}",
            caption=caption,
            source_message_id=self._source_message_id(update),
            sender_label=self._sender_label(user_id),
            attachments=attachments,
            history_only=history_only,
            ts=self._update_ts(update),
        )

    def _on_file_blocked(self, update: Any) -> None:
        try:
            identity, reason = self._normalize_inbound_identity(update)
            if identity is None:
                log.info("Dialogs DLP-blocked file ignored reason=%s", reason)
                return
            document = self._extract_document(update)
            if document is None or not self._mark_file_update(update, document, "blocked", identity=identity):
                return
            filename = self._safe_document_name(getattr(document, "name", ""))
            if not self._is_addressed(update):
                self._queue_file_context(update, identity, document, filename)
                return
            chat_id, user_id, _accepted_reason = identity
            self.send_text(
                chat_id,
                f"Файл «{filename}» заблокирован политикой безопасности SberChat.",
                reply_to=[self._source_message_id(update)],
                target_uid=user_id,
            )
        except Exception:
            log.exception("Dialogs DLP-blocked file handler failed")

    def _normalize_sdk_update_message(
        self,
        update: Any,
        *,
        history_only: bool = False,
    ) -> tuple[InboundMessage | None, str]:
        identity, reason = self._normalize_inbound_identity(update)
        if identity is None:
            return None, reason
        chat_id, user_id, accepted_reason = identity

        original_text = self._strip_bot_mention(self._extract_text(update))
        if not original_text:
            return None, "empty_text"

        # Recovered history never starts a task, so it must not pay for
        # addressing checks either — a reply would trigger a server lookup.
        context_only = history_only or not self._is_addressed(update)
        if self._refuse_oversized(original_text, update, identity, history_only=context_only):
            return None, "message_too_long"
        return InboundMessage(
            chat_id=chat_id,
            user_id=user_id,
            trace_id=aef_tracing.current_trace_id(),
            text=original_text,
            caption="",
            image_data=None,
            raw_command=original_text if str(original_text).startswith("/") else "",
            source_message_id=self._source_message_id(update),
            sender_label=self._sender_label(user_id),
            history_only=context_only,
            ts=self._update_ts(update),
        ), "context_only" if context_only else accepted_reason

    def _refuse_oversized(
        self, text: str, update: Any, identity: tuple[str, str, str], *, history_only: bool = False
    ) -> bool:
        refusal = oversized_message_refusal(text, self.settings.OUROBOROS_MAX_INBOUND_MESSAGE_CHARS)
        if not refusal:
            return False
        if not history_only:
            chat_id, user_id, _reason = identity
            self.send_text(chat_id, refusal, reply_to=[self._source_message_id(update)], target_uid=user_id)
        return True

    def _sender_label(self, user_id: str) -> str:
        if not self.settings.DIALOGS_PARTICIPANT_IDENTITY_ENABLED or not user_id:
            return ""
        cached = self._sender_labels.get(user_id)
        if cached is not None:
            return cached
        # Only successful lookups are cached: a transient SDK failure must not
        # pin the placeholder forever, so it is retried after a cooldown.
        if time.monotonic() < self._sender_label_retry_at.get(user_id, 0.0):
            return f"user-{user_id}"
        label = self._resolve_sender_label(user_id)
        if label:
            self._sender_labels[user_id] = label
            self._sender_label_retry_at.pop(user_id, None)
            return label
        self._sender_label_retry_at[user_id] = time.monotonic() + _SENDER_LABEL_RETRY_SEC
        return f"user-{user_id}"

    def _resolve_sender_label(self, user_id: str) -> str:
        lookup = getattr(getattr(self._bot, "users", None), "get_user_by_id_sync", None)
        if not callable(lookup):
            return ""
        try:
            user = self._call_with_timeout(lookup, int(user_id), operation="user lookup", **outbound_metadata())
        except Exception as exc:
            log.warning(
                "Dialogs sender label lookup failed user_id=%s exception_type=%s",
                user_id,
                type(exc).__name__,
            )
            return ""
        data = getattr(user, "data", None)
        name = str(getattr(data, "name", "") or getattr(data, "nick", "") or "")
        return " ".join(re.sub(r"[\x00-\x1f\x7f\[\]]", " ", name).split())[:64]

    def _is_addressed(self, update: Any) -> bool:
        """Whether a group message asks the bot for something; the rest is kept as context."""
        if not str(self.settings.DIALOGS_BOT_NICK or "").strip():
            return True
        if not self._is_group_peer(getattr(update, "peer", None)):
            return True
        text = self._update_text(update)
        return (
            self._mentions_bot(text)
            or bool(answered_slash_command(text))
            or is_confirm_command(text)
            or self._is_reply_to_self(update)
        )

    def _mention_pattern(self) -> re.Pattern[str] | None:
        nick = str(self.settings.DIALOGS_BOT_NICK or "").strip().lstrip("@")
        if not nick:
            return None
        return re.compile(rf"(?<![\w@])@{re.escape(nick)}(?![\w@-])", re.IGNORECASE)

    def _mention_strip_pattern(self) -> re.Pattern[str] | None:
        # The mention plus one trailing separator, so an inline "@bot," leaves
        # no dangling punctuation behind.
        nick = str(self.settings.DIALOGS_BOT_NICK or "").strip().lstrip("@")
        if not nick:
            return None
        return re.compile(
            rf"(?<![\w@])@{re.escape(nick)}(?![\w@-])[ \t]*[{re.escape(_MENTION_SEPARATORS)}]?",
            re.IGNORECASE,
        )

    def _mentions_bot(self, text: str) -> bool:
        pattern = self._mention_pattern()
        return bool(pattern and pattern.search(str(text or "")))

    def _strip_bot_mention(self, text: str) -> str:
        pattern = self._mention_strip_pattern()
        original = str(text or "")
        if pattern is None or not pattern.search(original):
            return text
        without = pattern.sub(" ", original).strip().lstrip(_MENTION_SEPARATORS)
        return re.sub(r"[ \t]{2,}", " ", without).strip()

    def _update_text(self, update: Any) -> str:
        text = self._extract_text(update)
        if text:
            return text
        return str(getattr(self._extract_document(update), "caption", "") or "")

    def _is_reply_to_self(self, update: Any) -> bool:
        mids: dict[str, Any] = {}
        for owner in (update, getattr(update, "message", None)):
            reply = getattr(owner, "reply", None)
            if reply is None:
                continue
            # The public SDK declares ``reply`` as a list of message UUIDs, but
            # a scalar MID must not take the whole live message down with it.
            if not isinstance(reply, (list, tuple, set, frozenset)):
                reply = (reply,)
            for value in reply:
                mid = serialize_mid(value)
                if mid:
                    mids.setdefault(mid, value)
        if any(mid in self._sent_message_ids for mid in mids):
            return True
        unknown = [(mid, value) for mid, value in mids.items() if mid not in self._foreign_message_ids]
        # A reply cites one message; the cap only bounds degenerate updates.
        return any(self._is_own_message(value, mid) for mid, value in unknown[:_REPLY_LOOKUPS_PER_MESSAGE])

    def _is_own_message(self, value: Any, mid: str) -> bool:
        """Ask the server who sent ``mid`` when the sent-id LRU does not know.

        The LRU forgets everything on restart and after 256 sends, so a miss
        is resolved against the server. Only a definite answer is cached: a
        failed or incomplete lookup returns False uncached, so the reply stays
        context-only chatter and the next reply retries. Lookups run on their
        own small slot pool with a short timeout — a hung SDK call degrades
        reply detection instead of starving outbound sends.
        """
        fetch = getattr(getattr(self._bot, "messaging", None), "get_message_by_id_sync", None)
        if not callable(fetch):
            if not self._reply_lookup_warned:
                self._reply_lookup_warned = True
                log.warning(
                    "Dialogs SDK get_message_by_id_sync is unavailable; "
                    "replies to the bot's older messages will not start tasks"
                )
            return False
        try:
            message = self._call_with_timeout(
                fetch,
                value,
                operation="reply lookup",
                slots=self._lookup_slots,
                timeout_sec=_REPLY_LOOKUP_TIMEOUT_SEC,
                **outbound_metadata(),
            )
        except Exception as exc:
            log.warning("Dialogs reply lookup failed mid=%s exception_type=%s", mid, type(exc).__name__)
            return False
        sender_id = self._sender_key(getattr(message, "sender_peer", None))
        self_id = self._self_sender_key()
        if not sender_id or not self_id:
            return False
        if sender_id == self_id:
            self._sent_message_ids.append(mid)
            return True
        self._foreign_message_ids.append(mid)
        return False

    def _normalize_inbound_identity(self, update: Any) -> tuple[tuple[str, str, str] | None, str]:
        peer: DialogsPeer | None = getattr(update, "peer", None)
        if peer is None:
            return None, "missing_peer_id"
        if self._group_filter_enabled():
            if not self._is_configured_group_peer(peer):
                return None, "ignored_peer"
            self._remember_group_peer(peer)
            peer_id = str(self._group_id())
        else:
            peer_id = self._peer_key(peer)
            if not peer_id:
                return None, "missing_peer_id"

        sender_peer = getattr(update, "sender_peer", None)
        if self._is_self_sender(sender_peer):
            return None, "ignored_self_message"
        sender_id = self._sender_key(sender_peer)
        if self._group_filter_enabled() and not sender_id:
            return None, "missing_sender"
        if self._user_filter_enabled() and not self._is_configured_user_sender(sender_peer):
            return None, "ignored_user"

        if self._group_filter_enabled():
            self._remember_out_peer(peer_id, peer)
            chat_id = peer_id
            user_id = sender_id or peer_id
            reason = "accepted_group_text"
        else:
            self._remember_out_peer(peer_id, peer)
            chat_id = peer_id
            user_id = sender_id or peer_id
            reason = "accepted_text"
        return (chat_id, user_id, reason), reason

    @staticmethod
    def _extract_document(update: Any) -> Any | None:
        message = getattr(update, "message", None)
        if message is None:
            return None
        return getattr(message, "document_message", None) or getattr(message, "documentMessage", None)

    @staticmethod
    def _safe_document_name(value: Any) -> str:
        raw = str(value or "").replace("\\", "/").strip()
        return re.sub(r"[\x00-\x1f\x7f]", "_", Path(raw).name)[:200] or "attachment.bin"

    @staticmethod
    def _document_mime(document: Any, filename: str) -> str:
        declared = (
            str(
                getattr(document, "mime_type", "")
                or getattr(document, "mime", "")
                or getattr(document, "content_type", "")
                or ""
            )
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        return declared or attachment_mime_for_suffix(filename) or "application/octet-stream"

    @staticmethod
    def _size_limit_reason(max_bytes: int) -> str:
        for scale, unit in ((1024 * 1024, "МиБ"), (1024, "КиБ")):
            if max_bytes >= scale:
                return f"размер превышает {max_bytes / scale:g} {unit} ({max_bytes} байт)"
        return f"размер превышает {max_bytes} байт"

    @staticmethod
    def _update_date(update: Any) -> int:
        try:
            return int(getattr(update, "date", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _update_ts(cls, update: Any) -> str:
        """SDK message date as ISO UTC, so recovered history keeps its real time."""
        raw = cls._update_date(update)
        if raw >= 1_000_000_000_000:
            seconds = raw / 1000.0
        elif raw >= 1_000_000_000:
            seconds = float(raw)
        else:
            return ""
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return ""

    @staticmethod
    def _source_message_id(update: Any) -> str:
        message = getattr(update, "message", None)
        values: list[Any] = []
        for owner in (message, update):
            for attr in ("message_id", "mid", "id"):
                value = getattr(owner, attr, None)
                if value not in (None, ""):
                    values.append(value)
        for value in values:
            msb = value.get("msb") if isinstance(value, dict) else getattr(value, "msb", None)
            lsb = value.get("lsb") if isinstance(value, dict) else getattr(value, "lsb", None)
            if msb is not None and lsb is not None:
                return f"{int(msb)}:{int(lsb)}"
        if values:
            return str(values[0])
        return ""

    def _send_file_rejection(
        self,
        update: Any,
        document: Any,
        reason: str,
        *,
        identity: tuple[str, str, str],
    ) -> None:
        chat_id, user_id, _accepted_reason = identity
        filename = self._safe_document_name(getattr(document, "name", ""))
        self.send_text(
            chat_id,
            f"Файл «{filename}» отклонён: {str(reason).rstrip('.')}.",
            reply_to=[self._source_message_id(update)],
            target_uid=user_id,
        )

    def _mark_file_update(
        self,
        update: Any,
        document: Any,
        status: str,
        *,
        identity: tuple[str, str, str],
    ) -> bool:
        chat_id, user_id, _accepted_reason = identity
        key = ":".join(
            (
                status,
                chat_id,
                user_id,
                self._source_message_id(update),
                str(getattr(document, "file_id", "") or ""),
                str(getattr(document, "access_hash", "") or ""),
            )
        )
        if key in self._seen_file_updates:
            return False
        self._seen_file_updates.add(key)
        self._seen_file_update_order.append(key)
        while len(self._seen_file_update_order) > 2048:
            self._seen_file_updates.discard(self._seen_file_update_order.popleft())
        return True
