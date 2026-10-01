"""The ``ToolEvents`` a running tool reports through, backed by the supervisor's queue.

Sits next to ``ouroboros/tools/surface.py``: both are the concrete side of an interface a tool
reaches through its ``ToolContext``, and both are built once per task.

Everything a caller used to branch on lives here. Each method has two paths — publish to the
supervisor queue, or, when there is none, do locally whatever the supervisor's handler would have
done with the event. That is why the queue-absent behaviour differs per kind: usage still has to be
counted, a lifecycle event still has to reach the event stream and the in-process bus, and a live
log line has nowhere to go and is dropped.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any

from new_core.llm.usage import add_usage
from new_core.tools.interfaces import ToolEvents
from ouroboros.observability.event_record import record_event
from ouroboros.observability.pricing import emit_llm_usage_event
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

# Lifecycle events the supervisor republishes on the in-process bus. Mirrors the topics
# ``supervisor/events.py`` publishes from its handlers, so the no-queue path delivers to the
# same subscribers the queue path would have reached. An event type absent from here is
# recorded to the event stream and nothing more.
_LIFECYCLE_BUS_TOPICS: dict[str, str] = {
    "skill_exec_finished": "skill.lifecycle",
    "skill_exec_failed": "skill.lifecycle",
}


class QueueToolEvents(ToolEvents):
    """Reports a task's events onto the supervisor queue, with the local fallbacks.

    Task identity is bound at construction rather than passed per call: a tool cannot emit an
    event attributed to another task, and cannot forget to attribute one.
    """

    def __init__(
        self,
        event_queue: Any | None,
        drive_root: pathlib.Path | None,
        *,
        task_id: str = "",
        task_type: str = "task",
        task_usage: dict[str, Any] | None = None,
    ) -> None:
        self._queue = event_queue
        self._drive_root = pathlib.Path(drive_root) if drive_root is not None else None
        self._task_id = str(task_id or "")
        self._task_type = str(task_type or "task")
        # The running task's totals, held by reference so the loop and the tools accumulate
        # into one dict. ``None`` for a caller outside a task (the safety supervisor's
        # context-free sink), which has no task total to add to.
        self._task_usage = task_usage

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
        if self._queue is not None:
            # Swallows its own exceptions; every usage producer goes through it, so a tool
            # call's usage lands in the same shape as the loop's.
            emit_llm_usage_event(
                self._queue,
                self._task_id,
                model,
                usage,
                category=category,
                provider=provider,
                source=source,
            )
            return
        try:
            # Local import: the fallback is the one place the tool layer needs the
            # supervisor's counters, and a module-level import would point the dependency
            # backwards at import time.
            from supervisor.state import update_usage_from_llm

            update_usage_from_llm(usage)
        except Exception:
            log.debug("Failed to account %s usage locally", category, exc_info=True)

    def lifecycle(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {
            "type": event_type,
            "ts": utc_now_iso(),
            "task_id": self._task_id,
            **payload,
        }
        if self._queue is not None:
            try:
                self._queue.put_nowait(event)
                return
            except Exception:
                log.debug("Could not queue %s lifecycle event", event_type, exc_info=True)
        self._record_lifecycle(event_type, event)

    def _record_lifecycle(self, event_type: str, event: dict[str, Any]) -> None:
        """Do what the supervisor's handler would have done, in this process."""
        if self._drive_root is not None:
            try:
                record_event(self._drive_root, "events", event_type, event)
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


__all__ = ["QueueToolEvents"]
