"""Custom Starlette middlewares."""

from __future__ import annotations

import logging
import traceback
from time import perf_counter
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.metrics import observe_request
from app.utils.context import log_context, request_context

logger = logging.getLogger(__name__)
REQUEST_ID_HEADER = "X-Request-Id"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Stash the current `Request` in a contextvar so any layer can read it."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        token = request_context.set(request)
        try:
            return await call_next(request)
        finally:
            request_context.reset(token)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log request start/end/error with stable correlation context."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started_at = perf_counter()
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
        request.state.request_id = request_id
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        base_diagnostics = _request_diagnostics(request, request_id=request_id, route_path=route_path)

        with log_context(**{"app.request_id": request_id}):
            op = "http.request"
            logger.debug(
                f"{op} start",
                extra={
                    "diagnostics": {**base_diagnostics, "phase": "request-start"},
                    "event.category": "request",
                    "event.action": "receive",
                    "app.operation": op,
                    "event.outcome": "unknown",
                    "app.status": "accepted",
                },
            )
            try:
                response = await call_next(request)
            except Exception as exc:
                duration_ms = int((perf_counter() - started_at) * 1000)
                logger.exception(
                    f"{op} failed",
                    extra={
                        "diagnostics": {
                            **base_diagnostics,
                            "phase": "request-error",
                            "durationMs": duration_ms,
                        },
                        "event.category": "request",
                        "event.action": "failure",
                        "app.operation": op,
                        "event.outcome": "unknown",
                        "app.status": "accepted",
                        "app.duration_ms": duration_ms,
                        "error.type": type(exc).__name__,
                        "error.message": str(exc),
                        "error.stack_trace": traceback.format_exc(),
                    },
                )
                raise

            duration_ms = int((perf_counter() - started_at) * 1000)
            response.headers.setdefault(REQUEST_ID_HEADER, request_id)
            response.headers.setdefault("X-Process-Time", f"{duration_ms / 1000:.6f}")
            logger.info(
                f"{op} end",
                extra={
                    "diagnostics": {
                        **base_diagnostics,
                        "phase": "request-end",
                        "statusCode": response.status_code,
                        "durationMs": duration_ms,
                    },
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": duration_ms,
                },
            )
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Capture basic request metrics for the `/metrics` endpoint."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started_at = perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        observe_request(
            method=request.method,
            path=path,
            status_code=response.status_code,
            duration_sec=perf_counter() - started_at,
        )
        return response


def _request_diagnostics(request: Request, *, request_id: str, route_path: str) -> dict[str, object]:
    client = request.client.host if request.client is not None else None
    return {
        "requestId": request_id,
        "method": request.method,
        "path": request.url.path,
        "route": route_path,
        "query": request.url.query,
        "client": client,
        "userAgent": request.headers.get("user-agent"),
        "contentLength": request.headers.get("content-length"),
        "contentType": request.headers.get("content-type"),
    }
