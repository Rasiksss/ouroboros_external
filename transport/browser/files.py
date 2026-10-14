from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import quote

import anyio
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette import status as http_status
from starlette._utils import collapse_excgroups
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from infra.async_engine import async_engine_factory
from new_core.identity import OwnerScope
from ouroboros.contracts.attachments import CONTENT_VALIDATION_VERSION
from ouroboros.contracts.owner_scope import CHAT_HEADER_NAME
from ouroboros.storage.attachment_validation import (
    AttachmentError,
    attachment_max_bytes,
    attachment_mime_for_suffix,
    image_signature,
    validate_attachment_path_async,
)
from ouroboros.storage.blob.async_backend import CHUNK_BYTES, AsyncBlob
from ouroboros.storage.chat_attachments import (
    file_transaction,
    stage_attachment,
    store_attachment_async,
)
from ouroboros.storage.file_catalog import FileCatalog, FileError, FileFailure, file_name
from ouroboros.storage.file_history import FileHistory, MessagePage
from ouroboros.storage.file_models import CatalogFile
from ouroboros.storage.file_transfers import get_transfer_limits
from ouroboros.storage.settings.store import load_settings
from ouroboros.utils import finish_file_io
from security_config.attachments import SAFE_INLINE_IMAGE_MIMES
from transport.browser.contracts import FileInfo
from transport.browser.identity import IdentityRefused, browser_identity

log = logging.getLogger(__name__)
UPLOAD_MULTIPART_OVERHEAD_BYTES = 65536
_HTTP_CLIENT_CLOSED_REQUEST = 499
_FILE_HTTP_STATUS = {
    FileFailure.NOT_FOUND: http_status.HTTP_404_NOT_FOUND,
    FileFailure.INVALID_INPUT: http_status.HTTP_400_BAD_REQUEST,
    FileFailure.CONFLICT: http_status.HTTP_409_CONFLICT,
    FileFailure.TOO_LARGE: http_status.HTTP_413_CONTENT_TOO_LARGE,
    FileFailure.TRANSFER_IN_PROGRESS: http_status.HTTP_409_CONFLICT,
    FileFailure.OWNER_CAPACITY: http_status.HTTP_429_TOO_MANY_REQUESTS,
    FileFailure.PROCESS_CAPACITY: http_status.HTTP_503_SERVICE_UNAVAILABLE,
}


def file_error_response(error: Exception) -> JSONResponse:
    retryable = not isinstance(error, FileError) or error.kind in {
        FileFailure.TRANSFER_IN_PROGRESS,
        FileFailure.OWNER_CAPACITY,
        FileFailure.PROCESS_CAPACITY,
    }
    return JSONResponse(
        {
            "ok": False,
            "code": ("attachment_too_large" if error.kind == FileFailure.TOO_LARGE else f"file_{error.kind.value}")
            if isinstance(error, FileError)
            else "file_storage_unavailable",
            "error": str(error) if isinstance(error, FileError) else "file storage unavailable",
            "retryable": retryable,
        },
        status_code=_FILE_HTTP_STATUS[error.kind]
        if isinstance(error, FileError)
        else http_status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": "1"} if retryable else None,
    )


class FilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    folder_id: str | None = None


class FolderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    parent_id: str | None = None


class FolderInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    parent_id: str | None


