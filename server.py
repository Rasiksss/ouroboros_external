"""Headless Starlette/uvicorn entry point for supervisor runtime and health API."""

import asyncio
import logging
import os
import pathlib
import sys
import threading
import time
from collections.abc import Callable
from functools import partial

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.routing import Mount

from infra.cache import cache_factory
from ouroboros.app.config import init_config, load_settings
from ouroboros.observability.infra.repositories.audit_events.repository import SqlAuditEventRepository

init_config()

from ouroboros.app.config import (
    DATA_DIR,
    PORT_FILE,
    REPO_DIR,
    apply_settings_to_env as _apply_settings_to_env,
    get_a2a_tool_timeout_sec,
    get_config,
    get_storage_provider,
    is_server_profile,
    save_settings,
)

# The security config lives at the repo root, so it goes on the path before anything that
# imports it (the task graph does, through the gateway): the server lists must not depend
# on the working directory the process was started from.
sys.path.insert(0, str(REPO_DIR))

from background_memory.env import MemoryEnv
from background_memory.handlers.consolidation import ConsolidationHandler, lease_expiry_sec, memory_drain_timeout_sec
from background_memory.handlers.distillation import DistillationHandler
from background_memory.handlers.titles import PersistedMessageHandlers, TitleHandler
from background_memory.identity import mint_holder_id
from background_memory.infra.events import memory_event_publisher_factory
from background_memory.infra.events.inmemory import MAX_PENDING_EVENTS, InMemoryEventConsumer, MemoryEventQueue
from background_memory.infra.foreground import TaskTableForegroundPressure
from background_memory.infra.repositories import (
    SqlAuditRepo,
    SqlChatHistoryRepo,
    SqlConsolidationLeaseRepo,
    SqlContextSummaryRepo,
    SqlMemoryHeaderRepo,
    SqlTaskRepo,
)
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    ConsolidationContinuationEvent,
    MemoryDistillationEvent,
    MemoryEvent,
    MemoryEventHandler,
)
from background_memory.runtime import CANCELLED_RELEASE_GRACE_SEC, MemoryRuntime
from config_registry.agent_registry.api.http.endpoint import router as agents_router
from config_registry.agent_registry.infra.repositories.repo import SqlAgentRegistryRepo
from config_registry.chat_registry.api.http.endpoint import router as chats_router
from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryRepo, SqlChatRegistryStore
from config_registry.user_settings.api.http.endpoint import router as owner_settings_router
from config_registry.user_settings.reader import user_settings_factory
from infra.async_engine import async_engine_factory
from infra.migrations.runner import assert_schema_at_head
from infra.security_context import security_context, security_context_collector
from infra.security_context.collector import DeclaredParametersCollector
from infra.security_context.declaration import HeaderCollectorDeclaration
from infra.security_context.interface import ISecurityContextStore
from infra.security_context.sql import SqlSecurityContextStore
from infra.utils.http.fastapi import install_trace_middleware
from new_core.coordination.inprocess import InProcessBudget, InProcessWorkSignal
from new_core.coordination.registry import concurrency_budgets, default_budget_limits
from new_core.identity import OwnerScope, ServiceJob
from new_core.skills.catalog import SkillCatalog, SkillCatalogSnapshot
from new_core.skills.interfaces import IExtensionRuntime
from new_core.skills.permissions import CatalogSkillPermissionChecker
from new_core.skills.providers import DistributionSkillProvider
from new_core.skills.surface import SkillAvailability, SkillSurface
from new_core.tools.catalog import tool_catalog
from new_core.tools.providers import (
    A2aToolProvider,
    BuiltinToolProvider,
    ExtensionToolProvider,
    McpToolProvider,
    SkillToolProvider,
    frozen_tool_modules,
)
from ouroboros.agent_events.inmemory import create_agent_event_transport
from ouroboros.agent_runtime.bootstrap import CLAIM_LOOP_STALL_SEC, build_agent_runtime
from ouroboros.agent_runtime.runtime import AgentRuntime
from ouroboros.app.egress_auth_composition import EgressAuthComposition, EgressList
from ouroboros.app.infra.repositories.deployment_keyspace.repo import SqlDeploymentKeyspaceRepository
from ouroboros.app.multiuser_validation import validate_deployment_keyspace, validate_multiuser_configuration
from ouroboros.app.server_control import restart_current_process as _restart_current_process_impl
from ouroboros.app.server_entrypoint import find_free_port, parse_server_args, write_port_file
from ouroboros.app.task_stop_notifications import TaskStopNotifier, create_task_stop_transport
from ouroboros.app.transport_lifecycle import (
    StartedTransports,
    report_missing_dialogs_transport,
    stop_transports_when_supervisor_exits,
)
from ouroboros.app.transport_wiring import (
    CoreToolSkillCatalog,
    SupervisorReadinessGate,
)
from ouroboros.contracts.owner_scope import service_identity
from ouroboros.egress.a2a.dependencies import A2aEgressDependencies
from ouroboros.egress.a2a.manager import a2a_manager
from ouroboros.egress.a2a.task_handles import A2aTaskHandles
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.gateway.router import collect_routes
from ouroboros.llm import LLMClient
from ouroboros.mcp.manager import mcp_manager
from ouroboros.observability.health import STARTING, IHealthProbes, LoopHeartbeat, database_failure
from ouroboros.observability.logging_setup import configure_logging, resolve_log_level
from ouroboros.scheduling.service import SchedulerService
from ouroboros.scheduling.store import default_schedule_store
from ouroboros.skills.availability import build_skill_availability
from ouroboros.skills.extensions.loader import reload_all
from ouroboros.skills.extensions.runtime import ExtensionRuntime
from ouroboros.skills.loader import DistributionSkillLoader
from ouroboros.skills.presentation import load_skill_presentations
from ouroboros.storage.blob.async_backend import open_file_blob
from ouroboros.storage.blob.factory import close_storage_provider, initialize_storage_provider
from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.chat_store import InboundMessageStore
from ouroboros.storage.engine_setup import refuse_without_runtime_database
from ouroboros.storage.file_transfers import initialize_transfer_limits
from ouroboros.storage.message_field_crypto import validate_chat_encryption
from ouroboros.storage.probe_database import probe_database_factory
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.schema import SettingsLike
from security_config.a2a import a2a_remote_agents
from security_config.auth.header_collector import header_collector_declaration
from security_config.mcp import mcp_servers
from supervisor.infra.repositories.owner_state.repo import SqlOwnerStateRepository
from supervisor.infra.repositories.usage.repo import SqlUsageRepository
from supervisor.message_bus import LocalChatBridge
from supervisor.queue import TaskQueue, get_task_queue
from supervisor.runtime import LOOP_STALL_SEC, TICK_STOP_BUDGET_SEC
from supervisor.state import RuntimeStateRepository, get_state_repository
from transport.browser.identity import ConfiguredOwnerIdentity, TrustedHeaderIdentity, browser_identity
from transport.browser.infra.repositories.owner_history.repo import SqlOwnerHistoryRepository
from transport.browser.ws import BrowserWebSocketHub, get_browser_ws_hub, resubmit_unprocessed
from transport.dialogs import (
    DialogsProvider,
    DialogsTransport,
    build_dialogs_transport,
    dialogs_feature_enabled,
)
from transport.dialogs.infra.repositories.repo import SqlDialogsCursorRepository
from transport.infra.inmemory import InProcessInbox, InProcessMessageGateway, InProcessOutbox
from transport.skill_bridge import SkillBridge

