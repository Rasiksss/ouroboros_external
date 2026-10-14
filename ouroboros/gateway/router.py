"""Gateway route collection.

Starlette route single source of truth for the headless HTTP boundary.
"""

from __future__ import annotations

import pathlib

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from ouroboros.gateway.state import api_health
from ouroboros.observability.health import probe_routes
from transport.browser.router import collect_routes as collect_browser_routes


def collect_routes(
    *,
    data_dir: pathlib.Path,
    repo_dir: pathlib.Path | None = None,
    browser_enabled: bool = False,
) -> list[BaseRoute]:
    """Return the configured gateway route set."""

    async def api_health_http(request: Request) -> JSONResponse:
        return JSONResponse(await api_health(request))

    health_routes = probe_routes()
    if browser_enabled:
        return [
            *collect_browser_routes(
                data_dir=data_dir,
                repo_dir=repo_dir or pathlib.Path.cwd(),
            ),
            *health_routes,
        ]
    return [*health_routes, Route("/api/health", endpoint=api_health_http)]


__all__ = ["collect_routes"]
