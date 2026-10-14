"""Task orchestration service tools."""

from __future__ import annotations

import logging
import uuid

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.agent_events.interfaces import SubtaskRequested, TaskCancellationRequested
from ouroboros.observability import aef_tracing
from ouroboros.storage.runtime_backend import STORE_FAILURES, ExecutionClaimUnavailable
from ouroboros.storage.runtime_models import TaskRecord
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.store import load_settings
from ouroboros.storage.task_results import (
    STATUS_COMPLETED,
    STATUS_REJECTED_DUPLICATE,
    STATUS_REQUESTED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    list_task_results,
    write_task_result,
)
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

MAX_SUBTASK_DEPTH = 3
MAX_SUBTASKS_PER_PARENT = 10
MAX_OUTSTANDING_SUBTASKS_PER_ROOT = 20
OUTSTANDING_SUBTASK_STATUSES = [STATUS_REQUESTED, STATUS_SCHEDULED, STATUS_RUNNING]


def _text_id(value: object) -> str:
    return str(value or "").strip()


def _current_task(ctx: ToolContext, task_id: str) -> TaskRecord | None:
    store = runtime_persistence.current.tasks_repo
    exact = store.get_current_job(task_id, scope=ctx.scope) or store.get_task(task_id)
    return exact if exact and (exact.owner_id, exact.agent_id) == (ctx.scope.owner_id, ctx.scope.agent_id) else None


def _chat_id_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _worker_slots() -> int:
    """How many foreground lanes a runtime holds, for the single-worker warning.

    Read from the setting the runtime reads, rather than from a runtime: a tool runs
    inside a run and has no business reaching for the component that started it — and on
    the day that component is another process, there is nothing there to reach for.
    """
    try:
        return max(1, int(load_settings().OUROBOROS_WORKER_SLOTS or 1))
    except Exception:
        return 1


def _current_parent_task_id(ctx: ToolContext, parent_task_id: str = "") -> str:
    explicit = _text_id(parent_task_id)
    return explicit if explicit and _current_task(ctx, explicit) else _text_id(getattr(ctx, "task_id", ""))


def _root_task_id(ctx: ToolContext, parent_task_id: str) -> str:
    current_root = _text_id(getattr(ctx, "root_task_id", "") or "")
    if current_root or not parent_task_id:
        return current_root or _text_id(getattr(ctx, "task_id", ""))
    parent = _current_task(ctx, parent_task_id)
    return (_text_id(parent.input.get("root_task_id")) or parent_task_id) if parent else _text_id(ctx.task_id)


