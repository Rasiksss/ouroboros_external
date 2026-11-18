"""Pagination helper for SELECT statements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


@dataclass
class PaginationResult[T]:
    data: list[T]
    current_page: int
    total_pages: int
    batch_size: int
    total_count: int


async def paginate(
    session: AsyncSession,
    stmt: Select[Any],
    *,
    page: int = 1,
    size: int = 50,
) -> PaginationResult[Any]:
    """Run `stmt` with LIMIT/OFFSET and return a `PaginationResult`."""
    if page < 1:
        page = 1
    if size < 1:
        size = 1

    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one() or 0)
    offset = (page - 1) * size
    result = await session.execute(stmt.limit(size).offset(offset))
    rows = list(result.scalars().all())
    total_pages = (total + size - 1) // size if total else 0
    return PaginationResult(
        data=rows,
        current_page=page,
        total_pages=total_pages,
        batch_size=size,
        total_count=total,
    )
