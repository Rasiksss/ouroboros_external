"""Owner messaging service tools."""

from __future__ import annotations

import logging
import mimetypes
import pathlib
import shutil
import time
import uuid

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.observability.event_record import record_event
from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.blob.keys import parse_data_rel_path
from ouroboros.storage.blob.protocol import ObjectTooLargeError
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import MAX_OUTBOUND_BYTES, AttachmentError, get_attachment, public_attachment
from ouroboros.tools.attachments import ValidatedAttachment, attachment_mime_for_suffix, validate_attachment_path
from ouroboros.utils import safe_relpath, utc_now_iso

log = logging.getLogger(__name__)


def _cleanup_outbound_staging(drive_root: pathlib.Path) -> None:
    from ouroboros.storage.settings.store import load_settings

    root = drive_root.resolve() / "tmp" / "outbound"
    if not root.is_dir():
        return
    try:
        ttl_hours = max(1, int(load_settings().OUROBOROS_JOB_SCRATCH_TTL_HOURS))
        cutoff = time.time() - ttl_hours * 3600
        for path in root.iterdir():
            if path.stat().st_mtime <= cutoff:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
    except OSError:
        log.warning("Failed to clean expired outbound staging", exc_info=True)


def _staged_path(ctx: ToolContext, filename: str) -> pathlib.Path:
    _cleanup_outbound_staging(pathlib.Path(ctx.drive_root))
    directory = pathlib.Path(ctx.drive_root).resolve() / "tmp" / "outbound" / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    return directory / pathlib.PurePosixPath(filename).name


def _deliver_media(
    ctx: ToolContext,
    data: bytes,
    validated: ValidatedAttachment,
    *,
    caption: str,
    image_only: bool,
) -> str:
    kind = "image" if image_only else "file"
    task_id = str(ctx.task_id or "")
    source = str(ctx.current_task_source or "")
    sender = ctx.media_senders.get(kind)
    log.info(
        "event=send_file_started task_id=%s source=%s filename=%s mime=%s size=%s",
        task_id,
        source,
        validated.filename,
        validated.mime,
        validated.size,
    )
    if not callable(sender):
        log.warning("event=send_file_failed task_id=%s source=%s reason=transport_unavailable", task_id, source)
        return "⚠️ Attachment transport is unavailable."
    kwargs = {
        "filename": validated.filename,
        "mime": validated.mime,
        "caption": caption or "",
        "source": source,
        "transport": dict(ctx.transport) if ctx.transport else None,
    }
    if not image_only:
        kwargs["task_id"] = task_id
    try:
        result = sender(ctx.current_chat_id, data, **kwargs)
    except Exception as exc:
        log.warning(
            "event=send_file_failed task_id=%s source=%s exception_type=%s", task_id, source, type(exc).__name__
        )
        return "⚠️ Attachment delivery failed."
    if not (
        isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool) and isinstance(result[1], str)
    ):
        log.warning("event=send_file_failed task_id=%s source=%s reason=invalid_transport_result", task_id, source)
        return "⚠️ Attachment delivery failed: invalid transport result."
    ok, detail = result
    if not ok:
        if detail == DELIVERY_UNKNOWN:
            log.warning("event=send_file_failed task_id=%s source=%s reason=delivery_unknown", task_id, source)
            return "⚠️ Attachment delivery is unconfirmed after timeout; do not retry automatically."
        log.warning("event=send_file_failed task_id=%s source=%s reason=transport_rejected", task_id, source)
        return "⚠️ Attachment delivery failed."
    log.info("event=send_file_succeeded task_id=%s source=%s filename=%s", task_id, source, validated.filename)
    return "OK: attachment delivered."


def _attachment_error(exc: AttachmentError) -> str:
    return f"⚠️ {exc.filename or 'Attachment'}: {exc.reason}"


