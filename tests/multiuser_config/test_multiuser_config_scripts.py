"""The per-component launch scripts: their entry point and the wiring that must not drift.

Most of them are not run here, so what is worth naming is what a deployment depends on:
that each tier the mode ships has one entry point, that every tier's start path runs
the whole refusal set — including the phase that reads a peer's row — that the swap point
holds every collaborator which stops being in-process, that each half of a seam follows the
one broker selector rather than a switch of its own, and that the memory dispatch table
still covers every event kind the seam declares.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import pathlib
import threading
from collections.abc import Mapping
from contextlib import AsyncExitStack
from types import ModuleType

import pytest

from background_memory import interfaces as memory_interfaces
from background_memory.infra.events import memory_event_publisher_factory
from background_memory.interfaces import MemoryEvent, NullMemoryEventPublisher
from multiuser_config.tiers import TIERS, Tier
from tests.helpers.memory_event_conformance import reset_memory_event_publisher
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import TEST_CHAT_ENCRYPTION_KEY
from tests.helpers.shared import (
    TEST_SCOPE,
    make_empty_extension_runtime,
    make_empty_skill_snapshot,
    make_skill_availability,
)


def _empty_skill_runtime():
    return make_empty_skill_snapshot(), make_skill_availability(), make_empty_extension_runtime()


@pytest.fixture(autouse=True)
def _clean_memory_publisher():
    """The holder is process-global: a root composed here must not follow the suite around."""
    reset_memory_event_publisher()
    yield
    reset_memory_event_publisher()


def _entrypoint_module(dotted: str) -> ModuleType:
    spec = importlib.util.find_spec(dotted)
    if spec is not None and spec.submodule_search_locations is not None:
        dotted = f"{dotted}.__main__"
    return importlib.import_module(dotted)


def _missing_entrypoints(tiers: Mapping[str, Tier]) -> list[str]:
    missing: list[str] = []
    for name, tier in tiers.items():
        if not tier.module:
            missing.append(name)
            continue
        try:
            module = _entrypoint_module(tier.module)
        except ImportError:
            missing.append(name)
            continue
        if not callable(getattr(module, "main", None)):
            missing.append(name)
    return missing


def test_every_tier_the_mode_ships_exposes_one_entry_point() -> None:
    assert set(TIERS) == {"transport", "supervisor", "agent-runtime", "background-memory", "config-registry"}
    assert _missing_entrypoints(TIERS) == []


def test_a_tier_named_with_no_entry_point_fails_the_check() -> None:
    assert _missing_entrypoints({"ghost": Tier("multiuser_config.ghost.run", ())}) == ["ghost"]
    assert _missing_entrypoints({"nameless": Tier("", ())}) == ["nameless"]


def test_each_tier_names_the_owner_facing_paths_its_root_mounts() -> None:
    from multiuser_config.config_registry.app import CHATS_MOUNT_PATH, MOUNT_PATH, SETTINGS_MOUNT_PATH
    from ouroboros.gateway.router import collect_routes

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    transport_paths = {
        route.path for route in collect_routes(data_dir=repo_root, repo_dir=repo_root, browser_enabled=True)
    }

    assert set(TIERS["config-registry"].paths) == {MOUNT_PATH, SETTINGS_MOUNT_PATH, CHATS_MOUNT_PATH}
    for path in TIERS["transport"].paths:
        assert any(mounted == path or mounted.startswith(f"{path}/") for mounted in transport_paths), (
            f"the transport tier names {path}, which its root does not mount"
        )

    claimed = [path for tier in TIERS.values() for path in tier.paths]
    assert len(claimed) == len(set(claimed)), f"two tiers claim one path: {claimed}"


def test_the_three_scripted_tiers_name_the_component_they_refuse_under() -> None:
    """One shape for all three: the module that holds `main` also names the component.

    The supervisor package has no `run.py` — its entry point is a `__main__` beside a
    composition root — so it re-exports the name its root declares rather than growing a
    special case here. The transport and config-registry tiers are absent by design: both
    compose inline and name their tier, not a component the shared start-up refuses under.
    """
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from multiuser_config.background_memory import run as background_memory_run
    from multiuser_config.supervisor import __main__ as supervisor_main

    for module in (agent_runtime_run, background_memory_run, supervisor_main):
        assert callable(module.main)
        assert module.COMPONENT


@pytest.fixture
def keyspace_calls(monkeypatch) -> list[str]:
    """Every place a tier could reach the deployment keyspace comparison, recorded.

    Two targets, because the transport root imports the name at module scope while the
    shared start-up imports it at call time: patching one of them proves nothing about the
    tier that reads the other.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        "ouroboros.app.multiuser_validation.validate_deployment_keyspace",
        lambda repository: calls.append("shared start-up"),
    )
    monkeypatch.setattr(
        "multiuser_config.transport.browser.app.validate_deployment_keyspace",
        lambda repository: calls.append("transport root"),
    )
    return calls


@pytest.fixture
def model_provider(monkeypatch):
    """The credentials the shared start-up refuses a component process without."""
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "https://models.example/v1")
    yield


@pytest.fixture
def db():
    from tests.helpers.background_memory_db import memory_database

    yield from memory_database()


@pytest.fixture(autouse=True)
def probe_server_stubbed(monkeypatch):
    """The probe server is this suite's noise: `tests/multiuser_config/test_worker_tier_probes.py` exercises it."""
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from multiuser_config.background_memory import run as background_memory_run

    for module in (agent_runtime_run, background_memory_run):
        monkeypatch.setattr(module, "serve_probes", lambda _probes, **_kwargs: lambda: None)


def test_the_transport_tier_compares_the_keyspace_when_it_composes(keyspace_calls, monkeypatch) -> None:
    """It composes inline rather than through the shared start-up, so it is checked alone."""
    from multiuser_config.transport.browser.app import build_transport_app
    from tests.helpers.agent_registry import FakeAgentRegistry
    from tests.helpers.security_context import TEST_CREDENTIALS_KEY
    from transport.browser.identity import ConfiguredOwnerIdentity, browser_identity
    from transport.browser.ws import reset_browser_ws_state_for_tests

    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", TEST_CREDENTIALS_KEY)
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", "")
    try:
        build_transport_app()
    finally:
        reset_browser_ws_state_for_tests()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))

    assert keyspace_calls == ["transport root"]


def test_the_supervisor_tier_compares_the_keyspace_through_the_shared_start_up(keyspace_calls, model_provider) -> None:
    from multiuser_config.supervisor.app import build_supervisor_app
    from supervisor.runtime_context import reset_runtime_for_tests

    process = build_supervisor_app()
    try:
        assert keyspace_calls == ["shared start-up"]
    finally:
        process.stop_event.set()
        reset_runtime_for_tests()


