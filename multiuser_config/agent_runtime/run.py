"""Run the agent runtime as this process's only component.

    python -m multiuser_config.agent_runtime.run

What it reports through follows the deployment's one broker selector (see
``dependencies.py``): on the default it composes the in-process pair, whose reader is in no
other process, so it starts a runtime whose reports reach nobody.

What a real deployment adds here and cannot inherit from the single-process server: the
asyncio loop the coroutine tool clients need, which this process owns for its lifetime
(`tool_client_loop.py`) and hands to the providers as it composes them. MCP is connected
that way, as is outbound A2A. The extension load the single-process server performs through
its skill bridge is not part of this tier.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack, ExitStack
from time import monotonic
from typing import Any

from multiuser_config.agent_runtime.dependencies import build_dependencies
from multiuser_config.agent_runtime.tool_client_loop import start_tool_client_loop
from multiuser_config.bootstrap import (
    ProcessBootstrap,
    bootstrap_process,
    install_stop_signal,
    serve_probes,
    stop_process_monitoring,
)
from new_core.skills.catalog import SkillCatalog, SkillCatalogSnapshot
from new_core.skills.interfaces import IExtensionRuntime
from new_core.skills.providers import DistributionSkillProvider
from new_core.skills.surface import SkillAvailability, SkillSurface
from ouroboros.observability.health import LoopHeartbeat, WorkerLoopProbes
from ouroboros.skills.availability import build_skill_availability
from ouroboros.skills.loader import DistributionSkillLoader
from ouroboros.storage.blob.async_backend import open_file_blob
from ouroboros.storage.blob.factory import close_storage_provider, initialize_storage_provider
from ouroboros.storage.probe_database import probe_database_factory
from ouroboros.storage.runtime_persistence import runtime_persistence

log = logging.getLogger(__name__)

#: How this component names itself in the logs and in the bootstrap's refusals.
COMPONENT = "agent-runtime"

#: How long the exit waits for the tool clients to close their sessions.
_SHUTDOWN_TIMEOUT_SEC = 10.0
_retained_resources: ExitStack | None = None


def _mcp_servers(boot: ProcessBootstrap) -> Sequence[Any]:
    """The MCP servers this process may connect to — the list, or nothing at all.

    The kill switch gates the *list* rather than the descriptors: the provider's refresh
    does not read it (only dispatch does, through the manager's availability check), so a
    tier that handed it the list regardless would open the very sessions the switch exists
    to prevent and only then decline to use them.
    """
    if not bool(boot.settings.get("MCP_ENABLED", False)):
        return ()
    from security_config.mcp import mcp_servers

    return mcp_servers


def _a2a_agents(*, egress_enabled: bool) -> Sequence[Any]:
    """The configured remote agents, without importing the list behind a disabled switch."""
    if not egress_enabled:
        return ()
    from security_config.a2a import a2a_remote_agents

    return a2a_remote_agents


def compose_process(
    boot: ProcessBootstrap,
    *,
    tool_client_loop: asyncio.AbstractEventLoop,
) -> tuple[SkillCatalogSnapshot, SkillAvailability, IExtensionRuntime]:
    """The process-wide singletons a run reads: what it may consume, and what it may call.

    ``tool_client_loop`` is this process's own: the providers whose clients are coroutine
    clients are handed it here, and connecting is part of composing rather than something
    the first task discovers, because under the mode the catalogue is refreshed exactly
    once — here.
    """
    from config_registry.user_settings.reader import user_settings_factory
    from infra.async_engine import async_engine_factory
    from infra.security_context import security_context
    from infra.security_context.sql import SqlSecurityContextStore
    from new_core.coordination.inprocess import InProcessBudget
    from new_core.coordination.registry import concurrency_budgets, default_budget_limits
    from new_core.identity import ServiceJob
    from new_core.tools.catalog import tool_catalog
    from new_core.tools.providers import (
        A2aToolProvider,
        BuiltinToolProvider,
        EmptyExtensionRuntime,
        ExtensionToolProvider,
        McpToolProvider,
        SkillToolProvider,
        frozen_tool_modules,
    )
    from ouroboros.app.config import get_a2a_tool_timeout_sec
    from ouroboros.app.egress_auth_composition import EgressAuthComposition
    from ouroboros.contracts.owner_scope import service_identity
    from ouroboros.egress.a2a.dependencies import A2aEgressDependencies
    from ouroboros.egress.a2a.manager import a2a_manager
    from ouroboros.egress.a2a.task_handles import A2aTaskHandles
    from ouroboros.egress.auth.interfaces import EgressAuthDependencies
    from ouroboros.storage.settings.profile import is_server_profile

    mcp_servers = _mcp_servers(boot)
    a2a_egress_enabled = boot.settings.A2A_EGRESS_ENABLED
    a2a_agents = _a2a_agents(egress_enabled=a2a_egress_enabled)
    async_engine_factory.initialize_beside_store(boot.settings)
    security_context_store = SqlSecurityContextStore(
        async_engine_factory.engine, encryption_key=boot.settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY
    )
    security_context.initialize(security_context_store)
    egress_auth = EgressAuthDependencies(
        security_context_store=security_context_store,
        user_token_store=EgressAuthComposition.user_token_store(
            [*mcp_servers, *a2a_agents],
            engine_factory=async_engine_factory,
            encryption_key=boot.settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY,
        ),
    )
    a2a_dependencies = A2aEgressDependencies(
        auth=egress_auth,
        task_handles=A2aTaskHandles(encryption_key=boot.settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY),
    )
    # What the runs may consume, in one place with the relationship between the numbers.
    worker_slots = max(1, int(boot.settings.get("OUROBOROS_WORKER_SLOTS", 1) or 1))
    budget_limits = default_budget_limits(
        worker_slots=worker_slots,
        llm_max_concurrency=int(boot.settings.get("OUROBOROS_LLM_MAX_CONCURRENCY", 0) or 0),
    )
    concurrency_budgets.initialize(InProcessBudget(budget_limits))
    multiuser = bool(boot.settings.get("OUROBOROS_MULTIUSER", False))
    skill_catalog = SkillCatalog()
    skill_catalog.initialize(
        [
            DistributionSkillProvider(
                distribution_root=boot.repo_dir / "skills",
                loader=DistributionSkillLoader(boot.settings),
            )
        ]
    )
    skill_availability = build_skill_availability(boot.settings)
    extension_runtime = EmptyExtensionRuntime()
    deployment_skill_surface = SkillSurface(
        snapshot=skill_catalog.snapshot(),
        availability=skill_availability,
    )
    tool_catalog.initialize(
        [
            BuiltinToolProvider(frozen_tool_modules(skill_execution=not multiuser)),
            SkillToolProvider(skill_surface=deployment_skill_surface),
            ExtensionToolProvider(
                skill_surface=deployment_skill_surface,
                extension_runtime=extension_runtime,
            ),
            McpToolProvider(
                servers=mcp_servers,
                server_ids_reader=user_settings_factory.settings().OUROBOROS_USER_ENABLED_MCP_IDS,
                auth_dependencies=egress_auth,
                loop=tool_client_loop,
            ),
            A2aToolProvider(
                a2a_manager,
                get_a2a_tool_timeout_sec(),
                agents=a2a_agents,
                loop=tool_client_loop,
                dependencies=a2a_dependencies,
                egress_enabled=a2a_egress_enabled,
            ),
        ]
    )
    startup_scope = service_identity(job=ServiceJob.STARTUP).as_scope()
    log.info(
        "Agent runtime process composed slots=%d tools=%d", worker_slots, len(tool_catalog.descriptors(startup_scope))
    )
    # Each agent process announces its ready inventory, including identical replicas.
    if is_server_profile():
        try:
            from ouroboros.app.agent_system_configuration_kafka import publish_agent_system_configuration_event

            publish_agent_system_configuration_event(logger=log, skill_surface=deployment_skill_surface)
        except Exception:
            log.warning("AGENT_SYSTEM_CONFIGURATION startup announcement failed", exc_info=True)
    return skill_catalog.snapshot(), skill_availability, extension_runtime


def main() -> int:
    global _retained_resources
    from ouroboros.agent_runtime.bootstrap import CLAIM_LOOP_STALL_SEC, build_agent_runtime
    from ouroboros.app.broker_composition import install_memory_event_publisher

    boot = bootstrap_process(COMPONENT)
    probe_database = probe_database_factory.initialize_beside_store(boot.settings)

    with ExitStack() as resources:
        resources.callback(runtime_persistence.close)
        install_memory_event_publisher()
        tool_client_loop, stop_tool_client_loop = start_tool_client_loop()
        resources.callback(stop_tool_client_loop)
        file_resources = AsyncExitStack()
        resources.callback(_shutdown_tool_clients, tool_client_loop, file_resources)
        file_blob = asyncio.run_coroutine_threadsafe(
            file_resources.enter_async_context(open_file_blob(boot.settings)), tool_client_loop
        ).result()
        initialize_storage_provider(boot.settings, file_client=file_blob.client, file_loop=tool_client_loop)
        resources.callback(close_storage_provider)
        skill_snapshot, skill_availability, extension_runtime = compose_process(boot, tool_client_loop=tool_client_loop)
        stop_event = threading.Event()
        install_stop_signal(stop_event)
        deps = build_dependencies(drive_root=boot.drive_root)
        heartbeat = LoopHeartbeat(COMPONENT, stall_after_sec=CLAIM_LOOP_STALL_SEC)
        runtime = build_agent_runtime(
            settings=dict(boot.settings),
            drive_root=boot.drive_root,
            repo_dir=boot.repo_dir,
            events=deps.events,
            skill_snapshot=skill_snapshot,
            skill_availability=skill_availability,
            extension_runtime=extension_runtime,
            outbound=deps.outbound,
            work_signal=deps.work_signal,
            stop_event=stop_event,
            heartbeat=heartbeat,
            task_stops=deps.task_stops,
        )
        stop_probes = serve_probes(WorkerLoopProbes(heartbeat, probe_database), tier=COMPONENT)
        log.info("Agent runtime starting runtime_id=%s", runtime.runtime_id)
        try:
            if stop_probes is None:
                # Inside the `try`, so the refusal leaves through the same exit a served run takes.
                return 1
            runtime.execute()
        finally:
            try:
                settled = False
                try:
                    settled = runtime.drain(deadline_sec=float(boot.settings.OUROBOROS_DRAIN_TIMEOUT_SEC))
                finally:
                    if not settled:
                        _retained_resources = resources.pop_all()
                if not settled:
                    raise TimeoutError("Agent bodies did not stop within the shutdown budget; shared clients retained")
            finally:
                try:
                    resources.close()
                finally:
                    try:
                        if stop_probes is not None:
                            stop_probes()
                    finally:
                        asyncio.run(probe_database_factory.dispose())
                        stop_process_monitoring()
        return 0


def _shutdown_tool_clients(loop: asyncio.AbstractEventLoop, file_resources: AsyncExitStack) -> None:
    """Close what this process opened concurrently within one shared exit budget."""
    from infra.async_engine import async_engine_factory
    from ouroboros.egress.a2a.manager import a2a_manager
    from ouroboros.mcp.manager import mcp_manager

    futures: list[tuple[str, Future[None]]] = []
    for name, shutdown in (
        ("file client", file_resources.aclose),
        ("MCP manager", mcp_manager.shutdown),
        ("A2A manager", a2a_manager.shutdown),
        ("async runtime DB engine", async_engine_factory.dispose),
    ):
        try:
            futures.append((name, asyncio.run_coroutine_threadsafe(shutdown(), loop)))
        except Exception:
            log.warning("%s shutdown could not be scheduled", name, exc_info=True)

    deadline = monotonic() + _SHUTDOWN_TIMEOUT_SEC
    for name, future in futures:
        try:
            future.result(timeout=max(0.0, deadline - monotonic()))
        except FutureTimeoutError:
            future.cancel()
            log.warning("%s shutdown timed out", name)
        except Exception:
            log.warning("%s did not shut down cleanly", name, exc_info=True)


if __name__ == "__main__":
    raise SystemExit(main())
