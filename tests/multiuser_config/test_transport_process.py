"""The browser transport as its own process: what it composes, and what it refuses.

`server.py` is the full runtime — transport, supervisor and agent in one. This suite is
about the second composition root: that it is a transport tier and nothing else, that the
obligations a second root can forget are refusals rather than conventions, and that the
surface it serves is the same one the full runtime mounts.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import socket

import pytest
from starlette.testclient import TestClient

from multiuser_config.transport.browser.app import build_transport_app
from ouroboros.contracts.owner_scope import ServiceJob, service_identity
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.interfaces.owner_state import OwnerStateDocument
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.database import refused_runtime_database
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import TEST_CHAT_ENCRYPTION_KEY
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from transport.browser.identity import ConfiguredOwnerIdentity, browser_identity

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROOT_PACKAGE = REPO_ROOT / "multiuser_config" / "transport" / "browser"


@pytest.fixture(autouse=True)
def _credentials_encryption_key(monkeypatch):
    from tests.helpers.security_context import TEST_CREDENTIALS_KEY

    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", TEST_CREDENTIALS_KEY)
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", "")


@pytest.fixture
def transport_app():
    """The browser transport tier, composed the way its own entrypoint composes it.

    The data dir is already pointed at ``tmp_path`` by the autouse config fixture, so this
    only composes and then puts the process globals back.
    """
    from transport.browser.ws import reset_browser_ws_state_for_tests

    app = build_transport_app()
    yield app
    reset_browser_ws_state_for_tests()
    browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_transport_root_names_neither_the_supervisor_nor_the_agent_runtime() -> None:
    """A tier that starts no supervisor must not be able to reach for one either."""
    forbidden = ("supervisor", "ouroboros.agent_runtime", "ouroboros.agent")

    for path in ROOT_PACKAGE.rglob("*.py"):
        for module in _imported_modules(path):
            assert not any(module == name or module.startswith(f"{name}.") for name in forbidden), (
                f"{path.name} imports {module}"
            )


def test_the_transport_root_starts_no_supervisor_and_no_agent_runtime(transport_app) -> None:
    import threading

    with TestClient(transport_app):
        names = {thread.name for thread in threading.enumerate()}

    assert not any(name.startswith("supervisor") for name in names), names
    assert not any("agent" in name for name in names), names


def test_transport_does_not_announce_a_catalog_it_does_not_compose(monkeypatch) -> None:
    from ouroboros.app import agent_system_configuration_kafka as announcement
    from transport.browser.ws import reset_browser_ws_state_for_tests

    monkeypatch.setattr("ouroboros.storage.settings.profile.is_server_profile", lambda: True)
    calls = []
    monkeypatch.setattr(announcement, "publish_agent_system_configuration_event", lambda **kwargs: calls.append(kwargs))
    try:
        build_transport_app()
        assert calls == []
    finally:
        reset_browser_ws_state_for_tests()


def test_the_identity_holder_refuses_before_a_root_installs_one() -> None:
    """A default is how a root that forgot to install a resolver keeps working — wrongly."""
    browser_identity.reset_for_tests()
    try:
        with pytest.raises(RuntimeError, match="not installed"):
            assert browser_identity.resolver
    finally:
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def test_the_transport_root_installs_a_resolver() -> None:
    """From uninstalled, since every test's autouse fixture installs one for it."""
    from transport.browser.ws import reset_browser_ws_state_for_tests

    browser_identity.reset_for_tests()
    try:
        build_transport_app()
        assert browser_identity.resolver is not None
    finally:
        reset_browser_ws_state_for_tests()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def test_the_transport_root_installs_the_sql_security_context_store() -> None:
    from pydantic import SecretStr

    from infra.async_engine import create_async_runtime_engine
    from infra.security_context import security_context, security_context_collector
    from infra.security_context.collector import DeclaredParametersCollector
    from infra.security_context.interface import SecurityContext, SecurityParameterKey, SecurityParameterType
    from infra.security_context.sql import SqlSecurityContextStore
    from ouroboros.contracts.owner_scope import SINGLE_AGENT_ID, configured_owner_id
    from ouroboros.storage.db_url import canonicalize_runtime_db_urls, collect_runtime_db_urls
    from ouroboros.storage.settings.store import load_settings
    from tests.helpers.security_context import (
        TEST_CREDENTIALS_KEY,
        install_fresh_security_context_collector,
        install_fresh_security_context_store,
        parameters_of,
    )
    from tests.helpers.shared import TEST_SCOPE, scope_with
    from transport.browser.ws import reset_browser_ws_state_for_tests

    sentinel_store = install_fresh_security_context_store()
    sentinel_collector = install_fresh_security_context_collector()
    try:
        app = build_transport_app()
        assert security_context.store is not sentinel_store
        assert isinstance(security_context.store, SqlSecurityContextStore)
        collector = security_context_collector.collector
        assert collector is not sentinel_collector
        assert isinstance(collector, DeclaredParametersCollector)
        assert collector.collect([("X-Sudir-Token", "t1"), ("Cookie", "sudir=s; sudir_session=s1")]) == parameters_of(
            headers={"X-Sudir-Token": "t1"}, cookies={"sudir_session": "s1"}, ttl_sec=3600
        )
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))
        with TestClient(app) as client, client.websocket_connect("/ws", headers={"X-Sudir-Token": "t1-secret"}):
            pass
        token = scope_with(TEST_SCOPE, owner_id=configured_owner_id(), agent_id=SINGLE_AGENT_ID).security_token
        keys = {SecurityParameterKey(SecurityParameterType.HEADER, "X-Sudir-Token")}
        urls = canonicalize_runtime_db_urls(collect_runtime_db_urls(load_settings()))
        engine = create_async_runtime_engine(urls)
        loop = asyncio.new_event_loop()
        try:
            reader = SqlSecurityContextStore(engine, encryption_key=SecretStr(TEST_CREDENTIALS_KEY))
            assert loop.run_until_complete(reader.load(token, keys)) == SecurityContext(
                dict.fromkeys(keys, "t1-secret")
            )
        finally:
            loop.run_until_complete(engine.dispose())
            loop.close()
    finally:
        reset_browser_ws_state_for_tests()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