def test_the_agent_runtime_tier_compares_the_keyspace_through_the_shared_start_up(
    keyspace_calls, model_provider, monkeypatch
) -> None:
    """A tier that runs no owner-facing surface still writes that owner's objects."""
    from multiuser_config.agent_runtime import run as agent_runtime_run

    monkeypatch.setattr(agent_runtime_run, "compose_process", lambda _boot, **_kwargs: _empty_skill_runtime())
    monkeypatch.setattr(agent_runtime_run, "install_stop_signal", lambda _stop: None)
    monkeypatch.setattr(agent_runtime_run, "build_dependencies", lambda **_kwargs: _StubDependencies())
    monkeypatch.setattr("ouroboros.agent_runtime.bootstrap.build_agent_runtime", lambda **_kwargs: _StubRuntime())

    assert agent_runtime_run.main() == 0
    assert keyspace_calls == ["shared start-up"]


def test_the_background_memory_tier_compares_the_keyspace_through_the_shared_start_up(
    keyspace_calls, model_provider, monkeypatch
) -> None:
    from multiuser_config.background_memory import run as background_memory_run

    monkeypatch.setattr(background_memory_run, "install_stop_signal", lambda _stop: None)
    monkeypatch.setattr(background_memory_run, "build_dependencies", lambda **_kwargs: _StubMemoryDependencies())
    monkeypatch.setattr("background_memory.runtime.MemoryRuntime", _StubMemoryRuntime)

    assert background_memory_run.main() == 0
    assert keyspace_calls == ["shared start-up"]


def test_the_background_memory_tier_composes_the_engine_its_repositories_need(
    keyspace_calls, model_provider, monkeypatch
) -> None:
    """Without it the tier's first repository call raises that no engine is composed."""
    from background_memory.infra.repositories import SqlConsolidationLeaseRepo
    from multiuser_config.background_memory import run as background_memory_run
    from tests.helpers.shared import TEST_SCOPE

    del keyspace_calls
    composed: dict = {}

    def _capture(**kwargs):
        composed.update(kwargs)
        return _StubMemoryDependencies()

    class _ReadingRuntime:
        def __init__(self, _consumer, *, handlers, stop_event, heartbeat, drain_timeout_sec) -> None:
            del handlers, stop_event, heartbeat, drain_timeout_sec

        async def execute(self) -> None:
            composed["lease"] = await SqlConsolidationLeaseRepo(composed["engine"]).read(TEST_SCOPE)

    monkeypatch.setattr(background_memory_run, "install_stop_signal", lambda _stop: None)
    monkeypatch.setattr(background_memory_run, "build_dependencies", _capture)
    monkeypatch.setattr("background_memory.runtime.MemoryRuntime", _ReadingRuntime)

    assert background_memory_run.main() == 0
    assert composed["lease"] is None


def test_the_background_memory_tier_composes_its_own_concurrency_limits(
    keyspace_calls, model_provider, monkeypatch
) -> None:
    """Left uncomposed the tier runs on the seam's fallback, which no setting can move."""
    from multiuser_config.background_memory import run as background_memory_run
    from new_core.coordination.registry import MODEL_CALLS, concurrency_budgets

    del keyspace_calls
    monkeypatch.setenv("OUROBOROS_LLM_MAX_CONCURRENCY", "9")
    monkeypatch.setattr(background_memory_run, "install_stop_signal", lambda _stop: None)
    monkeypatch.setattr(background_memory_run, "build_dependencies", lambda **_kwargs: _StubMemoryDependencies())
    composed: list[int] = []

    class _ReadingRuntime(_StubMemoryRuntime):
        async def execute(self) -> None:
            composed.append(concurrency_budgets.budgets.limit(MODEL_CALLS))

    monkeypatch.setattr("background_memory.runtime.MemoryRuntime", _ReadingRuntime)

    assert background_memory_run.main() == 0
    assert composed == [9]


class _StubMemoryDependencies:
    """What `build_dependencies` returns, with nothing this case needs from it."""

    consumer = None
    handlers: dict = {}

    @staticmethod
    async def release() -> None:
        return None


class _StubMemoryRuntime:
    """A loop that returns immediately, so `main` reaches its end without a broker."""

    def __init__(self, _consumer, *, handlers, stop_event, heartbeat, drain_timeout_sec) -> None:
        del handlers, stop_event, heartbeat, drain_timeout_sec

    async def execute(self) -> None:
        return None


@pytest.fixture
def monitoring_calls(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        "ouroboros.observability.dynatrace.initialize_dynatrace_monitoring",
        lambda: calls.append("start shared start-up") or True,
    )
    monkeypatch.setattr(
        "ouroboros.observability.dynatrace.shutdown_dynatrace_monitoring",
        lambda: calls.append("stop shared start-up"),
    )
    return calls


def test_the_transport_tier_monitors_its_own_process(monitoring_calls, monkeypatch) -> None:
    """It composes inline rather than through `bootstrap_process`, so it is checked alone."""
    from starlette.testclient import TestClient

    from multiuser_config.transport.browser.app import build_transport_app
    from tests.helpers.agent_registry import FakeAgentRegistry
    from tests.helpers.security_context import TEST_CREDENTIALS_KEY
    from transport.browser.identity import ConfiguredOwnerIdentity, browser_identity
    from transport.browser.ws import reset_browser_ws_state_for_tests

    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", TEST_CREDENTIALS_KEY)
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", "")
    try:
        with TestClient(build_transport_app()):
            assert monitoring_calls == ["start shared start-up"]
    finally:
        reset_browser_ws_state_for_tests()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))

    assert monitoring_calls == ["start shared start-up", "stop shared start-up"]


def test_the_agent_registry_tier_monitors_its_own_process(monitoring_calls) -> None:
    """The second root that composes inline, and it has to make the call itself.

    Monitoring attaches to a process, so a deployment of ten is monitored only if each of
    them attaches its own — an initialization only one inline root performs leaves half the
    fleet reporting a deployment that half exists.
    """
    from starlette.testclient import TestClient

    from multiuser_config.config_registry.app import build_registry_app

    with TestClient(build_registry_app()):
        assert monitoring_calls == ["start shared start-up"]

    assert monitoring_calls == ["start shared start-up", "stop shared start-up"]


def test_the_supervisor_tier_monitors_its_process_through_the_shared_start_up(monitoring_calls, model_provider) -> None:
    from multiuser_config.supervisor.app import build_supervisor_app
    from supervisor.runtime_context import reset_runtime_for_tests

    process = build_supervisor_app()
    try:
        assert monitoring_calls == ["start shared start-up"]
    finally:
        process.stop_event.set()
        reset_runtime_for_tests()


def test_the_agent_runtime_tier_monitors_its_process_and_releases_it(
    monitoring_calls, model_provider, monkeypatch
) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run

    monkeypatch.setattr(agent_runtime_run, "compose_process", lambda _boot, **_kwargs: _empty_skill_runtime())
    monkeypatch.setattr(agent_runtime_run, "install_stop_signal", lambda _stop: None)
    monkeypatch.setattr(agent_runtime_run, "build_dependencies", lambda **_kwargs: _StubDependencies())
    monkeypatch.setattr("ouroboros.agent_runtime.bootstrap.build_agent_runtime", lambda **_kwargs: _StubRuntime())

    assert agent_runtime_run.main() == 0
    assert monitoring_calls == ["start shared start-up", "stop shared start-up"]