_paths = get_config().paths
DEFAULT_HOST = str(load_settings().OUROBOROS_SERVER_HOST or "127.0.0.1")
DEFAULT_PORT = int(_paths.server_port or 8765)

if not _paths.agent_python:
    _agent_python = sys.executable
    if isinstance(_agent_python, str) and _agent_python:
        os.environ["OUROBOROS_AGENT_PYTHON"] = _agent_python

configure_logging()
log = logging.getLogger("server")

message_gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
# The one publishing boundary both producers reach the outbox through: the supervisor for
# text it answers with, and each run for the attachments it hands the owner.
outbound_bridge = LocalChatBridge(message_gateway.outbox, DATA_DIR)

RESTART_EXIT_CODE = 42
#: Set by every path that ends this process: an in-band restart request and a termination
#: signal alike. Every loop that must stop waits on this one event.
_stop_requested = threading.Event()
#: Set only when the process is meant to come back — an in-band restart, never an
#: orchestrator's SIGTERM. A termination signal that re-execed the process would fight
#: whatever asked it to stop.
_restart_on_exit = threading.Event()

# Captured in main() for Settings LAN-reachability metadata.
_BIND_HOST = DEFAULT_HOST


def _restart_current_process(host: str, port: int) -> None:
    _restart_current_process_impl(host, port, repo_dir=REPO_DIR, log=log)


from ouroboros.app.server_runtime import (
    apply_runtime_provider_defaults,
    has_supervisor_provider,
)

_supervisor_ready = threading.Event()
_supervisor_error: str | None = None
_supervisor_heartbeat = LoopHeartbeat("supervisor", stall_after_sec=LOOP_STALL_SEC)
_agent_heartbeat = LoopHeartbeat("agent runtime", stall_after_sec=CLAIM_LOOP_STALL_SEC)
_memory_heartbeat = LoopHeartbeat("memory runtime", stall_after_sec=lease_expiry_sec(load_settings()))
_event_loop: asyncio.AbstractEventLoop | None = None
_supervisor_thread: threading.Thread | None = None
_supervisor_shutdown_failed = threading.Event()
# Stopped when the supervisor thread returns: from that moment nothing drains
# what a transport keeps accepting.
_started_transports = StartedTransports()

# The agent publishes onto `_memory_event_queue`; the memory runtime consumes it
# and shares `_stop_requested` with the supervisor thread as its stop signal.
_MEMORY_QUEUE_MAXSIZE = MAX_PENDING_EVENTS
_memory_event_queue = MemoryEventQueue(_MEMORY_QUEUE_MAXSIZE)

# The seam between the runs and whoever consumes what they report: one transport, its two
# halves handed to the two components. Built here because this is the only place allowed to
# name both of them.
_agent_event_publisher, _agent_event_consumer = create_agent_event_transport()
# The wake-up that shortens a runtime's wait when the supervisor enqueues. An optimisation
# only: the runtime polls the claim on its own, so a lost signal costs latency.
_work_signal = InProcessWorkSignal()
_task_stop_publisher, _task_stop_consumer = create_task_stop_transport()


def _runtime_status(
    owner_scope: OwnerScope, *, task_queue: TaskQueue, state_repo: RuntimeStateRepository
) -> dict[str, int]:
    """Read browser status using the dependencies composed by this process."""
    status = {
        "workers_total": 0,
        "workers_busy": 0,
        "pending_count": 0,
        "running_count": 0,
        "spent_calls": 0,
    }
    try:
        status["pending_count"] = len(task_queue.list_pending_records(scope=owner_scope))
        status["running_count"] = len(task_queue.list_running(scope=owner_scope))
    except Exception:
        log.debug("Failed to read supervisor queue counts", exc_info=True)
    try:
        # Asked of the runtime this process started, not of the supervisor: lanes belong to
        # whoever runs the tasks, and after the split that is never the supervisor. These two
        # stay instance-wide on purpose — a lane belongs to this process, not to the asker.
        runtime = _agent_runtime_thread.runtime
        if runtime is not None:
            status["workers_total"], status["workers_busy"] = runtime.worker_counts()
    except Exception:
        log.debug("Failed to read worker counts", exc_info=True)
    try:
        state = state_repo.for_scope(owner_scope).load()
        status["spent_calls"] = int(getattr(state, "spent_calls", 0) or 0)
    except Exception:
        log.debug("Failed to read owner runtime state", exc_info=True)
    return status


def _build_dialogs_transport(
    drive_root: pathlib.Path, session_id: str, state_repo: RuntimeStateRepository
) -> DialogsTransport | None:
    """Compose the dialogs subscription before the supervisor can start publishing."""
    settings = load_settings()
    if not dialogs_feature_enabled(settings):
        log.info("Dialogs transport disabled (missing endpoint or bot token)")
        try:
            owner_source = str(state_repo.load().owner_chat_source or "")
        except Exception:
            log.debug("Failed to read the owner chat source", exc_info=True)
        else:
            report_missing_dialogs_transport(owner_source)
        return None
    return build_dialogs_transport(
        settings=settings,
        gateway=message_gateway,
        storage=get_storage_provider(),
        drive_root=drive_root,
        session_id=session_id,
        provider_factory=DialogsProvider,
        cursor_repository=SqlDialogsCursorRepository(
            runtime_persistence.current.engine, scope=service_identity(job=ServiceJob.DIALOGS_CURSOR).as_scope()
        ),
    )


