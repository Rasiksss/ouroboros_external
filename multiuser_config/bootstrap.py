"""The start-up and the serving every component process shares (`docs/ARCHITECTURE.md` §Process model)."""

from __future__ import annotations

import logging
import pathlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import uvicorn
from starlette.applications import Starlette

from ouroboros.app.config import init_config
from ouroboros.observability.health import IHealthProbes, probe_routes
from ouroboros.storage.settings.schema import AppSettings

log = logging.getLogger(__name__)

#: The manifest's probe timeout.
_PROBE_SERVER_WAIT_SEC = 5.0
_PROBE_SERVER_POLL_SEC = 0.05


@dataclass(frozen=True)
class ProcessBootstrap:
    """What the shared start-up resolved, handed on to the component's own composition."""

    settings: AppSettings
    drive_root: pathlib.Path
    repo_dir: pathlib.Path


def bootstrap_process(component: str) -> ProcessBootstrap:
    """Bring one component process up to where it can compose itself.

    The imports sit inside the call because ``init_config`` refreshes the path aliases, and a
    module that read them earlier would name the previous data dir.
    """
    init_config()

    from infra.cache import cache_factory, refuse_a_process_local_settings_cache
    from infra.migrations.runner import assert_schema_at_head
    from ouroboros.app import config
    from ouroboros.app.broker_composition import initialize_broker_backend
    from ouroboros.app.config import load_settings, validate_production_storage
    from ouroboros.app.infra.repositories.deployment_keyspace.repo import SqlDeploymentKeyspaceRepository
    from ouroboros.app.multiuser_validation import validate_deployment_keyspace, validate_multiuser_configuration
    from ouroboros.app.server_runtime import apply_runtime_provider_defaults, has_supervisor_provider
    from ouroboros.observability.logging_setup import configure_logging
    from ouroboros.storage.engine_setup import refuse_without_runtime_database
    from ouroboros.storage.file_transfers import initialize_transfer_limits
    from ouroboros.storage.message_field_crypto import validate_chat_encryption
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.settings.store import save_settings

    settings, defaults_changed, default_keys = apply_runtime_provider_defaults(load_settings())
    if defaults_changed:
        # An overlay over this process's settings, not a file: every later ``load_settings``
        # in this process has to see what the defaults resolved to.
        save_settings(settings, allow_elevation=True)
        log.info("Applied runtime provider defaults keys=%s", sorted(default_keys))
    configure_logging(
        level=str(settings.get("OUROBOROS_LOG_LEVEL") or "INFO"),
        log_format=str(settings.get("OUROBOROS_LOG_FORMAT") or "json"),
    )
    start_process_monitoring()
    if not has_supervisor_provider(settings):
        raise RuntimeError(
            f"{component} has no usable model provider; set CLOUDRU_FOUNDATION_MODELS_API_KEY, "
            "GIGACHAT_B2BANK_* or SELFHOSTED_LLM_BASE_URL in the environment"
        )
    refuse_without_runtime_database(load_settings())
    validate_production_storage()
    validate_chat_encryption()

    drive_root = pathlib.Path(config.DATA_DIR)
    repo_dir = pathlib.Path(config.REPO_DIR)
    validate_multiuser_configuration()
    refuse_a_process_local_settings_cache(component)
    initialize_broker_backend()
    runtime_settings = load_settings()
    cache_factory.initialize(
        str(runtime_settings.OUROBOROS_SETTINGS_CACHE_BACKEND or "none"),
        int(runtime_settings.OUROBOROS_SETTINGS_CACHE_TTL_SEC),
    )
    store = runtime_persistence.initialize()
    initialize_transfer_limits(store.engine, settings)
    # Before the comparison below, which reads a row a peer wrote: a start against an older
    # schema has to fail as a schema failure rather than as a missing column in a read.
    assert_schema_at_head(store.engine)
    # After the refusals, because this call *records* the keyspace when none exists yet: a
    # configuration the mode refuses must never be the one that defines the fleet's.
    validate_deployment_keyspace(SqlDeploymentKeyspaceRepository(store.engine))
    # Only agent roots announce runtime inventory, after composing their catalog.
    log.info("Bootstrapped %s drive_root=%s repo_dir=%s", component, drive_root, repo_dir)
    return ProcessBootstrap(settings=settings, drive_root=drive_root, repo_dir=repo_dir)


