"""The loop's phases as graph nodes (D6).

Each node is ``fn(state, ctx)`` that mutates ``state`` in place; branching lives
in the routers (see ``routers.py``), never in these bodies. The node bodies
preserve the ordering and conditions of the original ``run_llm_loop`` while
reaching every external effect through a ``GraphContext`` interface.

All but one run per round. ``prepare_tools`` runs once, before the loop is entered, and
is registered by the embedding graph rather than by ``register_loop_topology`` — the
standalone loop is entered with its surface already formed.
"""

from __future__ import annotations

import logging

from new_core.llm.usage import add_usage
from new_core.loop._helpers import (
    is_token_overflow,
    provider_failure_hint,
    provider_recovery_hint,
)
from new_core.loop.context import GraphContext
from new_core.loop.interfaces import LoopOverrides
from new_core.loop.state import RunState
from new_core.loop.transcript_merge import append_or_merge_user_message

log = logging.getLogger(__name__)


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


def guard(state: RunState, ctx: GraphContext) -> None:
    """Advance the round counter (round-limit routing is the guard router's job)."""
    state.round_idx += 1


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

    comp.microcompact(
        messages,
        keep_recent_rounds=3,
        token_threshold=0 if pending is not None else int(cfg.model_context_tokens * 0.5),
    )

    # Manual and emergency compaction always run; routine is local-only and
    # suppressed on checkpoint rounds to avoid a duplicate LLM call.
    compaction_usage = None
    if pending is not None:
        compaction_usage = comp.compact(messages, keep_recent=pending)
        state.pending_compaction = None
    elif (
        comp.estimate_chars(messages)
        > (cfg.emergency_threshold if cfg.emergency_use_chars else cfg.emergency_char_threshold)
    ) or (not cfg.emergency_use_chars and comp.estimate_tokens(messages) > cfg.emergency_threshold):
        compaction_usage = comp.compact(messages, keep_recent=50)
    elif not state.checkpoint_injected:
        if state.active_use_local and state.round_idx > 6 and len(messages) > 40:
            compaction_usage = comp.compact(messages, keep_recent=20)
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
    state.last_resp = resp


def recover(state: RunState, ctx: GraphContext) -> None:
    """Recover an empty response once: emergency compaction + retry, then fallback (D8).

    Never routes back into ``llm_call``; on exhausted recovery it records a
    descriptive failure message as ``final_content`` and the router ends the run.
    """
    cfg = ctx.config
    comp = ctx.compactor
    messages = state.messages

    # Token-overflow emergency compaction + same-model retry.
    if is_token_overflow(state.accumulated_usage):
        try:
            log.warning("Token overflow detected — running emergency compaction and retrying")
            comp.microcompact(messages, keep_recent_rounds=2, token_threshold=0)
            comp_usage = comp.compact(messages, keep_recent=20)
            if comp_usage:
                add_usage(state.accumulated_usage, comp_usage)
                ctx.events.usage(cfg.light_model, comp_usage, "compaction")
            ctx.events.typing()
            resp = ctx.llm.call(
                messages=messages,
                model=state.active_model,
                tools=state.tool_schemas,
                effort=state.active_effort,
                round_idx=state.round_idx,
                use_local=state.active_use_local,
                accumulated_usage=state.accumulated_usage,
            )
            state.last_resp = resp
        except Exception:
            log.warning("Emergency compaction retry failed", exc_info=True)

    if state.last_resp is not None:
        return

    # Single fallback-model call if a distinct fallback is configured.
    fallback_model = cfg.fallback_model
    primary_tag = " (local)" if state.active_use_local else ""
    if not fallback_model or fallback_model == state.active_model:
        state.final_content = (
            f"⚠️ Failed to get a response from model {state.active_model}{primary_tag} "
            f"after {cfg.max_retries} attempts. No viable fallback model configured."
            f"{provider_failure_hint(state.accumulated_usage)} "
            f"{provider_recovery_hint(state.accumulated_usage)}"
        )
        return

    fallback_tag = " (local)" if cfg.fallback_use_local else ""
    ctx.events.progress(
        f"⚡ Fallback: {state.active_model}{primary_tag} → {fallback_model}{fallback_tag} after empty response"
    )
    ctx.events.typing()
    resp = ctx.llm.call(
        messages=messages,
        model=fallback_model,
        tools=state.tool_schemas,
        effort=state.active_effort,
        round_idx=state.round_idx,
        use_local=cfg.fallback_use_local,
        accumulated_usage=state.accumulated_usage,
    )
    state.last_resp = resp
    if resp is None:
        state.final_content = (
            f"⚠️ All models are down. Primary ({state.active_model}{primary_tag}) and "
            f"fallback ({fallback_model}{fallback_tag}) both returned no response. Stopping."
            f"{provider_failure_hint(state.accumulated_usage)} "
            f"{provider_recovery_hint(state.accumulated_usage)}"
        )


