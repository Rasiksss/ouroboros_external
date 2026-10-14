"""The loop's phases as graph nodes (D6).

Each node is ``fn(state, ctx)`` that mutates ``state`` in place; branching lives
in the routers (see ``routers.py``), never in these bodies. The node bodies
preserve the ordering and conditions of the original ``run_llm_loop`` while
reaching every external effect through a ``GraphContext`` interface.

Most run per round. ``record_task_started``, ``prepare_context``, ``resume``, ``open_transcript``
and ``prepare_tools`` are the head of the task graph, running once before the loop is entered, and
``emit`` is its terminal. ``build_task_graph`` registers them rather than
``register_loop_topology`` — the standalone loop has no task record, nothing to assemble,
nothing that could have been parked, and is entered with its surface already formed.
"""

from __future__ import annotations

import logging
from typing import Any

from new_core.graph import GraphExecutionError
from new_core.llm.types import LLMFailure
from new_core.llm.usage import add_usage
from new_core.loop._helpers import (
    failure_message,
    is_model_unavailable,
    is_token_overflow,
    provider_failure_hint,
    provider_recovery_hint,
)
from new_core.loop.context import GraphContext
from new_core.loop.interfaces import LoopOverrides
from new_core.loop.state import RunState
from new_core.loop.transcript_merge import append_or_merge_user_message

log = logging.getLogger(__name__)


def record_task_started(state: RunState, ctx: GraphContext) -> None:
    """Open the task's record: the durable task-received event and the running status.

    The counterpart of ``emit`` at the head of the graph — one effect, reached through
    ``EventSink.task_started``, which is where the record's other end (``emit_result``)
    already lives. No prompt assembly happens here; that is ``prepare_context``.

    Typing leads (D6). It used to be the first thing the single head node did, ahead of
    any bookkeeping; signalling it after the two durable writes instead would reorder the
    emitted events for no reason. Keeping it first preserves ``typing`` → ``task_received``
    → running status exactly as before the split.
    """
    ctx.events.typing()
    ctx.events.task_started()


def prepare_context(state: RunState, ctx: GraphContext) -> None:
    """Bind the run's tool context; the transcript is ``open_transcript``'s, and a resumed run has one already.

    The context comes back **opaque** (D2), bound to ``state.messages``, which later nodes fill in place.
    """
    state.tool_ctx = ctx.assembler.tool_context(
        task=state.task,
        task_workspace=state.task_workspace,
        scope=state.scope,
        messages=state.messages,
    )


def open_transcript(state: RunState, ctx: GraphContext) -> None:
    """Assemble a fresh run's opening transcript, in place: the tool context is bound to this list."""
    state.messages[:] = ctx.assembler.opening_transcript(task=state.task, tool_ctx=state.tool_ctx, scope=state.scope)


def prepare_tools(state: RunState, ctx: GraphContext) -> None:
    """Form the run's starting tool surface, then seed the round-1 overrides (DA4).

    Runs once, on the edge into the loop. Two calls on the same interface: ``bind_surface``
    decides what the first model request carries, ``drain_overrides`` picks up anything a
    tool set before the loop began (a queued manual compaction, for instance), which
    ``apply_overrides`` consumes at the top of round 1.

    Writes nothing onto ``state.tool_ctx``. The context arrives complete from whatever
    assembled it, and stays as opaque to this node as it is to the per-round ones.
    """
    tool_ctx = state.tool_ctx
    state.tool_schemas, state.active_tool_names = ctx.tools.bind_surface(tool_ctx, state.messages)
    state.pending_overrides = ctx.tools.drain_overrides(tool_ctx)


def resume(state: RunState, ctx: GraphContext) -> None:
    """Pick a parked run back up, or leave a fresh one to start its first round."""
    snapshot = ctx.confirmations.restore(task_id=state.task_id, tool_ctx=state.tool_ctx)
    if snapshot is None:
        return
    state.restore(snapshot)
    # By name, so a surface `enable_tools` widened before the run parked comes back widened.
    state.tool_schemas[:] = ctx.tools.restore_surface(state.tool_ctx, state.active_tool_names)
    state.resumed = True


def guard(state: RunState, ctx: GraphContext) -> None:
    """Advance the round counter (round-limit routing is the guard router's job)."""
    state.round_idx += 1