def test_the_background_memory_tier_monitors_its_process_and_releases_it(
    monitoring_calls, model_provider, monkeypatch
) -> None:
    from multiuser_config.background_memory import run as background_memory_run

    monkeypatch.setattr(background_memory_run, "install_stop_signal", lambda _stop: None)
    monkeypatch.setattr(background_memory_run, "build_dependencies", lambda **_kwargs: _StubMemoryDependencies())
    monkeypatch.setattr("background_memory.runtime.MemoryRuntime", _StubMemoryRuntime)

    assert background_memory_run.main() == 0
    assert monitoring_calls == ["start shared start-up", "stop shared start-up"]


def test_a_monitoring_failure_does_not_stop_a_tier(model_provider, monkeypatch, caplog) -> None:
    """Monitoring reports on the runtime and is not part of it."""

    def _explode() -> bool:
        raise RuntimeError("no OneAgent here")

    monkeypatch.setattr("ouroboros.observability.dynatrace.initialize_dynatrace_monitoring", _explode)
    from multiuser_config import bootstrap

    boot = bootstrap.bootstrap_process("test-tier")

    assert boot.settings is not None


def test_the_supervisor_tiers_swap_point_holds_every_collaborator_that_leaves_the_process() -> None:
    """The mirror of the runtime's swap point, and until now called by no test at all.

    `build_dependencies` is the one substitution point of the supervisor root
    (`.rules/supervisor-process.md`), so a resolver that stopped following the selector
    would be composed here and nowhere else.
    """
    from multiuser_config.supervisor.dependencies import build_dependencies
    from new_core.coordination.inprocess import InProcessWorkSignal
    from ouroboros.agent_events.inmemory import InMemoryAgentEventConsumer
    from ouroboros.app.task_stop_notifications import InProcessTaskStopPublisher
    from transport.infra.inmemory import InProcessMessageGateway

    deps = build_dependencies(inbox_consumer="supervisor-1")

    assert isinstance(deps.gateway, InProcessMessageGateway)
    assert isinstance(deps.agent_events, InMemoryAgentEventConsumer)
    assert isinstance(deps.work_signal, InProcessWorkSignal)
    assert isinstance(deps.task_stop_publisher, InProcessTaskStopPublisher)


def test_the_supervisor_tier_composes_the_broker_the_configuration_names(monkeypatch) -> None:
    """Both reading halves move together, and the consuming one is never the in-process one.

    An unconditional in-process consumer here passes the whole gate otherwise: the tier
    drains a queue in its own process that no agent runtime publishes to, every report is
    stuck behind the publishers' back-pressure, no owner is answered — and readiness still
    reports 200, because the loop this process hosts is running.
    """
    from multiuser_config.supervisor.dependencies import build_dependencies
    from new_core.coordination.inprocess import InProcessWorkSignal
    from ouroboros.agent_events.redis_transport import RedisAgentEventConsumer
    from ouroboros.app.task_stop_notifications import RedisTaskStopPublisher
    from transport.infra.redis_gateway import RedisMessageGateway

    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "redis://localhost:6379/0")

    deps = build_dependencies(inbox_consumer="supervisor-1")

    assert isinstance(deps.gateway, RedisMessageGateway)
    assert isinstance(deps.agent_events, RedisAgentEventConsumer)
    assert isinstance(deps.work_signal, InProcessWorkSignal), "local by decision, not by omission"
    assert isinstance(deps.task_stop_publisher, RedisTaskStopPublisher)


def test_the_supervisor_tier_requires_this_instances_own_inbox_consumer_name() -> None:
    """Two instances presenting one name read each other's in-flight envelope."""
    from multiuser_config.supervisor.dependencies import build_dependencies

    with pytest.raises(ValueError, match="consumer"):
        build_dependencies(inbox_consumer="")


def test_the_runtimes_swap_point_holds_every_collaborator_that_leaves_the_process(
    tmp_path: pathlib.Path,
) -> None:
    """Both outbound paths and the wake-up, so a broker swap cannot cover only one of them."""
    from multiuser_config.agent_runtime.dependencies import build_dependencies
    from new_core.coordination.inprocess import InProcessWorkSignal
    from ouroboros.agent_events.inmemory import InMemoryAgentEventPublisher
    from ouroboros.app.task_stop_notifications import InProcessTaskStopConsumer
    from supervisor.message_bus import LocalChatBridge
    from transport.infra.inmemory import InProcessOutbox

    deps = build_dependencies(drive_root=tmp_path)

    assert isinstance(deps.events, InMemoryAgentEventPublisher)
    assert isinstance(deps.outbound, LocalChatBridge)
    assert isinstance(deps.outbound._outbox, InProcessOutbox)
    assert isinstance(deps.work_signal, InProcessWorkSignal)
    assert isinstance(deps.task_stops, InProcessTaskStopConsumer)
    # The memory publisher is deliberately *not* here: it is installed into the process-local
    # holder by `run.py`, before `compose_process` composes anything that could snapshot it.
    assert isinstance(memory_event_publisher_factory.memory_publisher, NullMemoryEventPublisher)