def file_errors(endpoint: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
    async def handle(request: Request) -> Response:
        try:
            return await endpoint(request)
        except ClientDisconnect:
            return Response(status_code=_HTTP_CLIENT_CLOSED_REQUEST)
        except IdentityRefused as error:
            return JSONResponse({"error": error.reason}, status_code=error.status)
        except AttachmentError as error:
            status = {
                "attachment_too_large": http_status.HTTP_413_CONTENT_TOO_LARGE,
                "attachment_type_not_allowed": http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "attachment_parser_unavailable": http_status.HTTP_503_SERVICE_UNAVAILABLE,
            }.get(error.code, http_status.HTTP_422_UNPROCESSABLE_CONTENT)
            return JSONResponse(
                {
                    "ok": False,
                    "code": error.code,
                    "message": error.reason,
                    "filename": error.filename,
                    "retryable": error.retryable,
                },
                status_code=status,
            )
        except FileError as error:
            return file_error_response(error)
        except (ValidationError, ValueError, MultiPartException):
            return JSONResponse({"ok": False, "error": "invalid request"}, status_code=http_status.HTTP_400_BAD_REQUEST)
        except (SQLAlchemyError, BotoCoreError, ClientError, OSError, TimeoutError) as error:
            log.warning("File operation failed", exc_info=True)
            return file_error_response(error)

    return handle


class LimitedMultipart(MultiPartParser):
    def __init__(self, request: Request) -> None:
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise FileError("invalid multipart request", FileFailure.INVALID_INPUT)
        self.file_bytes = 0
        settings = load_settings()
        maximum = max(settings.OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES, settings.OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES)

        async def bounded() -> AsyncGenerator[bytes, None]:
            received = 0
            stream = request.stream()
            while True:
                async with asyncio.timeout(settings.OUROBOROS_FILE_IO_TIMEOUT_SEC):
                    chunk = await anext(stream, None)
                if chunk is None:
                    break
                received += len(chunk)
                if received > maximum + UPLOAD_MULTIPART_OVERHEAD_BYTES:
                    raise AttachmentError("attachment_too_large", "upload body exceeded limit")
                for start in range(0, len(chunk), CHUNK_BYTES):
                    yield chunk[start : start + CHUNK_BYTES]

        super().__init__(request.headers, bounded(), max_files=1, max_fields=1, max_part_size=1024)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        file = self._current_part.file
        if file is not None:
            self.file_bytes += end - start
            mime = file.content_type or attachment_mime_for_suffix(file.filename or "")
            if self.file_bytes > attachment_max_bytes(mime):
                limit = attachment_max_bytes(mime)
                raise AttachmentError(
                    "attachment_too_large",
                    f"attachment exceeds {limit} bytes limit",
                    filename=Path(file.filename or "").name,
                )
        super().on_part_data(data, start, end)

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except BaseException:
            with anyio.CancelScope(shield=True):
                for file in self._files_to_close_on_error:
                    await anyio.wrap_file(file).aclose()
            raise


async def manager_scope(request: Request) -> OwnerScope:
    headers = dict(request.headers)
    headers.setdefault(CHAT_HEADER_NAME.lower(), "1")
    return await browser_identity.resolver.scope_for(headers)


async def upload(request: Request, *, draft: bool) -> Response:
    scope = await browser_identity.resolver.scope_for(request.headers) if draft else await manager_scope(request)
    with get_transfer_limits().client_slot():
        form = await LimitedMultipart(request).parse()
        file: CatalogFile | None = None
        with collapse_excgroups():
            async with anyio.create_task_group() as transfers:
                transfers.start_soon(_cancel_on_disconnect, request.receive, transfers.cancel_scope)
                try:
                    part = form.get("file")
                    if (
                        not isinstance(part, UploadFile)
                        or set(form) - {"file", "folder_id"}
                        or (draft and "folder_id" in form)
                    ):
                        raise FileError("invalid upload fields", FileFailure.INVALID_INPUT)
                    folder = form.get("folder_id")
                    if folder is not None and not isinstance(folder, str):
                        raise FileError("invalid folder", FileFailure.INVALID_INPUT)
                    raw_name = part.filename or ""
                    if draft:
                        raw_name = re.sub(r"[\s\x00-\x1f\x7f]", "_", Path(raw_name.replace("\\", "/")).name)
                    name = file_name(raw_name)
                    mime = part.content_type or attachment_mime_for_suffix(name)
                    async with AsyncExitStack() as staging:
                        directory = await staging.enter_async_context(
                            anyio.TemporaryDirectory(prefix="ouroboros-upload-")
                        )
                        started = time.perf_counter()
                        copied = validated_at = stored_at = None
                        phase, outcome = "copy", "ok"
                        try:
                            path = Path(directory) / ("file" + Path(name).suffix[:16])
                            size, fingerprint = await finish_file_io(
                                anyio.to_thread.run_sync(stage_attachment, part.file, path, name, mime)
                            )
                            copied = time.perf_counter()
                            phase = "validation"
                            validated = await validate_attachment_path_async(path, name, mime)
                            validated_at = time.perf_counter()
                            phase = "storage"
                            file = await store_attachment_async(
                                scope,
                                request.app.state.file_blob,
                                path,
                                filename=validated.filename,
                                mime=validated.mime,
                                size=size,
                                fingerprint=fingerprint,
                                validation_version=CONTENT_VALIDATION_VERSION,
                                folder_id=folder or None,
                                draft=draft,
                            )
                            stored_at = time.perf_counter()
                        except BaseException as error:
                            outcome = error.code if isinstance(error, AttachmentError) else type(error).__name__
                            raise
                        finally:
                            with anyio.CancelScope(shield=True):
                                await staging.aclose()
                            log.info(
                                "event=attachment_upload_finished phase=%s outcome=%s copy_ms=%s validation_ms=%s storage_ms=%s total_ms=%.3f",
                                phase,
                                outcome,
                                (copied - started) * 1000 if copied else None,
                                (validated_at - copied) * 1000 if validated_at and copied else None,
                                (stored_at - validated_at) * 1000 if stored_at and validated_at else None,
                                (time.perf_counter() - started) * 1000,
                            )
                finally:
                    with anyio.CancelScope(shield=True):
                        await form.close()
                transfers.cancel_scope.cancel()
        if file is None:
            raise ClientDisconnect()
    if not draft:
        return JSONResponse(FileInfo.model_validate(file).model_dump(), status_code=http_status.HTTP_201_CREATED)
    url = f"api/chat/files/{file.id}/{quote(file.name, safe='')}"
    return JSONResponse(
        {
            "ok": True,
            "upload_id": file.id,
            "attachment_id": file.id,
            "filename": file.name,
            "display_name": file.name,
            "path": url,
            "url": url,
            "mime": file.mime,
            "size": file.size,
            "status": file.status,
            "expires_at": file.expires_at,
        }
    )


def content_disposition(name: str, disposition: str = "attachment") -> str:
    safe = re.sub(r'["\\\x00-\x1f\x7f]', "_", name)
    ascii_name = safe.encode("ascii", "replace").decode("ascii")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe, safe='')}"