def confirm(state: RunState, ctx: GraphContext) -> None:
    """Collect the owner's decisions for this round, or park the run until they answer."""
    if ctx.cancellation.is_stopped():
        return
    resp = state.last_resp
    if resp is None or not resp.tool_calls:
        raise GraphExecutionError("confirm reached without tool calls")
    asked = ctx.confirmations.evaluate(
        resp.tool_calls,
        snapshot=state.snapshot(),
        tool_ctx=state.tool_ctx,
        task_id=state.task_id,
        round_idx=state.round_idx,
        active_tool_names=state.active_tool_names,
    )
    if asked.request is not None:
        if ctx.events.ask_owner(asked.request.question, request=asked.request):
            state.suspended = True
            return
        if not ctx.confirmations.abandon(asked.request):
            state.suspended = True
            return
    state.confirmation_decisions = dict(asked.decisions)


def apply_overrides(state: RunState, ctx: GraphContext) -> None:
    """Consume drained compaction and tool-surface requests, then clear."""
    overrides = state.pending_overrides
    if overrides.pending_compaction is not None:
        state.pending_compaction = overrides.pending_compaction
    for name, schema in overrides.enable_tools:
        # The tool already resolved the name; the node only widens the surface.
        if name in state.active_tool_names:
            continue
        state.tool_schemas.append(schema)
        state.active_tool_names.add(name)
    state.pending_overrides = LoopOverrides()


def drain_inbox(state: RunState, ctx: GraphContext) -> None:
    """Inject owner messages; on injection reset circuits and mark user input."""
    injected = ctx.inbox.drain(state.messages, state.owner_msg_seen)
    if injected:
        ctx.llm.reset_circuits(state.active_model, state.active_use_local)
        ctx.tools.notify_user_input()


def checkpoint(state: RunState, ctx: GraphContext) -> None:
    """Maybe inject a periodic self-check turn; record whether one was injected.

    Injected after owner messages so the checkpoint is the LLM-call tail; only
    routine compaction is suppressed on checkpoint rounds (see prepare_transcript).
    """
    state.checkpoint_injected = ctx.events.maybe_checkpoint(
        state.messages,
        round_idx=state.round_idx,
        max_rounds=ctx.config.max_rounds,
    )


def prepare_transcript(state: RunState, ctx: GraphContext) -> None:
    """Micro/manual/emergency/routine compaction policy, ledger note, cache seal (D7)."""
    cfg = ctx.config
    comp = ctx.compactor
    messages = state.messages
    pending = state.pending_compaction
    target_tokens = None if cfg.emergency_use_chars else cfg.emergency_threshold
    target_chars = cfg.emergency_threshold if cfg.emergency_use_chars else cfg.emergency_char_threshold

    comp.microcompact(
        messages,
        keep_recent_rounds=3,
        token_threshold=0 if pending is not None else int(cfg.model_context_tokens * 0.5),
    )

    # Manual and emergency compaction always run; routine is local-only and
    # suppressed on checkpoint rounds to avoid a duplicate LLM call.
    compaction_usage = None
    if pending is not None:
        compaction_usage = comp.compact(
            messages, keep_recent=pending, target_tokens=target_tokens, target_chars=target_chars
        )
        state.pending_compaction = None
    elif (
        comp.estimate_chars(messages)
        > (cfg.emergency_threshold if cfg.emergency_use_chars else cfg.emergency_char_threshold)
    ) or (not cfg.emergency_use_chars and comp.estimate_tokens(messages) > cfg.emergency_threshold):
        compaction_usage = comp.compact(
            messages, keep_recent=50, target_tokens=target_tokens, target_chars=target_chars
        )
    elif not state.checkpoint_injected:
        if state.active_use_local and state.round_idx > 6 and len(messages) > 40:
            compaction_usage = comp.compact(
                messages, keep_recent=20, target_tokens=target_tokens, target_chars=target_chars
            )
        # Remote routine compaction stays off; emergency handles overflow.

    if compaction_usage:
        add_usage(state.accumulated_usage, compaction_usage)
        ctx.events.usage(cfg.light_model, compaction_usage, "compaction")

    comp.microcompact(messages, keep_recent_rounds=3, token_threshold=int(cfg.model_context_tokens * 0.5))
    comp.upsert_ledger_note(messages)
    # Anthropic cache boundary; other providers strip cache_control in llm.py.
    comp.seal(messages)


