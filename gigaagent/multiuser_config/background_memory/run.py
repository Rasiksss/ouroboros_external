"""Run background memory as this process's only component.

    python -m multiuser_config.background_memory.run

What it consumes follows the deployment's one broker selector (see ``dependencies.py``): on the
default it composes the in-process consumer, whose queue has no writer in a process that
produces nothing, so it starts a loop that consumes nothing and says so at startup.

The loop drains on the way out. Every delivery still running may hold a consolidation lease,
and one abandoned at shutdown parks its ownership pair for every instance of the deployment
until that lease expires — so an ordinary redeploy would park every pair that happened to be
busy. ``MemoryRuntime.execute`` waits them out within its own bound and cancels what outlasts
it; this root awaits that task rather than letting the process report itself stopped first.

The process's main thread runs the event loop and the memory loop is a task on it: the
package's repositories and its consumer are coroutines, and one asynchronous engine may be
driven from exactly one event loop (`.rules/background-memory.md`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from infra.async_engine import async_engine_factory
from multiuser_config.background_memory.dependencies import BackgroundMemoryDependencies, build_dependencies
from multiuser_config.bootstrap import (
    ProcessBootstrap,
    bootstrap_process,
    install_stop_signal,
    serve_probes,
    stop_process_monitoring,
)
from new_core.coordination.inprocess import InProcessBudget
from new_core.coordination.registry import concurrency_budgets, default_budget_limits
from ouroboros.observability.health import LoopHeartbeat, WorkerLoopProbes
from ouroboros.storage.probe_database import probe_database_factory
from ouroboros.storage.runtime_persistence import runtime_persistence

log = logging.getLogger(__name__)

#: How this component names itself in the logs and in the bootstrap's refusals.
COMPONENT = "background-memory"

#: Connections this tier asks the runtime database for. A delivery holds one at a time and
#: deliveries for different pairs run concurrently, so this is a throughput choice and not a
#: count of what runs at once; the probe's engine is its own (`NullPool`) and nothing else in
#: this process holds one. The overflow covers the shielded lease release of a delivery the
#: drain cancelled.
DATABASE_POOL_SIZE = 4
DATABASE_MAX_OVERFLOW = 4


def main() -> int:
    boot = bootstrap_process(COMPONENT)
    return asyncio.run(_serve(boot))


async def _serve(boot: ProcessBootstrap) -> int:
    from background_memory.handlers.consolidation import lease_expiry_sec, memory_drain_timeout_sec
    from background_memory.runtime import MemoryRuntime

    deps: BackgroundMemoryDependencies | None = None
    stop_probes: Callable[[], None] | None = None
    try:
        async_engine_factory.initialize(
            _runtime_db_urls(boot), pool_size=DATABASE_POOL_SIZE, max_overflow=DATABASE_MAX_OVERFLOW
        )
        _compose_budgets(boot)
        probe_database = probe_database_factory.initialize_beside_store(boot.settings)
        stop_event = threading.Event()
        install_stop_signal(stop_event)
        deps = build_dependencies(
            drive_root=boot.drive_root,
            repo_dir=boot.repo_dir,
            stop_event=stop_event,
            engine=async_engine_factory.engine,
        )
        # The loop beats for the oldest delivery in flight, so the bound is the lease that a
        # delivery takes: a delivery stuck past it is a holder the fleet has already given up on.
        heartbeat = LoopHeartbeat(COMPONENT, stall_after_sec=lease_expiry_sec(boot.settings))
        stop_probes = serve_probes(WorkerLoopProbes(heartbeat, probe_database), tier=COMPONENT)
        log.info("Background memory starting kinds=%d", len(deps.handlers))
        if stop_probes is None:
            return 1
        runtime = MemoryRuntime(
            deps.consumer,
            handlers=deps.handlers,
            stop_event=stop_event,
            heartbeat=heartbeat,
            drain_timeout_sec=memory_drain_timeout_sec(boot.settings),
        )
        await runtime.execute()
    finally:
        try:
            # After the loop has drained, so no delivery is still reading through it.
            if deps is not None:
                await deps.release()
        finally:
            try:
                if stop_probes is not None:
                    stop_probes()
            finally:
                await probe_database_factory.dispose()
                await async_engine_factory.dispose()
                runtime_persistence.close()
                stop_process_monitoring()
    return 0


def _compose_budgets(boot: ProcessBootstrap) -> None:
    """The ceiling on this tier's model calls, stated rather than inherited.

    This tier runs no task slots, so the model-call number is the operator's
    ``OUROBOROS_LLM_MAX_CONCURRENCY`` and nothing else; left uncomposed the seam's fallback
    would hold every background model call of the fleet at two, with no setting able to
    move it (`new_core/coordination/registry.py`).
    """
    limits = default_budget_limits(
        worker_slots=1,
        llm_max_concurrency=int(boot.settings.get("OUROBOROS_LLM_MAX_CONCURRENCY", 0) or 0),
    )
    concurrency_budgets.initialize(InProcessBudget(limits))
    log.info("Background memory concurrency budgets composed limits=%s", limits)


def _runtime_db_urls(boot: ProcessBootstrap) -> list[str]:
    from ouroboros.storage.db_url import canonicalize_runtime_db_urls, collect_runtime_db_urls

    return canonicalize_runtime_db_urls(collect_runtime_db_urls(boot.settings))


if __name__ == "__main__":
    raise SystemExit(main())
