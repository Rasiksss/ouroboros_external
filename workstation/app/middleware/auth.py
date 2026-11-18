"""Session auth middleware.

This stage only parses headers and enforces the stable shape of authenticated
requests. Session/token validation against the database is added in the next
stage together with session models and orchestrator services.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.utils.exceptions.common import AuthRequiredException, auth_required

_PUBLIC_PATHS = {
    "/docs",
    "/redoc",
    "/openapi.json",
    "/health/liveness",
    "/health/readiness",
    "/version",
    "/metrics",
    "/info",
}
_CREATE_SESSION_PATH = "/v1/sessions"
_CREATE_SESSION_SECRET_HEADER = "X-Session-Create-Secret"


@dataclass(slots=True)
class SessionAuthContext:
    bearer_token: str | None
    session_id: str | None

    @property
    def is_authenticated(self) -> bool:
        return bool(self.bearer_token and self.session_id)


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Parse bearer/session headers and reject malformed auth eagerly."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = self._normalized_path(request)
        if path in _PUBLIC_PATHS:
            request.state.session_auth = SessionAuthContext(bearer_token=None, session_id=None)
            return await call_next(request)

        if request.method == "POST" and path == _CREATE_SESSION_PATH:
            denial = self._check_create_secret(request)
            if denial is not None:
                return self._error_response(denial)
            request.state.session_auth = SessionAuthContext(bearer_token=None, session_id=None)
            return await call_next(request)

        header = request.headers.get("Authorization")
        session_id = request.headers.get("X-Session-Id")
        if not header or not header.startswith("Bearer "):
            return self._error_response(auth_required("missing-or-malformed-bearer"))

        bearer_token = header.removeprefix("Bearer ").strip()
        if not bearer_token:
            return self._error_response(auth_required("empty-bearer"))
        if not session_id:
            return self._error_response(auth_required("missing-session-id"))

        request.state.session_auth = SessionAuthContext(
            bearer_token=bearer_token,
            session_id=session_id,
        )
        return await call_next(request)

    def _check_create_secret(self, request: Request) -> AuthRequiredException | None:
        """Gate the one route that runs before a session token exists.

        Session create both provisions a sandbox and restores a caller-named workspace
        scope, so with enforcement off anyone able to route to the pod can do either.
        The secret is shared with the gateway, not per session: it answers "is this our
        caller", not "does this caller own that scope".
        """
        if not settings.IS_CREATE_SESSION_SECRET:
            return None
        provided = (request.headers.get(_CREATE_SESSION_SECRET_HEADER) or "").strip()
        if not provided:
            return auth_required("missing-session-create-secret")
        # Attacker-controlled input compared on every create: constant time, and the
        # secret itself never reaches the response or the logs.
        expected = (settings.SESSION_CREATE_SECRET or "").strip()
        if not hmac.compare_digest(provided, expected):
            return auth_required("session-create-secret-mismatch")
        return None

    def _normalized_path(self, request: Request) -> str:
        path = request.url.path.rstrip("/") or "/"
        root_path = (request.scope.get("root_path") or settings.APP_ROOT_PATH).rstrip("/")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :] or "/"
        return path

    def _error_response(self, exc: AuthRequiredException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=exc.headers,
        )
