from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status

from ouroboros.contracts.owner_scope import OWNER_HEADER_NAME, SETTINGS_OWNER_ID, configured_owner_id
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.store import load_settings


async def resolve_owner(request: Request) -> str:
    owner_id = str(request.headers.get(OWNER_HEADER_NAME) or "").strip()
    if owner_id:
        return owner_id
    if not load_settings().OUROBOROS_MULTIUSER:
        return SETTINGS_OWNER_ID
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"{OWNER_HEADER_NAME} is absent or empty")


def is_configured_owner(owner_id: str) -> bool:
    return owner_id == configured_owner_id()


def install_trace_middleware(app: FastAPI, *, source: str) -> None:
    @app.middleware("http")
    async def establish_trace_id(request: Request, call_next: Any) -> Response:
        trace_id = aef_tracing.accept_trace_id_from_headers(request.headers, source=source)
        with aef_tracing.trace_scope(trace_id) as active:
            response: Response = await call_next(request)
        response.headers[aef_tracing.TRACE_HEADER_NAME] = active
        return response


__all__ = ["install_trace_middleware", "is_configured_owner", "resolve_owner"]