def _schedule_task(ctx: ToolContext, description: str, context: str = "", parent_task_id: str = "") -> str:
    new_depth = int(getattr(ctx, "task_depth", 0) or 0) + 1
    if new_depth > MAX_SUBTASK_DEPTH:
        return f"ERROR: Subtask depth limit ({MAX_SUBTASK_DEPTH}) exceeded. Simplify your approach."

    parent_id = _current_parent_task_id(ctx, parent_task_id)
    root_task_id = _root_task_id(ctx, parent_id)
    chat_id = ctx.scope.chat_id

    rows = list_task_results(ctx.drive_root, statuses=OUTSTANDING_SUBTASK_STATUSES, scope=ctx.scope)
    chat_text = _chat_id_text(chat_id)
    duplicate_id = next(
        (
            _text_id(row.get("task_id") or row.get("id"))
            for row in rows
            if (
                str(row.get("description") or "").strip(),
                str(row.get("context") or "").strip(),
                _text_id(row.get("parent_task_id")),
                _text_id(row.get("root_task_id")),
                _chat_id_text(row.get("chat_id")),
            )
            == (str(description or "").strip(), str(context or "").strip(), parent_id, root_task_id, chat_text)
            and _text_id(row.get("task_id") or row.get("id"))
        ),
        "",
    )
    if duplicate_id:
        return (
            "⚠️ SCHEDULE_TASK_BLOCKED: exact duplicate task is already outstanding "
            f"({duplicate_id}). Use wait_for_task/get_task_result for that task."
        )

    parent_outstanding = sum(1 for row in rows if parent_id and _text_id(row.get("parent_task_id")) == parent_id)
    if parent_outstanding >= MAX_SUBTASKS_PER_PARENT:
        return (
            "⚠️ SCHEDULE_TASK_BLOCKED: parent subtask limit reached "
            f"({MAX_SUBTASKS_PER_PARENT} outstanding for parent {parent_id})."
        )

    root_outstanding = sum(
        bool(
            (root_task_id and _text_id(row.get("root_task_id")) == root_task_id)
            or (chat_text and not _text_id(row.get("root_task_id")) and _chat_id_text(row.get("chat_id")) == chat_text)
        )
        for row in rows
    )
    if root_outstanding >= MAX_OUTSTANDING_SUBTASKS_PER_ROOT:
        scope = root_task_id or f"chat {chat_id}"
        return (
            "⚠️ SCHEDULE_TASK_BLOCKED: root subtask limit reached "
            f"({MAX_OUTSTANDING_SUBTASKS_PER_ROOT} outstanding for {scope})."
        )

    task_id = uuid.uuid4().hex[:8]
    trace_id = str(getattr(ctx, "trace_id", "") or aef_tracing.current_trace_id())
    source = ctx.scope.surface_id
    transport = getattr(ctx, "transport", None)
    detached = bool(getattr(ctx, "detached", False))
    requested = SubtaskRequested(
        scope=ctx.scope,
        task_id=task_id,
        description=description,
        context=context or "",
        depth=new_depth,
        parent_task_id=parent_id or "",
        root_task_id=root_task_id or "",
        source=str(source),
        transport=dict(transport) if isinstance(transport, dict) and transport else {},
        trace_id=trace_id or "",
        ts=utc_now_iso(),
        suppress_ack=detached,
    )
    ctx.pending_events.append(requested)
    try:
        write_task_result(
            ctx.drive_root,
            task_id,
            STATUS_REQUESTED,
            scope=ctx.scope,
            execution_task_id=str(ctx.task_id or "") if detached else None,
            claim_token=ctx.execution_claim if detached else "",
            parent_task_id=parent_id or None,
            root_task_id=root_task_id or None,
            chat_id=chat_id,
            description=description,
            context=context,
            trace_id=trace_id or None,
            source=source,
            transport=dict(transport) if isinstance(transport, dict) and transport else None,
            result="Task request queued. Awaiting supervisor acceptance.",
        )
    except Exception as exc:
        log.warning("Failed to persist requested task status for %s", task_id, exc_info=True)
        if detached:
            ctx.pending_events[:] = [event for event in ctx.pending_events if event is not requested]
            if isinstance(exc, STORE_FAILURES):
                raise ExecutionClaimUnavailable(str(ctx.task_id or "")) from exc
            return "⚠️ SCHEDULE_TASK_BLOCKED: task request was not persisted."
    return f"Task request queued {task_id}: {description}"


def _cancel_task(ctx: ToolContext, task_id: str) -> str:
    transport = getattr(ctx, "transport", None)
    current = _current_task(ctx, task_id)
    if current is not None and current.detached and not load_settings().OUROBOROS_DURABLE_JOBS_ENABLED:
        return "ERROR: Durable jobs are disabled."
    ctx.pending_events.append(
        TaskCancellationRequested(
            scope=ctx.scope,
            task_id=task_id,
            job_id=current.job_id if current is not None and current.job_id == task_id else "",
            source=str(ctx.scope.surface_id),
            transport=dict(transport) if isinstance(transport, dict) and transport else {},
            ts=utc_now_iso(),
            suppress_ack=bool(getattr(ctx, "detached", False)),
        )
    )
    return f"Cancel requested: {task_id}"


