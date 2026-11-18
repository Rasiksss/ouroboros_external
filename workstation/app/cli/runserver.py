"""Launch uvicorn from the CLI."""

from __future__ import annotations

import uvicorn

from app.core.config import settings
from app.utils.logging import get_log_config


def run_server(
    host: str | None = None,
    port: int | None = None,
    reload: bool | None = None,
    workers: int | None = None,
) -> None:
    use_reload = settings.APP_IS_RELOAD if reload is None else reload
    kwargs: dict[str, object] = {
        "host": host or settings.APP_HOST,
        "port": port or settings.APP_PORT,
        "reload": use_reload,
        "log_config": get_log_config("logging.json"),
    }
    if not use_reload and (workers or settings.APP_WORKERS) > 1:
        kwargs["workers"] = workers or settings.APP_WORKERS
    uvicorn.run("app.main:app", **kwargs)