def llm_call(state: RunState, ctx: GraphContext) -> None:
    """Call the model with the active tools; routing is the llm_call router's job."""
    # Chat typing indicators are short-lived, so re-signal before every model request.
    ctx.events.typing()
    resp = ctx.llm.call(
        messages=state.messages,
        model=state.active_model,
        tools=state.tool_schemas,
        effort=state.active_effort,
        round_idx=state.round_idx,
        use_local=state.active_use_local,
        accumulated_usage=state.accumulated_usage,
    )
    state.record_llm_result(resp)


def _retry_after_overflow(
    state: RunState, ctx: GraphContext, model: str, use_local: bool, tools: list[Any] | None
) -> bool:
    if ctx.cancellation.is_stopped():
        return False
    if state.last_resp is not None or not is_token_overflow(state.last_failure):
        return False
    log.warning("Token overflow detected — running emergency compaction and retrying")
    try:
        ctx.compactor.microcompact(state.messages, keep_recent_rounds=1, token_threshold=0)
        usage = ctx.compactor.compact(state.messages, keep_recent=1, target_tokens=0, target_chars=0)
        if usage:
            add_usage(state.accumulated_usage, usage)
            ctx.events.usage(ctx.config.light_model, usage, "compaction")
        ctx.events.typing()
        result = ctx.llm.call(
            messages=state.messages,
            model=model,
            tools=tools,
            effort=state.active_effort,
            round_idx=state.round_idx,
            use_local=use_local,
            accumulated_usage=state.accumulated_usage,
        )
        state.record_llm_result(result)
    except Exception as exc:
        state.record_llm_result(LLMFailure(str(exc)))
        log.warning("Emergency compaction retry failed", exc_info=True)
    return True


def recover(state: RunState, ctx: GraphContext) -> None:
    """Try emergency compaction once and a fallback, then report the current failure."""
    if ctx.cancellation.is_stopped():
        return
    cfg = ctx.config
    compacted = _retry_after_overflow(state, ctx, state.active_model, state.active_use_local, state.tool_schemas)
    if state.last_resp is not None:
        return

    primary_tag = " (local)" if state.active_use_local else ""
    model_unavailable = is_model_unavailable(state.last_failure)
    fallback_model = cfg.fallback_model
    if fallback_model and fallback_model != state.active_model:
        if ctx.cancellation.is_stopped():
            return
        fallback_tag = " (local)" if cfg.fallback_use_local else ""
        fallback_reason = "model temporarily unavailable" if model_unavailable else "empty response"
        ctx.events.progress(
            f"⚡ Fallback: {state.active_model}{primary_tag} → {fallback_model}{fallback_tag} after {fallback_reason}"
        )
        ctx.events.typing()
        result = ctx.llm.call(
            messages=state.messages,
            model=fallback_model,
            tools=state.tool_schemas,
            effort=state.active_effort,
            round_idx=state.round_idx,
            use_local=cfg.fallback_use_local,
            accumulated_usage=state.accumulated_usage,
        )
        state.record_llm_result(result)
        if not compacted:
            _retry_after_overflow(state, ctx, fallback_model, cfg.fallback_use_local, state.tool_schemas)
        if state.last_resp is not None:
            if model_unavailable:
                state.active_model = fallback_model
                state.active_use_local = cfg.fallback_use_local
            return
        failure_text = (
            f"⚠️ All models are down. Primary ({state.active_model}{primary_tag}) and "
            f"fallback ({fallback_model}{fallback_tag}) both returned no response. Stopping."
        )
    else:
        failure_text = (
            f"⚠️ Failed to get a response from model {state.active_model}{primary_tag} "
            f"after {cfg.max_retries} attempts. No viable fallback model configured."
        )
    state.final_content = failure_message(
        state.last_failure,
        f"{failure_text}{provider_failure_hint(state.last_failure)} {provider_recovery_hint(state.last_failure)}",
        model_unavailable=is_model_unavailable(state.last_failure),
    )


