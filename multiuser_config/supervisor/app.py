"""The supervisor tier as its own process: what it composes, and what it does not.

`server.py` is the full runtime — transport, supervisor and agent in one process, under one
gate, so a deployment that wants three supervisor instances and one agent runtime gets three
of each or none. This is the third composition root, beside the browser transport's
(`multiuser_config/transport/browser/app.py`): it composes the task queue, the orchestrator,
the runtime state repository, the outbound bridge over the gateway it built, the agent-event
*consumer* and the loop that ticks them — and it starts **no agent runtime**. Nothing here
names `ouroboros.agent_runtime`, `ouroboros.agent`, the tool substrate, the MCP client
surface or the A2A egress manager; a test parses the import graph. That is the inverse of the
transport root's rule, which forbids `supervisor` — two tiers, two boundaries.

A second root rather than a flag on `server.py`, for the reason the transport root gives:
what a supervisor-only process substitutes is built before the first line of settings-driven
startup runs, and a flag would put deployment topology into import order.

Unlike the transport root this one uses the shared component start-up
(`multiuser_config/bootstrap.py`), and one fact decides that it may: that start-up refuses a
process with no model provider, and the supervisor loop genuinely calls a model — the
duplicate-task check in `supervisor/events.py` builds a prompt and calls the light model on
the loop's own thread. A transport tier holds no such call, which is why its root composes
inline instead.

**The probes answer for the loop this process hosts**: its ready event, the error its callback
recorded and the heartbeat the root hands it, behind the one object the probes read — the only
key published, because with `browser_enabled=False` no mounted route reads anything else
(`.rules/supervisor-process.md`).

**One root per process.** The supervisor runtime registry (`supervisor/runtime_context.py`),
the runtime store factory and the memory-event publisher holder are process globals, so
building two supervisor apps in one interpreter gives the second one's loop the first one's
registry entry. Tests build one at a time.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from starlette.applications import Starlette

from multiuser_config.bootstrap import ProcessBootstrap, bootstrap_process
from multiuser_config.supervisor.dependencies import SupervisorDependencies, build_dependencies
from new_core.coordination.inprocess import InProcessBudget
from new_core.coordination.registry import concurrency_budgets, default_budget_limits
from ouroboros.app.broker_composition import install_memory_event_publisher
from ouroboros.gateway.router import collect_routes
from ouroboros.observability.health import STARTING, IHealthProbes, LoopHeartbeat, database_failure
from ouroboros.storage.blob.async_backend import open_file_blob
from ouroboros.storage.blob.factory import close_storage_provider, initialize_storage_provider
from ouroboros.storage.probe_database import ProbeDatabase, probe_database_factory
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.message_bus import LocalChatBridge
from supervisor.runtime import LOOP_STALL_SEC, TICK_STOP_BUDGET_SEC, SupervisorShutdownError

log = logging.getLogger(__name__)

#: The tier this root serves, and how it names itself in the shared start-up's refusals.
#: One name, because a component that logged one and refused under another would be two.
TIER = "supervisor"
COMPONENT = TIER

#: The loop's shutdown is cooperative: it returns at the top of its next iteration, so what
#: the join has to outlive is the iteration it is inside. That number belongs to the tick and
#: is taken from it rather than restated here — a join equal to one step of the tick (the
#: duplicate check's model deadline, say) would expire on every signal that arrived during
#: that step and abandon the thread mid-write. Still bounded, because the process must leave
#: on a termination signal even when a tick will not return; an expiry is reported rather
#: than swallowed, and this tier holds no run for it to cost.
_LOOP_JOIN_SEC = TICK_STOP_BUDGET_SEC
_retained_resources: AsyncExitStack | None = None


@dataclass(frozen=True)
class SupervisorProcess:
    """What the entrypoint watches: the app it serves, this process's stop, the loop's error.

    Returned rather than kept in module globals: the one-root-per-process trap is already
    paid for by the runtime registry and the store factory, and a second set of globals
    would make a second app in one interpreter silently share this one's shutdown.
    """

    app: Starlette
    stop_event: threading.Event
    get_error: Callable[[], str]


class _ErrorSlot:
    """What the loop reported, written on its thread and read on the probe's."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._message = ""

    def record(self, message: str) -> None:
        with self._lock:
            self._message = str(message or "")
        log.error("The %s loop reported: %s", TIER, message)

    def read(self) -> str:
        with self._lock:
            return self._message