def start_process_monitoring() -> None:
    """Attach infrastructure monitoring to this component's process.

    Guarded: monitoring reports on the runtime and is not part of it, so a tier whose OneAgent
    is absent or unusable still serves.
    """
    from ouroboros.observability.dynatrace import initialize_dynatrace_monitoring

    try:
        initialize_dynatrace_monitoring()
    except Exception:
        log.warning("Process monitoring could not be initialized", exc_info=True)


def stop_process_monitoring() -> None:
    """Release what `start_process_monitoring` attached, on the way out of the process."""
    from ouroboros.observability.dynatrace import shutdown_dynatrace_monitoring

    try:
        shutdown_dynatrace_monitoring()
    except Exception:
        log.warning("Process monitoring could not be shut down cleanly", exc_info=True)


def tier_config(app: Starlette, *, tier: str, access_log: bool = True) -> uvicorn.Config | None:
    """``None`` when the configured address is unusable (`.rules/supervisor-process.md`)."""
    from ouroboros.storage.settings.paths import load_path_settings
    from ouroboros.storage.settings.store import load_settings

    host = str(load_settings().OUROBOROS_SERVER_HOST).strip()
    if not host:
        # An empty host is INADDR_ANY, and the schema's default only covers an absent value.
        log.error("The %s tier was configured with a blank host; it binds nothing", tier)
        return None
    port = int(load_path_settings().server_port)
    log.info("The %s tier will serve on %s:%d", tier, host, port)
    return uvicorn.Config(app, host=host, port=port, log_config=None, access_log=access_log)


def serve_tier(server: uvicorn.Server, *, tier: str) -> int:
    try:
        server.run()
    except SystemExit as bind_failure:
        # How uvicorn reports a bind it could not make.
        log.error("The %s tier could not serve on %s:%d", tier, server.config.host, server.config.port)
        return bind_failure.code if isinstance(bind_failure.code, int) else 1
    if not server.started:
        # A failed start-up leaves through `should_exit`, and `started` is never unset once serving.
        log.error("The %s tier stopped before it served on %s:%d", tier, server.config.host, server.config.port)
        return 1
    return 0


def serve_probes(probes: IHealthProbes, *, tier: str) -> Callable[[], None] | None:
    """Probes beside a tier's loop; the returned call stops them, ``None`` means no port.

    Off the main thread uvicorn installs no signal handlers, so the stop signal the tier
    installed for its loop stays in force.
    """
    app = Starlette(routes=probe_routes())
    app.state.health_probes = probes
    config = tier_config(app, tier=tier, access_log=False)
    if config is None:
        return None
    server = uvicorn.Server(config)

    def serve() -> None:
        try:
            serve_tier(server, tier=tier)
        except Exception:
            log.exception("The %s tier's probe server stopped serving", tier)

    thread = threading.Thread(target=serve, name=f"{tier}-probes", daemon=True)
    thread.start()

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=_PROBE_SERVER_WAIT_SEC)
        if thread.is_alive():
            log.warning("The %s tier's probe server did not stop within %.0fs", tier, _PROBE_SERVER_WAIT_SEC)

    deadline = time.monotonic() + _PROBE_SERVER_WAIT_SEC
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(_PROBE_SERVER_POLL_SEC)
    if not server.started:
        # uvicorn binds inside its own startup, so `should_exit` cannot prevent a late bind:
        # the join is what closes it before the tier stops answering for a leaving process.
        stop()
        return None
    return stop


def install_stop_signal(stop_event: threading.Event) -> None:
    """A tier whose main thread is its loop has no uvicorn to install these for it."""
    from ouroboros.platform_layer import install_termination_handlers

    installed = install_termination_handlers(lambda *_: stop_event.set())
    log.info("Termination handlers installed for %s", ", ".join(installed) or "no signal")


__all__ = [
    "ProcessBootstrap",
    "bootstrap_process",
    "install_stop_signal",
    "serve_probes",
    "serve_tier",
    "start_process_monitoring",
    "stop_process_monitoring",
    "tier_config",
]
