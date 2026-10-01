"""Canonical lifecycle for browser and Dialogs chat attachments."""

from __future__ import annotations

import base64
import binascii
import logging
import pathlib
import re
import tempfile
import threading
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, BinaryIO, Literal, NamedTuple, cast

from typing_extensions import NotRequired, TypedDict

from ouroboros.storage.blob.keys import normalize_storage_key
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS

log = logging.getLogger(__name__)

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_OUTBOUND_BYTES = 50 * 1024 * 1024
CHAT_ATTACHMENT_MAX_BYTES = MAX_DOCUMENT_BYTES
SAFE_INLINE_IMAGE_MIMES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})

_ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_META_PREFIX = "_meta/"
_CANONICAL_META_PREFIX = "_meta/chat/"
_LEGACY_META_PREFIX = "_meta/attachments/"
_PUBLIC_META_PREFIX = "_meta/public/"
_EXPIRY_PREFIX = "_expiry/"
_CLEANUP_CHECKPOINT_KEY = "_maintenance/attachment_cleanup.json"
_ATTACHMENT_BIND_LOCK = threading.RLock()
ATTACHMENT_EXPIRY_BATCH_SIZE = 200
_LEGACY_META_SCAN_LIMIT = 16


class AttachmentMetadata(TypedDict):
    id: str
    storage_key: str
    filename: str
    mime: str
    kind: Literal["image", "file"]
    size: int
    source: str
    status: str
    message_id: str
    created_at: str
    expires_at: str


class _StoredAttachment(NamedTuple):
    metadata: AttachmentMetadata
    metadata_key: str
    raw_metadata: dict[str, Any]


class PublicAttachment(TypedDict, total=False):
    id: str
    kind: Literal["image", "file"]
    filename: str
    mime: str
    size: int
    status: str
    expires_at: str
    url: NotRequired[str]


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


def attachment_meta_key(attachment_id: str) -> str:
    return f"{_CANONICAL_META_PREFIX}{_attachment_id(attachment_id)}.json"


def _legacy_meta_key(attachment_id: str) -> str:
    return f"{_LEGACY_META_PREFIX}{_attachment_id(attachment_id)}.json"


def attachment_kind(mime: str) -> Literal["image", "file"]:
    return "image" if str(mime or "").lower() in SAFE_INLINE_IMAGE_MIMES else "file"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _ttl_hours() -> int:
    try:
        from ouroboros.storage.settings.store import load_settings

        return max(1, int(load_settings().OUROBOROS_USER_FILE_TTL_HOURS))
    except Exception:
        return 720


