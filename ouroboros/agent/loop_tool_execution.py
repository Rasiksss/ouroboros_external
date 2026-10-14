"""LLM-loop tool execution: dispatch, timeouts, truncation, live logs.

One class owns a round's tool work: :class:`ToolCallExecutor`. Its outward channels — the
on-disk event journal, the working-memory invocation record, tracing spans, and the commit
gate's late/hard-ceiling records — are the interfaces defined at the top of this module,
injected at construction. Concrete implementations live in ``ouroboros/agent/loop_adapters.py``
next to the other adapters; nothing in this module names a drive path, a runtime store, or a
tracing backend.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
import json
import logging
import pathlib
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from config_registry.user_settings.tool_access import skill_tool_refusal
from new_core.coordination.registry import TOOL_BODIES, concurrency_budgets
from new_core.llm.types import ToolCall
from new_core.tools.catalog import ToolCatalog
from new_core.tools.interfaces import ToolResult
from ouroboros.agent.context import retain_skill_page
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger, ToolOutcomes, safe_args_hash
from ouroboros.contracts.tool_confirmation import (
    CONFIRMATION_APPROVED,
    CONFIRMATION_DENIED,
    CONFIRMATION_EXPIRED,
    CONFIRMATION_NOT_ASKED,
)
from ouroboros.observability.event_record import LogStream
from ouroboros.policy.capabilities import (
    DEFAULT_TOOL_RESULT_LIMIT as _DEFAULT_TOOL_RESULT_LIMIT,
    REVIEWED_MUTATIVE_TOOLS,
    TOOL_RESULT_LIMITS as _TOOL_RESULT_LIMITS,
    UNTRUNCATED_TOOL_RESULTS as _UNTRUNCATED_TOOL_RESULTS,
)
from ouroboros.policy.tool_authorization import PolicyDecision, authorize_tool, build_policy_tool_denied_event
from ouroboros.tools.catalog import TOOL_SPECS
from ouroboros.tools.dispatch import execute_tool
from ouroboros.tools.runtime_metadata import ResolvedToolRuntimeMetadata, resolve_tool_runtime_metadata
from ouroboros.utils import (
    sanitize_tool_args_for_log,
    sanitize_tool_result_for_log,
    truncate_for_log,
    utc_now_iso,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from new_core.identity import OwnerScope
    from new_core.loop.interfaces import CancellationSignal, ConfirmationKey, EventSink
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)

_FAILURE_PREFIXES = (
    "⚠️ TOOL_",
    "⚠️ SHELL_",
    "⚠️ SKILLS_UNAVAILABLE",
    "⚠️ SKILL_READ_",
    "⚠️ SKILL_EXEC_",
    "⚠️ MCP_TOOL_ERROR",
    # Named one by one rather than as "⚠️ A2A_": A2A_INPUT_REQUIRED wears the same glyph and
    # is not a failure — the remote task is alive and the question it asked is the result.
    "⚠️ A2A_ERROR",
    "⚠️ A2A_TIMEOUT",
    "⚠️ A2A_SUBMISSION_UNKNOWN",
)
_FILE_FAILURE_PREFIXES = (
    "⚠️ DATA_WRITE_",
    "⚠️ SKILL_PAYLOAD_",
    "⚠️ SKILL_REDIRECT_",
    "⚠️ SEND_IMAGE_",
    "⚠️ SEND_FILE_",
)
_EXIT_CODE_RE = re.compile(r"exit_code=(-?\d+)")
_SIGNAL_RE = re.compile(r"signal=([A-Z0-9_]+)")
_TOOL_ERROR_CODE_RE = re.compile(r"^⚠️ ([A-Z][A-Z0-9_]+)")
_DELIVERY_UNKNOWN_ERROR_CODES = frozenset(
    {"SEND_FILE_DELIVERY_UNKNOWN", "SEND_IMAGE_DELIVERY_UNKNOWN", "A2A_SUBMISSION_UNKNOWN"}
)
_MEDIA_TIMEOUT_ERROR_CODES = {
    "send_file": "SEND_FILE_DELIVERY_UNKNOWN",
    "send_image": "SEND_IMAGE_DELIVERY_UNKNOWN",
    "send_photo": "SEND_IMAGE_DELIVERY_UNKNOWN",
}

# Reviewed mutative tools get a hard ceiling after their soft timeout.
_REVIEWED_MUTATIVE_HARD_CEILING = 1800

#: How many of a round's tool calls may be in flight at once. A share of the process-wide
#: budget below rather than an allowance of its own: one round's ceiling multiplied by the
#: slot count is not a bound on the process.
_ROUND_TOOL_BODY_ALLOWANCE = 8
_TOOL_BUDGET_WAIT_SLICE_SEC = 0.05
#: How long a tool body's grant outlives a holder that never releases it. Above the longest
#: tool deadline plus the slot grace period, so an abandoned body's slot comes back once the
#: thread it kept can no longer be running.
_TOOL_BODY_LEASE_TTL_SEC = float(_REVIEWED_MUTATIVE_HARD_CEILING + 900)
#: Marks a timeout result whose body kept its thread, so the round leaves that call's grant
#: to expire rather than releasing it. Carried on the result rather than in instance state
#: keyed by the provider's tool-call id: those ids repeat across rounds on some providers,
#: and a repeat made a later call skip a release it should have made.
_BODY_ABANDONED_KEY = "body_abandoned"

_CONFIRMATION_MESSAGES = {
    CONFIRMATION_DENIED: "⚠️ CONFIRMATION_DENIED ({tool}): the owner refused this call. Do not retry it; "
    "continue without it and say what was left undone.",
    CONFIRMATION_EXPIRED: "⚠️ CONFIRMATION_EXPIRED ({tool}): the request was closed without an answer, so the "
    "call was not made. Continue without it and say what is waiting on their decision.",
    CONFIRMATION_NOT_ASKED: "⚠️ CONFIRMATION_NOT_ASKED ({tool}): the owner was not asked about this call, so it "
    "was not made. If a request in this chat is already waiting for their decision, it stays open and they "
    "answer it there. Continue without this call and say it was not made.",
}

#: Statuses that mean the call was not made and nothing about it failed.
_CALL_NOT_MADE = frozenset(_CONFIRMATION_MESSAGES)


class EventRecorder(abc.ABC):
    """The on-disk event journal (``events`` / ``tools`` streams).

    Where the journal lives is bound inside the implementation, which is what removes the
    drive path from every signature in this module.
    """

    @abc.abstractmethod
    def record(self, stream: LogStream, name: str, payload: dict[str, Any]) -> None:
        """Append one event to the named stream."""


class InvocationRecorder(abc.ABC):
    """Working-memory invocation recording after tool execution."""

    @abc.abstractmethod
    def record(self, ctx: ToolContext | None, execution: dict[str, Any]) -> None:
        """Record one execution-result dict for the task ``ctx`` belongs to."""


class ToolTracer(abc.ABC):
    """Tracing spans around a tool body. Payload masking lives in the implementation."""

    @abc.abstractmethod
    def span(self, *, tool: str, args: dict[str, Any], task_id: str) -> AbstractContextManager[Any]:
        """Open a span for one tool call; the yielded handle is opaque to the executor."""

    @abc.abstractmethod
    def add_result(self, span: Any, *, result: Any, is_error: bool) -> None:
        """Attach the call's outcome to ``span``. Best-effort; must not raise."""


