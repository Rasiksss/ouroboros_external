from __future__ import annotations

import asyncio
import logging
import multiprocessing
import pathlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Literal

import anyio

from ouroboros.attachment_parser import ParserResult, parser_child
from ouroboros.contracts.attachments import attachment_kind
from ouroboros.storage.settings.store import load_settings
from ouroboros.utils import finish_file_io
from security_config.attachments import ATTACHMENT_FORMATS, DOCX_MIME, PPTX_MIME, XLSX_MIME

log = logging.getLogger(__name__)
_PARSER_SLOT = threading.BoundedSemaphore(1)


class AttachmentError(ValueError):
    """Stable attachment rejection used by browser and transport adapters."""

    def __init__(
        self,
        code: str,
        reason: str,
        *,
        filename: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.filename = filename
        self.retryable = retryable


@dataclass(frozen=True)
class ValidatedAttachment:
    filename: str
    mime: str
    kind: Literal["image", "file"]
    size: int


def attachment_max_bytes(mime: str) -> int:
    settings = load_settings()
    return (
        settings.OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES
        if attachment_kind(mime) == "image"
        else settings.OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES
    )


def attachment_mime_for_suffix(filename: str) -> str:
    """Return the canonical MIME for an allowed filename suffix."""
    rule = ATTACHMENT_FORMATS.get(pathlib.Path(str(filename or "")).suffix.lower())
    return rule.mime if rule is not None else ""


def image_signature(head: bytes) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _parser_unavailable(filename: str, reason: str) -> AttachmentError:
    return AttachmentError("attachment_parser_unavailable", reason, filename=filename, retryable=True)


def run_parser(
    path: pathlib.Path,
    mime: str,
    *,
    mode: Literal["validate", "extract"],
) -> str:
    settings = load_settings()
    started = time.monotonic()
    acquired = _PARSER_SLOT.acquire(timeout=settings.OUROBOROS_ATTACHMENT_SLOT_TIMEOUT_SEC)
    _log_parser_admission(mime, mode, started, "acquired" if acquired else "timeout")
    if not acquired:
        raise _parser_unavailable(path.name, "waiting for a parser slot timed out")
    try:
        return _run_parser_process(path, mime, mode)
    finally:
        _PARSER_SLOT.release()


def _log_parser_admission(mime: str, mode: str, started: float, outcome: str) -> None:
    log.info(
        "event=attachment_parser_admission mime=%s mode=%s outcome=%s wait_ms=%.3f",
        mime,
        mode,
        outcome,
        (time.monotonic() - started) * 1000,
    )


def _phase_ms(seconds: float | None) -> str:
    return "unknown" if seconds is None else f"{seconds * 1000:.3f}"


def _run_parser_process(path: pathlib.Path, mime: str, mode: str) -> str:
    settings = load_settings()
    started = time.monotonic()
    deadline = started + settings.OUROBOROS_ATTACHMENT_PARSE_TIMEOUT_SEC
    join_timeout = settings.OUROBOROS_ATTACHMENT_PARSER_JOIN_TIMEOUT_SEC
    process_context = multiprocessing.get_context("spawn")
    result: ParserResult | None = None
    received_at: float | None = None
    outcome = "error"
    receive, send = process_context.Pipe(duplex=False)
    with receive, send:
        process = process_context.Process(
            target=parser_child,
            args=(send, str(path), str(mime), mode, settings.OUROBOROS_ATTACHMENT_PARSER_CPU_SEC),
            daemon=True,
        )
        try:
            process.start()
            send.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not receive.poll(remaining):
                outcome = "timeout"
                raise _parser_unavailable(path.name, "attachment parsing timed out")
            result = receive.recv()
            received_at = time.monotonic()
            outcome = result.status
        except EOFError as exc:
            outcome = "exited"
            raise AttachmentError(
                "invalid_attachment", "attachment parser exited unexpectedly", filename=path.name
            ) from exc
        finally:
            cleanup_started = time.monotonic()
            if process.pid is not None:
                process.join(timeout=join_timeout)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=join_timeout)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=join_timeout)
            process.close()
            finished = time.monotonic()
            log.info(
                "event=attachment_parser_finished mime=%s mode=%s outcome=%s "
                "startup_ms=%s import_ms=%s parse_ms=%s transfer_ms=%s cleanup_ms=%.3f total_ms=%.3f",
                mime,
                mode,
                outcome,
                _phase_ms(result.started_at - started if result is not None else None),
                _phase_ms(result.import_seconds if result is not None else None),
                _phase_ms(result.parse_seconds if result is not None else None),
                _phase_ms(received_at - result.finished_at if result is not None and received_at is not None else None),
                (finished - cleanup_started) * 1000,
                (finished - started) * 1000,
            )
    if result is None:
        raise _parser_unavailable(path.name, "attachment parser returned no result")
    if result.status == "ok":
        return result.payload
    if result.status == "unavailable":
        raise _parser_unavailable(path.name, f"attachment parser dependency is unavailable: {result.payload}")
    raise AttachmentError("invalid_attachment", result.payload, filename=path.name)


