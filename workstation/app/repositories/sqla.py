"""SQLAlchemy async implementation of `BaseCRUDRepository`.

Usage:
    repo = generate_repository(MyModel, session=session)
    obj = await repo.get_single(id=42)

`generate_repository` auto-composes these mixins based on model mixins:
    - `SoftDeleteMixin` -> `SoftDeleteRepoMixin`  (delete is UPDATE, default filter is_deleted=False)
    - `AuditMixin`      -> `AuditRepoMixin`       (created_by/updated_by from user_data_context)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import Select, delete, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import AuditMixin, SoftDeleteMixin
from app.repositories.base import BaseCRUDRepository
from app.utils.context import user_data_context

logger = logging.getLogger(__name__)

M = TypeVar("M")


class BaseSQLARepository[M](BaseCRUDRepository):
    """Generic repository over one model."""

    def __init__(
        self,
        session: AsyncSession,
        model: type[M],
        default_filters: dict[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._model = model
        self._default_filters = default_filters or {}

    @property
    def session(self) -> AsyncSession:
        return self._session

    def _apply_filters(self, stmt: Select[Any], **conditions: Any) -> Select[Any]:
        filters = {**self._default_filters, **conditions}
        for key, value in filters.items():
            if key.endswith("__in"):
                column = getattr(self._model, key[:-4])
                stmt = stmt.where(column.in_(value))
            else:
                column = getattr(self._model, key)
                stmt = stmt.where(column == value)
        return stmt

    def build_query(
        self,
        *,
        order_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        **conditions: Any,
    ) -> Select[Any]:
        """Собрать SELECT без исполнения — базовый инструмент для lazy-паттерна.

        Сервис может дополнить возвращённый stmt своими `.where()` / `.options()`
        и отдать в `execute_query()` / `paginate()` / `session.stream_scalars()`.
        В память попадают только финально отобранные строки.
        """
        stmt = self._apply_filters(select(self._model), **conditions)
        if order_by:
            stmt = self._apply_order_by(stmt, order_by)
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return stmt

    async def execute_query(self, stmt: Select[Any]) -> list[M]:
        """Исполнить предварительно собранный stmt и вернуть scalars()."""
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_single(self, **conditions: Any) -> M:
        stmt = self.build_query(limit=1, **conditions)
        result = await self._session.execute(stmt)
        obj = result.scalars().first()
        if obj is None:
            raise NoResultFound(f"{self._model.__name__} not found for {conditions}")
        return obj

    async def get_list(
        self,
        *,
        order_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        **conditions: Any,
    ) -> list[M]:
        """Материализующая обёртка поверх `build_query`.

        Для сложных случаев (нужно доложить фильтр из сервиса, стримить, пагинировать)
        пользуйся `build_query()` + `execute_query()` / `paginate()`.
        """
        stmt = self.build_query(order_by=order_by, limit=limit, offset=offset, **conditions)
        return await self.execute_query(stmt)

    def _apply_order_by(self, stmt: Select[Any], order_by: str) -> Select[Any]:
        for field in (f.strip() for f in order_by.split(",") if f.strip()):
            descending = field.startswith("-")
            name = field.lstrip("-")
            column = getattr(self._model, name)
            stmt = stmt.order_by(column.desc() if descending else column.asc())
        return stmt

    async def create(self, **data: Any) -> M:
        obj = self._model(**data)
        self._session.add(obj)
        await self._session.flush()  # get generated id without committing
        await self._session.refresh(obj)
        return obj

    async def update(self, object_id: Any, **data: Any) -> M:
        obj = await self.get_single(id=object_id)
        for key, value in data.items():
            setattr(obj, key, value)
        await self._session.flush()
        await self._session.refresh(obj)
        return obj

    async def delete(self, object_id: Any, **_: Any) -> bool:
        stmt = delete(self._model).where(self._model.id == object_id)  # type: ignore[attr-defined]
        result = await self._session.execute(stmt)
        return bool(result.rowcount)

    async def bulk_create(self, objects: Iterable[dict[str, Any]]) -> list[M]:
        instances = [self._model(**obj) for obj in objects]
        self._session.add_all(instances)
        await self._session.flush()
        for obj in instances:
            await self._session.refresh(obj)
        return instances

    async def is_exists(self, **conditions: Any) -> bool:
        stmt = self._apply_filters(select(self._model.id), **conditions).limit(1)  # type: ignore[attr-defined]
        result = await self._session.execute(stmt)
        return result.scalar() is not None

    async def get_count(self, **conditions: Any) -> int:
        from sqlalchemy import func

        stmt = self._apply_filters(select(func.count()).select_from(self._model), **conditions)
        result = await self._session.execute(stmt)
        return int(result.scalar_one() or 0)


class SoftDeleteRepoMixin(BaseSQLARepository[M]):
    """Soft-delete override: mark `is_deleted=True` instead of DELETE."""

    async def delete(self, object_id: Any, **_: Any) -> bool:
        user = user_data_context()
        stmt = (
            update(self._model)
            .where(self._model.id == object_id)  # type: ignore[attr-defined]
            .values(
                is_deleted=True,
                deleted_at=datetime.now(tz=UTC),
                deleted_by=getattr(user, "fio", None) or "Unknown",
            )
        )
        result = await self._session.execute(stmt)
        return bool(result.rowcount)


class AuditRepoMixin(BaseSQLARepository[M]):
    """Audit override: stamp `created_by`/`updated_by` from user context."""

    def _current_user(self) -> str:
        user = user_data_context()
        return getattr(user, "fio", None) or "Unknown"

    async def create(self, **data: Any) -> M:
        data.setdefault("created_by", self._current_user())
        return await super().create(**data)

    async def update(self, object_id: Any, **data: Any) -> M:
        data["updated_by"] = self._current_user()
        return await super().update(object_id, **data)


def generate_repository[M](
    model: type[M],
    *,
    session: AsyncSession,
    extra_mixins: list[type] | None = None,
    default_filters: dict[str, Any] | None = None,
) -> BaseSQLARepository[M]:
    """Build a concrete repository instance for `model`, composing mixins."""
    bases: list[type] = []
    filters = dict(default_filters or {})

    if issubclass(model, AuditMixin):
        bases.append(AuditRepoMixin)
    if issubclass(model, SoftDeleteMixin):
        bases.append(SoftDeleteRepoMixin)
        filters.setdefault("is_deleted", False)

    for mixin in extra_mixins or []:
        if not issubclass(mixin, BaseSQLARepository):
            raise TypeError(f"{mixin.__name__} must inherit BaseSQLARepository")
        bases.append(mixin)

    bases.append(BaseSQLARepository)
    repo_cls = type(f"{model.__name__}Repository", tuple(bases), {})
    return repo_cls(session=session, model=model, default_filters=filters)
