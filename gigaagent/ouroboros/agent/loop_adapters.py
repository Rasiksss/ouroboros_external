"""Concrete adapters wiring ``new_core.loop`` onto the ouroboros side.

Each adapter subclasses an interface ABC from ``new_core.loop.interfaces`` and **owns its
implementation**: a helper called by exactly one adapter is a method of that adapter, and
helpers shared between owners live in named modules (``new_core.loop.transcript_merge``,
``context_compaction``, ``loop_llm_call``). Nothing is reached through a module object at
call time; the seam tests use is the adapter methods themselves — the same boundary the
graph nodes call.

Per-task tool-execution state that no tool reads — the repeat guard, the tool-outcome
ledger, and the micro-compaction receipt memo — is owned by these adapters rather than
carried on ``ToolContext``: the per-task builder creates each object once and hands the
ledger to both the tool runner (writer) and the compactor (reader). ``ToolContext``
keeps only what tools themselves touch (option A, D3/D4). The repeat guard is the one
piece of that state with a *single* owner — the executor — so it is not handed around:
a second reference could be the one reset on owner input while the executor keeps
recording failures into another, leaving a blocked tool blocked for the rest of the task.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import queue
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final, TypedDict, cast

from background_memory.interfaces import (
    IMemoryEventPublisher,
    MemoryDistillationEvent,
    MemoryTaskRef,
)
from new_core.identity import SURFACES_WITH_A_PERSON, OwnerScope, SurfaceId
from new_core.llm.types import LLMFailure, LLMMessage, ToolCall
from new_core.loop import is_token_overflow
from new_core.loop.interfaces import (
    CancellationSignal,
    ConfirmationBroker,
    ConfirmationKey,
    ConfirmationRequest,
    ConfirmationRound,
    ContextAssembler,
    EventSink,
    LLMMiddleware,
    LLMProvider,
    LoopOverrides,
    OwnerInbox,
    ToolRunner,
    TranscriptCompactor,
)
from new_core.loop.transcript_merge import (
    MERGE_SEPARATOR,
    append_or_merge_user_content,
    append_or_merge_user_message,
)
from new_core.tools.catalog import ToolCatalog
from ouroboros.agent.context import (
    active_skill_instructions,
    build_user_content,
    compact_active_skills,
    restore_skills,
    skills_snapshot,
)
from ouroboros.agent.context_compaction import (
    _COMPACTION_PROTECTED_TOOLS,
    _find_tool_name_for_result,
    compact_tool_history_llm,
)
from ouroboros.agent.loop_llm_call import call_llm_with_retry
from ouroboros.agent.loop_tool_execution import (
    EventRecorder,
    GatedCall,
    InvocationRecorder,
    ReviewGateSink,
    ToolCallExecutor,
    ToolTracer,
)
from ouroboros.agent.response_language_guard import (
    build_language_repair_messages,
    find_language_violation,
)
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger, ToolOutcomes
from ouroboros.agent.tool_receipts import ToolResultReplacementState
from ouroboros.agent.working_retrieval import build_tool_invocation
from ouroboros.agent_events.interfaces import (
    AgentEvent,
    IAgentEventPublisher,
    OutboundText,
    OwnerMessageInjected,
    TypingStarted,
)
from ouroboros.contracts.attachments import TerminalAttachment
from ouroboros.contracts.chat_commands import CONFIRMATION_ID_HINT_LEN, TYPED_ANSWER_SURFACES
from ouroboros.contracts.tool_confirmation import CONFIRMATION_TRANSPORT_KEY
from ouroboros.llm import LLMClient
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_record import LogStream, emit_worker_log_event, record_event
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.observability.pricing import emit_llm_usage_event
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.runtime_backend import STORE_FAILURES, ExecutionClaimUnavailable
from ouroboros.storage.runtime_models import ToolConfirmation
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.store import load_settings
from ouroboros.storage.task_results import STATUS_RUNNING, write_task_result
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from ouroboros.tools.runtime_metadata import ResolvedToolRuntimeMetadata
from ouroboros.tools.surface import ProviderToolSurface
from ouroboros.utils import estimate_tokens, sanitize_task_for_event, truncate_for_log, utc_now_iso

if TYPE_CHECKING:
    from new_core.skills.surface import SkillSurface
    from new_core.tools.context import ToolContext
    from ouroboros.agent.core import OuroborosAgent

log = logging.getLogger(__name__)

_IMAGE_BLOCK_TOKEN_ESTIMATE = 2000
_MICROCOMPACT_CLEARED = "[Tool result cleared to save context space]"
_TOOL_LEDGER_NOTE_START = "[SYSTEM RUNTIME NOTE: TOOL OUTCOME LEDGER]"
_TOOL_LEDGER_NOTE_END = "[/SYSTEM RUNTIME NOTE]"


class LLMProviderAdapter(LLMProvider):
    """LLM calls, circuit reset, and final-response language repair."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        audit_event_repository: IAuditEventRepository,
        drive_logs: pathlib.Path,
        task_id: str,
        task_type: str,
        events: IAgentEventPublisher | None,
        max_retries: int,
        fallback_model: str,
        fallback_use_local: bool,
        scope: OwnerScope,
        middlewares: Sequence[LLMMiddleware],
        cancellation: CancellationSignal,
        tool_context: Callable[[], ToolContext | None] = lambda: None,
    ) -> None:
        self._llm = llm
        self._audit_event_repository = audit_event_repository
        self._drive_logs = drive_logs
        self._task_id = task_id
        self._task_type = task_type
        self._events = events
        self._max_retries = max_retries
        self._fallback_model = fallback_model
        self._fallback_use_local = fallback_use_local
        #: Whose work these calls are, so their spend is recorded under that owner.
        self._scope = scope
        self.middlewares = tuple(middlewares)
        self._cancellation = cancellation
        self._tool_context = tool_context
        self._last_response_route: tuple[str, bool] | None = None

    def call(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[Any] | None,
        effort: str,
        round_idx: int,
        use_local: bool,
        accumulated_usage: dict[str, Any],
    ) -> LLMMessage | LLMFailure:
        response = self._call_model(messages, model, tools, effort, round_idx, use_local, accumulated_usage)
        if isinstance(response, LLMMessage):
            self._last_response_route = model, use_local
            tool_ctx = self._tool_context()
            if tool_ctx is not None and tool_ctx.pending_skill_read in tool_ctx.projected_skills:
                tool_ctx.pending_skill_read = None
        return response

    def _call_model(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[Any] | None,
        effort: str,
        round_idx: int,
        use_local: bool,
        accumulated_usage: dict[str, Any],
    ) -> LLMMessage | LLMFailure:
        """The one model call of this adapter: every middleware sees its request and its reply."""
        for middleware in self.middlewares:
            middleware.before_call(messages)
        result = call_llm_with_retry(
            self._llm,
            messages,
            model,
            tools,
            effort,
            self._max_retries,
            self._drive_logs,
            self._task_id,
            round_idx,
            self._events,
            accumulated_usage,
            self._task_type,
            use_local=use_local,
            scope=self._scope,
            cancellation=self._cancellation,
            audit_event_repository=self._audit_event_repository,
        )
        tool_ctx = self._tool_context()
        if (
            isinstance(result, LLMFailure)
            and is_token_overflow(result)
            and tool_ctx is not None
            and tool_ctx.pending_skill_read
            and tool_ctx.projected_skills == {tool_ctx.pending_skill_read}
        ):
            result = replace(
                result,
                user_message=(
                    "⚠️ SKILL_CONTEXT_OVERFLOW: последняя прочитанная инструкция скилла "
                    "и обязательный контекст не помещаются в запрос модели."
                ),
            )
        for middleware in self.middlewares:
            middleware.after_call(result if isinstance(result, LLMMessage) else None)
        return result

    def reset_circuits(self, model: str, use_local: bool) -> None:
        try:
            from ouroboros.llm.circuit_breaker import reset_provider_circuits_for_models
            from ouroboros.storage.settings.store import load_settings

            candidates = [(model, use_local)]
            if self._fallback_model:
                candidates.append((self._fallback_model, self._fallback_use_local))
            # The vlm_query tool calls the remote vision model through the same
            # client; unstick its breaker on owner input alongside main/fallback.
            vision_model = str(load_settings().OUROBOROS_MODEL_VISION or "").strip()
            if vision_model:
                candidates.append((vision_model, False))
            reset_provider_circuits_for_models(candidates)
        except Exception:
            log.debug("Failed to reset provider circuit breakers after owner input", exc_info=True)

    def repair_language(
        self,
        content: str | None,
        *,
        messages: list[dict[str, Any]],
        model: str,
        effort: str,
        use_local: bool,
        round_idx: int,
        accumulated_usage: dict[str, Any],
    ) -> str:
        original = str(content or "")
        if self._cancellation.is_stopped():
            return ""
        instructions = active_skill_instructions(self._tool_context())
        if find_language_violation(original, messages, additional_evidence=instructions) is None:
            return original
        model, use_local = self._last_response_route or (model, use_local)
        try:
            repair_messages = build_language_repair_messages(original)
            if instructions:
                repair_messages.insert(0, {"role": "system", "content": instructions})
            repaired_message = self._call_model(
                repair_messages, model, None, effort, round_idx, use_local, accumulated_usage
            )
        except Exception:
            log.debug("Final response language repair failed", exc_info=True)
            return original
        if isinstance(repaired_message, LLMFailure) or repaired_message.tool_calls:
            return original
        repaired = str(repaired_message.content or "")
        if (
            not repaired.strip()
            or find_language_violation(repaired, messages, additional_evidence=instructions) is not None
        ):
            return original
        return repaired


