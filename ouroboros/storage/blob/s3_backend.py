"""S3-compatible StorageProvider."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from collections.abc import AsyncIterator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, BinaryIO

import anyio
import boto3
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import BotoCoreError, ClientError

from ouroboros.storage.settings.schema import AppSettings
from ouroboros.utils import finish_file_io

if TYPE_CHECKING:
    from types_aiobotocore_s3.client import S3Client
    from types_aiobotocore_s3.type_defs import CompletedPartTypeDef

from new_core.identity import OwnerScope
from ouroboros.storage.blob.keys import (
    build_object_key,
    build_scope_prefix,
    normalize_storage_key,
    normalized_scope,
)
from ouroboros.storage.blob.protocol import ObjectTooLargeError

log = logging.getLogger(__name__)
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def _error_details(exc: Exception) -> tuple[str, object]:
    response = getattr(exc, "response", {})
    return (
        str(response.get("Error", {}).get("Code", "")),
        response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
    )


@asynccontextmanager
async def open_s3_client(settings: AppSettings) -> AsyncIterator[S3Client]:
    # Observability imports storage configuration during startup.
    from ouroboros.observability.aef_tracing import AWS_OUTBOUND_HEADER_EVENT, apply_outbound_headers_to_aws_request

    async with get_session().create_client(
        "s3",
        region_name=settings.OUROBOROS_S3_REGION or None,
        endpoint_url=settings.OUROBOROS_S3_ENDPOINT or None,
        aws_access_key_id=settings.OUROBOROS_S3_ACCESS_KEY.get_secret_value() or None,
        aws_secret_access_key=settings.OUROBOROS_S3_SECRET_KEY.get_secret_value() or None,
        config=s3_config(settings),
    ) as client:
        client.meta.events.register(AWS_OUTBOUND_HEADER_EVENT, apply_outbound_headers_to_aws_request)
        yield client


async def put_s3_fileobj(
    client: S3Client, settings: AppSettings, key: str, mime: str, source: anyio.AsyncFile[bytes], *, size: int
) -> None:
    bucket = settings.OUROBOROS_S3_BUCKET
    async with asyncio.timeout(settings.OUROBOROS_FILE_IO_TIMEOUT_SEC):
        if size < 8 * 1024 * 1024:
            await client.put_object(
                Bucket=bucket, Key=key, ContentType=mime, Body=await finish_file_io(source.read(size)), IfNoneMatch="*"
            )
            return
        upload = await client.create_multipart_upload(Bucket=bucket, Key=key, ContentType=mime)
        upload_id = upload["UploadId"]
        parts: list[CompletedPartTypeDef] = []
        committed = False
        try:
            while True:
                chunk = await finish_file_io(source.read(8 * 1024 * 1024))
                if not chunk and parts:
                    break
                number = len(parts) + 1
                part = await client.upload_part(
                    Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=chunk
                )
                parts.append({"ETag": part["ETag"], "PartNumber": number})
                if not chunk:
                    break
            await client.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}, IfNoneMatch="*"
            )
            committed = True
        finally:
            if not committed:
                with anyio.move_on_after(min(5, settings.OUROBOROS_FILE_IO_TIMEOUT_SEC), shield=True) as abort:
                    try:
                        await client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
                    except ClientError as error:
                        if _error_details(error)[0] != "NoSuchUpload":
                            log.warning("Multipart abort deferred to bucket lifecycle", exc_info=True)
                    except (BotoCoreError, OSError):
                        log.warning("Multipart abort deferred to bucket lifecycle", exc_info=True)
                if abort.cancel_called:
                    log.warning("Multipart abort timed out; deferred to bucket lifecycle")


def s3_config(settings: AppSettings) -> AioConfig:

    return AioConfig(
        signature_version="s3v4",
        connect_timeout=settings.OUROBOROS_S3_CONNECT_TIMEOUT_SEC,
        read_timeout=settings.OUROBOROS_S3_READ_TIMEOUT_SEC,
        max_pool_connections=settings.OUROBOROS_S3_POOL_SIZE,
        retries={"mode": "standard", "total_max_attempts": settings.OUROBOROS_S3_TOTAL_MAX_ATTEMPTS},
    )


class S3StorageProvider:
    """S3-compatible object storage backend."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        file_client: S3Client | None = None,
        file_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:

        # Observability imports storage configuration during startup.
        from ouroboros.observability.aef_tracing import AWS_OUTBOUND_HEADER_EVENT, apply_outbound_headers_to_aws_request

        self.bucket = settings.OUROBOROS_S3_BUCKET.strip()
        if not self.bucket:
            raise ValueError("OUROBOROS_S3_BUCKET is required for S3 storage backend")
        self.settings = settings.model_copy(update={"OUROBOROS_S3_BUCKET": self.bucket})
        self._file_client, self._file_loop = file_client, file_loop

        self._client = boto3.client(
            "s3",
            config=s3_config(self.settings),
            region_name=settings.OUROBOROS_S3_REGION or None,
            endpoint_url=settings.OUROBOROS_S3_ENDPOINT or None,
            aws_access_key_id=settings.OUROBOROS_S3_ACCESS_KEY.get_secret_value() or None,
            aws_secret_access_key=settings.OUROBOROS_S3_SECRET_KEY.get_secret_value() or None,
        )
        try:
            self._client.meta.events.register(AWS_OUTBOUND_HEADER_EVENT, apply_outbound_headers_to_aws_request)
        except BaseException:
            self._client.close()
            raise

    def _object_key(self, owner_scope: OwnerScope, scope: str, key: str) -> str:
        return build_object_key(owner_scope, scope, key)

    def exists(self, owner_scope: OwnerScope, scope: str, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._object_key(owner_scope, scope, key))
        except Exception as exc:
            if _error_details(exc)[0] not in _NOT_FOUND_CODES:
                raise
            return False
        return True

    def size(self, owner_scope: OwnerScope, scope: str, key: str) -> int:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=self._object_key(owner_scope, scope, key))
        except ClientError as exc:
            if _error_details(exc)[0] in _NOT_FOUND_CODES:
                raise FileNotFoundError(key) from exc
            raise
        return int(response["ContentLength"])

    def put_text(
        self, owner_scope: OwnerScope, scope: str, key: str, content: str, *, content_type: str = "text/plain"
    ) -> None:
        self.put_bytes(owner_scope, scope, key, content.encode("utf-8"), content_type=content_type)

    def get_text(self, owner_scope: OwnerScope, scope: str, key: str) -> str | None:
        raw = self.get_bytes(owner_scope, scope, key)
        if raw is None:
            return None
        return raw.decode("utf-8")

    def put_bytes(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._object_key(owner_scope, scope, key),
            Body=data,
            ContentType=content_type,
        )

    def put_fileobj(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        if self._file_client is None or self._file_loop is None or not self._file_loop.is_running():
            raise RuntimeError("File uploads require the process S3 client and its running event loop")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._file_loop:
            raise RuntimeError("Synchronous uploads must run outside the file client's event loop")
        position = fileobj.tell()
        size = fileobj.seek(0, 2) - position
        fileobj.seek(position)
        client = self._file_client
        started: Future[None] = Future()

        async def upload() -> None:
            if not started.set_running_or_notify_cancel():
                return
            started.set_result(None)
            await put_s3_fileobj(
                client,
                self.settings,
                self._object_key(owner_scope, scope, key),
                content_type,
                anyio.wrap_file(fileobj),
                size=size,
            )

        coroutine = upload()
        try:
            pending = asyncio.run_coroutine_threadsafe(coroutine, self._file_loop)
        except RuntimeError:
            coroutine.close()
            raise
        try:
            started.result(timeout=self.settings.OUROBOROS_FILE_IO_TIMEOUT_SEC)
        except TimeoutError:
            if started.cancel():
                pending.cancel()
                raise TimeoutError("S3 event loop did not accept the upload") from None
        pending.result()

    def get_bytes(self, owner_scope: OwnerScope, scope: str, key: str) -> bytes | None:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._object_key(owner_scope, scope, key))
        except Exception as exc:
            if _error_details(exc)[0] not in _NOT_FOUND_CODES:
                raise
            return None
        body = response.get("Body")
        if body is None:
            return None
        return body.read()

    def copy_to_path(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        destination: pathlib.Path,
        *,
        max_bytes: int,
    ) -> int:
        deadline = time.monotonic() + self.settings.OUROBOROS_FILE_IO_TIMEOUT_SEC
        target = pathlib.Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = None
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._object_key(owner_scope, scope, key))
            body = response.get("Body")
            if body is None:
                raise OSError("S3 get_object response has no body")
            written = 0
            with target.open("wb") as dst:
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("S3 download deadline exceeded")
                    chunk = body.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise ObjectTooLargeError(f"object exceeds {max_bytes} bytes")
                    dst.write(chunk)
            return written
        except Exception as exc:
            target.unlink(missing_ok=True)
            if _error_details(exc)[0] in _NOT_FOUND_CODES:
                raise FileNotFoundError(key) from exc
            raise
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def put_json(self, owner_scope: OwnerScope, scope: str, key: str, value: dict[str, Any]) -> None:
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        self.put_text(owner_scope, scope, key, text, content_type="application/json")

    def get_json(self, owner_scope: OwnerScope, scope: str, key: str) -> dict[str, Any] | None:
        raw = self.get_text(owner_scope, scope, key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            log.warning("Failed to parse S3 JSON at %s/%s", scope, key, exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    def create_json_if_absent(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        value: dict[str, Any],
    ) -> bool:
        content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        for attempt in range(3):
            try:
                self._client.put_object(
                    Bucket=self.bucket,
                    Key=self._object_key(owner_scope, scope, key),
                    Body=content,
                    ContentType="application/json",
                    IfNoneMatch="*",
                )
                return True
            except Exception as exc:
                code, status = _error_details(exc)
                if code in {"412", "PreconditionFailed"} or status == 412:
                    return False
                conflict = code in {"409", "ConditionalRequestConflict"} or status == 409
                if not conflict or attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def delete_json_if_matches(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        expected: dict[str, Any],
    ) -> bool:
        target = self._matching_json_etag(owner_scope, scope, key, expected)
        if target is None:
            return False
        object_key, etag = target
        try:
            self._client.delete_object(
                Bucket=self.bucket,
                Key=object_key,
                IfMatch=etag,
            )
            return True
        except Exception as exc:
            code, status = _error_details(exc)
            if code in _NOT_FOUND_CODES | {"409", "412", "ConditionalRequestConflict", "PreconditionFailed"}:
                return False
            if status in {409, 412}:
                return False
            raise

    def replace_json_if_matches(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool:
        target = self._matching_json_etag(owner_scope, scope, key, expected)
        if target is None:
            return False
        object_key, etag = target
        content = (json.dumps(replacement, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=content,
                ContentType="application/json",
                IfMatch=etag,
            )
            return True
        except Exception as exc:
            code, status = _error_details(exc)
            if code in {"409", "412", "ConditionalRequestConflict", "PreconditionFailed"}:
                return False
            if status in {409, 412}:
                return False
            raise

    def _matching_json_etag(
        self,
        owner_scope: OwnerScope,
        scope: str,
        key: str,
        expected: dict[str, Any],
    ) -> tuple[str, str] | None:
        object_key = self._object_key(owner_scope, scope, key)
        body = None
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=object_key)
            body = response.get("Body")
            if body is None or json.loads(body.read()) != expected:
                return None
            etag = str(response.get("ETag") or "")
            return (object_key, etag) if etag else None
        except Exception as exc:
            if _error_details(exc)[0] in _NOT_FOUND_CODES:
                return None
            raise
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def list_keys(
        self,
        owner_scope: OwnerScope,
        scope: str,
        prefix: str = "",
        *,
        limit: int | None = None,
        start_after: str = "",
    ) -> list[str]:
        if limit is not None and limit <= 0:
            return []
        scope_norm = normalized_scope(scope)
        tenant_prefix = build_scope_prefix(owner_scope, scope_norm)
        norm_prefix = normalize_storage_key(prefix) if prefix else ""
        list_prefix = f"{tenant_prefix}{norm_prefix}" if norm_prefix else tenant_prefix

        keys: list[str] = []
        continuation: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": list_prefix}
            if limit is not None:
                kwargs["MaxKeys"] = min(limit - len(keys), 1000)
            if continuation:
                kwargs["ContinuationToken"] = continuation
            elif start_after:
                kwargs["StartAfter"] = f"{tenant_prefix}{normalize_storage_key(start_after)}"
            response = self._client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                full_key = str(item.get("Key") or "")
                if full_key.startswith(tenant_prefix):
                    keys.append(full_key[len(tenant_prefix) :])
            if (limit is not None and len(keys) >= limit) or not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
            if not continuation:
                break
        ordered = sorted(keys)
        return ordered[:limit] if limit is not None else ordered

    def delete(self, owner_scope: OwnerScope, scope: str, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._object_key(owner_scope, scope, key))

    def atomic_put_json(self, owner_scope: OwnerScope, scope: str, key: str, value: dict[str, Any]) -> None:
        self.put_json(owner_scope, scope, key, value)

    def retire_file(self, owner_scope: OwnerScope, scope: str, key: str) -> None:
        self.put_bytes(owner_scope, scope, key, b"")

    def close(self) -> None:
        self._client.close()
