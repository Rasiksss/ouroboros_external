"""The per-task ``GraphContext`` and ``RunState`` for the graph ``new_core/loop/build.py`` authors.

What stays here is the decisions the adapters call: ``resolve_task_lineage`` and
``bind_tool_surface`` plus the policy each applies.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from background_memory.infra.events import memory_event_publisher_factory
from config_registry.user_settings.reader import UserSettings, user_settings_factory
from config_registry.user_settings.tool_access import admitted_skill_origins
from new_core.identity import OwnerScope
from new_core.loop import (
    CancellationSignal,
    GraphContext,
    RunConfig,
    RunState,
    make_run_state,
)
from new_core.loop.transcript_merge import merge_system_note
from new_core.skills.catalog import SkillCatalogSnapshot
from new_core.skills.models import SkillId
from new_core.skills.surface import SkillAvailability, SkillSurface
from new_core.tools.catalog import ToolCatalog, tool_catalog
from new_core.tools.interfaces import ToolDescriptor
from ouroboros.agent.loop_adapters import (
    ContextAssemblerAdapter,
    EventSinkAdapter,
    LLMProviderAdapter,
    OwnerInboxAdapter,
    ToolConfirmationBroker,
    ToolRunnerAdapter,
    TranscriptCompactorAdapter,
    build_tool_call_executor,
)
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger
from ouroboros.app.config import get_light_model, resolve_effort, skill_run_profile
from ouroboros.policy.capabilities import CORE_TOOL_NAMES
from ouroboros.policy.llm_middlewares import llm_middlewares
from ouroboros.storage.task_results import load_task_result
from ouroboros.tools.catalog import WORKSTATION_TOOL_NAMES, core_tool_names, spec_for_tool
from ouroboros.tools.surface import ProviderToolSurface

if TYPE_CHECKING:
    import pathlib

    from new_core.tools.context import ToolContext
    from ouroboros.agent.core import OuroborosAgent
    from ouroboros.storage.settings.schema import AppSettings

log = logging.getLogger(__name__)

# Emergency compaction fallback: when the char-based override is not configured, this is
# the transcript size (in characters) past which the loop compacts aggressively.
_EMERGENCY_CHAR_THRESHOLD = 1_200_000


def _parse_enabled_tools(raw: str | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\s,]+", str(raw or "")):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _env_true(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _build_task_skill_surface(
    *,
    snapshot: SkillCatalogSnapshot,
    availability: SkillAvailability,
    scope: OwnerScope,
    settings: UserSettings,
) -> SkillSurface:
    task_selection = frozenset(skill.skill_id for skill in snapshot.skills())
    if not availability.executable_skills:
        try:
            enabled = frozenset(SkillId.parse(value) for value in settings.OUROBOROS_USER_ENABLED_SKILL_IDS(scope))
            task_selection = task_selection.intersection(enabled)
        except Exception:
            task_selection = frozenset()
            log.warning(
                "Failed to resolve owner skill allowlist for owner=%s agent=%s; denying task skill access",
                scope.owner_id,
                scope.agent_id,
                exc_info=True,
            )
    return SkillSurface(
        snapshot=snapshot,
        availability=availability,
        agent_name=scope.agent_id,
        task_selection=task_selection,
    )


def _local_descriptors(descriptors: list[ToolDescriptor]) -> list[ToolDescriptor]:
    """Repo-defined and skill tools — the set the registry used to hold in ``_entries``.

    Extension and MCP tools are deliberately not here: the first-request surface never
    drew on them through this path, and the extension budget below adds them separately.
    """
    return [d for d in descriptors if d.origin == "builtin" or d.origin.startswith("skill:")]


def _configured_initial_tools(descriptors: list[ToolDescriptor], settings: AppSettings) -> list[ToolDescriptor]:
    """Tools promoted into the first prompt by config, never by permission, in configured order."""
    requested = _parse_enabled_tools(settings.OUROBOROS_ENABLED_TOOLS)

    # Compatibility for pre-catalog opt-in flags such as TOOL_CALCULATOR_ENABLED.
    # Falsey legacy values no longer disable installed tools; they just do not
    # promote discoverable schemas into the first prompt.
    local = _local_descriptors(descriptors)
    for desc in local:
        prefix = (desc.env_prefix or "").strip()
        if prefix and _env_true(settings.get(f"{prefix}_ENABLED")):
            requested.append(desc.name.strip())

    selectable = local + [d for d in descriptors if d.origin.startswith("ext:")]
    stable_name_counts = Counter(d.stable_name for d in selectable if d.stable_name)
    unambiguous_stable_names = {d.stable_name: d for d in selectable if stable_name_counts[d.stable_name] == 1}
    by_configured_name = unambiguous_stable_names | {d.name: d for d in selectable}
    configured: dict[str, ToolDescriptor] = {}
    for configured_name in requested:
        selected = by_configured_name.get(configured_name)
        if selected is None or spec_for_tool(selected.name).surface == "excluded":
            continue
        configured.setdefault(selected.name, selected)
    return list(configured.values())


def _initial_tool_schemas(descriptors: list[ToolDescriptor], tool_ctx: ToolContext) -> tuple[list[Any], set[str]]:
    """Schemas and names of the first model request (docs/ARCHITECTURE.md §Tool catalog and selection)."""
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    configured = _configured_initial_tools(descriptors, settings)
    initial_names = set(core_tool_names()) | {desc.name for desc in configured}
    attachments = tool_ctx.attachments or []
    has_image = any(
        isinstance(item, dict) and (item.get("kind") == "image" or str(item.get("mime") or "").startswith("image/"))
        for item in attachments
    )
    if has_image:
        initial_names.add("vlm_query")
    else:
        initial_names.discard("vlm_query")

    if settings.WORKSTATION_BASE_URL.strip():
        initial_names.update(WORKSTATION_TOOL_NAMES)
    else:
        initial_names.difference_update(WORKSTATION_TOOL_NAMES)

    local = _local_descriptors(descriptors)
    # Core names first, then the discoverable ones — the order the registry produced.
    ordered = [d for d in local if d.name in CORE_TOOL_NAMES] + [d for d in local if d.surface == "discoverable"]

    result: list[Any] = []
    seen: set[str] = set()
    for desc in ordered:
        if not desc.name or desc.name in seen or desc.name not in initial_names:
            continue
        if spec_for_tool(desc.name).surface == "excluded":  # disabled via OUROBOROS_DISABLED_TOOLS
            continue
        result.append(desc.schema)
        seen.add(desc.name)

    extension_budget = int(settings.OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET)
    extension_bytes = 0
    for desc in configured:
        if not desc.origin.startswith("ext:"):
            continue
        encoded = len(json.dumps(desc.schema, ensure_ascii=False, default=str))
        if extension_bytes + encoded > extension_budget:
            log.warning("Skipping initial extension tool schema %s: extension schema budget exceeded", desc.name)
            continue
        extension_bytes += encoded
        result.append(desc.schema)
        seen.add(desc.name)
    return result, seen


def _non_core_count(descriptors: list[ToolDescriptor]) -> int:
    """The count the system note quotes: every discoverable tool, across all sources.

    Deliberately *not* minus the active set, mirroring the old `list_non_core_tools`. That
    means a discoverable tool promoted into the first prompt by config is counted as
    "additional" here while `list_available_tools` will not list it — a pre-existing
    off-by-one in the note's wording, preserved rather than quietly corrected.
    """
    return len({d.name for d in descriptors if d.name and d.surface == "discoverable"})


def bind_tool_surface(
    catalog: ToolCatalog,
    tool_ctx: ToolContext,
    messages: list[dict[str, Any]],
) -> tuple[list[Any], set[str]]:
    """Form the starting prompt surface and attach it to this task's ``ToolContext``.

    The one place the initial surface is decided. Returns ``(tool_schemas,
    active_tool_names)`` for ``RunState``; the tool surface it installs on ``tool_ctx``
    lets ``list_available_tools`` / ``enable_tools`` read the catalog without a registry.
    """
    active_tool_names: set[str] = set()
    surface = ProviderToolSurface(catalog, active_tool_names, tool_ctx.scope, tool_ctx.skill_origins)
    descriptors = list(surface.catalog())
    tool_schemas, initial_names = _initial_tool_schemas(descriptors, tool_ctx)
    active_tool_names.update(initial_names)
    tool_ctx.tool_surface = surface

    non_core_count = _non_core_count(descriptors)
    if non_core_count > 0:
        merge_system_note(
            messages,
            (
                f"Note: You have {len(tool_schemas)} core tools loaded. "
                f"There are {non_core_count} additional tools available "
                f"(use `list_available_tools` to see them, `enable_tools` to activate). "
                f"Core tools cover most tasks. Enable extras only when needed."
            ),
        )
    return tool_schemas, active_tool_names


def resolve_task_lineage(drive_root: pathlib.Path | None, task: dict[str, Any]) -> tuple[str | None, str]:
    """Resolve this task's ``(parent_task_id, root_task_id)`` from the task and its record.

    Called once per run, by ``build_task_run``, and handed to both halves that need it —
    the task-record write on ``EventSinkAdapter`` and the ``ToolContext`` the assembler
    builds (D4). Deriving it in each would mean two durable reads of the same record for
    one task.

    A record that cannot be read is not an error here: the task dict's own fields, and
    failing those the task's own id, are enough to root the lineage.
    """
    task_id = str(task.get("id") or "")
    try:
        existing_task = load_task_result(drive_root, task_id) if task_id else {}
    except Exception:
        log.debug("Failed to load existing task status for %s", task_id, exc_info=True)
        existing_task = {}
    parent_task_id = (
        str(task.get("parent_task_id") or (existing_task or {}).get("parent_task_id") or "").strip() or None
    )
    root_task_id = str(task.get("root_task_id") or (existing_task or {}).get("root_task_id") or "").strip()
    if not root_task_id:
        root_task_id = parent_task_id or task_id
    return parent_task_id, root_task_id


def build_task_run(
    agent: OuroborosAgent,
    task: dict[str, Any],
    *,
    scope: OwnerScope,
    task_workspace: pathlib.Path | None,
    start_time: float,
    drive_logs: pathlib.Path,
    cancellation: CancellationSignal,
) -> tuple[GraphContext, RunState[ToolContext]]:
    """Build the per-task ``GraphContext`` + ``RunState`` for one ``invoke`` (DA8).

    Mirrors ``run_llm_loop``'s ``RunConfig`` / ``GraphContext`` construction, but
    with terminal ``emit`` and an ``EventSinkAdapter`` that can publish the final
    result. The seven adapters close over the catalog / llm / queues / ids and take
    the ``ToolContext`` as an argument, so the whole context is constructible up
    front, before any task state exists. Messages start empty (``open_transcript``
    fills them, or ``resume`` from the snapshot); tool schemas and active tool names start
    empty and are filled by ``prepare_tools`` through ``ToolRunner.bind_surface``.
    """
    task_id = str(task.get("id") or "")
    task_type = str(task.get("type") or "").lower()
    initial_effort = resolve_effort(scope)

    # One durable read for the whole run: the record write and the built ToolContext both
    # need the lineage, and neither derives it again (D4).
    parent_task_id, root_task_id = resolve_task_lineage(agent.env.drive_root, task)

    active_model = agent.llm.default_model(scope)
    active_effort = initial_effort
    # A subtask runs on the model of the skill it was ordered under; the root never does, whatever its text says.
    if int(task.get("depth") or 0) > 0:
        profile = skill_run_profile(task.get("description"))
        if profile is not None:
            active_model, active_effort = profile[0], profile[1] or initial_effort

    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    active_use_local = str(settings.USE_LOCAL_MAIN or "").lower() in ("true", "1")

    from ouroboros.llm.provider_models import get_model_context_tokens

    _ctx_tokens = get_model_context_tokens()
    _emergency_raw = str(settings.OUROBOROS_EMERGENCY_COMPACTION_CHARS or "").strip()
    _emergency_use_chars = bool(_emergency_raw)
    if _emergency_use_chars:
        _emergency_threshold = int(_emergency_raw)
    else:
        _emergency_threshold = int(_ctx_tokens * 0.75)

    max_retries = 3
    try:
        MAX_ROUNDS = max(1, int(settings.OUROBOROS_MAX_ROUNDS or 200))
    except (ValueError, TypeError):
        MAX_ROUNDS = 200
        log.warning("Invalid OUROBOROS_MAX_ROUNDS, defaulting to 200")
    settings_for_run = user_settings_factory.settings()
    reasoning_progress = settings_for_run.OUROBOROS_REASONING_PROGRESS_ENABLED(scope)
    confirmation_gate = settings_for_run.OUROBOROS_TOOL_CONFIRMATION_ENABLED(scope)
    fallback_model = settings_for_run.OUROBOROS_MODEL_FALLBACK(scope).strip()
    fallback_use_local = str(settings.USE_LOCAL_FALLBACK or "").lower() in ("true", "1")

    # Per-task tool-execution state. No tool touches it, so it is owned by the adapters
    # rather than carried on ToolContext: the ledger is shared by its writer (the tool
    # runner) and its reader (the compactor's ledger note), which is why it is built
    # here. The repeat guard has a single owner — the executor — and is built inline.
    task_ledger = TaskLedger()

    run_skill_surface = _build_task_skill_surface(
        snapshot=agent.skill_snapshot,
        availability=agent.skill_availability,
        scope=scope,
        settings=settings_for_run,
    )
    skill_origins = admitted_skill_origins(run_skill_surface)

    config = RunConfig(
        max_rounds=MAX_ROUNDS,
        model_context_tokens=_ctx_tokens,
        emergency_threshold=_emergency_threshold,
        emergency_use_chars=_emergency_use_chars,
        emergency_char_threshold=_EMERGENCY_CHAR_THRESHOLD,
        fallback_model=fallback_model,
        fallback_use_local=fallback_use_local,
        task_id=task_id,
        task_type=task_type,
        scope=scope,
        drive_root=agent.env.drive_root,
        drive_logs=drive_logs,
        max_retries=max_retries,
        light_model=get_light_model(scope),
        default_model=active_model,
        default_effort=active_effort,
        default_use_local=active_use_local,
        reasoning_progress=reasoning_progress,
        terminal="emit",
    )
    # One sink for the whole run: the graph reports its own events through it, and tool
    # execution its live logs, so both halves of a round appear on one channel.
    events = EventSinkAdapter(
        emit_progress=agent._progress_emitter(scope, task.get("transport") or {}, task_id),
        events=agent._events,
        task_id=task_id,
        drive_logs=drive_logs,
        # From the registry, not an argument: nothing between the composition
        # root and this builder has a reason to carry a memory publisher.
        memory_events=memory_event_publisher_factory.memory_publisher,
        execution_memory_repository=agent.execution_memory_repository,
        scope=scope,
        env=agent.env,
        llm=agent.llm,
        pending_events=agent._pending_events,
        task=task,
        start_time=start_time,
        parent_task_id=parent_task_id,
        root_task_id=root_task_id,
        cancellation=cancellation,
        audit_event_repository=agent.audit_event_repository,
    )
    executor = build_tool_call_executor(
        catalog=tool_catalog,
        execution_memory_repository=agent.execution_memory_repository,
        events=events,
        task_ledger=task_ledger,
        repeat_guard=RepeatToolGuard(),
        confirmation_gate=confirmation_gate,
        audit_event_repository=agent.audit_event_repository,
    )
    graph_ctx = GraphContext(
        llm=LLMProviderAdapter(
            llm=agent.llm,
            drive_logs=drive_logs,
            task_id=task_id,
            task_type=task_type,
            events=agent._events,
            max_retries=max_retries,
            fallback_model=fallback_model,
            fallback_use_local=fallback_use_local,
            scope=scope,
            middlewares=[factory() for factory in llm_middlewares],
            cancellation=cancellation,
            tool_context=lambda: state.tool_ctx,
            audit_event_repository=agent.audit_event_repository,
        ),
        tools=ToolRunnerAdapter(executor=executor, drive_root=agent.env.drive_root, cancellation=cancellation),
        confirmations=ToolConfirmationBroker(
            executor=executor,
            claim_token=str(task.get("execution_claim") or ""),
            tool_confirmation_reader=agent.tool_confirmation_reader,
        ),
        compactor=TranscriptCompactorAdapter(
            drive_root=agent.env.drive_root,
            task_id=task_id,
            task_ledger=task_ledger,
            tool_context=lambda: state.tool_ctx,
            light_model=config.light_model,
            cancellation=cancellation,
        ),
        assembler=ContextAssemblerAdapter(
            agent=agent,
            root_task_id=root_task_id,
            light_model=config.light_model,
            skill_surface=run_skill_surface,
            skill_origins=skill_origins,
            cancellation=cancellation,
        ),
        inbox=OwnerInboxAdapter(
            incoming_messages=agent._incoming_messages,
            drive_root=agent.env.drive_root,
            task_id=task_id,
            events=agent._events,
            scope=scope,
        ),
        events=events,
        config=config,
        cancellation=cancellation,
    )

    state: RunState[ToolContext] = make_run_state(config, messages=[], tool_schemas=[], active_tool_names=set())
    state.task = task
    state.task_workspace = task_workspace
    # Bind the run's totals before `prepare_context` builds the ToolContext, so the events
    # channel a tool reports through accumulates into the same dict the loop does. A model
    # call a tool makes (vlm_query) is the task's spend as much as the loop's, and this is
    # the first point where the state that owns those totals exists.
    agent._task_usage = state.accumulated_usage
    return graph_ctx, state


__all__ = ["build_task_run", "resolve_task_lineage"]
