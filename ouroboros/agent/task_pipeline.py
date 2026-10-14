"""Post-task result emission, working-memory episodes, and review context.

Memory distillation used to live here too, between the enqueue helper and the
tool loop that ran it. Both moved to ``background_memory``: the agent now reports
a completed task and decides nothing about it.
"""

from __future__ import annotations

import logging
import pathlib
import time
from typing import TYPE_CHECKING, Any

from new_core.identity import OwnerScope
from ouroboros.agent.working_retrieval import episode_search_terms
from ouroboros.agent_events.interfaces import (
    AgentEvent,
    FileSendRequested,
    LiveLogLine,
    LlmUsageReported,
    OutboundText,
    OwnerMessageInjected,
    PhotoSendRequested,
    SkillLifecycle,
    SubtaskRequested,
    TaskCancellationRequested,
    TaskFinished,
    TaskHeartbeat,
    TaskMetrics,
)
from ouroboros.observability.event_record import record_event
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.runtime_backend import STORE_FAILURES, ExecutionClaimUnavailable
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.store import load_settings
from ouroboros.storage.task_episodes import store_task_episode
from ouroboros.storage.task_results import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_REQUESTED,
    TERMINAL_STATUSES,
    load_task_result,
    write_task_result,
)
from ouroboros.utils import build_trace_summary, utc_now_iso

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)


def _record_working_task_episode(
    env: Any,
    task: dict[str, Any],
    text: str,
    llm_trace: dict[str, Any],
    scope: OwnerScope,
    execution_memory_repository: IExecutionMemoryRepository,
) -> None:
    try:
        settings = load_settings()
        store_task_episode(
            task,
            text,
            llm_trace,
            scope=scope,
            execution_memory_repository=execution_memory_repository,
            search_terms=episode_search_terms(task, text, llm_trace),
            retention_days=int(settings.OUROBOROS_EPISODE_RETENTION_DAYS or 90),
        )
    except STORE_FAILURES:
        # The episode is best-effort and must not fail the task; a bug in the write path is
        # not best-effort and surfaces.
        log.warning("Failed to record working-memory task episode", exc_info=True)


def _late_result_marker(record: dict[str, Any]) -> str:
    """Preface naming why and when the task was stopped, for an answer that finished after."""
    status = str(record.get("status") or "").strip()
    when = str(record.get("ts") or "").strip()
    reason = " ".join(str(record.get("result") or "").split())[:200]
    detail = f"{status}: {reason}" if status and reason else (reason or status or "stopped")
    stopped_at = f" at {when}" if when else ""
    return (
        f"\u23f3 Late result \u2014 this task had already been stopped{stopped_at} ({detail}). "
        "The answer below finished after that; it is kept in case it is still useful."
    )


def _load_stored_record(env: Any, task_id: str) -> dict[str, Any] | None:
    """The stored record for this task, or ``None`` when it cannot be read."""
    try:
        return load_task_result(env.drive_root, task_id) or {}
    except Exception:
        log.debug("Failed to read the stored task record", exc_info=True)
        return None


def _settle_cancelled_child_requests(pending_events: list[AgentEvent], scope: OwnerScope, task_id: str) -> None:
    """Cancel and suppress children whose schedule call lost a race with the parent stop."""
    requests = [event for event in pending_events if isinstance(event, SubtaskRequested)]
    if not requests:
        return
    pending_events[:] = [event for event in pending_events if not isinstance(event, SubtaskRequested)]
    store = runtime_persistence.current.tasks_repo
    try:
        for event in requests:
            child = store.get_task(event.task_id)
            if (
                child is not None
                and child.status == STATUS_REQUESTED
                and (child.owner_id, child.agent_id) == (scope.owner_id, scope.agent_id)
            ):
                store.cancel_task(child.task_id)
    except STORE_FAILURES as exc:
        raise ExecutionClaimUnavailable(task_id) from exc


def _suppress_cancelled_foreground_actions(pending_events: list[AgentEvent]) -> None:
    """Keep audit reports but discard effects that a stopped run never published."""
    actions = (OutboundText, FileSendRequested, PhotoSendRequested, SubtaskRequested, TaskCancellationRequested)
    pending_events[:] = [event for event in pending_events if not isinstance(event, actions)]


def _owner_message_text(
    record: dict[str, Any] | None,
    text: str,
    *,
    has_answer: bool,
    stopped_externally: bool,
) -> str | None:
    """The owner-facing text for this result, or ``None`` to publish nothing.

    A terminal record means the supervisor already told the owner the task was stopped, so a
    late answer is labelled and a run that stopped without one says nothing at all. A record
    the run closed itself was announced to nobody, so it publishes as it always did.
    """
    # An unreadable record must not cost the owner their answer.
    if record is None or not stopped_externally or str(record.get("status") or "") not in TERMINAL_STATUSES:
        return text
    return f"{_late_result_marker(record)}\n\n{text}" if has_answer else None


