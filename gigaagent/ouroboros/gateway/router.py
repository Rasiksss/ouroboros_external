"""Gateway route collection.

Starlette route single source of truth for the headless HTTP boundary.
"""

from __future__ import annotations

import pathlib

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from ouroboros.gateway.state import api_health
from ouroboros.observability.health import liveness_probe, probe_routes, readiness_probe, startup_probe
from transport.browser.router import collect_routes as collect_browser_routes


def _platform_probe_routes() -> list[Route]:
    """`/health/*` — те же ответы, что у `/start`, `/ready`, `/live`, под именами платформы.

    Платформа (wrapper 1.0.0.openclaw) спрашивает `GET /health/readiness` и
    `GET /health/liveness` и по 404 объявляет контейнер нездоровым: под уходил в
    рестарт-петлю (17 перезапусков, `Ready 2/3`) при живом приложении, потому что
    Ouroboros отдаёт пробы только по своим путям. Обработчики — те же объекты, что
    и в `probe_routes()`: второй копии логики нет, `/ready` и `/live` остаются
    контрактом (`transport/browser/contracts.py:HTTP_ENDPOINTS`). `/health` здесь
    не занимается: без browser ingress на нём висит `/api/health`.
    """
    return [
        Route("/health/startup", endpoint=startup_probe),
        Route("/health/readiness", endpoint=readiness_probe),
        Route("/health/liveness", endpoint=liveness_probe),
    ]


def collect_routes(
    *,
    data_dir: pathlib.Path,
    repo_dir: pathlib.Path | None = None,
    browser_enabled: bool = False,
) -> list[BaseRoute]:
    """Return the configured gateway route set."""

    async def api_health_http(request: Request) -> JSONResponse:
        return JSONResponse(await api_health(request))

    health_routes = [*probe_routes(), *_platform_probe_routes()]
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