@parametrize_from_dict(
    {
        "empty key": {"raw": ""},
        "short key": {"raw": "short-s3cret-key"},
        "a chat encryption key": {"raw": TEST_CHAT_ENCRYPTION_KEY},
    }
)
def test_the_transport_root_refuses_to_compose_without_a_valid_credentials_key(monkeypatch, raw) -> None:
    from infra.security_context import security_context
    from tests.helpers.security_context import install_fresh_security_context_store
    from transport.browser.ws import reset_browser_ws_state_for_tests

    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", TEST_CHAT_ENCRYPTION_KEY)
    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", raw)
    sentinel_store = install_fresh_security_context_store()
    try:
        with pytest.raises(ValueError, match="OUROBOROS_CREDENTIALS_ENCRYPTION_KEY") as refused:
            build_transport_app()
    finally:
        reset_browser_ws_state_for_tests()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))

    if raw:
        assert raw not in str(refused.value)
    assert security_context.store is sentinel_store


def test_readiness_answers_about_this_process_even_with_model_credentials(monkeypatch) -> None:
    """With credentials the probe used to demand a bridge only a local supervisor sets.

    So a transport image built with them answered 503 forever and the load balancer
    admitted nobody, while one built without them answered 200 with the supervisor fleet
    possibly down. The gate reported on the environment, not on either process.
    """
    from ouroboros.app.server_runtime import has_supervisor_provider
    from ouroboros.storage.settings.store import load_settings
    from tests.conftest import refresh_test_config
    from transport.browser.ws import reset_browser_ws_state_for_tests

    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "https://models.example/v1")
    refresh_test_config()
    # The predicate the old probe gated on, so this really is the case that answered 503.
    assert has_supervisor_provider(load_settings())

    app = build_transport_app()
    try:
        with TestClient(app) as client:
            assert client.get("/ready").status_code == 200
            assert client.get("/start").status_code == 200
            assert client.get("/live").status_code == 200
    finally:
        reset_browser_ws_state_for_tests()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def test_readiness_fails_on_an_unreachable_database(transport_app) -> None:
    with TestClient(transport_app) as client, refused_runtime_database():
        assert client.get("/ready").status_code == 503