def test_the_runtime_composes_the_broker_the_configuration_names(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Both outbound paths move together: `ctx.outbound` is the only live path for an attachment.

    `work_signal` stays local by decision — both ends of the signal that matters are in this
    process, and the half that crosses costs at most one poll interval of latency.
    """
    from multiuser_config.agent_runtime.dependencies import build_dependencies
    from new_core.coordination.inprocess import InProcessWorkSignal
    from ouroboros.agent_events.redis_transport import RedisAgentEventPublisher
    from ouroboros.app.task_stop_notifications import RedisTaskStopConsumer
    from supervisor.message_bus import LocalChatBridge
    from transport.infra.redis_gateway import RedisOutbox

    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "redis://localhost:6379/0")

    deps = build_dependencies(drive_root=tmp_path)

    assert isinstance(deps.events, RedisAgentEventPublisher)
    assert isinstance(deps.outbound, LocalChatBridge)
    assert isinstance(deps.outbound._outbox, RedisOutbox), "an attachment publishes onto the outbox, not an event"
    assert isinstance(deps.work_signal, InProcessWorkSignal)
    assert isinstance(deps.task_stops, RedisTaskStopConsumer)


def test_the_producer_root_installs_the_memory_publisher_before_it_composes_a_reader(tmp_path, monkeypatch) -> None:
    """A collaborator handed the publisher at construction holds what was installed then.

    Watched at `compose_process`, which is the step this root's docstring says a deployment
    fills with the readers that snapshot the holder — the skill bridge and the A2A server,
    both handed `memory_events=` as an argument in `server.py`. Watching a step that never
    reads the holder would pass whatever the order was.
    """
    import os

    from multiuser_config.agent_runtime import run as agent_runtime_run
    from multiuser_config.bootstrap import ProcessBootstrap
    from ouroboros.storage.settings.schema import AppSettings
    from tests.helpers.schema_harness import RUNTIME_DB_URL_PREFIX

    seen: list[str] = []
    boot = ProcessBootstrap(
        settings=AppSettings(**{f"{RUNTIME_DB_URL_PREFIX}_1": os.environ[f"{RUNTIME_DB_URL_PREFIX}_1"]}),
        drive_root=tmp_path,
        repo_dir=tmp_path,
    )
    monkeypatch.setattr(agent_runtime_run, "bootstrap_process", lambda _component: boot)
    monkeypatch.setattr(agent_runtime_run, "install_stop_signal", lambda _stop: None)

    def compose(_boot, **_kwargs):
        seen.append(type(memory_event_publisher_factory.memory_publisher).__name__)
        return _empty_skill_runtime()

    monkeypatch.setattr(agent_runtime_run, "compose_process", compose)
    monkeypatch.setattr(agent_runtime_run, "build_dependencies", lambda **_kwargs: _StubDependencies())
    monkeypatch.setattr(
        "ouroboros.agent_runtime.bootstrap.build_agent_runtime",
        lambda **_kwargs: _StubRuntime(),
    )

    assert agent_runtime_run.main() == 0

    # The holder is reset to the null publisher before every case here, so an install that
    # happened after this step would be visible as exactly that name.
    assert seen == ["InMemoryEventPublisher"], (
        "a reader composed before the install would hold the null publisher for the process's life"
    )
    assert NullMemoryEventPublisher.__name__ not in seen


class _StubDependencies:
    """What `build_dependencies` returns, with nothing this case needs from it."""

    events = None
    outbound = None
    work_signal = None
    task_stops = None


class _StubRuntime:
    """A runtime that starts and stops, so `main` reaches its end without a real one."""

    runtime_id = "runtime-under-test"

    def execute(self) -> None:
        return None

    def drain(self, *, deadline_sec: float) -> bool:
        return True


def test_the_consuming_process_installs_no_publisher(tmp_path, db) -> None:
    """A publisher in the tier that only consumes would mean that process talking to itself."""
    from multiuser_config.background_memory.dependencies import build_dependencies

    build_dependencies(
        drive_root=tmp_path,
        repo_dir=pathlib.Path.cwd(),
        stop_event=threading.Event(),
        engine=db.engine,
    )

    assert isinstance(memory_event_publisher_factory.memory_publisher, NullMemoryEventPublisher)


def test_the_memory_tier_says_at_startup_that_the_default_consumes_nothing(tmp_path, caplog, db) -> None:
    """Invisible otherwise: the loop starts, says so, and polls an empty queue forever."""
    from multiuser_config.background_memory.dependencies import build_dependencies

    with caplog.at_level("WARNING", logger="multiuser_config.background_memory.dependencies"):
        build_dependencies(
            drive_root=tmp_path,
            repo_dir=pathlib.Path.cwd(),
            stop_event=threading.Event(),
            engine=db.engine,
        )

    assert any("consume nothing" in record.getMessage() for record in caplog.records)


def test_the_memory_tier_composes_the_broker_the_configuration_names(tmp_path, monkeypatch, db) -> None:
    """The consumer moves with the one selector; there is no switch of this seam's own."""
    from background_memory.infra.events.redis_transport import RedisMemoryEventConsumer
    from multiuser_config.background_memory.dependencies import build_dependencies

    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "redis://localhost:6379/0")

    deps = build_dependencies(
        drive_root=tmp_path,
        repo_dir=pathlib.Path.cwd(),
        stop_event=threading.Event(),
        engine=db.engine,
    )

    assert isinstance(deps.consumer, RedisMemoryEventConsumer)
    assert isinstance(memory_event_publisher_factory.memory_publisher, NullMemoryEventPublisher)


def test_the_memory_dispatch_table_covers_every_declared_event_kind(tmp_path: pathlib.Path, db) -> None:
    """A kind added to the seam and not wired here would be dropped with a warning."""
    from background_memory.infra.events.inmemory import InMemoryEventConsumer
    from multiuser_config.background_memory.dependencies import build_dependencies

    declared = {
        kind
        for name in memory_interfaces.__all__
        if isinstance(kind := getattr(memory_interfaces, name), type)
        and issubclass(kind, MemoryEvent)
        and kind is not MemoryEvent
    }

    deps = build_dependencies(
        drive_root=tmp_path,
        repo_dir=pathlib.Path.cwd(),
        stop_event=threading.Event(),
        engine=db.engine,
    )

    assert set(deps.handlers) == declared
    assert isinstance(deps.consumer, InMemoryEventConsumer)
    persisted = deps.handlers[memory_interfaces.ChatMessagePersistedEvent].__self__
    assert (
        deps.handlers[memory_interfaces.MemoryDistillationEvent].__self__._tasks
        is persisted._consolidation._foreground_pressure._tasks
    )


# --- the tool sources the tier connects to ----------------------------------------


class _FakeMcpManager:
    """The process-wide manager, with only what a provider and a shutdown ask of it."""

    def __init__(self, tool_names: tuple[str, ...] = (), connect_error: Exception | None = None) -> None:
        self._tool_names = tool_names
        self._connect_error = connect_error
        self._initialized = False
        self.servers: list = []
        self.shutdowns = 0

    async def initialize(self, servers, loop, auth_dependencies) -> None:
        del loop
        self.auth_dependencies = auth_dependencies
        if self._connect_error is not None:
            raise self._connect_error
        self.servers = list(servers)
        self._initialized = True

    def is_available(self) -> bool:
        return self._initialized

    def discovered_tools(self) -> list:
        from types import SimpleNamespace

        from new_core.tools.schema import ToolSchema

        return [
            SimpleNamespace(server_id="shared", schema=ToolSchema(name=name, description=name, parameters={}))
            for name in self._tool_names
        ]

    def retrieval_aliases(self, full_name: str) -> tuple:
        del full_name
        return ()

    def tool_result_limit(self, full_name: str) -> int | None:
        del full_name
        return None

    async def shutdown(self) -> None:
        self.shutdowns += 1
        self._initialized = False


class _FakeA2aConnection:
    def __init__(self, entry) -> None:
        from types import SimpleNamespace

        from new_core.tools.schema import ToolSchema

        self.entry = SimpleNamespace(
            id=entry.id,
            retrieval_aliases=tuple(entry.retrieval_aliases),
            tool_result_limit=entry.tool_result_limit,
        )
        self.tool_name = f"a2a__{entry.id}"
        self.schema = ToolSchema(name=self.tool_name, description=entry.name or entry.id, parameters={})


