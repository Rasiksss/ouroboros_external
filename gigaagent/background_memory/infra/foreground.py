"""Reading foreground pressure off the supervisor's task table."""

from __future__ import annotations

import logging

from background_memory.infra.repositories import SqlTaskRepo
from background_memory.interfaces import IForegroundPressure

log = logging.getLogger(__name__)

#: What counts as foreground work in hand. No exclusion set: no memory rows exist
#: to exclude — both memory task types are retired.
_ACTIVE_TASK_STATUSES = ("queued", "scheduled", "interrupted", "running")


class TaskTableForegroundPressure(IForegroundPressure):
    """Foreground work is any task the supervisor has not closed yet.

    The task table is where a single-process supervisor keeps that fact; a multi-instance
    one puts it in shared state and swaps this class, leaving the handler as it is.
    """

    def __init__(self, tasks: SqlTaskRepo) -> None:
        self._tasks = tasks

    async def is_pending(self) -> bool:
        try:
            return bool(await self._tasks.list_tasks(statuses=_ACTIVE_TASK_STATUSES, limit=1))
        except Exception:
            # Yield on doubt: a background run that cannot see the foreground must
            # not decide it has the machine to itself.
            log.warning("Failed to read foreground task pressure", exc_info=True)
            return True


__all__ = ["TaskTableForegroundPressure"]