def _get_task_result(ctx: ToolContext, task_id: str, current: TaskRecord | None = None) -> str:
    current = current or _current_task(ctx, task_id)
    if current is None:
        return f"Task {task_id}: unknown or not yet registered"
    if current.detached and not load_settings().OUROBOROS_DURABLE_JOBS_ENABLED:
        return "ERROR: Durable jobs are disabled."
    status = current.status
    result = current.result
    trace = current.metadata.get("trace_summary", "")
    if status == STATUS_COMPLETED:
        output = f"Task {task_id} [{status}]\n\n[BEGIN_SUBTASK_OUTPUT]\n{result}\n[END_SUBTASK_OUTPUT]"
    elif status == STATUS_REJECTED_DUPLICATE:
        duplicate_of = str(current.metadata.get("duplicate_of") or "?")
        output = f"Task {task_id} [{status}]: duplicate_of={duplicate_of}\n\n{result or f'Task was rejected as a duplicate of {duplicate_of}.'}"
    else:
        output = f"Task {task_id} [{status}]: {result or 'No details available.'}"
    if trace:
        output += f"\n\n[SUBTASK_TRACE]\n{trace}\n[/SUBTASK_TRACE]"
    return output


def _wait_for_task(ctx: ToolContext, task_id: str) -> str:
    current = _current_task(ctx, task_id)
    if current and current.detached and not load_settings().OUROBOROS_DURABLE_JOBS_ENABLED:
        return _get_task_result(ctx, task_id, current)
    if current and _worker_slots() <= 1 and current.status in OUTSTANDING_SUBTASK_STATUSES:
        current_task_id = _text_id(getattr(ctx, "task_id", "") or "")
        current_root_id = _text_id(getattr(ctx, "root_task_id", "") or "") or current_task_id
        target_parent_id = _text_id(current.metadata.get("parent_task_id", current.input.get("parent_task_id")))
        target_root_id = _text_id(current.metadata.get("root_task_id", current.input.get("root_task_id")))
        target_id = _text_id(task_id)
        waits_on_child = bool(current_task_id and target_parent_id == current_task_id)
        waits_on_root = bool(current_root_id and target_id == current_root_id)
        waits_within_root = bool(current_root_id and target_root_id == current_root_id and target_id != current_task_id)
        if waits_on_child or waits_on_root or waits_within_root:
            return (
                "⚠️ WAIT_BLOCKED_SINGLE_WORKER: waiting for this task can deadlock "
                "because only one task worker is available. Check status later or "
                "continue without blocking."
            )
    return _get_task_result(ctx, task_id, current)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="schedule_task",
            schema=ToolSchema(
                name="schedule_task",
                description=(
                    "Schedule a background task. Returns task_id for later retrieval. "
                    "For complex tasks, decompose into focused subtasks with clear scope."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "Task description - be specific about scope and expected deliverable",
                        },
                        "context": {
                            "type": "string",
                            "description": "Optional parent-task context: background, constraints, style guide, etc.",
                        },
                        "parent_task_id": {
                            "type": "string",
                            "description": "Optional parent task ID for tracking lineage",
                        },
                    },
                    "required": ["description"],
                },
            ),
            handler=_schedule_task,
        ),
        ToolEntry(
            name="cancel_task",
            schema=ToolSchema(
                name="cancel_task",
                description="Cancel a task by ID.",
                parameters={
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            ),
            handler=_cancel_task,
        ),
        ToolEntry(
            name="get_task_result",
            schema=ToolSchema(
                name="get_task_result",
                description="Read the result of a completed subtask. Use after schedule_task to collect results.",
                parameters={
                    "type": "object",
                    "required": ["task_id"],
                    "properties": {
                        "task_id": {"type": "string", "description": "Task ID returned by schedule_task"},
                    },
                },
            ),
            handler=_get_task_result,
        ),
        ToolEntry(
            name="wait_for_task",
            schema=ToolSchema(
                name="wait_for_task",
                description=(
                    "Check whether a subtask has completed. Returns result if done, "
                    "or the current task status if still pending/running."
                ),
                parameters={
                    "type": "object",
                    "required": ["task_id"],
                    "properties": {
                        "task_id": {"type": "string", "description": "Task ID to check"},
                    },
                },
            ),
            handler=_wait_for_task,
        ),
    ]
