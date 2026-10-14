from abc import ABC, abstractmethod
from contextlib import AbstractContextManager

from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import AuditEvent


class IAuditEventRepository(ABC):
    @property
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        pass

    @abstractmethod
    def list_audit_events(
        self,
        *,
        scope: OwnerScope | None = None,
        limit: int = 500,
        event_type: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        log_stream: str | None = None,
    ) -> list[AuditEvent]:
        pass

    @abstractmethod
    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        pass