def exec_tools(state: RunState, ctx: GraphContext) -> None:
    """Append the assistant turn, emit progress/trace, run tools, record them, drain overrides.

    Running the calls and recording them are two calls on the tool runner, not one: the
    node holds the results in between, so what executed and what the model is told about
    it are separate steps here rather than one effect behind the interface.
    """
    if ctx.cancellation.is_stopped():
        return
    resp = state.last_resp
    if resp is None or not resp.tool_calls:
        raise GraphExecutionError("exec_tools reached without tool calls")
    content = resp.content
    state.messages.append(
        {
            "role": "assistant",
            "content": content or "",
            "tool_calls": [tc.to_dict() for tc in resp.tool_calls],
        }
    )

    if content and content.strip():
        if ctx.config.reasoning_progress:
            ctx.events.progress(content.strip())
        state.llm_trace["reasoning_notes"].append(content.strip())

    results = ctx.tools.handle_tool_calls(
        tool_calls=resp.tool_calls,
        active_tool_names=state.active_tool_names,
        tool_ctx=state.tool_ctx,
        task_id=state.task_id,
        decisions=state.confirmation_decisions,
    )
    state.confirmation_decisions = {}
    if ctx.cancellation.is_stopped():
        return
    ctx.tools.process_tool_results(
        results=results,
        messages=state.messages,
        trace=state.llm_trace,
        tool_ctx=state.tool_ctx,
    )
    state.pending_overrides = ctx.tools.drain_overrides(state.tool_ctx)


def finalize(state: RunState, ctx: GraphContext) -> None:
    """Record the language-repaired final text."""
    if ctx.cancellation.is_stopped():
        return
    resp = state.last_resp
    if resp is None:
        raise GraphExecutionError("finalize reached without a response")
    content = resp.content
    final = ctx.llm.repair_language(
        content,
        messages=state.messages,
        model=state.active_model,
        effort=state.active_effort,
        use_local=state.active_use_local,
        round_idx=state.round_idx,
        accumulated_usage=state.accumulated_usage,
    )
    if final and final.strip():
        state.llm_trace["reasoning_notes"].append(final.strip())
    state.final_content = final or ""


def round_limit_final(state: RunState, ctx: GraphContext) -> None:
    """Inject the round-limit notice, make one final call, and record the answer."""
    if ctx.cancellation.is_stopped():
        return
    cfg = ctx.config
    finish_reason = (
        f"⚠️ Task exceeded MAX_ROUNDS ({cfg.max_rounds}). Consider decomposing into subtasks via schedule_task."
    )
    append_or_merge_user_message(state.messages, f"[ROUND_LIMIT] {finish_reason}")
    try:
        prepare_transcript(state, ctx)
        ctx.events.typing()
        result = ctx.llm.call(
            messages=state.messages,
            model=state.active_model,
            tools=None,
            effort=state.active_effort,
            round_idx=state.round_idx,
            use_local=state.active_use_local,
            accumulated_usage=state.accumulated_usage,
        )
        state.record_llm_result(result)
        _retry_after_overflow(state, ctx, state.active_model, state.active_use_local, None)
    except Exception as exc:
        state.record_llm_result(LLMFailure(str(exc)))
        log.warning("Failed to get final response after round limit", exc_info=True)
    state.final_content = (
        state.last_resp.content or finish_reason
        if state.last_resp is not None
        else failure_message(
            state.last_failure,
            f"{finish_reason}{provider_failure_hint(state.last_failure)}",
            model_unavailable=is_model_unavailable(state.last_failure),
        )
    )


def emit(state: RunState, ctx: GraphContext) -> None:
    """Publish the task's terminal outcome.

    A suspended run publishes nothing: it has no answer yet, and its record is parked for the owner's reply.
    """
    if state.suspended:
        return
    if state.tool_ctx is None:
        raise GraphExecutionError("emit reached before prepare_context bound the tool context")
    if ctx.cancellation.is_stopped():
        state.final_content = None
    ctx.events.emit_result(
        text=state.final_content,
        usage=state.accumulated_usage,
        trace=state.llm_trace,
        tool_ctx=state.tool_ctx,
    )


__all__ = [
    "apply_overrides",
    "checkpoint",
    "confirm",
    "drain_inbox",
    "emit",
    "exec_tools",
    "finalize",
    "guard",
    "llm_call",
    "open_transcript",
    "prepare_context",
    "prepare_tools",
    "prepare_transcript",
    "record_task_started",
    "recover",
    "resume",
    "round_limit_final",
]
