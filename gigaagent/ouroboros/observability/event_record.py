"""Unified recording for structured runtime events."""

from __future__ import annotations

import logging
from typing import Any, Literal

from new_core.identity import OwnerScope, ServiceJob
from ouroboros.agent_events.interfaces import IAgentEventPublisher, LiveLogLine
from ouroboros.contracts.owner_scope import service_identity
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.runtime_models import AuditEvent
from ouroboros.utils import emit_ui_log

log = logging.getLogger(__name__)

#: ``memory`` is background memory work, which has no task row to leave a trace on.
LogStream = Literal["events", "tools", "supervisor", "memory"]


def record_event(
    stream: LogStream,
    event_type: str,
    payload: dict[str, Any],
    *,
    resource_type: str = "task",
    resource_id: str = "",
    decision: str = "",
    reason: str = "",
    scope: OwnerScope | None = None,
    audit_event_repository: IAuditEventRepository,
) -> dict[str, Any]:
    """Persist an owner-scoped event, or attribute ownerless work to the runtime service."""
    actor = scope if scope is not None else service_identity(job=ServiceJob.RUNTIME_LOG).as_scope()
    event = AuditEvent.runtime_event(
        stream,
        event_type,
        payload,
        owner_id=actor.owner_id,
        agent_id=actor.agent_id,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision,
        reason=reason,
    )
    try:
        audit_event_repository.append_audit_event(event)
    except Exception:
        log.warning("Failed to persist audit event for %s", event.event_type, exc_info=True)
    if stream == "events":
        emit_ui_log(event.metadata)
    return event.metadata


def list_runtime_events(
    *,
    audit_event_repository: IAuditEventRepository,
    limit: int = 500,
    event_type: str | None = None,
    resource_id: str | None = None,
    log_stream: str = "events",
    scope: OwnerScope | None = None,
) -> list[dict[str, Any]]:
    try:
        events = audit_event_repository.list_audit_events(
            scope=scope,
            limit=limit,
            event_type=event_type,
            resource_id=resource_id,
            log_stream=log_stream,
        )
    except Exception:
        log.warning("Failed to load runtime events from audit store", exc_info=True)
        return []
    return [dict(event.metadata or {}) for event in events]


def emit_worker_log_event(
    events: IAgentEventPublisher | None,
    payload: dict[str, Any],
    *,
    scope: OwnerScope,
    log_label: str = "live log",
) -> None:
    """Report one live log line. Droppable by its kind, so a full transport costs a line."""
    if events is None:
        return
    try:
        events.publish(LiveLogLine(scope=scope, data=dict(payload)))
    except Exception:
        log.debug("Failed to emit %s event", log_label, exc_info=True)
