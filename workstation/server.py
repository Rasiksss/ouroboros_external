"""CLI entry point: migrate | runserver.

Usage:
    python server.py migrate
    python server.py runserver [--reload] [--host HOST] [--port PORT]
"""

from __future__ import annotations

import sys

import click

from app.cli.migrate import run_migrate
from app.cli.runserver import run_server


@click.group()
def cli() -> None:
    """Service management commands."""


@cli.command()
@click.option("--revision", default="head", show_default=True, help="Alembic target revision.")
def migrate(revision: str) -> None:
    """Apply Alembic migrations (alembic upgrade <revision>)."""
    run_migrate(revision)


@cli.command()
@click.option("--host", default=None, help="Bind host (default from settings.APP_HOST).")
@click.option("--port", type=int, default=None, help="Bind port (default from settings.APP_PORT).")
@click.option("--reload/--no-reload", default=None, help="Auto-reload on file changes.")
@click.option("--workers", type=int, default=None, help="Worker count (ignored with --reload).")
def runserver(
    host: str | None,
    port: int | None,
    reload: bool | None,
    workers: int | None,
) -> None:
    """Start the FastAPI app with uvicorn."""
    run_server(host=host, port=port, reload=reload, workers=workers)


if __name__ == "__main__":
    sys.exit(cli())
