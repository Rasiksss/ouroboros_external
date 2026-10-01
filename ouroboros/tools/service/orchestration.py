"""Task orchestration service tools."""

from __future__ import annotations

import logging
import uuid

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_record import record_event
from ouroboros.storage.task_results import (
    STATUS_COMPLETED,
    STATUS_REJECTED_DUPLICATE,
    STATUS_REQUESTED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    list_task_results,
    load_task_result,
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


def _chat_id(ctx: ToolContext) -> object | None:
    value = getattr(ctx, "current_chat_id", None)
    if value is None or str(value).strip() == "":
        return None
    return value


def _chat_id_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _worker_slots() -> int:
    """Return available foreground task slots, defaulting to the safe single-worker mode."""
    try:
        from supervisor.runtime_context import try_get_executor

        executor = try_get_executor()
        if executor is not None:
            slots = executor.all_slots() if hasattr(executor, "all_slots") else []
            if slots:
                return max(1, len(slots))
    except Exception:
        pass
    try:
        from ouroboros.storage.settings.store import load_settings

        return max(1, int(load_settings().OUROBOROS_WORKER_SLOTS or 1))
    except Exception:
        return 1


def _current_parent_task_id(ctx: ToolContext, parent_task_id: str = "") -> str:
    explicit_parent = _text_id(parent_task_id)
    if explicit_parent:
        return explicit_parent
    return _text_id(getattr(ctx, "task_id", "") or "")


def _root_task_id(ctx: ToolContext, parent_task_id: str) -> str:
    current_root = _text_id(getattr(ctx, "root_task_id", "") or "")
    if current_root:
        return current_root
    if parent_task_id:
        parent_data = load_task_result(ctx.drive_root, parent_task_id) or {}
        parent_root = _text_id(parent_data.get("root_task_id"))
        if parent_root:
            return parent_root
        return parent_task_id
    return _text_id(getattr(ctx, "task_id", "") or "")


def _outstanding_children_for_parent(ctx: ToolContext, parent_task_id: str) -> int:
    if not parent_task_id:
        return 0
    rows = list_task_results(ctx.drive_root, statuses=OUTSTANDING_SUBTASK_STATUSES)
    return sum(1 for row in rows if _text_id(row.get("parent_task_id")) == parent_task_id)


def _outstanding_children_for_root(ctx: ToolContext, root_task_id: str, chat_id: object | None) -> int:
    if not root_task_id and chat_id is None:
        return 0
    chat_text = _chat_id_text(chat_id)
    rows = list_task_results(ctx.drive_root, statuses=OUTSTANDING_SUBTASK_STATUSES)
    count = 0
    for row in rows:
        row_root = _text_id(row.get("root_task_id"))
        if root_task_id and row_root == root_task_id:
            count += 1
            continue
        if chat_text and not row_root and _chat_id_text(row.get("chat_id")) == chat_text:
            count += 1
    return count


def _exact_duplicate_task_id(
    ctx: ToolContext,
    *,
    description: str,
    context: str,
    parent_task_id: str,
    root_task_id: str,
    chat_id: object | None,
) -> str:
    chat_text = _chat_id_text(chat_id)
    desc = str(description or "").strip()
    task_context = str(context or "").strip()
    rows = list_task_results(ctx.drive_root, statuses=OUTSTANDING_SUBTASK_STATUSES)
    for row in rows:
        if str(row.get("description") or "").strip() != desc:
            continue
        if str(row.get("context") or "").strip() != task_context:
            continue
        if _text_id(row.get("parent_task_id")) != parent_task_id:
            continue
        if _text_id(row.get("root_task_id")) != root_task_id:
            continue
        if _chat_id_text(row.get("chat_id")) != chat_text:
            continue
        task_id = _text_id(row.get("task_id") or row.get("id"))
        if task_id:
            return task_id
    return ""


def _schedule_task(ctx: ToolContext, description: str, context: str = "", parent_task_id: str = "") -> str:
    if getattr(ctx, "is_direct_chat", False) and _worker_slots() <= 1:
        return (
            "⚠️ TOOL_SCHEDULE_TASK_BLOCKED_DIRECT_CHAT_SINGLE_WORKER: "
            "direct chat has only one task worker, so scheduling a subtask here "
            "would not be able to run safely. Continue in the current task."
        )

    new_depth = int(getattr(ctx, "task_depth", 0) or 0) + 1
    if new_depth > MAX_SUBTASK_DEPTH:
        return f"ERROR: Subtask depth limit ({MAX_SUBTASK_DEPTH}) exceeded. Simplify your approach."

    parent_id = _current_parent_task_id(ctx, parent_task_id)
    root_task_id = _root_task_id(ctx, parent_id)
    chat_id = _chat_id(ctx)

    duplicate_id = _exact_duplicate_task_id(
        ctx,
        description=description,
        context=context,
        parent_task_id=parent_id,
        root_task_id=root_task_id,
        chat_id=chat_id,
    )
    if duplicate_id:
        return (
            "⚠️ SCHEDULE_TASK_BLOCKED: exact duplicate task is already outstanding "
            f"({duplicate_id}). Use wait_for_task/get_task_result for that task."
        )

    parent_outstanding = _outstanding_children_for_parent(ctx, parent_id)
    if parent_outstanding >= MAX_SUBTASKS_PER_PARENT:
        return (
            "⚠️ SCHEDULE_TASK_BLOCKED: parent subtask limit reached "
            f"({MAX_SUBTASKS_PER_PARENT} outstanding for parent {parent_id})."
        )

    root_outstanding = _outstanding_children_for_root(ctx, root_task_id, chat_id)
    if root_outstanding >= MAX_OUTSTANDING_SUBTASKS_PER_ROOT:
        scope = root_task_id or f"chat {chat_id}"
        return (
            "⚠️ SCHEDULE_TASK_BLOCKED: root subtask limit reached "
            f"({MAX_OUTSTANDING_SUBTASKS_PER_ROOT} outstanding for {scope})."
        )

    if getattr(ctx, "is_direct_chat", False):
        try:
            record_event(
                ctx.drive_root,
                "events",
                "schedule_task_from_direct_chat",
                {
                    "ts": utc_now_iso(),
                    "description": description[:200],
                    "warning": "schedule_task called from direct chat context - potential duplicate work",
                },
            )
        except Exception:
            log.debug("Failed to record direct-chat schedule_task warning", exc_info=True)

    task_id = uuid.uuid4().hex[:8]
    trace_id = str(getattr(ctx, "trace_id", "") or aef_tracing.current_trace_id())
    evt = {
        "type": "schedule_task",
        "description": description,
        "task_id": task_id,
        "depth": new_depth,
        "ts": utc_now_iso(),
    }
    if context:
        evt["context"] = context
    if parent_id:
        evt["parent_task_id"] = parent_id
    if root_task_id:
        evt["root_task_id"] = root_task_id
    if chat_id is not None:
        evt["chat_id"] = chat_id
    if trace_id:
        evt["trace_id"] = trace_id
    source = str(getattr(ctx, "current_task_source", "") or "").strip()
    if source:
        evt["source"] = source
    conversation_id = str(getattr(ctx, "conversation_id", "") or "").strip()
    if conversation_id:
        evt["conversation_id"] = conversation_id
    transport = getattr(ctx, "transport", None)
    if isinstance(transport, dict) and transport:
        evt["transport"] = dict(transport)
    ctx.pending_events.append(evt)
    try:
        write_task_result(
            ctx.drive_root,
            task_id,
            STATUS_REQUESTED,
            parent_task_id=parent_id or None,
            root_task_id=root_task_id or None,
            chat_id=chat_id,
            description=description,
            context=context,
            trace_id=trace_id or None,
            source=source,
            conversation_id=conversation_id,
            transport=dict(transport) if isinstance(transport, dict) and transport else None,
            result="Task request queued. Awaiting supervisor acceptance.",
        )
    except Exception:
        log.warning("Failed to persist requested task status for %s", task_id, exc_info=True)
    return f"Task request queued {task_id}: {description}"


def _cancel_task(ctx: ToolContext, task_id: str) -> str:
    evt: dict[str, object] = {"type": "cancel_task", "task_id": task_id, "ts": utc_now_iso()}
    chat_id = _chat_id(ctx)
    if chat_id is not None:
        evt["chat_id"] = chat_id
    source = str(getattr(ctx, "current_task_source", "") or "").strip()
    if source:
        evt["source"] = source
    transport = getattr(ctx, "transport", None)
    if isinstance(transport, dict) and transport:
        evt["transport"] = dict(transport)
    ctx.pending_events.append(evt)
    return f"Cancel requested: {task_id}"


def _get_task_result(ctx: ToolContext, task_id: str) -> str:
    data = load_task_result(ctx.drive_root, task_id)
    if not data:
        return f"Task {task_id}: unknown or not yet registered"
    status = data.get("status", "unknown")
    result = data.get("result", "")
    trace = data.get("trace_summary", "")
    if status == STATUS_COMPLETED:
        output = f"Task {task_id} [{status}]\n\n[BEGIN_SUBTASK_OUTPUT]\n{result}\n[END_SUBTASK_OUTPUT]"
    elif status == STATUS_REJECTED_DUPLICATE:
        duplicate_of = str(data.get("duplicate_of") or "?")
        output = (
            f"Task {task_id} [{status}]: duplicate_of={duplicate_of}\n\n"
            f"{result or f'Task was rejected as a duplicate of {duplicate_of}.'}"
        )
    else:
        output = f"Task {task_id} [{status}]: {result or 'No details available.'}"
    if trace:
        output += f"\n\n[SUBTASK_TRACE]\n{trace}\n[/SUBTASK_TRACE]"
    return output


def _wait_for_task(ctx: ToolContext, task_id: str) -> str:
    data = load_task_result(ctx.drive_root, task_id)
    if data and _worker_slots() <= 1 and str(data.get("status") or "") in OUTSTANDING_SUBTASK_STATUSES:
        current_task_id = _text_id(getattr(ctx, "task_id", "") or "")
        current_root_id = _text_id(getattr(ctx, "root_task_id", "") or "") or current_task_id
        target_parent_id = _text_id(data.get("parent_task_id"))
        target_root_id = _text_id(data.get("root_task_id"))
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
    return _get_task_result(ctx, task_id)


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
