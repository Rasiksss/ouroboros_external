"""Which broker backend a composition root builds, and what it refuses to build.

The selector is the whole of the decision: a deployment that names a broker and cannot
reach one must stop, because the fallback — an in-process queue per instance — is
indistinguishable from success at startup and shows up later as submissions no supervisor
drains.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from ouroboros.agent_events.redis_transport import RedisAgentEventPublisher
from ouroboros.app.broker_composition import build_message_gateway, resolve_broker_backend, stream_key
from tests.helpers.memory_event_conformance import reset_memory_event_publisher
from tests.helpers.parametrize import parametrize_from_dict
from transport.infra.inmemory import InProcessMessageGateway
from transport.infra.redis_gateway import RedisMessageGateway

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

_SELECTOR_RESOLVERS: dict[str, dict[str, str]] = {
    "resolve_broker_backend": {},
    "initialize_broker_backend": {},
    "build_message_gateway": {"inbox_consumer": "t"},
    "resolve_agent_event_publisher": {},
    "resolve_agent_event_consumer": {},
    "install_memory_event_publisher": {},
    "resolve_memory_event_consumer": {},
}

unhandled_value_cases = {
    f"{name}-multiuser-{str(multiuser).lower()}": {"resolver": name, "kwargs": kwargs, "multiuser": multiuser}
    for name, kwargs in _SELECTOR_RESOLVERS.items()
    for multiuser in (False, True)
}

seam_without_an_arm_cases = {
    name: {"resolver": name, "kwargs": kwargs}
    for name, kwargs in _SELECTOR_RESOLVERS.items()
    if name != "resolve_broker_backend"
}


@pytest.fixture
def redis_selected(monkeypatch):
    """The selector plus a reachable-looking URL; nothing connects until a command runs."""
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "redis://localhost:6379/0")


def test_the_named_backend_is_the_one_composed(redis_selected) -> None:
    """`redis.Redis.from_url` connects lazily, so this needs no server."""
    gateway = build_message_gateway(inbox_consumer="t")

    assert isinstance(gateway, RedisMessageGateway)


def test_a_named_backend_that_cannot_be_built_refuses_naming_both_settings(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "")

    with pytest.raises(RuntimeError) as refusal:
        build_message_gateway(inbox_consumer="t")

    message = str(refusal.value)
    assert "OUROBOROS_BROKER_BACKEND" in message
    assert "OUROBOROS_REDIS_URL" in message


def test_a_named_backend_never_falls_back_to_the_in_process_one(monkeypatch) -> None:
    """A fallback is a deployment of N instances each holding its own queue."""
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "   ")

    with pytest.raises(RuntimeError):
        build_message_gateway(inbox_consumer="t")


def test_an_unconfigured_deployment_keeps_the_in_process_backend() -> None:
    assert resolve_broker_backend() == "inprocess"
    assert isinstance(build_message_gateway(inbox_consumer="t"), InProcessMessageGateway)


def test_an_unknown_backend_name_is_refused_at_settings_load(monkeypatch) -> None:
    import pydantic

    from ouroboros.storage.settings.schema import AppSettings

    with pytest.raises(pydantic.ValidationError):
        AppSettings(OUROBOROS_BROKER_BACKEND="rabbitmq")


def test_the_mode_accepts_the_kafka_broker(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "kafka")

    assert resolve_broker_backend() == "kafka"


def test_the_composed_client_states_its_timeouts_and_its_retry_policy(redis_selected) -> None:
    """Not inherited: redis-py's retry default has moved between minor versions.

    An append may not be retried — a repeated `XADD` whose reply was lost duplicates a user's
    message — while a read must be, or one dropped connection stops a transport consuming for
    the life of the process (`transport/reader_loop.py`).
    """
    from ouroboros.app.broker_composition import redis_client

    for retries in (0, 3):
        composed = redis_client(retries=retries).connection_pool.connection_kwargs

        assert composed["retry"]._retries == retries
        assert composed["socket_connect_timeout"] and composed["socket_timeout"]
        assert composed["health_check_interval"], "a connection the server reaped must not surface as an error"


def test_a_sentinel_url_composes_a_failover_managed_client(monkeypatch, redis_selected) -> None:
    """`Sentinel.master_for` connects lazily, so this needs no sentinels standing up.

    redis-py's `Redis.from_url` refuses sentinel schemes outright (`ValueError`), so this
    is the path that makes `OUROBOROS_REDIS_URL` address a Sentinel deployment at all.
    """
    monkeypatch.setenv(
        "OUROBOROS_REDIS_URL",
        "sentinel://user:pw@sentinel-a:26379,sentinel-b:26379?service_name=mymaster&db=2",
    )
    from ouroboros.app.broker_composition import redis_client

    composed = redis_client(retries=3)
    pool = composed.connection_pool

    assert type(pool).__name__ == "SentinelConnectionPool"
    assert pool.service_name == "mymaster"
    kwargs = pool.connection_kwargs
    assert kwargs["retry"]._retries == 3
    assert kwargs["db"] == 2
    assert kwargs["decode_responses"] is True
    assert kwargs["socket_connect_timeout"] and kwargs["socket_timeout"]
    assert kwargs["health_check_interval"]


def test_the_sentinel_pool_carries_the_requested_read_retry(monkeypatch, redis_selected) -> None:
    monkeypatch.setenv(
        "OUROBOROS_REDIS_URL",
        "sentinel://sentinel-a:26379?service_name=mymaster",
    )
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    from ouroboros.app.broker_composition import redis_client

    pool = redis_client(retries=0).connection_pool

    assert pool.connection_kwargs["retry"] == Retry(NoBackoff(), 0)


def test_a_tls_sentinel_url_uses_the_ssl_connection_class(monkeypatch, redis_selected) -> None:
    from redis.sentinel import SentinelManagedSSLConnection

    from ouroboros.app.broker_composition import redis_client

    monkeypatch.setenv(
        "OUROBOROS_REDIS_URL",
        "rediss-sentinel://sentinel-a:26379?service_name=mymaster",
    )

    assert redis_client(retries=1).connection_pool.connection_class is SentinelManagedSSLConnection


def test_the_sentinel_url_parser_drops_userinfo_before_the_addresses() -> None:
    from infra.redis_client import parse_sentinel_url

    parsed = parse_sentinel_url(
        "sentinel://user:pw@sentinel-a:26379,sentinel-b:26379?service_name=m&db=2&socket_timeout=7"
    )

    assert parsed["sentinels"] == [("sentinel-a", 26379), ("sentinel-b", 26379)]
    assert parsed["service_name"] == "m"
    assert parsed["db"] == 2
    assert parsed["username"] == "user"
    assert parsed["ssl"] is False
    assert parsed["kwargs"] == {"socket_timeout": "7"}


def test_a_sentinel_url_needs_a_service_name(monkeypatch) -> None:
    from ouroboros.app.broker_composition import redis_client

    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "sentinel://sentinel-a:26379")

    with pytest.raises(RuntimeError, match="service_name"):
        redis_client(retries=1)


def test_a_sentinel_address_needs_a_port(monkeypatch) -> None:
    from ouroboros.app.broker_composition import redis_client

    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "sentinel://sentinel-a?service_name=m")

    with pytest.raises(RuntimeError, match="host:port"):
        redis_client(retries=1)


def test_the_gateway_reads_and_appends_on_different_clients(redis_selected) -> None:
    """redis-py's retry is per connection, so the two policies need two clients."""
    gateway = build_message_gateway(inbox_consumer="t")

    assert gateway.inbox._client is not gateway.inbox._append_client
    assert gateway.outbox._client is gateway.inbox._client, "one read pool for the whole gateway"
    assert gateway.outbox._append_client is gateway.inbox._append_client


