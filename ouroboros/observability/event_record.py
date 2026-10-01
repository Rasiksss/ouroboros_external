"""Unified recording for structured runtime events."""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Literal

from ouroboros.storage.runtime_models import AuditEvent
from ouroboros.storage.runtime_store import get_runtime_store
from ouroboros.utils import _emit_log_sink, emit_log_event, utc_now_iso

log = logging.getLogger(__name__)

LogStream = Literal["events", "tools", "supervisor"]


def record_event(
    drive_root: pathlib.Path,
    stream: LogStream,
    event_type: str,
    payload: dict[str, Any],
    *,
    resource_type: str = "task",
    resource_id: str = "",
    decision: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Persist a runtime event to the audit_events table under its log_stream."""
    event_payload = {
        "ts": str(payload.get("ts") or utc_now_iso()),
        "type": str(event_type or payload.get("type") or "event"),
        "log_stream": stream,
        "log_kind": "runtime_event",
        **payload,
    }
    try:
        store = get_runtime_store(drive_root)
        if store is not None:
            store.append_audit_event(
                AuditEvent(
                    timestamp=str(event_payload.get("ts") or utc_now_iso()),
                    event_type=str(event_payload.get("type") or "event"),
                    log_stream=stream,
                    resource_type=resource_type,
                    resource_id=str(resource_id or event_payload.get("task_id") or ""),
                    decision=decision,
                    reason=reason,
                    metadata=event_payload,
                )
            )
    except Exception:
        log.debug("Failed to persist audit event for %s", event_payload["type"], exc_info=True)
    if stream == "events":
        _emit_log_sink(event_payload)
    return event_payload


def list_runtime_events(
    drive_root: pathlib.Path,
    *,
    limit: int = 500,
    event_type: str | None = None,
    resource_id: str | None = None,
    log_stream: str = "events",
) -> list[dict[str, Any]]:
    try:
        store = get_runtime_store(drive_root)
        if store is None:
            return []
        events = store.list_audit_events(
            limit=limit,
            event_type=event_type,
            resource_id=resource_id,
            log_stream=log_stream,
        )
    except Exception:
        log.debug("Failed to load runtime events from audit store", exc_info=True)
        return []
    return [dict(event.metadata or {}) for event in events]


def emit_worker_log_event(
    event_queue: Any,
    payload: dict[str, Any],
    *,
    blocking: bool = False,
    log_label: str = "live log",
) -> None:
    emit_log_event(
        event_queue=event_queue,
        payload=payload,
        blocking=blocking,
        log_label=log_label,
    )