class _SupervisorProbes(IHealthProbes):
    """The loop this process hosts, and what each probe makes of it (`.rules/supervisor-process.md`)."""

    def __init__(
        self, ready_event: threading.Event, errors: _ErrorSlot, heartbeat: LoopHeartbeat, database: ProbeDatabase
    ) -> None:
        self._ready_event = ready_event
        self._errors = errors
        self._heartbeat = heartbeat
        self._database = database

    def startup(self) -> str:
        return "" if self._ready_event.is_set() else STARTING

    async def readiness(self) -> str:
        if not self._ready_event.is_set():
            return STARTING
        return self._errors.read() or await database_failure(self._database)

    def liveness(self) -> str:
        if not self._ready_event.is_set():
            return ""
        return self._errors.read() or self._heartbeat.failure()


def safe_restart() -> tuple[bool, str]:
    """Refuse, naming the tier: a restart handled here would restart a process that runs no task.

    The owner's runs are held by agent-runtime instances this process cannot re-exec; refusing in
    the hook stops `_handle_restart` before it cancels records another instance is executing.
    """
    return False, f"the {TIER} tier runs no task; restart it through the orchestrator"


def build_supervisor_app() -> SupervisorProcess:
    boot = bootstrap_process(COMPONENT)
    probe_database = probe_database_factory.initialize_beside_store(boot.settings)
    _budget_limits = default_budget_limits(
        worker_slots=1,
        llm_max_concurrency=int(boot.settings.get("OUROBOROS_LLM_MAX_CONCURRENCY", 0) or 0),
    )
    concurrency_budgets.initialize(InProcessBudget(_budget_limits))
    # Before anything that could snapshot the holder. The supervisor is a producer of
    # `ChatMessagePersistedEvent` — its chat writer announces every persisted row — and the
    # holder's default publisher drops silently, so a tier that skipped this would run a
    # deployment whose consolidation triggers never fire. Which transport is installed is the
    # selector's decision, not this root's.
    install_memory_event_publisher()
    # Minted per process rather than the tier name: the consumer name is this instance's own,
    # and two instances presenting one would share a pending list and read each other's
    # in-flight envelope as a redelivery. The cost of minting is that a restarted instance
    # leaves a consumer entry behind and its one unsettled envelope waits out the backend's
    # idle bound before a peer reclaims it; a name a deployment supplies would buy both back.
    deps = build_dependencies(inbox_consumer=f"{TIER}-{uuid4().hex}")
    # Built here, the way `server.py` builds it: the constructor takes the outbox and the
    # drive root from the composition root that owns the gateway, so the bridge publishes onto
    # the outbox of the one gateway this process composed rather than resolving either from a
    # process-local registry.
    bridge = LocalChatBridge(deps.gateway.outbox, boot.drive_root)

    stop_event = threading.Event()
    ready_event = threading.Event()
    errors = _ErrorSlot()
    heartbeat = LoopHeartbeat(TIER, stall_after_sec=LOOP_STALL_SEC)
    app = Starlette(
        routes=collect_routes(data_dir=boot.drive_root, repo_dir=boot.repo_dir, browser_enabled=False),
        lifespan=_lifespan_for(
            boot=boot,
            deps=deps,
            bridge=bridge,
            stop_event=stop_event,
            ready_event=ready_event,
            errors=errors,
            heartbeat=heartbeat,
        ),
    )
    # The one object the probes read; the transport root's other keys have no reader here, and
    # nothing here re-execs (`.rules/supervisor-process.md`).
    app.state.health_probes = _SupervisorProbes(ready_event, errors, heartbeat, probe_database)
    # This tier installs no browser identity resolver and resolves no conversation id: it
    # mounts no route that asks the resolver, and it stamps no row that carries a conversation.
    log.info(
        "Composed the %s tier: gateway=%s agent_events=%s multiuser=%s drive_root=%s",
        TIER,
        type(deps.gateway).__name__,
        type(deps.agent_events).__name__,
        bool(boot.settings.get("OUROBOROS_MULTIUSER", False)),
        boot.drive_root,
    )
    return SupervisorProcess(app=app, stop_event=stop_event, get_error=errors.read)