def test_the_tier_reports_no_supervisor_rather_than_claiming_the_one_it_cannot_see(transport_app) -> None:
    """The probes answer for this process; `supervisor_ready` answers about a tier it has no signal from."""
    with TestClient(transport_app) as client:
        assert client.get("/ready").status_code == 200
        payload = client.get("/api/state").json()

    assert payload["supervisor_ready"] is False
    assert payload["supervisor_error"] is None


@parametrize_from_dict({"disabled": {"enabled": False}, "enabled": {"enabled": True}})
def test_http_stop_uses_the_composed_broker_publisher(monkeypatch, request, enabled):
    from new_core.coordination.interfaces import TaskStopNotice
    from ouroboros.app.broker_composition import resolve_task_stop_consumer
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from tests.helpers.shared import TEST_SCOPE

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", str(enabled).lower())
    app = request.getfixturevalue("transport_app")
    consumer = resolve_task_stop_consumer()
    while consumer.poll(0.0) is not None:
        pass
    store = runtime_persistence.current
    store.tasks_repo.create_task(TaskRequest(id="http-stop", text="work", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.claim_next_task(runtime_id="runtime-a")

    with TestClient(app) as client:
        assert client.get("/api/jobs").json()["detached_enabled"] is enabled
        response = client.post("/api/jobs/http-stop/stop")

    assert response.status_code == 200
    assert consumer.poll(0.0) == TaskStopNotice(runtime_id="runtime-a", task_ids=("http-stop",))


def test_the_transport_app_publishes_only_what_a_mounted_route_reads(transport_app) -> None:
    """The six keys of the self-restart model have no reader, and re-execing is the
    orchestrator's job in a scaled deployment — a fight with it if this process claims it."""
    #: Everything a mounted route reads off the app state.
    read_by_routes = {
        "drive_root",
        "repo_dir",
        "app_start",
        "message_gateway",
        "inbound_messages",
        "scheduler",
        "chat_session_id",
        "owner_history_repository",
        "detached_enabled",
        "task_stop_notifier",
        "health_probes",
        "runtime_status",
        "supervisor_ready_event",
        "get_supervisor_error",
    }
    # The keys a route reads and this root does not publish, the route falling back on its own:
    # the figures and the supervisor's state belong to the supervisor tier.
    expected = read_by_routes - {"runtime_status", "supervisor_ready_event", "get_supervisor_error"}
    # Starlette keeps the state in a private mapping, so `vars()` sees only that mapping.
    published = set(transport_app.state._state)

    assert published == expected
    for absent in ("request_restart", "safe_restart", "bind_host", "port_file", "default_port"):
        assert absent not in published


def test_the_transport_route_set_is_the_browser_profile_route_set(transport_app) -> None:
    from ouroboros.gateway.router import collect_routes

    expected = {
        (route.path, tuple(sorted(getattr(route, "methods", None) or ())))
        for route in collect_routes(data_dir=REPO_ROOT, repo_dir=REPO_ROOT, browser_enabled=True)
    }
    served = {
        (route.path, tuple(sorted(getattr(route, "methods", None) or ()))) for route in transport_app.router.routes
    }

    assert served == expected


def test_the_conversation_id_comes_from_storage_not_from_the_supervisor(request) -> None:
    """The transport root publishes the stored session for its mounted routes."""
    store = runtime_persistence.initialize()
    scope = service_identity(job=ServiceJob.JOURNAL_SWEEP).as_scope()
    sql_owner_state_repository(store.engine).save(OwnerStateDocument({"session_id": "sess-from-storage"}), scope)

    app = request.getfixturevalue("transport_app")
    assert app.state.chat_session_id == "sess-from-storage"


def test_the_entrypoint_fails_when_its_port_is_taken(monkeypatch, tmp_path, caplog) -> None:
    """Bind or exit: a drift onto a neighbour's port is lethal when instances share a namespace.

    Really bound, not a stubbed `uvicorn.run`: uvicorn logs a bind failure and calls
    `sys.exit(1)` itself, so a test that patched it would pin a path production never takes.
    """
    import contextlib
    import logging

    from starlette.applications import Starlette

    from multiuser_config.transport.browser import __main__ as entrypoint
    from tests.conftest import refresh_test_config

    taken = socket.socket()
    taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    lifespan_ran: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        lifespan_ran.append("started")
        try:
            yield
        finally:
            lifespan_ran.append("stopped")

    monkeypatch.setattr(
        "multiuser_config.transport.browser.app.build_transport_app",
        lambda **_kwargs: Starlette(lifespan=lifespan),
    )
    monkeypatch.setenv("OUROBOROS_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("OUROBOROS_SERVER_PORT", str(port))
    refresh_test_config()

    try:
        with caplog.at_level(logging.ERROR, logger="multiuser_config.bootstrap"):
            assert entrypoint.main() != 0
    finally:
        taken.close()
        for name in ("OUROBOROS_SERVER_HOST", "OUROBOROS_SERVER_PORT"):
            monkeypatch.delenv(name, raising=False)
        refresh_test_config()

    assert f"127.0.0.1:{port}" in caplog.text, "the refusal must name the address it could not take"
    assert list(tmp_path.rglob("server_port")) == [], "a transport process shares no port file"
    # uvicorn starts the lifespan before it binds, so a taken port costs this tier its start-up.
    assert lifespan_ran == ["started", "stopped"], "a refused tier left its start-up half-done"


def test_the_entrypoint_uses_no_port_sweep_or_port_file() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in ROOT_PACKAGE.rglob("*.py"))

    for forbidden in ("find_free_port", "write_port_file", "kill_process_on_port"):
        assert forbidden not in source


def _recorded_kafka_transport_exit(monkeypatch) -> list[str]:
    from infra.async_engine import async_engine_factory
    from infra.kafka_client import KafkaClientFactory, KafkaConnection
    from multiuser_config.transport.browser import app as transport_root
    from ouroboros.storage.settings.store import load_settings
    from tests.helpers.kafka_composition import install_client_factory, record_client_builds, select_kafka
    from transport.browser.ws import get_browser_ws_hub

    seen: list[str] = []
    select_kafka(monkeypatch)
    factory = install_client_factory(monkeypatch, KafkaClientFactory())
    record_client_builds(monkeypatch, factory, seen=seen)

    hub = get_browser_ws_hub()
    stop_hub = hub.stop
    dispose_engine = async_engine_factory.dispose

    async def _stop_hub() -> None:
        seen.append("hub stopped")
        await stop_hub()

    async def _dispose_engine() -> None:
        seen.append("engine disposed")
        await dispose_engine()

    monkeypatch.setattr(
        transport_root,
        "initialize_broker_backend",
        lambda: factory.initialize(KafkaConnection.from_settings(load_settings())),
    )
    monkeypatch.setattr(hub, "stop", _stop_hub)
    monkeypatch.setattr(async_engine_factory, "dispose", _dispose_engine)
    monkeypatch.setattr(transport_root, "stop_process_monitoring", lambda: seen.append("monitoring stopped"))
    return seen


def _serve_and_stop_the_transport() -> None:
    from tests.helpers.fake_gateway import FakeGateway
    from transport.browser.ws import reset_browser_ws_state_for_tests

    try:
        with TestClient(build_transport_app(gateway=FakeGateway())) as client:
            assert client.get("/ready").status_code == 200
    finally:
        reset_browser_ws_state_for_tests()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def test_a_kafka_transport_stops_the_hub_disposes_the_engine_and_takes_no_broker_step(monkeypatch) -> None:
    seen = _recorded_kafka_transport_exit(monkeypatch)

    _serve_and_stop_the_transport()

    assert seen == ["hub stopped", "engine disposed", "monitoring stopped"]
