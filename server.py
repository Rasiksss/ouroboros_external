"""Headless Starlette/uvicorn entry point for supervisor runtime and health API."""

import asyncio
import logging
import os
import pathlib
import sys
import threading
import time

import uvicorn
from starlette.applications import Starlette

from ouroboros.app.config import init_config, load_settings

init_config()

from new_core.tools.catalog import tool_catalog
from new_core.tools.providers import (
    A2aToolProvider,
    BuiltinToolProvider,
    ExtensionToolProvider,
    McpToolProvider,
    SkillToolProvider,
)
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
from ouroboros.app.server_control import restart_current_process as _restart_current_process_impl
from ouroboros.app.server_entrypoint import find_free_port, parse_server_args, write_port_file
from ouroboros.egress.a2a.manager import a2a_manager
from ouroboros.gateway.browser_ws import get_browser_ws_hub
from ouroboros.gateway.router import collect_routes
from ouroboros.mcp.manager import mcp_manager
from ouroboros.observability.logging_setup import configure_logging, resolve_log_level
from ouroboros.skills.extensions.loader import reload_all
from ouroboros.storage.settings.schema import SettingsLike

# The security config lives at the repo root, so it goes on the path before it is imported:
# the server lists must not depend on the working directory the process was started from.
sys.path.insert(0, str(REPO_DIR))

from security_config.a2a import a2a_remote_agents
from security_config.mcp import mcp_servers

_paths = get_config().paths
DEFAULT_HOST = str(load_settings().OUROBOROS_SERVER_HOST or "127.0.0.1")
DEFAULT_PORT = int(_paths.server_port or 8765)

if not _paths.agent_python:
    _agent_python = sys.executable
    if isinstance(_agent_python, str) and _agent_python:
        os.environ["OUROBOROS_AGENT_PYTHON"] = _agent_python

configure_logging()
log = logging.getLogger("server")

RESTART_EXIT_CODE = 42
_restart_requested = threading.Event()
_agent_system_configuration_publish_attempted = False

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
_event_loop: asyncio.AbstractEventLoop | None = None
_supervisor_thread: threading.Thread | None = None


def _server_profile_unavailable_message(command: str) -> str:
    return f"⚠️ {command} unavailable in server profile."


def _load_enabled_extensions(drive_root: pathlib.Path) -> dict[str, object]:
    """Load every trusted distribution extension or fail startup."""
    results = reload_all(
        pathlib.Path(drive_root),
        load_settings,
        workspace_root=pathlib.Path(REPO_DIR),
        storage_provider=get_storage_provider(),
    )
    failures = {name: error for name, error in results.items() if error and error != "disabled"}
    if failures:
        details = "; ".join(f"{name}: {error}" for name, error in sorted(failures.items()))
        raise RuntimeError(f"Distribution extension startup failed: {details}")
    log.info("Distribution extension skills loaded count=%d", len(results))
    return results


def _publish_agent_system_configuration_startup_event() -> None:
    global _agent_system_configuration_publish_attempted
    if _agent_system_configuration_publish_attempted or not is_server_profile():
        return
    _agent_system_configuration_publish_attempted = True
    try:
        from ouroboros.app.agent_system_configuration_kafka import publish_agent_system_configuration_event

        publish_agent_system_configuration_event(logger=log)
    except Exception:
        log.warning("AGENT_SYSTEM_CONFIGURATION startup publish failed", exc_info=True)


def _start_supervisor_if_needed(settings: SettingsLike) -> bool:
    """Start the supervisor once when runtime providers become available."""
    global _supervisor_thread, _supervisor_error
    if not has_supervisor_provider(settings):
        return False
    if _supervisor_thread and _supervisor_thread.is_alive():
        log.info("Supervisor thread already running; skip duplicate start")
        return False
    _supervisor_error = None
    log.info(
        "Starting supervisor background thread drive_root=%s repo_dir=%s",
        DATA_DIR,
        REPO_DIR,
    )
    _supervisor_thread = threading.Thread(
        target=_run_supervisor,
        args=(settings,),
        daemon=True,
        name="supervisor-main",
    )
    _supervisor_thread.start()
    return True


def _run_supervisor(settings: dict) -> None:
    """Initialize and run the supervisor loop. Called in a background thread."""
    global _supervisor_error, _supervisor_thread

    def _on_error(message: str) -> None:
        global _supervisor_error, _supervisor_thread
        _supervisor_error = message
        _supervisor_thread = None
        log.error("Supervisor thread reported startup/runtime error: %s", message)

    log.info("Supervisor thread entered run_supervisor")
    from supervisor.runtime import run_supervisor

    run_supervisor(
        settings,
        drive_root=DATA_DIR,
        repo_dir=REPO_DIR,
        stop_event=_restart_requested,
        ready_event=_supervisor_ready,
        on_error=_on_error,
        request_restart=_request_restart_exit,
        safe_restart=safe_restart,
        apply_settings_to_env=_apply_settings_to_env,
    )
    _supervisor_thread = None