def _start_dialogs_transport(transport: DialogsTransport) -> None:
    """Connect the SDK and start the loops; the subscription is already open."""
    transport.start()
    # Anything persisted but never handed over — a submit that failed, a process
    # that died between the two — is submitted again here.
    transport.resubmit_unprocessed()
    log.info(
        "Dialogs transport started provider_active=%s startup_error=%s cursor=%s",
        transport.provider_active,
        transport.startup_error,
        transport.cursor.value,
    )


def _compose_security_context(
    engine: AsyncEngine, encryption_key: SecretStr, declaration: HeaderCollectorDeclaration
) -> ISecurityContextStore:
    store = SqlSecurityContextStore(engine, encryption_key=encryption_key)
    security_context.initialize(store)
    security_context_collector.initialize(DeclaredParametersCollector(declaration))
    return store


def _chat_session_id(state_repo: RuntimeStateRepository) -> str:
    """Read the shared conversation id once at process startup."""
    try:
        return str(state_repo.session_id() or "")
    except Exception:
        log.warning("Failed to read the chat session id", exc_info=True)
        return ""


#: How long a stop from the supervisor thread waits for the browser hub's own
#: shutdown, which runs on the event loop.
_BROWSER_STOP_TIMEOUT_SEC = 10.0


def _browser_hub_stopper(browser_hub: BrowserWebSocketHub) -> Callable[[], None]:
    """A thread-callable stop for the hub, whose own stop runs on the loop."""

    def _stop() -> None:
        loop = _event_loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(browser_hub.stop(), loop).result(timeout=_BROWSER_STOP_TIMEOUT_SEC)

    return _stop


def _server_profile_unavailable_message(command: str) -> str:
    return f"⚠️ {command} unavailable in server profile."


def _load_distribution_extensions(
    drive_root: pathlib.Path,
    skill_bridge: SkillBridge,
    skill_surface: SkillSurface,
) -> dict[str, object]:
    """Load every trusted distribution extension or fail startup."""
    results = reload_all(
        pathlib.Path(drive_root),
        load_settings,
        skill_surface=skill_surface,
        storage_provider=get_storage_provider(),
        skill_bridge=skill_bridge,
    )
    failures = {name: error for name, error in results.items() if error and error != "disabled"}
    if failures:
        details = "; ".join(f"{name}: {error}" for name, error in sorted(failures.items()))
        raise RuntimeError(f"Distribution extension startup failed: {details}")
    log.info("Distribution extension skills loaded count=%d", len(results))
    return results


def _publish_agent_system_configuration_startup_event(skill_surface: SkillSurface) -> None:
    """Publish the ready catalog on every deployment-profile process start."""
    if not is_server_profile():
        return
    try:
        from ouroboros.app.agent_system_configuration_kafka import publish_agent_system_configuration_event

        publish_agent_system_configuration_event(logger=log, skill_surface=skill_surface)
    except Exception:
        log.warning("AGENT_SYSTEM_CONFIGURATION startup announcement failed", exc_info=True)


def _start_supervisor_if_needed(settings: SettingsLike, safe_restart: Callable[[], tuple[bool, str]]) -> bool:
    """Start the supervisor once when runtime providers become available."""
    global _supervisor_thread, _supervisor_error
    if not has_supervisor_provider(settings):
        return False
    if _supervisor_thread and _supervisor_thread.is_alive():
        log.info("Supervisor thread already running; skip duplicate start")
        return False
    _supervisor_error = None
    _supervisor_shutdown_failed.clear()
    log.info(
        "Starting supervisor background thread drive_root=%s repo_dir=%s",
        DATA_DIR,
        REPO_DIR,
    )
    _supervisor_thread = threading.Thread(
        target=_run_supervisor,
        args=(settings, safe_restart),
        daemon=True,
        name="supervisor-main",
    )
    _supervisor_thread.start()
    return True


def _run_supervisor(settings: dict, safe_restart: Callable[[], tuple[bool, str]]) -> None:
    """Initialize and run the supervisor loop. Called in a background thread."""
    global _supervisor_error, _supervisor_thread

    def _on_error(message: str) -> None:
        global _supervisor_error
        _supervisor_error = message
        log.error("Supervisor thread reported startup/runtime error: %s", message)

    log.info("Supervisor thread entered run_supervisor")
    from supervisor.runtime import SupervisorShutdownError, run_supervisor

    def _run() -> None:
        # The composition root names both halves: the bridge publishes onto the outbox
        # of the one gateway every transport reads.
        run_supervisor(
            settings,
            drive_root=DATA_DIR,
            repo_dir=REPO_DIR,
            stop_event=_stop_requested,
            ready_event=_supervisor_ready,
            on_error=_on_error,
            heartbeat=_supervisor_heartbeat,
            request_restart=_request_restart_exit,
            safe_restart=safe_restart,
            apply_settings_to_env=_apply_settings_to_env,
            gateway=message_gateway,
            bridge=outbound_bridge,
            agent_events=_agent_event_consumer,
            work_signal=_work_signal,
            task_stop_publisher=_task_stop_publisher,
        )

    try:
        stop_transports_when_supervisor_exits(
            _run,
            transports=_started_transports,
            supervisor_error=lambda: _supervisor_error,
        )
    except SupervisorShutdownError as error:
        _supervisor_shutdown_failed.set()
        _on_error(str(error))
    finally:
        _supervisor_thread = None


#: This process's identity on the consolidation leases its memory loop takes. Minted at module
#: scope, not per table build: a table rebuilt here takes back the pairs its predecessor left,
#: which a fresh identity would leave parked until they expired.
_MEMORY_HOLDER_ID = mint_holder_id()


