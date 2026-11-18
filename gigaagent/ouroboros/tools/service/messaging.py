"""Owner messaging service tools."""

from __future__ import annotations

import logging
import mimetypes
import pathlib
import tempfile
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import SQLAlchemyError

from new_core.identity.owner_scope import SurfaceId
from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.agent_events.interfaces import OutboundText
from ouroboros.contracts.attachments import (
    CONTENT_VALIDATION_VERSION,
    StoredFile,
    TerminalAttachment,
    public_attachment,
)
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.storage.attachment_validation import (
    AttachmentError,
    ValidatedAttachment,
    attachment_max_bytes,
    attachment_mime_for_suffix,
    image_signature,
    validate_attachment_metadata,
    validate_attachment_path,
)
from ouroboros.storage.blob.drive import get_drive_storage
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.protocol import ObjectTooLargeError
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import (
    delete_attachment_metadata,
    get_attachment,
    store_attachment_fileobj,
)
from ouroboros.storage.file_catalog import FileCatalog, FileError, FileTransferPending
from ouroboros.storage.file_transfers import get_transfer_limits
from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.tools.catalog import spec_for_tool
from ouroboros.tools.service.artifacts import record_workspace_source, resolve_artifact, workspace_source
from ouroboros.utils import file_sha256, utc_now_iso
from security_config.attachments import IMAGE_SUFFIXES

log = logging.getLogger(__name__)


def _deliver_media(
    ctx: ToolContext,
    data: StoredFile,
    validated: ValidatedAttachment,
    *,
    caption: str,
    image_only: bool,
) -> str:
    task_id = str(ctx.task_id or "")
    source = ctx.scope.surface_id
    if ctx.detached:
        try:
            metadata = get_attachment(ctx.scope, data.file_id)
            if metadata is None:
                return _send_error(image_only, "STORAGE_ERROR", "Stored file is temporarily unavailable.")
            ctx.terminal_attachments.append(TerminalAttachment(**public_attachment(metadata), caption=caption))
            return "OK: attachment saved for terminal delivery."
        except Exception:
            log.warning("Failed to prepare terminal attachment", exc_info=True)
            return _send_error(image_only, "STORAGE_ERROR", "Stored file is temporarily unavailable.")

    outbound = ctx.outbound
    log.info(
        "event=send_file_started task_id=%s source=%s filename=%s mime=%s size=%s",
        task_id,
        source,
        validated.filename,
        validated.mime,
        validated.size,
    )
    if outbound is None:
        log.warning("event=send_file_failed task_id=%s source=%s reason=transport_unavailable", task_id, source)
        return _send_error(image_only, "DELIVERY_ERROR", "Attachment transport is unavailable.")
    transport = dict(ctx.transport) if ctx.transport else None
    try:
        if image_only:
            result = outbound.send_image(
                data,
                scope=ctx.scope,
                filename=validated.filename,
                mime=validated.mime,
                caption=caption or "",
                source=source,
                transport=transport,
            )
        else:
            result = outbound.send_file(
                data,
                task_id=task_id,
                scope=ctx.scope,
                filename=validated.filename,
                mime=validated.mime,
                caption=caption or "",
                source=source,
                transport=transport,
            )
    except FileTransferPending:
        raise
    except Exception as exc:
        log.warning(
            "event=send_file_failed task_id=%s source=%s exception_type=%s", task_id, source, type(exc).__name__
        )
        return _send_error(image_only, "DELIVERY_ERROR", "Attachment delivery failed.")
    if not (
        isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool) and isinstance(result[1], str)
    ):
        log.warning("event=send_file_failed task_id=%s source=%s reason=invalid_transport_result", task_id, source)
        return _send_error(image_only, "DELIVERY_ERROR", "Attachment delivery failed: invalid transport result.")
    ok, detail = result
    if not ok:
        if detail == DELIVERY_UNKNOWN:
            log.warning("event=send_file_failed task_id=%s source=%s reason=delivery_unknown", task_id, source)
            return _send_error(
                image_only,
                "DELIVERY_UNKNOWN",
                "Attachment delivery is unconfirmed after timeout; do not retry automatically.",
            )
        log.warning("event=send_file_failed task_id=%s source=%s reason=transport_rejected", task_id, source)
        return _send_error(image_only, "DELIVERY_ERROR", "Attachment delivery failed.")
    log.info("event=send_file_succeeded task_id=%s source=%s filename=%s", task_id, source, validated.filename)
    return "OK: attachment accepted for delivery."


