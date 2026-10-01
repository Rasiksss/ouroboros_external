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

import json
import logging
import pathlib
import queue
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from new_core.llm.types import LLMMessage, ToolCall
from new_core.loop.interfaces import (
    EventSink,
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
from ouroboros.agent.context import build_user_content
from ouroboros.agent.context_compaction import (
    _COMPACTION_PROTECTED_TOOLS,
    _find_tool_name_for_result,
    compact_tool_history_llm,
)
from ouroboros.agent.loop_llm_call import call_llm_with_retry
from ouroboros.agent.loop_tool_execution import (
    EventRecorder,
    InvocationRecorder,
    ReviewGateSink,
    ToolCallExecutor,
    ToolTracer,
)
from ouroboros.agent.response_language_guard import (
    build_language_repair_messages,
    find_language_violation,
)
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger
from ouroboros.agent.tool_receipts import ToolResultReplacementState
from ouroboros.agent.working_retrieval import build_tool_invocation
from ouroboros.contracts.skill_payload_policy import SKILL_PAYLOAD_BUCKETS
from ouroboros.llm import LLMClient
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_record import LogStream, emit_worker_log_event, record_event
from ouroboros.observability.pricing import emit_llm_usage_event
from ouroboros.storage.runtime_store import get_runtime_store
from ouroboros.storage.settings.store import load_settings
from ouroboros.tools.runtime_metadata import ResolvedToolRuntimeMetadata
from ouroboros.utils import estimate_tokens, utc_now_iso

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext

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
        drive_logs: pathlib.Path,
        task_id: str,
        task_type: str,
        event_queue: queue.Queue | None,
        max_retries: int,
        fallback_model: str,
        fallback_use_local: bool,
    ) -> None:
        self._llm = llm
        self._drive_logs = drive_logs
        self._task_id = task_id
        self._task_type = task_type
        self._event_queue = event_queue
        self._max_retries = max_retries
        self._fallback_model = fallback_model
        self._fallback_use_local = fallback_use_local

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
    ) -> LLMMessage | None:
        return call_llm_with_retry(
            self._llm,
            messages,
            model,
            tools,
            effort,
            self._max_retries,
            self._drive_logs,
            self._task_id,
            round_idx,
            self._event_queue,
            accumulated_usage,
            self._task_type,
            use_local=use_local,
        )

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
        if find_language_violation(original, messages) is None:
            return original
        try:
            repaired_message = call_llm_with_retry(
                self._llm,
                build_language_repair_messages(original),
                model,
                None,
                effort,
                self._max_retries,
                self._drive_logs,
                self._task_id,
                round_idx,
                self._event_queue,
                accumulated_usage,
                self._task_type,
                use_local=use_local,
            )
        except Exception:
            log.debug("Final response language repair failed", exc_info=True)
            return original
        if not repaired_message or repaired_message.tool_calls:
            return original
        repaired = str(repaired_message.content or "")
        if not repaired.strip() or find_language_violation(repaired, messages) is not None:
            return original
        return repaired


class DriveEventRecorder(EventRecorder):
    """The on-disk journal, bound to this task's drive; the executor never sees the path."""

    def __init__(self, root: pathlib.Path) -> None:
        self._root = root

    def record(self, stream: LogStream, name: str, payload: dict[str, Any]) -> None:
        record_event(self._root, stream, name, payload)


class RuntimeStoreInvocationRecorder(InvocationRecorder):
    """Working-memory tool invocations, written to the runtime store on the task's drive."""

    def record(self, ctx: ToolContext | None, execution: dict[str, Any]) -> None:
        if ctx is None:
            return
        drive_root = ctx.drive_root
        task_id = str(ctx.task_id or "")
        if drive_root is None or not task_id:
            return
        try:
            store = get_runtime_store(drive_root)
            if store is None:
                return
            conversation_id = str(ctx.conversation_id or "")
            if not conversation_id:
                chat_id = ctx.current_chat_id
                if chat_id not in (None, ""):
                    conversation_id = f"chat:{chat_id}"
            settings = load_settings()
            metadata = execution.get("tool_metadata")
            retrieval_aliases: tuple[str, ...] = ()
            if isinstance(metadata, ResolvedToolRuntimeMetadata):
                # The stable name rides along so e.g. "navigator-bi.navigator_bi_search"
                # contributes navigator terms even without declared aliases.
                retrieval_aliases = (*metadata.retrieval_aliases, metadata.stable_name)
            invocation = build_tool_invocation(
                execution,
                task_id=task_id,
                conversation_id=conversation_id,
                retention_days=int(settings.OUROBOROS_TOOL_SUMMARY_RETENTION_DAYS or 30),
                raw_retention_days=int(settings.OUROBOROS_TOOL_RAW_RETENTION_DAYS or 0),
                retrieval_aliases=retrieval_aliases,
            )
            store.record_tool_invocation(invocation)
        except Exception:
            log.debug("Failed to record working-memory tool invocation", exc_info=True)


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


