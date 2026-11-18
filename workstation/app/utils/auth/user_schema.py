"""Shape of the current-user object injected by `UserRolePermission`."""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, PrivateAttr

from app.utils.exceptions.common import PolicyDeniedException

logger = logging.getLogger(__name__)


class UserSchema(BaseModel):
    """Flat projection of the external auth response.

    `extra="allow"` so the downstream provider can add fields without breaking us.
    """

    fio: str | None = ""
    roles: list[str | None] | None = []
    permissions: list[str | None] | None = []
    agents: list[str | None] | None = []

    model_config = ConfigDict(extra="allow")

    _is_extra: bool = PrivateAttr(default=False)

    def check_agents_permission(self, agent_ids: list[str]) -> None:
        """Raise 403 unless the user is attached to every requested agent."""
        logger.debug("Check agents permission for %s", self.fio)
        if self._is_extra:
            return
        if not all(agent_id in (self.agents or []) for agent_id in agent_ids):
            raise PolicyDeniedException

    def set_extra(self, value: bool) -> None:
        self._is_extra = value