class DriveEventRecorder(EventRecorder):
    """Persist loop events through the composed audit repository."""

    def __init__(self, *, audit_event_repository: IAuditEventRepository) -> None:
        self._audit_event_repository = audit_event_repository

    def record(self, stream: LogStream, name: str, payload: dict[str, Any]) -> None:
        record_event(stream, name, payload, audit_event_repository=self._audit_event_repository)


class ExecutionMemoryInvocationRecorder(InvocationRecorder):
    """Record tool invocations through the invocation repository."""

    def __init__(self, repository: IExecutionMemoryRepository) -> None:
        self._repository = repository

    def record(self, ctx: ToolContext | None, execution: dict[str, Any]) -> None:
        if ctx is None or ctx.drive_root is None or not ctx.task_id:
            return
        try:
            settings = load_settings()
            metadata = execution.get("tool_metadata")
            invocation = build_tool_invocation(
                execution,
                task_id=ctx.task_id,
                scope=ctx.scope,
                retention_days=int(settings.OUROBOROS_TOOL_SUMMARY_RETENTION_DAYS or 30),
                raw_retention_days=int(settings.OUROBOROS_TOOL_RAW_RETENTION_DAYS or 0),
                retrieval_aliases=(*metadata.retrieval_aliases, metadata.stable_name)
                if isinstance(metadata, ResolvedToolRuntimeMetadata)
                else (),
            )
            self._repository.record_tool_invocation(invocation)
        except STORE_FAILURES:
            log.warning("Failed to record working-memory tool invocation", exc_info=True)


class AefToolTracer(ToolTracer):
    """AEF tracing spans; payload masking happens here, not in the executor.

    Span inputs default to the hard trace mask. On debug contours
    ``AEF_TOOL_TRACE_PAYLOAD_ENABLED`` lets them through as given: the executor
    already passes ``args_for_log`` — sanitized with the tool's declared
    ``log_redacted_args`` — so nothing is re-resolved here. Results are always
    masked — tools have no redaction contract for their output.
    """

    def _span_input(self, *, tool: str, args: dict[str, Any], task_id: str) -> str | dict[str, Any]:
        payload = {"tool": tool, "arguments": args, "task_id": task_id}
        try:
            if not load_settings().AEF_TOOL_TRACE_PAYLOAD_ENABLED:
                return aef_tracing.mask_trace_payload(payload)
            return aef_tracing.make_aef_json_safe(payload)
        except Exception:
            # Fail closed: any doubt and the span gets the mask.
            log.debug("Failed to build tool span input", exc_info=True)
            return aef_tracing.mask_trace_payload(payload)

    def span(self, *, tool: str, args: dict[str, Any], task_id: str):
        return aef_tracing.tool_run(input=self._span_input(tool=tool, args=args, task_id=task_id))

    def add_result(self, span: Any, *, result: Any, is_error: bool) -> None:
        try:
            span.add_tool_result(output=aef_tracing.mask_trace_payload({"result": result, "is_error": is_error}))
        except Exception:
            log.debug("Failed to add AEF tool result", exc_info=True)


class CommitGateReviewSink(ReviewGateSink):
    """Commit-gate records for reviewed mutative tools.

    ``commit_gate`` is imported lazily on purpose: the module pulls in review machinery
    whose import at load time would close a cycle, and some profiles do not ship it at
    all — both methods swallow the failure, as the inline imports here always did.
    """

    def mark_attempt_late(self, ctx: ToolContext | None, *, soft_timeout_sec: int, duration_sec: float) -> None:
        try:
            from ouroboros.tools.commit_gate import _mark_review_attempt_late

            if ctx is not None:
                _mark_review_attempt_late(ctx, soft_timeout_sec=soft_timeout_sec, duration_sec=duration_sec)
        except Exception:
            log.debug("Failed to mark reviewed attempt as late_result_pending", exc_info=True)

    def record_hard_ceiling(self, ctx: ToolContext | None, *, duration_sec: float, hard_ceiling_sec: int) -> None:
        try:
            from ouroboros.tools.commit_gate import _record_commit_attempt

            if ctx is not None:
                _record_commit_attempt(
                    ctx,
                    commit_message=str(getattr(ctx, "_current_review_commit_message", "") or ""),
                    status="failed",
                    block_reason="infra_failure",
                    block_details=(
                        f"Hard ceiling timeout ({hard_ceiling_sec}s). "
                        "The underlying operation may still complete later."
                    ),
                    duration_sec=duration_sec,
                    late_result_pending=True,
                    phase="late_hard_ceiling",
                    readiness_warnings=[
                        "Reviewed mutative tool exceeded the hard ceiling; late result may still arrive."
                    ],
                    degraded_reasons=[f"hard_ceiling_timeout:{hard_ceiling_sec}"],
                )
        except Exception:
            log.debug("Failed to record reviewed attempt as hard-ceiling failure", exc_info=True)


