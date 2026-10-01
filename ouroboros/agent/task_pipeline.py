"""Post-task result emission, memory work, reflections, and review context."""

from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ouroboros.agent.working_retrieval import store_task_episode
from ouroboros.llm import InvalidResponse
from ouroboros.llm.providers.base import assistant_history_message
from ouroboros.observability.event_record import record_event
from ouroboros.storage.runtime_store import get_runtime_store
from ouroboros.storage.settings.store import load_settings
from ouroboros.storage.settings.user_settings import SETTINGS_OWNER_ID, user_settings
from ouroboros.storage.task_results import (
    STATUS_COMPLETED,
    TERMINAL_STATUSES,
    load_task_result,
    write_task_result,
)
from ouroboros.utils import truncate_review_artifact as _truncate_with_notice, utc_now_iso

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)


def build_trace_summary(llm_trace: dict) -> str:
    """Return a compact human-readable summary of tool calls and agent notes."""
    tool_calls = llm_trace.get("tool_calls", []) or []
    notes = llm_trace.get("reasoning_notes", []) or []

    n = len(tool_calls)
    errors = sum(1 for tc in tool_calls if isinstance(tc, dict) and tc.get("is_error"))

    lines: list[str] = [f"## Tool trace ({n} calls, {errors} errors)"]

    if not tool_calls:
        lines.append("No tool calls.")
    else:

        def _fmt_call(idx: int, tc: dict) -> str:
            name = tc.get("tool", "unknown")
            args = tc.get("args", {})
            if isinstance(args, dict):
                parts = []
                for k, v in list(args.items())[:2]:
                    v_str = str(v)
                    if len(v_str) > 60:
                        v_str = v_str[:57] + "..."
                    parts.append(f"{k}={v_str!r}")
                if len(args) > 2:
                    parts.append(f"... (+{len(args) - 2} more args)")
                args_str = ", ".join(parts)
            else:
                args_str = repr(args)
                if len(args_str) > 80:
                    args_str = args_str[:77] + "..."
            facts = []
            status = str(tc.get("status") or "").strip()
            if status and status != "ok":
                facts.append(f"status={status}")
            if tc.get("exit_code") not in (None, 0):
                facts.append(f"exit_code={tc.get('exit_code')}")
            if tc.get("signal"):
                facts.append(f"signal={tc.get('signal')}")
            fact_suffix = f" [{', '.join(facts)}]" if facts else ""
            suffix = " → ERROR" if tc.get("is_error") else ""
            return f"{idx}. {name}({args_str}){fact_suffix}{suffix}"

        if n > 30:
            shown = (
                [_fmt_call(i + 1, tool_calls[i]) for i in range(15)]
                + [f"... ({n - 30} more calls) ..."]
                + [_fmt_call(n - 14 + i, tool_calls[n - 15 + i]) for i in range(15)]
            )
        else:
            shown = [_fmt_call(i + 1, tool_calls[i]) for i in range(n)]
        lines.extend(shown)

    if notes:
        lines.append("\n## Agent notes (supplementary, not source of truth)")
        lines.extend(f"- {note}" for note in notes)

    summary = "\n".join(lines)
    if len(summary) > 4000:
        summary = summary[:3997] + "..."
    return summary


def _task_ended_unsuccessfully(env: Any, task_id: str) -> bool:
    """Whether the record closed on any terminal status other than ``completed``.

    A cooperative stop lands as ``cancelled`` or ``budget_exceeded``, and its run has no
    answer worth an episode or a distillation pass.
    """
    if not task_id:
        return False
    try:
        payload = load_task_result(env.drive_root, task_id) or {}
        status = str(payload.get("status") or "")
        return status in TERMINAL_STATUSES and status != STATUS_COMPLETED
    except Exception:
        log.debug("Failed to inspect task result status for memory distillation", exc_info=True)
    return False