def exec_tools(state: RunState, ctx: GraphContext) -> None:
    """Append the assistant turn, emit progress/trace, run tools, record them, drain overrides.

    Running the calls and recording them are two calls on the tool runner, not one: the
    node holds the results in between, so what executed and what the model is told about
    it are separate steps here rather than one effect behind the interface.
    """
    resp = state.last_resp
    assert resp is not None  # the llm_call/recover routers only reach exec_tools with a response
    assert resp.tool_calls is not None
    content = resp.content
    state.messages.append(
        {
            "role": "assistant",
            "content": content or "",
            "tool_calls": [tc.to_dict() for tc in resp.tool_calls],
        }
    )

    if content and content.strip():
        ctx.events.progress(content.strip())
        state.llm_trace["reasoning_notes"].append(content.strip())

    results = ctx.tools.handle_tool_calls(
        tool_calls=resp.tool_calls,
        active_tool_names=state.active_tool_names,
        tool_ctx=state.tool_ctx,
        task_id=state.task_id,
    )
    ctx.tools.process_tool_results(
        results=results,
        messages=state.messages,
        trace=state.llm_trace,
        tool_ctx=state.tool_ctx,
    )
    state.pending_overrides = ctx.tools.drain_overrides(state.tool_ctx)


def finalize(state: RunState, ctx: GraphContext) -> None:
    """Skill-finalization gate, else language-repaired final text (router ends the run).

    On the skill-finalization path ``final_content`` stays ``None`` so the
    finalize router loops back to ``guard``; otherwise it is set (possibly "")
    and the router routes to ``END``.
    """
    resp = state.last_resp
    assert resp is not None  # the llm_call/recover routers only reach finalize with a response
    content = resp.content
    finalization_msg = ctx.tools.skill_finalization_message(state.llm_trace)
    if finalization_msg and not state.skill_finalization_injected:
        state.skill_finalization_injected = True
        if content and content.strip():
            state.messages.append({"role": "assistant", "content": content})
        append_or_merge_user_message(state.messages, f"[SYSTEM REMINDER]\n{finalization_msg}")
        ctx.events.progress(finalization_msg)
        state.llm_trace["reasoning_notes"].append(finalization_msg)
        return

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
    cfg = ctx.config
    finish_reason = (
        f"⚠️ Task exceeded MAX_ROUNDS ({cfg.max_rounds}). Consider decomposing into subtasks via schedule_task."
    )
    append_or_merge_user_message(state.messages, f"[ROUND_LIMIT] {finish_reason}")
    try:
        ctx.events.typing()
        final_msg = ctx.llm.call(
            messages=state.messages,
            model=state.active_model,
            tools=None,
            effort=state.active_effort,
            round_idx=state.round_idx,
            use_local=state.active_use_local,
            accumulated_usage=state.accumulated_usage,
        )
        if final_msg is not None:
            state.final_content = final_msg.content or finish_reason
        else:
            state.final_content = finish_reason
    except Exception:
        log.warning("Failed to get final response after round limit", exc_info=True)
        state.final_content = finish_reason


__all__ = [
    "apply_overrides",
    "checkpoint",
    "drain_inbox",
    "exec_tools",
    "finalize",
    "guard",
    "llm_call",
    "prepare_tools",
    "prepare_transcript",
    "recover",
    "round_limit_final",
]
