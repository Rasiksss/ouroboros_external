"""The four conditional-edge routers (D6).

A router is ``fn(state, ctx) -> next node name | END``. Routers read the state a
node just mutated; they never mutate it themselves.
"""

from __future__ import annotations

from new_core.loop.context import GraphContext
from new_core.loop.state import RunState


def route_guard(state: RunState, ctx: GraphContext) -> str:
    """Two domain edges: a cooperative stop → terminal; over ``max_rounds`` → the wrap-up node.

    The stop is checked first and skips the wrap-up, because the round limit spends one
    more model call to produce a useful answer while a stop means no answer is wanted.
    """
    if ctx.cancellation.is_stopped():
        return ctx.config.terminal
    if state.round_idx > ctx.config.max_rounds:
        return "round_limit_final"
    return "apply_overrides"


def route_after_llm(state: RunState, ctx: GraphContext) -> str:
    """Empty → recover; tool calls → exec_tools; plain text → finalize."""
    resp = state.last_resp
    if resp is None:
        return "recover"
    if resp.tool_calls:
        return "exec_tools"
    return "finalize"


def route_after_recover(state: RunState, ctx: GraphContext) -> str:
    """Still empty → terminal (failure recorded); tool calls → exec_tools; text → finalize."""
    resp = state.last_resp
    if resp is None:
        return ctx.config.terminal
    if resp.tool_calls:
        return "exec_tools"
    return "finalize"


def route_after_finalize(state: RunState, ctx: GraphContext) -> str:
    """Skill-finalization reinject → guard; otherwise the run reaches the terminal."""
    if state.final_content is not None:
        return ctx.config.terminal
    return "guard"


def route_after_round_limit(state: RunState, ctx: GraphContext) -> str:
    """One-branch terminal router: the round-limit wrap-up always ends the run.

    A router (not a static edge) so the round-limit path reaches the same
    configured terminal (``END`` standalone, ``emit`` when embedded) as the other
    terminal outcomes (DA3).
    """
    return ctx.config.terminal


__all__ = [
    "route_after_finalize",
    "route_after_llm",
    "route_after_recover",
    "route_after_round_limit",
    "route_guard",
]
