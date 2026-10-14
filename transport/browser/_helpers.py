"""Shared Starlette helpers for thin gateway route modules."""

from __future__ import annotations

import pathlib
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.paths import load_path_settings

_TRUE_LITERALS = frozenset({"1", "true", "yes", "on"})
_FALSE_LITERALS = frozenset({"0", "false", "no", "off"})


def request_drive_root(request: Request) -> pathlib.Path:
    """Drive root pinned on ``request.app.state`` or the configured default."""
    state = getattr(request.app, "state", None)
    drive_root = getattr(state, "drive_root", None) if state is not None else None
    return pathlib.Path(drive_root) if drive_root is not None else load_path_settings().data_dir


def request_repo_dir(request: Request) -> pathlib.Path:
    """Repo dir pinned on ``request.app.state`` or the configured default."""
    state = getattr(request.app, "state", None)
    repo_dir = getattr(state, "repo_dir", None) if state is not None else None
    return pathlib.Path(repo_dir) if repo_dir is not None else load_path_settings().repo_dir


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Best-effort bool coercion accepting common HTTP truthy/falsy literals."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_LITERALS:
            return True
        if lowered in _FALSE_LITERALS:
            return False
    return default


def coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion. Returns ``default`` on parse failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def json_error(message: str, status: int = 500, **extra: Any) -> JSONResponse:
    """Return a stable error payload without stack traces."""
    payload: dict[str, Any] = {"error": message, "ok": False}
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


def reject_unexpected_query(request: Request) -> JSONResponse | None:
    if request.query_params:
        return json_error("invalid query parameters", status=400)
    return None


class TracedRoute(Route):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        trace_id = aef_tracing.accept_trace_id_from_headers(Headers(scope=scope), source="browser")
        with aef_tracing.trace_scope(trace_id):
            await super().handle(scope, receive, send)


def state_attr(request: Request, name: str, default: Any = None) -> Any:
    app_state = getattr(request.app, "state", None)
    return getattr(app_state, name, default) if app_state is not None else default


__all__ = (
    "TracedRoute",
    "coerce_bool",
    "coerce_int",
    "json_error",
    "reject_unexpected_query",
    "request_drive_root",
    "request_repo_dir",
    "state_attr",
)