def _memory_handlers() -> dict[type[MemoryEvent], MemoryEventHandler]:
    """The dispatch table the memory loop runs: one entry per event kind.

    Both consolidation entries are the same object's method on purpose — the
    continuation is how a run is handed back the state it left on the handler.
    """
    drive_root = pathlib.Path(DATA_DIR)
    env = MemoryEnv(repo_dir=pathlib.Path(REPO_DIR), drive_root=drive_root)
    # Built here, not per run: a handler that has no repository has no work to do,
    # and that is a startup failure, not something to rediscover on every event.
    engine = async_engine_factory.engine
    history = SqlChatHistoryRepo(engine)
    tasks = SqlTaskRepo(engine)
    audit = SqlAuditRepo(engine)
    user_settings = user_settings_factory.settings()
    consolidation = ConsolidationHandler(
        LLMClient(),
        leases=SqlConsolidationLeaseRepo(engine),
        summaries=SqlContextSummaryRepo(engine),
        history=history,
        audit=audit,
        user_settings=user_settings,
        foreground_pressure=TaskTableForegroundPressure(tasks),
        stop_event=_stop_requested,
        holder_id=_MEMORY_HOLDER_ID,
    )
    persisted = PersistedMessageHandlers(
        TitleHandler(LLMClient(), SqlChatRegistryRepo(engine), history=history, user_settings=user_settings),
        consolidation,
    )
    return {
        MemoryDistillationEvent: DistillationHandler(
            env,
            LLMClient(),
            tasks=tasks,
            audit=audit,
            memory_headers=SqlMemoryHeaderRepo(engine),
            user_settings=user_settings,
        ).handle,
        ChatMessagePersistedEvent: persisted.handle,
        ConsolidationContinuationEvent: consolidation.handle,
    }


class _MemoryRuntimeTask:
    """Owns the background memory loop: one task, started once and awaited on the way out.

    No thread and no restart wrapper. A failing delivery already fails alone — the loop logs
    it and keeps polling — so there was nothing left for a restart to recover, and a loop
    restarted while its predecessor's deliveries still held leases is how a pair ends up
    parked behind two holders.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._runtime: MemoryRuntime | None = None
        self._error: str | None = None

    @property
    def error(self) -> str | None:
        return self._error

    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def start_if_needed(self, settings: SettingsLike, *, loop: asyncio.AbstractEventLoop) -> bool:
        """Initialize the transport and start the loop, unless one already runs.

        Gated on the supervisor's provider check: distillation is only meaningful
        when a usable model provider exists. Ungated, the factory keeps its null
        publisher and completed tasks are dropped.
        """
        if not has_supervisor_provider(settings):
            return False
        if self.is_alive():
            log.info("Memory runtime loop already running; skip duplicate start")
            return False
        self._error = None
        memory_event_publisher_factory.initialize_inmemory(_memory_event_queue)
        log.info("Starting background memory loop cap=%d drive_root=%s", _MEMORY_QUEUE_MAXSIZE, DATA_DIR)
        self._runtime = MemoryRuntime(
            InMemoryEventConsumer(_memory_event_queue),
            handlers=_memory_handlers(),
            stop_event=_stop_requested,
            heartbeat=_memory_heartbeat,
            drain_timeout_sec=memory_drain_timeout_sec(load_settings()),
        )
        self._task = loop.create_task(self._run(self._runtime), name="memory-runtime")
        return True

    async def _run(self, runtime: MemoryRuntime) -> None:
        try:
            await runtime.execute()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = f"Memory runtime failed: {exc}"
            log.error("Memory runtime failed", exc_info=True)

    async def join(self, timeout: float | None = None) -> None:
        """Await the loop's own task, so the process never reports itself stopped before it."""
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("The memory runtime loop did not finish draining within %ss; cancelling it", timeout)
            task.cancel()
            await asyncio.wait([task], timeout=CANCELLED_RELEASE_GRACE_SEC)


_memory_runtime = _MemoryRuntimeTask()


class _AgentRuntimeThread:
    """Owns the agent runtime thread: started once, restarted on a crash.

    Its own unit of execution, and its own failure: a dead runtime and a dead supervisor
    are different outages and lead to different actions, so the error is reported apart.
    """

    #: Consecutive crashes before task execution is given up on for this process.
    MAX_RESTARTS = 3

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._runtime: AgentRuntime | None = None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def runtime(self) -> AgentRuntime | None:
        return self._runtime

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_if_needed(
        self,
        settings: SettingsLike,
        *,
        skill_snapshot: SkillCatalogSnapshot,
        skill_availability: SkillAvailability,
        extension_runtime: IExtensionRuntime,
    ) -> bool:
        """Build the runtime and start its thread, unless one already runs."""
        if not has_supervisor_provider(settings):
            return False
        if self.is_alive():
            log.info("Agent runtime thread already running; skip duplicate start")
            return False
        self._error = None
        self._runtime = build_agent_runtime(
            settings=dict(settings),
            drive_root=DATA_DIR,
            repo_dir=REPO_DIR,
            events=_agent_event_publisher,
            skill_snapshot=skill_snapshot,
            skill_availability=skill_availability,
            extension_runtime=extension_runtime,
            outbound=outbound_bridge,
            work_signal=_work_signal,
            stop_event=_stop_requested,
            heartbeat=_agent_heartbeat,
            task_stops=_task_stop_consumer,
        )
        log.info("Starting agent runtime thread drive_root=%s repo_dir=%s", DATA_DIR, REPO_DIR)
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent-runtime")
        self._thread.start()
        return True

    def drain(self, *, deadline_sec: float) -> bool:
        """Hand this runtime's runs back within a bounded deadline."""
        runtime = self._runtime
        return runtime is None or runtime.drain(deadline_sec=deadline_sec)

    def _run(self) -> None:
        """Run the claim loop, restarting it on an exponential backoff."""
        crash_count = 0
        while not _stop_requested.is_set():
            runtime = self._runtime
            if runtime is None:
                break
            try:
                runtime.execute()
                break  # returned on the stop signal, not on a failure
            except Exception as exc:
                crash_count += 1
                log.error("Agent runtime crash #%d", crash_count, exc_info=True)
                if crash_count >= self.MAX_RESTARTS:
                    # Recorded here only: liveness reads this slot, and a crash the loop
                    # recovers from is not a process to replace.
                    self._error = f"Agent runtime crash #{crash_count}: {exc}"
                    log.critical("Agent runtime exceeded max restarts; this process runs no more tasks")
                    break
                _stop_requested.wait(min(30, 2**crash_count))
        self._thread = None


_agent_runtime_thread = _AgentRuntimeThread()


