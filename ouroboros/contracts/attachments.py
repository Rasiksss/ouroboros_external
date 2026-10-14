from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, TypeAdapter
from typing_extensions import TypedDict

from security_config.attachments import SAFE_INLINE_IMAGE_MIMES

FileStatus = Literal["staging", "draft", "ready", "deleting", "deleted"]


@dataclass(frozen=True)
class StoredFile:
    file_id: str


class AttachmentIdentity(BaseModel):
    id: str


class Entry(BaseModel):
    id: str
    name: str
    kind: Literal["folder", "file"]


class EntryPage(BaseModel):
    items: list[Entry]
    next_cursor: str | None = None
    path: list[Entry] = []


CONTENT_VALIDATION_VERSION = 1


class AttachmentMetadata(TypedDict):
    id: str
    storage_key: str
    filename: str
    mime: str
    kind: Literal["image", "file"]
    size: int
    source: str
    status: FileStatus
    message_id: str
    created_at: str
    expires_at: str

    owner_id: str
    agent_id: str
    surface_id: str
    chat_id: str
    fingerprint: str
    validation_version: int


class PublicAttachment(TypedDict, total=False):
    id: str
    kind: Literal["image", "file"]
    filename: str
    mime: str
    size: int
    status: str
    expires_at: str


class TerminalAttachment(PublicAttachment, total=False):
    caption: str


def attachment_kind(mime: str) -> Literal["image", "file"]:
    return "image" if str(mime or "").lower() in SAFE_INLINE_IMAGE_MIMES else "file"


ATTACHMENTS_ADAPTER = TypeAdapter(list[AttachmentMetadata])
ATTACHMENT_IDS_ADAPTER = TypeAdapter(list[AttachmentIdentity])
TERMINAL_ATTACHMENTS_ADAPTER = TypeAdapter(list[TerminalAttachment])


def attachment_status(metadata: AttachmentMetadata) -> str:
    if metadata["status"] in {"deleting", "deleted"}:
        return "deleted"
    expiry = datetime.fromisoformat(metadata["expires_at"].replace("Z", "+00:00")) if metadata["expires_at"] else None
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    expired = expiry is not None and expiry <= datetime.now(timezone.utc)
    return "expired" if expired else metadata["status"]


def public_attachment(metadata: AttachmentMetadata) -> PublicAttachment:
    attachment_id = metadata["id"]
    if not attachment_id:
        return {}
    result: PublicAttachment = {
        "id": attachment_id,
        "kind": metadata["kind"],
        "filename": metadata["filename"],
        "mime": metadata["mime"],
        "size": metadata["size"],
        "status": attachment_status(metadata),
        "expires_at": metadata["expires_at"],
    }
    return result
