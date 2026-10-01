"""Apply Alembic migrations against a SQLAlchemy engine."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config

from ouroboros.storage.db_url import escape_percent_for_alembic_ini

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def alembic_config(db_url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", escape_percent_for_alembic_ini(db_url))
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade_head(engine: Engine) -> None:
    """Run pending migrations on the given engine."""
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    cfg.attributes.pop("connection", None)