class _ServerProbes(IHealthProbes):
    """Every loop of the full runtime, and which of them is vital."""

    def startup(self) -> str:
        return "" if _supervisor_ready.is_set() else STARTING

    async def readiness(self) -> str:
        if not _supervisor_ready.is_set():
            return STARTING
        return _supervisor_error or await database_failure(probe_database_factory.database)

    def liveness(self) -> str:
        if not _supervisor_ready.is_set():
            return ""
        return (
            _supervisor_error
            or _agent_runtime_thread.error
            or _supervisor_heartbeat.failure()
            or _agent_heartbeat.failure()
            or (_memory_heartbeat.failure() if _memory_runtime.is_alive() else "")
        )


def _start_memory_runtime_if_needed(settings: SettingsLike, *, loop: asyncio.AbstractEventLoop) -> bool:
    """Start the background memory loop, once, under the supervisor's gate."""
    return _memory_runtime.start_if_needed(settings, loop=loop)


def _request_restart_exit() -> None:
    """Ask for a shutdown this process is meant to come back from."""
    _restart_on_exit.set()
    _stop_requested.set()


def _request_termination(signum: int = 0, _frame: object = None) -> None:
    """Start the drain on a termination signal, rather than exiting where we stand.

    The same event the in-band restart request sets, so there is one shutdown path: the
    supervisor loop stops, uvicorn stops accepting, and the lifespan's shutdown drains the
    runs this instance holds before the outbound side is torn down. What this path does not
    set is `_restart_on_exit`: an orchestrator asking the process to stop does not want it
    started again by itself.
    """
    if signum:
        log.info("Received signal %d — draining before exit.", signum)
    _stop_requested.set()


def _install_termination_handlers() -> None:
    """Route the platform's termination signals into the drain.

    uvicorn installs its own handlers for the duration of `serve()` and restores these
    afterwards, so during the run the handler that matters is `_DrainingServer.handle_exit`;
    these cover the windows on either side of it.
    """
    from ouroboros.platform_layer import install_termination_handlers

    installed = install_termination_handlers(_request_termination)
    log.info("Termination handlers installed for %s", ", ".join(installed) or "no signal")


def safe_restart() -> tuple[bool, str]:
    """Validate restart; the caller signals shutdown after its durable preparation."""
    return True, "restart requested"


def refuse_multiuser_restart() -> tuple[bool, str]:
    return False, "this instance serves several owners, and a restart would stop all of their work"


APP_START = time.time()


owner_api = FastAPI(title="Ouroboros owner API", description="The agents an owner has, and the settings of each.")
owner_api.include_router(agents_router, prefix="/agents")
owner_api.include_router(owner_settings_router, prefix="/owner-settings")
owner_api.include_router(chats_router, prefix="/chats")
install_trace_middleware(owner_api, source="owner-api")

routes = [
    *collect_routes(
        data_dir=DATA_DIR,
        repo_dir=REPO_DIR,
        browser_enabled=load_settings().BROWSER_INGRESS_ENABLED,
    ),
    Mount("/api", app=owner_api),
]

from contextlib import AsyncExitStack, asynccontextmanager, suppress


def _drain_timeout_sec() -> int:
    """How long the shutdown may spend handing runs back. Read once, in one place.

    The force-exit watchdog is sized from it, so a deployment that lengthens the drain
    lengthens the guard around it rather than being killed inside it.
    """
    try:
        return max(0, int(load_settings().OUROBOROS_DRAIN_TIMEOUT_SEC or 0))
    except Exception:
        log.warning("Unreadable OUROBOROS_DRAIN_TIMEOUT_SEC; draining with no deadline of its own", exc_info=True)
        return 0


def _drain_runs() -> bool:
    """Ask the runtime to hand its runs back, within a bounded deadline."""
    try:
        return _agent_runtime_thread.drain(deadline_sec=_drain_timeout_sec())
    except Exception:
        log.warning("Failed to drain the runs this instance holds", exc_info=True)
        return False


_retained_file_resources: AsyncExitStack | None = None