_CONFIRMATION_ARGS_CHARS = 1000

#: What hangs off the tool context rather than ``RunState``, and has to be put back on resume.
WORKSTATION_SNAPSHOT_KEY: Final = "workstation_session"
_TOOL_OUTCOMES_SNAPSHOT_KEY: Final = "tool_outcomes"
_TERMINAL_ATTACHMENTS_SNAPSHOT_KEY: Final = "terminal_attachments"
_SKILLS_SNAPSHOT_KEY: Final = "skills"

_CONFIRMATION_ASK = "Подтвердите действие:"
_CONFIRMATION_HOW_TO_ANSWER = (
    "Ответьте «да» или «нет». Если разговор уже ушёл дальше, ответьте «да {hint}» или «нет {hint}»."
)


class _ConfirmationResumeState(TypedDict, total=False):
    workstation_session: str
    tool_outcomes: ToolOutcomes
    terminal_attachments: list[TerminalAttachment]
    skills: dict[str, object]


class ToolConfirmationBroker(ConfirmationBroker):
    """Owner decisions in the runtime store, which is where the reply path writes them."""

    def __init__(
        self, *, executor: ToolCallExecutor, claim_token: str, tool_confirmation_reader: IToolConfirmationRepository
    ) -> None:
        self._executor = executor
        self._claim_token = claim_token
        self._tool_confirmation_reader = tool_confirmation_reader
        self._resumed = False

    def evaluate(
        self,
        tool_calls: list[ToolCall],
        *,
        snapshot: dict[str, Any],
        tool_ctx: ToolContext,
        task_id: str,
        round_idx: int,
        active_tool_names: Iterable[str] | None,
    ) -> ConfirmationRound:
        resumed = self._resumed
        self._resumed = False
        gated = self._executor.gated_calls(tool_calls, ctx=tool_ctx, active_tool_names=active_tool_names)
        if not gated:
            if resumed:
                decisions = self._decided(None, task_id=task_id, round_idx=round_idx)
                if not decisions or not self._spend(task_id=task_id, round_idx=round_idx):
                    raise RuntimeError("Cannot apply the parked round's owner confirmation")
                return ConfirmationRound(decisions=decisions, request=None)
            return ConfirmationRound(decisions={}, request=None)
        decisions = self._decided(gated, task_id=task_id, round_idx=round_idx)
        if decisions is None:
            return ConfirmationRound(decisions={}, request=None)
        undecided = [call for call in gated if call.key not in decisions]
        if undecided and tool_ctx.scope.surface_id not in SURFACES_WITH_A_PERSON:
            # Parking here would wait on a reply no peer agent or skill will ever send.
            return ConfirmationRound(decisions=decisions, request=None)
        if not undecided:
            # Spent where the round goes on to run: two gated calls park twice and read the first answer twice.
            if not self._spend(task_id=task_id, round_idx=round_idx):
                return ConfirmationRound(decisions={}, request=None)
            return ConfirmationRound(decisions=decisions, request=None)
        return ConfirmationRound(
            decisions=decisions,
            request=self._open(
                undecided[0], snapshot=snapshot, tool_ctx=tool_ctx, task_id=task_id, round_idx=round_idx
            ),
        )

    def _decided(
        self, gated: list[GatedCall] | None, *, task_id: str, round_idx: int
    ) -> dict[ConfirmationKey, str] | None:
        try:
            stored = self._tool_confirmation_reader.read_tool_confirmation_decisions(task_id, round_idx=round_idx)
        except STORE_FAILURES:
            # Not a reason to fail the task, nor to ask again: a second answer to the call would be stored
            # beside the unread one, and a later read could act on either.
            log.warning("Failed to read the owner's confirmations", exc_info=True)
            return None
        return stored if gated is None else {call.key: stored[call.key] for call in gated if stored.get(call.key)}

    def _spend(self, *, task_id: str, round_idx: int) -> bool:
        try:
            spent = self._tool_confirmation_reader.spend_round_decisions(
                task_id, round_idx=round_idx, now_iso=utc_now_iso()
            )
        except STORE_FAILURES:
            log.warning("Failed to spend the owner's confirmations", exc_info=True)
            return False
        # An answer this attempt did not stamp still stands for the next one, which would run the call again.
        return spent > 0

    def _open(
        self, asked: GatedCall, *, snapshot: dict[str, Any], tool_ctx: ToolContext, task_id: str, round_idx: int
    ) -> ConfirmationRequest | None:
        tool = asked.tool
        confirmation_id = uuid.uuid4().hex
        preview, truncated = self._arguments_preview(asked.args_for_log)
        scope = tool_ctx.scope
        question = self._question(tool, preview, confirmation_id, scope.surface_id)
        try:
            resume_state: _ConfirmationResumeState = {
                WORKSTATION_SNAPSHOT_KEY: ""
                if tool_ctx.workstation is None
                else tool_ctx.workstation.record.session_id,
                _TOOL_OUTCOMES_SNAPSHOT_KEY: self._executor.outcomes(),
                _TERMINAL_ATTACHMENTS_SNAPSHOT_KEY: list(tool_ctx.terminal_attachments),
                _SKILLS_SNAPSHOT_KEY: skills_snapshot(tool_ctx),
            }
            opened = self._tool_confirmation_reader.park_and_open(
                task_id=task_id,
                claim_token=self._claim_token,
                confirmation=ToolConfirmation(
                    owner_id=scope.owner_id,
                    agent_id=scope.agent_id,
                    surface_id=str(scope.surface_id),
                    chat_id=scope.chat_id,
                    confirmation_id=confirmation_id,
                    task_id=task_id,
                    tool_name=tool,
                    call_ordinal=asked.ordinal,
                    round_idx=round_idx,
                    args_hash=asked.args_hash,
                ),
                snapshot=snapshot | resume_state,
                workspace=str(tool_ctx.task_workspace or ""),
                arguments=asked.args_for_log,
            )
        except STORE_FAILURES:
            log.warning("Failed to park a run on an owner confirmation", exc_info=True)
            return None
        if opened is None:
            return None
        log.info(
            "event=tool_confirmation_requested task_id=%s tool=%s confirmation_id=%s", task_id, tool, confirmation_id
        )
        return ConfirmationRequest(
            confirmation_id=confirmation_id,
            task_id=task_id,
            tool=tool,
            arguments_preview=preview,
            arguments_truncated=truncated,
            question=question,
        )

    def abandon(self, request: ConfirmationRequest) -> bool:
        try:
            if not self._tool_confirmation_reader.expire_tool_confirmation(
                request.confirmation_id, now_iso=utc_now_iso()
            ):
                return False
            return self._tool_confirmation_reader.unpark_task(request.confirmation_id, now_iso=utc_now_iso())
        except STORE_FAILURES:
            # Reporting it withdrawn would run the round while the record may still be parked on its question.
            log.warning("Failed to withdraw an undelivered confirmation request", exc_info=True)
            return False

    def restore(self, *, task_id: str, tool_ctx: ToolContext) -> dict[str, Any] | None:
        # Deliberately unguarded: a store failure here must reach the run rather than read as
        # a task that never parked, which would replay every round before the parked one.
        snapshot = self._tool_confirmation_reader.take_parked_snapshot(task_id)
        if snapshot is None:
            return None
        self._resumed = True
        resume_state = cast(_ConfirmationResumeState, snapshot)
        if session_id := resume_state.get(WORKSTATION_SNAPSHOT_KEY):
            self._reattach_workstation(tool_ctx, session_id)
        self._executor.restore_outcomes(resume_state.get(_TOOL_OUTCOMES_SNAPSHOT_KEY, {}))
        tool_ctx.terminal_attachments[:] = resume_state.get(_TERMINAL_ATTACHMENTS_SNAPSHOT_KEY, [])
        restore_skills(tool_ctx, resume_state.get(_SKILLS_SNAPSHOT_KEY, {}))
        return snapshot

    @staticmethod
    def _reattach_workstation(tool_ctx: ToolContext, session_id: str) -> None:
        """Best effort: a session reaped meanwhile leaves the round to tell the model, which reconnects."""
        from ouroboros.tools.workstation.connect_tool import WorkstationUnavailable, attach_session

        try:
            handle = attach_session(tool_ctx)
        except WorkstationUnavailable as exc:
            log.warning("Could not put the parked run's WorkStation session back: %s", exc)
            return
        if handle.record.session_id != session_id:
            # An ephemeral session is one process's: another instance gets a fresh sandbox, without
            # the files the approved call is about. Refused like a reaped one, so the model reconnects.
            log.warning("Could not put the parked run's WorkStation session back: another session answered for it")
            tool_ctx.workstation = None

    @staticmethod
    def _arguments_preview(args_for_log: dict[str, Any]) -> tuple[str, bool]:
        described = json.dumps(args_for_log, ensure_ascii=False, default=str)
        return truncate_for_log(described, _CONFIRMATION_ARGS_CHARS), len(described) > _CONFIRMATION_ARGS_CHARS

    @staticmethod
    def _question(tool: str, arguments_preview: str, confirmation_id: str, surface: SurfaceId) -> str:
        hint = confirmation_id[:CONFIRMATION_ID_HINT_LEN]
        question = f"{_CONFIRMATION_ASK} {tool} [{hint}]\n{arguments_preview}"
        if surface not in TYPED_ANSWER_SURFACES:
            return question
        return f"{question}\n\n{_CONFIRMATION_HOW_TO_ANSWER.format(hint=hint)}"