def validate_attachment_metadata(
    filename: str, declared_mime: str, size: int, *, stored: bool = False
) -> ValidatedAttachment:
    clean_name = re.sub(
        r"[\x00-\x1f\x7f]",
        "_",
        pathlib.PurePosixPath(str(filename or "").replace("\\", "/")).name,
    )
    mime = str(declared_mime or "").split(";", 1)[0].strip().lower()
    rule = (
        next((value for value in ATTACHMENT_FORMATS.values() if value.mime == mime), None)
        if stored
        else ATTACHMENT_FORMATS.get(pathlib.Path(clean_name).suffix.lower())
    )
    if rule is None:
        raise AttachmentError("attachment_type_not_allowed", "unsupported file format", filename=clean_name)
    if mime not in rule.aliases:
        raise AttachmentError(
            "attachment_type_not_allowed",
            "file extension and MIME type do not match",
            filename=clean_name,
        )
    limit = attachment_max_bytes(rule.mime)
    if size > limit:
        raise AttachmentError(
            "attachment_too_large",
            f"attachment exceeds {limit} bytes limit",
            filename=clean_name,
        )
    return ValidatedAttachment(clean_name, rule.mime, attachment_kind(rule.mime), size)


def _inspect_attachment_path(
    path: pathlib.Path, filename: str, declared_mime: str, *, stored: bool = False
) -> ValidatedAttachment:
    validated = validate_attachment_metadata(filename, declared_mime, path.stat().st_size, stored=stored)
    clean_name = validated.filename
    with path.open("rb") as handle:
        head = handle.read(16)
    if validated.kind == "image":
        if image_signature(head) != validated.mime:
            raise AttachmentError("invalid_attachment", "file content does not match image type", filename=clean_name)
    else:
        if validated.mime == "application/pdf" and not head.startswith(b"%PDF-"):
            raise AttachmentError("invalid_attachment", "file content does not match PDF", filename=clean_name)
        if validated.mime in {XLSX_MIME, DOCX_MIME, PPTX_MIME} and not head.startswith(b"PK"):
            raise AttachmentError("invalid_attachment", "file is not a valid OOXML package", filename=clean_name)
    return validated


def validate_attachment_path(
    path: pathlib.Path, filename: str, declared_mime: str, *, stored: bool = False
) -> ValidatedAttachment:
    validated = _inspect_attachment_path(path, filename, declared_mime, stored=stored)
    if validated.kind == "file":
        try:
            run_parser(path, validated.mime, mode="validate")
        except AttachmentError as exc:
            raise AttachmentError(exc.code, exc.reason, filename=validated.filename, retryable=exc.retryable) from exc
    return validated


async def validate_attachment_path_async(path: pathlib.Path, filename: str, declared_mime: str) -> ValidatedAttachment:
    validated = await anyio.to_thread.run_sync(_inspect_attachment_path, path, filename, declared_mime)
    if validated.kind == "image":
        return validated
    started = time.monotonic()
    deadline = started + load_settings().OUROBOROS_ATTACHMENT_SLOT_TIMEOUT_SEC
    try:
        while not _PARSER_SLOT.acquire(blocking=False):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log_parser_admission(validated.mime, "validate", started, "timeout")
                raise _parser_unavailable(validated.filename, "waiting for a parser slot timed out")
            await asyncio.sleep(min(0.05, remaining))
    except asyncio.CancelledError:
        _log_parser_admission(validated.mime, "validate", started, "cancelled")
        raise
    _log_parser_admission(validated.mime, "validate", started, "acquired")
    try:
        await finish_file_io(asyncio.to_thread(_run_parser_process, path, validated.mime, "validate"))
    except AttachmentError as exc:
        raise AttachmentError(exc.code, exc.reason, filename=validated.filename, retryable=exc.retryable) from exc
    finally:
        _PARSER_SLOT.release()
    return validated