async def _cancel_on_disconnect(receive: Receive, scope: anyio.CancelScope) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass
    scope.cancel()


class FileContent(Response):
    def __init__(self, blob: AsyncBlob, owner: OwnerScope, file: CatalogFile, download: bool) -> None:
        super().__init__()
        self.blob, self.owner, self.file, self.download = blob, owner, file, download

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(_cancel_on_disconnect, receive, group.cancel_scope)
            await self._stream(scope, receive, send)
            group.cancel_scope.cancel()

    async def _stream(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False
        try:
            limit = attachment_max_bytes(self.file.mime)
            if self.file.size > limit:
                raise FileError("attachment exceeds current size limit", FileFailure.TOO_LARGE)
            timeout = load_settings().OUROBOROS_FILE_IO_TIMEOUT_SEC
            with get_transfer_limits().client_slot(), tempfile.TemporaryFile() as local:
                staged = anyio.wrap_file(local)
                received, first = 0, b""
                async with (
                    get_transfer_limits().async_slot(self.owner, async_engine_factory.engine, wait_seconds=timeout),
                    asyncio.timeout(timeout),
                    self.blob.open(self.owner, self.file.storage_scope, self.file.storage_key) as chunks,
                ):
                    async for chunk in chunks:
                        received += len(chunk)
                        if received > limit:
                            raise FileError("attachment exceeds current size limit", FileFailure.TOO_LARGE)
                        first = (first + chunk[:16])[:16]
                        await finish_file_io(staged.write(chunk))
                await finish_file_io(staged.seek(0))
                mime = self.file.mime
                safe_image = mime in SAFE_INLINE_IMAGE_MIMES and image_signature(first) == mime
                if mime in SAFE_INLINE_IMAGE_MIMES and not safe_image:
                    mime = "application/octet-stream"
                disposition = "inline" if safe_image and not self.download else "attachment"

                async def body() -> AsyncGenerator[bytes, None]:
                    while chunk := await finish_file_io(staged.read(CHUNK_BYTES)):
                        yield chunk

                async def send_progress(message: Message) -> None:
                    nonlocal started
                    if message["type"] == "http.response.start":
                        started = True
                    async with asyncio.timeout(timeout):
                        await send(message)

                response = StreamingResponse(
                    body(),
                    media_type=mime,
                    headers={
                        "Content-Disposition": content_disposition(self.file.name, disposition),
                        "X-Content-Type-Options": "nosniff",
                    },
                )
                await response.stream_response(send_progress)
        except (FileError, SQLAlchemyError, BotoCoreError, ClientError, OSError, TimeoutError) as error:
            if started:
                raise
            await file_error_response(error)(scope, receive, send)


@file_errors
async def api_file_upload(request: Request) -> Response:
    return await upload(request, draft=False)


@file_errors
async def api_file_entries(request: Request) -> Response:
    scope = await manager_scope(request)
    async with file_transaction() as conn:
        entries = await conn.run_sync(
            lambda c: FileCatalog(c, scope).entries(
                request.query_params.get("folder_id") or None,
                q=request.query_params.get("q", ""),
                cursor=request.query_params.get("cursor", ""),
                limit=int(request.query_params.get("limit", "50")),
            )
        )
    return JSONResponse(entries.model_dump())


@file_errors
async def api_file_messages(request: Request) -> Response:
    scope = await manager_scope(request)
    file_id = request.path_params["id"]
    async with file_transaction() as conn:
        window = await conn.run_sync(
            lambda c: FileHistory(c, scope).file_messages(
                file_id,
                cursor=request.query_params.get("cursor", ""),
                limit=int(request.query_params.get("limit", "50")),
            )
        )
    return await anyio.to_thread.run_sync(
        lambda: JSONResponse(
            MessagePage.from_messages(window.messages(), file_id=file_id, more=window.more).model_dump()
        )
    )


@file_errors
async def api_file_get(request: Request) -> Response:
    scope = await manager_scope(request)
    async with file_transaction() as conn:
        file = await conn.run_sync(lambda c: FileCatalog(c, scope).file(request.path_params["id"]))
    return JSONResponse(FileInfo.model_validate(file).model_dump())


@file_errors
async def api_file_content(request: Request) -> Response:
    scope = await manager_scope(request)
    async with file_transaction() as conn:
        file = await conn.run_sync(lambda c: FileCatalog(c, scope).file(request.path_params["id"]))
    return FileContent(request.app.state.file_blob, scope, file, request.query_params.get("download") == "1")


@file_errors
async def api_file_patch(request: Request) -> Response:
    scope = await manager_scope(request)
    patch = FilePatch.model_validate(await request.json())
    if not patch.model_fields_set or ("name" in patch.model_fields_set and patch.name is None):
        raise FileError("invalid patch", FileFailure.INVALID_INPUT)
    async with file_transaction() as conn:
        file = await conn.run_sync(
            lambda c: FileCatalog(c, scope).edit_file(
                request.path_params["id"],
                name=patch.name,
                folder_id=patch.folder_id,
                move="folder_id" in patch.model_fields_set,
            )
        )
    return JSONResponse(FileInfo.model_validate(file).model_dump())


@file_errors
async def api_file_delete(request: Request) -> Response:
    scope = await manager_scope(request)
    async with file_transaction() as conn:
        await conn.run_sync(lambda c: FileCatalog(c, scope).delete_file(request.path_params["id"]))
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@file_errors
async def api_folder_create(request: Request) -> Response:
    scope = await manager_scope(request)
    patch = FolderPatch.model_validate(await request.json())
    if not patch.model_fields_set or ("name" in patch.model_fields_set and patch.name is None):
        raise FileError("invalid patch", FileFailure.INVALID_INPUT)
    async with file_transaction() as conn:
        folder = await conn.run_sync(lambda c: FileCatalog(c, scope).create_folder(patch.name or "", patch.parent_id))
    return JSONResponse(FolderInfo.model_validate(folder).model_dump(), status_code=http_status.HTTP_201_CREATED)


@file_errors
async def api_folder_patch(request: Request) -> Response:
    scope = await manager_scope(request)
    patch = FolderPatch.model_validate(await request.json())
    if not patch.model_fields_set or ("name" in patch.model_fields_set and patch.name is None):
        raise FileError("invalid patch", FileFailure.INVALID_INPUT)
    async with file_transaction() as conn:
        folder = await conn.run_sync(
            lambda c: FileCatalog(c, scope).edit_folder(
                request.path_params["id"],
                name=patch.name,
                parent_id=patch.parent_id,
                move="parent_id" in patch.model_fields_set,
            )
        )
    return JSONResponse(FolderInfo.model_validate(folder).model_dump())


@file_errors
async def api_folder_delete(request: Request) -> Response:
    scope = await manager_scope(request)
    async with file_transaction() as conn:
        await conn.run_sync(lambda c: FileCatalog(c, scope).delete_folder(request.path_params["id"]))
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