def _request_restart_exit() -> None:
    """Signal server shutdown with restart exit code."""
    _restart_requested.set()


def safe_restart(reason: str = "", unsynced_policy: str = "") -> tuple[bool, str]:
    """Headless restart hook with no git rollback/update semantics."""
    del reason, unsynced_policy
    _request_restart_exit()
    return True, "restart requested"


APP_START = time.time()


routes = collect_routes(
    data_dir=DATA_DIR,
    repo_dir=REPO_DIR,
    browser_enabled=load_settings().BROWSER_INGRESS_ENABLED,
)

from contextlib import asynccontextmanager, suppress


@asynccontextmanager
async def lifespan(app):
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    browser_hub = get_browser_ws_hub() if load_settings().BROWSER_INGRESS_ENABLED else None
    log.info("Ouroboros lifespan startup begin")

    from ouroboros.app.supervisor_readiness import configure_supervisor_readiness

    configure_supervisor_readiness(_supervisor_ready, lambda: _supervisor_error)
    log.info("Supervisor readiness hooks configured")

    settings, provider_defaults_changed, _provider_default_keys = apply_runtime_provider_defaults(load_settings())
    if provider_defaults_changed:
        save_settings(settings, allow_elevation=True)
        log.info(
            "Applied runtime provider defaults keys=%s",
            sorted(_provider_default_keys),
        )
    try:
        from ouroboros.app.config import validate_production_storage

        validate_production_storage()
        log.info("Production storage configuration validated")
    except Exception:
        log.exception("Production storage configuration invalid")
        raise
    lifespan_drive_root = pathlib.Path(
        app.state.drive_root if hasattr(app, "state") and hasattr(app.state, "drive_root") else DATA_DIR
    )
    log.info(
        "Ouroboros runtime paths drive_root=%s repo_dir=%s",
        lifespan_drive_root,
        REPO_DIR,
    )
    log_level = str(settings.get("OUROBOROS_LOG_LEVEL") or "INFO")
    log_format = str(settings.get("OUROBOROS_LOG_FORMAT") or "json")
    configure_logging(
        level=log_level,
        log_format=log_format,
    )
    log.info(
        "Ouroboros logging configured level=%s format=%s",
        log_level,
        log_format,
    )
    from ouroboros.observability.dynatrace import initialize_dynatrace_monitoring

    initialize_dynatrace_monitoring()

    # The one catalog this process has, in resolution order (first wins a name collision).
    # Wired before the supervisor and the A2A server start, so every later reader — task
    # graph, agent card — sees the same list. The extension, MCP and A2A providers read
    # their sources live, so the extension load and the connects below still show up.
    tool_catalog.initialize(
        [
            BuiltinToolProvider(),
            SkillToolProvider(drive_root=lifespan_drive_root, repo_dir=REPO_DIR),
            ExtensionToolProvider(lifespan_drive_root, REPO_DIR),
            McpToolProvider(),
            A2aToolProvider(a2a_manager, get_a2a_tool_timeout_sec()),
        ]
    )
    log.info("Tool catalog initialized tools=%d", len(tool_catalog.descriptors()))

    mcp_enabled = bool(settings.get("MCP_ENABLED", False))
    if mcp_enabled and mcp_servers:
        try:
            await mcp_manager.initialize(mcp_servers, _event_loop)
            log.info("MCP manager initialized tools=%d", len(mcp_manager.discovered_tools()))
        except Exception:
            log.warning("Failed to initialize MCP manager", exc_info=True)
    elif mcp_enabled:
        log.info("MCP enabled, but security_config/mcp.py lists no servers; MCP manager not initialized")

    a2a_egress_enabled = bool(settings.get("A2A_EGRESS_ENABLED", False))
    if a2a_egress_enabled and a2a_remote_agents:
        try:
            await a2a_manager.initialize(a2a_remote_agents, _event_loop, a2a_egress_enabled)
            log.info("A2A egress manager initialized agents=%d", len(a2a_manager.connections()))
        except Exception:
            log.warning("Failed to initialize the A2A egress manager", exc_info=True)
    elif a2a_egress_enabled:
        log.info("A2A egress enabled, but security_config/a2a.py lists no agents; egress manager not initialized")

    if has_supervisor_provider(settings):
        log.info("Supervisor provider configured; scheduling supervisor startup")
        _start_supervisor_if_needed(settings)
    else:
        _supervisor_ready.set()
        log.info("No supported provider or local routing configured. Supervisor not started.")
        if settings.get("A2A_ENABLED", False):
            log.error(
                "A2A_ENABLED but no remote provider is configured — set "
                "CLOUDRU_FOUNDATION_MODELS_API_KEY or GIGACHAT_B2BANK_* in the environment"
            )

    try:
        log.info("Initializing extension runtime helpers")
        from ouroboros.observability.event_bus import init_global_event_bus
        from ouroboros.skills.extensions.companion import init_global_supervisor

        init_global_event_bus().set_loop(_event_loop)
        init_global_supervisor(lifespan_drive_root)
        if browser_hub is not None:
            await browser_hub.start(_event_loop)
        log.info("Extension runtime helpers initialized")
        await asyncio.to_thread(_load_enabled_extensions, lifespan_drive_root)
    except Exception as exc:
        log.exception("Failed to initialize distribution extension runtime")
        raise RuntimeError("Distribution extensions are not ready") from exc

    a2a_server_task = None
    if settings.get("A2A_ENABLED", False):
        a2a_port = int(settings.get("A2A_PORT", 18800))

        async def _start_a2a_when_supervisor_ready() -> None:
            try:
                from ouroboros.app.supervisor_readiness import wait_for_supervisor_ready
                from ouroboros.ingress.a2a_server import start_a2a_server

                startup_timeout = float(load_settings().OUROBOROS_SUPERVISOR_STARTUP_TIMEOUT_SEC)
                await wait_for_supervisor_ready(
                    timeout_sec=startup_timeout,
                    require_bridge=has_supervisor_provider(settings),
                )
                log.info("Supervisor ready — starting A2A server on port %d", a2a_port)
                await start_a2a_server(settings)
            except Exception:
                log.warning("Failed to start A2A server", exc_info=True)

        a2a_server_task = asyncio.create_task(
            _start_a2a_when_supervisor_ready(),
            name="a2a-server",
        )
        log.info("A2A server task scheduled on port %d (after supervisor ready)", a2a_port)

    try:
        _publish_agent_system_configuration_startup_event()
    except Exception:
        log.warning("AGENT_SYSTEM_CONFIGURATION startup publish failed", exc_info=True)

    log.info("Ouroboros lifespan startup complete; application ready")
    try:
        yield
    finally:
        if browser_hub is not None:
            await browser_hub.stop()
        from ouroboros.observability.dynatrace import shutdown_dynatrace_monitoring

        shutdown_dynatrace_monitoring()
        if a2a_server_task is not None:
            try:
                from ouroboros.ingress.a2a_server import stop_a2a_server

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
            from supervisor.runtime_context import try_get_runtime

            runtime = try_get_runtime()
            if runtime is not None:
                runtime.executor.shutdown()
        except Exception:
            pass
        try:
            from supervisor.message_bus import get_bridge

            get_bridge().shutdown()
        except Exception:
            pass
        try:
            from ouroboros.llm.registry import get_registry

            get_registry().close()
        except Exception:
            log.warning("Failed to close LLM provider clients", exc_info=True)


