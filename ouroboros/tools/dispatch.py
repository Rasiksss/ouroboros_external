"""The one place the cross-cutting tool gates run, before any provider is reached.

Deliberately a function, not a class: it holds neither entries nor context, or it would be
``ToolRegistry`` under a new name. ``ctx`` is an argument, so nothing retains it after the
call returns.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import pathlib
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from new_core.tools.catalog import ToolCatalog
from new_core.tools.interfaces import ToolDescriptor
from ouroboros.contracts.skill_payload_policy import (
    cross_skill_redirect_error,
    decide_payload_short_form,
    is_skill_payload_path,
)
from ouroboros.contracts.task_constraint import TaskConstraint, normalize_task_constraint
from ouroboros.tools.catalog import skill_repair_tool_names, spec_for_tool

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)

_HEAL_MODE_ALLOWED_TOOLS = skill_repair_tool_names()

_HEAL_PROTECTED_PAYLOAD_FILENAMES = frozenset(
    {
        ".clawhub.json",
        ".ouroboroshub.json",
        ".self_authored.json",
        # Protect provenance/update-lane markers during heal-mode payload edits.
        "skill.openclaw.md",
    }
)


def _heal_protected_payload_sidecar(path_text: str) -> bool:
    name = pathlib.PurePosixPath(str(path_text or "").replace("\\", "/")).name
    return name.lower() in _HEAL_PROTECTED_PAYLOAD_FILENAMES


def _task_constraint_path_allowed(
    path_text: str,
    constraint: TaskConstraint | None,
    drive_root: pathlib.Path,
) -> bool:
    return is_skill_payload_path(
        drive_root,
        path_text or "",
        constraint=constraint,
        allow_short_relative=True,
        allow_control_plane=True,
    )


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


def _heal_mode_refusal(
    ctx: ToolContext,
    name: str,
    args: dict[str, Any],
    descriptor: ToolDescriptor | None,
    constraint: TaskConstraint,
) -> str | None:
    """Skill-repair restrictions. Returns the refusal text, or None to continue."""
    heal_skill = constraint.skill_name or ""
    drive_root = pathlib.Path(ctx.drive_root)

    if name in {"data_read", "data_write"}:
        data_path = str(args.get("path", "") or "")
        if not _task_constraint_path_allowed(data_path, constraint, drive_root):
            return (
                "⚠️ HEAL_MODE_BLOCKED: Repair data access is limited "
                "to the selected skill payload under data/skills/external "
                "data/skills/clawhub, or data/skills/ouroboroshub."
            )
        if name == "data_write" and _heal_protected_payload_sidecar(data_path):
            return (
                "⚠️ HEAL_MODE_BLOCKED: Repair may not edit official "
                "provenance sidecars (.clawhub.json, "
                ".ouroboroshub.json, SKILL.openclaw.md). "
                "Edit the user-authored payload files instead."
            )
    if name == "data_list":
        data_dir = str(args.get("dir", args.get("path", "")) or "")
        if not _task_constraint_path_allowed(data_dir, constraint, drive_root):
            return (
                "⚠️ HEAL_MODE_BLOCKED: Repair data listing is limited "
                "to the selected skill payload under data/skills/external "
                "data/skills/clawhub, or data/skills/ouroboroshub."
            )
    if name == "skill_preflight" and str(args.get("skill", "") or "").strip() != heal_skill:
        return "⚠️ HEAL_MODE_BLOCKED: Repair may only preflight the selected skill."

    # Every tool that reaches outside the process is refused wholesale, before any provider
    # is reached — extensions, MCP servers, and remote A2A agents alike.
    from_remote_source = descriptor is not None and descriptor.origin.startswith(("ext:", "mcp:", "a2a:"))
    if from_remote_source or name not in _HEAL_MODE_ALLOWED_TOOLS:
        return (
            "⚠️ HEAL_MODE_BLOCKED: Repair tasks may inspect/edit skill "
            "payloads and run skill_preflight only. Shell automation, "
            "repo mutation, skill execution, extension tools, MCP tools, "
            "remote agent tools, delegation, and enable/disable flows are "
            "unavailable."
        )
    return None


def _data_write_short_form_refusal(
    ctx: ToolContext,
    args: dict[str, Any],
    constraint: TaskConstraint | None,
) -> str | None:
    raw_bucket = str(args.get("bucket", "") or "")
    raw_skill_name = str(args.get("skill_name", "") or "")
    short_path_text = str(args.get("path", "") or "")
    decision = decide_payload_short_form(
        bucket=raw_bucket,
        skill_name=raw_skill_name,
        path_text=short_path_text or ".",
        repo_dir=pathlib.Path(ctx.repo_dir),
        drive_root=pathlib.Path(ctx.drive_root),
    )
    if (raw_bucket or raw_skill_name) and decision.error:
        return f"⚠️ SKILL_PAYLOAD_ARG_ERROR: {decision.error}"
    redirect_err = cross_skill_redirect_error(constraint, decision.constraint)
    if redirect_err:
        return f"⚠️ SKILL_REDIRECT_BLOCKED: {redirect_err}"
    return None


async def execute_tool(
    catalog: ToolCatalog,
    ctx: ToolContext,
    name: str,
    args: dict[str, Any],
) -> str:
    """Run the gates once, then delegate to the provider that owns the name.

    The cheap gates (table lookups, a cached settings read) run on the calling
    loop; ``check_safety`` runs in a thread because its LLM path blocks.
    """
    name = str(name or "").strip()
    args = dict(args or {})

    direct_excluded_allowed = _excluded_direct_execute_allowed(ctx, name, args)
    if spec_for_tool(name).surface == "excluded" and not direct_excluded_allowed:
        return f"⚠️ TOOL_UNAVAILABLE: tool {name} is not available in this runtime profile."

    descriptor = catalog.find(name)
    if descriptor is not None:
        # A provider may have resolved an alias; gates run on the canonical name.
        name = descriptor.name

    constraint = normalize_task_constraint(ctx.task_constraint)
    if constraint is not None and constraint.mode == "skill_repair":
        refusal = _heal_mode_refusal(ctx, name, args, descriptor, constraint)
        if refusal is not None:
            return refusal

    if descriptor is None:
        # A provider may own the name and still withhold it — a dead extension's tool is
        # hidden from every surface read, so nothing resolves here. Ask before concluding
        # the name is unknown, and ask now, ahead of the safety gate: a call that will not
        # run must not first cost a model call.
        withheld = catalog.refuse_dispatch(name)
        if withheld is not None:
            return withheld
        # Only the repo and skill tools are listed, as before: enumerating the whole
        # catalog would run the extension provider's per-tool liveness walk, and a model
        # looping on a bad name would pay it every round.
        return f"⚠️ Unknown tool: {name}. Available: {', '.join(catalog.local_tool_names())}"

    if descriptor.surface == "excluded" and not direct_excluded_allowed:
        return f"⚠️ TOOL_UNAVAILABLE: tool {name} is not available in this runtime profile."

    if name == "data_write":
        refusal = _data_write_short_form_refusal(ctx, args, constraint)
        if refusal is not None:
            return refusal

    safety_msg = ""
    # A declared "skip" cannot block (check_safety returns immediately), so it
    # takes no thread hop — a parallel batch of skip tools spawns no executors.
    needs_verdict = not descriptor.skips_llm_safety and str(descriptor.safety_policy or "").strip().lower() != "skip"
    if needs_verdict:
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

    result = await catalog.aexecute(ctx, name, args)
    result_str = result if isinstance(result, str) else str(result)
    if safety_msg:
        return f"{safety_msg}\n\n---\n{result_str}"
    return result_str