def _lifespan_for(
    *,
    boot: ProcessBootstrap,
    deps: SupervisorDependencies,
    bridge: LocalChatBridge,
    stop_event: threading.Event,
    ready_event: threading.Event,
    errors: _ErrorSlot,
    heartbeat: LoopHeartbeat,
) -> Any:
    """The loop's thread is owned by the lifespan that started it.

    Started here rather than in `__main__.py` so the composed app is exercisable with a test
    client exactly as the transport root's is, and so the shutdown that stops the loop lives
    in the same object that started it.
    """
    import asyncio
    import contextlib

    shutdown_failed = threading.Event()

    def run() -> None:
        from supervisor.runtime import run_supervisor

        try:
            run_supervisor(
                dict(boot.settings),
                drive_root=boot.drive_root,
                repo_dir=boot.repo_dir,
                stop_event=stop_event,
                ready_event=ready_event,
                on_error=errors.record,
                heartbeat=heartbeat,
                # The deployment orchestrator owns replacement; this process only stops.
                request_restart=stop_event.set,
                safe_restart=safe_restart,
                # `apply_settings_to_env` stays unset: it is the "subprocess bridge only"
                # projection of the settings, and this tier spawns no subprocess.
                gateway=deps.gateway,
                bridge=bridge,
                agent_events=deps.agent_events,
                work_signal=deps.work_signal,
                task_stop_publisher=deps.task_stop_publisher,
            )
        except SupervisorShutdownError as exc:
            shutdown_failed.set()
            errors.record(str(exc))

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        global _retained_resources
        # Daemon, and the join below is bounded: the loop stops cooperatively at the top of
        # its next iteration, so a tick that will not return must cost a warning rather than a
        # process an orchestrator has to SIGKILL. This tier holds no run, so there is nothing
        # a longer wait would save.
        thread = threading.Thread(target=run, name="supervisor-main", daemon=True)
        async with AsyncExitStack() as resources:
            resources.callback(runtime_persistence.close)
            file_blob = await resources.enter_async_context(open_file_blob(boot.settings))
            initialize_storage_provider(
                boot.settings,
                file_client=file_blob.client,
                file_loop=asyncio.get_running_loop(),
            )
            resources.callback(close_storage_provider)
            try:
                thread.start()
                yield
            finally:
                # A partial startup must stop its consumers before closing their clients.
                stop_event.set()
                if thread.ident is not None:
                    await asyncio.to_thread(thread.join, _LOOP_JOIN_SEC)
                if thread.is_alive() or shutdown_failed.is_set():
                    _retained_resources = resources.pop_all()
                    # Recorded, not just logged: the process is about to leave a thread
                    # behind in the middle of whatever it was writing, and the exit code is
                    # how an orchestrator learns that this was not a clean stop.
                    errors.record(
                        f"the {TIER} loop did not stop within {_LOOP_JOIN_SEC:.0f}s; the process is leaving it behind"
                    )
            await probe_database_factory.dispose()

    return lifespan


__all__ = ["COMPONENT", "TIER", "SupervisorProcess", "build_supervisor_app", "safe_restart"]