class _FakeA2aManager:
    def __init__(self, *, failed_ids: tuple[str, ...] = ()) -> None:
        self._initialized = False
        self.failed_ids = frozenset(failed_ids)
        self.agents: list = []
        self.loop = None
        self.dependencies = None
        self.initializations = 0
        self.shutdowns = 0
        self._connections: list[_FakeA2aConnection] = []

    async def initialize(self, agents, loop, egress_enabled, dependencies) -> None:
        self.initializations += 1
        self.agents = list(agents)
        self.loop = loop
        self.dependencies = dependencies
        self._connections = [
            _FakeA2aConnection(entry) for entry in self.agents if entry.id not in self.failed_ids and egress_enabled
        ]
        self._initialized = True

    def is_available(self) -> bool:
        return self._initialized

    def connections(self) -> list[_FakeA2aConnection]:
        return list(self._connections)

    def connection_for_tool(self, tool_name: str):
        return next((connection for connection in self._connections if connection.tool_name == tool_name), None)

    async def shutdown(self) -> None:
        self.shutdowns += 1
        self._initialized = False
        self._connections.clear()


@pytest.fixture
def fake_mcp(monkeypatch):
    """The manager the tier's provider reaches, and one configured server to reach it with."""
    from ouroboros.storage.settings.schema import McpServerEntry

    manager = _FakeMcpManager(tool_names=("shared__search",))
    monkeypatch.setattr("ouroboros.mcp.manager.mcp_manager", manager)
    entry = McpServerEntry(id="shared", transport="streamable_http", url="https://shared.example/mcp")
    monkeypatch.setattr("security_config.mcp.mcp_servers", [entry])
    return manager


@pytest.fixture
def fake_a2a(monkeypatch):
    from ouroboros.storage.settings.schema import A2aRemoteAgentEntry

    manager = _FakeA2aManager()
    monkeypatch.setattr("ouroboros.egress.a2a.manager.a2a_manager", manager)
    entries = [
        A2aRemoteAgentEntry(id="researcher", name="Researcher", url="https://researcher.example/a2a"),
        A2aRemoteAgentEntry(id="writer", name="Writer", url="https://writer.example/a2a"),
    ]
    monkeypatch.setattr("security_config.a2a.a2a_remote_agents", entries)
    return manager


@pytest.fixture
def tier_loop():
    """The loop `main` owns, given to the composition the way the tier gives it."""
    from multiuser_config.agent_runtime.tool_client_loop import start_tool_client_loop

    loop, stop = start_tool_client_loop()
    yield loop
    stop()


@pytest.fixture
def shared_mcp_selection():
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    reset_user_settings_for_tests(StubUserSettingsStore({"OUROBOROS_USER_ENABLED_MCP_IDS": '["shared"]'}))


_UNUSABLE_CREDENTIALS_KEYS = {
    "empty key": {"raw": ""},
    "short key": {"raw": "short-s3cret-key"},
    "a chat encryption key": {"raw": TEST_CHAT_ENCRYPTION_KEY},
}


def _boot(tmp_path: pathlib.Path, **settings):
    import os

    from multiuser_config.bootstrap import ProcessBootstrap
    from ouroboros.storage.settings.schema import AppSettings
    from tests.helpers.schema_harness import RUNTIME_DB_URL_PREFIX
    from tests.helpers.security_context import TEST_CREDENTIALS_KEY

    return ProcessBootstrap(
        settings=AppSettings(
            **{
                "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY": TEST_CREDENTIALS_KEY,
                f"{RUNTIME_DB_URL_PREFIX}_1": os.environ[f"{RUNTIME_DB_URL_PREFIX}_1"],
                **settings,
            }
        ),
        drive_root=tmp_path,
        repo_dir=tmp_path,
    )


@pytest.mark.parametrize("component", ["supervisor", "agent-runtime", "background-memory"])
def test_shared_bootstrap_does_not_announce_an_uncomposed_catalog(monkeypatch, component) -> None:
    from multiuser_config.bootstrap import bootstrap_process
    from ouroboros.app import agent_system_configuration_kafka as announcement

    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "https://models.example/v1")
    monkeypatch.setattr("ouroboros.storage.settings.profile.is_server_profile", lambda: True)
    calls = []
    monkeypatch.setattr(announcement, "publish_agent_system_configuration_event", lambda **kwargs: calls.append(kwargs))

    bootstrap_process(component)

    assert calls == []


@pytest.mark.parametrize("production", [True, False])
@pytest.mark.parametrize("mcp_enabled", [True, False])
def test_agent_configuration_is_announced_only_after_catalog_composition(
    tmp_path, monkeypatch, fake_mcp, fake_a2a, tier_loop, restore_process_singletons, production, mcp_enabled
) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from ouroboros.app import agent_system_configuration_kafka as announcement

    monkeypatch.setattr("ouroboros.storage.settings.profile.is_server_profile", lambda: production)
    snapshots = []

    def announce(**kwargs):
        snapshots.append(announcement._collect_tools())
        return True

    monkeypatch.setattr(announcement, "publish_agent_system_configuration_event", announce)
    agent_runtime_run.compose_process(
        _boot(tmp_path, MCP_ENABLED=mcp_enabled, A2A_EGRESS_ENABLED=True), tool_client_loop=tier_loop
    )

    if not production:
        assert snapshots == []
        return
    assert len(snapshots) == 1
    tools = snapshots[0]
    names = {tool["tool-name"] for tool in tools}
    assert ("shared__search" in names) is mcp_enabled
    assert {"a2a__researcher", "a2a__writer"} <= names


@pytest.mark.parametrize("disabled", [True, False])
def test_startup_mcp_inventory_preserves_owner_access_and_tool_policy(
    tmp_path, monkeypatch, fake_mcp, tier_loop, restore_process_singletons, disabled
) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog
    from ouroboros.app import agent_system_configuration_kafka as announcement
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    monkeypatch.setenv("OUROBOROS_DISABLED_TOOLS", "shared__search" if disabled else "")
    store = StubUserSettingsStore(by_pair={(TEST_SCOPE.owner_id, TEST_SCOPE.agent_id): {}})
    reset_user_settings_for_tests(store)
    agent_runtime_run.compose_process(_boot(tmp_path, MCP_ENABLED=True), tool_client_loop=tier_loop)
    before = announcement._collect_tools()
    reported = {tool["tool-name"]: tool for tool in before}
    assert reported["shared__search"]["active-tool"] is not disabled
    assert not [d for d in tool_catalog.descriptors(TEST_SCOPE) if d.origin.startswith("mcp:")]

    store.put(TEST_SCOPE.owner_id, TEST_SCOPE.agent_id, "OUROBOROS_USER_ENABLED_MCP_IDS", '["shared"]')

    assert "shared__search" in {d.name for d in tool_catalog.descriptors(TEST_SCOPE)}
    assert announcement._collect_tools() == before


class _RecordingMcpManager(_FakeMcpManager):
    def __init__(self) -> None:
        super().__init__(tool_names=("shared__search",))
        self.scopes: list = []

    def resolve(self, full_name: str) -> tuple[str, str] | None:
        return ("shared", "search") if full_name == "shared__search" else None

    def call_tool_sync(self, scope, server_id, tool_name, arguments, timeout_sec) -> str:
        self.scopes.append(scope)
        return "ok"


