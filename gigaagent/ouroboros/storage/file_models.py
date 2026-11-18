from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ouroboros.contracts.attachments import AttachmentMetadata, FileStatus, attachment_kind


class FileBase(DeclarativeBase):
    pass


class FileTransferLease(FileBase):
    __tablename__ = "file_transfer_leases"
    token: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text)
    process_id: Mapped[str] = mapped_column(Text)
    outgoing: Mapped[bool] = mapped_column(Boolean)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("idx_file_transfers_owner", "owner_id", "agent_id", "expires_at"),
        Index("idx_file_transfers_process", "process_id", "expires_at"),
        Index("idx_file_transfers_expiry", "expires_at"),
    )


class Folder(FileBase):
    __tablename__ = "folders"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("owner_id", "agent_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "agent_id", "parent_id"], ["folders.owner_id", "folders.agent_id", "folders.id"]
        ),
        Index("idx_folders_page", "owner_id", "agent_id", "parent_id", "name", "id"),
    )


Index(
    "uq_folders_name", Folder.owner_id, Folder.agent_id, func.coalesce(Folder.parent_id, ""), Folder.name, unique=True
)


class CatalogFile(FileBase):
    __tablename__ = "files"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text)
    folder_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    storage_scope: Mapped[str] = mapped_column(Text, default="uploads")
    storage_key: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    origin: Mapped[str] = mapped_column(Text)
    surface_id: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text, default="")
    validation_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    producer_task: Mapped[str] = mapped_column(Text, default="")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    artifact_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[FileStatus] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[str | None] = mapped_column(Text)
    claim_token: Mapped[str | None] = mapped_column(Text)
    claim_until: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("owner_id", "agent_id", "id"),
        UniqueConstraint("owner_id", "agent_id", "storage_scope", "storage_key"),
        ForeignKeyConstraint(
            ["owner_id", "agent_id", "folder_id"], ["folders.owner_id", "folders.agent_id", "folders.id"]
        ),
        CheckConstraint("status IN ('staging','draft','ready','deleting','deleted')"),
        CheckConstraint("size >= 0"),
        Index("idx_files_page", "owner_id", "agent_id", "folder_id", "status", "name", "id"),
        Index("idx_files_cleanup", "status", "expires_at", "claim_until", "id"),
        Index(
            "idx_files_artifact", "owner_id", "agent_id", "producer_task", "artifact_path", "artifact_sequence", "id"
        ),
    )


Table(
    "messages",
    FileBase.metadata,
    Column("message_id", Text, primary_key=True),
    Column("owner_id", Text),
    Column("agent_id", Text),
    Column("chat_id", Text),
    Column("source", Text),
)


class MessageFile(FileBase):
    __tablename__ = "message_files"
    file_id: Mapped[str] = mapped_column(Text, primary_key=True)
    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(["owner_id", "agent_id", "file_id"], ["files.owner_id", "files.agent_id", "files.id"]),
        ForeignKeyConstraint(
            ["owner_id", "agent_id", "message_id"],
            ["messages.owner_id", "messages.agent_id", "messages.message_id"],
            ondelete="CASCADE",
        ),
        Index("idx_message_files_message", "message_id", "file_id"),
    )


def attachment_metadata(file: CatalogFile) -> AttachmentMetadata:
    return {
        "id": file.id,
        "storage_key": file.storage_key,
        "filename": file.name,
        "mime": file.mime,
        "kind": attachment_kind(file.mime),
        "size": file.size,
        "source": file.origin,
        "status": file.status,
        "message_id": "",
        "created_at": file.created_at,
        "expires_at": file.expires_at or "",
        "owner_id": file.owner_id,
        "agent_id": file.agent_id,
        "surface_id": file.surface_id,
        "chat_id": file.chat_id,
        "fingerprint": file.fingerprint,
        "validation_version": file.validation_version,
    }