def _attachment_error(exc: AttachmentError, image_only: bool) -> str:
    if exc.code in {"attachment_parser_unavailable", "invalid_attachment"}:
        return f"⚠️ TOOL_{exc.code.upper()}: retryable={str(exc.retryable).lower()}; {exc.filename or 'Attachment'}: {exc.reason}"
    code = {
        "attachment_not_found": "NOT_FOUND",
        "attachment_type_not_allowed": "TYPE_NOT_ALLOWED",
        "attachment_too_large": "TOO_LARGE",
    }.get(exc.code, "REJECTED")
    return _send_error(image_only, code, f"{exc.filename or 'Attachment'}: {exc.reason}")


def _send_path(ctx: ToolContext, *, file_path: str, caption: str, image_only: bool) -> str:
    try:
        return _prepare_path(ctx, file_path=file_path, caption=caption, image_only=image_only)
    except AttachmentError as exc:
        return _attachment_error(exc, image_only)
    except ObjectTooLargeError as exc:
        return _send_error(image_only, "TOO_LARGE", str(exc))
    except FileTransferPending as exc:
        return _send_error(
            image_only,
            "TRANSFER_PENDING",
            f"retryable=true; {exc}; retry with file_path={file_path} while this task is active.",
        )
    except (FileError, OSError, SQLAlchemyError, BotoCoreError, ClientError) as exc:
        ctx.events.audit(
            "tools",
            "artifact_materialization_error",
            {
                "task_id": str(ctx.task_id or ""),
                "stage": "storage_materialization",
                "exception_type": type(exc).__name__,
            },
            scope=ctx.scope,
        )
        return _send_error(image_only, "STORAGE_ERROR", f"Stored file is temporarily unavailable: {type(exc).__name__}")


def _prepare_path(
    ctx: ToolContext,
    *,
    file_path: str,
    caption: str,
    image_only: bool,
) -> str:
    if ctx.task_workspace is None:
        return _send_error(image_only, "REJECTED", "No active task workspace - cannot send attachment.")
    candidate = pathlib.Path(file_path).expanduser().absolute()
    workspace = pathlib.Path(ctx.task_workspace).expanduser().resolve()

    metadata = (
        workspace_source(ctx, candidate)
        if candidate.is_file() and candidate.is_relative_to(workspace)
        else resolve_artifact(ctx, file_path)
    )
    if metadata is not None:
        validated = validate_attachment_metadata(metadata["filename"], metadata["mime"], metadata["size"], stored=True)
        if image_only and validated.kind != "image":
            return _send_error(image_only, "TYPE_NOT_ALLOWED", "Unsupported image format. Use PNG, JPEG, WEBP, or GIF.")
        if metadata.get("validation_version", 0) != CONTENT_VALIDATION_VERSION:
            with tempfile.TemporaryDirectory(prefix="ouroboros-send-") as temp_dir:
                stored_path = pathlib.Path(temp_dir) / ("content" + pathlib.Path(metadata["filename"]).suffix)
                with get_transfer_limits().slot(ctx.scope):
                    get_drive_storage(ctx.drive_root).provider.copy_to_path(
                        ctx.scope,
                        SCOPE_UPLOADS,
                        metadata["storage_key"],
                        stored_path,
                        max_bytes=attachment_max_bytes(validated.mime),
                    )
                validated = validate_attachment_path(stored_path, validated.filename, validated.mime, stored=True)
                fingerprint = file_sha256(stored_path)
                with resolve_runtime_engine("validate file").begin() as conn:
                    FileCatalog(conn, ctx.scope).record_validation(
                        metadata["id"],
                        fingerprint=fingerprint,
                        size=validated.size,
                        version=CONTENT_VALIDATION_VERSION,
                    )
        return _deliver_media(ctx, StoredFile(metadata["id"]), validated, caption=caption, image_only=image_only)
    try:
        relative = candidate.relative_to(workspace)
    except ValueError:
        return _send_error(
            image_only, "NOT_OWNED", "File must be in the current task workspace or an accessible stored file."
        )
    if not candidate.is_file():
        return _send_error(image_only, "NOT_FOUND", f"File not found: {file_path}")
    current = workspace
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return _send_error(image_only, "REJECTED", "Symlinked artifacts cannot be sent.")
    path = candidate.resolve()
    if not path.is_relative_to(workspace):
        return _send_error(image_only, "NOT_OWNED", "File must be inside the current task workspace.")
    mime = attachment_mime_for_suffix(path.name) or str(
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )
    validated = validate_attachment_metadata(
        path.name,
        mime,
        path.stat().st_size,
    )
    if image_only and validated.kind != "image":
        return _send_error(image_only, "TYPE_NOT_ALLOWED", "Unsupported image format. Use PNG, JPEG, WEBP, or GIF.")
    with path.open("rb") as fileobj:
        metadata = store_attachment_fileobj(
            ctx.scope,
            get_storage_provider(),
            attachment_id=uuid.uuid4().hex,
            filename=validated.filename,
            mime=validated.mime,
            fileobj=fileobj,
            size=path.stat().st_size,
            status="ready",
            source=str(ctx.scope.surface_id),
            producer_task=str(ctx.task_id or ""),
            artifact_path="workspace/" + relative.as_posix(),
            validate_content=True,
        )
    validated = ValidatedAttachment(metadata["filename"], metadata["mime"], metadata["kind"], metadata["size"])
    result = _deliver_media(ctx, StoredFile(metadata["id"]), validated, caption=caption, image_only=image_only)
    if result.startswith(("OK:", "⚠️ SEND_IMAGE_DELIVERY_UNKNOWN:", "⚠️ SEND_FILE_DELIVERY_UNKNOWN:")):
        record_workspace_source(path, metadata["id"])
    else:
        delete_attachment_metadata(ctx.scope, metadata["id"])
    return result


