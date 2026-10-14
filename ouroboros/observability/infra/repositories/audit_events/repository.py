import uuid
from contextlib import AbstractContextManager

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row

from infra.sql_transaction import SqlTransactionManager
from new_core.identity import OwnerScope
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.runtime_models import AuditEvent
from ouroboros.storage.runtime_serialization import dump_json as _dump_json, load_json as _load_json
from ouroboros.utils import utc_now_iso


class SqlAuditEventRepository(IAuditEventRepository):
    def __init__(self, transaction_manager: SqlTransactionManager) -> None:
        self.transaction_manager = transaction_manager

    @property
    def transaction(self) -> AbstractContextManager[None]:
        return self.transaction_manager.transaction

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        with self.transaction, self.transaction_manager.connection() as conn:
            insert_audit_event(conn, event)
        return event

    def list_audit_events(
        self,
        *,
        limit: int = 500,
        event_type: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        log_stream: str | None = None,
        scope: OwnerScope | None = None,
    ) -> list[AuditEvent]:
        """Audit events, narrowed to one owner's when ``scope`` says so."""
        clauses: list[str] = []
        params: dict[str, str | int] = {}
        if scope is not None:
            clauses.append("owner_id=:owner_id AND agent_id=:agent_id")
            params.update({"owner_id": scope.owner_id, "agent_id": scope.agent_id})
        if event_type:
            clauses.append("event_type = :event_type")
            params["event_type"] = event_type
        if resource_type:
            clauses.append("resource_type = :resource_type")
            params["resource_type"] = resource_type
        if resource_id:
            clauses.append("resource_id = :resource_id")
            params["resource_id"] = resource_id
        if log_stream:
            clauses.append("log_stream = :log_stream")
            params["log_stream"] = str(log_stream)
        params["limit"] = max(1, int(limit or 1))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.transaction_manager.connection() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM audit_events {where}
                    ORDER BY timestamp DESC, event_id DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).fetchall()
        events = [self._row_to_audit_event(row) for row in rows]
        return list(reversed(events))

    def _row_to_audit_event(self, row: Row[tuple[object, ...]]) -> AuditEvent:
        mapping = row._mapping
        metadata = _load_json(mapping["metadata_json"])
        payload = _load_json(mapping["payload_json"])
        if isinstance(payload, dict) and payload:
            payload.setdefault("metadata", metadata)
            # The columns are authoritative: payload_json of rows written before the 0005
            # migration carries no log_stream, and before 0010 no ownership axes.
            payload["log_stream"] = mapping["log_stream"]
            payload["owner_id"] = mapping["owner_id"]
            payload["agent_id"] = mapping["agent_id"]
            return AuditEvent.model_validate(payload)
        return AuditEvent(
            event_id=mapping["event_id"],
            owner_id=mapping["owner_id"],
            agent_id=mapping["agent_id"],
            timestamp=mapping["timestamp"],
            correlation_id=mapping["correlation_id"],
            actor_type=mapping["actor_type"],
            resource_type=mapping["resource_type"],
            resource_id=mapping["resource_id"],
            event_type=mapping["event_type"],
            log_stream=mapping["log_stream"],
            decision=mapping["decision"],
            reason=mapping["reason"],
            metadata=metadata,
        )


def insert_audit_event(conn: Connection, event: AuditEvent) -> None:
    if not event.event_id:
        event.event_id = uuid.uuid4().hex
    if not event.timestamp:
        event.timestamp = utc_now_iso()
    payload = event.model_dump(mode="json")
    conn.execute(
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
        {
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
            "metadata_json": _dump_json(event.metadata),
            "payload_json": _dump_json(payload),
        },
    )