def build_tool_call_executor(
    *,
    catalog: ToolCatalog,
    execution_memory_repository: IExecutionMemoryRepository,
    audit_event_repository: IAuditEventRepository,
    events: EventSink,
    confirmation_gate: bool,
    task_ledger: TaskLedger | None = None,
    repeat_guard: RepeatToolGuard | None = None,
) -> ToolCallExecutor:
    """Wire the executor to persistence and the run's shared live-log sink."""
    return ToolCallExecutor(
        catalog=catalog,
        journal=DriveEventRecorder(audit_event_repository=audit_event_repository),
        invocations=ExecutionMemoryInvocationRecorder(execution_memory_repository),
        tracer=AefToolTracer(),
        review_gate=CommitGateReviewSink(),
        events=events,
        confirmation_gate=confirmation_gate,
        task_ledger=task_ledger,
        repeat_guard=repeat_guard,
    )


class ToolRunnerAdapter(ToolRunner):
    """Tool execution plus the ``ToolContext`` override channel (option A).

    Holds this task's fixed configuration and a reference to the process-wide catalog,
    nothing per round: the ``ToolContext`` arrives as an argument on every call (D3).
    That also sidesteps the build-order problem — the adapter is constructed before
    ``prepare_context`` has produced a context, and never has to reach for one, and it
    is what lets concurrent tasks share one catalog.

    Which task is running travels the same way: ``task_id`` lives on ``RunState`` and is
    handed to ``handle_tool_calls``, the only method that needs it, rather than being
    captured here.

    The tool work itself is owned by one :class:`ToolCallExecutor`, injected here rather
    than assembled in ``__init__``; both halves of a round delegate to it. The caller
    picks the wiring — :func:`build_tool_call_executor` for the production senders — so
    that substituting one sender does not mean reaching inside this adapter. The catalog
    comes from that executor too: one run, one catalog, held in one place — and so is the
    repeat guard: this adapter keeps no reference of its own, and ``notify_user_input``
    delegates, so there is no second instance that could be the one reset while the
    executor keeps recording failures into another.
    """

    def __init__(
        self,
        *,
        executor: ToolCallExecutor,
        drive_root: pathlib.Path | None,
        cancellation: CancellationSignal,
    ) -> None:
        self._drive_root = drive_root
        self._executor = executor
        self._cancellation = cancellation

    def bind_surface(
        self,
        tool_ctx: ToolContext,
        messages: list[dict[str, Any]],
    ) -> tuple[list[Any], set[str]]:
        # Call-time import: `task_graph` imports this module, so a bound import would close
        # the cycle.
        from ouroboros.agent.task_graph import bind_tool_surface

        return bind_tool_surface(self._executor.catalog, tool_ctx, messages)

    def restore_surface(self, tool_ctx: ToolContext, active_tool_names: set[str]) -> list[Any]:
        catalog = self._executor.catalog
        surface = ProviderToolSurface(catalog, active_tool_names, tool_ctx.scope, tool_ctx.skill_origins)
        tool_ctx.tool_surface = surface
        # Catalog order, not the caller's: a set has none, and the model reads what it is handed.
        return [desc.schema for desc in surface.catalog() if desc.name in active_tool_names]

    def handle_tool_calls(
        self,
        *,
        tool_calls: list[ToolCall],
        active_tool_names: Iterable[str] | None,
        tool_ctx: ToolContext,
        task_id: str,
        decisions: Mapping[ConfirmationKey, str],
    ) -> list[Any]:
        # Tool live logs go out the graph's own sink: the executor is wired with it at
        # construction (see the callers), so the run's one channel stays one channel.
        return self._executor.execute(
            tool_calls,
            ctx=tool_ctx,
            task_id=task_id,
            active_tool_names=active_tool_names,
            cancellation=self._cancellation,
            decisions=decisions,
        )

    def process_tool_results(
        self,
        *,
        results: list[Any],
        messages: list[dict[str, Any]],
        trace: dict[str, Any],
        tool_ctx: ToolContext,
    ) -> int:
        return self._executor.process_results(
            results,
            messages=messages,
            trace=trace,
            ctx=tool_ctx,
        )

    def drain_overrides(self, tool_ctx: ToolContext) -> LoopOverrides:
        ctx = tool_ctx

        pending = ctx._pending_compaction
        ctx._pending_compaction = None

        # `tool_surface` is optional on the context (a task may run without one); the
        # drain is not optional on a surface — it is part of `ToolSurface`.
        surface = ctx.tool_surface
        enable_tools = list(surface.drain_pending_enables()) if surface is not None else []

        return LoopOverrides(
            pending_compaction=pending,
            enable_tools=enable_tools,
        )

    def notify_user_input(self) -> None:
        self._executor.notify_user_input()


