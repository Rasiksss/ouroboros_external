"""FastAPI dependency that validates the Bearer token and enforces permissions.

When `settings.AUTH_ENABLED` is False (local dev / tests), the dependency
returns a stub `UserSchema` with the requested permissions already granted —
so endpoints under it are reachable without a real token.
"""

from __future__ import annotations

import logging

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.clients.auth import auth_client
from app.core.config import settings
from app.utils.auth.enums import UserPermission, UserRole
from app.utils.auth.user_schema import UserSchema
from app.utils.exceptions.common import (
    AuthRequiredException,
    PolicyDeniedException,
)

logger = logging.getLogger(__name__)


class UserRolePermission:
    """`Depends()` it in endpoints — receives `UserSchema` as the parameter type."""

    def __init__(
        self,
        permissions: list[UserPermission] | None = None,
        extra_roles: list[UserRole] | None = None,
    ) -> None:
        self.permissions = permissions or []
        self.extra_roles = extra_roles or []

    async def __call__(
        self,
        auth_creds: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    ) -> UserSchema:
        if not settings.AUTH_ENABLED:
            return self._stub_user()

        if auth_creds is None:
            raise AuthRequiredException

        raw = await auth_client.get_auth_data(auth_token=f"{auth_creds.scheme} {auth_creds.credentials}")
        user = UserSchema(**raw)

        if self.extra_roles and self._has_role(user):
            user.set_extra(True)
            return user
        if not self.permissions or self._has_permission(user):
            return user
        raise PolicyDeniedException

    def _has_role(self, user: UserSchema) -> bool:
        roles = user.roles or []
        return any(role in {r.value for r in self.extra_roles} for role in roles)

    def _has_permission(self, user: UserSchema) -> bool:
        perms = user.permissions or []
        wanted = {p.value for p in self.permissions}
        return any(p in wanted for p in perms)

    def _stub_user(self) -> UserSchema:
        stub = UserSchema(
            fio="dev-stub",
            roles=[r.value for r in self.extra_roles],
            permissions=[p.value for p in self.permissions],
            agents=[],
        )
        stub.set_extra(True)
        return stub
