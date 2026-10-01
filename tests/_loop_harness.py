"""Assemble and run the loop sub-graph in isolation, for tests.

``run_llm_loop`` used to be this assembly, and production no longer has it: ``handle_task``
invokes the whole task graph through ``build_task_run``. The loop nodes, routers, and
adapters it wires are still production code, and several suites need them without the
task-level bracket (compaction policy, language repair, the outcome ledger). That
assembly lives here now, as scaffolding rather than a shipped entry point.

The task-level behaviour — context preparation, tool binding, result emission, cleanup —
belongs to ``test_task_graph``, which drives ``Agent.handle_task``.
"""

from __future__ import annotations

import pathlib
import queue
from typing import Any

from new_core.loop import (
    GraphContext,
    RunConfig,
    get_loop_graph,
    make_run_state,
    max_steps_for,
)
from new_core.tools.catalog import ToolCatalog
from ouroboros.agent.loop_adapters import (
    EventSinkAdapter,
    LLMProviderAdapter,
    OwnerInboxAdapter,
    ToolRunnerAdapter,
    TranscriptCompactorAdapter,
    build_tool_call_executor,
)
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger
from tests._shared import FakeCancellation, make_catalog


def drive_loop(
    *,
    messages: list[dict[str, Any]],
    tool_ctx: Any,
    llm: Any,
    catalog: ToolCatalog | None = None,
    drive_logs: Any = None,
    emit_progress=lambda _text: None,
    incoming_messages: queue.Queue | None = None,
    task_type: str = "",
    task_id: str = "test",
    event_queue: queue.Queue | None = None,
    initial_effort: str = "medium",
    drive_root: pathlib.Path | None = None,
    tool_schemas: list[Any] | None = None,
    active_tool_names: set[str] | None = None,
    max_rounds: int | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Run the loop graph over ``messages``; return (final text, usage, trace).

    Mirrors what ``run_llm_loop`` assembled: the five adapters, a ``RunConfig`` from
    settings, and one ``invoke`` of the shared loop graph.
    """
    from ouroboros.agent.task_graph import _EMERGENCY_CHAR_THRESHOLD
    from ouroboros.app.config import get_light_model
    from ouroboros.llm.provider_models import get_model_context_tokens
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    active_model = llm.default_model()
    active_use_local = str(settings.USE_LOCAL_MAIN or "").lower() in ("true", "1")

    ctx_tokens = get_model_context_tokens()
    emergency_raw = str(settings.OUROBOROS_EMERGENCY_COMPACTION_CHARS or "").strip()
    emergency_use_chars = bool(emergency_raw)
    emergency_threshold = int(emergency_raw) if emergency_use_chars else int(ctx_tokens * 0.75)

    try:
        rounds = max_rounds if max_rounds is not None else max(1, int(settings.OUROBOROS_MAX_ROUNDS or 200))
    except (ValueError, TypeError):
        rounds = 200

    catalog = catalog if catalog is not None else make_catalog([])
    task_ledger = TaskLedger()

    config = RunConfig(
        max_rounds=rounds,
        model_context_tokens=ctx_tokens,
        emergency_threshold=emergency_threshold,
        emergency_use_chars=emergency_use_chars,
        emergency_char_threshold=_EMERGENCY_CHAR_THRESHOLD,
        fallback_model=(settings.OUROBOROS_MODEL_FALLBACK or "").strip(),
        fallback_use_local=str(settings.USE_LOCAL_FALLBACK or "").lower() in ("true", "1"),
        task_id=task_id,
        task_type=task_type,
        drive_root=drive_root,
        drive_logs=drive_logs,
        max_retries=3,
        light_model=get_light_model(),
        default_model=active_model,
        default_effort=initial_effort,
        default_use_local=active_use_local,
    )

    # One sink for the run, as production wires it: the graph's events and the tool live
    # logs go out the same channel.
    events = EventSinkAdapter(
        emit_progress=emit_progress,
        event_queue=event_queue,
        task_id=task_id,
        drive_logs=drive_logs,
    )

    graph_ctx = GraphContext(
        llm=LLMProviderAdapter(
            llm=llm,
            drive_logs=drive_logs,
            task_id=task_id,
            task_type=task_type,
            event_queue=event_queue,
            max_retries=3,
            fallback_model=config.fallback_model,
            fallback_use_local=config.fallback_use_local,
        ),
        tools=ToolRunnerAdapter(
            executor=build_tool_call_executor(
                catalog=catalog,
                drive_logs=drive_logs,
                events=events,
                task_ledger=task_ledger,
                repeat_guard=RepeatToolGuard(),
            ),
            drive_root=drive_root,
        ),
        compactor=TranscriptCompactorAdapter(
            drive_root=drive_root,
            task_id=task_id,
            task_ledger=task_ledger,
        ),
        inbox=OwnerInboxAdapter(
            incoming_messages=incoming_messages if incoming_messages is not None else queue.Queue(),
            drive_root=drive_root,
            task_id=task_id,
            event_queue=event_queue,
        ),
        events=events,
        config=config,
        cancellation=FakeCancellation(),
    )

    state = make_run_state(
        config,
        messages=messages,
        tool_schemas=tool_schemas if tool_schemas is not None else [],
        active_tool_names=active_tool_names if active_tool_names is not None else set(),
    )
    state.tool_ctx = tool_ctx
    state.pending_overrides = graph_ctx.tools.drain_overrides(tool_ctx)

    get_loop_graph().invoke(state, graph_ctx, max_steps=max_steps_for(rounds))
    return state.final_content or "", state.accumulated_usage, state.llm_trace