def build_tool_call_executor(
    *,
    catalog: ToolCatalog,
    drive_logs: pathlib.Path,
    events: EventSink,
    task_ledger: TaskLedger | None = None,
    repeat_guard: RepeatToolGuard | None = None,
) -> ToolCallExecutor:
    """The default-wired executor: production senders bound to this task's drive.

    ``events`` is the run's live-log sink — the same one the graph carries on its context,
    so tool live logs and the graph's own events go out one channel.
    """
    return ToolCallExecutor(
        catalog=catalog,
        journal=DriveEventRecorder(drive_logs.parent),
        invocations=RuntimeStoreInvocationRecorder(),
        tracer=AefToolTracer(),
        review_gate=CommitGateReviewSink(),
        events=events,
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
    ) -> None:
        self._drive_root = drive_root
        self._executor = executor

    def bind_surface(
        self,
        tool_ctx: ToolContext,
        messages: list[dict[str, Any]],
    ) -> tuple[list[Any], set[str]]:
        # Call-time import: `task_graph` imports this module, so a bound import would close
        # the cycle.
        from ouroboros.agent.task_graph import bind_tool_surface

        return bind_tool_surface(self._executor.catalog, tool_ctx, messages)

    def handle_tool_calls(
        self,
        *,
        tool_calls: list[ToolCall],
        active_tool_names: Iterable[str] | None,
        tool_ctx: ToolContext,
        task_id: str,
    ) -> list[Any]:
        # Tool live logs go out the graph's own sink: the executor is wired with it at
        # construction (see the callers), so the run's one channel stays one channel.
        return self._executor.execute(
            tool_calls,
            ctx=tool_ctx,
            task_id=task_id,
            active_tool_names=active_tool_names,
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

    def skill_finalization_message(self, trace: dict[str, Any]) -> str:
        if self._drive_root is None:
            return ""
        names = self._skill_names_touched_by_trace(trace)
        if not names:
            return ""
        try:
            from ouroboros.skills.loader import find_skill
            from ouroboros.skills.readiness import skill_readiness_for_execution
        except Exception:
            return ""
        blockers: list[str] = []
        for name in names:
            try:
                skill = find_skill(pathlib.Path(self._drive_root), name)
                if skill is None or not getattr(skill, "is_self_authored", False):
                    continue
                readiness = skill_readiness_for_execution(pathlib.Path(self._drive_root), skill)
                ready = readiness.ready
            except Exception:
                continue
            if not ready:
                blockers.append(f"{skill.name}: blockers={readiness.blockers}")
        if not blockers:
            return ""
        return (
            "⚠️ SKILL_NOT_FINALIZED: The skill files you just updated are not ready "
            "for use yet. Resolve the preflight/grants/enablement blockers before "
            "declaring the task done. Current blockers: " + "; ".join(blockers)
        )

    @staticmethod
    def _skill_names_touched_by_trace(llm_trace: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for call in llm_trace.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            tool = str(call.get("tool") or "")
            if tool not in {"data_write", "str_replace_editor"}:
                continue
            _args_raw = call.get("args")
            args = _args_raw if isinstance(_args_raw, dict) else {}
            bucket = str(args.get("bucket") or "").strip().lower()
            skill_name = str(args.get("skill_name") or "").strip()
            if bucket in SKILL_PAYLOAD_BUCKETS and skill_name:
                if skill_name not in names:
                    names.append(skill_name)
                continue
            candidates = [str(args.get("path") or "")]
            for raw in candidates:
                norm = raw.replace("\\", "/").strip().lstrip("/")
                if norm.startswith("data/"):
                    norm = norm[len("data/") :]
                parts = pathlib.PurePosixPath(norm).parts
                if len(parts) >= 3 and parts[0] == "skills" and parts[1] in SKILL_PAYLOAD_BUCKETS:
                    name = parts[2]
                    if name and name not in names:
                        names.append(name)
        return names


class TranscriptCompactorAdapter(TranscriptCompactor):
    """Transcript size estimation, compaction, ledger note, and cache seal."""

    def __init__(
        self,
        *,
        drive_root: pathlib.Path | None,
        task_id: str,
        task_ledger: TaskLedger,
    ) -> None:
        self._drive_root = drive_root
        self._task_id = task_id
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

    def compact(self, messages: list[dict[str, Any]], *, keep_recent: int) -> dict[str, Any] | None:
        new_messages, usage = compact_tool_history_llm(messages, keep_recent=keep_recent)
        # Replace contents in place so the same list object (shared with
        # ToolContext.messages) always reflects the compacted transcript.
        if new_messages is not messages:
            messages[:] = new_messages
        return usage

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
        event_queue: queue.Queue | None,
    ) -> None:
        self._incoming_messages = incoming_messages
        self._drive_root = drive_root
        self._task_id = task_id
        self._event_queue = event_queue

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

            drive_msgs = drain_owner_messages(self._drive_root, task_id=self._task_id, seen_ids=seen)
            for dmsg in drive_msgs:
                append_or_merge_user_message(messages, self._owner_marked_content(dmsg))
                injected_any = True
                if self._event_queue is not None:
                    try:
                        self._event_queue.put_nowait(
                            {
                                "type": "owner_message_injected",
                                "task_id": self._task_id,
                                "text": dmsg,
                            }
                        )
                    except Exception:
                        pass
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


class EventSinkAdapter(EventSink):
    """Progress, usage, periodic checkpoint, and final-result surfacing.

    ``emit_result`` publishes the task's final result (the empty-text fallback
    plus ``emit_task_results``); it closes over ``env`` / ``llm`` /
    ``pending_events`` / ``task`` / ``start_time`` (all provided only when the
    adapter drives the task graph). The standalone ``run_llm_loop`` builds this
    adapter without them and never calls ``emit_result`` (its terminal is
    ``END``), so the extra fields stay ``None`` there.
    """

    def __init__(
        self,
        *,
        emit_progress: Callable[[str], None],
        event_queue: queue.Queue | None,
        task_id: str,
        drive_logs: pathlib.Path,
        env: Any = None,
        llm: Any = None,
        pending_events: list[dict[str, Any]] | None = None,
        task: dict[str, Any] | None = None,
        start_time: float = 0.0,
    ) -> None:
        self._emit_progress = emit_progress
        self._event_queue = event_queue
        self._task_id = task_id
        self._drive_logs = drive_logs
        self._env = env
        self._llm = llm
        self._pending_events = pending_events
        self._task = task
        self._start_time = start_time
        #: Set True the moment ``emit_result`` begins its work, so ``handle_task``'s
        #: crash path can tell a publish-time failure (already emitting) from a node
        #: crash and avoid publishing the result a second time.
        self.result_emitted = False
        #: Set True by ``handle_task``'s crash path once it has written the terminal record
        #: itself, so its own error text is not published as a late answer to a stop.
        self.self_failed = False

    def progress(self, text: str) -> None:
        self._emit_progress(text)

    def typing(self) -> None:
        chat_id = (self._task or {}).get("chat_id")
        if self._event_queue is None or chat_id in (None, ""):
            return
        try:
            self._event_queue.put(
                {
                    "type": "typing_start",
                    "chat_id": chat_id,
                    "source": str((self._task or {}).get("source") or ""),
                    "trace_id": aef_tracing.current_trace_id(),
                    "ts": utc_now_iso(),
                }
            )
        except Exception:
            log.warning("Failed to emit typing start event", exc_info=True)

    def emit_tool_live_log(self, payload: dict[str, Any]) -> None:
        """Put one live log line on the event queue, stamped with time and task.

        The SSOT helper no-ops on a missing queue and swallows its own failures, which is
        what makes this callable from a tool worker thread without a guard at the call site.
        The payload's own keys win, so a caller that names a different task still can.
        """
        emit_worker_log_event(
            self._event_queue,
            {"ts": utc_now_iso(), "task_id": self._task_id, **payload},
            log_label="tool live",
        )

    def usage(self, model: str, usage: dict[str, Any], kind: str) -> None:
        emit_llm_usage_event(self._event_queue, self._task_id, model, usage, kind)

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
            self._emit_progress(f"Checkpoint {checkpoint_num} at round {round_idx}: ~{ctx_tokens} tokens")

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
        """Emit a task_checkpoint via event queue or direct runtime event record."""
        from ouroboros.agent.loop_llm_call import _emit_live_log

        payload = {"type": "task_checkpoint", "task_id": self._task_id, **data}
        if self._event_queue is not None:
            _emit_live_log(self._event_queue, payload)
        elif self._drive_logs:
            try:
                record_event(self._drive_logs.parent, "events", "task_checkpoint", {"ts": utc_now_iso(), **payload})
            except Exception:
                pass

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
        """Apply the empty-text fallback, then publish via ``emit_task_results``.

        Only the task graph reaches this (its terminal ``emit`` node); the FAILED
        write for a crashing node stays in ``handle_task``'s scaffolding, because
        a graceful error terminal must publish its ⚠️ text as a normal result —
        exactly as the pre-migration ``handle_task`` did — and the four-argument
        signature cannot tell that terminal apart from a crash.

        Whether an answer existed at all travels beside the text, because the fallback
        erases the difference between a stopped run and a model that replied with nothing.
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
        emit_task_results(
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
            has_answer=has_answer,
            stopped_externally=not self.self_failed,
        )


__all__ = [
    "EventSinkAdapter",
    "LLMProviderAdapter",
    "OwnerInboxAdapter",
    "ToolRunnerAdapter",
    "TranscriptCompactorAdapter",
]