def send_image_data(ctx: ToolContext, data: bytes, *, caption: str) -> str:
    if ctx.task_workspace is None:
        return "⚠️ No active task workspace - cannot send attachment."
    mime = image_signature(data)
    suffix = IMAGE_SUFFIXES.get(mime)
    if suffix is None:
        return "⚠️ Unsupported image format. Use PNG, JPEG, WEBP, or GIF."
    path = pathlib.Path(ctx.task_workspace) / f"image-{uuid.uuid4().hex}{suffix}"
    try:
        path.write_bytes(data)
    except OSError:
        return _send_error(True, "STORAGE_ERROR", "Unable to save image in task workspace.")
    return _send_path(ctx, file_path=str(path), caption=caption, image_only=True)


def _send_error(image_only: bool, code: str, reason: str) -> str:
    prefix = "SEND_IMAGE" if image_only else "SEND_FILE"
    return f"⚠️ {prefix}_{code}: {reason}"


def _send_image(ctx: ToolContext, file_path: str, caption: str = "") -> str:
    return _send_path(ctx, file_path=file_path, caption=caption, image_only=True)


def _send_file(ctx: ToolContext, file_path: str, caption: str = "") -> str:
    return _send_path(ctx, file_path=file_path, caption=caption, image_only=False)


def _send_user_message(ctx: ToolContext, text: str, reason: str = "") -> str:
    if not text or not text.strip():
        return "⚠️ Empty message."

    ctx.pending_events.append(
        OutboundText(
            scope=ctx.scope,
            text=text,
            fmt="markdown",
            is_progress=ctx.scope.surface_id is SurfaceId.A2A,
            source=str(ctx.scope.surface_id),
            transport=dict(ctx.transport) if ctx.transport else {},
            ts=utc_now_iso(),
        )
    )
    ctx.events.audit(
        "events",
        "proactive_message",
        {"ts": utc_now_iso(), "reason": reason, "text_preview": text[:200]},
        scope=ctx.scope,
    )
    return "OK: message queued for delivery."


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "send_user_message",
            ToolSchema(
                name="send_user_message",
                description="Send a proactive message to the user. Use for a worthwhile "
                "question, status update, or collaboration request. Task final "
                "answers are sent automatically.",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Message text"},
                        "reason": {"type": "string", "description": "Why this message is being sent"},
                    },
                    "required": ["text"],
                },
            ),
            _send_user_message,
        ),
        ToolEntry(
            "send_image",
            ToolSchema(
                name="send_image",
                description="Send a PNG, JPEG, WEBP, or GIF from the task workspace or an accessible stored file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Task workspace path, uploads/... path from this chat, or an accessible "
                            "attachment ID",
                        },
                        "caption": {"type": "string", "description": "Optional caption"},
                    },
                    "required": ["file_path"],
                },
            ),
            _send_image,
        ),
        ToolEntry(
            "send_file",
            ToolSchema(
                name="send_file",
                description="Send a file from the current task workspace or an accessible stored file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Task workspace path, uploads/... path from this chat, or an accessible "
                            "attachment ID",
                        },
                        "caption": {"type": "string", "description": "Optional caption"},
                    },
                    "required": ["file_path"],
                },
            ),
            _send_file,
            timeout_sec=spec_for_tool("send_file").timeout_sec,
        ),
    ]
