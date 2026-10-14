from __future__ import annotations

import hashlib
import hmac
import json

from pydantic import SecretStr

from infra.credentials_key import CredentialsKey
from new_core.identity import OwnerScope

_DOMAIN_LABEL = b"ouroboros:a2a-task-handle:v1"
_SEPARATOR = "~"
_MAC_HEX_LENGTH = 32


class A2aTaskHandles:
    def __init__(self, *, encryption_key: SecretStr) -> None:
        self._key = CredentialsKey(encryption_key).derive(_DOMAIN_LABEL)

    def seal(self, scope: OwnerScope, remote_agent_id: str, task_id: str) -> str:
        return f"{task_id}{_SEPARATOR}{self._mac(scope, remote_agent_id, task_id)}"

    def open(self, scope: OwnerScope, remote_agent_id: str, handle: str) -> str | None:
        task_id, separator, mac = handle.rpartition(_SEPARATOR)
        if not (separator and task_id):
            return None
        expected = self._mac(scope, remote_agent_id, task_id)
        if not hmac.compare_digest(mac.encode("utf-8"), expected.encode("ascii")):
            return None
        return task_id

    def is_handle_format(self, handle: str) -> bool:
        task_id, separator, _ = handle.rpartition(_SEPARATOR)
        return bool(separator and task_id)

    def _mac(self, scope: OwnerScope, remote_agent_id: str, task_id: str) -> str:
        payload = json.dumps(
            ["a2a-task", remote_agent_id, task_id, scope.owner_id, scope.agent_id], separators=(",", ":")
        ).encode("utf-8")
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()[:_MAC_HEX_LENGTH]


__all__ = ["A2aTaskHandles"]