def _materialize_data_artifact(
    ctx: ToolContext,
    file_path: str,
    workspace: pathlib.Path,
) -> tuple[pathlib.Path | None, str]:
    raw_path = pathlib.Path(file_path).expanduser()
    drive_root = pathlib.Path(ctx.drive_root).resolve()
    try:
        rel = (
            raw_path.resolve(strict=False).relative_to(drive_root).as_posix()
            if raw_path.is_absolute()
            else pathlib.PurePosixPath(safe_relpath(file_path)).as_posix()
        )
        scope, key = parse_data_rel_path(rel)
    except ValueError:
        return None, ""
    if scope == SCOPE_UPLOADS and key.startswith("chat/"):
        return None, ""

    from ouroboros.storage.blob.drive import get_drive_storage

    drive = get_drive_storage(drive_root)
    try:
        if not drive.provider.exists(scope, key):
            return None, f"⚠️ File not found: {file_path}"
        provenance = drive.provider.get_json(scope, key + ".metadata.json") or {}
        task_id = str(ctx.task_id or "")
        if not task_id or not (
            provenance.get("origin") == "agent_generated"
            and provenance.get("purpose") == "artifact"
            and provenance.get("created_by_tool") == "data_write"
            and str(provenance.get("task_id") or "") == task_id
        ):
            return None, "⚠️ File must be inside the current task workspace or created by the current task."
        target = workspace / ".storage" / uuid.uuid4().hex / pathlib.PurePosixPath(key).name
        drive.provider.copy_to_path(scope, key, target, max_bytes=MAX_OUTBOUND_BYTES)
        return target, ""
    except ObjectTooLargeError:
        return None, f"⚠️ Attachment exceeds the {MAX_OUTBOUND_BYTES // (1024 * 1024)} MB limit."
    except Exception as exc:
        record_event(
            drive_root,
            "tools",
            "send_file_materialization_error",
            {
                "ts": utc_now_iso(),
                "task_id": str(ctx.task_id or ""),
                "stage": "storage_materialization",
                "exception_type": type(exc).__name__,
            },
        )
        return None, "⚠️ Stored file is temporarily unavailable."


def _materialize_chat_attachment(
    ctx: ToolContext,
    attachment_id: str,
    workspace: pathlib.Path,
) -> tuple[pathlib.Path | None, str, str]:
    try:
        try:
            raw_path = pathlib.Path(attachment_id).expanduser()
            rel = (
                raw_path.resolve(strict=False).relative_to(pathlib.Path(ctx.drive_root).resolve()).as_posix()
                if raw_path.is_absolute()
                else pathlib.PurePosixPath(safe_relpath(attachment_id)).as_posix()
            )
            scope, key = parse_data_rel_path(rel)
            if scope == SCOPE_UPLOADS and key.startswith("chat/"):
                attachment_id = pathlib.PurePosixPath(key).parts[1]
        except ValueError:
            pass
        provider = create_storage_provider(data_dir=ctx.drive_root)
        metadata = get_attachment(provider, attachment_id)
        if metadata is None or public_attachment(metadata).get("status") != "ready":
            return None, "", ""
        target = workspace / ".storage" / uuid.uuid4().hex / pathlib.PurePosixPath(metadata["filename"]).name
        provider.copy_to_path(SCOPE_UPLOADS, metadata["storage_key"], target, max_bytes=MAX_OUTBOUND_BYTES)
        return target, str(metadata.get("mime") or ""), ""
    except ObjectTooLargeError:
        return None, "", f"⚠️ Attachment exceeds the {MAX_OUTBOUND_BYTES // (1024 * 1024)} MB limit."
    except FileNotFoundError:
        return None, "", ""
    except Exception as exc:
        log.warning("Failed to materialize stored attachment (%s)", type(exc).__name__)
        return None, "", "⚠️ Stored file is temporarily unavailable."


