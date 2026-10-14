"""The browser transport as its own process: what it composes, and what it does not.

`server.py` is the full runtime — transport, supervisor and agent in one process. This is
the second composition root, and it declares its tier: it composes the route set, the
identity resolver, the agent registry that resolver resolves the agent axis through, the
runtime metadata store, the drive root and the message gateway, and it starts **no
supervisor loop and no agent runtime**. Nothing here imports `supervisor`.

A second root rather than a flag on `server.py`: the objects a transport-only process
substitutes — the gateway, the routes, the app state — are built at that module's scope,
before the first line of settings-driven startup runs, and both console scripts land
there. A flag would put deployment topology into import order.

**The gateway is the one the configuration names**, resolved at the composition boundary by
`ouroboros.app.broker_composition`: a gateway that spans processes now exists, and with it
selected two instances of this entrypoint share one queue. The default keeps an
unconfigured deployment on the in-process backend, so composing a broker is a decision
rather than an accident — and a named backend that cannot be built fails the process here
rather than falling back, because a fallback is a deployment of N instances each holding
its own queue. The composed backend is logged at startup so the running process says which
it is. Selecting a broker here is a whole deployment now: the supervisor tier drains the
same inbound stream and the agent runtime publishes onto the same outbox.
`server.py` composes the in-process backend directly and
the selector does not reach it, so the two arrangements are alternatives rather than layers.

The obligations that hurt when a second root forgets them are refusals rather than
conventions: no identity resolver means no start, no composed store means a loud crash, a
schema below the build's means a refusal, and the mode's own refusal set — plus the
production storage guard — is the same one `server.py` runs, because a refusal only one
entrypoint performs is not a refusal.

**One root per process.** The browser hub, the runtime store factory and the identity
holder are process globals by design, so building two transport apps in one interpreter
gives the second one's shutdown the first one's hub. Tests build one at a time.
"""

from __future__ import annotations

import logging
import pathlib
import time
from typing import Any
from uuid import uuid4

from starlette.applications import Starlette

from config_registry.agent_registry.infra.repositories.repo import SqlAgentRegistryRepo
from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryStore
from infra.async_engine import async_engine_factory
from infra.cache import cache_factory, refuse_a_process_local_settings_cache
from infra.migrations.runner import assert_schema_at_head
from infra.security_context import security_context, security_context_collector
from infra.security_context.collector import DeclaredParametersCollector
from infra.security_context.sql import SqlSecurityContextStore
from multiuser_config.bootstrap import start_process_monitoring, stop_process_monitoring
from ouroboros.app.broker_composition import (
    build_message_gateway,
    initialize_broker_backend,
    resolve_task_stop_publisher,
    start_message_gateway,
)
from ouroboros.app.infra.repositories.deployment_keyspace.repo import SqlDeploymentKeyspaceRepository
from ouroboros.app.multiuser_validation import validate_deployment_keyspace, validate_multiuser_configuration
from ouroboros.app.task_stop_notifications import TaskStopNotifier
from ouroboros.contracts.owner_scope import ServiceJob, service_identity
from ouroboros.gateway.router import collect_routes
from ouroboros.observability.health import ComposedProbes
from ouroboros.observability.logging_setup import configure_logging
from ouroboros.scheduling.service import SchedulerService
from ouroboros.scheduling.store import default_schedule_store
from ouroboros.storage.blob.async_backend import open_file_blob
from ouroboros.storage.blob.factory import close_storage_provider, initialize_storage_provider
from ouroboros.storage.blob.guards import validate_production_storage
from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.chat_store import InboundMessageStore
from ouroboros.storage.engine_setup import refuse_without_runtime_database
from ouroboros.storage.file_transfers import initialize_transfer_limits
from ouroboros.storage.message_field_crypto import validate_chat_encryption
from ouroboros.storage.probe_database import probe_database_factory
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.paths import load_path_settings
from ouroboros.storage.settings.schema import AppSettings
from ouroboros.storage.settings.store import load_settings
from security_config.auth.header_collector import header_collector_declaration
from transport.browser.identity import ConfiguredOwnerIdentity, TrustedHeaderIdentity, browser_identity
from transport.browser.infra.repositories.owner_history.repo import SqlOwnerHistoryRepository
from transport.browser.infra.repositories.owner_session.repo import SqlOwnerSessionRepository
from transport.browser.interfaces.owner_session import IOwnerSessionRepository
from transport.browser.ws import get_browser_ws_hub, resubmit_unprocessed
from transport.interfaces import IMessageGateway

log = logging.getLogger(__name__)

TIER = "browser-transport"


def chat_session_id(repository: IOwnerSessionRepository) -> str:
    """Read the configured owner's session without creating one during transport startup."""
    scope = service_identity(job=ServiceJob.JOURNAL_SWEEP).as_scope()
    try:
        return repository.get_session_id(scope)
    except Exception:
        log.warning("Failed to read the chat session id", exc_info=True)
        return ""


