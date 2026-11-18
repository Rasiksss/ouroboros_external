"""Background cleanup scheduler and startup maintenance."""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass

from app.core.config import settings
from app.core.db import get_session_maker
from app.services.session.factory import build_session_service

logger = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger
except ImportError:  # pragma: no cover
    AsyncIOScheduler = None
    IntervalTrigger = None


@dataclass(slots=True, frozen=True)
class StartupMaintenanceResult:
    failed_missing_rootfs: int
    removed_orphans: int
    cleaned_expired: int


class PeriodicTasks:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler() if AsyncIOScheduler is not None else None

    def start(self) -> None:
        if self._scheduler is None or IntervalTrigger is None:
            logger.warning("APScheduler is not available; periodic cleanup is disabled")
            return

        self._scheduler.add_job(
            self._cleanup_sessions,
            IntervalTrigger(seconds=settings.CLEANUP_INTERVAL_SEC),
            id="cleanup_sessions",
            replace_existing=True,
        )
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def _cleanup_sessions(self) -> None:
        logger.debug(
            "Starting cleanup task",
            extra={
                "event.category": "database",
                "event.action": "receive",
                "app.operation": "cleanup_sessions",
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        started = time.perf_counter()
        try:
            maker = get_session_maker()
            async with maker() as session:
                service = build_session_service(session)
                cleaned = await service.cleanup_expired_sessions()
                await session.commit()
                if cleaned:
                    logger.info("Cleanup task removed %s sessions", cleaned)
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "Cleanup task failed",
                extra={
                    "event.category": "database",
                    "event.action": "failure",
                    "app.operation": "cleanup_sessions",
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(e).__name__,
                    "error.message": str(e),
                    "error.stack_trace": traceback.format_exc(),
                },
            )
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "Cleanup task complete",
                extra={
                    "event.category": "database",
                    "event.action": "complete",
                    "app.operation": "cleanup_sessions",
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                },
            )


async def run_startup_maintenance() -> StartupMaintenanceResult:
    logger.debug(
        "Starting startup maintenance",
        extra={
            "event.category": "database",
            "event.action": "receive",
            "app.operation": "startup_maintenance",
            "event.outcome": "unknown",
            "app.status": "accepted",
        },
    )
    started = time.perf_counter()
    try:
        maker = get_session_maker()
        async with maker() as session:
            service = build_session_service(session)
            reconcile = await service.reconcile_sessions()
            cleaned = await service.cleanup_expired_sessions()
            await session.commit()

            maintenance = StartupMaintenanceResult(
                failed_missing_rootfs=reconcile.failed_missing_rootfs,
                removed_orphans=reconcile.removed_orphans,
                cleaned_expired=cleaned,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "Startup maintenance: failed_missing_rootfs=%s removed_orphans=%s cleaned_expired=%s",
                maintenance.failed_missing_rootfs,
                maintenance.removed_orphans,
                maintenance.cleaned_expired,
                extra={
                    "event.category": "database",
                    "event.action": "complete",
                    "app.operation": "startup_maintenance",
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                },
            )

            return maintenance
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.error(
            "Startup maintenance failed",
            extra={
                "event.category": "database",
                "event.action": "failure",
                "app.operation": "startup_maintenance",
                "event.outcome": "failure",
                "app.status": "failed",
                "app.duration_ms": elapsed_ms,
                "error.type": type(e).__name__,
                "error.message": str(e),
                "error.stack_trace": traceback.format_exc(),
            },
        )
        raise
