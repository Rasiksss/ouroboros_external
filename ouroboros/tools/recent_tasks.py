"""Read-only access to recent task summaries for LLM-first context recovery."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.storage.runtime_store import get_runtime_store, init_runtime_store
from ouroboros.storage.task_results import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED_DUPLICATE,
    list_task_results,
)

_MAX_TASKS = 20
_PREVIEW_CHARS = 800
_TERMINAL_STATUSES = [
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED_DUPLICATE,
    STATUS_CANCELLED,
    STATUS_BUDGET_EXCEEDED,
]
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


def _running_tasks(drive_root: pathlib.Path) -> list[dict[str, Any]]:
    store = get_runtime_store(drive_root) or init_runtime_store(drive_root)
    rows: list[dict[str, Any]] = []
    for record in store.list_tasks(statuses=["running"]):
        task = dict(record.input)
        rows.append(
            {
                "task_id": record.task_id,
                "status": "running",
                "description": str(task.get("text") or task.get("description") or ""),
                "ts": str(record.started_at or record.created_at or ""),
            }
        )
    return rows


def _active_tasks(drive_root: pathlib.Path, limit: int) -> list[dict[str, Any]]:
    payloads = list_task_results(
        drive_root,
        statuses=_ACTIVE_STATUSES,
        limit=limit,
        activity_first=True,
    )
    return [_task_record(item, include_results=False, include_traces=False) for item in payloads]


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
    payloads = list_task_results(
        drive_root,
        statuses=_TERMINAL_STATUSES,
        limit=task_limit,
        activity_first=True,
    )
    tasks = [
        _task_record(item, include_results=bool(include_results), include_traces=bool(include_traces))
        for item in payloads
    ]
    return json.dumps(
        {
            "active": _active_tasks(drive_root, task_limit),
            "running": _running_tasks(drive_root),
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
