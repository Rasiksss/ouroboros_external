"""The one place the cross-cutting tool gates run, before any provider is reached.

Deliberately a function, not a class: it holds neither entries nor context, or it would be
``ToolRegistry`` under a new name. ``ctx`` is an argument, so nothing retains it after the
call returns.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from config_registry.user_settings.tool_access import skill_tool_refusal
from new_core.tools.catalog import ToolCatalog
from new_core.tools.interfaces import ToolResult
from ouroboros.storage.runtime_backend import STORE_FAILURES, ExecutionClaimUnavailable
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.catalog import spec_for_tool

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)


def _excluded_direct_execute_allowed(ctx: ToolContext, name: str, args: dict[str, Any]) -> bool:
    """No excluded tool is granted an off-surface exception.

    ``repo_read`` once stayed callable on skill-readable absolute paths via
    ``skills.loader.is_allowed_skill_read_path``. That carve-out was retired, and the tool
    itself followed: ``repo_read``, ``repo_list``, ``code_search``, ``codebase_digest`` and
    ``codebase_health`` are metadata-only tombstones now, with no handler behind the name.
    The gate refuses every excluded tool. Kept as the single decision point in case a
    future exception needs one place to land.
    """
    return False


async def _attempt_inactive(ctx: ToolContext, task_id: str) -> bool:
    if not (ctx.detached and task_id and ctx.current_task_type == "task"):
        return False
    from ouroboros.storage.task_results import execution_claim

    claim = execution_claim.get()
    try:
        active = await asyncio.to_thread(
            runtime_persistence.current.tasks_repo.is_task_attempt_active, task_id, claim_token=ctx.execution_claim
        )
    except STORE_FAILURES as exc:
        raise ExecutionClaimUnavailable(task_id) from exc
    return not active or (claim is not None and await asyncio.to_thread(claim[2]))


async def _dispatch_blocked(ctx: ToolContext, task_id: str) -> bool:
    """Whether this call lost its task authority before its handler can start."""
    return ctx.stop_requested() or await _attempt_inactive(ctx, task_id)


async def execute_tool(
    catalog: ToolCatalog,
    ctx: ToolContext,
    name: str,
    args: dict[str, Any],
    *,
    origin: str,
) -> str | ToolResult:
    """Run the gates once, then delegate to the provider that owns the name.

    The cheap gates (table lookups, a cached settings read) run on the calling
    loop; ``check_safety`` runs in a thread because its LLM path blocks.
    """
    task_id = str(ctx.task_id or "").strip()
    name = str(name or "").strip()
    args = dict(args or {})

    direct_excluded_allowed = _excluded_direct_execute_allowed(ctx, name, args)
    if spec_for_tool(name).surface == "excluded" and not direct_excluded_allowed:
        return f"⚠️ TOOL_UNAVAILABLE: tool {name} is not available in this runtime profile."

    descriptor = catalog.find(name, ctx.scope)
    if descriptor is not None and descriptor.origin != origin:
        # The round decided whether to ask the owner on ``origin``, and a name that resolves to
        # another one now was never put to that decision.
        return f"⚠️ TOOL_UNAVAILABLE: tool {name} changed after the call was authorized; call it again."
    if descriptor is not None:
        # A provider may have resolved an alias; gates run on the canonical name.
        name = descriptor.name

    if descriptor is None:
        # A provider may own the name and still withhold it — a dead extension's tool is
        # hidden from every surface read, so nothing resolves here. Ask before concluding
        # the name is unknown, and ask now, ahead of the safety gate: a call that will not
        # run must not first cost a model call.
        withheld = catalog.refuse_dispatch(name, ctx.scope)
        if withheld is not None:
            return withheld
        if ctx.skill_origins is not None:
            return f"⚠️ Unknown tool: {name}."
        # Only the repo and skill tools are listed, as before: enumerating the whole
        # catalog would run the extension provider's per-tool liveness walk, and a model
        # looping on a bad name would pay it every round.
        return f"⚠️ Unknown tool: {name}. Available: {', '.join(catalog.local_tool_names())}"

    if descriptor.surface == "excluded" and not direct_excluded_allowed:
        return f"⚠️ TOOL_UNAVAILABLE: tool {name} is not available in this runtime profile."

    if denied := skill_tool_refusal(name, descriptor.origin, ctx.skill_origins):
        return denied

    safety_msg = ""
    # A declared "skip" cannot block (check_safety returns immediately), so it
    # takes no thread hop — a parallel batch of skip tools spawns no executors.
    needs_verdict = not descriptor.skips_llm_safety and str(descriptor.safety_policy or "").strip().lower() != "skip"
    if needs_verdict:
        if await _dispatch_blocked(ctx, task_id):
            return "⚠️ TOOL_TASK_ATTEMPT_INACTIVE"
        from ouroboros.policy import safety as safety_policy

        # Off the event loop, on an owned per-call executor — NOT asyncio.to_thread:
        # the loop's default executor is joined unboundedly at round teardown on
        # 3.10/3.11, so an abandoned check would hold the whole round open (the
        # measured hazard in new_core/tools/runtimes.py's module docstring).
        call = contextvars.copy_context()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"safety-{name}")
        try:
            is_safe, safety_msg = await asyncio.wrap_future(
                executor.submit(
                    call.run,
                    safety_policy.check_safety,
                    name,
                    args,
                    messages=ctx.messages,
                    ctx=ctx,
                    # Declared descriptor policy, keyed by the canonical name (see check_safety).
                    policy=descriptor.safety_policy,
                )
            )
        finally:
            # wait=False is the abandonment: a cancelled call leaves the check to
            # finish on its own thread and its verdict is discarded.
            executor.shutdown(wait=False)
        if not is_safe:
            return safety_msg

    if await _dispatch_blocked(ctx, task_id):
        return "⚠️ TOOL_TASK_ATTEMPT_INACTIVE"
    result = await catalog.aexecute(ctx, name, args)
    if isinstance(result, ToolResult):
        return replace(result, text=f"{safety_msg}\n\n---\n{result.text}") if safety_msg else result
    result_str = result if isinstance(result, str) else str(result)
    if safety_msg:
        return f"{safety_msg}\n\n---\n{result_str}"
    return result_str
