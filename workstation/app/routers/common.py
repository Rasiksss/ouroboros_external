"""System endpoints: health, readiness, version, metrics."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.metrics import render_metrics
from app.schemas.common import HealthzResponse, VersionResponse
from app.utils.healthz import (
    DatabaseInaccessibilityError,
    RedisInaccessibilityError,
    probe_db,
    probe_redis,
)

router = APIRouter()

ROUTE_HEALTH = "/health/liveness"
ROUTE_READY = "/health/readiness"
ROUTE_VERSION = "/version"
ROUTE_METRICS = "/metrics"
ROUTE_INFO = "/info"


@router.get(ROUTE_HEALTH, response_model=HealthzResponse)
async def healthcheck() -> HealthzResponse:
    """Liveness probe — 200 while the process is alive."""
    return HealthzResponse()


@router.get(ROUTE_READY, response_model=HealthzResponse)
async def readiness(session: AsyncSession = Depends(get_session)) -> HealthzResponse | JSONResponse:
    """Readiness probe — DB + Redis (if enabled)."""
    try:
        await probe_db(session)
        await probe_redis()
    except DatabaseInaccessibilityError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "database": "down", "redis": "unknown", "runtime": "unknown"},
        )
    except RedisInaccessibilityError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "database": "ok", "redis": "down", "runtime": "unknown"},
        )
    return HealthzResponse()


@router.get(ROUTE_VERSION, response_model=VersionResponse)
async def version() -> VersionResponse:
    return VersionResponse(name=settings.SERVICE_NAME, version=settings.APP_VERSION)


@router.get(ROUTE_METRICS, response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(content=render_metrics())


@router.get(ROUTE_INFO)
async def info() -> dict[str, object]:
    """Deployment metadata compatibility endpoint without secret-bearing fields."""
    return {
        "app": {
            "agentid": settings.AGENT_ID,
            "namespace": settings.POD_NAMESPACE or settings.NAMESPACE,
            "name": settings.SERVICE_NAME,
            "version": settings.APP_VERSION,
            "loglevel": str(settings.APP_LOG_LEVEL),
            "distributive": settings.DISTRIBUTIVE,
        },
        "pod": {
            "cluster": settings.CLUSTER_ID,
            "node": settings.NODE_NAME,
            "namespace": settings.NAMESPACE,
            "pod_name": settings.POD_NAME,
            "instance_ip": os.getenv("INSTANCE_IP", ""),
            "host_ip": os.getenv("HOST_IP", ""),
        },
    }


class EndpointFilter(logging.Filter):
    """Filter uvicorn access-log noise from system probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return all(route not in msg for route in (ROUTE_HEALTH, ROUTE_READY, ROUTE_METRICS, ROUTE_INFO))


logging.getLogger("uvicorn.access").addFilter(EndpointFilter())
