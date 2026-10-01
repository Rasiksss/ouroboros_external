"""Browser chat history and upload HTTP handlers."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import pathlib
import re
import shutil
import tempfile
import urllib.parse
import uuid
from typing import Any

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ouroboros.gateway._helpers import coerce_int, json_error, request_drive_root
from ouroboros.gateway.contracts import ChatHistoryResponse, StatusResponse, UploadResponse
from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import (
    MAX_DOCUMENT_BYTES,
    SAFE_INLINE_IMAGE_MIMES,
    AttachmentError,
    delete_attachment_metadata,
    delete_draft_attachment,
    get_attachment,
    public_attachment,
    store_attachment_path,
)
from ouroboros.storage.chat_store import list_owner_messages
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.tools.attachments import validate_attachment_path

log = logging.getLogger(__name__)

CHAT_UPLOAD_MAX_BYTES = MAX_DOCUMENT_BYTES
_UPLOAD_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_UPLOAD_LIMIT_DETAIL = "upload body exceeded limit"
_HISTORY_DEFAULT_LIMIT = 1000
_HISTORY_MAX_LIMIT = 2000
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CHAT_FILES_PREFIX = "api/chat/files/"
_META_PREFIX = "_meta/"
_PUBLIC_META_PREFIX = "_meta/public/"
_ATTACHMENT_ERROR_STATUS = {
    "attachment_too_large": 413,
    "attachment_type_not_allowed": 415,
    "invalid_attachment": 422,
    "attachment_parser_unavailable": 503,
}


def _attachment_error_response(exc: AttachmentError) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "code": exc.code,
            "message": exc.reason,
            "filename": exc.filename,
            "retryable": exc.retryable,
        },
        status_code=_ATTACHMENT_ERROR_STATUS.get(exc.code, 422),
    )


def _actor_role(actor_type: str) -> str | None:
    if actor_type == "owner":
        return "user"
    if actor_type == "agent":
        return "assistant"
    if actor_type == "system":
        return "system"
    if actor_type == "service":
        return "assistant"
    return None


def _message_to_history_record(message: ConversationMessage) -> dict[str, Any]:
    role = _actor_role(str(message.actor_type or ""))
    if role is None:
        return {}
    metadata = dict(message.metadata or {})
    is_progress = bool(metadata.get("is_progress"))
    record: dict[str, Any] = {
        "text": message.display_text(),
        "role": role,
        "ts": str(message.created_at or ""),
        "is_progress": is_progress,
        "markdown": str(metadata.get("format") or "").lower() == "markdown",
        "source": str(message.source or metadata.get("source") or ""),
        "sender_label": str(metadata.get("sender_label") or ""),
        "sender_session_id": str(metadata.get("sender_session_id") or ""),
        "client_message_id": str(message.source_message_id or message.message_id or ""),
        "task_id": str(metadata.get("task_id") or ""),
    }
    if metadata.get("type"):
        record["system_type"] = str(metadata["type"])
    raw_attachments = metadata.get("attachments")
    if isinstance(raw_attachments, list):
        attachments = [
            public for item in raw_attachments if isinstance(item, dict) and (public := public_attachment(item))
        ]
        if attachments:
            record["attachments"] = attachments
    return record


def _sanitize_upload_filename(filename: str) -> str:
    raw = (filename or "").replace("\\", "/").strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "_", os.path.basename(raw)).replace(" ", "_")[:200].strip()
    if not name or name in {".", ".."}:
        raise ValueError("invalid filename")
    if "/" in name or "\\" in name:
        raise ValueError("invalid filename")
    return name


def _guess_upload_mime(filename: str, declared: str | None) -> str:
    declared_mime = str(declared or "").split(";", 1)[0].strip().lower()
    guessed, _ = mimetypes.guess_type(filename)
    guessed_mime = str(guessed or "").strip().lower()
    if declared_mime:
        return declared_mime
    return guessed_mime or declared_mime or "application/octet-stream"


def _sniff_safe_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _upload_storage_key(upload_id: str, filename: str) -> str:
    return f"chat/{upload_id}/{filename}"


def _upload_meta_key(storage_key: str) -> str:
    return f"{_META_PREFIX}{storage_key}.json"


def _public_meta_key(public_name: str) -> str:
    return f"{_PUBLIC_META_PREFIX}{public_name}.json"


def _chat_file_path(upload_id: str, filename: str) -> str:
    return f"{_CHAT_FILES_PREFIX}{upload_id}/{urllib.parse.quote(filename, safe='')}"


def _safe_download_name(name: str) -> str:
    clean = _sanitize_upload_filename(name)
    return clean or "download"


def _content_disposition(filename: str, *, inline: bool = False) -> str:
    name = _safe_download_name(filename)
    ascii_name = name.encode("ascii", "ignore").decode("ascii") or "download"
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_")
    quoted = urllib.parse.quote(name)
    disposition = "inline" if inline else "attachment"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _clean_file_ref(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip().strip("'\"")
    if not value:
        return "", "missing file path"
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.path:
        value = parsed.path
    value = urllib.parse.unquote(value).strip().lstrip("/")
    if value.startswith(_CHAT_FILES_PREFIX):
        value = value[len(_CHAT_FILES_PREFIX) :]
    if value.startswith("uploads/"):
        value = value[len("uploads/") :]
    parts = [part for part in value.split("/") if part]
    if not parts:
        return "", "missing file path"
    if any(part == ".." for part in parts):
        return "", "invalid file path"
    if parts[0] == "_meta":
        return "", "invalid file path"
    return "/".join(parts), ""


def _guess_content_type(filename: str, metadata: dict[str, Any] | None = None) -> str:
    if metadata:
        stored = str(metadata.get("content_type") or metadata.get("mime") or "").strip()
        if stored:
            return stored
    guessed, _ = mimetypes.guess_type(filename)
    return str(guessed or "application/octet-stream")


def _metadata_for_key(provider: Any, storage_key: str) -> dict[str, Any] | None:
    meta = provider.get_json(SCOPE_UPLOADS, _upload_meta_key(storage_key))
    return meta if isinstance(meta, dict) else None


def _resolve_storage_key(provider: Any, raw_path: str) -> tuple[str, dict[str, Any] | None, str]:
    path, error = _clean_file_ref(raw_path)
    if error:
        return "", None, error

    parts = path.split("/")
    if len(parts) == 1:
        attachment = get_attachment(provider, parts[0])
        if attachment is not None:
            storage_key = str(attachment.get("storage_key") or "").strip()
            if storage_key and not storage_key.startswith(_META_PREFIX):
                return storage_key, dict(attachment), ""
    if len(parts) >= 2 and _UPLOAD_ID_RE.fullmatch(parts[0]):
        storage_key = _upload_storage_key(parts[0], "/".join(parts[1:]))
        attachment = get_attachment(provider, parts[0])
        if attachment is not None and attachment.get("storage_key") == storage_key:
            return storage_key, dict(attachment), ""
        return storage_key, _metadata_for_key(provider, storage_key), ""

    if path.startswith("chat/"):
        chat_parts = path.split("/", 2)
        if len(chat_parts) == 3:
            attachment = get_attachment(provider, chat_parts[1])
            if attachment is not None and attachment.get("storage_key") == path:
                return path, dict(attachment), ""
        return path, _metadata_for_key(provider, path), ""

    if path.startswith("generated/"):
        return path, _metadata_for_key(provider, path), ""

    public_name = os.path.basename(path)
    public_meta = provider.get_json(SCOPE_UPLOADS, _public_meta_key(public_name))
    if isinstance(public_meta, dict):
        storage_key = str(public_meta.get("storage_key") or "").strip()
        if storage_key and not storage_key.startswith(_META_PREFIX):
            return storage_key, public_meta, ""

    if provider.exists(SCOPE_UPLOADS, path):
        return path, _metadata_for_key(provider, path), ""

    matches: list[str] = []
    for prefix in ("chat/", "generated/"):
        keys = provider.list_keys(SCOPE_UPLOADS, prefix)
        matches.extend(key for key in keys if os.path.basename(key) == public_name and not key.startswith(_META_PREFIX))
    unique = sorted(set(matches))
    if len(unique) == 1:
        storage_key = unique[0]
        return storage_key, _metadata_for_key(provider, storage_key), ""
    if len(unique) > 1:
        return "", None, "ambiguous file path"
    return "", None, "file not found"


def _storage_provider_for_request(request: Request):
    drive_root = request_drive_root(request)
    return create_storage_provider(data_dir=drive_root)


async def _request_storage_provider(request: Request) -> Any | None:
    try:
        return await asyncio.to_thread(_storage_provider_for_request, request)
    except Exception:
        log.warning("Failed to initialize upload storage", exc_info=True)
        return None


def _file_size_and_rewind(fileobj: Any) -> int:
    fileobj.seek(0, os.SEEK_END)
    size = int(fileobj.tell())
    fileobj.seek(0)
    return size


def _copy_upload(fileobj: Any, destination: pathlib.Path) -> None:
    fileobj.seek(0)
    with destination.open("wb") as output:
        shutil.copyfileobj(fileobj, output, length=64 * 1024)


def _limited_multipart_request(request: Request, *, max_body_bytes: int) -> Request:
    received = 0

    async def receive() -> dict[str, Any]:
        nonlocal received
        message = await request.receive()
        if message.get("type") == "http.request":
            received += len(message.get("body") or b"")
            if received > max_body_bytes:
                raise MultiPartException(_UPLOAD_LIMIT_DETAIL)
        return dict(message)

    scope = dict(request.scope)
    scope.pop("app", None)
    return Request(scope, receive)


def _history_page(
    drive_root: pathlib.Path,
    *,
    limit: int,
    before_ts: str,
) -> tuple[list[ConversationMessage], bool]:
    """The newest ``limit`` messages older than ``before_ts``, and whether more remain.

    The window is taken from the newest end: the store cuts a plain ``limit``
    from the oldest one, which would serve a stale page and leave everything the
    user just said unreachable. ``before_ts`` has no SQL predicate behind it, so
    it is applied here over a window that widens until it holds a full page —
    without that, paging backwards stops at the first fetch instead of the
    oldest message.
    """
    fetch = max(limit, 1) + 1
    previous = -1
    while True:
        messages = list_owner_messages(
            drive_root,
            limit=fetch,
            include_progress=True,
            newest_first=True,
        )
        window = (
            [message for message in messages if str(message.created_at or "") < before_ts] if before_ts else messages
        )
        has_more = len(window) > limit
        # A window the store could not fill is the whole store; so is one that
        # stopped growing, which is what a row dropped after the LIMIT looks like.
        if has_more or len(messages) < fetch or len(messages) == previous:
            return (window[-limit:] if limit else []), has_more
        previous = len(messages)
        fetch *= 2


async def api_chat_history(request: Request) -> JSONResponse:
    try:
        limit = coerce_int(request.query_params.get("limit"), _HISTORY_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = _HISTORY_DEFAULT_LIMIT
    limit = max(0, min(limit, _HISTORY_MAX_LIMIT))
    before_ts = str(request.query_params.get("before_ts") or "").strip()

    drive_root = request_drive_root(request)
    try:
        window, has_more = await asyncio.to_thread(
            _history_page,
            drive_root,
            limit=limit,
            before_ts=before_ts,
        )
    except Exception as exc:
        log.warning("Failed to read browser chat history: %s", exc)
        return json_error("failed to load chat history", status=500)

    next_before_ts = str(window[0].created_at or "") if has_more and window else ""

    records = [record for message in window if (record := _message_to_history_record(message))]
    payload: ChatHistoryResponse = {
        "messages": records,
    }
    if has_more:
        payload["has_more"] = True
        payload["next_before_ts"] = next_before_ts
    return JSONResponse(payload)


async def api_chat_upload(request: Request) -> JSONResponse:
    try:
        content_length = int(request.headers.get("content-length", 0) or 0)
    except (TypeError, ValueError):
        content_length = 0
    max_body_bytes = CHAT_UPLOAD_MAX_BYTES + _UPLOAD_MULTIPART_OVERHEAD_BYTES
    if content_length > max_body_bytes:
        return _attachment_error_response(AttachmentError("attachment_too_large", "file exceeds upload limit"))

    try:
        limited_request = _limited_multipart_request(request, max_body_bytes=max_body_bytes)
        form = await limited_request.form(
            max_files=1,
            max_fields=8,
            max_part_size=_UPLOAD_MULTIPART_OVERHEAD_BYTES,
        )
    except MultiPartException as exc:
        if exc.message == _UPLOAD_LIMIT_DETAIL:
            return _attachment_error_response(AttachmentError("attachment_too_large", "file exceeds upload limit"))
        return json_error("invalid multipart request", status=400)
    except Exception:
        return json_error("invalid multipart request", status=400)

    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return json_error("missing file upload", status=400)

    try:
        safe_name = _sanitize_upload_filename(upload.filename or "")
    except ValueError as exc:
        await upload.close()
        return json_error(str(exc), status=400)

    mime = _guess_upload_mime(safe_name, upload.content_type)
    temp_path: pathlib.Path | None = None
    try:
        bytes_written = await asyncio.to_thread(_file_size_and_rewind, upload.file)
        if bytes_written > CHAT_UPLOAD_MAX_BYTES:
            raise AttachmentError("attachment_too_large", "file exceeds upload limit", filename=safe_name)
        file_descriptor, raw_temp_path = tempfile.mkstemp(
            prefix="ouroboros-upload-",
            suffix=pathlib.Path(safe_name).suffix[:16],
        )
        os.close(file_descriptor)
        temp_path = pathlib.Path(raw_temp_path)
        await asyncio.to_thread(_copy_upload, upload.file, temp_path)
        validated = await asyncio.to_thread(
            validate_attachment_path,
            temp_path,
            safe_name,
            mime,
        )
        upload_id = uuid.uuid4().hex
        provider = await _request_storage_provider(request)
        if provider is None:
            return json_error("upload storage unavailable", status=500)
        metadata = await asyncio.to_thread(
            store_attachment_path,
            provider,
            attachment_id=upload_id,
            filename=validated.filename,
            mime=validated.mime,
            path=temp_path,
            source="browser",
            status="draft",
        )
    except AttachmentError as exc:
        return _attachment_error_response(exc)
    except Exception as exc:
        log.warning("Failed to process chat upload: %s", exc)
        return json_error("failed to store upload", status=500)
    finally:
        await upload.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    payload: UploadResponse = {
        "ok": True,
        "upload_id": upload_id,
        "filename": safe_name,
        "display_name": safe_name,
        "path": _chat_file_path(upload_id, safe_name),
        "mime": str(metadata["mime"]),
        "size": bytes_written,
        "attachment_id": upload_id,
        "status": str(metadata["status"]),
        "expires_at": str(metadata["expires_at"]),
        "url": _chat_file_path(upload_id, safe_name),
    }
    return JSONResponse(payload)


async def api_chat_file_download(request: Request) -> Response:
    provider = await _request_storage_provider(request)
    if provider is None:
        return json_error("upload storage unavailable", status=500)
    raw_path = str(request.path_params.get("path") or "")
    try:
        storage_key, metadata, error = await asyncio.to_thread(_resolve_storage_key, provider, raw_path)
    except Exception as exc:
        log.warning("Failed to resolve chat file %s: %s", raw_path, exc)
        return json_error("failed to resolve file", status=500)
    if error == "invalid file path":
        return json_error("invalid file path", status=400)
    if error:
        return json_error("file not found", status=404)
    if metadata and public_attachment(metadata).get("status") == "expired":
        return json_error("file not found", status=404)
    try:
        data = await asyncio.to_thread(provider.get_bytes, SCOPE_UPLOADS, storage_key)
    except Exception as exc:
        log.warning("Failed to read chat file %s: %s", storage_key, exc)
        return json_error("failed to read file", status=500)
    if data is None:
        return json_error("file not found", status=404)
    display_name = str(
        (metadata or {}).get("display_name")
        or (metadata or {}).get("filename")
        or os.path.basename(storage_key)
        or "download"
    )
    content_type = _guess_content_type(display_name, metadata)
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    safe_image = (
        normalized_content_type in SAFE_INLINE_IMAGE_MIMES and _sniff_safe_image_mime(data) == normalized_content_type
    )
    if normalized_content_type in SAFE_INLINE_IMAGE_MIMES and not safe_image:
        content_type = "application/octet-stream"
    inline = safe_image and str(request.query_params.get("download") or "").lower() not in {"1", "true", "yes"}
    return Response(
        data,
        media_type=content_type,
        headers={
            "Content-Disposition": _content_disposition(display_name, inline=inline),
            "X-Content-Type-Options": "nosniff",
        },
    )


async def api_chat_upload_delete(request: Request) -> JSONResponse:
    upload_id = str(request.query_params.get("id") or "").strip()
    if not upload_id:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        legacy_path = str(payload.get("path") or payload.get("filename") or "").strip()
        if not legacy_path:
            return json_error("missing upload id", status=400)
        provider = await _request_storage_provider(request)
        if provider is None:
            return json_error("upload storage unavailable", status=500)
        path, error = _clean_file_ref(legacy_path)
        if error == "invalid file path":
            return json_error("invalid file path", status=400)
        parts = path.split("/")
        if error or len(parts) < 2 or not _UPLOAD_ID_RE.fullmatch(parts[0]):
            return json_error("upload not found", status=404)
        attachment_id = parts[0]
        metadata = await asyncio.to_thread(get_attachment, provider, attachment_id)
        if metadata is None or metadata["storage_key"] != _upload_storage_key(attachment_id, "/".join(parts[1:])):
            return json_error("upload not found", status=404)
        try:
            deleted = await asyncio.to_thread(delete_draft_attachment, provider, attachment_id)
        except ValueError as exc:
            return json_error(str(exc), status=409)
        if not deleted:
            return json_error("upload not found", status=404)
        legacy_status: StatusResponse = {"status": "ok"}
        return JSONResponse(legacy_status)
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        return json_error("invalid upload id", status=400)

    provider = await _request_storage_provider(request)
    if provider is None:
        return json_error("upload storage unavailable", status=500)
    try:
        deleted = await asyncio.to_thread(delete_draft_attachment, provider, upload_id)
    except ValueError as exc:
        return json_error(str(exc), status=409)
    if deleted:
        draft_status: StatusResponse = {"status": "ok"}
        return JSONResponse(draft_status)
    prefix = f"chat/{upload_id}/"
    try:
        keys = await asyncio.to_thread(provider.list_keys, SCOPE_UPLOADS, prefix)
    except Exception as exc:
        log.warning("Failed to list upload keys for %s: %s", upload_id, exc)
        return json_error("failed to delete upload", status=500)

    if not keys:
        return json_error("upload not found", status=404)

    try:
        for key in keys:
            await asyncio.to_thread(provider.delete, SCOPE_UPLOADS, key)
        meta_keys = await asyncio.to_thread(
            provider.list_keys,
            SCOPE_UPLOADS,
            f"_meta/chat/{upload_id}/",
        )
        for meta_key in meta_keys:
            await asyncio.to_thread(provider.delete, SCOPE_UPLOADS, meta_key)
        await asyncio.to_thread(delete_attachment_metadata, provider, upload_id)
    except Exception as exc:
        log.warning("Failed to delete upload %s: %s", upload_id, exc)
        return json_error("failed to delete upload", status=500)

    delete_status: StatusResponse = {"status": "ok"}
    return JSONResponse(delete_status)


__all__ = [
    "CHAT_UPLOAD_MAX_BYTES",
    "api_chat_file_download",
    "api_chat_history",
    "api_chat_upload",
    "api_chat_upload_delete",
]
