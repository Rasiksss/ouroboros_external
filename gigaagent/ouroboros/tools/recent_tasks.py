"""Read-only access to recent task summaries for LLM-first context recovery."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.storage.runtime_models import TaskRecord
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.task_results import TERMINAL_STATUSES, _record_to_payload, list_task_results

_MAX_TASKS = 20
_PREVIEW_CHARS = 800
_ACTIVE_STATUSES = [
    "requested",
    "queued",
    "scheduled",
    "running",
    "interrupted",
    "waiting_confirmation",
]


def _coerce_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 5
    return max(1, min(_MAX_TASKS, limit))


def _preview(text: Any) -> str:
    value = str(text or "")
    if len(value) <= _PREVIEW_CHARS:
        return value
    return value[:_PREVIEW_CHARS] + f"\n... (truncated preview from {len(value)} chars)"


def _task_record(
    data: dict[str, Any],
    *,
    include_results: bool,
    include_traces: bool,
) -> dict[str, Any]:
    result = str(data.get("result") or "")
    record: dict[str, Any] = {
        "task_id": str(data.get("task_id") or data.get("id") or ""),
        "ts": str(data.get("ts") or ""),
        "status": str(data.get("status") or ""),
        "description": str(data.get("description") or data.get("text") or ""),
        "total_rounds": data.get("total_rounds"),
        "result_preview": _preview(result),
    }
    if include_results:
        record["result"] = result
    if include_traces:
        record["trace_summary"] = str(data.get("trace_summary") or "")
    return record


def _handle_recent_tasks(
    ctx: ToolContext,
    limit: int = 5,
    include_results: bool = False,
    include_traces: bool = False,
    **_kwargs: Any,
) -> str:
    """Return active and recent terminal task summaries from the runtime store."""
    drive_root = pathlib.Path(ctx.drive_root)
    task_limit = _coerce_limit(limit)
    snapshot = None
    if getattr(ctx, "detached", False):
        task = runtime_persistence.current.tasks_repo.get_task(str(ctx.task_id))
        if task is None or (task.owner_id, task.agent_id) != (ctx.scope.owner_id, ctx.scope.agent_id):
            return "ERROR: detached history snapshot unavailable"
        snapshot = [_record_to_payload(TaskRecord.model_validate(row)) for row in task.input.get("task_snapshot") or []]

    def select(statuses: list[str], count: int) -> list[dict[str, Any]]:
        if snapshot is not None:
            return [row for row in snapshot if row["status"] in statuses][:count]
        return list_task_results(
            drive_root, statuses=statuses, limit=count, activity_first=statuses != ["running"], scope=ctx.scope
        )

    payloads = select(list(TERMINAL_STATUSES), task_limit)
    tasks = [
        _task_record(item, include_results=bool(include_results), include_traces=bool(include_traces))
        for item in payloads
    ]
    return json.dumps(
        {
            "active": [
                _task_record(row, include_results=False, include_traces=False)
                for row in select(_ACTIVE_STATUSES, task_limit)
            ],
            "running": [
                {
                    "task_id": row["task_id"],
                    "status": "running",
                    "description": str(row.get("text") or row.get("description") or ""),
                    "ts": row["ts"],
                }
                for row in select(["running"], 500)
            ],
            "tasks": tasks,
            "unreadable_tasks": [],
        },
        ensure_ascii=False,
        indent=2,
    )


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="recent_tasks",
            schema=ToolSchema(
                name="recent_tasks",
                description=(
                    "Read active tasks and recent task results from this drive. Use when "
                    "prior work, continuations, retries, or incomplete context may matter."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Number of recent completed tasks to return (1-20).",
                            "default": 5,
                        },
                        "include_results": {
                            "type": "boolean",
                            "description": "Include full result text instead of only result_preview.",
                            "default": False,
                        },
                        "include_traces": {
                            "type": "boolean",
                            "description": "Include each task's trace_summary.",
                            "default": False,
                        },
                    },
                    "required": [],
                },
            ),
            handler=_handle_recent_tasks,
        ),
    ]
