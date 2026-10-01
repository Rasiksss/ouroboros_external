"""Shared Starlette helpers for thin gateway route modules."""

from __future__ import annotations

import pathlib
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.app.config import DATA_DIR, REPO_DIR

_TRUE_LITERALS = frozenset({"1", "true", "yes", "on"})
_FALSE_LITERALS = frozenset({"0", "false", "no", "off"})


def request_drive_root(request: Request) -> pathlib.Path:
    """Drive root pinned on ``request.app.state`` or the configured default."""
    state = getattr(request.app, "state", None)
    drive_root = getattr(state, "drive_root", None) if state is not None else None
    return pathlib.Path(drive_root) if drive_root is not None else pathlib.Path(DATA_DIR)


def request_repo_dir(request: Request) -> pathlib.Path:
    """Repo dir pinned on ``request.app.state`` or the configured default."""
    state = getattr(request.app, "state", None)
    repo_dir = getattr(state, "repo_dir", None) if state is not None else None
    return pathlib.Path(repo_dir) if repo_dir is not None else pathlib.Path(REPO_DIR)


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


def state_attr(request: Request, name: str, default: Any = None) -> Any:
    app_state = getattr(request.app, "state", None)
    return getattr(app_state, name, default) if app_state is not None else default


__all__ = (
    "coerce_bool",
    "coerce_int",
    "json_error",
    "reject_unexpected_query",
    "request_drive_root",
    "request_repo_dir",
    "state_attr",
)