def _send_path(
    ctx: ToolContext,
    *,
    file_path: str,
    caption: str,
    image_only: bool,
) -> str:
    if ctx.current_chat_id in (None, ""):
        return "⚠️ No active chat - cannot send attachment."
    if ctx.task_workspace is None:
        return "⚠️ No active task workspace - cannot send attachment."
    candidate = pathlib.Path(file_path).expanduser().absolute()
    workspace = pathlib.Path(ctx.task_workspace).expanduser().resolve()
    stored_mime = ""
    try:
        relative = candidate.relative_to(workspace)
        if not candidate.is_file():
            raise FileNotFoundError(file_path)
    except (FileNotFoundError, ValueError):
        materialized, error = _materialize_data_artifact(ctx, file_path, workspace)
        if materialized is None and not error:
            materialized, stored_mime, error = _materialize_chat_attachment(ctx, file_path, workspace)
        if materialized is None:
            if error:
                return error
            if pathlib.Path(file_path).expanduser().absolute().is_file():
                return "⚠️ File must be inside the current task workspace."
            return f"⚠️ File not found: {file_path}"
        candidate = materialized
        relative = candidate.relative_to(workspace)
    if not candidate.is_file():
        return f"⚠️ File not found: {file_path}"
    current = workspace
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return "⚠️ Symlinked artifacts cannot be sent."
    path = candidate.resolve()
    if not path.is_relative_to(workspace):
        return "⚠️ File must be inside the current task workspace."
    mime = (
        stored_mime
        or attachment_mime_for_suffix(path.name)
        or str(mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    )
    try:
        validated = validate_attachment_path(
            path,
            path.name,
            mime,
            image_max_bytes=MAX_OUTBOUND_BYTES,
        )
    except AttachmentError as exc:
        return _attachment_error(exc)
    if image_only and validated.kind != "image":
        return "⚠️ Unsupported image format. Use PNG, JPEG, WEBP, or GIF."
    return _deliver_media(ctx, path.read_bytes(), validated, caption=caption, image_only=image_only)


def _send_image_bytes(
    ctx: ToolContext,
    data: bytes,
    *,
    filename: str,
    mime: str,
    caption: str,
) -> str:
    if ctx.current_chat_id in (None, ""):
        return "⚠️ No active chat - cannot send attachment."
    if ctx.task_workspace is None:
        return "⚠️ No active task workspace - cannot send attachment."
    staged_path = _staged_path(ctx, filename)
    try:
        staged_path.write_bytes(data)
        validated = validate_attachment_path(
            staged_path,
            filename,
            mime,
            image_max_bytes=MAX_OUTBOUND_BYTES,
        )
        if validated.kind != "image":
            raise AttachmentError(
                "attachment_type_not_allowed",
                "unsupported image format",
                filename=filename,
            )
    except AttachmentError as exc:
        return _send_result(_attachment_error(exc), "SEND_IMAGE")
    finally:
        shutil.rmtree(staged_path.parent, ignore_errors=True)
    return _send_result(_deliver_media(ctx, data, validated, caption=caption, image_only=True), "SEND_IMAGE")


def _send_result(result: str, error_prefix: str) -> str:
    if not result.startswith("⚠️"):
        return result
    lowered = result.lower()
    codes = {
        "stored file is temporarily unavailable": "STORAGE_ERROR",
        "delivery failed": "DELIVERY_ERROR",
        "unconfirmed after timeout": "DELIVERY_UNKNOWN",
        "transport is unavailable": "DELIVERY_ERROR",
        "not found": "NOT_FOUND",
        "must be inside": "NOT_OWNED",
        "unsupported": "TYPE_NOT_ALLOWED",
        "not allowed": "TYPE_NOT_ALLOWED",
        "exceeds": "TOO_LARGE",
    }
    code = next((value for marker, value in codes.items() if marker in lowered), "REJECTED")
    return f"⚠️ {error_prefix}_{code}: {result.removeprefix('⚠️ ').strip()}"


def _send_image(ctx: ToolContext, file_path: str, caption: str = "") -> str:
    return _send_result(_send_path(ctx, file_path=file_path, caption=caption, image_only=True), "SEND_IMAGE")


def _send_file(ctx: ToolContext, file_path: str, caption: str = "") -> str:
    return _send_result(_send_path(ctx, file_path=file_path, caption=caption, image_only=False), "SEND_FILE")


def _send_user_message(ctx: ToolContext, text: str, reason: str = "") -> str:
    if ctx.current_chat_id in (None, ""):
        return "⚠️ No active chat - cannot send proactive message."
    if not text or not text.strip():
        return "⚠️ Empty message."

    ctx.pending_events.append(
        {
            "type": "send_message",
            "chat_id": ctx.current_chat_id,
            "text": text,
            "format": "markdown",
            "is_progress": False,
            "source": str(ctx.current_task_source or ""),
            "ts": utc_now_iso(),
            **({"transport": dict(ctx.transport)} if ctx.transport else {}),
        }
    )
    record_event(
        ctx.drive_root,
        "events",
        "proactive_message",
        {"ts": utc_now_iso(), "reason": reason, "text_preview": text[:200]},
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
                description="Send a PNG, JPEG, WEBP, or GIF from the task workspace, data_write, or a stored attachment ID.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Task workspace path, data_write path from this task, or stored attachment ID",
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
                description="Send a file from the current task workspace, created by the current task with data_write, or referenced by a stored attachment ID.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Current task file, data_write artifact from this task, or stored attachment ID",
                        },
                        "caption": {"type": "string", "description": "Optional caption"},
                    },
                    "required": ["file_path"],
                },
            ),
            _send_file,
        ),
    ]