def _record_working_task_episode(
    env: Any,
    task: dict[str, Any],
    text: str,
    llm_trace: dict[str, Any],
) -> None:
    if str(task.get("type") or "") == "memory_distillation":
        return
    try:
        settings = load_settings()
        store_task_episode(
            get_runtime_store(env.drive_root),
            task,
            text,
            llm_trace,
            retention_days=int(settings.OUROBOROS_EPISODE_RETENTION_DAYS or 90),
        )
    except Exception:
        log.debug("Failed to record working-memory task episode", exc_info=True)


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
    if not has_answer:
        return None
    return f"{_late_result_marker(record)}\n\n{text}"


def emit_task_results(
    env: Any,
    llm: Any,
    pending_events: list[dict[str, Any]],
    task: dict[str, Any],
    text: str,
    usage: dict[str, Any],
    llm_trace: dict[str, Any],
    start_time: float,
    drive_logs: pathlib.Path,
    ctx: ToolContext | None = None,
    *,
    has_answer: bool = True,
    stopped_externally: bool = True,
) -> None:
    """Emit end-of-task events and enqueue durable memory distillation.

    ``has_answer`` is False when the run reached its terminal with no final content, which the
    empty-response fallback would otherwise hide. ``stopped_externally`` is False when the caller
    wrote the terminal record itself. Only owner-facing publication looks at either.
    """
    trace_id = str(task.get("trace_id") or "")
    stored = _load_stored_record(env, str(task.get("id") or ""))
    owner_text = _owner_message_text(stored, text, has_answer=has_answer, stopped_externally=stopped_externally)
    if owner_text is not None:
        pending_events.append(
            {
                "type": "send_message",
                "chat_id": task["chat_id"],
                "text": owner_text or "\u200b",
                "log_text": owner_text or "",
                "format": "markdown",
                "source": task.get("source") or "",
                "transport": dict(task.get("transport") or {}),
                "task_id": task.get("id"),
                "ts": utc_now_iso(),
                "trace_id": trace_id,
            }
        )

    duration_sec = round(time.time() - start_time, 3)
    n_tool_calls = len(llm_trace.get("tool_calls", []))
    n_tool_errors = sum(1 for tc in llm_trace.get("tool_calls", []) if isinstance(tc, dict) and tc.get("is_error"))
    try:
        record_event(
            env.drive_root,
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
        )
    except Exception:
        log.warning("Failed to log task eval event", exc_info=True)

    pending_events.append(
        {
            "type": "task_metrics",
            "task_id": task.get("id"),
            "task_type": task.get("type"),
            "duration_sec": duration_sec,
            "tool_calls": n_tool_calls,
            "tool_errors": n_tool_errors,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_rounds": int(usage.get("rounds") or 0),
            "ts": utc_now_iso(),
            "trace_id": trace_id,
        }
    )

    pending_events.append(
        {
            "type": "task_done",
            "task_id": task.get("id"),
            "task_type": task.get("type"),
            "result": text or "",
            "total_rounds": int(usage.get("rounds") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "ts": utc_now_iso(),
            "trace_id": trace_id,
        }
    )
    # NOTE: task_done is NOT persisted here.
    # It goes through EVENT_Q → supervisor _handle_task_done → runtime event store.
    # This ensures causal ordering: send_message reaches the UI before task_done,
    # preventing the live card from collapsing before the assistant reply arrives.

    review_evidence: dict[str, Any] = {}
    _store_task_result(env, task, text, usage, llm_trace, stored or {}, review_evidence=review_evidence)
    unsuccessful = _task_ended_unsuccessfully(env, str(task.get("id") or ""))
    if not unsuccessful:
        _record_working_task_episode(env, task, text, llm_trace)
    restart_reason = str((ctx.pending_restart_reason if ctx is not None else "") or "").strip()
    if ctx is not None and restart_reason:
        pending_events.append(
            {
                "type": "restart_request",
                "reason": restart_reason,
                "ts": utc_now_iso(),
            }
        )
        try:
            ctx.pending_restart_reason = None
        except Exception:
            pass

    if not unsuccessful:
        _enqueue_memory_distillation(env, task, text, llm_trace=llm_trace)


def _store_task_result(
    env: Any,
    task: dict[str, Any],
    text: str,
    usage: dict[str, Any],
    llm_trace: dict[str, Any],
    existing: dict[str, Any],
    review_evidence: dict[str, Any] | None = None,
) -> None:
    """Store task result for parent task retrieval."""
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
        # write_task_result reaches upsert_task, which does not apply the absorbing-status
        # guard, so a late body keeps the status and result that closed the task itself.
        existing_status = str(existing.get("status") or "")
        terminal = existing_status in TERMINAL_STATUSES
        status = existing_status if terminal else STATUS_COMPLETED
        write_task_result(
            env.drive_root,
            task_id,
            status,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id or None,
            chat_id=chat_id,
            description=task.get("description"),
            context=task.get("context"),
            result="" if terminal else (text or ""),
            trace_summary=trace_summary,
            trace_id=str(task.get("trace_id") or existing.get("trace_id") or ""),
            total_rounds=int(usage.get("rounds") or 0),
            review_evidence=review_evidence or {},
            # A late body records its trace, never a new completion time for a closed task.
            ts=str(existing.get("ts") or "") if terminal else utc_now_iso(),
        )
    except Exception:
        log.warning("Failed to store task result", exc_info=True)


def build_review_context(env: Any) -> str:
    """Review continuity context (removed with LLM review stack)."""
    del env
    return ""


# ---------------------------------------------------------------------------
# Post-task memory distillation (KAIROS pattern)
# ---------------------------------------------------------------------------

#: What each writable kind means, in the words the distillation prompt uses.
#: Keyed by kind so the prompt's list is rendered from the policy instead of
#: written out beside it. ``identity`` has no entry on purpose: distillation may
#: never write it, and its absence is pinned by a test.
_DISTILL_KIND_GUIDANCE = {
    "project": "confirmed long-term project facts, agreements, goals, deadlines, or incidents",
    "reference": "pointers to external systems, dashboards, or durable source references",
    "knowledge": "operational lessons this run established — recipes, gotchas, and error patterns",
    "user": "who the user is and how they work",
    "feedback": "behavioral corrections and confirmations",
}

#: Why each withheld kind is withheld, in the words the prompt uses. Keyed by
#: kind so a sentence written for one kind never leans on another's rationale.
_DISTILL_FORBIDDEN_REASON = {
    "user": (
        "who the user is — only the user's own words establish it, so it is left to regular "
        "memory_save when the conversation clearly supports the fact"
    ),
    "feedback": (
        "how the user wants to be worked with — a correction is only a correction when the user "
        "makes it, so it too is left to regular memory_save"
    ),
    "identity": (
        "the agent's own identity document — rewriting the self is a deliberate act inside a task, "
        "never something a background job derives from a transcript"
    ),
}

_DISTILL_SYSTEM_TEMPLATE = """\
You are extracting facts worth saving as long-term memories after a completed task.

Review the task query and the response, then decide what (if anything) should be saved
to long-term memory using memory_save.

Only save facts supported by both the response and the execution trace summary.

Save only non-obvious, durable facts, using ONLY these memory_type values:
{permitted}

Automated post-task extraction cannot write these memory_type values, each for its own reason:
{forbidden}

Do NOT save transient execution details, temporary status, or facts that will stop
being useful after the current task ends.

Do NOT save facts whose only source is an agent-generated data artifact or a
DATA_PROVENANCE_WARNING block; those are working artifacts, not authoritative
long-term memory.

For time-sensitive facts (rates, deadlines, statuses), include
'- observed_at: <ISO-8601> — <fact>' lines in memory content.

If nothing is worth saving, respond with exactly: NOTHING_TO_SAVE
Otherwise call memory_save for each memorable fact (check memory_list first to avoid duplicates).
"""


def _distill_system_prompt(ctx: ToolContext) -> str:
    """The distillation system prompt, with its kind list rendered from the policy.

    Both the permitted and the withheld lists are rendered from the same
    functions the tool schema reads, so a kind added to (or withheld from)
    distillation reaches the prompt and the schema together. The withheld half
    is further narrowed to kinds anyone can write in this deployment: a kind no
    one is offered is not "withheld from distillation", it is simply absent.
    """
    from ouroboros.storage.core_memory.memory.domain.models import (
        DISTILLATION_FORBIDDEN_KINDS,
        allowed_kinds,
        writable_kinds,
    )

    forbidden = DISTILLATION_FORBIDDEN_KINDS.intersection(writable_kinds())
    return _DISTILL_SYSTEM_TEMPLATE.format(
        permitted="\n".join(f"- {kind}: {_DISTILL_KIND_GUIDANCE[kind]}" for kind in allowed_kinds(ctx)),
        forbidden="\n".join(f"- {kind}: {_DISTILL_FORBIDDEN_REASON[kind]}" for kind in sorted(forbidden)),
    )


_DISTILL_TIMEOUT_SEC = 45  # budget for starting new LLM rounds
_DISTILL_MAX_ROUNDS = 4

#: The dedupe list distillation is told to check. Capped by entries, with the
#: character cap only as a backstop for pathological descriptions — the reverse
#: of the old budget, where formatting consumed the cap before any entry did.
_DISTILL_MANIFEST_ENTRIES = 60
_DISTILL_MANIFEST_CHARS = 4_000


def _distillation_recorded_at(env: Any, payload: dict[str, Any]) -> float | None:
    """Resolve the source task timestamp for distillation memory_save stamping."""
    source_task_id = str(payload.get("source_task_id") or "").strip()
    if not source_task_id:
        return None
    try:
        from ouroboros.storage.core_memory.memory.domain.render import parse_memory_timestamp
        from ouroboros.storage.runtime_store import get_runtime_store

        store = get_runtime_store(env.drive_root)
        if store is None:
            return None
        record = store.get_task(source_task_id)
        if record is None:
            return None
        for field in (record.finished_at, record.created_at):
            ts = parse_memory_timestamp(str(field or ""))
            if ts is not None:
                return ts
    except Exception:
        log.debug("Failed to resolve distillation source task timestamp", exc_info=True)
    return None


def _record_distillation_usage(
    event_queue: Any,
    task_id: str,
    model: str,
    usage: dict[str, Any],
) -> None:
    event_model = str(usage.get("resolved_model") or model)
    provider = str(usage.get("provider") or "").strip() or None
    if event_queue is not None:
        try:
            from ouroboros.observability.pricing import emit_llm_usage_event

            emit_llm_usage_event(
                event_queue,
                task_id,
                event_model,
                usage,
                category="memory_distillation",
                provider=provider,
                source="memory_distill",
            )
            return
        except Exception:
            log.debug("Failed to emit memory distillation usage event", exc_info=True)
    try:
        from supervisor.state import update_usage_from_llm

        update_usage_from_llm(usage)
    except Exception:
        log.debug("Failed to record memory distillation usage", exc_info=True)


def _is_wellformed_tool_call(call: Any) -> bool:
    """The call names a function and carries string arguments — executable as-is."""
    if not isinstance(call, dict):
        return False
    function = call.get("function")
    if not isinstance(function, dict):
        return False
    return bool(str(function.get("name") or "").strip()) and isinstance(function.get("arguments"), str)


def _distillation_response_error(response: dict[str, Any]) -> InvalidResponse | None:
    content = response.get("content")
    tool_calls = response.get("tool_calls") or []
    if tool_calls and all(_is_wellformed_tool_call(call) for call in tool_calls):
        return None
    finish_reason = response.get("finish_reason")
    normalized_finish = str(finish_reason or "").strip().lower()
    if isinstance(content, str) and content.strip():
        # Content only ends the tool loop (never persisted), so a missing
        # finish_reason is acceptable here, unlike the consolidation summary.
        if finish_reason is None or normalized_finish in {"stop", "end_turn"}:
            return None
        return InvalidResponse.rejected("invalid memory distillation response")
    if finish_reason is None:
        return InvalidResponse.incomplete("provider_incomplete_response finish_reason=null")
    return InvalidResponse.rejected("invalid memory distillation response")


def _saved_memory_name(tool_result: str) -> str:
    prefix = "✅ Memory '"
    text = str(tool_result or "")
    if not text.startswith(prefix):
        return ""
    return text[len(prefix) :].split("'", 1)[0].strip()


@dataclass
class DistillResult:
    """Outcome of run_memory_distillation_task, for TaskExecutor._run_memory_distillation."""

    status: str  # "completed" | "failed"
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    rounds: int = 0
    saved_count: int = 0
    saved_names: list[str] = field(default_factory=list)


_MAX_PENDING_DISTILLATIONS = 8


def _trace_blocks_memory_distillation(llm_trace: dict[str, Any] | None) -> str:
    """Return why trace evidence is unsafe for automatic long-term facts ("" = safe).

    A failure is recovered only by a later nonempty successful result from the
    same tool with the same args fingerprint; a failure without a fingerprint
    can never be recovered and blocks unconditionally.
    """
    if not llm_trace:
        return ""
    unresolved: dict[tuple[str, str], str] = {}
    unfingerprinted_failure = ""
    provenance_warning = ""
    for call in llm_trace.get("tool_calls", []) or []:
        if not isinstance(call, dict):
            continue
        status = str(call.get("status") or "").strip().lower()
        failed = bool(
            call.get("is_error")
            or (status and status not in {"ok", "ok_autocorrected"})
            or call.get("exit_code") not in (None, 0)
            or call.get("signal")
        )
        tool = str(call.get("tool") or "").strip()
        args_hash = str(call.get("args_hash") or "").strip()
        result = str(call.get("result") or "")
        if "DATA_PROVENANCE_WARNING:" in result:
            provenance_warning = provenance_warning or f"DATA_PROVENANCE_WARNING from tool={tool or 'unknown'}"
        if failed:
            if tool and args_hash:
                unresolved[(tool, args_hash)] = f"unrecovered tool failure: tool={tool} args_hash={args_hash}"
            else:
                unfingerprinted_failure = f"tool failure without args fingerprint: tool={tool or 'unknown'}"
            continue
        if "DATA_PROVENANCE_WARNING:" not in result and result.strip() and tool and args_hash:
            unresolved.pop((tool, args_hash), None)
    return provenance_warning or unfingerprinted_failure or next(iter(unresolved.values()), "")


def _enqueue_memory_distillation(
    env: Any,
    task: dict[str, Any],
    response_text: str,
    *,
    llm_trace: dict[str, Any] | None = None,
) -> None:
    """Enqueue a durable memory_distillation task with payload in runtime DB.

    Each completed task creates its own distillation task — no dedup, no
    local backlog files. Throttled by _MAX_PENDING_DISTILLATIONS cap.
    """
    if not user_settings.OUROBOROS_MEMORY_DISTILL(SETTINGS_OWNER_ID):
        return

    task_text = str(task.get("text") or "").strip()
    task_type = str(task.get("type") or "").lower()
    if not task_text or len(response_text.strip()) < 80:
        return
    if task_type in {"cron", "system", "health_check", "heartbeat"}:
        return
    # Skip reasons go into the message itself: the JSON/text log formatters
    # emit a fixed field set and drop unknown `extra` attributes.
    block_reason = _trace_blocks_memory_distillation(llm_trace)
    if block_reason:
        log.warning("Memory distillation skipped: unsafe trace (%s) source_task_id=%s", block_reason, task.get("id"))
        return

    from supervisor.queue import get_task_queue

    queue = get_task_queue(env.drive_root)
    pending = sum(
        1
        for r in queue.store.list_tasks(statuses=["queued", "scheduled"])
        if str(r.task_type or "") == "memory_distillation"
    )
    if pending >= _MAX_PENDING_DISTILLATIONS:
        log.warning(
            "Memory distillation skipped: pending cap reached (pending=%d cap=%d) source_task_id=%s",
            pending,
            _MAX_PENDING_DISTILLATIONS,
            task.get("id"),
        )
        return
    queue.enqueue_dict(
        {
            "type": "memory_distillation",
            "task_text": _truncate_with_notice(task_text, 800),
            "response_text": _truncate_with_notice(response_text, 1200),
            "trace_summary": _truncate_with_notice(build_trace_summary(llm_trace or {}), 1200),
            "owner_id": "global",
            "chat_id": task.get("chat_id"),
            "source_task_id": task.get("id"),
        }
    )


def run_memory_distillation_task(
    env: Any,
    llm: Any,
    payload: dict[str, Any],
    *,
    event_queue: Any = None,
    task_id: str = "",
) -> DistillResult:
    """Extract durable facts from a completed task and save them via memory_save.

    Runs the KAIROS-pattern tool loop (memory_save/memory_list, max 4 rounds) against
    `payload` (task_text/response_text/owner_id/chat_id from _enqueue_memory_distillation).
    Starts no new LLM rounds after _DISTILL_TIMEOUT_SEC; in-flight HTTP phases receive
    the remaining time as a cap but are not wall-clock cancelled. Emits an llm_usage
    event for each LLM call via event_queue/task_id.
    """
    deadline = time.monotonic() + _DISTILL_TIMEOUT_SEC
    prompt_tokens = 0
    completion_tokens = 0
    rounds = 0
    saved_names: list[str] = []
    try:
        from new_core.tools.context import ToolContext
        from ouroboros.app.config import get_main_model, load_settings as load_app_settings
        from ouroboros.llm import chat_with_fallback
        from ouroboros.storage.core_memory.memory import get_memory
        from ouroboros.storage.core_memory.memory.domain.render import render_manifest

        primary_model = get_main_model()
        settings = load_app_settings()
        fallback_model = user_settings.OUROBOROS_MODEL_FALLBACK(SETTINGS_OWNER_ID).strip()
        read_timeout = max(1.0, float(settings.OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC))
        if not primary_model:
            return DistillResult(status="completed", error="NOTHING_TO_SAVE")

        owner_id = "global"
        task_text = str(payload.get("task_text") or "")
        response_text = str(payload.get("response_text") or "")
        trace_summary = str(payload.get("trace_summary") or "")
        if not task_text.strip():
            return DistillResult(status="completed", error="NOTHING_TO_SAVE")

        # The manifest, not the prompt's index sections: those carry ~750
        # characters of heading and banner, which at a 1 000-character cap left
        # room for about seven entries and never reached the knowledge section
        # at all — while the prompt above tells the model to check this list for
        # duplicates.
        index_content = render_manifest(get_memory().list_prompt_headers(), limit=_DISTILL_MANIFEST_ENTRIES)

        user_prompt = (
            f"Task: {_truncate_with_notice(task_text, 800)}\n\n"
            f"Response: {_truncate_with_notice(response_text, 1200)}\n\n"
            f"Execution trace summary:\n{_truncate_with_notice(trace_summary, 1200) or '(none)'}\n\n"
            f"Existing memories:\n{_truncate_with_notice(index_content, _DISTILL_MANIFEST_CHARS) or '(empty)'}"
        )

        from ouroboros.tools.memory_tools import get_tools as _mem_tools

        try:
            raw_chat_id = payload.get("chat_id")
            safe_chat_id: int | None = int(raw_chat_id) if raw_chat_id is not None else None
        except (TypeError, ValueError):
            safe_chat_id = None

        fake_ctx = ToolContext(
            repo_dir=env.repo_dir,
            drive_root=env.drive_root,
            pending_events=[],
            current_chat_id=safe_chat_id,
            current_task_type="distillation",
            emit_progress_fn=lambda _: None,
            task_depth=0,
            is_direct_chat=False,
            memory_owner_id=owner_id,
            task_constraint=None,
            memory_recorded_at=_distillation_recorded_at(env, payload),
        )
        # The ctx is what narrows the memory_save kind enum: a distillation
        # caller is never offered 'user' or 'feedback'.
        tool_entries = _mem_tools(fake_ctx)
        tool_schemas = [e.schema for e in tool_entries if e.name in {"memory_save", "memory_list"}]
        tool_map = {e.name: e for e in tool_entries if e.name in {"memory_save", "memory_list"}}

        messages: list[dict[str, Any]] = [
            # Built from the same ctx that narrows the tool schema below, so the
            # prompt cannot offer a kind the schema withholds, or withhold one it offers.
            {"role": "system", "content": _distill_system_prompt(fake_ctx)},
            {"role": "user", "content": user_prompt},
        ]

        def record_usage(used_model: str, usage: dict[str, Any]) -> None:
            nonlocal prompt_tokens, completion_tokens
            if not usage:
                return
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)
            _record_distillation_usage(event_queue, task_id, used_model, usage)

        # Sticky routing: once a round succeeds on the fallback, later rounds
        # start there instead of re-paying a failing primary every round.
        active_model = primary_model
        for _ in range(_DISTILL_MAX_ROUNDS):
            remaining_sec = deadline - time.monotonic()
            if remaining_sec <= 0:
                if rounds == 0:
                    # Nothing was analyzed (e.g. slow preparation) — stay retryable.
                    return DistillResult(status="failed", error="deadline expired before the first round")
                log.debug("Memory distillation: timeout reached, stopping")
                break

            rounds += 1
            msg, _, active_model = chat_with_fallback(
                llm,
                messages=messages,
                model=active_model,
                fallback_model=fallback_model,
                deadline=deadline,
                tools=tool_schemas,
                max_tokens=512,
                tool_choice="auto",
                no_proxy=False,
                timeout_sec=30.0,
                read_timeout_sec=read_timeout,
                response_error=_distillation_response_error,
                on_response=record_usage,
            )

            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []

            if isinstance(content, str) and content.strip() == "NOTHING_TO_SAVE":
                break
            if not tool_calls:
                break

            prepared_calls: list[tuple[dict[str, Any], str, str, str]] = []
            for index, raw_call in enumerate(tool_calls):
                call = raw_call if isinstance(raw_call, dict) else {}
                raw_function = call.get("function")
                fn = raw_function if isinstance(raw_function, dict) else {}
                name = str(fn.get("name") or "").strip()
                call_id = str(call.get("id") or f"distill_call_{index}")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not name:
                        raise ValueError("tool function name is required")
                    if not isinstance(args, dict):
                        raise TypeError("tool arguments must decode to an object")
                except (TypeError, ValueError) as exc:
                    args = {}
                    result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{name or 'unknown_tool'}': {exc}"
                else:
                    entry = tool_map.get(name)
                    result = entry.handler(fake_ctx, **args) if entry else f"unknown tool: {name}"
                history_call = {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name or "unknown_tool", "arguments": json.dumps(args, ensure_ascii=False)},
                }
                prepared_calls.append((history_call, name, call_id, str(result)))

            history_message = assistant_history_message(msg)
            history_message["tool_calls"] = [call for call, _, _, _ in prepared_calls]
            messages.append(history_message)
            for _, name, call_id, result in prepared_calls:
                saved_name = _saved_memory_name(str(result)) if name == "memory_save" else ""
                if saved_name and saved_name not in saved_names:
                    saved_names.append(saved_name)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result,
                    }
                )

        return DistillResult(
            status="completed",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            rounds=rounds,
            saved_count=len(saved_names),
            saved_names=saved_names,
        )
    except Exception as exc:
        log.warning("Memory distillation task failed", exc_info=True)
        return DistillResult(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            rounds=rounds,
            saved_count=len(saved_names),
            saved_names=saved_names,
        )