def test_the_inbox_consumer_is_required(redis_selected) -> None:
    """Two instances sharing a consumer name read each other's in-flight entry."""
    with pytest.raises(ValueError, match="consumer"):
        build_message_gateway(inbox_consumer="")


def test_stream_names_are_formed_in_one_place(monkeypatch) -> None:
    assert stream_key("inbound") == "ouroboros:inbound"

    monkeypatch.setenv("OUROBOROS_REDIS_PREFIX", "fleet-a")
    assert stream_key("inbound") == "fleet-a:inbound"
    assert stream_key("outbound") == "fleet-a:outbound"


def test_the_gateway_forms_its_streams_from_the_suffix_constants(redis_selected, monkeypatch) -> None:
    from ouroboros.app import broker_composition

    assert broker_composition.INBOUND_STREAM == "inbound"
    assert broker_composition.OUTBOUND_STREAM == "outbound"

    monkeypatch.setattr(broker_composition, "INBOUND_STREAM", "in-elsewhere")
    monkeypatch.setattr(broker_composition, "OUTBOUND_STREAM", "out-elsewhere")

    gateway = broker_composition.build_message_gateway(inbox_consumer="t")

    assert gateway.inbox._stream == "ouroboros:in-elsewhere"
    assert gateway.outbox._stream == "ouroboros:out-elsewhere"


