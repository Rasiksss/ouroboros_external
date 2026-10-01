"""Core health HTTP endpoint for the gateway boundary."""

from __future__ import annotations

from starlette.requests import Request

from ouroboros.gateway.contracts import HealthResponse


async def api_health(_request: Request) -> HealthResponse:
    return {"status": "ok"}


__all__ = ["api_health"]
