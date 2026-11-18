"""SQLAlchemy model for session lifecycle."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.time import utcnow


class SessionStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    FAILED = "failed"


class Session(Base, TimestampMixin):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rootfs_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    network_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_timeout_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    absolute_ttl_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    file_bytes_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workspace_persistence_scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    workspace_persistence_root: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_restored_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workspace_snapshot_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