_starlette_app = Starlette(routes=routes, lifespan=lifespan)
_starlette_app.state.drive_root = pathlib.Path(DATA_DIR)
_starlette_app.state.repo_dir = pathlib.Path(REPO_DIR)
_starlette_app.state.app_start = APP_START
_starlette_app.state.supervisor_ready_event = _supervisor_ready
_starlette_app.state.get_supervisor_error = lambda: _supervisor_error
_starlette_app.state.request_restart = _request_restart_exit
_starlette_app.state.safe_restart = safe_restart
_starlette_app.state.bind_host = _BIND_HOST
_starlette_app.state.port_file = PORT_FILE
_starlette_app.state.default_port = DEFAULT_PORT
_starlette_app.state.start_supervisor_if_needed = _start_supervisor_if_needed
app = _starlette_app


def _emergency_process_cleanup(*, port_sweep: bool = True) -> None:
    """Kill child processes, workers, companions, and runtime port holders."""
    try:
        from ouroboros.tools.shell import kill_all_tracked_subprocesses

        kill_all_tracked_subprocesses()
    except Exception:
        pass
    try:
        from supervisor.runtime_context import try_get_runtime

        runtime = try_get_runtime()
        if runtime is not None:
            runtime.executor.shutdown()
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
    server = uvicorn.Server(config)
    _uvicorn_exited = threading.Event()

    def _check_restart():
        """Monitor restart signal, then shut down uvicorn."""
        while not _restart_requested.is_set():
            time.sleep(0.5)
        log.info("Restart requested — shutting down server.")
        server.should_exit = True

        # Force-exit only if uvicorn never returns; leave cleanup/re-exec time.
        force_exit_timeout_sec = 30
        if _uvicorn_exited.wait(timeout=force_exit_timeout_sec):
            return
        log.warning(
            "Uvicorn did not exit within %ss — running emergency cleanup before os._exit(%d)",
            force_exit_timeout_sec,
            RESTART_EXIT_CODE,
        )
        _emergency_process_cleanup()
        os._exit(RESTART_EXIT_CODE)

    threading.Thread(target=_check_restart, daemon=True).start()

    try:
        server.run()
    finally:
        _uvicorn_exited.set()

    if _restart_requested.is_set():
        log.info("Exiting with code %d (restart signal).", RESTART_EXIT_CODE)
        _emergency_process_cleanup(port_sweep=False)
        _restart_current_process(args.host, actual_port)
        os._exit(RESTART_EXIT_CODE)

    return 0


if __name__ == "__main__":
    sys.exit(main())