def test_the_transport_root_composes_the_backend_the_configuration_names(monkeypatch) -> None:
    """The behaviour `build_transport_app` had before the rewiring must survive it."""
    from multiuser_config.transport.browser.app import build_transport_app
    from tests.helpers.agent_registry import FakeAgentRegistry
    from tests.helpers.security_context import TEST_CREDENTIALS_KEY
    from transport.browser.identity import ConfiguredOwnerIdentity, browser_identity
    from transport.browser.ws import reset_browser_ws_state_for_tests

    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", TEST_CREDENTIALS_KEY)
    try:
        app = build_transport_app()
        assert isinstance(app.state.message_gateway, InProcessMessageGateway)
    finally:
        reset_browser_ws_state_for_tests()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def test_an_explicit_gateway_still_takes_precedence(monkeypatch) -> None:
    """Every test that injects a fake must be untouched by the selector."""
    from multiuser_config.transport.browser.app import build_transport_app
    from tests.helpers.agent_registry import FakeAgentRegistry
    from tests.helpers.fake_gateway import FakeGateway
    from tests.helpers.security_context import TEST_CREDENTIALS_KEY
    from transport.browser.identity import ConfiguredOwnerIdentity, browser_identity
    from transport.browser.ws import reset_browser_ws_state_for_tests

    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", TEST_CREDENTIALS_KEY)
    injected = FakeGateway()
    try:
        app = build_transport_app(gateway=injected)
        assert app.state.message_gateway is injected
    finally:
        reset_browser_ws_state_for_tests()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


@pytest.fixture
def clean_memory_holder(monkeypatch):
    """The publisher holder and its queue are process-global; a case that installs one puts both back."""
    from background_memory.infra.events import memory_event_publisher_factory
    from ouroboros.app import broker_composition

    monkeypatch.setattr(broker_composition, "_INPROCESS_MEMORY_QUEUE", None)
    reset_memory_event_publisher()
    yield memory_event_publisher_factory
    reset_memory_event_publisher()


def test_the_memory_seam_follows_the_same_selector(redis_selected, clean_memory_holder) -> None:
    """One selector for every seam: a deployment cannot run memory events in process."""
    from background_memory.infra.events.redis_transport import RedisMemoryEventConsumer, RedisMemoryEventPublisher
    from ouroboros.app.broker_composition import install_memory_event_publisher, resolve_memory_event_consumer

    installed = install_memory_event_publisher()

    assert isinstance(installed, RedisMemoryEventPublisher)
    assert clean_memory_holder.memory_publisher is installed, "the producers read the holder, not a return value"
    assert isinstance(resolve_memory_event_consumer(), RedisMemoryEventConsumer)


