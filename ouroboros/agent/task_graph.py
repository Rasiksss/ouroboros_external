"""The whole-task graph: context assembly, tool binding, the LLM loop, and emission.

This is the ouroboros-side assembly of the task lifecycle onto the ``new_core``
graph engine (option A, DA1). Glue nodes bracket the reused, unchanged loop
sub-topology (``register_loop_topology``):

    START → prepare_context → prepare_tools → guard → … (loop) …
        ─(terminal ``emit``)─► emit → END

Of the three bracket nodes only two are authored here. ``prepare_context`` runs
``Agent._prepare_task_context`` — the ToolContext, memory recall and the transcript,
and nothing else — and ``emit`` publishes the final result through the effects
interface's ``emit_result`` member (DA5). Both may import ``ouroboros`` freely.

``prepare_tools`` is not glue and lives in ``new_core/loop/nodes.py``: it reaches the
surface decision through ``ToolRunner.bind_surface`` like any other loop node reaches
its effects. What stays here is that decision's *implementation* —
``bind_tool_surface`` and the policy it applies — which ``ToolRunnerAdapter`` calls.
This module registers the node, because the standalone loop is entered with its
surface already formed and ``register_loop_topology`` therefore does not.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from new_core.graph import END, START, BuiltGraph, Graph
from new_core.loop import (
    CancellationSignal,
    GraphContext,
    RunConfig,
    RunState,
    make_run_state,
    nodes,
    register_loop_topology,
)
from new_core.loop.transcript_merge import merge_system_note
from new_core.tools.catalog import ToolCatalog, tool_catalog
from new_core.tools.interfaces import ToolDescriptor
from ouroboros.agent.loop_adapters import (
    EventSinkAdapter,
    LLMProviderAdapter,
    OwnerInboxAdapter,
    ToolRunnerAdapter,
    TranscriptCompactorAdapter,
    build_tool_call_executor,
)
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger
from ouroboros.app.config import get_light_model, resolve_effort
from ouroboros.policy.capabilities import CORE_TOOL_NAMES
from ouroboros.storage.settings.user_settings import SETTINGS_OWNER_ID, user_settings
from ouroboros.tools.catalog import core_tool_names, skill_repair_tool_names, spec_for_tool
from ouroboros.tools.surface import ProviderToolSurface

if TYPE_CHECKING:
    import pathlib

    from new_core.tools.context import ToolContext
    from ouroboros.agent.core import OuroborosAgent

log = logging.getLogger(__name__)

# Emergency compaction fallback: when the char-based override is not configured, this is
# the transcript size (in characters) past which the loop compacts aggressively.
_EMERGENCY_CHAR_THRESHOLD = 1_200_000


def _make_prepare_context_node(agent: OuroborosAgent):
    """Build the ``prepare_context`` node as a closure over the stable Agent (DA4).

    Phase 2 only — ToolContext, memory recall, ``build_llm_messages``. Per-task data
    flows through ``RunState`` (``task`` / ``task_workspace`` in, ``messages`` /
    ``tool_ctx`` / ``cap_info`` out), never through the closure. The registry is
    ``prepare_tools``' business; this node never reaches for ``agent.tools``.
    """

    def prepare_context(state: RunState[ToolContext], ctx: GraphContext) -> None:
        # Cover the context-build window (incl. memory recall) before the first loop round.
        ctx.events.typing()

        tool_ctx, messages, cap_info = agent._prepare_task_context(state.task, state.task_workspace)

        state.messages = messages
        state.tool_ctx = tool_ctx
        state.cap_info = cap_info

    return prepare_context


ENABLED_TOOLS_ENV = "OUROBOROS_ENABLED_TOOLS"
INITIAL_EXTENSION_SCHEMA_BUDGET = 8_000


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


def _local_descriptors(descriptors: list[ToolDescriptor]) -> list[ToolDescriptor]:
    """Repo-defined and skill tools — the set the registry used to hold in ``_entries``.

    Extension and MCP tools are deliberately not here: the first-request surface never
    drew on them through this path, and the extension budget below adds them separately.
    """
    return [d for d in descriptors if d.origin == "builtin" or d.origin.startswith("skill:")]


def _configured_initial_tool_names(descriptors: list[ToolDescriptor]) -> frozenset[str]:
    """Tools promoted into the first prompt by config, never by permission."""
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    enabled_tools = set(_parse_enabled_tools(settings.OUROBOROS_ENABLED_TOOLS))

    # Compatibility for pre-catalog opt-in flags such as TOOL_CALCULATOR_ENABLED.
    # Falsey legacy values no longer disable installed tools; they just do not
    # promote discoverable schemas into the first prompt.
    local = _local_descriptors(descriptors)
    for desc in local:
        prefix = (desc.env_prefix or "").strip()
        if prefix and _env_true(settings.get(f"{prefix}_ENABLED")):
            enabled_tools.add(desc.name.strip())

    allowed = set(core_tool_names()) | {d.name for d in local if d.surface == "discoverable"}
    return frozenset(name for name in enabled_tools if name in allowed and spec_for_tool(name).surface != "excluded")


def _initial_tool_schemas(descriptors: list[ToolDescriptor], tool_ctx: ToolContext) -> tuple[list[Any], set[str]]:
    """The schema set the first model request carries, plus its names.

    The whole starting-set decision lives here: the task constraint (heal mode keeps six
    names and drops extension and MCP entirely), the attachment rule that adds
    ``vlm_query``, the configured names, and the extension schema budget — a single budget
    across all extension tools, which is why no provider can decide this for itself.
    """
    task_constraint = tool_ctx.task_constraint
    repair_mode = task_constraint is not None and task_constraint.mode == "skill_repair"

    if repair_mode:
        initial_names = set(skill_repair_tool_names())
    else:
        initial_names = set(core_tool_names()) | set(_configured_initial_tool_names(descriptors))
        attachments = tool_ctx.attachments or []
        has_image = any(
            isinstance(item, dict) and (item.get("kind") == "image" or str(item.get("mime") or "").startswith("image/"))
            for item in attachments
        )
        if has_image:
            initial_names.add("vlm_query")
        else:
            initial_names.discard("vlm_query")

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

    if repair_mode:
        return result, seen

    extension_bytes = 0
    for desc in descriptors:
        if not desc.origin.startswith("ext:") or not desc.name or desc.name in seen:
            continue
        if spec_for_tool(desc.name).surface == "excluded":  # disabled via OUROBOROS_DISABLED_TOOLS
            continue
        encoded = len(json.dumps(desc.schema, ensure_ascii=False, default=str))
        if extension_bytes + encoded > INITIAL_EXTENSION_SCHEMA_BUDGET:
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
    descriptors = catalog.descriptors()
    tool_schemas, active_tool_names = _initial_tool_schemas(descriptors, tool_ctx)
    tool_ctx.tool_surface = ProviderToolSurface(catalog, active_tool_names)

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


def emit(state: RunState[ToolContext], ctx: GraphContext) -> None:
    """Publish the task's terminal outcome through the effects interface (DA5).

    Reached as the loop sub-topology's configured terminal (``ctx.config.terminal
    == "emit"``); reads the final answer, usage, trace, and tool context off
    ``RunState`` and hands them to ``emit_result``, which applies the empty-text
    fallback and runs end-of-task publication. Routes to ``END``.
    """
    assert state.tool_ctx is not None  # the loop only reaches a terminal after prepare_context
    ctx.events.emit_result(
        text=state.final_content,
        usage=state.accumulated_usage,
        trace=state.llm_trace,
        tool_ctx=state.tool_ctx,
    )


def build_task_graph(agent: OuroborosAgent) -> BuiltGraph[GraphContext, RunState[ToolContext]]:
    """Assemble the whole-task graph once, closing glue nodes over ``agent`` (DA2)."""
    graph: Graph[GraphContext, RunState[ToolContext]] = Graph()

    graph.add_node("prepare_context", _make_prepare_context_node(agent))
    # A loop-layer node, not glue: it reaches the surface decision through `ToolRunner`.
    # Registered here rather than by `register_loop_topology` because the standalone loop
    # is entered with its surface already formed.
    graph.add_node("prepare_tools", nodes.prepare_tools)
    register_loop_topology(graph)
    graph.add_node("emit", emit)

    graph.add_edge(START, "prepare_context")
    graph.add_edge("prepare_context", "prepare_tools")
    graph.add_edge("prepare_tools", "guard")
    graph.add_edge("emit", END)

    return graph.build()


def build_task_run(
    agent: OuroborosAgent,
    task: dict[str, Any],
    *,
    task_workspace: pathlib.Path | None,
    start_time: float,
    drive_logs: pathlib.Path,
    cancellation: CancellationSignal,
) -> tuple[GraphContext, RunState[ToolContext]]:
    """Build the per-task ``GraphContext`` + ``RunState`` for one ``invoke`` (DA8).

    Mirrors ``run_llm_loop``'s ``RunConfig`` / ``GraphContext`` construction, but
    with terminal ``emit`` and an ``EventSinkAdapter`` that can publish the final
    result. The five adapters close over the catalog / llm / queues / ids and take
    the ``ToolContext`` as an argument, so the whole context is constructible up
    front, before any task state exists. Messages start empty (``prepare_context``
    fills them); tool schemas and active tool names start empty and are filled by
    ``prepare_tools`` through ``ToolRunner.bind_surface``.
    """
    task_id = str(task.get("id") or "")
    task_type = str(task.get("type") or "").lower()
    initial_effort = resolve_effort()

    active_model = agent.llm.default_model()
    active_effort = initial_effort

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
    fallback_model = user_settings.OUROBOROS_MODEL_FALLBACK(SETTINGS_OWNER_ID).strip()
    fallback_use_local = str(settings.USE_LOCAL_FALLBACK or "").lower() in ("true", "1")

    # Per-task tool-execution state. No tool touches it, so it is owned by the adapters
    # rather than carried on ToolContext: the ledger is shared by its writer (the tool
    # runner) and its reader (the compactor's ledger note), which is why it is built
    # here. The repeat guard has a single owner — the executor — and is built inline.
    task_ledger = TaskLedger()

    # One catalog per process (wired by the server lifespan), refreshed here rather than
    # rebuilt: the refresh is the few milliseconds of filesystem walk that lets a skill
    # enabled since the last task show up, and it is what the per-task `ToolCatalog`
    # rebuild used to buy. The catalog holds no task state, so sharing it is safe.
    tool_catalog.refresh()

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
        drive_root=agent.env.drive_root,
        drive_logs=drive_logs,
        max_retries=max_retries,
        light_model=get_light_model(),
        default_model=active_model,
        default_effort=active_effort,
        default_use_local=active_use_local,
        terminal="emit",
    )
    # One sink for the whole run: the graph reports its own events through it, and tool
    # execution its live logs, so both halves of a round appear on one channel.
    events = EventSinkAdapter(
        emit_progress=agent._emit_progress,
        event_queue=agent._event_queue,
        task_id=task_id,
        drive_logs=drive_logs,
        env=agent.env,
        llm=agent.llm,
        pending_events=agent._pending_events,
        task=task,
        start_time=start_time,
    )
    graph_ctx = GraphContext(
        llm=LLMProviderAdapter(
            llm=agent.llm,
            drive_logs=drive_logs,
            task_id=task_id,
            task_type=task_type,
            event_queue=agent._event_queue,
            max_retries=max_retries,
            fallback_model=fallback_model,
            fallback_use_local=fallback_use_local,
        ),
        tools=ToolRunnerAdapter(
            executor=build_tool_call_executor(
                catalog=tool_catalog,
                drive_logs=drive_logs,
                events=events,
                task_ledger=task_ledger,
                repeat_guard=RepeatToolGuard(),
            ),
            drive_root=agent.env.drive_root,
        ),
        compactor=TranscriptCompactorAdapter(
            drive_root=agent.env.drive_root,
            task_id=task_id,
            task_ledger=task_ledger,
        ),
        inbox=OwnerInboxAdapter(
            incoming_messages=agent._incoming_messages,
            drive_root=agent.env.drive_root,
            task_id=task_id,
            event_queue=agent._event_queue,
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


__all__ = ["build_task_graph", "build_task_run", "emit"]