def test_the_agent_tier_installs_the_sql_security_context_store(
    tmp_path, monkeypatch, fake_mcp, tier_loop, shared_mcp_selection, restore_process_singletons, runtime_db_url
) -> None:
    from infra.security_context import security_context, security_context_collector
    from infra.security_context.sql import SqlSecurityContextStore
    from infra.user_tokens.sql import SqlUserTokenStore
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog
    from new_core.tools.providers import McpToolProvider
    from tests.helpers.security_context import (
        context_of,
        install_fresh_security_context_collector,
        install_fresh_security_context_store,
        parameters_of,
    )
    from tests.helpers.shared import make_tool_ctx

    manager = _RecordingMcpManager()
    monkeypatch.setattr("ouroboros.mcp.manager.mcp_manager", manager)
    sentinel_store = install_fresh_security_context_store()
    sentinel_collector = install_fresh_security_context_collector()

    agent_runtime_run.compose_process(_boot(tmp_path, MCP_ENABLED=True), tool_client_loop=tier_loop)

    store = security_context.store
    assert store is not sentinel_store
    assert isinstance(store, SqlSecurityContextStore)
    assert security_context_collector.collector is sentinel_collector
    (provider,) = [provider for provider in tool_catalog.providers if isinstance(provider, McpToolProvider)]
    assert provider.execute(make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path), "shared__search", {}) == "ok"
    assert manager.auth_dependencies.security_context_store is store
    assert isinstance(manager.auth_dependencies.user_token_store, SqlUserTokenStore)
    assert len(manager.scopes) == 1
    parameters = parameters_of(headers={"authorization": "Bearer secret"})
    loop = asyncio.get_event_loop()
    loop.run_until_complete(store.save(TEST_SCOPE.security_token, parameters))
    keys = {parameter.key for parameter in parameters}
    assert loop.run_until_complete(store.load(TEST_SCOPE.security_token, keys)) == context_of(
        headers={"authorization": "Bearer secret"}
    )


@parametrize_from_dict(_UNUSABLE_CREDENTIALS_KEYS)
def test_the_agent_tier_refuses_to_compose_without_a_valid_credentials_key(
    tmp_path, fake_mcp, tier_loop, restore_process_singletons, raw
) -> None:
    from infra.security_context import security_context
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from tests.helpers.security_context import install_fresh_security_context_store

    sentinel_store = install_fresh_security_context_store()

    with pytest.raises(ValueError, match="OUROBOROS_CREDENTIALS_ENCRYPTION_KEY") as refused:
        agent_runtime_run.compose_process(
            _boot(
                tmp_path,
                MCP_ENABLED=True,
                OUROBOROS_CHAT_ENCRYPTION_KEY=TEST_CHAT_ENCRYPTION_KEY,
                OUROBOROS_CREDENTIALS_ENCRYPTION_KEY=raw,
            ),
            tool_client_loop=tier_loop,
        )

    if raw:
        assert raw not in str(refused.value)
    assert security_context.store is sentinel_store
    assert fake_mcp.servers == []


def test_the_agent_tier_composes_the_sql_user_token_store_for_a_streamable_user_token_server(
    tmp_path, monkeypatch, fake_mcp, tier_loop, restore_process_singletons
) -> None:
    from infra.user_tokens.sql import SqlUserTokenStore
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from ouroboros.storage.settings.schema import McpServerEntry, UserTokenAuth

    entry = McpServerEntry(id="shared", url="https://shared.example/mcp", auth=UserTokenAuth(auth_header="X-API-Key"))
    monkeypatch.setattr("security_config.mcp.mcp_servers", [entry])

    agent_runtime_run.compose_process(_boot(tmp_path, MCP_ENABLED=True), tool_client_loop=tier_loop)

    assert [server.id for server in fake_mcp.servers] == ["shared"]
    assert isinstance(fake_mcp.auth_dependencies.user_token_store, SqlUserTokenStore)


def test_agent_configuration_failure_does_not_stop_catalog_composition(
    tmp_path, monkeypatch, fake_mcp, tier_loop, restore_process_singletons, caplog
) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.identity import ServiceJob
    from new_core.tools.catalog import tool_catalog
    from ouroboros.app import agent_system_configuration_kafka as announcement
    from ouroboros.contracts.owner_scope import service_identity

    monkeypatch.setattr("ouroboros.storage.settings.profile.is_server_profile", lambda: True)

    def fail(**kwargs):
        raise RuntimeError("announcement unavailable")

    monkeypatch.setattr(announcement, "publish_agent_system_configuration_event", fail)
    agent_runtime_run.compose_process(_boot(tmp_path, MCP_ENABLED=True), tool_client_loop=tier_loop)

    assert tool_catalog.descriptors(service_identity(job=ServiceJob.STARTUP).as_scope())
    assert "announcement unavailable" in caplog.text


def test_the_agent_tier_offers_the_mcp_tools_the_owner_setting_names(
    tmp_path, fake_mcp, tier_loop, shared_mcp_selection, restore_process_singletons
) -> None:
    """Registered without a manager to read, the provider contributes nothing and says nothing."""
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog

    agent_runtime_run.compose_process(_boot(tmp_path, MCP_ENABLED=True), tool_client_loop=tier_loop)

    assert [entry.id for entry in fake_mcp.servers] == ["shared"]
    assert any(descriptor.origin == "mcp:shared" for descriptor in tool_catalog.descriptors(TEST_SCOPE))


def test_the_agent_tier_connects_while_it_composes_not_on_the_task_path(
    tmp_path, monkeypatch, fake_mcp, tier_loop, shared_mcp_selection, restore_process_singletons
) -> None:
    """Under the mode the catalogue is fixed, so composition is the only refresh there is.

    A provider handed its servers after that point would be a provider nothing refreshes
    again: composition is the one initialization point, and the connect would never happen.
    """
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog

    connects: list[int] = []
    original = fake_mcp.initialize

    async def _count(servers, loop, auth_dependencies) -> None:
        connects.append(len(list(servers)))
        await original(servers, loop, auth_dependencies)

    monkeypatch.setattr(fake_mcp, "initialize", _count)

    agent_runtime_run.compose_process(
        _boot(tmp_path, MCP_ENABLED=True, OUROBOROS_MULTIUSER=True),
        tool_client_loop=tier_loop,
    )

    assert connects == [1], "the tier connects once, while it composes"

    assert connects == [1], "and no task lifecycle reconnects or discovers a missing connection"
    assert any(descriptor.origin == "mcp:shared" for descriptor in tool_catalog.descriptors(TEST_SCOPE))


def test_the_agent_tier_denies_mcp_without_an_owner_setting(
    tmp_path, fake_mcp, tier_loop, restore_process_singletons
) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog

    agent_runtime_run.compose_process(_boot(tmp_path, MCP_ENABLED=True), tool_client_loop=tier_loop)

    assert [entry.id for entry in fake_mcp.servers] == ["shared"]
    assert not [
        descriptor for descriptor in tool_catalog.descriptors(TEST_SCOPE) if descriptor.origin.startswith("mcp:")
    ]