def test_the_consuming_half_of_the_agent_event_seam_follows_the_same_selector(redis_selected) -> None:
    """The half the supervisor tier reads reports through; the constructor issues no I/O.

    Its two siblings — the gateway above and the memory seam beside it — each had this case
    and this one did not, which left "return the in-process consumer whatever the selector
    says" passing the whole gate: the tier would then drain a queue in its own process that
    no runtime publishes to, and answer its readiness probe while every owner waited.
    """
    from ouroboros.agent_events.redis_transport import RedisAgentEventConsumer
    from ouroboros.app.broker_composition import resolve_agent_event_consumer, resolve_agent_event_publisher

    assert isinstance(resolve_agent_event_consumer(), RedisAgentEventConsumer)
    assert isinstance(resolve_agent_event_publisher(), RedisAgentEventPublisher)


def test_the_agent_event_consumer_takes_no_command_retry(redis_selected) -> None:
    """Its read is `XREADGROUP >` with NOACK, which a retry cannot be safe over.

    redis-py retries whole commands. A retry after a lost reply returns the *next* entries,
    and under NOACK the first batch is in no pending list — no XPENDING and no XAUTOCLAIM
    reaches it. A non-droppable `OutboundText` is the message the owner is waiting for, so
    the recovery has to be `poll`'s outage handling rather than the client's retry.
    """
    from ouroboros.app.broker_composition import resolve_agent_event_consumer

    consumer = resolve_agent_event_consumer()

    assert consumer._client.get_retry()._retries == 0


def test_the_task_stop_hint_publisher_has_a_short_failure_budget(redis_selected) -> None:
    from ouroboros.app.broker_composition import TASK_STOP_PUBLISH_TIMEOUT_SEC, resolve_task_stop_publisher

    publisher = resolve_task_stop_publisher()
    composed = publisher._client.connection_pool.connection_kwargs

    assert composed["retry"]._retries == 0
    assert composed["socket_connect_timeout"] == TASK_STOP_PUBLISH_TIMEOUT_SEC
    assert composed["socket_timeout"] == TASK_STOP_PUBLISH_TIMEOUT_SEC


def test_the_mode_refuses_a_component_tier_holding_a_private_queue(monkeypatch) -> None:
    """Every tier reaches the selector while composing, so this is where the refusal lands.

    An in-process queue in a component tier puts its writer and its reader in different
    processes: every message is accepted, none is ever processed, and health answers 200
    throughout — the one misconfiguration of this mode that reports nothing wrong.
    """
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "inprocess")

    with pytest.raises(RuntimeError, match="component tier"):
        resolve_broker_backend()


def test_the_single_process_runtime_still_composes_the_in_process_backend(monkeypatch) -> None:
    """`server.py` names it directly and never reads the selector, so the mode is fine there."""
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "redis://localhost:6379/0")

    assert resolve_broker_backend() == "redis"


@pytest.fixture
def clean_agent_event_pair(monkeypatch):
    """A pair of this case's own, and the process's own pair put back afterwards.

    The holder has no `reset_for_tests` by design — one process holds one pair, and a reset
    hook would be a test-only seam on a composition module — so the case reaches the same
    isolation the memory holder's fixture gives, through `monkeypatch`, which restores the
    module attribute when it ends. Without it this case both consumes whatever a previous
    one published and leaves its own publication behind.
    """
    from ouroboros.app import broker_composition

    monkeypatch.setattr(broker_composition, "_INPROCESS_AGENT_EVENTS", None)


def test_the_default_agent_event_transport_is_one_connected_pair(clean_agent_event_pair) -> None:
    """Two unrelated queues in one process is the failure the shared build exists to prevent."""
    from ouroboros.agent_events.interfaces import OutboundText
    from ouroboros.app.broker_composition import resolve_agent_event_consumer, resolve_agent_event_publisher
    from tests.helpers.shared import TEST_SCOPE

    publisher = resolve_agent_event_publisher()
    consumer = resolve_agent_event_consumer()

    publisher.publish(OutboundText(scope=TEST_SCOPE, text="one process, one pair"))
    polled = consumer.poll(0.5)
    assert polled is not None and polled.text == "one process, one pair"


