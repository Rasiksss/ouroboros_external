"""Service layer: thin orchestrator over repository + serializer."""

from __future__ import annotations

import logging
import time
import traceback
from functools import wraps
from typing import Any

from app.repositories import BaseCRUDRepository
from app.serializers import BaseSerializer

logger = logging.getLogger(__name__)


def _ecs_logger[F](func: F) -> F:
    @wraps(func)
    async def wrapped(self, *args, **kwargs):
        op = f"{type(self).__name__}.{func.__name__}"
        started = time.perf_counter()
        logger.debug(
            "%s start, args=%s, kwargs=%s",
            op,
            args,
            kwargs,
            extra={
                "event.category": "processing",
                "event.action": "persist",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            result = await func(self, *args, **kwargs)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                "%s failed",
                op,
                extra={
                    "event.category": "processing",
                    "event.action": "persist",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.trace_back": traceback.format_exc(),
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "%s success",
                op,
                extra={
                    "event.category": "processing",
                    "event.action": "persist",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                },
            )
            return result

    return wrapped


class BaseCRUDService[M, S]:
    """Delegate CRUD to repository, then pass the result through serializer."""

    def __init__(self, repository: BaseCRUDRepository, serializer: BaseSerializer) -> None:
        self._repository = repository
        self._serializer = serializer

    @_ecs_logger
    async def create(self, **data: Any) -> S:
        obj = await self._repository.create(**data)
        return await self._serializer.on_create(obj)

    @_ecs_logger
    async def update(self, object_id: Any, **data: Any) -> S:
        obj = await self._repository.update(object_id, **data)
        return await self._serializer.on_update(obj)

    @_ecs_logger
    async def get_single(self, **conditions: Any) -> S | None:
        obj = await self._repository.get_single(**conditions)
        return await self._serializer.get_single(obj)

    @_ecs_logger
    async def get_list(self, **conditions: Any) -> list[S]:
        objects = await self._repository.get_list(**conditions)
        return await self._serializer.get_list(objects)

    @_ecs_logger
    async def delete(self, object_id: Any, **kwargs: Any) -> bool:
        result = await self._repository.delete(object_id, **kwargs)
        return bool(await self._serializer.on_delete(result))
