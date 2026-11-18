from __future__ import annotations

import asyncio
from uuid import UUID

from starlette import status
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ouroboros.scheduling.service import SchedulerError, SchedulerService
from ouroboros.scheduling.store import (
    ConcurrentUpdateError,
    ScheduleNotFoundError,
    StorageError,
)
from transport.browser.identity import IdentityRefused, browser_identity

_SQL_MAX_INTEGER = 2**63 - 1


async def api_schedules(request: Request) -> Response:
    try:
        scope = await browser_identity.resolver.scope_for(request.headers)
    except IdentityRefused as exc:
        return JSONResponse({"error": exc.reason}, status_code=exc.status)
    try:
        cursor = int(request.query_params.get("cursor", "0"))
        limit = int(request.query_params.get("limit", "15"))
        if not 0 <= cursor <= _SQL_MAX_INTEGER or limit < 1:
            raise ValueError
    except ValueError:
        return JSONResponse(
            {"error": "cursor must be a non-negative 64-bit integer and limit must be positive"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    service: SchedulerService = request.app.state.scheduler
    try:
        result = await asyncio.to_thread(
            service.list_schedules,
            scope,
            state=request.query_params.get("state", ""),
            cursor=cursor,
            limit=limit,
        )
    except StorageError:
        return JSONResponse(
            {"error": "schedule storage is unavailable"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    return JSONResponse(result)


async def api_schedule_delete(request: Request) -> Response:
    try:
        scope = await browser_identity.resolver.scope_for(request.headers)
    except IdentityRefused as exc:
        return JSONResponse({"error": exc.reason}, status_code=exc.status)
    try:
        revision = int(request.query_params.get("revision", ""))
        if not 1 <= revision <= _SQL_MAX_INTEGER:
            raise ValueError
    except ValueError:
        return JSONResponse(
            {"error": "revision must be a positive 64-bit integer"}, status_code=status.HTTP_400_BAD_REQUEST
        )
    try:
        schedule_id = str(UUID(request.path_params["schedule_id"]))
    except ValueError:
        return JSONResponse({"error": "schedule not found"}, status_code=status.HTTP_404_NOT_FOUND)
    service: SchedulerService = request.app.state.scheduler
    try:
        await asyncio.to_thread(service.delete_schedule, scope, schedule_id, revision=revision)
    except ScheduleNotFoundError:
        return JSONResponse({"error": "schedule not found"}, status_code=status.HTTP_404_NOT_FOUND)
    except ConcurrentUpdateError:
        return JSONResponse(
            {"error": "schedule revision changed; list schedules and retry"}, status_code=status.HTTP_409_CONFLICT
        )
    except SchedulerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    except StorageError:
        return JSONResponse(
            {"error": "schedule storage is unavailable"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