def test_the_default_memory_transport_is_one_connected_pair(clean_memory_holder) -> None:
    """Two unrelated queues in one process is the failure the shared build exists to prevent."""
    from background_memory.infra.events.inmemory import InMemoryEventPublisher
    from background_memory.interfaces import ChatMessagePersistedEvent
    from ouroboros.app.broker_composition import install_memory_event_publisher, resolve_memory_event_consumer
    from tests.helpers.memory_event_conformance import poll
    from tests.helpers.shared import TEST_SCOPE

    publisher = install_memory_event_publisher()
    consumer = resolve_memory_event_consumer()

    assert isinstance(publisher, InMemoryEventPublisher)
    publisher.publish(ChatMessagePersistedEvent(scope=TEST_SCOPE, message_id="message-0001"))
    polled = poll(consumer, 0.5)
    assert isinstance(polled, ChatMessagePersistedEvent) and polled.message_id == "message-0001"


def _run_the_decision(resolver: str, kwargs: dict[str, str]) -> None:
    from ouroboros.app import broker_composition

    getattr(broker_composition, resolver)(**kwargs)


def _assert_refused_without_composing_in_process(message: str, memory_holder) -> None:
    from background_memory.interfaces import NullMemoryEventPublisher
    from ouroboros.app import broker_composition

    assert "OUROBOROS_BROKER_BACKEND" in message and "'carrier-pigeon'" in message
    assert "component tier" not in message
    assert broker_composition._INPROCESS_AGENT_EVENTS is None
    assert broker_composition._INPROCESS_MEMORY_QUEUE is None
    assert isinstance(memory_holder.memory_publisher, NullMemoryEventPublisher)


@parametrize_from_dict(unhandled_value_cases)
def test_a_value_no_arm_handles_is_refused_by_every_resolver(
    resolver, kwargs, multiuser, monkeypatch, clean_memory_holder, clean_agent_event_pair
) -> None:
    from ouroboros.app import broker_composition
    from ouroboros.app.broker_composition import UnhandledBrokerBackend
    from ouroboros.storage.settings.schema import AppSettings

    settings = AppSettings.model_construct(OUROBOROS_BROKER_BACKEND="carrier-pigeon", OUROBOROS_MULTIUSER=multiuser)
    monkeypatch.setattr(broker_composition, "load_settings", lambda: settings)

    with pytest.raises(UnhandledBrokerBackend) as refusal:
        _run_the_decision(resolver, kwargs)

    _assert_refused_without_composing_in_process(str(refusal.value), clean_memory_holder)


@parametrize_from_dict(seam_without_an_arm_cases)
def test_a_resolver_without_an_arm_for_the_value_refuses_on_its_own(
    resolver, kwargs, monkeypatch, clean_memory_holder, clean_agent_event_pair
) -> None:
    from ouroboros.app import broker_composition
    from ouroboros.app.broker_composition import UnhandledBrokerBackend

    monkeypatch.setattr(broker_composition, "resolve_broker_backend", lambda: "carrier-pigeon")

    with pytest.raises(UnhandledBrokerBackend) as refusal:
        _run_the_decision(resolver, kwargs)

    _assert_refused_without_composing_in_process(str(refusal.value), clean_memory_holder)


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_resolver_is_reached_by_composition_roots_alone() -> None:
    """A transport that could reach the resolver could reach a backend."""
    resolver = "ouroboros.app.broker_composition"

    for package in ("transport", "background_memory"):
        for path in (REPO_ROOT / package).rglob("*.py"):
            for module in _imported_modules(path):
                assert module != resolver and not module.startswith(f"{resolver}."), f"{path} imports {module}"