def _parse_expiry(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("attachment expiry is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_expired(metadata: Mapping[str, object], *, now: datetime | None = None) -> bool:
    expiry = _parse_expiry(metadata.get("expires_at"))
    return expiry is not None and expiry <= (now or _now())


def _normalize_size(value: object) -> int:
    return max(0, int(value)) if isinstance(value, (str, bytes, bytearray, int, float)) else 0


def _normalize_metadata(metadata: Mapping[str, object]) -> AttachmentMetadata:
    mime = str(metadata.get("mime") or metadata.get("content_type") or "application/octet-stream").lower()
    return {
        "id": str(metadata.get("id") or ""),
        "storage_key": str(metadata.get("storage_key") or ""),
        "filename": str(metadata.get("filename") or metadata.get("display_name") or "attachment"),
        "mime": mime,
        "kind": attachment_kind(mime),
        "size": _normalize_size(metadata.get("size")),
        "source": str(metadata.get("source") or ""),
        "status": str(metadata.get("status") or "ready"),
        "message_id": str(metadata.get("message_id") or ""),
        "created_at": str(metadata.get("created_at") or ""),
        "expires_at": str(metadata.get("expires_at") or ""),
    }


def _replace_metadata(
    provider: StorageProvider,
    metadata_key: str,
    current: Mapping[str, object],
    replacement: Mapping[str, object],
) -> None:
    if not provider.replace_json_if_matches(
        SCOPE_UPLOADS,
        metadata_key,
        dict(current),
        dict(replacement),
    ):
        attachment_id = str(replacement.get("id") or current.get("id") or "")
        raise ValueError(f"attachment changed concurrently: {attachment_id}")


def register_attachment(
    provider: StorageProvider,
    *,
    attachment_id: str,
    storage_key: str,
    filename: str,
    mime: str,
    size: int,
    source: str,
    status: str = "draft",
    created_at: str = "",
    expires_at: str = "",
    message_id: str = "",
) -> AttachmentMetadata:
    created = str(created_at or _iso(_now()))
    expiry = str(expires_at or _iso(_now() + timedelta(hours=_ttl_hours())))
    metadata = _normalize_metadata(
        {
            "id": _attachment_id(attachment_id),
            "storage_key": str(storage_key or ""),
            "filename": _filename(filename),
            "mime": str(mime or "application/octet-stream").lower(),
            "size": size,
            "kind": attachment_kind(mime),
            "source": source,
            "status": status,
            "message_id": message_id,
            "created_at": created,
            "expires_at": expiry,
        }
    )
    metadata_key = attachment_meta_key(metadata["id"])
    if get_attachment(provider, metadata["id"]) is not None:
        raise ValueError(f"attachment id already exists: {metadata['id']}")
    if not provider.create_json_if_absent(SCOPE_UPLOADS, metadata_key, dict(metadata)):
        raise ValueError(f"attachment id already exists: {metadata['id']}")
    return metadata


def _find_attachment(
    provider: StorageProvider,
    attachment_id: str,
) -> _StoredAttachment | None:
    try:
        clean_id = _attachment_id(attachment_id)
    except ValueError:
        return None
    canonical_key = attachment_meta_key(clean_id)
    metadata = provider.get_json(SCOPE_UPLOADS, canonical_key)
    if isinstance(metadata, dict):
        return _StoredAttachment(_normalize_metadata(metadata), canonical_key, dict(metadata))
    keys = provider.list_keys(
        SCOPE_UPLOADS,
        f"{_CANONICAL_META_PREFIX}{clean_id}/",
        limit=_LEGACY_META_SCAN_LIMIT,
    )
    for key in keys:
        if not key.endswith(".json"):
            continue
        metadata = provider.get_json(SCOPE_UPLOADS, key)
        if isinstance(metadata, dict):
            normalized = _normalize_metadata(metadata)
            if not normalized["id"]:
                normalized["id"] = clean_id
            return _StoredAttachment(normalized, key, dict(metadata))
    legacy_key = _legacy_meta_key(clean_id)
    metadata = provider.get_json(SCOPE_UPLOADS, legacy_key)
    if not isinstance(metadata, dict):
        return None
    normalized = _normalize_metadata(metadata)
    if not normalized["id"]:
        normalized["id"] = clean_id
    return _StoredAttachment(normalized, legacy_key, dict(metadata))


def get_attachment(provider: StorageProvider, attachment_id: str) -> AttachmentMetadata | None:
    record = _find_attachment(provider, attachment_id)
    return record.metadata if record is not None else None


def bind_attachments(
    provider: StorageProvider,
    attachment_ids: list[str],
    *,
    message_id: str,
) -> list[AttachmentMetadata]:
    with _ATTACHMENT_BIND_LOCK:
        originals: list[_StoredAttachment] = []
        changed: list[tuple[_StoredAttachment, AttachmentMetadata]] = []
        for attachment_id in attachment_ids:
            record = _find_attachment(provider, attachment_id)
            if record is None:
                raise ValueError(f"attachment not found: {attachment_id}")
            metadata = record.metadata
            if _is_expired(metadata):
                raise ValueError(f"attachment expired: {attachment_id}")
            if metadata["status"] == "ready" and metadata["message_id"] == str(message_id or ""):
                originals.append(record)
                continue
            if metadata["status"] != "draft" or metadata["message_id"]:
                raise ValueError(f"attachment not found: {attachment_id}")
            originals.append(record)
        try:
            bound = []
            for record in originals:
                original = record.metadata
                if original["status"] == "ready":
                    bound.append(original)
                    continue
                metadata = _normalize_metadata({**original, "status": "ready", "message_id": str(message_id or "")})
                _replace_metadata(
                    provider,
                    record.metadata_key,
                    record.raw_metadata,
                    metadata,
                )
                changed.append((record, metadata))
                bound.append(metadata)
            return bound
        except Exception:
            for record, updated in changed:
                try:
                    _replace_metadata(
                        provider,
                        record.metadata_key,
                        updated,
                        record.raw_metadata,
                    )
                except Exception:
                    log.warning("Failed to roll back attachment binding %s", record.metadata["id"], exc_info=True)
            raise


def release_attachments(
    provider: StorageProvider,
    attachment_ids: list[str],
    *,
    message_id: str,
) -> None:
    with _ATTACHMENT_BIND_LOCK:
        for attachment_id in attachment_ids:
            record = _find_attachment(provider, attachment_id)
            if record is None:
                continue
            metadata = record.metadata
            if metadata["status"] != "ready" or metadata["message_id"] != str(message_id or ""):
                continue
            released = _normalize_metadata({**metadata, "status": "draft", "message_id": ""})
            _replace_metadata(
                provider,
                record.metadata_key,
                record.raw_metadata,
                released,
            )


def delete_attachment_metadata(provider: StorageProvider, attachment_id: str) -> None:
    try:
        clean_id = _attachment_id(attachment_id)
    except ValueError:
        return
    provider.delete(
        SCOPE_UPLOADS,
        attachment_meta_key(clean_id),
    )
    for key in provider.list_keys(SCOPE_UPLOADS, f"{_CANONICAL_META_PREFIX}{clean_id}/"):
        provider.delete(SCOPE_UPLOADS, key)
    provider.delete(SCOPE_UPLOADS, _legacy_meta_key(clean_id))


def delete_draft_attachment(provider: StorageProvider, attachment_id: str) -> bool:
    with _ATTACHMENT_BIND_LOCK:
        record = _find_attachment(provider, attachment_id)
        if record is None:
            return False
        metadata = record.metadata
        if metadata["status"] == "draft" and not metadata["message_id"]:
            deleting = _normalize_metadata({**metadata, "status": "deleting"})
            _replace_metadata(
                provider,
                record.metadata_key,
                record.raw_metadata,
                deleting,
            )
            deleting_raw: Mapping[str, object] = deleting
        elif metadata["status"] == "deleting" and not metadata["message_id"]:
            deleting = metadata
            deleting_raw = record.raw_metadata
        else:
            raise ValueError("upload is already attached to a message")
    if deleting["storage_key"]:
        provider.delete(SCOPE_UPLOADS, deleting["storage_key"])
    provider.delete_json_if_matches(
        SCOPE_UPLOADS,
        record.metadata_key,
        dict(deleting_raw),
    )
    return True


def _canonical_payload_key(
    metadata_key: str,
    metadata: Mapping[str, object],
) -> str:
    if (
        not metadata_key.startswith(_META_PREFIX)
        or metadata_key.startswith(_PUBLIC_META_PREFIX)
        or not metadata_key.endswith(".json")
    ):
        return ""
    if metadata_key.startswith(_CANONICAL_META_PREFIX):
        canonical_chat_id = metadata_key[len(_CANONICAL_META_PREFIX) : -5]
    else:
        canonical_chat_id = ""
    if canonical_chat_id and "/" not in canonical_chat_id:
        try:
            attachment_id = _attachment_id(canonical_chat_id)
            filename = _filename(str(metadata.get("filename") or ""))
        except ValueError:
            return ""
        if str(metadata.get("id") or attachment_id) != attachment_id:
            return ""
        return f"chat/{attachment_id}/{filename}"
    payload_key = metadata_key[len(_META_PREFIX) : -5]
    try:
        canonical = normalize_storage_key(payload_key) == payload_key and all(
            part not in {"", ".", ".."} for part in payload_key.split("/")
        )
    except ValueError:
        return ""
    return payload_key if canonical and payload_key.startswith(("chat/", "generated/")) else ""


def _drain_expiry_index(provider: StorageProvider, limit: int) -> int:
    if limit <= 0:
        return 0
    keys = provider.list_keys(SCOPE_UPLOADS, _EXPIRY_PREFIX, limit=limit)
    for key in keys:
        try:
            provider.delete(SCOPE_UPLOADS, key)
        except Exception:
            log.warning("Failed to delete deprecated attachment expiry index %s", key, exc_info=True)
    return len(keys)


def _cleanup_metadata(
    provider: StorageProvider,
    metadata_key: str,
    *,
    now: datetime,
) -> int:
    if not metadata_key.endswith(".json"):
        return 0
    try:
        metadata = provider.get_json(SCOPE_UPLOADS, metadata_key)
        if not isinstance(metadata, Mapping) or not _is_expired(metadata, now=now):
            return 0
        if metadata_key.startswith(_PUBLIC_META_PREFIX):
            provider.delete_json_if_matches(
                SCOPE_UPLOADS,
                metadata_key,
                dict(metadata),
            )
            return 0
        payload_key = _canonical_payload_key(metadata_key, metadata)
        if not payload_key or str(metadata.get("storage_key") or "") != payload_key:
            log.warning("Attachment metadata key mismatch %s", metadata_key)
            return 0
        deleting = {**metadata, "status": "deleting"}
        if not provider.replace_json_if_matches(
            SCOPE_UPLOADS,
            metadata_key,
            dict(metadata),
            deleting,
        ):
            return 0
        provider.delete(SCOPE_UPLOADS, payload_key)
        return int(
            provider.delete_json_if_matches(
                SCOPE_UPLOADS,
                metadata_key,
                deleting,
            )
        )
    except (TypeError, ValueError):
        log.warning("Invalid attachment metadata %s", metadata_key, exc_info=True)
    except Exception:
        log.warning("Failed to delete expired attachment metadata %s", metadata_key, exc_info=True)
    return 0


def cleanup_expired_attachments(
    provider: StorageProvider,
    *,
    now: datetime | None = None,
    batch_size: int = ATTACHMENT_EXPIRY_BATCH_SIZE,
) -> int:
    """Delete one bounded page of expired upload metadata."""
    limit = max(batch_size, 0)
    if limit == 0:
        return 0
    index_count = _drain_expiry_index(provider, limit // 4)
    metadata_limit = limit - index_count
    try:
        checkpoint = provider.get_json(SCOPE_UPLOADS, _CLEANUP_CHECKPOINT_KEY)
    except Exception:
        log.warning("Failed to read attachment cleanup checkpoint", exc_info=True)
        checkpoint = None
    cursor = str(checkpoint.get("cursor") or "") if isinstance(checkpoint, Mapping) else ""
    try:
        cursor = cursor if cursor.startswith(_META_PREFIX) and normalize_storage_key(cursor) == cursor else ""
    except ValueError:
        cursor = ""
    metadata_keys = provider.list_keys(
        SCOPE_UPLOADS,
        _META_PREFIX,
        limit=metadata_limit,
        start_after=cursor,
    )
    current = now or _now()
    removed = sum(_cleanup_metadata(provider, key, now=current) for key in metadata_keys)
    next_cursor = metadata_keys[-1] if metadata_limit and len(metadata_keys) == metadata_limit else ""
    try:
        provider.atomic_put_json(
            SCOPE_UPLOADS,
            _CLEANUP_CHECKPOINT_KEY,
            {"cursor": next_cursor},
        )
    except Exception:
        log.warning("Failed to write attachment cleanup checkpoint", exc_info=True)
    return removed


def public_attachment(metadata: Mapping[str, object]) -> PublicAttachment:
    normalized = _normalize_metadata(metadata)
    attachment_id = normalized["id"]
    if not attachment_id:
        return {}
    expired = _is_expired(normalized)
    result: PublicAttachment = {
        "id": attachment_id,
        "kind": normalized["kind"],
        "filename": normalized["filename"],
        "mime": normalized["mime"],
        "size": normalized["size"],
        "status": "expired" if expired else normalized["status"],
        "expires_at": normalized["expires_at"],
    }
    if not expired:
        filename = urllib.parse.quote(normalized["filename"], safe="")
        result["url"] = f"api/chat/files/{attachment_id}/{filename}"
    return result


def _discard_staged_attachment(
    provider: StorageProvider,
    storage_key: str,
    attachment_id: str,
) -> None:
    try:
        provider.delete(SCOPE_UPLOADS, storage_key)
    except Exception:
        log.warning("Failed to roll back staged attachment %s", storage_key, exc_info=True)
        return
    try:
        delete_attachment_metadata(provider, attachment_id)
    except Exception:
        log.warning("Failed to remove staged attachment metadata %s", attachment_id, exc_info=True)


def store_attachment_fileobj(
    provider: StorageProvider,
    *,
    attachment_id: str,
    filename: str,
    mime: str,
    size: int,
    fileobj: BinaryIO,
    source: str,
    created_at: str = "",
    expires_at: str = "",
    status: str = "draft",
    message_id: str = "",
) -> AttachmentMetadata:
    if size > MAX_DOCUMENT_BYTES:
        raise AttachmentError("attachment_too_large", "attachment exceeds 50 MB limit", filename=filename)
    clean_name = _filename(filename)
    clean_id = _attachment_id(attachment_id)
    storage_key = f"chat/{clean_id}/{clean_name}"
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as staged_file:
        actual_size = 0
        while chunk := fileobj.read(1024 * 1024):
            actual_size += len(chunk)
            if actual_size > MAX_DOCUMENT_BYTES:
                raise AttachmentError(
                    "attachment_too_large",
                    "attachment exceeds 50 MB limit",
                    filename=filename,
                )
            staged_file.write(chunk)
        staged_file.seek(0)
        metadata = register_attachment(
            provider,
            attachment_id=clean_id,
            storage_key=storage_key,
            filename=clean_name,
            mime=mime,
            size=actual_size,
            source=source,
            created_at=created_at,
            expires_at=expires_at,
            status="staging",
        )
        try:
            provider.put_fileobj(
                SCOPE_UPLOADS,
                storage_key,
                cast(BinaryIO, staged_file),
                content_type=mime,
            )
            ready = _normalize_metadata({**metadata, "status": status, "message_id": message_id})
            _replace_metadata(
                provider,
                attachment_meta_key(metadata["id"]),
                metadata,
                ready,
            )
            return ready
        except Exception:
            _discard_staged_attachment(provider, storage_key, clean_id)
            raise


def store_attachment_bytes(
    provider: StorageProvider,
    *,
    attachment_id: str,
    filename: str,
    mime: str,
    data: bytes,
    source: str,
    status: str = "ready",
    message_id: str = "",
) -> AttachmentMetadata:
    return store_attachment_fileobj(
        provider,
        attachment_id=attachment_id,
        filename=filename,
        mime=mime,
        size=len(data),
        fileobj=BytesIO(data),
        source=source,
        status=status,
        message_id=message_id,
    )


def store_attachment_path(
    provider: StorageProvider,
    *,
    attachment_id: str,
    filename: str,
    mime: str,
    path: pathlib.Path,
    source: str,
    status: str = "ready",
    message_id: str = "",
) -> AttachmentMetadata:
    size = path.stat().st_size
    with path.open("rb") as fileobj:
        return store_attachment_fileobj(
            provider,
            attachment_id=attachment_id,
            filename=filename,
            mime=mime,
            size=size,
            fileobj=fileobj,
            source=source,
            status=status,
            message_id=message_id,
        )


def store_legacy_image_base64(
    provider: StorageProvider,
    *,
    attachment_id: str,
    image_base64: str,
    claimed_mime: str,
    source: str,
    message_id: str,
) -> AttachmentMetadata:
    encoded = str(image_base64 or "").strip()
    mime = str(claimed_mime or "").strip().lower()
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or not header.lower().endswith(";base64"):
            raise ValueError("legacy image data URL is invalid")
        mime = header[5:-7].strip().lower()
    compact = "".join(encoded.split())
    max_encoded_size = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 8
    if not compact or len(compact) > max_encoded_size:
        raise ValueError("legacy image payload is empty or too large")
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("legacy image payload is invalid base64") from exc
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
        (b"\xff\xd8\xff", "image/jpeg", "jpg"),
        (b"GIF87a", "image/gif", "gif"),
        (b"GIF89a", "image/gif", "gif"),
    )
    detected = next(((m, e) for sig, m, e in signatures if data.startswith(sig)), None)
    if detected is None and len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        detected = ("image/webp", "webp")
    if detected is None or (mime and mime != detected[0]):
        raise ValueError("legacy image format is unsupported")
    return store_attachment_bytes(
        provider,
        attachment_id=attachment_id,
        filename=f"image.{detected[1]}",
        mime=detected[0],
        data=data,
        source=source,
        status="ready",
        message_id=message_id,
    )


__all__ = [
    "CHAT_ATTACHMENT_MAX_BYTES",
    "MAX_DOCUMENT_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_OUTBOUND_BYTES",
    "SAFE_INLINE_IMAGE_MIMES",
    "AttachmentError",
    "AttachmentMetadata",
    "PublicAttachment",
    "attachment_kind",
    "attachment_meta_key",
    "bind_attachments",
    "cleanup_expired_attachments",
    "delete_attachment_metadata",
    "delete_draft_attachment",
    "get_attachment",
    "public_attachment",
    "register_attachment",
    "release_attachments",
    "store_attachment_bytes",
    "store_attachment_fileobj",
    "store_attachment_path",
    "store_legacy_image_base64",
]