def emit_task_results(
    env: Any,
    llm: Any,
    pending_events: list[AgentEvent],
    task: dict[str, Any],
    text: str,
    usage: dict[str, Any],
    llm_trace: dict[str, Any],
    start_time: float,
    drive_logs: pathlib.Path,
    ctx: ToolContext | None = None,
    *,
    scope: OwnerScope,
    execution_memory_repository: IExecutionMemoryRepository,
    audit_event_repository: IAuditEventRepository,
    has_answer: bool = True,
    stopped_externally: bool = True,
    stop_observed: bool = False,
) -> bool:
    """Store the outcome, enqueue allowed reports, and return whether memory work must stop."""
    trace_id = str(task.get("trace_id") or "")
    task_id = str(task.get("id") or "")
    detached = bool(task.get("detached"))
    stored = None if detached else _load_stored_record(env, task_id)
    completion_won = not detached
    if detached:
        candidates = [event for event in pending_events if not isinstance(event, (OutboundText, TaskFinished))]
        pending_events[:] = [
            event
            for event in candidates
            if isinstance(
                event, (TaskMetrics, LlmUsageReported, LiveLogLine, TaskHeartbeat, OwnerMessageInjected, SkillLifecycle)
            )
        ]
        store = runtime_persistence.current.tasks_repo
        try:
            current = store.get_task(task_id)
        except STORE_FAILURES as exc:
            raise ExecutionClaimUnavailable(task_id) from exc
        metadata = dict(current.metadata) if current is not None else {}
        metadata.update(
            terminal_attachments=ctx.terminal_attachments if ctx is not None else [],
            llm_trace=llm_trace,
            trace_summary=build_trace_summary(llm_trace),
            episode_search_terms=episode_search_terms(task, text, llm_trace),
            total_rounds=int(usage.get("rounds") or 0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
        try:
            winner = store.finish_detached_attempt(
                task_id,
                status="completed",
                result=text,
                metadata=metadata,
                claim_token=str(task.get("execution_claim") or ""),
                pending_child_ids=tuple(event.task_id for event in candidates if isinstance(event, SubtaskRequested)),
            )
            current = (
                winner if winner is not None else store.get_current_job(str(task.get("job_id") or task_id), scope=scope)
            )
            completion_won = winner is not None and winner.status == STATUS_COMPLETED
            if completion_won:
                candidates = [
                    event
                    for event in candidates
                    if not isinstance(event, SubtaskRequested)
                    or (
                        (child := store.get_task(event.task_id)) is not None
                        and child.status == STATUS_REQUESTED
                        and (child.owner_id, child.agent_id) == (scope.owner_id, scope.agent_id)
                    )
                ]
        except STORE_FAILURES as exc:
            raise ExecutionClaimUnavailable(task_id) from exc
        if completion_won:
            pending_events[:] = candidates
    else:
        status = _store_task_result(
            env,
            task,
            text,
            usage,
            llm_trace,
            stored or {},
            scope=scope,
            review_evidence={},
            cancelled=stop_observed,
        )
        if stop_observed or status == STATUS_CANCELLED:
            status = STATUS_CANCELLED
            text = "Task cancelled."
            _settle_cancelled_child_requests(pending_events, scope, task_id)
            _suppress_cancelled_foreground_actions(pending_events)
    owner_text = (
        None
        if detached or status == STATUS_CANCELLED
        else _owner_message_text(stored, text, has_answer=has_answer, stopped_externally=stopped_externally)
    )
    if owner_text is not None:
        pending_events.append(
            OutboundText(
                scope=scope,
                text=owner_text or "\u200b",
                log_text=owner_text or "",
                fmt="markdown",
                source=str(task.get("source") or ""),
                transport=dict(task.get("transport") or {}),
                task_id=str(task.get("id") or ""),
                ts=utc_now_iso(),
                trace_id=trace_id,
            )
        )

    duration_sec = round(time.time() - start_time, 3)
    n_tool_calls = len(llm_trace.get("tool_calls", []))
    n_tool_errors = sum(1 for tc in llm_trace.get("tool_calls", []) if isinstance(tc, dict) and tc.get("is_error"))
    try:
        record_event(
            "events",
            "task_eval",
            {
                "ts": utc_now_iso(),
                "ok": True,
                "task_id": task.get("id"),
                "task_type": task.get("type"),
                "duration_sec": duration_sec,
                "tool_calls": n_tool_calls,
                "tool_errors": n_tool_errors,
                "response_len": len(text),
            },
            audit_event_repository=audit_event_repository,
        )
    except Exception:
        log.warning("Failed to log task eval event", exc_info=True)

    pending_events.append(
        TaskMetrics(
            scope=scope,
            task_id=str(task.get("id") or ""),
            task_type=str(task.get("type") or ""),
            duration_sec=duration_sec,
            tool_calls=n_tool_calls,
            tool_errors=n_tool_errors,
            ts=utc_now_iso(),
        )
    )

    if detached:
        if current is None or current.task_id != task_id or current.status not in TERMINAL_STATUSES:
            return True
        status, text = current.status, current.result
    pending_events.append(
        TaskFinished(
            scope=scope,
            task_id=task_id,
            task_type=str(task.get("type") or ""),
            status=status,
            result=text,
            total_rounds=int(usage.get("rounds") or 0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            ts=utc_now_iso(),
            trace_id=trace_id,
        )
    )
    unsuccessful = not completion_won or status != STATUS_COMPLETED
    if not unsuccessful and ctx is not None and not detached:
        _record_working_task_episode(env, task, text, llm_trace, ctx.scope, execution_memory_repository)
    return unsuccessful


def _store_task_result(
    env: Any,
    task: dict[str, Any],
    text: str,
    usage: dict[str, Any],
    llm_trace: dict[str, Any],
    existing: dict[str, Any],
    *,
    scope: OwnerScope,
    review_evidence: dict[str, Any] | None = None,
    cancelled: bool = False,
) -> str:
    """Close the record this run produced, and report the terminal status it wrote.

    The one author of a task's terminal status: whoever observed the outcome records it,
    synchronously, before anything about it is reported outward. The token counts and the
    round count are written here for the same reason — this is the write that holds them.
    """
    try:
        trace_summary = build_trace_summary(llm_trace)
        task_id = str(task.get("id") or "")
        # The caller's snapshot predates publication; a sweep may have closed the record since,
        # and writing over that would drop the reason it was closed.
        current = _load_stored_record(env, task_id)
        existing = current if current else existing
        parent_task_id = str(task.get("parent_task_id") or existing.get("parent_task_id") or "").strip() or None
        root_task_id = str(task.get("root_task_id") or existing.get("root_task_id") or "").strip()
        root_task_id = root_task_id or parent_task_id or task_id
        chat_id = task.get("chat_id")
        if chat_id in (None, ""):
            chat_id = existing.get("chat_id")
        # For a record that exists, write_task_result reaches update_task_status, whose
        # absorbing guard refuses a transition away from a terminal status. A late body
        # therefore records its trace against the status that closed the task, and the
        # status it names below is the one it would have written had it been first.
        existing_status = str(existing.get("status") or "")
        terminal = existing_status in TERMINAL_STATUSES
        status = existing_status if terminal else (STATUS_CANCELLED if cancelled else STATUS_COMPLETED)
        written = write_task_result(
            env.drive_root,
            task_id,
            status,
            scope=scope,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id or None,
            chat_id=chat_id,
            description=task.get("description"),
            context=task.get("context"),
            result="" if terminal else ("Task cancelled." if cancelled else (text or "")),
            trace_summary=trace_summary,
            trace_id=str(task.get("trace_id") or existing.get("trace_id") or ""),
            total_rounds=int(usage.get("rounds") or 0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            review_evidence=review_evidence or {},
            # A late body records its trace, never a new completion time for a closed task.
            ts=str(existing.get("ts") or "") if terminal else utc_now_iso(),
        )
        return str(written.get("status") or status)
    except Exception:
        log.warning("Failed to store task result", exc_info=True)
        return _close_without_trace(
            task, "Task cancelled." if cancelled else text, status=STATUS_CANCELLED if cancelled else STATUS_COMPLETED
        )


def _close_without_trace(task: dict[str, Any], text: str, *, status: str = STATUS_COMPLETED) -> str:
    """The smallest write that still closes a record whose full result write failed.

    Nothing else authors a terminal status — the consumer's `task_done` handler performs no
    transition. A swallowed failure therefore leaves the record running with its answer
    already sent, so the lease sweep fails it two minutes later, requeues it, and answers
    the owner a second time for the same request. The trace summary and the counters are
    most of what the failed write carried; the status alone is worth one more attempt
    without them.
    """
    task_id = str(task.get("id") or "")
    if not task_id:
        return ""
    try:
        record = runtime_persistence.current.tasks_repo.update_task_status(
            task_id,
            status,  # type: ignore[arg-type]
            result=text or "",
        )
    except Exception:
        log.error("Task %s could not be closed; its lease will be swept instead", task_id, exc_info=True)
        return ""
    return str(record.status) if record is not None else ""


def build_review_context(env: Any) -> str:
    """Review continuity context (removed with LLM review stack)."""
    del env
    return ""
