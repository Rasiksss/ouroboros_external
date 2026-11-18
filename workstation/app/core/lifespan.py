"""Application lifespan for startup / shutdown orchestration."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import suppress

from fastapi import FastAPI

from app.core.config import settings
from app.core.db import dispose_engine, init_engine
from app.services.periodic_tasks import PeriodicTasks, run_startup_maintenance
from app.services.system_events import system_event_publisher

logger = logging.getLogger(__name__)


def _warn_on_ephemeral_revision_history() -> None:
    """Flag a store-enabled deployment whose revision history dies with the pod.

    Restore itself is safe on SQLite (it reads the pointer from the object store), but
    the audit trail of workspace revisions is lost on every restart, which almost always
    means DATABASE_URL never got its secman value.
    """
    if not settings.WORKSPACE_STORE_ENABLED:
        return
    if not settings.DATABASE_URL.startswith("sqlite"):
        return
    logger.warning(
        "WORKSPACE_STORE_ENABLED=true with a SQLite DATABASE_URL: workspace restore still "
        "works, but revision history will not survive a restart"
    )


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown orchestration.

    Scheduler and background cleanup are added in a later stage.
    """
    logger.info("Starting %s v%s", settings.SERVICE_NAME, settings.APP_VERSION)

    init_engine()
    _warn_on_ephemeral_revision_history()
    scheduler = PeriodicTasks()
    with suppress(Exception):
        await run_startup_maintenance()

    try:
        await system_event_publisher.publish_workstation_start()
    except Exception:
        logger.exception("Startup system event publication failed")
    scheduler.start()
    _app.state.scheduler = scheduler

    try:
        yield
    finally:
        logger.info("Shutting down %s", settings.SERVICE_NAME)
        scheduler.shutdown()
        await system_event_publisher.close()
        await dispose_engine()
