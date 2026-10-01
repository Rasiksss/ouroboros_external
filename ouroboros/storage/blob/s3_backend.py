"""S3-compatible StorageProvider."""

from __future__ import annotations

import json
import logging
import pathlib
import uuid
from typing import Any, BinaryIO

from ouroboros.storage.blob.keys import build_object_key, build_scope_prefix, normalize_storage_key
from ouroboros.storage.blob.protocol import ObjectTooLargeError
from ouroboros.storage.blob.scopes import ALL_SCOPES, SCOPE_LOGS
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def _error_details(exc: Exception) -> tuple[str, object]:
    response = getattr(exc, "response", {})
    return (
        str(response.get("Error", {}).get("Code", "")),
        response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
    )


def _reject_logs_scope(scope: str, *, operation: str) -> bool:
    """Return True when the operation must not target S3 (logs are local-only)."""
    if str(scope or "").strip().lower() == SCOPE_LOGS:
        log.warning(
            "S3 storage rejects %s for logs scope; use ScopedHybridProvider / local file backend",
            operation,
        )
        return True
    return False


class S3StorageProvider:
    """S3-compatible object storage backend."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str = "",
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
    ) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = str(bucket or "").strip()
        if not self.bucket:
            raise ValueError("OUROBOROS_S3_BUCKET is required for S3 storage backend")

        client_kwargs: dict[str, Any] = {"config": Config(signature_version="s3v4")}
        if region:
            client_kwargs["region_name"] = region
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key

        self._client = boto3.client("s3", **client_kwargs)

    def _object_key(self, scope: str, key: str) -> str:
        return build_object_key(scope, key)

    def exists(self, scope: str, key: str) -> bool:
        if _reject_logs_scope(scope, operation="exists"):
            return False
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._object_key(scope, key))
            return True
        except Exception as exc:
            if _error_details(exc)[0] in _NOT_FOUND_CODES:
                return False
            raise

    def put_text(self, scope: str, key: str, content: str, *, content_type: str = "text/plain") -> None:
        if _reject_logs_scope(scope, operation="put_text"):
            return
        self.put_bytes(scope, key, content.encode("utf-8"), content_type=content_type)

    def get_text(self, scope: str, key: str) -> str | None:
        if _reject_logs_scope(scope, operation="get_text"):
            return None
        raw = self.get_bytes(scope, key)
        if raw is None:
            return None
        return raw.decode("utf-8")

    def put_bytes(
        self,
        scope: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        if _reject_logs_scope(scope, operation="put_bytes"):
            return
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._object_key(scope, key),
            Body=data,
            ContentType=content_type,
        )

    def put_fileobj(
        self,
        scope: str,
        key: str,
        fileobj: BinaryIO,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        if _reject_logs_scope(scope, operation="put_fileobj"):
            return
        self._client.upload_fileobj(
            fileobj,
            self.bucket,
            self._object_key(scope, key),
            ExtraArgs={"ContentType": content_type},
        )

    def get_bytes(self, scope: str, key: str) -> bytes | None:
        if _reject_logs_scope(scope, operation="get_bytes"):
            return None
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._object_key(scope, key))
            body = response.get("Body")
            if body is None:
                return None
            return body.read()
        except Exception as exc:
            if _error_details(exc)[0] in _NOT_FOUND_CODES:
                return None
            raise

    def copy_to_path(
        self,
        scope: str,
        key: str,
        destination: pathlib.Path,
        *,
        max_bytes: int,
    ) -> int:
        if _reject_logs_scope(scope, operation="copy_to_path"):
            raise FileNotFoundError(key)
        target = pathlib.Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = None
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._object_key(scope, key))
            body = response.get("Body")
            if body is None:
                raise OSError("S3 get_object response has no body")
            written = 0
            with target.open("wb") as dst:
                while True:
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

    def put_json(self, scope: str, key: str, value: dict[str, Any]) -> None:
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        self.put_text(scope, key, text, content_type="application/json")

    def get_json(self, scope: str, key: str) -> dict[str, Any] | None:
        raw = self.get_text(scope, key)
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
        scope: str,
        key: str,
        value: dict[str, Any],
    ) -> bool:
        if _reject_logs_scope(scope, operation="create_json_if_absent"):
            return False
        content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        for attempt in range(3):
            try:
                self._client.put_object(
                    Bucket=self.bucket,
                    Key=self._object_key(scope, key),
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
        scope: str,
        key: str,
        expected: dict[str, Any],
    ) -> bool:
        if _reject_logs_scope(scope, operation="delete_json_if_matches"):
            return False
        target = self._matching_json_etag(scope, key, expected)
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
        scope: str,
        key: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool:
        if _reject_logs_scope(scope, operation="replace_json_if_matches"):
            return False
        target = self._matching_json_etag(scope, key, expected)
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
        scope: str,
        key: str,
        expected: dict[str, Any],
    ) -> tuple[str, str] | None:
        object_key = self._object_key(scope, key)
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

    def append_event(self, scope: str, stream: str, event: dict[str, Any]) -> bool:
        if _reject_logs_scope(scope, operation="append_event"):
            return False
        stream_name = str(stream or "").strip() or "default"
        if scope == "logs" and stream_name.endswith(".jsonl"):
            base = stream_name[: -len(".jsonl")]
        else:
            base = stream_name
        event_id = uuid.uuid4().hex[:12]
        ts = utc_now_iso().replace(":", "-")
        key = normalize_storage_key(f"{base}/events/{ts}_{event_id}.json")
        line = json.dumps(event, ensure_ascii=False)
        try:
            self.put_bytes(scope, key, (line + "\n").encode("utf-8"), content_type="application/json")
            return True
        except Exception:
            log.warning("S3 append_event failed scope=%s stream=%s", scope, stream, exc_info=True)
            return False

    def list_keys(
        self,
        scope: str,
        prefix: str = "",
        *,
        limit: int | None = None,
        start_after: str = "",
    ) -> list[str]:
        if _reject_logs_scope(scope, operation="list_keys"):
            return []
        if limit is not None and limit <= 0:
            return []
        scope_norm = str(scope or "").strip().lower()
        if scope_norm not in ALL_SCOPES:
            raise ValueError(f"Unknown storage scope: {scope!r}")
        tenant_prefix = build_scope_prefix(scope_norm)
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

    def delete(self, scope: str, key: str) -> None:
        if _reject_logs_scope(scope, operation="delete"):
            return
        self._client.delete_object(Bucket=self.bucket, Key=self._object_key(scope, key))

    def atomic_put_json(self, scope: str, key: str, value: dict[str, Any]) -> None:
        self.put_json(scope, key, value)