class ReviewGateSink(abc.ABC):
    """Commit-gate records for reviewed mutative tools that outlive their soft timeout.

    Both methods are best-effort and must not raise — the gate reacting late must never
    break the round that is trying to report it.
    """

    @abc.abstractmethod
    def mark_attempt_late(self, ctx: ToolContext | None, *, soft_timeout_sec: int, duration_sec: float) -> None:
        """Mark the in-flight review attempt as late_result_pending."""

    @abc.abstractmethod
    def record_hard_ceiling(self, ctx: ToolContext | None, *, duration_sec: float, hard_ceiling_sec: int) -> None:
        """Record the terminal hard-ceiling state; a late real result may overwrite it."""


@dataclasses.dataclass(frozen=True)
class GatedCall:
    """One call of a round that may not run unasked, with what the question is built from."""

    ordinal: int
    tool: str
    args_for_log: dict[str, Any]
    args_hash: str

    @property
    def key(self) -> ConfirmationKey:
        return (self.ordinal, self.tool, self.args_hash)


@dataclasses.dataclass(frozen=True)
class _Preflight:
    """What both entry points check before a call.

    ``decision`` is the policy's, or, for a call refused before the policy is asked, the round's result.
    """

    fn_name: str
    args: dict[str, Any] | None
    args_for_log: dict[str, Any]
    is_code_tool: bool
    decision: PolicyDecision | dict[str, Any]


