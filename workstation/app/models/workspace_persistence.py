"""SQLAlchemy models for durable /workspace snapshots."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.time import utcnow


class WorkspaceScope(Base, TimestampMixin):
    __tablename__ = "workspace_scopes"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    latest_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WorkspaceRevision(Base):
    __tablename__ = "workspace_revisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scope_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("workspace_scopes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_from_session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    root: Mapped[str] = mapped_column(String(64), nullable=False, default="/workspace")
    manifest_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class WorkspaceFile(Base):
    __tablename__ = "workspace_files"

    revision_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspace_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    path: Mapped[str] = mapped_column(String(2048), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    mode: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
