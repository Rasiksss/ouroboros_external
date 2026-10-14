"""SQL-owned attachments shared by transports and tools."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import pathlib
import re
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from typing import IO, TYPE_CHECKING, BinaryIO, Literal

import anyio
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.ext.asyncio import AsyncConnection

from infra.async_engine import async_engine_factory
from new_core.identity import OwnerScope, ServiceJob
from new_core.loop import CancellationSignal
from ouroboros.contracts.attachments import (
    CONTENT_VALIDATION_VERSION,
    AttachmentMetadata,
    attachment_status,
)
from ouroboros.contracts.owner_scope import stored_pair_identity
from ouroboros.storage.attachment_validation import (
    AttachmentError,
    attachment_max_bytes,
    image_signature,
    validate_attachment_path,
)
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.file_catalog import FileCatalog, FileError, FileFailure, file_claim_seconds
from ouroboros.storage.file_models import CatalogFile, attachment_metadata
from ouroboros.storage.file_transfers import get_transfer_limits
from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.storage.settings.store import load_settings
from ouroboros.utils import finish_file_io
from security_config.attachments import IMAGE_SUFFIXES

if TYPE_CHECKING:
    from ouroboros.storage.blob.async_backend import AsyncBlob

log = logging.getLogger(__name__)
_ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
ATTACHMENT_EXPIRY_BATCH_SIZE = 200
_ATTACHMENT_CLEANUP_FILE_BUDGET = 1000
_ATTACHMENT_CLEANUP_PASS_BUDGET_SEC = 180


def _attachment_id(value: str) -> str:
    clean = str(value or "").strip()
    if not _ATTACHMENT_ID_RE.fullmatch(clean):
        raise ValueError("invalid attachment id")
    return clean


def _filename(value: str) -> str:
    clean = pathlib.PurePosixPath(str(value or "").replace("\\", "/")).name.strip()
    clean = re.sub(r"[\x00-\x1f\x7f]", "_", clean)
    if not clean or clean in {".", ".."}:
        raise ValueError("invalid attachment filename")
    return clean[:200]


def read_attachment_bytes(
    owner_scope: OwnerScope, provider: StorageProvider, storage_key: str, *, mime: str, max_bytes: int | None = None
) -> bytes | None:
    """Read an owned attachment under the current format limit; return None when absent."""
    limit = attachment_max_bytes(mime)
    if max_bytes is not None:
        limit = min(limit, max_bytes)
    try:
        with (
            get_transfer_limits().slot(owner_scope),
            tempfile.TemporaryDirectory(prefix="ouroboros-attachment-") as directory,
        ):
            path = pathlib.Path(directory) / "attachment"
            provider.copy_to_path(owner_scope, SCOPE_UPLOADS, storage_key, path, max_bytes=limit)
            return path.read_bytes()
    except FileNotFoundError:
        return None


def get_attachment(owner_scope: OwnerScope, attachment_id: str) -> AttachmentMetadata | None:
    with resolve_runtime_engine("files").begin() as conn:
        try:
            return attachment_metadata(FileCatalog(conn, owner_scope).file(attachment_id, available=False))
        except FileError:
            return None


def delete_attachment_metadata(owner_scope: OwnerScope, attachment_id: str) -> None:
    with resolve_runtime_engine("files").begin() as conn:
        try:
            FileCatalog(conn, owner_scope).delete_unlinked_file(attachment_id)
        except FileError:
            return


def cleanup_expired_attachments(provider: StorageProvider, *, cancellation: CancellationSignal) -> int:
    engine = resolve_runtime_engine("files")
    lease = file_claim_seconds(load_settings().OUROBOROS_FILE_IO_TIMEOUT_SEC)
    deadline = time.monotonic() + _ATTACHMENT_CLEANUP_PASS_BUDGET_SEC
    remaining = _ATTACHMENT_CLEANUP_FILE_BUDGET
    removed = 0
    while remaining > 0 and not cancellation.is_stopped() and time.monotonic() < deadline:
        with engine.begin() as conn:
            claims = FileCatalog.claim_cleanup(
                conn, limit=min(ATTACHMENT_EXPIRY_BATCH_SIZE, remaining), lease_seconds=lease
            )
        if not claims:
            break
        remaining -= len(claims)
        for file in claims:
            if cancellation.is_stopped() or time.monotonic() >= deadline:
                return removed
            scope = stored_pair_identity(
                owner_id=file.owner_id, agent_id=file.agent_id, job=ServiceJob.CONTEXT_CLEANUP
            ).as_scope()
            try:
                provider.retire_file(scope, file.storage_scope, file.storage_key)
                with engine.begin() as conn:
                    removed += FileCatalog(conn, scope).finish_cleanup(file.id, file.claim_token)
            except (OSError, TimeoutError, RuntimeError, BotoCoreError, ClientError):
                log.warning("Attachment cleanup failed id=%s", file.id, exc_info=True)
    return removed


@asynccontextmanager
async def file_transaction() -> AsyncIterator[AsyncConnection]:

    with anyio.CancelScope(shield=True):
        async with asyncio.timeout(load_settings().OUROBOROS_FILE_IO_TIMEOUT_SEC):
            async with async_engine_factory.engine.begin() as conn:
                yield conn


async def store_attachment_async(
    scope: OwnerScope,
    blob: AsyncBlob,
    path: pathlib.Path,
    *,
    filename: str,
    mime: str,
    size: int,
    fingerprint: str,
    folder_id: str | None,
    draft: bool,
    validation_version: int = 0,
) -> CatalogFile:
    async with get_transfer_limits().async_slot(scope, async_engine_factory.engine):
        async with file_transaction() as conn:
            file = await conn.run_sync(
                lambda c: FileCatalog(c, scope).reserve(filename, mime, origin="browser", folder_id=folder_id)
            )

        async def release() -> None:
            async with file_transaction() as conn:
                await conn.run_sync(lambda c: FileCatalog(c, scope).release_claim(file.id, file.claim_token))

        try:
            await blob.put(scope, file.storage_scope, file.storage_key, path, mime, size=size)
            async with file_transaction() as conn:
                return await conn.run_sync(
                    lambda c: FileCatalog(c, scope).complete(
                        file.id,
                        size=size,
                        fingerprint=fingerprint,
                        draft=draft,
                        token=file.claim_token,
                        validation_version=validation_version,
                    )
                )
        except BaseException:
            await finish_file_io(release())
            raise


def stage_attachment(source: IO[bytes], path: pathlib.Path, filename: str, mime: str) -> tuple[int, str]:
    limit = attachment_max_bytes(mime)
    digest, size = hashlib.sha256(), 0
    with path.open("wb") as staged:
        while chunk := source.read(256 * 1024):
            size += len(chunk)
            if size > limit:
                raise AttachmentError(
                    "attachment_too_large", f"attachment exceeds {limit} bytes limit", filename=filename
                )
            digest.update(chunk)
            staged.write(chunk)
    return size, digest.hexdigest()


def store_attachment_fileobj(
    owner_scope: OwnerScope,
    provider: StorageProvider,
    *,
    attachment_id: str,
    filename: str,
    mime: str,
    size: int,
    fileobj: BinaryIO,
    source: str,
    status: Literal["draft", "ready"] = "draft",
    producer_task: str = "",
    artifact_path: str = "",
    validate_content: bool = False,
) -> AttachmentMetadata:
    limit = attachment_max_bytes(mime)
    clean_name, clean_id = _filename(filename), _attachment_id(attachment_id)
    if size > limit:
        raise AttachmentError("attachment_too_large", f"attachment exceeds {limit} bytes limit", filename=clean_name)
    engine = resolve_runtime_engine("files")
    with tempfile.TemporaryDirectory(prefix="ouroboros-stage-") as directory:
        path = pathlib.Path(directory) / ("content" + pathlib.Path(clean_name).suffix)
        size, fingerprint = stage_attachment(fileobj, path, clean_name, mime)
        if validate_content:
            validated = validate_attachment_path(path, clean_name, mime)
            clean_name, mime = validated.filename, validated.mime
        with get_transfer_limits().slot(owner_scope):
            with engine.begin() as conn:
                catalog = FileCatalog(conn, owner_scope)
                file = catalog.reserve(
                    clean_name,
                    mime,
                    origin=source,
                    file_id=clean_id,
                    producer_task=producer_task,
                    artifact_path=artifact_path,
                    fingerprint=fingerprint,
                )
                if file.status in {"ready", "draft"}:
                    return attachment_metadata(file)
                key = file.storage_key
            try:
                with path.open("rb") as staged:
                    provider.put_fileobj(owner_scope, SCOPE_UPLOADS, key, staged, content_type=mime)
                with engine.begin() as conn:
                    completed = FileCatalog(conn, owner_scope).complete(
                        file.id,
                        size=size,
                        fingerprint=fingerprint,
                        draft=status == "draft",
                        token=file.claim_token,
                        validation_version=CONTENT_VALIDATION_VERSION if validate_content else 0,
                    )
                    return attachment_metadata(completed)
            except BaseException:
                with engine.begin() as conn:
                    FileCatalog(conn, owner_scope).release_claim(file.id, file.claim_token)
                raise


def store_legacy_image_base64(
    owner_scope: OwnerScope,
    provider: StorageProvider,
    *,
    attachment_id: str,
    image_base64: str,
    claimed_mime: str,
    source: str,
) -> AttachmentMetadata:
    limit = load_settings().OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES
    encoded = str(image_base64 or "").strip()
    mime = str(claimed_mime or "").strip().lower()
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or not header.lower().endswith(";base64"):
            raise ValueError("legacy image data URL is invalid")
        mime = header[5:-7].strip().lower()
    compact = "".join(encoded.split())
    max_encoded_size = ((limit + 2) // 3) * 4
    if not compact:
        raise ValueError("legacy image payload is empty")
    if len(compact) > max_encoded_size:
        raise AttachmentError("attachment_too_large", f"attachment exceeds {limit} bytes limit")
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("legacy image payload is invalid base64") from exc
    detected = image_signature(data)
    if not detected or (mime and mime != detected):
        raise ValueError("legacy image format is unsupported")
    existing = get_attachment(owner_scope, attachment_id)
    if existing is not None and not existing["fingerprint"]:
        if attachment_status(existing) not in {"ready", "draft"}:
            raise FileError("upload is unavailable", FileFailure.CONFLICT)
        if read_attachment_bytes(owner_scope, provider, existing["storage_key"], mime=detected) != data:
            raise FileError("file identity conflicts with content", FileFailure.CONFLICT)
        return existing
    with BytesIO(data) as fileobj:
        return store_attachment_fileobj(
            owner_scope,
            provider,
            attachment_id=attachment_id,
            filename="image" + IMAGE_SUFFIXES[detected],
            mime=detected,
            fileobj=fileobj,
            size=fileobj.getbuffer().nbytes,
            source=source,
            status="draft",
        )