@asynccontextmanager
async def lifespan(app):
    global _event_loop, _retained_file_resources
    _event_loop = asyncio.get_running_loop()
    browser_hub = get_browser_ws_hub() if load_settings().BROWSER_INGRESS_ENABLED else None
    dialogs_transport: DialogsTransport | None = None
    log.info("Ouroboros lifespan startup begin")

    from ouroboros.app.supervisor_readiness import configure_supervisor_readiness

    cache_factory.initialize(
        load_settings().OUROBOROS_SETTINGS_CACHE_BACKEND or "none", load_settings().OUROBOROS_SETTINGS_CACHE_TTL_SEC
    )
    configure_supervisor_readiness(_supervisor_ready, lambda: _supervisor_error)
    log.info("Supervisor readiness hooks configured")

    settings, provider_defaults_changed, _provider_default_keys = apply_runtime_provider_defaults(load_settings())
    if provider_defaults_changed:
        save_settings(settings, allow_elevation=True)
        log.info(
            "Applied runtime provider defaults keys=%s",
            sorted(_provider_default_keys),
        )
    refuse_without_runtime_database(load_settings())
    try:
        from ouroboros.app.config import validate_production_storage

        validate_production_storage()
        log.info("Production storage configuration validated")
    except Exception:
        log.exception("Production storage configuration invalid")
        raise
    validate_chat_encryption()
    lifespan_drive_root = pathlib.Path(
        app.state.drive_root if hasattr(app, "state") and hasattr(app.state, "drive_root") else DATA_DIR
    )
    log.info(
        "Ouroboros runtime paths drive_root=%s repo_dir=%s",
        lifespan_drive_root,
        REPO_DIR,
    )
    async with AsyncExitStack() as file_resources:
        file_blob = await file_resources.enter_async_context(open_file_blob(settings))
        initialize_storage_provider(settings, file_client=file_blob.client, file_loop=asyncio.get_running_loop())
        file_resources.callback(close_storage_provider)
        if browser_hub is not None:
            app.state.file_blob = file_blob
            browser_hub.file_blob = file_blob
        # Compose the mode once; keyspace validation follows after the database is ready.
        multiuser = bool(settings.get("OUROBOROS_MULTIUSER", False))
        validate_multiuser_configuration()
        async_engine_factory.initialize_beside_store(settings)
        if browser_hub is not None:
            app.state.inbound_messages = InboundMessageStore(
                async_engine_factory.engine, SqlChatRegistryStore(), timeout=settings.OUROBOROS_FILE_IO_TIMEOUT_SEC
            )
        agent_registry = SqlAgentRegistryRepo(async_engine_factory.engine)
        browser_identity.initialize(
            TrustedHeaderIdentity(agent_registry) if multiuser else ConfiguredOwnerIdentity(agent_registry)
        )
        security_context_store = _compose_security_context(
            async_engine_factory.engine, settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY, header_collector_declaration
        )
        # What the runs may consume, in one place with the relationship between the numbers:
        # the connection pool sits above the budgets, and the budgets above the slot count.
        worker_slots = max(1, int(settings.get("OUROBOROS_WORKER_SLOTS", 1) or 1))
        budget_limits = default_budget_limits(
            worker_slots=worker_slots,
            llm_max_concurrency=int(settings.get("OUROBOROS_LLM_MAX_CONCURRENCY", 0) or 0),
        )
        concurrency_budgets.initialize(InProcessBudget(budget_limits))
        log.info("Concurrency budgets composed slots=%d limits=%s", worker_slots, budget_limits)
        persistence = runtime_persistence.initialize()
        file_resources.callback(runtime_persistence.close)
        audit_event_repository = SqlAuditEventRepository(runtime_persistence.current.transaction_manager)
        state_repo = get_state_repository(
            lifespan_drive_root,
            owner_state_repository=SqlOwnerStateRepository(persistence.engine),
            usage_repository=SqlUsageRepository(persistence.engine),
            audit_event_repository=audit_event_repository,
        )
        status_queue = get_task_queue(lifespan_drive_root, audit_event_repository=audit_event_repository)
        app.state.runtime_status = partial(_runtime_status, task_queue=status_queue, state_repo=state_repo)
        app.state.owner_history_repository = SqlOwnerHistoryRepository(
            persistence.engine, history_transactions=file_transaction
        )
        initialize_transfer_limits(persistence.engine, settings)
        # Bringing the schema up is a deployment step, not something a starting runtime does:
        # several instances starting together would race each other through the migrations.
        assert_schema_at_head(persistence.engine)
        if settings.BROWSER_INGRESS_ENABLED:
            app.state.scheduler = SchedulerService(default_schedule_store())
        # Now that the fleet's shared substrate is open and at head: does this instance mean
        # the same object keyspace as the one that recorded it? Before anything serves.
        validate_deployment_keyspace(SqlDeploymentKeyspaceRepository(persistence.engine))
        probe_database_factory.initialize_beside_store(settings)
        log.info("Runtime metadata store composed")
        # Every transport stamps its rows with this one value, so it is read once here
        # rather than per message; the browser reads it off ``app.state``.
        chat_session_id = _chat_session_id(state_repo)
        app.state.chat_session_id = chat_session_id
        log_level = str(settings.get("OUROBOROS_LOG_LEVEL") or "INFO")
        log_format = str(settings.get("OUROBOROS_LOG_FORMAT") or "json")
        log_sink = str(settings.get("OUROBOROS_LOG_SINK") or "stdout")
        effective_sink = configure_logging(
            level=log_level,
            log_format=log_format,
            sink=log_sink,
        )
        # Effective, not requested: an unwritable log dir degrades to stdout.
        log.info(
            "Ouroboros logging configured level=%s format=%s sink=%s",
            log_level,
            log_format,
            effective_sink,
        )
        from ouroboros.observability.dynatrace import initialize_dynatrace_monitoring

        initialize_dynatrace_monitoring()

        mcp_enabled = bool(settings.get("MCP_ENABLED", False))
        connected_mcp_servers = mcp_servers if mcp_enabled else []
        a2a_egress_enabled = settings.A2A_EGRESS_ENABLED
        connected_a2a_agents = a2a_remote_agents if a2a_egress_enabled else []
        EgressAuthComposition.refuse_undeclared_user_headers(
            connected_mcp_servers, header_collector_declaration, source=EgressList.MCP
        )
        EgressAuthComposition.refuse_undeclared_user_headers(
            connected_a2a_agents, header_collector_declaration, source=EgressList.A2A
        )
        egress_auth = EgressAuthDependencies(
            security_context_store=security_context_store,
            user_token_store=EgressAuthComposition.user_token_store(
                [*connected_mcp_servers, *connected_a2a_agents],
                engine_factory=async_engine_factory,
                encryption_key=settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY,
            ),
        )
        a2a_egress = A2aEgressDependencies(
            auth=egress_auth,
            task_handles=A2aTaskHandles(encryption_key=settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY),
        )

        # Build the catalog before consumers start so they share the same tool surface.
        skill_catalog = SkillCatalog()
        skill_catalog.initialize(
            [
                DistributionSkillProvider(
                    distribution_root=pathlib.Path(REPO_DIR) / "skills",
                    loader=DistributionSkillLoader(settings),
                )
            ]
        )
        skill_availability = build_skill_availability(settings)
        owner_api.state.skill_presentations = await asyncio.to_thread(
            load_skill_presentations, skill_catalog.snapshot()
        )
        extension_runtime = ExtensionRuntime()
        deployment_skill_surface = SkillSurface(
            snapshot=skill_catalog.snapshot(),
            availability=skill_availability,
        )
        owner_api.state.skill_surface = deployment_skill_surface
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
                ),
                A2aToolProvider(
                    a2a_manager,
                    get_a2a_tool_timeout_sec(),
                    agents=None,
                    loop=None,
                    dependencies=a2a_egress,
                    egress_enabled=a2a_egress_enabled,
                ),
            ]
        )
        startup_scope = service_identity(job=ServiceJob.STARTUP).as_scope()
        log.info("Tool catalog initialized tools=%d", len(tool_catalog.descriptors(startup_scope)))

        if mcp_enabled and mcp_servers:
            try:
                await mcp_manager.initialize(mcp_servers, _event_loop, egress_auth)
                log.info("MCP manager initialized tools=%d", len(mcp_manager.discovered_tools()))
            except Exception:
                log.warning("Failed to initialize MCP manager", exc_info=True)
        elif mcp_enabled:
            log.info("MCP enabled, but security_config/mcp.py lists no servers; MCP manager not initialized")

        if a2a_egress_enabled and a2a_remote_agents:
            try:
                await a2a_manager.initialize(a2a_remote_agents, _event_loop, a2a_egress_enabled, a2a_egress)
                log.info("A2A egress manager initialized agents=%d", len(a2a_manager.connections()))
            except Exception:
                log.warning("Failed to initialize the A2A egress manager", exc_info=True)
        elif a2a_egress_enabled:
            log.info("A2A egress enabled, but security_config/a2a.py lists no agents; egress manager not initialized")

        # Subscribe before the supervisor starts so early replies have a recipient.
        dialogs_transport = await asyncio.to_thread(
            _build_dialogs_transport, lifespan_drive_root, chat_session_id, state_repo
        )
        if dialogs_transport is not None:
            _started_transports.register("dialogs", dialogs_transport.stop)

        a2a_server_task = None
        try:
            if has_supervisor_provider(settings):
                log.info("Supervisor provider configured; scheduling supervisor startup")
                _start_supervisor_if_needed(
                    settings, safe_restart=refuse_multiuser_restart if multiuser else safe_restart
                )
                _agent_runtime_thread.start_if_needed(
                    settings,
                    skill_snapshot=skill_catalog.snapshot(),
                    skill_availability=skill_availability,
                    extension_runtime=extension_runtime,
                )
                _start_memory_runtime_if_needed(settings, loop=_event_loop)
            else:
                _supervisor_ready.set()
                log.info("No supported provider or local routing configured. Supervisor not started.")
                if settings.get("A2A_ENABLED", False):
                    log.error(
                        "A2A_ENABLED but no remote provider is configured — set "
                        "CLOUDRU_FOUNDATION_MODELS_API_KEY or GIGACHAT_B2BANK_* in the environment"
                    )

            # After the memory runtime, whose start is what replaces the null publisher, and before
            # extensions load: a skill that injects on registration needs the seam.
            skill_bridge = SkillBridge(
                lifespan_drive_root,
                gateway=message_gateway,
                session_id=chat_session_id,
                memory_events=memory_event_publisher_factory.memory_publisher,
                permission_checker=CatalogSkillPermissionChecker(
                    skill_surface=deployment_skill_surface,
                ),
            )

            try:
                log.info("Initializing extension runtime helpers")
                from ouroboros.observability.event_bus import init_global_event_bus
                from ouroboros.skills.extensions.companion import init_global_supervisor

                init_global_event_bus().set_loop(_event_loop)
                init_global_supervisor(lifespan_drive_root)
                if browser_hub is not None:
                    await browser_hub.start(
                        _event_loop,
                        gateway=message_gateway,
                        drive_root=lifespan_drive_root,
                    )
                    _started_transports.register("browser", _browser_hub_stopper(browser_hub))
                    # Anything persisted but never handed over — a submit that failed, a
                    # process that died between the two — is submitted again here.
                    await asyncio.to_thread(
                        resubmit_unprocessed,
                        message_gateway,
                        lifespan_drive_root,
                    )
                if dialogs_transport is not None:
                    await asyncio.to_thread(_start_dialogs_transport, dialogs_transport)
                log.info("Extension runtime helpers initialized")
                await asyncio.to_thread(
                    _load_distribution_extensions,
                    lifespan_drive_root,
                    skill_bridge,
                    deployment_skill_surface,
                )
            except Exception as exc:
                log.exception("Failed to initialize distribution extension runtime")
                raise RuntimeError("Distribution extensions are not ready") from exc

            if settings.get("A2A_ENABLED", False):
                a2a_port = int(settings.get("A2A_PORT", 18800))

                async def _start_a2a_when_supervisor_ready() -> None:
                    try:
                        from ouroboros.app.supervisor_readiness import wait_for_supervisor_ready
                        from transport.a2a.server import start_a2a_server, stop_a2a_server

                        startup_timeout = float(load_settings().OUROBOROS_SUPERVISOR_STARTUP_TIMEOUT_SEC)
                        await wait_for_supervisor_ready(
                            timeout_sec=startup_timeout,
                            require_bridge=has_supervisor_provider(settings),
                        )
                        log.info("Supervisor ready — starting A2A server on port %d", a2a_port)
                        _started_transports.register("a2a", stop_a2a_server)
                        # The card's skills and the readiness gate live above the seam,
                        # so a2a takes them as interfaces rather than importing them.
                        await start_a2a_server(
                            settings,
                            gateway=message_gateway,
                            drive_root=lifespan_drive_root,
                            skill_catalog=CoreToolSkillCatalog(),
                            readiness=SupervisorReadinessGate(),
                            session_id=chat_session_id,
                            memory_events=memory_event_publisher_factory.memory_publisher,
                        )
                    except Exception:
                        log.warning("Failed to start A2A server", exc_info=True)

                a2a_server_task = asyncio.create_task(
                    _start_a2a_when_supervisor_ready(),
                    name="a2a-server",
                )
                log.info("A2A server task scheduled on port %d (after supervisor ready)", a2a_port)

            try:
                _publish_agent_system_configuration_startup_event(deployment_skill_surface)
            except Exception:
                log.warning("AGENT_SYSTEM_CONFIGURATION startup publish failed", exc_info=True)

            log.info("Ouroboros lifespan startup complete; application ready")
            yield
        finally:
            # Drain with delivery and storage still available to finishing runs.
            _stop_requested.set()
            try:
                if not await asyncio.to_thread(_drain_runs):
                    raise TimeoutError("Agent bodies did not stop within the shutdown budget")
                supervisor_thread = _supervisor_thread
                if supervisor_thread is not None:
                    await asyncio.to_thread(supervisor_thread.join, TICK_STOP_BUDGET_SEC)
                if (
                    supervisor_thread is not None and supervisor_thread.is_alive()
                ) or _supervisor_shutdown_failed.is_set():
                    raise TimeoutError("Supervisor consumers did not stop within the shutdown budget")
                await _memory_runtime.join(timeout=memory_drain_timeout_sec(load_settings()) * 2)
                if browser_hub is not None:
                    await browser_hub.stop()
                if dialogs_transport is not None:
                    await asyncio.to_thread(dialogs_transport.stop)
            except BaseException:
                _retained_file_resources = file_resources.pop_all()
                raise
            await message_gateway.aclose()
            from ouroboros.observability.dynatrace import shutdown_dynatrace_monitoring

            shutdown_dynatrace_monitoring()
            if a2a_server_task is not None:
                try:
                    from transport.a2a.server import stop_a2a_server

                    stop_a2a_server()
                    a2a_server_task.cancel()
                    with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                        await asyncio.wait_for(a2a_server_task, timeout=5)
                except Exception:
                    log.warning("Failed to stop A2A server", exc_info=True)
            log.info("Server shutting down...")
            try:
                await mcp_manager.shutdown()
            except Exception:
                log.warning("Failed to shut down MCP manager", exc_info=True)
            try:
                await a2a_manager.shutdown()
            except Exception:
                log.warning("Failed to shut down the A2A egress manager", exc_info=True)
            try:
                tool_catalog.close()
            except Exception:
                log.warning("Failed to close the tool catalog", exc_info=True)
            try:
                from ouroboros.tools.shell import kill_all_tracked_subprocesses

                kill_all_tracked_subprocesses()
            except Exception:
                pass
            try:
                from ouroboros.skills.extensions.companion import get_global_supervisor

                supervisor = get_global_supervisor()
                if supervisor is not None:
                    supervisor.stop_all()
            except Exception:
                pass
            try:
                from ouroboros.llm.registry import get_registry

                get_registry().close()
            except Exception:
                log.warning("Failed to close LLM provider clients", exc_info=True)
            try:
                await async_engine_factory.dispose()
            except Exception:
                log.warning("Failed to dispose the async runtime DB engine", exc_info=True)
            try:
                await probe_database_factory.dispose()
            except Exception:
                log.warning("Failed to dispose the readiness probe's database", exc_info=True)