class ToolCallExecutor:
    """Executes one round's tool calls and folds their results into the transcript.

    Constructed once per task with the stable collaborators: the catalog, the sender
    interfaces — the live-log ``EventSink`` among them, since the run has exactly one and
    it would otherwise be threaded through every private step — and the ledger/repeat-guard
    pair. Per-round state — the tool context, the task id, the active-tool set — arrives on
    the public methods: no per-round state is retained here.

    A round's tool work is two calls rather than one: :meth:`aexecute` (or its synchronous
    bridge :meth:`execute`) runs the calls, :meth:`process_results` folds what they returned
    into ``messages`` and ``trace``. The graph makes both, in that order, so that "what ran"
    and "what the model is told about it" are separate steps rather than one opaque effect.

    Concurrency stays a decision of this class: ``can_parallel`` says who may overlap, the
    semaphore says how many at once. A runtime places one call and knows nothing about the
    batch it belongs to.
    """

    def __init__(
        self,
        *,
        catalog: ToolCatalog,
        journal: EventRecorder,
        invocations: InvocationRecorder,
        tracer: ToolTracer,
        review_gate: ReviewGateSink,
        events: EventSink,
        confirmation_gate: bool,
        task_ledger: TaskLedger | None = None,
        repeat_guard: RepeatToolGuard | None = None,
    ) -> None:
        self._catalog = catalog
        self._journal = journal
        self._invocations = invocations
        self._tracer = tracer
        self._review_gate = review_gate
        self._events = events
        self._confirmation_gate = confirmation_gate
        self._task_ledger = task_ledger
        self._repeat_guard = repeat_guard

    @property
    def catalog(self) -> ToolCatalog:
        """The tool catalog this executor dispatches through.

        Public because the catalog is the run's, not this class's: whoever holds the
        executor holds its catalog too, and reads it here rather than being handed a
        second reference to keep in step.
        """
        return self._catalog

    def notify_user_input(self) -> None:
        """Clear the repeat guard's failure streaks — the owner has said something new.

        The guard is this executor's: it is the only writer of the failure streaks, so the
        reset goes through here rather than through a second reference held by a caller.
        Two references could drift apart, and a caller resetting the wrong instance would
        leave a blocked tool blocked for the rest of the task.
        """
        if self._repeat_guard is not None:
            self._repeat_guard.mark_user_input()

    def outcomes(self) -> ToolOutcomes:
        outcomes = ToolOutcomes()
        if self._task_ledger is not None:
            outcomes["ledger"] = self._task_ledger.snapshot()
        if self._repeat_guard is not None:
            outcomes["repeat_guard"] = self._repeat_guard.snapshot()
        return outcomes

    def restore_outcomes(self, outcomes: ToolOutcomes) -> None:
        if self._task_ledger is not None:
            self._task_ledger.restore(outcomes.get("ledger", []))
        if self._repeat_guard is not None:
            self._repeat_guard.restore(outcomes.get("repeat_guard", []))

    # --- public: the two halves of a round's tool work ------------------------------------

    async def aexecute(
        self,
        tool_calls: list[ToolCall],
        *,
        ctx: ToolContext,
        task_id: str,
        active_tool_names: Iterable[str] | None = None,
        decisions: Mapping[ConfirmationKey, str],
        cancellation: CancellationSignal | None = None,
    ) -> list[dict[str, Any]]:
        """Execute calls in order while active, returning one raw result per started call.

        Execution only: turning the results into transcript messages, trace entries and
        ledger rows is :meth:`process_results`, which the caller runs next.

        ``decisions`` carries the owner's verdicts for this round, keyed by the call each was
        given for. A gated call runs on an approval and on nothing else; putting the question
        and waiting for the answer happen a layer up, where a run can leave the graph.

        The live logs every path out of a tool call emits (started / finished / late /
        timeout) go to the ``EventSink`` this executor was built with — the run has one,
        and it is wired once rather than handed down through each private step.
        """
        if cancellation is not None:
            ctx.stop_requested = cancellation.is_stopped
        code_tools = self._catalog.code_tool_names()
        active_names = (
            None if active_tool_names is None else frozenset(str(name or "").strip() for name in active_tool_names)
        )
        # One frozen runtime-metadata snapshot per tool call, resolved up front and
        # threaded through execution, logging, truncation, and retrieval. Resolving here
        # rather than inside each call is what makes the parallel decision below and the
        # decisions inside the call agree on the same facts.
        per_call_metadata = [
            resolve_tool_runtime_metadata(str(tc.function.name or "").strip(), self._catalog, scope=ctx.scope)
            for tc in tool_calls
        ]
        can_parallel = len(tool_calls) > 1 and all(md.parallel_safe for md in per_call_metadata)

        def stopped() -> bool:
            return cancellation is not None and cancellation.is_stopped()

        async def _call(ordinal: int, tc: ToolCall, metadata: ResolvedToolRuntimeMetadata) -> dict[str, Any] | None:
            if stopped():
                return None
            return await self._execute_with_timeout(
                tc,
                ordinal=ordinal,
                ctx=ctx,
                timeout_sec=self._get_tool_timeout(metadata),
                task_id=task_id,
                active_tool_names=active_names,
                metadata=metadata,
                cancellation=cancellation,
                decisions=decisions,
            )

        if not can_parallel:
            sequential_results: list[dict[str, Any]] = []
            for ordinal, (tc, metadata) in enumerate(zip(tool_calls, per_call_metadata, strict=True)):
                result = await _call(ordinal, tc, metadata)
                if result is not None:
                    sequential_results.append(result)
                if stopped():
                    break
            return sequential_results

        # A round's allowance is a share of the process's, not an allowance of its own: with
        # several runs, a per-round ceiling multiplied by the slots — plus bodies abandoned at
        # their deadline, which keep their threads for the grace period — is not a bound. The
        # semaphore still shapes the round; the budget below is what the process is held to.
        semaphore = asyncio.Semaphore(min(len(tool_calls), _ROUND_TOOL_BODY_ALLOWANCE))
        budgets = concurrency_budgets.budgets

        async def _bounded(ordinal: int, tc: ToolCall, metadata: ResolvedToolRuntimeMetadata) -> dict[str, Any] | None:
            async with semaphore:
                while True:
                    if stopped():
                        return None
                    lease = await asyncio.to_thread(
                        budgets.acquire,
                        TOOL_BODIES,
                        holder=f"tool-{task_id}-{tc.id}",
                        ttl_sec=_TOOL_BODY_LEASE_TTL_SEC,
                        timeout_sec=_TOOL_BUDGET_WAIT_SLICE_SEC,
                    )
                    if lease is not None:
                        break
                abandoned = False
                try:
                    if stopped():
                        return None
                    result = await _call(ordinal, tc, metadata)
                    if result is None:
                        return None
                    # A body abandoned at its deadline keeps its thread, so its slot is not
                    # free when the round moves on. Its lease is left to expire instead of
                    # released: crediting the budget here would describe a bound that does
                    # not hold. The flag rides on the result so it names this call, not a
                    # provider id a later call may repeat.
                    abandoned = bool(result.get(_BODY_ABANDONED_KEY))
                    return result
                finally:
                    # The release belongs here, not on the success path: `_call` raises for
                    # everything `_execute_single_tool` does not turn into a result dict —
                    # authorization, the trace span, the journal — and the gather below
                    # already handles that. A grant leaked there holds a share of the
                    # process's tool-body budget for its whole TTL, and the next round's
                    # acquire has no deadline to give up on.
                    if lease is not None and not abandoned:
                        budgets.release(lease)

        gathered = await asyncio.gather(
            *(
                _bounded(ordinal, tc, md)
                for ordinal, (tc, md) in enumerate(zip(tool_calls, per_call_metadata, strict=True))
            ),
            return_exceptions=True,
        )

        results: list[dict[str, Any]] = []
        for tc, md, outcome in zip(tool_calls, per_call_metadata, gathered, strict=True):
            if isinstance(outcome, BaseException):
                # ``_execute_single_tool`` already turns a failing tool into a result dict, so
                # reaching here means something unexpected above it. One call must not lose the
                # whole round's results, and its position in the list has to hold.
                if not isinstance(outcome, Exception):
                    raise outcome
                fn_name = str(tc.function.name or "").strip()
                text = f"⚠️ TOOL_ERROR: Unexpected error: {outcome}"
                results.append(
                    {
                        "tool_call_id": tc.id,
                        "fn_name": fn_name,
                        "result": text,
                        "is_error": True,
                        "tool_args": None,
                        "args_for_log": {},
                        "is_code_tool": fn_name in code_tools,
                        "result_meta": self._extract_result_metadata(fn_name, text, True),
                        # Pinned here too: this dict reaches `process_results` like any
                        # other, and without the snapshot the call would silently fall back to
                        # the core result cap on the one path that failed unexpectedly.
                        "tool_metadata": md,
                    }
                )
            elif outcome is not None:
                results.append(outcome)
        return results

    def execute(
        self,
        tool_calls: list[ToolCall],
        *,
        ctx: ToolContext,
        task_id: str,
        active_tool_names: Iterable[str] | None = None,
        decisions: Mapping[ConfirmationKey, str],
        cancellation: CancellationSignal | None = None,
    ) -> list[dict[str, Any]]:
        """The synchronous bridge: one event loop per round, on the calling thread.

        Everything above this — the ``exec_tools`` node, the ``ToolRunner`` interface,
        ``new_core/loop`` — is still synchronous and becomes asynchronous in its own change.

        The loop is created on the *calling* thread rather than the coroutine being scheduled
        onto the process's main loop: tool bodies bridge back to that loop themselves
        (``MCPManager.call_tool_sync``) and would then be waiting on the loop they run on.

        This is the only crossing left on the production path, and it is reached once per
        round rather than once per tool call — the catalog's own bridge is gone.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # No loop on this thread — the bridge is the right call.
        else:
            raise RuntimeError(
                "ToolCallExecutor.execute is the synchronous bridge and cannot be called from "
                "a thread that already runs an event loop; await aexecute instead."
            )
        return asyncio.run(
            self.aexecute(
                tool_calls,
                ctx=ctx,
                task_id=task_id,
                active_tool_names=active_tool_names,
                decisions=decisions,
                cancellation=cancellation,
            )
        )

    def process_results(
        self,
        results: list[dict[str, Any]],
        *,
        messages: list[dict[str, Any]],
        trace: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> int:
        """Append tool results to ``messages`` / ``trace`` and return the error count.

        The second half of a round's tool work: ``results`` is what :meth:`aexecute`
        returned. Everything that records an outcome — the repeat guard, the ledger, the
        transcript, the trace — happens here, so a caller that only wants the calls run can
        stop after the first half.
        """
        error_count = 0

        for exec_result in results:
            fn_name = exec_result["fn_name"]
            is_error = exec_result["is_error"]
            result = exec_result["result"]
            result_text = result.text if isinstance(result, ToolResult) else str(result)
            exec_result = {**exec_result, "result": result_text}
            if (
                fn_name == "skill_read"
                and not is_error
                and ctx is not None
                and isinstance(result, ToolResult)
                and result.skill_page is not None
            ):
                result_text = retain_skill_page(ctx, result.skill_page)
                is_error = self._is_tool_execution_failure(tool_ok=True, result=result_text, tool_name=fn_name)
                if is_error:
                    exec_result = {
                        **exec_result,
                        "is_error": True,
                        "result": result_text,
                        "result_meta": self._extract_result_metadata(fn_name, result_text, True),
                    }
            _raw_tool_args = exec_result.get("tool_args")
            tool_args: dict[str, Any] = _raw_tool_args if isinstance(_raw_tool_args, dict) else {}
            args_hash = safe_args_hash(_raw_tool_args) if isinstance(_raw_tool_args, dict) else ""
            result_meta = exec_result.get("result_meta") or {}
            self._invocations.record(ctx, exec_result)

            if is_error:
                error_count += 1
                if self._repeat_guard is not None and result_meta.get("status") != "repeat_blocked":
                    guard_note = self._repeat_guard.record_failure(
                        fn_name,
                        tool_args,
                        self._failure_class_for_result(exec_result),
                    )
                    if guard_note:
                        result_text = f"{result_text}\n\n{guard_note}"
            elif self._repeat_guard is not None and result_meta.get("status") not in _CALL_NOT_MADE:
                # Clearing the streak here would forgive earlier failures on a call that never ran.
                self._repeat_guard.mark_success(fn_name, tool_args)

            if self._task_ledger is not None:
                self._task_ledger.record_tool_result(
                    tool_name=fn_name,
                    args_hash=args_hash,
                    status=str(result_meta.get("status") or ("error" if is_error else "ok")),
                    failure_class=self._failure_class_for_result(exec_result) if is_error else "",
                    short_outcome=result_text,
                )
            if is_error and fn_name in {"data_write", "send_file", "send_image", "send_photo"}:
                raw_path = str(tool_args.get("path" if fn_name == "data_write" else "file_path") or "")
                path = pathlib.PurePosixPath(raw_path.replace("\\", "/"))
                scope = (
                    path.parts[0]
                    if path.parts and path.parts[0] in {"uploads", "skills", "state", "tmp"}
                    else "workspace"
                )
                filename = re.sub(r"[\x00-\x1f\x7f\s]+", "_", path.name)[:200] or "unknown"
                error_code = str(result_meta.get("error_code") or "TOOL_ERROR")
                stage = "transport_delivery" if "_DELIVERY_" in error_code else "pre_transport"
                log.warning(
                    "event=tool_outcome_failed task_id=%s tool=%s status=%s error_code=%s reason=%s stage=%s scope=%s filename=%s",
                    getattr(ctx, "task_id", "") or "unknown",
                    fn_name,
                    result_meta.get("status", "error"),
                    error_code,
                    error_code.lower(),
                    stage,
                    scope,
                    filename,
                )

            # Use the metadata snapshot pinned when the tool call was resolved; an
            # extension unloaded between aexecute() and here must not drop its
            # declared cap back to the core default. Helper-built error dicts
            # without the snapshot keep the built-in per-tool map/default.
            tool_metadata = exec_result.get("tool_metadata")
            limit_override = (
                tool_metadata.result_limit if isinstance(tool_metadata, ResolvedToolRuntimeMetadata) else None
            )
            truncated_result = self._truncate_tool_result(
                result_text,
                tool_name=fn_name,
                tool_args=tool_args,
                limit_override=limit_override,
            )

            messages.append({"role": "tool", "tool_call_id": exec_result["tool_call_id"], "content": truncated_result})

            trace["tool_calls"].append(
                {
                    "tool": fn_name,
                    "args": self._safe_args(exec_result["args_for_log"]),
                    **({"args_hash": args_hash} if args_hash else {}),
                    "result": self._tool_result_preview_for_log(fn_name, result_text, 700),
                    "is_error": is_error,
                    **result_meta,
                }
            )

        return error_count

    # --- private: one call's execution flow ------------------------------------------------

    def _emit_call_finished(
        self,
        *,
        task_id: str,
        tool: str,
        args: dict[str, Any],
        started_at: float,
        is_error: bool,
        result_text: Any = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "tool_call_finished",
            "task_id": task_id,
            "tool": tool,
            "args": args,
            "duration_sec": round(time.perf_counter() - started_at, 3),
            "is_error": is_error,
            **extra,
        }
        if result_text is not None:
            payload["result_preview"] = self._tool_result_preview_for_log(tool, result_text, 500)
        self._events.emit_tool_live_log(payload)

    def _preflight(
        self,
        tc: ToolCall,
        *,
        ctx: ToolContext,
        active_tool_names: frozenset[str] | None,
        metadata: ResolvedToolRuntimeMetadata,
    ) -> _Preflight:
        fn_name = str(tc.function.name or "").strip()
        is_code_tool = fn_name in self._catalog.code_tool_names()
        args: dict[str, Any] | None = None
        args_for_log: dict[str, Any] = {}
        arg_error: Exception | None = None
        try:
            decoded = json.loads(self._normalize_tool_call_arguments_for_history(tc))
            if not isinstance(decoded, dict):
                raise TypeError("tool arguments must decode to an object")
            args_for_log = sanitize_tool_args_for_log(fn_name, decoded, redact_keys=metadata.log_redacted_args)
            args = decoded
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            arg_error = exc

        if (owner_refusal := skill_tool_refusal(fn_name, metadata.origin, ctx.skill_origins)) is not None:
            refusal = {
                "tool_call_id": tc.id,
                "fn_name": fn_name,
                "result": owner_refusal,
                "is_error": True,
                "tool_args": args,
                "args_for_log": args_for_log,
                "is_code_tool": is_code_tool,
                "result_meta": self._extract_result_metadata(fn_name, owner_refusal, True),
            }
        elif self._tool_inactive_for_task(fn_name, active_tool_names, ctx.scope):
            refusal = self._make_tool_not_enabled_result(fn_name, tc.id, is_code_tool, args, args_for_log)
        elif arg_error is not None:
            refusal = self._make_tool_arg_error_result(tc.function.name, fn_name, tc.id, is_code_tool, arg_error)
        elif (
            args is not None
            and self._repeat_guard is not None
            and (blocked := self._repeat_guard.preflight(fn_name, args))
        ):
            refusal = self._make_tool_repeat_blocked_result(fn_name, tc.id, is_code_tool, args, args_for_log, blocked)
        else:
            decision = authorize_tool(
                fn_name,
                capability_class=metadata.capability_class,
                origin=metadata.origin,
                confirmation_gate=self._confirmation_gate,
            )
            return _Preflight(fn_name, args, args_for_log, is_code_tool, decision)
        return _Preflight(fn_name, args, args_for_log, is_code_tool, refusal)

    async def _execute_with_timeout(
        self,
        tc: ToolCall,
        *,
        ordinal: int,
        ctx: ToolContext,
        timeout_sec: int,
        task_id: str,
        active_tool_names: frozenset[str] | None,
        metadata: ResolvedToolRuntimeMetadata,
        decisions: Mapping[ConfirmationKey, str],
        cancellation: CancellationSignal | None = None,
    ) -> dict[str, Any] | None:
        """Execute one tool call with timeout handling.

        ``metadata`` is the round's frozen snapshot for this call.

        The deadline is ``asyncio.wait`` over an explicit task rather than
        ``asyncio.wait_for``, on both paths. The reviewed-mutative one needs it: after the
        soft timeout it keeps waiting on *the same* call up to the hard ceiling, so a slow
        commit still returns a real result instead of an ambiguous timeout, and ``wait_for``
        cancels what it awaits.

        The plain path uses it too, for one primitive rather than two and because
        ``wait_for`` does not just cancel — it *awaits* the cancellation before raising. A
        body that delays its own cancellation would delay the round with it, which is the one
        thing abandonment exists to rule out. ``asyncio.wait`` plus :meth:`_abandon` never
        waits: the cancel is fired and the round moves on, whatever the body goes on to do.
        """
        if cancellation is not None and cancellation.is_stopped():
            return None
        started_at = time.perf_counter()
        preflight = self._preflight(tc, ctx=ctx, active_tool_names=active_tool_names, metadata=metadata)
        fn_name, parsed_args, args_for_log = preflight.fn_name, preflight.args, preflight.args_for_log
        tool_call_id, is_code_tool = tc.id, preflight.is_code_tool
        self._events.emit_tool_live_log(
            {
                "type": "tool_call_started",
                "task_id": task_id,
                "tool": fn_name,
                "timeout_sec": timeout_sec,
                "args": args_for_log,
            }
        )
        if not isinstance(preflight.decision, PolicyDecision):
            refusal = preflight.decision
            result_meta = refusal.get("result_meta") or {}
            self._emit_call_finished(
                task_id=task_id,
                tool=fn_name,
                args=args_for_log,
                started_at=started_at,
                is_error=True,
                result_text=refusal["result"],
                status=result_meta.get("status"),
                exit_code=result_meta.get("exit_code"),
                signal=result_meta.get("signal"),
            )
            return refusal

        decision = preflight.decision
        verdict = decisions.get((ordinal, fn_name, safe_args_hash(parsed_args)))
        if decision.outcome == "confirm_pending" or (decision.outcome == "allow" and verdict in _CALL_NOT_MADE):
            verdict = verdict or CONFIRMATION_NOT_ASKED
            log.info("event=tool_confirmation_applied task_id=%s tool=%s decision=%s", task_id, fn_name, verdict)
            if verdict == CONFIRMATION_APPROVED:
                decision = dataclasses.replace(decision, outcome="allow")
                # However long the owner took is theirs, not the tool's.
                started_at = time.perf_counter()
            else:
                result = self._make_confirmation_result(
                    fn_name, tool_call_id, is_code_tool, parsed_args, args_for_log, verdict
                )
                self._emit_call_finished(
                    task_id=task_id,
                    tool=fn_name,
                    args=args_for_log,
                    started_at=started_at,
                    is_error=False,
                    result_text=result["result"],
                    status=verdict,
                )
                return result

        if cancellation is not None and cancellation.is_stopped():
            return None
        call = asyncio.ensure_future(
            self._execute_single_tool(
                ctx,
                tc,
                task_id,
                metadata=metadata,
                decision=decision,
                cancellation=cancellation,
            )
        )
        done, _pending = await asyncio.wait({call}, timeout=timeout_sec)
        if done:
            call_result = call.result()
            if call_result is None:
                return None
            result = call_result
            result_meta = result.get("result_meta") or {}
            self._emit_call_finished(
                task_id=task_id,
                tool=fn_name,
                args=result.get("args_for_log", args_for_log),
                started_at=started_at,
                is_error=bool(result.get("is_error")),
                result_text=result.get("result", ""),
                status=result_meta.get("status"),
                exit_code=result_meta.get("exit_code"),
                signal=result_meta.get("signal"),
            )
            return result

        is_reviewed_mutative = fn_name in REVIEWED_MUTATIVE_TOOLS

        if is_reviewed_mutative:
            # Commit/review tools must not end with ambiguous timeout.
            self._review_gate.mark_attempt_late(
                ctx,
                soft_timeout_sec=timeout_sec,
                duration_sec=round(time.perf_counter() - started_at, 1),
            )
            self._events.emit_tool_live_log(
                {
                    "type": "tool_call_late",
                    "task_id": task_id,
                    "tool": fn_name,
                    "args": args_for_log,
                    "soft_timeout_sec": timeout_sec,
                    "message": (
                        f"Reviewed mutative tool '{fn_name}' exceeded "
                        f"{timeout_sec}s — still waiting for result "
                        f"(hard ceiling: {_REVIEWED_MUTATIVE_HARD_CEILING}s)"
                    ),
                }
            )
            ceiling = max(_REVIEWED_MUTATIVE_HARD_CEILING, timeout_sec + 60)
            remaining = max(1, ceiling - timeout_sec)
            done, _pending = await asyncio.wait({call}, timeout=remaining)
            if done:
                late_result = call.result()
                if late_result is None:
                    return None
                result = late_result
                result_meta = result.get("result_meta") or {}
                self._emit_call_finished(
                    task_id=task_id,
                    tool=fn_name,
                    args=result.get("args_for_log", args_for_log),
                    started_at=started_at,
                    is_error=bool(result.get("is_error")),
                    status=result_meta.get("status"),
                    late=True,
                )
                return result

            # Hard ceiling records terminal state; late real result may overwrite.
            self._abandon(call, task_id=task_id, tool=fn_name)
            self._review_gate.record_hard_ceiling(
                ctx,
                duration_sec=round(time.perf_counter() - started_at, 1),
                hard_ceiling_sec=_REVIEWED_MUTATIVE_HARD_CEILING,
            )
            timeout_result = self._make_timeout_result(
                fn_name,
                tool_call_id,
                is_code_tool,
                tc,
                _REVIEWED_MUTATIVE_HARD_CEILING,
                reset_msg=(
                    f"CRITICAL: Reviewed mutative tool hit hard ceiling "
                    f"({_REVIEWED_MUTATIVE_HARD_CEILING}s). "
                    "Verify whether the operation actually completed before retrying. "
                ),
                metadata=metadata,
            )
            self._events.emit_tool_live_log(
                {
                    "type": "tool_call_timeout",
                    "task_id": task_id,
                    "tool": fn_name,
                    "args": args_for_log,
                    "duration_sec": round(time.perf_counter() - started_at, 3),
                    "timeout_sec": _REVIEWED_MUTATIVE_HARD_CEILING,
                    "hard_ceiling": True,
                }
            )
            timeout_result[_BODY_ABANDONED_KEY] = True
            return timeout_result

        self._abandon(call, task_id=task_id, tool=fn_name)
        timeout_result = self._make_timeout_result(
            fn_name, tool_call_id, is_code_tool, tc, timeout_sec, reset_msg="", metadata=metadata
        )
        timeout_result[_BODY_ABANDONED_KEY] = True
        self._events.emit_tool_live_log(
            {
                "type": "tool_call_timeout",
                "task_id": task_id,
                "tool": fn_name,
                "args": args_for_log,
                "duration_sec": round(time.perf_counter() - started_at, 3),
                "timeout_sec": timeout_sec,
            }
        )
        return timeout_result

    def gated_calls(
        self,
        tool_calls: list[ToolCall],
        *,
        ctx: ToolContext,
        active_tool_names: Iterable[str] | None,
    ) -> list[GatedCall]:
        """Which of this round's calls the owner has to be asked about, in call order.

        Runs the round's own preflight, so no question is asked about a call it would refuse anyway.
        """
        active = None if active_tool_names is None else frozenset(str(name or "").strip() for name in active_tool_names)
        gated: list[GatedCall] = []
        for ordinal, tc in enumerate(tool_calls):
            metadata = resolve_tool_runtime_metadata(
                str(tc.function.name or "").strip(), self._catalog, scope=ctx.scope
            )
            preflight = self._preflight(tc, ctx=ctx, active_tool_names=active, metadata=metadata)
            decision = preflight.decision
            if isinstance(decision, PolicyDecision) and decision.outcome == "confirm_pending":
                gated.append(
                    GatedCall(
                        ordinal=ordinal,
                        tool=preflight.fn_name,
                        args_for_log=preflight.args_for_log,
                        args_hash=safe_args_hash(preflight.args),
                    )
                )
        return gated

    @staticmethod
    def _make_confirmation_result(
        fn_name: str,
        tool_call_id: str,
        is_code_tool: bool,
        tool_args: dict[str, Any] | None,
        args_for_log: dict[str, Any],
        verdict: str,
    ) -> dict[str, Any]:
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": _CONFIRMATION_MESSAGES[verdict].format(tool=fn_name),
            # Nothing failed: an error here would spend the repeat guard's streak on the owner.
            "is_error": False,
            "tool_args": tool_args if isinstance(tool_args, dict) else {},
            "args_for_log": args_for_log,
            "is_code_tool": is_code_tool,
            "result_meta": {"status": verdict},
        }

    async def _execute_single_tool(
        self,
        ctx: ToolContext,
        tc: ToolCall,
        task_id: str,
        metadata: ResolvedToolRuntimeMetadata,
        decision: PolicyDecision,
        cancellation: CancellationSignal | None = None,
    ) -> dict[str, Any] | None:
        """Execute a single tool call and return all needed info.

        ``metadata`` is the frozen runtime-metadata snapshot taken when the tool
        call was resolved. It is pinned into the result (``tool_metadata``) so
        logging and truncation later cannot re-resolve against an unloaded
        extension.

        ``decision`` still reading ``confirm_pending`` means the caller reached here without a
        verdict, so the body does not run.

        Returns dict with: tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
        """
        requested_fn_name = tc.function.name
        fn_name = str(requested_fn_name or "").strip()
        tool_call_id = tc.id
        is_code_tool = fn_name in self._catalog.code_tool_names()

        try:
            args = json.loads(self._normalize_tool_call_arguments_for_history(tc))
            if not isinstance(args, dict):
                raise TypeError("tool arguments must decode to an object")
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return self._make_tool_arg_error_result(requested_fn_name, fn_name, tool_call_id, is_code_tool, e)

        args_for_log = sanitize_tool_args_for_log(
            fn_name,
            args,
            redact_keys=metadata.log_redacted_args,
        )

        result: str | ToolResult
        if decision.outcome == "confirm_pending":
            return self._make_confirmation_result(
                fn_name, tool_call_id, is_code_tool, args, args_for_log, CONFIRMATION_NOT_ASKED
            )
        if decision.outcome == "deny":
            result = f"⚠️ POLICY_DENIED ({fn_name}): {decision.deny_reason}"
            self._journal.record(
                "events",
                "policy.tool_denied",
                build_policy_tool_denied_event(
                    tool=fn_name,
                    capability_class=decision.capability_class,
                    reason=str(decision.deny_reason or ""),
                ),
            )
            return {
                "tool_call_id": tool_call_id,
                "fn_name": fn_name,
                "result": result,
                "is_error": True,
                "tool_args": args,
                "args_for_log": args_for_log,
                "is_code_tool": is_code_tool,
                "result_meta": self._extract_result_metadata(fn_name, result, True),
                "tool_metadata": metadata,
            }

        tool_ok = True
        if cancellation is not None and cancellation.is_stopped():
            return None
        with self._tracer.span(tool=fn_name, args=args_for_log, task_id=task_id) as tool_span:
            call_ctx = replace(ctx, tool_call_id=tc.id)
            pending_compaction = ctx._pending_compaction
            workstation = ctx.workstation
            try:
                if cancellation is not None and cancellation.is_stopped():
                    return None
                result = await execute_tool(self._catalog, call_ctx, fn_name, args, origin=metadata.origin)
            except Exception as e:
                tool_ok = False
                result = f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}"
                self._journal.record(
                    "events",
                    "tool_error",
                    {
                        "ts": utc_now_iso(),
                        "task_id": task_id,
                        "tool": fn_name,
                        "args": args_for_log,
                        "error": repr(e),
                    },
                )
            finally:
                if call_ctx._pending_compaction != pending_compaction:
                    ctx._pending_compaction = call_ctx._pending_compaction
                if call_ctx.workstation is not workstation:
                    ctx.workstation = call_ctx.workstation
            result_text = result.text if isinstance(result, ToolResult) else result
            is_error = self._is_tool_execution_failure(tool_ok, result_text, tool_name=fn_name)
            self._tracer.add_result(tool_span, result=result_text, is_error=is_error)

        self._journal.record(
            "tools",
            "tool_call",
            {
                "ts": utc_now_iso(),
                "tool": fn_name,
                "task_id": task_id,
            },
        )

        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": is_error,
            "tool_args": args,
            "args_for_log": args_for_log,
            "is_code_tool": is_code_tool,
            "result_meta": self._extract_result_metadata(fn_name, result_text, is_error),
            "tool_metadata": metadata,
        }

    def _make_timeout_result(
        self,
        fn_name: str,
        tool_call_id: str,
        is_code_tool: bool,
        tc: ToolCall,
        timeout_sec: int,
        reset_msg: str = "",
        metadata: ResolvedToolRuntimeMetadata | None = None,
    ) -> dict[str, Any]:
        """Create and log a timeout result."""
        args: Any = {}
        args_for_log = {}
        try:
            args = json.loads(tc.function.arguments or "{}")
            args_for_log = sanitize_tool_args_for_log(
                fn_name,
                args if isinstance(args, dict) else {},
                redact_keys=metadata.log_redacted_args if metadata is not None else frozenset(),
            )
        except Exception:
            pass

        delivery_error_code = _MEDIA_TIMEOUT_ERROR_CODES.get(fn_name)
        if delivery_error_code:
            result = (
                f"⚠️ {delivery_error_code}: exceeded {timeout_sec}s limit. "
                "The send is still running in background and may complete; do not retry automatically."
            )
        else:
            result = (
                f"⚠️ TOOL_TIMEOUT ({fn_name}): exceeded {timeout_sec}s limit. "
                f"The tool is still running in background but control is returned to you. "
                f"{reset_msg}Try a different approach or inform the user{' about the issue' if not reset_msg else ''}."
            )

        self._journal.record(
            "events",
            "tool_timeout",
            {
                "ts": utc_now_iso(),
                "tool": fn_name,
                "args": args_for_log,
                "timeout_sec": timeout_sec,
                **({"error_code": delivery_error_code, "delivery_status": "unknown"} if delivery_error_code else {}),
            },
        )
        self._journal.record(
            "tools",
            "tool_call",
            {
                "ts": utc_now_iso(),
                "tool": fn_name,
            },
        )

        result_meta = self._extract_result_metadata(fn_name, result, True)
        if delivery_error_code:
            result_meta.update(
                status="timeout",
                failure_class=delivery_error_code,
                delivery_status="unknown",
            )

        timeout_result: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "tool_args": args if isinstance(args, dict) else {},
            "args_for_log": args_for_log,
            "is_code_tool": is_code_tool,
            "result_meta": result_meta,
        }
        if metadata is not None:
            timeout_result["tool_metadata"] = metadata
        return timeout_result

    @staticmethod
    def _get_tool_timeout(metadata: ResolvedToolRuntimeMetadata) -> int:
        """Return max(config timeout, the snapshot's per-tool minimum).

        Reads the pinned snapshot rather than the catalog: the deadline a call runs under must
        be the same fact the rest of its execution was resolved against, not a second lookup
        that an unload in between could answer differently.
        """
        # Call-time import: settings are resolved through the store module on every call,
        # so a live settings change (or a test patching the store) is picked up.
        from ouroboros.storage.settings.store import load_settings

        settings_val = 0
        try:
            settings_val = int(load_settings().OUROBOROS_TOOL_TIMEOUT_SEC or 0)
        except Exception:
            pass
        per_tool = metadata.timeout_sec
        return max(settings_val, per_tool) if settings_val > 0 else per_tool

    def _tool_inactive_for_task(
        self,
        fn_name: str,
        active_tool_names: frozenset[str] | None,
        scope: OwnerScope,
    ) -> bool:
        if active_tool_names is None:
            return False
        name = str(fn_name or "").strip()
        if name in active_tool_names:
            return False
        spec = TOOL_SPECS.get(name)
        if spec is not None:
            return spec.surface == "discoverable"
        # Not a repo-defined tool: one catalog lookup answers for extension, MCP and remote
        # A2A alike. Restricted to those origins on purpose — it stands in for the old
        # `_extension_tool_exists` / `_mcp_tool_exists` pair, with A2A named because it too
        # reaches outside the process. A `type: tool` skill was never gated by the active set
        # either, and widening that here would be a policy change riding along on a refactor.
        desc = self._catalog.find(name, scope)
        if desc is None:
            return False
        # `find` resolves provider aliases, so `myskill.ping` lands on `ext_5_myskill_ping`.
        # The active set holds canonical names, and the old code let the alias form through
        # entirely (it bailed before the lookup); comparing the resolved name keeps the alias
        # usable for an active tool without also re-opening the gate for an inactive one.
        if desc.name in active_tool_names:
            return False
        return desc.origin.startswith(("ext:", "mcp:", "a2a:"))

    # --- private: pure helpers --------------------------------------------------------------

    @staticmethod
    def _abandon(task: asyncio.Task[Any], *, task_id: str = "", tool: str = "") -> None:
        """Stop waiting on a body that outlived its deadline; keep its late outcome attributed.

        Cancellation reaches the body only at a suspension point, so a blocking body on a thread
        placement runs to completion regardless — which is exactly the abandonment the pool's
        ``shutdown(wait=False, cancel_futures=True)`` performed here before. What ``cancel``
        settles is this side: the round stops waiting.

        The done-callback consumes whatever the task ends with. Without it, a body that fails
        after the round moved on logs "Task exception was never retrieved" at interpreter
        shutdown — noise with no call site attached, which reads like a new defect. A cancelled
        task carries nothing to retrieve. A task that ends with a real exception, though, is an
        abandoned body that failed later: retrieving it silently would drop that fact, so it is
        logged once with the call it belongs to instead.

        This sees only the rare placement whose *task itself* ends with an exception — e.g. a
        ``BaseException`` the provider's own ``except Exception`` never turned into a result. The
        common case — a blocking thread body finishing or failing after the round moved on — ends
        this task ``CancelledError`` and settles its outcome in its executor's future instead;
        :class:`ThreadToolRuntime` logs that one where it lands.
        """
        task.cancel()

        def _drain(t: asyncio.Task[Any]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.warning(
                    "Abandoned tool body failed after its deadline (task_id=%s tool=%s): %r",
                    task_id,
                    tool,
                    exc,
                )

        task.add_done_callback(_drain)

    @staticmethod
    def _path_is_cognitive_artifact(tool_name: str, tool_args: dict[str, Any] | None) -> bool:
        """Return whether a read target must stay whole."""
        if not tool_args:
            return False

        raw_path = str(tool_args.get("path") or "").strip()
        if not raw_path:
            return False

        normalized = raw_path.replace("\\", "/").lstrip("./")

        if tool_name == "data_read":
            return normalized.startswith("memory/") and "/_backup/" not in normalized

        return False

    @staticmethod
    def _should_skip_tool_result_truncation(
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
    ) -> bool:
        """Canonical/cognitive reads must remain whole."""
        return tool_name in _UNTRUNCATED_TOOL_RESULTS or ToolCallExecutor._path_is_cognitive_artifact(
            tool_name, tool_args
        )

    @staticmethod
    def _truncate_tool_result(
        result: Any,
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
        limit_override: int | None = None,
    ) -> str:
        """Cap tool result unless it is an untruncated artifact.

        ``limit_override`` (e.g. a metadata-resolved ``result_limit``) wins over
        the built-in per-tool map/default when set.
        """
        limit = limit_override
        if limit is None:
            limit = _TOOL_RESULT_LIMITS.get(tool_name, _DEFAULT_TOOL_RESULT_LIMIT)
        s = str(result)
        if ToolCallExecutor._should_skip_tool_result_truncation(tool_name, tool_args):
            return s
        if len(s) <= limit:
            return s
        return s[:limit] + f"\n... (truncated from {len(s)} chars, limit={limit})"

    @staticmethod
    def _tool_result_preview_for_log(tool_name: str, result: Any, max_chars: int) -> str:
        del tool_name
        if isinstance(result, ToolResult):
            result = result.text
        return sanitize_tool_result_for_log(truncate_for_log(str(result or ""), max_chars))

    @staticmethod
    def _is_tool_execution_failure(tool_ok: bool, result: Any, tool_name: str = "") -> bool:
        """Treat only executor/runtime failures as UI tool failures."""
        if not tool_ok:
            return True
        text = str(result or "")
        if text.startswith("⚠️ SHELL_REGEX_AUTO_CORRECTED") and "⚠️ SHELL_EXIT_ERROR" not in text:
            return False
        file_failure = (not tool_name or tool_name in {"data_write", "send_file", "send_image", "send_photo"}) and any(
            line.startswith(_FILE_FAILURE_PREFIXES) for line in text.splitlines()
        )
        return (
            text.startswith(_FAILURE_PREFIXES)
            or file_failure
            or any(
                marker in text
                for marker in (
                    "\n⚠️ TOOL_",
                    "\n---\n⚠️ TOOL_",
                    "⚠️ TOOL_ERROR",
                    "⚠️ TOOL_TIMEOUT",
                )
            )
        )

    @staticmethod
    def _extract_result_metadata(fn_name: str, result: Any, is_error: bool) -> dict[str, Any]:
        """Extract structured outcome facts for summaries and reflections."""
        text = str(result or "")
        status = "error" if is_error else "ok"
        if text.startswith("⚠️ TOOL_TIMEOUT"):
            status = "timeout"
        elif text.startswith("⚠️ SHELL_REGEX_AUTO_CORRECTED") and "⚠️ SHELL_EXIT_ERROR" not in text:
            status = "ok_autocorrected"
        elif text.startswith("⚠️ SHELL_EXIT_ERROR"):
            status = "non_zero_exit"
        elif text.startswith("⚠️ SHELL_"):
            status = "shell_error"
        elif text.startswith("⚠️ MCP_TOOL_ERROR"):
            # Both timeout shapes: manager's cross-thread wait ("timed out after Ns")
            # and the SDK's read timeout exception text.
            status = "timeout" if "timed out" in text.lower() else "error"
        elif text.startswith("⚠️ A2A_SUBMISSION_UNKNOWN"):
            # Not "timeout": that status reads as "nothing happened, try again", and here the
            # remote agent may be running the work already.
            status = "delivery_unknown"
        elif text.startswith("⚠️ A2A_TIMEOUT"):
            status = "timeout"
        elif text.startswith("⚠️ A2A_ERROR"):
            status = "error"

        meta: dict[str, Any] = {"status": status}
        code_match = next(filter(None, map(_TOOL_ERROR_CODE_RE.match, text.splitlines())), None)
        if is_error and code_match:
            meta["error_code"] = code_match.group(1)
        exit_match = _EXIT_CODE_RE.search(text)
        if exit_match:
            try:
                meta["exit_code"] = int(exit_match.group(1))
            except ValueError:
                pass
        signal_match = _SIGNAL_RE.search(text)
        if signal_match:
            meta["signal"] = signal_match.group(1)
        if fn_name == "run_shell" and not is_error and meta.get("exit_code") == 0:
            if status == "ok_autocorrected":
                meta["status"] = "ok_autocorrected"
            else:
                meta["status"] = "ok"
        return meta

    @staticmethod
    def _normalize_tool_call_arguments_for_history(tc: ToolCall) -> str:
        raw_args = tc.function.arguments
        if raw_args is None:
            return "{}"
        if isinstance(raw_args, str):
            if not raw_args.strip():
                return "{}"
            try:
                json.loads(raw_args)
            except (json.JSONDecodeError, ValueError):
                raise
            return raw_args

        normalized = json.dumps(raw_args, ensure_ascii=False, default=str)
        return normalized

    @staticmethod
    def _make_tool_arg_error_result(
        requested_fn_name: Any,
        fn_name: str,
        tool_call_id: str,
        is_code_tool: bool,
        exc: Exception,
    ) -> dict[str, Any]:
        result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{requested_fn_name}': {exc}"
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "tool_args": None,
            "args_for_log": {},
            "is_code_tool": is_code_tool,
            "result_meta": ToolCallExecutor._extract_result_metadata(fn_name, result, True),
        }

    @staticmethod
    def _make_tool_not_enabled_result(
        fn_name: str,
        tool_call_id: str,
        is_code_tool: bool,
        tool_args: dict[str, Any] | None,
        args_for_log: dict[str, Any],
    ) -> dict[str, Any]:
        result = (
            f"⚠️ TOOL_NOT_ENABLED: tool {fn_name} is not active for this task. "
            f"Call enable_tools(tools='{fn_name}') first."
        )
        result_meta = ToolCallExecutor._extract_result_metadata(fn_name, result, True)
        result_meta["failure_class"] = "TOOL_NOT_ENABLED"
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "tool_args": tool_args,
            "args_for_log": args_for_log,
            "is_code_tool": is_code_tool,
            "result_meta": result_meta,
        }

    @staticmethod
    def _make_tool_repeat_blocked_result(
        fn_name: str,
        tool_call_id: str,
        is_code_tool: bool,
        tool_args: dict[str, Any],
        args_for_log: dict[str, Any],
        message: str,
    ) -> dict[str, Any]:
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": message,
            "is_error": True,
            "tool_args": tool_args,
            "args_for_log": args_for_log,
            "is_code_tool": is_code_tool,
            "result_meta": {"status": "repeat_blocked", "failure_class": "repeat_blocked"},
        }

    @staticmethod
    def _failure_class_for_result(exec_result: dict[str, Any]) -> str:
        meta = exec_result.get("result_meta") or {}
        error_code = str(meta.get("error_code") or "").strip()
        if error_code in _DELIVERY_UNKNOWN_ERROR_CODES:
            return error_code
        for key in ("failure_class", "status", "error_code"):
            value = str(meta.get(key) or "").strip()
            if value:
                return value
        text = str(exec_result.get("result") or "").strip()
        if text.startswith("⚠️"):
            first_line = text.splitlines()[0]
            return first_line[:160]
        return "tool_error"

    @staticmethod
    def _safe_args(v: Any) -> Any:
        """Ensure args are JSON-serializable for trace logging."""
        try:
            return json.loads(json.dumps(v, ensure_ascii=False, default=str))
        except Exception:
            log.debug("Failed to serialize args for trace logging", exc_info=True)
            return {"_repr": repr(v)}
