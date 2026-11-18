"""Apply Alembic migrations (alembic upgrade <revision>)."""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)


def run_migrate(revision: str = "head") -> None:
    cfg = Config("alembic.ini")
    logger.info("Running alembic upgrade to %s", revision)
    command.upgrade(cfg, revision)
