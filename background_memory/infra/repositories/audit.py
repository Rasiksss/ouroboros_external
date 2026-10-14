from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from background_memory.infra.repositories._rows import dump_json
from ouroboros.storage.runtime_models import AuditEvent
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


class SqlAuditRepo:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def claim(self, event: AuditEvent) -> bool:
        try:
            await self._append(event)
        except IntegrityError:
            return False
        return True

    async def record(self, event: AuditEvent) -> None:
        """A run's own trail. Absorbed on failure: there is no task row to report to instead."""
        try:
            await self._append(event)
        except (SQLAlchemyError, OSError):
            log.warning("Failed to record a background memory audit event %s", event.event_type, exc_info=True)

    async def _append(self, event: AuditEvent) -> None:
        if not event.event_id:
            event.event_id = uuid.uuid4().hex
        if not event.timestamp:
            event.timestamp = utc_now_iso()
        row = {
            "event_id": event.event_id,
            "owner_id": event.owner_id,
            "agent_id": event.agent_id,
            "timestamp": event.timestamp,
            "correlation_id": event.correlation_id,
            "actor_type": event.actor_type,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "event_type": event.event_type,
            "log_stream": event.log_stream or "events",
            "decision": event.decision,
            "reason": event.reason,
            "metadata_json": dump_json(event.metadata),
            "payload_json": dump_json(event.model_dump(mode="json")),
        }
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        event_id, owner_id, agent_id, timestamp, correlation_id, actor_type,
                        resource_type, resource_id, event_type, log_stream, decision, reason,
                        metadata_json, payload_json
                    ) VALUES (
                        :event_id, :owner_id, :agent_id, :timestamp, :correlation_id, :actor_type,
                        :resource_type, :resource_id, :event_type, :log_stream, :decision, :reason,
                        :metadata_json, :payload_json
                    )
                    """
                ),
                row,
            )


__all__ = ["SqlAuditRepo"]
