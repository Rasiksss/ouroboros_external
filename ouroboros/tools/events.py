"""The ``ToolEvents`` a running tool reports through, backed by the run's publisher.

Sits next to ``ouroboros/tools/surface.py``: both are the concrete side of an interface a tool
reaches through its ``ToolContext``, and both are built once per task.

Everything a caller used to branch on lives here. Each method has two paths — publish onto the
seam, or, when the run has no publisher, do locally whatever the consuming component would have
done with the message. That is why the unpublished behaviour differs per kind: usage still has to
be counted, a lifecycle event still has to reach the event stream and the in-process bus, and a
live log line has nowhere to go and is dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from new_core.identity import OwnerScope
from new_core.llm.usage import add_usage
from new_core.tools.interfaces import ToolEvents
from ouroboros.agent_events.interfaces import IAgentEventPublisher, SkillLifecycle
from ouroboros.observability.event_record import LogStream, record_event
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.observability.pricing import emit_llm_usage_event
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

# Lifecycle events the consuming component republishes on the in-process bus. Mirrors the
# topics ``supervisor/events.py`` publishes from its handlers, so the unpublished path reaches
# the same subscribers the published one would have. An event type absent from here is
# recorded to the event stream and nothing more.
_LIFECYCLE_BUS_TOPICS: dict[str, str] = {
    "skill_exec_finished": "skill.lifecycle",
    "skill_exec_failed": "skill.lifecycle",
}


class PublishedToolEvents(ToolEvents):
    """Reports a task's events onto the run's publisher, with the local fallbacks.

    Task identity is bound at construction rather than passed per call: a tool cannot emit an
    event attributed to another task, and cannot forget to attribute one.
    """

    def __init__(
        self,
        events: IAgentEventPublisher | None,
        *,
        audit_event_repository: IAuditEventRepository | None,
        task_id: str = "",
        task_type: str = "task",
        task_usage: dict[str, Any] | None = None,
        scope: OwnerScope | None = None,
    ) -> None:
        self._audit_event_repository = audit_event_repository
        self._events = events
        self._task_id = str(task_id or "")
        self._task_type = str(task_type or "task")
        # The running task's totals, held by reference so the loop and the tools accumulate
        # into one dict. ``None`` for a caller outside a task (the safety supervisor's
        # context-free sink), which has no task total to add to.
        self._task_usage = task_usage
        #: Whose work these tools run for, so their spend is recorded under that owner.
        #: ``None`` for the context-free sink, which has no publisher to report onto either.
        self._scope = scope

    def audit(
        self,
        stream: LogStream,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        scope: OwnerScope | None,
    ) -> None:
        if self._audit_event_repository is None:
            return
        record_event(
            stream,
            event_type,
            dict(payload),
            scope=scope,
            audit_event_repository=self._audit_event_repository,
        )

    def usage(
        self,
        model: str,
        usage: dict[str, Any],
        *,
        category: str = "task",
        provider: str | None = None,
        source: str = "tool",
    ) -> None:
        # Two accountings, not one. The event below is the supervisor's, which bills every
        # model call whoever made it; this is the *task's* own total, the number published
        # in its result and shown to the model at each checkpoint, so only work billed to
        # the task counts. Safety-supervisor calls are infrastructure and stay out.
        if self._task_usage is not None and category == "task":
            try:
                add_usage(self._task_usage, usage)
            except Exception:
                log.debug("Failed to add %s usage to the task total", source, exc_info=True)
        if self._events is not None:
            # Swallows its own exceptions; every usage producer goes through it, so a tool
            # call's usage lands in the same shape as the loop's.
            emit_llm_usage_event(
                self._events,
                self._task_id,
                model,
                usage,
                category=category,
                provider=provider,
                source=source,
                scope=self._scope,
            )
            return
        try:
            # Local import: the fallback is the one place the tool layer needs the
            # supervisor's counters, and a module-level import would point the dependency
            # backwards at import time.
            from supervisor.state import update_usage_from_llm

            update_usage_from_llm(usage, owner_scope=self._scope)
        except Exception:
            log.debug("Failed to account %s usage locally", category, exc_info=True)

    def lifecycle(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "type": event_type,
            "ts": utc_now_iso(),
            "task_id": self._task_id,
            **payload,
        }
        if self._events is not None and self._scope is not None:
            try:
                self._events.publish(
                    SkillLifecycle(
                        scope=self._scope,
                        event_type=str(event_type or ""),
                        task_id=self._task_id,
                        payload=dict(event),
                        ts=str(event.get("ts") or ""),
                    )
                )
                return
            except Exception:
                log.debug("Could not publish the %s lifecycle event", event_type, exc_info=True)
        self._record_lifecycle(event_type, event)

    def request_subtask(self, request: Any) -> bool:
        if self._events is None:
            return False
        try:
            self._events.publish(request)
        except Exception:
            # Not reported at all would lose the child; the caller keeps it for the run result.
            log.warning("Could not hand subtask %s to the supervisor", getattr(request, "task_id", ""), exc_info=True)
            return False
        return True

    def _record_lifecycle(self, event_type: str, event: dict[str, Any]) -> None:
        """Do what the consuming component's handler would have done, in this process."""
        if self._audit_event_repository is not None:
            try:
                self.audit("events", event_type, event, scope=self._scope)
            except Exception:
                log.debug("Could not append %s lifecycle event", event_type, exc_info=True)
        topic = _LIFECYCLE_BUS_TOPICS.get(event_type)
        if topic is None:
            return
        try:
            from ouroboros.observability.event_bus import publish_event

            publish_event(topic, event)
        except Exception:
            log.debug("Could not publish %s lifecycle event", event_type, exc_info=True)


__all__ = ["PublishedToolEvents"]