def build_transport_app(*, gateway: IMessageGateway | None = None) -> Starlette:
    settings = load_settings()
    configure_logging(level=str(settings.OUROBOROS_LOG_LEVEL or "INFO"))
    start_process_monitoring()
    paths = load_path_settings()
    drive_root = pathlib.Path(paths.data_dir)
    repo_dir = pathlib.Path(paths.repo_dir)

    refuse_without_runtime_database(load_settings())
    validate_production_storage()
    validate_chat_encryption()
    validate_multiuser_configuration()
    refuse_a_process_local_settings_cache(TIER)
    initialize_broker_backend()
    cache_factory.initialize(
        str(settings.OUROBOROS_SETTINGS_CACHE_BACKEND or "none"), int(settings.OUROBOROS_SETTINGS_CACHE_TTL_SEC)
    )

    persistence = runtime_persistence.initialize()
    initialize_transfer_limits(persistence.engine, settings, reserve_outgoing=True)
    # Asserted, never upgraded: bringing the schema up is a deployment step, so N
    # instances starting together change nothing their peers are serving.
    assert_schema_at_head(persistence.engine)
    validate_deployment_keyspace(SqlDeploymentKeyspaceRepository(persistence.engine))
    probe_database = probe_database_factory.initialize_beside_store(settings)

    async_engine_factory.initialize_beside_store(settings)
    agent_registry = SqlAgentRegistryRepo(async_engine_factory.engine)

    multiuser = bool(settings.OUROBOROS_MULTIUSER)
    # Installed explicitly in both branches. The holder has no default: a process that may
    # serve several owners must not fall back to resolving every caller to one.
    browser_identity.initialize(
        TrustedHeaderIdentity(agent_registry) if multiuser else ConfiguredOwnerIdentity(agent_registry)
    )
    security_context.initialize(
        SqlSecurityContextStore(
            async_engine_factory.engine, encryption_key=settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY
        )
    )
    security_context_collector.initialize(DeclaredParametersCollector(header_collector_declaration))

    # Minted per process rather than the tier name: a consumer name is this instance's own,
    # and two instances sharing one share a pending list and read each other's in-flight
    # envelope as a redelivery. This tier only submits today — the hub reads the outbox and
    # `resubmit_unprocessed` submits too — so nothing creates the consumer in Redis and the
    # name costs nothing; a constant would leave the hazard resting on that staying true,
    # while the gateway is on `app.state` and `IInbox.poll` is public. There is no
    # predecessor to inherit from either, which is what a stable name would buy.
    composed = gateway if gateway is not None else build_message_gateway(inbox_consumer=f"{TIER}-{uuid4().hex}")
    app = Starlette(
        routes=collect_routes(
            data_dir=drive_root,
            repo_dir=repo_dir,
            browser_enabled=True,
        ),
        lifespan=_lifespan_for(composed, drive_root, settings),
    )
    # Only what a mounted route reads. The six keys of the self-restart model have no
    # reader anywhere, and re-execing the process is the orchestrator's job in a scaled
    # deployment — a fight with it if this process also claims it.
    app.state.drive_root = drive_root
    app.state.repo_dir = repo_dir
    app.state.app_start = time.time()
    app.state.message_gateway = composed
    app.state.inbound_messages = InboundMessageStore(
        async_engine_factory.engine, SqlChatRegistryStore(), timeout=settings.OUROBOROS_FILE_IO_TIMEOUT_SEC
    )
    app.state.detached_enabled = settings.OUROBOROS_DURABLE_JOBS_ENABLED
    app.state.task_stop_notifier = TaskStopNotifier(resolve_task_stop_publisher())
    app.state.scheduler = SchedulerService(default_schedule_store())
    app.state.owner_history_repository = SqlOwnerHistoryRepository(
        persistence.engine, history_transactions=file_transaction
    )
    app.state.chat_session_id = chat_session_id(SqlOwnerSessionRepository(persistence.engine))
    # This process *is* the tier the probes answer for: composed here, before it serves, and
    # hosting no loop that could die apart from it.
    app.state.health_probes = ComposedProbes(probe_database)
    # No runtime_status provider and no supervisor readiness: queue depth, worker counts,
    # spend and the supervisor's own state belong to the supervisor tier, and no signal
    # crosses that boundary here. The route's own fallback reports the absence rather than
    # this root standing in with a value that always reads ready.
    log.info(
        "Composed the %s tier: gateway=%s multiuser=%s drive_root=%s",
        TIER,
        type(composed).__name__,
        multiuser,
        drive_root,
    )
    return app


def _lifespan_for(gateway: IMessageGateway, drive_root: pathlib.Path, settings: AppSettings) -> Any:
    import asyncio
    import contextlib

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        hub = get_browser_ws_hub()
        try:
            async with contextlib.AsyncExitStack() as resources:
                resources.callback(runtime_persistence.close)
                resources.push_async_callback(async_engine_factory.dispose)
                resources.push_async_callback(gateway.aclose)
                await start_message_gateway(gateway)
                file_blob = await resources.enter_async_context(open_file_blob(settings))
                initialize_storage_provider(
                    settings, file_client=file_blob.client, file_loop=asyncio.get_running_loop()
                )
                resources.callback(close_storage_provider)
                _app.state.file_blob = file_blob
                hub.file_blob = file_blob
                try:
                    await hub.start(asyncio.get_running_loop(), gateway=gateway, drive_root=drive_root)
                    await asyncio.to_thread(resubmit_unprocessed, gateway, drive_root)
                    yield
                finally:
                    try:
                        await hub.stop()
                    except BaseException:
                        _app.state.retained_resources = resources.pop_all()
                        raise
        finally:
            await probe_database_factory.dispose()
            stop_process_monitoring()

    return lifespan


__all__ = ["TIER", "build_transport_app", "chat_session_id"]
