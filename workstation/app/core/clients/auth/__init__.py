"""External auth-service validator.

Pattern (from llmaaj): module-level instance, lazy `_get_client()`, domain-specific
methods (`get_auth_data`), own settings in `config.py`. No shared base class.

When `AUTH_ENABLED=False`, `get_auth_data` returns `{}` and the
`UserRolePermission` dependency hands out a stub user — local dev / tests have
nothing to mock.
"""

from __future__ import annotations

import logging

import httpx
from httpx import AsyncClient

from app.core.clients.auth.config import settings
from app.core.outbound_tracing import outbound_tracing_headers
from app.utils.exceptions.common import (
    InternalServerErrorException,
    ValidationErrorException,
)

logger = logging.getLogger(__name__)


class AuthClient:
    def __init__(self) -> None:
        self._client: AsyncClient | None = None

    async def _get_client(self) -> AsyncClient:
        if self._client is None:
            logger.info("Initialize auth client base_url=%s", settings.AUTH_HOST)
            self._client = AsyncClient(
                base_url=settings.AUTH_HOST or "",
                timeout=settings.AUTH_TIMEOUT,
            )
        return self._client

    async def get_auth_data(self, auth_token: str) -> dict[str, object]:
        if not settings.AUTH_ENABLED:
            return {}
        headers = {"Authorization": auth_token, **outbound_tracing_headers()}
        logger.debug("Request auth data uri=%s", settings.AUTH_URI)
        client = await self._get_client()
        try:
            response = await client.get(settings.AUTH_URI, headers=headers)
            response.raise_for_status()
        except httpx.ConnectError as exc:
            logger.error("Auth service is not available")
            raise ValidationErrorException("Сервис авторизации недоступен") from exc
        except httpx.HTTPError as exc:
            logger.error("Auth service returned an error: %s", exc)
            raise InternalServerErrorException(str(exc)) from exc
        return dict(response.json())

    async def close(self) -> None:
        if self._client is not None:
            logger.info("Close auth client")
            await self._client.aclose()
            self._client = None


auth_client = AuthClient()