class TranscriptCompactorAdapter(TranscriptCompactor):
    """Transcript size estimation, compaction, ledger note, and cache seal."""

    def __init__(
        self,
        *,
        drive_root: pathlib.Path | None,
        task_id: str,
        task_ledger: TaskLedger,
        light_model: str,
        cancellation: CancellationSignal,
        tool_context: Callable[[], ToolContext | None] = lambda: None,
    ) -> None:
        self._drive_root = drive_root
        self._task_id = task_id
        self._tool_context = tool_context
        # From the run configuration, snapshotted at task start: the model that summarizes
        # a round must not change under a run because someone stored an override mid-task.
        self._light_model = light_model
        self._cancellation = cancellation
        # The ledger the tool runner writes into: same instance, handed to both
        # adapters by the per-task builder.
        self._task_ledger = task_ledger
        # Receipt memo for repeated micro-compaction runs. No tool touches it, so
        # it lives here rather than on ToolContext; the adapter is per task, which
        # is exactly the lifetime the memo needs.
        self._replacement_state = ToolResultReplacementState()

    def estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        return self._estimate_messages_tokens(messages)

    def estimate_chars(self, messages: list[dict[str, Any]]) -> int:
        return self._estimate_messages_chars(messages)

    def microcompact(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_recent_rounds: int,
        token_threshold: int,
    ) -> None:
        """Clear old tool results, keeping the last N rounds intact.

        Skips protected tools (memory_read, data_read) and error results.
        Only runs when estimated context exceeds token_threshold.
        Counts by assistant rounds (which may have multiple parallel tool calls),
        not by individual tool messages.
        """
        if self._estimate_messages_tokens(messages) < token_threshold:
            return

        round_starts: list[int] = []
        for i, m in enumerate(messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                round_starts.append(i)

        if len(round_starts) <= keep_recent_rounds:
            return

        protected_boundary = round_starts[-keep_recent_rounds]

        for i, m in enumerate(messages):
            if i >= protected_boundary:
                break
            if m.get("role") != "tool":
                continue
            if m.get("content") == _MICROCOMPACT_CLEARED:
                continue
            content = str(m.get("content") or "")
            if content.startswith("[Tool receipt]"):
                continue
            if content.startswith("⚠️"):
                continue
            tool_name = _find_tool_name_for_result(m, messages)
            if tool_name in _COMPACTION_PROTECTED_TOOLS:
                continue
            from ouroboros.agent.tool_receipts import receipt_from_message

            messages[i] = {
                **m,
                "content": receipt_from_message(
                    drive_root=self._drive_root,
                    task_id=self._task_id,
                    message=m,
                    messages=messages,
                    tool_name=tool_name or "unknown_tool",
                    state=self._replacement_state,
                ),
            }

    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_recent: int,
        target_tokens: int | None = None,
        target_chars: int | None = None,
    ) -> dict[str, Any] | None:
        tool_ctx = self._tool_context()
        instructions = active_skill_instructions(tool_ctx)
        result = compact_tool_history_llm(
            messages,
            self._light_model,
            keep_recent=keep_recent,
            protected_instructions=instructions,
            cancellation=self._cancellation,
            task_id=self._task_id,
        )
        if self._cancellation.is_stopped():
            return None
        # Replace contents in place so the same list object (shared with
        # ToolContext.messages) always reflects the compacted transcript.
        messages[:] = result.messages
        if instructions and tool_ctx is not None:
            tokens = self.estimate_tokens(messages)
            chars = self.estimate_chars(messages)
            if (target_tokens is not None and tokens > target_tokens) or (
                target_chars is not None and chars > target_chars
            ):
                compact_active_skills(
                    tool_ctx,
                    token_budget=None
                    if target_tokens is None
                    else target_tokens - tokens + estimate_tokens(instructions),
                    char_budget=None if target_chars is None else target_chars - chars + len(instructions),
                )
        updated_instructions = active_skill_instructions(tool_ctx)
        if is_token_overflow(result.failure) and updated_instructions != instructions:
            result = compact_tool_history_llm(
                messages,
                self._light_model,
                keep_recent=keep_recent,
                protected_instructions=updated_instructions,
                cancellation=self._cancellation,
                task_id=self._task_id,
            )
            if self._cancellation.is_stopped():
                return None
            messages[:] = result.messages
        return result.usage

    def upsert_ledger_note(self, messages: list[dict[str, Any]]) -> None:
        """Refresh the ledger note in place, touching nothing else in the transcript.

        The note is a *delimited block*, not a message of its own. It cannot be one: the
        checkpoint and the owner inbox both merge their text into a trailing user turn, so
        a note that owned its message would take theirs down with it the moment the next
        round replaced it — and the round-15 self-check reminder is exactly the text that
        lands there. Stripping the block and re-appending through the shared merge helper
        keeps the note single and current while rewriting only what this method wrote.

        Appending through ``append_or_merge_user_message`` rather than ``messages.append``
        is what keeps two user turns from ending up adjacent, which providers reject.
        """
        try:
            rendered = str(self._task_ledger.render() or "").strip()
        except Exception:
            # Keep the last good note. A render that failed says nothing about whether the
            # outcomes it described are still worth showing the model, and dropping through
            # to the empty-ledger path would delete them on the strength of a bug.
            log.debug("Failed to render tool outcome ledger", exc_info=True)
            return

        self._strip_ledger_notes(messages)
        if not rendered:
            return
        append_or_merge_user_message(
            messages,
            f"{_TOOL_LEDGER_NOTE_START}\n{rendered}\n{_TOOL_LEDGER_NOTE_END}",
        )

    def seal(self, messages: list[dict[str, Any]]) -> None:
        self._seal_transcript(messages)

    @staticmethod
    def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
        """Estimate transcript size in tokens using Cyrillic-aware heuristic."""
        total = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total += estimate_tokens(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype in ("image_url", "image", "document"):
                        total += _IMAGE_BLOCK_TOKEN_ESTIMATE
                    else:
                        text = block.get("text") or block.get("content") or ""
                        if text:
                            total += estimate_tokens(str(text))
            reasoning_content = msg.get("reasoning_content")
            if reasoning_content:
                total += estimate_tokens(str(reasoning_content))
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total += estimate_tokens(str(tool_calls))
            tc_id = msg.get("tool_call_id")
            if tc_id:
                total += estimate_tokens(str(tc_id))
            total += 4
        return total

    @staticmethod
    def _estimate_messages_chars(messages: list[dict[str, Any]]) -> int:
        """Estimate mutable transcript size; excludes the static cached system block."""
        total = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        try:
                            total += len(json.dumps(block, ensure_ascii=False))
                        except (TypeError, ValueError):
                            total += len(str(block))
            reasoning_content = msg.get("reasoning_content")
            if reasoning_content:
                total += len(str(reasoning_content))
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                try:
                    total += len(json.dumps(tool_calls, ensure_ascii=False))
                except (TypeError, ValueError):
                    total += sum(len(str(tc)) for tc in tool_calls)
            tc_id = msg.get("tool_call_id")
            if tc_id:
                total += len(str(tc_id))
        return total

    @staticmethod
    def _is_tool_outcome_ledger_note(message: dict[str, Any]) -> bool:
        content = str(message.get("content") or "")
        return message.get("role") == "user" and _TOOL_LEDGER_NOTE_START in content and _TOOL_LEDGER_NOTE_END in content

    @staticmethod
    def _strip_ledger_block(text: str) -> str:
        """Return ``text`` without the ledger block, and without the seam it was merged on.

        Leaving the ``MERGE_SEPARATOR`` behind would grow a trailing ``---`` rule per round
        in whatever turn the note was folded into.
        """
        start = text.find(_TOOL_LEDGER_NOTE_START)
        if start < 0:
            return text
        end = text.find(_TOOL_LEDGER_NOTE_END, start)
        head = text[:start]
        tail = "" if end < 0 else text[end + len(_TOOL_LEDGER_NOTE_END) :]
        if head.endswith(MERGE_SEPARATOR):
            head = head[: -len(MERGE_SEPARATOR)]
        return (head + tail).strip()

    @staticmethod
    def _strip_ledger_notes(messages: list[dict[str, Any]]) -> None:
        """Remove every ledger block in place, keeping whatever else shares its message.

        A message is dropped only when the block was all it held — the case where the note
        was appended on its own.
        """
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not TranscriptCompactorAdapter._is_tool_outcome_ledger_note(message):
                continue
            content = message.get("content")
            if isinstance(content, list):
                blocks: list[Any] = []
                for block in content:
                    if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                        blocks.append(block)
                        continue
                    stripped = TranscriptCompactorAdapter._strip_ledger_block(block["text"])
                    if stripped:
                        blocks.append({**block, "text": stripped})
                if blocks:
                    messages[index] = {**message, "content": blocks}
                else:
                    del messages[index]
                continue
            stripped = TranscriptCompactorAdapter._strip_ledger_block(str(content or ""))
            if stripped:
                messages[index] = {**message, "content": stripped}
            else:
                del messages[index]

    @staticmethod
    def _seal_transcript(
        messages: list[dict[str, Any]],
        keep_active: int = 5,
        min_prefix_tokens: int = 2048,
    ) -> None:
        """Mark one stable old tool-result boundary with Anthropic cache_control."""
        extract = TranscriptCompactorAdapter._extract_plain_text_from_content
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                # Flatten the old sealed boundary before choosing a new one.
                msg["content"] = extract(content)

        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_indices) <= keep_active:
            return

        seal_candidate_idx = tool_indices[-(keep_active + 1)]

        prefix_text_len = sum(len(extract(m.get("content", ""))) for m in messages[: seal_candidate_idx + 1])
        prefix_tokens = prefix_text_len // 4  # rough 4-chars-per-token estimate

        if prefix_tokens < min_prefix_tokens:
            return

        candidate = messages[seal_candidate_idx]
        plain_text = str(candidate.get("content", ""))
        candidate["content"] = [
            {
                "type": "text",
                "text": plain_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    @staticmethod
    def _extract_plain_text_from_content(content: Any) -> str:
        """Extract text from strings or multipart content for transcript sealing."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", ""))
            return "".join(parts)
        return str(content) if content is not None else ""


class OwnerInboxAdapter(OwnerInbox):
    """Injects owner messages from the queue and the drive mailbox."""

    def __init__(
        self,
        *,
        incoming_messages: queue.Queue,
        drive_root: pathlib.Path | None,
        task_id: str,
        events: IAgentEventPublisher | None,
        scope: OwnerScope,
    ) -> None:
        self._incoming_messages = incoming_messages
        self._drive_root = drive_root
        self._task_id = task_id
        self._events = events
        self._scope = scope

    def drain(self, messages: list[dict[str, Any]], seen: set[str]) -> bool:
        """Inject owner messages received during task execution."""
        injected_any = False
        while not self._incoming_messages.empty():
            try:
                injected = self._incoming_messages.get_nowait()
                if isinstance(injected, dict):
                    append_or_merge_user_content(messages, self._owner_marked_content(build_user_content(injected)))
                else:
                    append_or_merge_user_message(messages, self._owner_marked_content(injected))
                injected_any = True
            except queue.Empty:
                break

        if self._drive_root is not None and self._task_id:
            from ouroboros.agent.owner_inject import drain_owner_messages

            drive_msgs = drain_owner_messages(self._scope, self._drive_root, task_id=self._task_id, seen_ids=seen)
            for dmsg in drive_msgs:
                append_or_merge_user_message(messages, self._owner_marked_content(dmsg))
                injected_any = True
                if self._events is not None:
                    try:
                        self._events.publish(
                            OwnerMessageInjected(
                                scope=self._scope,
                                task_id=self._task_id,
                                text=str(dmsg),
                                ts=utc_now_iso(),
                            )
                        )
                    except Exception:
                        log.debug("Failed to report an owner message injection", exc_info=True)
        return injected_any

    @staticmethod
    def _owner_marked_content(content: Any) -> Any:
        """Mark direct owner injections with the same priority tag as mailbox messages."""
        prefix = "[Message from my human]: "
        if isinstance(content, list):
            blocks = [dict(block) if isinstance(block, dict) else block for block in content]
            for block in blocks:
                if isinstance(block, dict) and str(block.get("type") or "") in {"text", "input_text"}:
                    block["text"] = prefix + str(block.get("text") or "")
                    return blocks
            return [{"type": "text", "text": prefix.rstrip()}, *blocks]
        return prefix + str(content or "")


class ContextAssemblerAdapter(ContextAssembler):
    """The run's ``ToolContext`` and opening transcript, built by the agent (D8).

    Delegates to ``OuroborosAgent._build_tool_context`` and ``OuroborosAgent._assemble_opening_transcript``.
    The same shape as ``prepare_tools``: the node reaches an interface, and the policy
    behind it — the tool context, the soft-cap budget, memory recall, the prompt build —
    stays on the ouroboros side rather than moving into ``new_core``.

    ``root_task_id`` is resolved once for the run by ``resolve_task_lineage`` and held
    here, so assembly does not re-read the task record the ``EventSinkAdapter`` already
    read for the running-status write (D4).
    """

    def __init__(
        self,
        *,
        agent: OuroborosAgent,
        root_task_id: str,
        light_model: str,
        skill_surface: SkillSurface,
        skill_origins: frozenset[str] | None,
        cancellation: CancellationSignal,
    ) -> None:
        self._agent = agent
        self._root_task_id = root_task_id
        self._light_model = light_model
        self._skill_surface = skill_surface
        self._skill_origins = skill_origins
        self._cancellation = cancellation

    def tool_context(
        self,
        *,
        task: dict[str, Any],
        task_workspace: pathlib.Path | None,
        scope: OwnerScope,
        messages: list[dict[str, Any]],
    ) -> ToolContext:
        tool_ctx = self._agent._build_tool_context(
            task,
            task_workspace,
            root_task_id=self._root_task_id,
            scope=scope,
            skill_surface=self._skill_surface,
        )
        tool_ctx.light_model = self._light_model
        tool_ctx.skill_origins = self._skill_origins
        tool_ctx.messages = messages
        return tool_ctx

    def opening_transcript(
        self,
        *,
        task: dict[str, Any],
        tool_ctx: ToolContext,
        scope: OwnerScope,
    ) -> list[dict[str, Any]]:
        return self._agent._assemble_opening_transcript(
            task, tool_ctx, scope, self._skill_surface, cancellation=self._cancellation
        )


class EventSinkAdapter(EventSink):
    """Progress, usage, periodic checkpoint, and final-result surfacing.

    ``task_started`` opens the task's record and ``emit_result`` publishes its final
    result (the empty-text fallback plus ``emit_task_results``); both close over ``env``
    / ``task`` and the run's lineage — ``emit_result`` over ``llm`` / ``pending_events``
    / ``start_time`` as well — all provided only when the adapter drives the task graph.
    The standalone ``run_llm_loop`` builds this adapter without them and calls neither
    (it has no task record, and its terminal is ``END``), so the extra fields stay
    ``None`` there.

    ``memory_events`` is required rather than defaulted, so a caller that reaches
    ``emit_result`` cannot silently drop every completed task on the floor. The
    adapter applies no eligibility rule of its own — what is worth distilling is
    decided on the consuming side, by the distillation handler.

    ``scope`` is a required constructor argument, not a read off the optional
    ``tool_ctx``: the crash path reaches ``emit_result`` with an unguarded
    ``state.tool_ctx`` which may be ``None``, and the event's identity must not
    depend on which terminal reached it.
    """

    def __init__(
        self,
        *,
        emit_progress: Callable[[str], None],
        events: IAgentEventPublisher | None,
        task_id: str,
        drive_logs: pathlib.Path,
        memory_events: IMemoryEventPublisher,
        execution_memory_repository: IExecutionMemoryRepository,
        audit_event_repository: IAuditEventRepository,
        scope: OwnerScope,
        env: Any = None,
        llm: Any = None,
        pending_events: list[AgentEvent] | None = None,
        task: dict[str, Any] | None = None,
        start_time: float = 0.0,
        parent_task_id: str | None = None,
        root_task_id: str = "",
        cancellation: CancellationSignal | None = None,
    ) -> None:
        self._memory_events = memory_events
        self._execution_memory_repository = execution_memory_repository
        self._audit_event_repository = audit_event_repository
        self._emit_progress = emit_progress
        self._events = events
        self._task_id = task_id
        self._drive_logs = drive_logs
        self._scope = scope
        self._env = env
        self._llm = llm
        self._pending_events = pending_events
        self._task = task
        self._detached = bool((task or {}).get("detached"))
        self._start_time = start_time
        # Resolved once per run by `resolve_task_lineage` and handed in, rather than read
        # off the task record here and again by the context assembler (D4).
        self._parent_task_id = parent_task_id
        self._root_task_id = root_task_id
        self._cancellation = cancellation
        #: Set True the moment ``emit_result`` begins its work, so ``handle_task``'s
        #: crash path can tell a publish-time failure (already emitting) from a node
        #: crash and avoid publishing the result a second time.
        self.result_emitted = False
        #: Set True by ``handle_task``'s crash path once it has written the terminal record
        #: itself, so its own error text is not published as a late answer to a stop.
        self.self_failed = False

    def progress(self, text: str) -> None:
        if self._detached:
            return
        self._emit_progress(text)

    def typing(self) -> None:
        if self._events is None or self._detached:
            return
        try:
            self._events.publish(
                TypingStarted(
                    scope=self._scope,
                    source=str(self._scope.surface_id),
                    trace_id=aef_tracing.current_trace_id(),
                    ts=utc_now_iso(),
                )
            )
        except Exception:
            log.warning("Failed to report the typing start", exc_info=True)

    def ask_owner(self, text: str, *, request: ConfirmationRequest) -> bool:
        if self._events is None:
            return False
        try:
            self._events.publish(
                OutboundText(
                    scope=self._scope,
                    text=text,
                    fmt="markdown",
                    # Not a pending event: those flush when the task ends, and it is waiting on this.
                    is_progress=False,
                    task_id=self._task_id,
                    source=str(self._scope.surface_id),
                    transport={
                        **dict((self._task or {}).get("transport") or {}),
                        CONFIRMATION_TRANSPORT_KEY: dataclasses.asdict(request),
                    },
                    trace_id=aef_tracing.current_trace_id(),
                    ts=utc_now_iso(),
                )
            )
        except Exception:
            log.warning("Failed to ask the owner for a confirmation", exc_info=True)
            return False
        return True

    def emit_tool_live_log(self, payload: dict[str, Any]) -> None:
        """Put one live log line on the event queue, stamped with time and task.

        The SSOT helper no-ops on a missing queue and swallows its own failures, which is
        what makes this callable from a tool worker thread without a guard at the call site.
        The payload's own keys win, so a caller that names a different task still can.
        """
        emit_worker_log_event(
            self._events,
            {"ts": utc_now_iso(), "task_id": self._task_id, **payload},
            scope=self._scope,
            log_label="tool live",
        )

    def usage(self, model: str, usage: dict[str, Any], kind: str) -> None:
        emit_llm_usage_event(self._events, self._task_id, model, usage, kind, scope=self._scope)

    def maybe_checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        round_idx: int,
        max_rounds: int,
    ) -> bool:
        """Inject a normal user-turn self-check and emit one checkpoint event."""
        REMINDER_INTERVAL = 15
        if round_idx <= 1 or round_idx % REMINDER_INTERVAL != 0 or round_idx >= max_rounds:
            return False

        ctx_tokens = sum(
            estimate_tokens(str(m.get("content", "")))
            if isinstance(m.get("content"), str)
            else sum(estimate_tokens(str(b.get("text", ""))) for b in m.get("content", []) if isinstance(b, dict))
            for m in messages
        )
        checkpoint_num = round_idx // REMINDER_INTERVAL

        tool_trace = self._build_recent_tool_trace(messages)

        reminder = (
            f"[CHECKPOINT {checkpoint_num} — round {round_idx}/{max_rounds}]\n"
            f"Context: ~{ctx_tokens} tokens | Rounds remaining: {max_rounds - round_idx}\n"
        )
        if tool_trace:
            reminder += f"\n{tool_trace}\n"
        reminder += (
            "\nThis is a periodic self-check, not a command to stop. "
            "Glance at your recent tool-call trace above and briefly consider:\n"
            "- Are you still making progress toward the task, or repeating the same actions?\n"
            "- Is the current approach still the right one, or should you narrow scope / try a different angle?\n"
            "- If the task is effectively done, wrap up by replying with your final answer in plain text (no tool call). "
            "Otherwise continue with the most valuable next step.\n"
            "\nNo special format required — just think, then act."
        )

        # Merge into a prior user turn to avoid Anthropic consecutive-role 400s,
        # preserving multipart blocks so images/cache markers survive.
        append_or_merge_user_message(messages, reminder)
        if load_settings().OUROBOROS_CHECKPOINT_PROGRESS_ENABLED:
            self.progress(f"Checkpoint {checkpoint_num} at round {round_idx}: ~{ctx_tokens} tokens")

        self._emit_checkpoint_event(
            {
                "checkpoint_number": checkpoint_num,
                "round": round_idx,
                "max_rounds": max_rounds,
                "context_tokens": ctx_tokens,
            }
        )

        return True

    @staticmethod
    def _build_recent_tool_trace(messages: list[dict[str, Any]], window: int = 15) -> str:
        """Build a compact recent-tool trace for the self-check prompt."""
        all_calls: list[str] = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", "")
                    if isinstance(args, dict):
                        args = json.dumps(args, sort_keys=True)
                    args_str = str(args)
                    summary = f"{name}({args_str[:80]})" if len(args_str) > 80 else f"{name}({args_str})"
                    all_calls.append(summary)
        recent = all_calls[-window:] if all_calls else []
        if not recent:
            return ""
        return "Recent tool calls (oldest first):\n" + "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(recent))

    def _emit_checkpoint_event(self, data: dict[str, Any]) -> None:
        """Report a task_checkpoint onto the seam, or record it here when there is none."""
        from ouroboros.agent.loop_llm_call import _emit_live_log

        payload = {"type": "task_checkpoint", "task_id": self._task_id, **data}
        if self._events is not None:
            _emit_live_log(self._events, self._scope, payload)
        elif self._drive_logs:
            try:
                record_event(
                    "events",
                    "task_checkpoint",
                    {"ts": utc_now_iso(), **payload},
                    audit_event_repository=self._audit_event_repository,
                )
            except Exception:
                pass

    def task_started(self) -> None:
        """Record startup after checking detached execution authority."""
        task = self._task or {}
        if task.get("detached"):
            try:
                active = runtime_persistence.current.tasks_repo.is_task_attempt_active(
                    self._task_id, claim_token=str(task.get("execution_claim") or "")
                )
            except STORE_FAILURES as exc:
                raise ExecutionClaimUnavailable(self._task_id) from exc
            if not active:
                raise RuntimeError("Task execution authority lost before startup")
            return
        sanitized_task = sanitize_task_for_event(task)
        record_event(
            "events",
            "task_received",
            {"ts": utc_now_iso(), "task": sanitized_task},
            audit_event_repository=self._audit_event_repository,
        )
        try:
            write_task_result(
                self._env.drive_root,
                self._task_id,
                STATUS_RUNNING,
                scope=self._scope,
                parent_task_id=self._parent_task_id,
                root_task_id=self._root_task_id or None,
                chat_id=self._scope.chat_id,
                description=task.get("description"),
                context=task.get("context"),
                trace_id=str(task.get("trace_id") or ""),
                result="Task is running.",
            )
        except Exception:
            log.debug("Failed to persist running task status", exc_info=True)

    def emit_result(
        self,
        *,
        text: str | None,
        usage: dict[str, Any],
        trace: dict[str, Any],
        # The ``EventSink`` ABC declares this ``Any`` — ``new_core`` cannot name an
        # ouroboros type — but the adapter knows what it actually receives, so it says so.
        tool_ctx: ToolContext,
    ) -> None:
        """Apply the empty-text fallback, publish the result, then report the task.

        Only the task graph reaches this (its terminal ``emit`` node); the FAILED
        write for a crashing node stays in ``handle_task``'s scaffolding, because
        a graceful error terminal must publish its ⚠️ text as a normal result —
        exactly as the pre-migration ``handle_task`` did — and the four-argument
        signature cannot tell that terminal apart from a crash.

        Whether an answer existed at all travels beside the text, because the fallback
        erases the difference between a stopped run and a model that replied with nothing.

        A task that ended on ``completed`` is then reported to background memory as one
        raw event, stamped here because the consumer runs long before ``finished_at`` is
        written.
        """
        # Mark before doing any work: emit_task_results appends the send_message
        # before its DB/consolidation work, so if it raises partway handle_task
        # must not re-publish (that would double the user's reply).
        self.result_emitted = True
        from ouroboros.agent.task_pipeline import emit_task_results

        if isinstance(text, str) and text.strip():
            has_answer, published = True, text
        else:
            has_answer, published = False, "⚠️ Model returned an empty response. Try rephrasing your request."
        unsuccessful = emit_task_results(
            self._env,
            self._llm,
            self._pending_events if self._pending_events is not None else [],
            self._task or {},
            published,
            usage,
            trace,
            self._start_time,
            self._drive_logs,
            ctx=tool_ctx,
            scope=self._scope,
            execution_memory_repository=self._execution_memory_repository,
            has_answer=has_answer,
            stopped_externally=not self.self_failed,
            stop_observed=self._cancellation.is_stopped() if self._cancellation is not None else False,
            audit_event_repository=self._audit_event_repository,
        )
        if unsuccessful or self._detached:
            return
        task = self._task or {}
        self._memory_events.publish(
            MemoryDistillationEvent(
                scope=self._scope,
                # Distillation continues the task's own operation, on another thread that
                # cannot see this one's context. Read here, carried as a field.
                trace_id=aef_tracing.current_trace_id(),
                task=MemoryTaskRef(
                    task_id=str(task.get("id") or ""),
                    task_type=str(task.get("type") or ""),
                    text=str(task.get("text") or ""),
                    recorded_at=time.time(),
                ),
                response_text=published,
                llm_trace=trace,
            )
        )


__all__ = [
    "ContextAssemblerAdapter",
    "EventSinkAdapter",
    "LLMProviderAdapter",
    "OwnerInboxAdapter",
    "ToolRunnerAdapter",
    "TranscriptCompactorAdapter",
]
