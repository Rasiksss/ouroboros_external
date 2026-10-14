"""Extract durable facts from a completed task and save them via ``memory_save``.

The KAIROS-pattern tool loop. It runs against a request the business layer already
bounded; its only task-store operation is the deterministic pre-distillation claim.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from background_memory.env import MemoryEnv
from background_memory.infra.repositories import SqlAuditRepo, SqlMemoryHeaderRepo, SqlTaskRepo
from background_memory.interfaces import FollowUp, MemoryDistillationEvent
from config_registry.user_settings.reader import UserSettings
from new_core.coordination.registry import TOOL_BODIES, concurrency_budgets
from new_core.identity import OwnerScope
from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from ouroboros.llm import InvalidResponse
from ouroboros.llm.client import LLMClient, async_chat_with_fallback
from ouroboros.llm.providers.base import assistant_history_message
from ouroboros.observability import aef_tracing
from ouroboros.storage.core_memory.memory.domain.models import (
    DISTILLATION_FORBIDDEN_KINDS,
    allowed_kinds,
)
from ouroboros.storage.core_memory.memory.domain.render import render_manifest
from ouroboros.storage.runtime_models import AuditEvent
from ouroboros.storage.settings.store import load_settings as load_app_settings
from ouroboros.tools.memory_tools import get_tools as _mem_tools
from ouroboros.utils import build_trace_summary, truncate_review_artifact as _truncate_with_notice, utc_now_iso

log = logging.getLogger(__name__)


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
    forbidden = DISTILLATION_FORBIDDEN_KINDS
    return _DISTILL_SYSTEM_TEMPLATE.format(
        permitted="\n".join(f"- {kind}: {_DISTILL_KIND_GUIDANCE[kind]}" for kind in allowed_kinds(ctx)),
        forbidden="\n".join(f"- {kind}: {_DISTILL_FORBIDDEN_REASON[kind]}" for kind in sorted(forbidden)),
    )


_DISTILL_TIMEOUT_SEC = 45  # budget for starting new LLM rounds
_DISTILL_MAX_ROUNDS = 4

#: The least a tool body is given before the run stops waiting for it, for a body invoked
#: once the run's own deadline has already passed. Above it the body gets whatever is left
#: of that deadline, so the bound is the run's rather than a second one beside it.
_TOOL_BODY_FLOOR_SEC = 5.0

_TOOL_BODY_LEASE_TTL_SEC = 3600.0

#: Response text shorter than this has nothing durable to extract.
_MIN_RESPONSE_CHARS = 80

#: Task types whose completions are machinery, not work worth remembering.
_EXCLUDED_TASK_TYPES = frozenset({"cron", "system", "health_check", "heartbeat"})

_TASK_TEXT_CHARS = 800
_RESPONSE_TEXT_CHARS = 1200
_TRACE_SUMMARY_CHARS = 1200

#: The dedupe list distillation is told to check. Capped by entries, with the
#: character cap only as a backstop for pathological descriptions — the reverse
#: of the old budget, where formatting consumed the cap before any entry did.
_DISTILL_MANIFEST_ENTRIES = 60
_DISTILL_MANIFEST_CHARS = 4_000


def _tool_budget_sec(deadline: float) -> float:
    return max(_TOOL_BODY_FLOOR_SEC, deadline - time.monotonic())


async def _tool_body_off_the_loop(
    entry: ToolEntry, ctx: ToolContext, args: dict[str, object], *, deadline: float
) -> str:
    """One tool body on an executor this call owns, abandoned rather than awaited past its budget.

    Why an owned executor and not ``asyncio.to_thread``: ``new_core/tools/runtimes.py:24-46``.
    """
    name = entry.name
    budgets = concurrency_budgets.budgets
    wait_sec = _tool_budget_sec(deadline)
    lease = await budgets.acquire_async(
        TOOL_BODIES,
        holder=f"memory-tool-{name}-{uuid.uuid4().hex}",
        ttl_sec=_TOOL_BODY_LEASE_TTL_SEC,
        timeout_sec=wait_sec,
    )
    if lease is None:
        log.warning("Memory tool body found no free slot within %.1fs tool=%s", wait_sec, name)
        return f"⚠️ TOOL_ERROR: '{name}' found no free tool slot within its budget"
    budget_sec = _tool_budget_sec(deadline)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"memory-tool-{name}")
    call = contextvars.copy_context()
    try:
        future: Future[object] = executor.submit(call.run, functools.partial(entry.handler, ctx, **args))
    except BaseException:
        budgets.release(lease)
        raise
    future.add_done_callback(lambda _settled: budgets.release(lease))
    state = threading.Lock()
    abandoned = False
    reported = False

    def _report_late_outcome(settled: Future[object]) -> None:
        nonlocal reported
        with state:
            if not abandoned or reported or settled.cancelled():
                return
            reported = True
        failure = settled.exception()
        if failure is not None:
            log.warning("Abandoned memory tool body raised after its budget tool=%s: %r", name, failure)
        else:
            log.warning("Abandoned memory tool body finished after its budget, result discarded tool=%s", name)

    def _abandon() -> None:
        nonlocal abandoned
        with state:
            abandoned = True
        if future.done():
            _report_late_outcome(future)

    future.add_done_callback(_report_late_outcome)
    try:
        return str(await asyncio.wait_for(asyncio.wrap_future(future), timeout=budget_sec))
    except (asyncio.TimeoutError, TimeoutError) as expiry:
        if not (future.done() and not future.cancelled() and future.exception() is expiry):
            _abandon()
        log.warning("Memory tool body abandoned after %.1fs tool=%s", budget_sec, name)
        return f"⚠️ TOOL_ERROR: '{name}' did not answer within its budget"
    except BaseException as unwind:
        if not (future.done() and not future.cancelled() and future.exception() is unwind):
            _abandon()
        raise
    finally:
        executor.shutdown(wait=False)


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


@dataclass(frozen=True)
class MemoryDistillationRequest:
    """One event's worth of work, bounded and self-contained.

    The run never rereads the source task. ``scope`` is the source task's own — the
    run executes under the identity that produced it rather than minting one.
    """

    scope: OwnerScope
    task_text: str
    response_text: str
    trace_summary: str
    source_task_id: str
    recorded_at: float
    trace_id: str = ""


@dataclass
class DistillResult:
    """Outcome of one distillation run, as reported to the observability trail."""

    status: str  # "completed" | "failed"
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    rounds: int = 0
    saved_count: int = 0
    saved_names: list[str] = field(default_factory=list)


class DistillationHandler:
    """Runs one distillation request against the memory tools.

    A run emits no usage event: there is no task row to attribute spend to, and
    what it cost is on the observability event it records.
    """

    def __init__(
        self,
        env: MemoryEnv,
        llm: LLMClient,
        *,
        tasks: SqlTaskRepo,
        audit: SqlAuditRepo,
        memory_headers: SqlMemoryHeaderRepo,
        user_settings: UserSettings,
    ) -> None:
        self._env = env
        self._llm = llm
        self._tasks = tasks
        self._audit = audit
        self._memory_headers = memory_headers
        self._user_settings = user_settings

    async def handle(self, event: MemoryDistillationEvent) -> FollowUp | None:
        """Run one event and report its result. Distillation never asks for a follow-up.

        One event occupies the loop exactly once: the run is bounded by its own
        deadline and has nothing to continue into.
        """
        job_id = str(event.task.job_id or "")
        if job_id:
            # Older producers populated job_id for ordinary roots/children too; their queued events still replay.
            detached = await self._tasks.is_detached(event.task.task_id)
            if detached is False:
                job_id = ""
        if job_id:
            record = await self._tasks.current_job(job_id, scope=event.scope)
            if (
                record is None
                or record.status != "completed"
                or record.task_id != event.task.task_id
                or record.scope != event.scope
            ):
                return None
            claimed = await self._audit.claim(
                AuditEvent(
                    event_id=f"memory-distillation:{event.scope.owner_id}:{job_id}",
                    owner_id=event.scope.owner_id,
                    agent_id=event.scope.agent_id,
                    event_type="memory_distillation_claim",
                    log_stream="memory",
                    resource_type="task",
                    resource_id=job_id,
                    metadata={"status": "claimed", "task_id": job_id, "trace_id": event.trace_id},
                )
            )
            if not claimed:
                return None
        result = await self.distil(event)
        if result is None:
            return None  # the run logged why it skipped
        if result.status == "completed":
            log.info(
                "Memory distillation completed source_task_id=%s saved=%d rounds=%d",
                event.task.task_id,
                result.saved_count,
                result.rounds,
            )
        else:
            log.warning(
                "Memory distillation failed source_task_id=%s error=%s",
                event.task.task_id,
                result.error,
            )
        return None

    async def distil(self, event: MemoryDistillationEvent) -> DistillResult | None:
        """Distil one completed task, or ``None`` when the event is not worth distilling.

        The outcome is recorded before returning: without a task row, a failed run
        would otherwise be nothing but a log line.
        """
        distillation_enabled = await self._user_settings.async_OUROBOROS_MEMORY_DISTILL(event.scope)
        request = self._request_from_event(event, distillation_enabled=distillation_enabled)
        if request is None:
            return None
        with aef_tracing.trace_scope(aef_tracing.ensure_trace_id(request.trace_id)):
            result = await self._run(request)
            await self._record_outcome(request, result)
        return result

    def _request_from_event(
        self, event: MemoryDistillationEvent, *, distillation_enabled: bool
    ) -> MemoryDistillationRequest | None:
        """Bound an eligible event into a request; log and drop the rest."""
        skip_reason = self._skip_reason(event, distillation_enabled=distillation_enabled)
        if skip_reason:
            # The reason goes into the message itself: the JSON/text log formatters
            # emit a fixed field set and drop unknown `extra` attributes.
            log.warning("Memory distillation skipped: %s source_task_id=%s", skip_reason, event.task.task_id)
            return None
        return MemoryDistillationRequest(
            scope=event.scope,
            task_text=_truncate_with_notice(str(event.task.text or ""), _TASK_TEXT_CHARS),
            response_text=_truncate_with_notice(str(event.response_text or ""), _RESPONSE_TEXT_CHARS),
            trace_summary=_truncate_with_notice(build_trace_summary(event.llm_trace or {}), _TRACE_SUMMARY_CHARS),
            source_task_id=str(event.task.job_id or event.task.task_id or ""),
            recorded_at=event.task.recorded_at,
            trace_id=event.trace_id,
        )

    @staticmethod
    def _skip_reason(event: MemoryDistillationEvent, *, distillation_enabled: bool) -> str:
        """Return why this event yields no distillation ("" = it does)."""
        if not distillation_enabled:
            return "memory distillation is disabled"
        if not str(event.task.text or "").strip():
            return "empty task text"
        if len(str(event.response_text or "").strip()) < _MIN_RESPONSE_CHARS:
            return f"response shorter than {_MIN_RESPONSE_CHARS} characters"
        if str(event.task.task_type or "").lower() in _EXCLUDED_TASK_TYPES:
            return f"excluded task type: {event.task.task_type}"
        block_reason = DistillationHandler._trace_blocks_distillation(event.llm_trace)
        if block_reason:
            return f"unsafe trace ({block_reason})"
        return ""

    @staticmethod
    def _trace_blocks_distillation(llm_trace: dict[str, Any] | None) -> str:
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

    async def _record_outcome(self, request: MemoryDistillationRequest, result: DistillResult) -> None:
        await self._audit.record(
            AuditEvent.runtime_event(
                "memory",
                "memory_distillation",
                {
                    "ts": utc_now_iso(),
                    "status": result.status,
                    "error": result.error,
                    "saved_names": list(result.saved_names),
                    "saved_count": result.saved_count,
                    "rounds": result.rounds,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "task_id": request.source_task_id,
                    # All four axes: unlike consolidation, a distillation run
                    # really is one task's run, in one chat, on one surface.
                    "owner_id": request.scope.owner_id,
                    "agent_id": request.scope.agent_id,
                    "surface_id": str(request.scope.surface_id),
                    "chat_id": request.scope.chat_id,
                },
                owner_id=request.scope.owner_id,
                agent_id=request.scope.agent_id,
                resource_id=request.source_task_id,
            )
        )

    async def _run(self, request: MemoryDistillationRequest) -> DistillResult:
        """The tool loop: at most 4 rounds over memory_save/memory_list.

        Starts no new LLM round after ``_DISTILL_TIMEOUT_SEC``; an in-flight HTTP
        phase receives the remaining time as a cap but is not wall-clock cancelled.
        """
        deadline = time.monotonic() + _DISTILL_TIMEOUT_SEC
        prompt_tokens = 0
        completion_tokens = 0
        rounds = 0
        saved_names: list[str] = []
        try:
            primary_model = await self._user_settings.async_OUROBOROS_MODEL(request.scope)
            settings = load_app_settings()
            fallback_model = await self._user_settings.async_OUROBOROS_MODEL_FALLBACK(request.scope)
            read_timeout = max(1.0, float(settings.OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC))
            max_tokens = int(settings.OUROBOROS_MEMORY_DISTILL_MAX_TOKENS)
            if not primary_model:
                return DistillResult(status="completed", error="NOTHING_TO_SAVE")

            if not request.task_text.strip():
                return DistillResult(status="completed", error="NOTHING_TO_SAVE")

            # The manifest, not the prompt's index sections: those carry ~750
            # characters of heading and banner, which at a 1 000-character cap left
            # room for about seven entries and never reached the knowledge section
            # at all — while the prompt above tells the model to check this list for
            # duplicates.
            index_content = render_manifest(
                await self._memory_headers.list_prompt_headers(request.scope),
                limit=_DISTILL_MANIFEST_ENTRIES,
            )

            user_prompt = (
                f"Task: {request.task_text}\n\n"
                f"Response: {request.response_text}\n\n"
                f"Execution trace summary:\n{request.trace_summary or '(none)'}\n\n"
                f"Existing memories:\n{_truncate_with_notice(index_content, _DISTILL_MANIFEST_CHARS) or '(empty)'}"
            )

            fake_ctx = ToolContext(
                repo_dir=self._env.repo_dir,
                drive_root=self._env.drive_root,
                # The scope the event carried: the dedupe list above and the saves
                # below address the same namespace the source task ran in.
                scope=request.scope,
                skill_surface=None,
                pending_events=[],
                current_task_type="distillation",
                emit_progress_fn=lambda _: None,
                task_depth=0,
                # From the event request, not a fresh source-task lookup: detached
                # reconciliation may run later, but its persisted terminal time must
                # still determine the memory's chronology.
                memory_recorded_at=request.recorded_at or None,
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

            def accumulate_usage(used_model: str, usage: dict[str, Any]) -> None:
                """Totals for the outcome event — a rejected round's tokens count too."""
                del used_model
                nonlocal prompt_tokens, completion_tokens
                if not usage:
                    return
                prompt_tokens += int(usage.get("prompt_tokens") or 0)
                completion_tokens += int(usage.get("completion_tokens") or 0)

            # Sticky routing: once a round succeeds on the fallback, later rounds
            # start there instead of re-paying a failing primary every round.
            active_model = primary_model
            for _ in range(_DISTILL_MAX_ROUNDS):
                remaining_sec = deadline - time.monotonic()
                if remaining_sec <= 0:
                    if rounds == 0:
                        # Nothing was analyzed (e.g. slow preparation) — report a failure.
                        return DistillResult(status="failed", error="deadline expired before the first round")
                    log.debug("Memory distillation: timeout reached, stopping")
                    break

                rounds += 1
                msg, _, active_model = await async_chat_with_fallback(
                    self._llm,
                    messages=messages,
                    model=active_model,
                    fallback_model=fallback_model,
                    deadline=deadline,
                    tools=tool_schemas,
                    max_tokens=max_tokens,
                    reasoning_effort="low",
                    tool_choice="auto",
                    no_proxy=False,
                    timeout_sec=30.0,
                    read_timeout_sec=read_timeout,
                    response_error=_distillation_response_error,
                    on_response=accumulate_usage,
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
                        result = (
                            await _tool_body_off_the_loop(entry, fake_ctx, args, deadline=deadline)
                            if entry
                            else f"unknown tool: {name}"
                        )
                    history_call = {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name or "unknown_tool",
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
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


__all__ = ["DistillResult", "DistillationHandler", "MemoryDistillationRequest"]
