"""Prod entrypoint invoked as `python3 app.py` from the production Dockerfile.

Mirrors the local startup: apply Alembic migrations, then launch uvicorn.
Host/port come from APP_HOST / APP_PORT (defaults 0.0.0.0:8080).
"""

from __future__ import annotations

import os

from app.cli.migrate import run_migrate
from app.cli.runserver import run_server


def main() -> None:
    run_migrate("head")
    run_server(
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8080")),
        reload=False,
        workers=None,
    )


if __name__ == "__main__":
    main()