def test_the_kill_switch_off_opens_no_session(tmp_path, fake_mcp, tier_loop, restore_process_singletons) -> None:
    """`refresh()` never reads the switch; a tier that passed the list anyway would connect."""
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog

    agent_runtime_run.compose_process(_boot(tmp_path), tool_client_loop=tier_loop)

    assert fake_mcp.servers == []
    assert not fake_mcp.is_available()
    assert not [
        descriptor for descriptor in tool_catalog.descriptors(TEST_SCOPE) if descriptor.origin.startswith("mcp:")
    ]


def test_a_tool_server_that_cannot_be_reached_does_not_stop_the_tier(
    tmp_path, monkeypatch, fake_mcp, tier_loop, restore_process_singletons
) -> None:
    """The tier serves the tasks that do not use it; the failure is logged and skipped."""
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog

    async def _refuse(servers, loop, auth_dependencies) -> None:
        del servers, loop, auth_dependencies
        raise ConnectionError("no route to the tool server")

    monkeypatch.setattr(fake_mcp, "initialize", _refuse)

    agent_runtime_run.compose_process(_boot(tmp_path, MCP_ENABLED=True), tool_client_loop=tier_loop)

    assert not [
        descriptor for descriptor in tool_catalog.descriptors(TEST_SCOPE) if descriptor.origin.startswith("mcp:")
    ]


def test_the_agent_tier_connects_a2a_while_it_composes(
    tmp_path, fake_a2a, tier_loop, restore_process_singletons
) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog

    agent_runtime_run.compose_process(
        _boot(tmp_path, A2A_EGRESS_ENABLED=True, OUROBOROS_MULTIUSER=True),
        tool_client_loop=tier_loop,
    )

    assert [entry.id for entry in fake_a2a.agents] == ["researcher", "writer"]
    assert fake_a2a.loop is tier_loop
    assert fake_a2a.initializations == 1
    assert [
        descriptor.name for descriptor in tool_catalog.descriptors(TEST_SCOPE) if descriptor.origin.startswith("a2a:")
    ] == [
        "a2a__researcher",
        "a2a__writer",
        "a2a_task_status",
        "a2a_task_cancel",
    ]

    assert fake_a2a.initializations == 1


def test_the_a2a_kill_switch_off_opens_no_connection(tmp_path, fake_a2a, tier_loop, restore_process_singletons) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog

    agent_runtime_run.compose_process(_boot(tmp_path), tool_client_loop=tier_loop)

    assert fake_a2a.initializations == 0
    assert not [
        descriptor for descriptor in tool_catalog.descriptors(TEST_SCOPE) if descriptor.origin.startswith("a2a:")
    ]


def test_the_agent_tier_hands_one_egress_auth_value_and_task_handles_to_the_a2a_manager(
    tmp_path, fake_mcp, fake_a2a, tier_loop, restore_process_singletons
) -> None:
    from pydantic import SecretStr

    from infra.security_context import security_context
    from infra.security_context.sql import SqlSecurityContextStore
    from infra.user_tokens.sql import SqlUserTokenStore
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from ouroboros.egress.a2a.task_handles import A2aTaskHandles
    from tests.helpers.security_context import TEST_CREDENTIALS_KEY

    agent_runtime_run.compose_process(
        _boot(tmp_path, MCP_ENABLED=True, A2A_EGRESS_ENABLED=True, OUROBOROS_MULTIUSER=True),
        tool_client_loop=tier_loop,
    )

    dependencies = fake_a2a.dependencies
    handle = A2aTaskHandles(encryption_key=SecretStr(TEST_CREDENTIALS_KEY)).seal(TEST_SCOPE, "researcher", "task-1")
    assert fake_a2a.initializations == 1
    assert dependencies.auth is fake_mcp.auth_dependencies
    assert isinstance(dependencies.auth.security_context_store, SqlSecurityContextStore)
    assert dependencies.auth.security_context_store is security_context.store
    assert isinstance(dependencies.auth.user_token_store, SqlUserTokenStore)
    assert dependencies.task_handles.open(TEST_SCOPE, "researcher", handle) == "task-1"


@parametrize_from_dict(_UNUSABLE_CREDENTIALS_KEYS)
def test_a_key_the_tier_cannot_seal_with_opens_no_a2a_connection(
    tmp_path, fake_a2a, tier_loop, restore_process_singletons, raw
) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run

    with pytest.raises(ValueError, match="OUROBOROS_CREDENTIALS_ENCRYPTION_KEY") as refused:
        agent_runtime_run.compose_process(
            _boot(
                tmp_path,
                A2A_EGRESS_ENABLED=True,
                OUROBOROS_MULTIUSER=True,
                OUROBOROS_CHAT_ENCRYPTION_KEY=TEST_CHAT_ENCRYPTION_KEY,
                OUROBOROS_CREDENTIALS_ENCRYPTION_KEY=raw,
            ),
            tool_client_loop=tier_loop,
        )

    if raw:
        assert raw not in str(refused.value)
    assert fake_a2a.initializations == 0


def test_an_empty_a2a_list_skips_initialization_and_logs_why(
    tmp_path, monkeypatch, fake_a2a, tier_loop, restore_process_singletons, caplog
) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run

    monkeypatch.setattr("security_config.a2a.a2a_remote_agents", [])

    with caplog.at_level("INFO", logger="new_core.tools.providers.a2a"):
        agent_runtime_run.compose_process(
            _boot(tmp_path, A2A_EGRESS_ENABLED=True, OUROBOROS_MULTIUSER=True),
            tool_client_loop=tier_loop,
        )

    assert fake_a2a.initializations == 0
    assert any("lists no agents" in record.getMessage() for record in caplog.records)


def test_one_unreachable_a2a_agent_does_not_hide_the_others(
    tmp_path, monkeypatch, tier_loop, restore_process_singletons
) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from new_core.tools.catalog import tool_catalog
    from ouroboros.storage.settings.schema import A2aRemoteAgentEntry

    manager = _FakeA2aManager(failed_ids=("dead",))
    monkeypatch.setattr("ouroboros.egress.a2a.manager.a2a_manager", manager)
    monkeypatch.setattr(
        "security_config.a2a.a2a_remote_agents",
        [
            A2aRemoteAgentEntry(id="live", url="https://live.example/a2a"),
            A2aRemoteAgentEntry(id="dead", url="https://dead.example/a2a"),
        ],
    )

    agent_runtime_run.compose_process(
        _boot(tmp_path, A2A_EGRESS_ENABLED=True, OUROBOROS_MULTIUSER=True),
        tool_client_loop=tier_loop,
    )

    assert manager.initializations == 1
    assert tool_catalog.find("a2a__live", TEST_SCOPE) is not None
    assert tool_catalog.find("a2a__dead", TEST_SCOPE) is None