_starlette_app = Starlette(routes=routes, lifespan=lifespan)
_starlette_app.state.drive_root = pathlib.Path(DATA_DIR)
_starlette_app.state.repo_dir = pathlib.Path(REPO_DIR)
_starlette_app.state.app_start = APP_START
_starlette_app.state.health_probes = _ServerProbes()
# The probes answer for this process; these two answer for the supervisor `/api/state` asks
# about, and a root that hosts no supervisor publishes neither.
_starlette_app.state.supervisor_ready_event = _supervisor_ready
_starlette_app.state.get_supervisor_error = lambda: _supervisor_error
_starlette_app.state.message_gateway = message_gateway
_starlette_app.state.detached_enabled = load_settings().OUROBOROS_DURABLE_JOBS_ENABLED
_starlette_app.state.task_stop_notifier = TaskStopNotifier(_task_stop_publisher)
_starlette_app.state.bind_host = _BIND_HOST
_starlette_app.state.port_file = PORT_FILE
_starlette_app.state.default_port = DEFAULT_PORT
app = _starlette_app


def _emergency_process_cleanup(*, port_sweep: bool = True) -> None:
    """Kill child processes, workers, companions, and runtime port holders."""
    try:
        from ouroboros.tools.shell import kill_all_tracked_subprocesses

        kill_all_tracked_subprocesses()
    except Exception:
        pass
    try:
        runtime = _agent_runtime_thread.runtime
        if runtime is not None:
            runtime.drain(deadline_sec=0)
    except Exception:
        pass
    import multiprocessing

    from ouroboros.platform_layer import force_kill_pid, kill_process_on_port

    for child in multiprocessing.active_children():
        pid = child.pid
        if pid is None:
            continue
        try:
            force_kill_pid(pid)
        except (ProcessLookupError, PermissionError):
            pass
    if port_sweep:
        kill_process_on_port(DEFAULT_PORT)
        kill_process_on_port(8766)
    try:
        from ouroboros.skills.extensions.companion import panic_kill_all

        panic_kill_all()
    except Exception:
        pass


