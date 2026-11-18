"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.lifespan import lifespan
from app.core.middlewares import MetricsMiddleware, RequestContextMiddleware, RequestLoggingMiddleware
from app.middleware.auth import SessionAuthMiddleware
from app.utils.exceptions.handlers import exception_handlers


def _register_exception_handlers(app: FastAPI) -> None:
    for exc_class, handler in exception_handlers:
        app.add_exception_handler(exc_class, handler)


def create_application() -> FastAPI:
    """Build a FastAPI instance. Routers are included by `app/main.py`."""
    app = FastAPI(
        title=settings.APP_TITLE,
        description=settings.APP_DESCRIPTION,
        version=settings.APP_VERSION,
        root_path=settings.APP_ROOT_PATH,
        docs_url=None if settings.NODOC else "/docs",
        redoc_url=None if settings.NODOC else "/redoc",
        lifespan=lifespan,
    )

    if settings.BACKEND_CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[str(url) for url in settings.BACKEND_CORS_ORIGINS],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(SessionAuthMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    _register_exception_handlers(app)

    return app