def _main_with_stubs(monkeypatch, tmp_path, runtime) -> list[str]:
    """`main` with everything but the shutdown order stubbed out, recording that order."""
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from multiuser_config.agent_runtime.tool_client_loop import start_tool_client_loop

    seen: list[str] = []
    loop, stop = start_tool_client_loop()

    def _loop_with_record():
        def _stop() -> None:
            seen.append("loop stopped")
            stop()

        return loop, _stop

    monkeypatch.setattr(agent_runtime_run, "bootstrap_process", lambda _component: _boot(tmp_path))
    monkeypatch.setattr(agent_runtime_run, "start_tool_client_loop", _loop_with_record)
    monkeypatch.setattr(agent_runtime_run, "compose_process", lambda _boot, **_kwargs: _empty_skill_runtime())
    monkeypatch.setattr(agent_runtime_run, "install_stop_signal", lambda _stop: None)
    monkeypatch.setattr(agent_runtime_run, "build_dependencies", lambda **_kwargs: _StubDependencies())
    monkeypatch.setattr("ouroboros.agent_runtime.bootstrap.build_agent_runtime", lambda **_kwargs: runtime)

    manager = _FakeMcpManager()

    async def _shutdown() -> None:
        seen.append("clients shut down")

    monkeypatch.setattr(manager, "shutdown", _shutdown)
    monkeypatch.setattr("ouroboros.mcp.manager.mcp_manager", manager)
    runtime.seen = seen
    return seen


class _RecordingRuntime(_StubRuntime):
    """Drains into the same record, so the order between the three steps is visible."""

    seen: list[str] = []
    execute_error: Exception | None = None

    def execute(self) -> None:
        if self.execute_error is not None:
            raise self.execute_error

    def drain(self, *, deadline_sec: float) -> bool:
        self.seen.append("drained")
        return True


def test_the_tier_drains_then_closes_its_clients_then_stops_the_loop(monkeypatch, tmp_path) -> None:
    """A run may still be inside a tool call, and closing a session is work for the loop."""
    from multiuser_config.agent_runtime import run as agent_runtime_run

    runtime = _RecordingRuntime()
    seen = _main_with_stubs(monkeypatch, tmp_path, runtime)

    assert agent_runtime_run.main() == 0
    assert seen == ["drained", "clients shut down", "loop stopped"]


def test_a_failing_runtime_still_closes_its_clients_and_stops_the_loop(monkeypatch, tmp_path) -> None:
    """Otherwise the process that failed is the one that leaves sessions and a thread behind."""
    from multiuser_config.agent_runtime import run as agent_runtime_run

    runtime = _RecordingRuntime()
    runtime.execute_error = RuntimeError("the runtime gave up")
    seen = _main_with_stubs(monkeypatch, tmp_path, runtime)

    with pytest.raises(RuntimeError, match="gave up"):
        agent_runtime_run.main()

    assert seen == ["drained", "clients shut down", "loop stopped"]


def test_a_client_that_will_not_close_does_not_keep_the_loop_running(monkeypatch, tmp_path) -> None:
    """One failure in the exit path must not skip the next step of it."""
    from multiuser_config.agent_runtime import run as agent_runtime_run

    runtime = _RecordingRuntime()
    seen = _main_with_stubs(monkeypatch, tmp_path, runtime)

    async def _refuse() -> None:
        raise RuntimeError("the session will not close")

    monkeypatch.setattr("ouroboros.mcp.manager.mcp_manager.shutdown", _refuse)

    assert agent_runtime_run.main() == 0
    assert seen == ["drained", "loop stopped"]


def test_a_kafka_runtime_exits_through_its_clients_and_probes_without_a_broker_step(monkeypatch, tmp_path) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run
    from tests.helpers.kafka_composition import install_initialized_client_factory, record_client_builds, select_kafka

    select_kafka(monkeypatch)
    seen = _main_with_stubs(monkeypatch, tmp_path, _RecordingRuntime())
    record_client_builds(monkeypatch, install_initialized_client_factory(monkeypatch), seen=seen)
    monkeypatch.setattr(
        agent_runtime_run, "serve_probes", lambda _probes, **_kwargs: lambda: seen.append("probes stopped")
    )
    monkeypatch.setattr(agent_runtime_run, "stop_process_monitoring", lambda: seen.append("monitoring stopped"))

    assert agent_runtime_run.main() == 0
    assert seen == ["drained", "clients shut down", "loop stopped", "probes stopped", "monitoring stopped"]


def test_a_failing_mcp_shutdown_does_not_skip_a2a(monkeypatch, tier_loop) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run

    closed: list[str] = []

    async def _mcp_failure() -> None:
        raise RuntimeError("MCP close failed")

    async def _a2a_shutdown() -> None:
        closed.append("a2a")

    monkeypatch.setattr("ouroboros.mcp.manager.mcp_manager.shutdown", _mcp_failure)
    monkeypatch.setattr("ouroboros.egress.a2a.manager.a2a_manager.shutdown", _a2a_shutdown)

    agent_runtime_run._shutdown_tool_clients(tier_loop, AsyncExitStack())

    assert closed == ["a2a"]


def test_a_failing_a2a_shutdown_does_not_skip_mcp(monkeypatch, tier_loop) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run

    closed: list[str] = []

    async def _mcp_shutdown() -> None:
        closed.append("mcp")

    async def _a2a_failure() -> None:
        raise RuntimeError("A2A close failed")

    monkeypatch.setattr("ouroboros.mcp.manager.mcp_manager.shutdown", _mcp_shutdown)
    monkeypatch.setattr("ouroboros.egress.a2a.manager.a2a_manager.shutdown", _a2a_failure)

    agent_runtime_run._shutdown_tool_clients(tier_loop, AsyncExitStack())

    assert closed == ["mcp"]


def test_a_hung_client_does_not_skip_the_other_or_hold_shutdown(monkeypatch, tier_loop) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run

    closed: list[str] = []

    async def _never_returns() -> None:
        await asyncio.Event().wait()

    async def _a2a_shutdown() -> None:
        closed.append("a2a")

    monkeypatch.setattr(agent_runtime_run, "_SHUTDOWN_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("ouroboros.mcp.manager.mcp_manager.shutdown", _never_returns)
    monkeypatch.setattr("ouroboros.egress.a2a.manager.a2a_manager.shutdown", _a2a_shutdown)

    agent_runtime_run._shutdown_tool_clients(tier_loop, AsyncExitStack())

    assert closed == ["a2a"]


def test_the_tier_disposes_the_async_engine_on_its_loop_even_behind_a_hung_client(monkeypatch, tier_loop) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run

    disposed_on: list[asyncio.AbstractEventLoop] = []

    async def _never_returns() -> None:
        await asyncio.Event().wait()

    async def _a2a_shutdown() -> None:
        pass

    async def _dispose() -> None:
        disposed_on.append(asyncio.get_running_loop())

    monkeypatch.setattr(agent_runtime_run, "_SHUTDOWN_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("ouroboros.mcp.manager.mcp_manager.shutdown", _never_returns)
    monkeypatch.setattr("ouroboros.egress.a2a.manager.a2a_manager.shutdown", _a2a_shutdown)
    monkeypatch.setattr("infra.async_engine.async_engine_factory.dispose", _dispose)

    agent_runtime_run._shutdown_tool_clients(tier_loop, AsyncExitStack())

    assert disposed_on == [tier_loop]
