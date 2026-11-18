"""SQLAlchemy 2.0 `DeclarativeBase` + composable mixins.

Compose mixins on your model, then use `generate_repository()` —
it detects mixins and adds the matching repo-mixin behaviour automatically.

    class MyModel(Base, IdMixin, AuditMixin, SoftDeleteMixin):
        __tablename__ = "my_models"
        ...
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Root declarative class — Alembic reads `Base.metadata`."""


class UUIDMixin:
    """UUID primary key (server-generated default)."""

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        index=True,
    )


class IdMixin:
    """BigInt auto-increment primary key (Postgres); Integer on SQLite for portability."""

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
        index=True,
    )


class TimestampMixin:
    """`created_at` (server now), `updated_at` (on update)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=func.now(),
        nullable=True,
    )


class SoftDeleteMixin:
    """Add-on for soft-delete behaviour — `generate_repository()` switches
    `delete()` to an UPDATE and filters lists by `is_deleted=False`."""

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditMixin(TimestampMixin):
    """Tracks who created / last updated the row (filled by AuditRepoMixin
    from `user_data_context`)."""

    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


def has_mixin(model: type[Any], mixin: type) -> bool:
    """Check whether a model composes a given mixin (for repo auto-detection)."""
    return isinstance(model, type) and issubclass(model, mixin)