class _DrainingServer(uvicorn.Server):
    """uvicorn's server, with a termination signal routed into the drain.

    uvicorn captures SIGTERM and SIGINT for the duration of `serve()`, so this override is
    where a signal is actually seen while the process is up. It only records the intent;
    the drain itself runs in the lifespan's shutdown, where the outbound side is still up.
    """

    def handle_exit(self, sig: int, frame: object) -> None:
        _request_termination(sig)
        super().handle_exit(sig, frame)  # type: ignore[arg-type]


def main() -> int:
    saved_host = str(load_settings().OUROBOROS_SERVER_HOST or "").strip()
    default_host = saved_host or DEFAULT_HOST
    args = parse_server_args(default_host, DEFAULT_PORT)
    global _BIND_HOST
    _BIND_HOST = args.host
    _starlette_app.state.bind_host = args.host
    actual_port = find_free_port(args.host, args.port)
    if actual_port != args.port:
        log.info("Port %d busy on %s, using %d instead", args.port, args.host, actual_port)
    write_port_file(PORT_FILE, actual_port)
    log.info("Starting Ouroboros server on %s:%d", args.host, actual_port)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=actual_port,
        log_level=resolve_log_level(None),
        log_config=None,
        access_log=False,
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
    _install_termination_handlers()
    server = _DrainingServer(config)
    _uvicorn_exited = threading.Event()

    def _check_restart():
        """Monitor the stop signal, then shut down uvicorn."""
        while not _stop_requested.is_set():
            time.sleep(0.5)
        log.info("Shutdown requested — stopping the server.")
        server.should_exit = True

        # Force-exit only if uvicorn never returns; leave cleanup/re-exec time. Derived from
        # the drain rather than declared, because the drain is the longest bounded wait the
        # lifespan makes and a watchdog under it kills the hand-back it exists to allow:
        # firing mid-drain leaves the held runs `running` under a lease nobody renews, to
        # close later as `lease_lost` instead of `instance_released`.
        force_exit_timeout_sec = _drain_timeout_sec() + 30
        if _uvicorn_exited.wait(timeout=force_exit_timeout_sec):
            return
        # 42 means "start me again", and only an in-band restart asked for that. A signal
        # from whatever supervises the process did not, so it must not be answered with the
        # code that brings the process back.
        exit_code = RESTART_EXIT_CODE if _restart_on_exit.is_set() else 1
        log.warning(
            "Uvicorn did not exit within %ss — running emergency cleanup before os._exit(%d)",
            force_exit_timeout_sec,
            exit_code,
        )
        _emergency_process_cleanup()
        os._exit(exit_code)

    threading.Thread(target=_check_restart, daemon=True).start()

    try:
        server.run()
    finally:
        _uvicorn_exited.set()

    if _restart_on_exit.is_set():
        log.info("Exiting with code %d (restart signal).", RESTART_EXIT_CODE)
        _emergency_process_cleanup(port_sweep=False)
        _restart_current_process(args.host, actual_port)
        os._exit(RESTART_EXIT_CODE)

    return 0


if __name__ == "__main__":
    sys.exit(main())
