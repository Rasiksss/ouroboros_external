from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable
from typing import Any

import pytest

from background_memory.infra.events.kafka_transport import (
    KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
    KafkaMemoryEventConsumer,
    KafkaMemoryEventPublisher,
)
from infra.kafka_client import KAFKA_MAX_POLL_INTERVAL_CEILING_MS, KafkaClientFactory
from ouroboros.agent_events.kafka_transport import (
    KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS,
    KafkaAgentEventConsumer,
    KafkaAgentEventPublisher,
)
from ouroboros.app import broker_composition
from ouroboros.app.task_stop_kafka import KafkaTaskStopConsumer, KafkaTaskStopPublisher
from tests.helpers.kafka_composition import (
    KAFKA_TEST_MAX_MESSAGE_BYTES,
    KAFKA_TEST_TOPIC_PREFIX,
    install_client_factory,
    install_initialized_client_factory,
    kafka_imports,
    record_client_builds,
    select_kafka,
)
from tests.helpers.memory_event_conformance import reset_memory_event_publisher
from tests.helpers.parametrize import parametrize_from_dict
from transport.infra.kafka_gateway import KAFKA_INBOX_MAX_POLL_INTERVAL_MS, KafkaMessageGateway

COMPOSITION_MODULE = pathlib.Path(broker_composition.__file__)

_SEAM_RESOLVERS: dict[str, Callable[[], Any]] = {
    "build_message_gateway": lambda: broker_composition.build_message_gateway(inbox_consumer="supervisor-t"),
    "resolve_agent_event_publisher": broker_composition.resolve_agent_event_publisher,
    "resolve_agent_event_consumer": broker_composition.resolve_agent_event_consumer,
    "install_memory_event_publisher": broker_composition.install_memory_event_publisher,
    "resolve_memory_event_consumer": broker_composition.resolve_memory_event_consumer,
    "resolve_task_stop_publisher": broker_composition.resolve_task_stop_publisher,
    "resolve_task_stop_consumer": broker_composition.resolve_task_stop_consumer,
}

seam_resolver_cases = {name: {"resolver": name} for name in _SEAM_RESOLVERS}


@pytest.fixture
def clean_holders(monkeypatch):
    from background_memory.infra.events import memory_event_publisher_factory

    monkeypatch.setattr(broker_composition, "_INPROCESS_MEMORY_QUEUE", None)
    monkeypatch.setattr(broker_composition, "_INPROCESS_AGENT_EVENTS", None)
    monkeypatch.setattr(broker_composition, "_INPROCESS_TASK_STOPS", None)
    reset_memory_event_publisher()
    yield memory_event_publisher_factory
    reset_memory_event_publisher()


@pytest.fixture
def kafka_selected(monkeypatch, clean_holders) -> KafkaClientFactory:
    select_kafka(monkeypatch)
    return install_initialized_client_factory(monkeypatch)


def test_the_named_kafka_backend_is_the_one_composed(kafka_selected) -> None:
    assert isinstance(broker_composition.build_message_gateway(inbox_consumer="supervisor-t"), KafkaMessageGateway)


def test_the_kafka_agent_event_seam_follows_the_same_selector(kafka_selected) -> None:
    assert isinstance(broker_composition.resolve_agent_event_publisher(), KafkaAgentEventPublisher)
    assert isinstance(broker_composition.resolve_agent_event_consumer(), KafkaAgentEventConsumer)


def test_the_kafka_task_stop_seam_follows_the_same_selector(kafka_selected) -> None:
    publisher = broker_composition.resolve_task_stop_publisher()
    consumer = broker_composition.resolve_task_stop_consumer()

    assert isinstance(publisher, KafkaTaskStopPublisher)
    assert isinstance(consumer, KafkaTaskStopConsumer)
    assert (publisher._topic, consumer._topic) == (
        f"{KAFKA_TEST_TOPIC_PREFIX}.task-stops",
        f"{KAFKA_TEST_TOPIC_PREFIX}.task-stops",
    )
    assert broker_composition._INPROCESS_TASK_STOPS is None


def test_the_kafka_memory_seam_follows_the_same_selector(kafka_selected, clean_holders) -> None:
    installed = broker_composition.install_memory_event_publisher()

    assert isinstance(installed, KafkaMemoryEventPublisher)
    assert clean_holders.memory_publisher is installed
    assert isinstance(broker_composition.resolve_memory_event_consumer(), KafkaMemoryEventConsumer)


@parametrize_from_dict(seam_resolver_cases)
def test_the_kafka_backend_never_falls_back_to_the_in_process_one(resolver, monkeypatch, clean_holders) -> None:
    from background_memory.interfaces import NullMemoryEventPublisher

    select_kafka(monkeypatch)
    install_client_factory(monkeypatch, KafkaClientFactory())

    with pytest.raises(RuntimeError, match="initialize"):
        _SEAM_RESOLVERS[resolver]()

    assert broker_composition._INPROCESS_AGENT_EVENTS is None
    assert broker_composition._INPROCESS_MEMORY_QUEUE is None
    assert broker_composition._INPROCESS_TASK_STOPS is None
    assert isinstance(clean_holders.memory_publisher, NullMemoryEventPublisher)


def test_kafka_topic_and_group_names_come_from_the_prefix(kafka_selected) -> None:
    prefix = KAFKA_TEST_TOPIC_PREFIX
    gateway = broker_composition.build_message_gateway(inbox_consumer="supervisor-t")
    publisher = broker_composition.resolve_agent_event_publisher()
    consumer = broker_composition.resolve_agent_event_consumer()
    memory_publisher = broker_composition.install_memory_event_publisher()
    memory_consumer = broker_composition.resolve_memory_event_consumer()

    assert (gateway.inbox._topic, gateway.inbox._group_id) == (f"{prefix}.inbound", f"{prefix}.inbound.supervisor")
    assert gateway.outbox._topic == f"{prefix}.outbound"
    assert publisher._topic == f"{prefix}.agent-events"
    assert (consumer._topic, consumer._group_id) == (f"{prefix}.agent-events", f"{prefix}.agent-events.supervisor")
    assert memory_publisher._topic == f"{prefix}.memory-events"
    assert (memory_consumer._topic, memory_consumer._group_id) == (
        f"{prefix}.memory-events",
        f"{prefix}.memory-events.background-memory",
    )


def test_the_kafka_poll_intervals_come_from_their_owning_constants(kafka_selected) -> None:
    gateway = broker_composition.build_message_gateway(inbox_consumer="supervisor-t")

    assert gateway.inbox._max_poll_interval_ms == KAFKA_INBOX_MAX_POLL_INTERVAL_MS == 300_000
    assert broker_composition.resolve_agent_event_consumer()._max_poll_interval_ms == (
        KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS
    )
    assert KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS == 300_000
    assert (
        broker_composition.resolve_memory_event_consumer()._max_poll_interval_ms
        == KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS
        == KAFKA_MAX_POLL_INTERVAL_CEILING_MS
    )


def test_every_kafka_group_consumer_names_its_instance(kafka_selected) -> None:
    gateway = broker_composition.build_message_gateway(inbox_consumer="supervisor-instance-a")
    agent_ids = {broker_composition.resolve_agent_event_consumer()._client_id for _ in range(2)}
    memory_ids = {broker_composition.resolve_memory_event_consumer()._client_id for _ in range(2)}

    assert gateway.inbox._client_id == "supervisor-instance-a"
    assert len(agent_ids) == 2 and all(client_id.startswith("supervisor-") for client_id in agent_ids)
    assert len(memory_ids) == 2 and all(client_id.startswith("background-memory-") for client_id in memory_ids)


def test_no_kafka_backend_is_handed_the_ceiling_and_the_report_publisher_names_no_group(kafka_selected) -> None:
    gateway = broker_composition.build_message_gateway(inbox_consumer="supervisor-t")
    report_publisher = broker_composition.resolve_agent_event_publisher()
    backends = [
        gateway.inbox,
        gateway.outbox,
        report_publisher,
        broker_composition.resolve_agent_event_consumer(),
        broker_composition.install_memory_event_publisher(),
        broker_composition.resolve_memory_event_consumer(),
    ]

    assert kafka_selected.connection.max_message_bytes == KAFKA_TEST_MAX_MESSAGE_BYTES
    assert [backend for backend in backends if hasattr(backend, "_max_message_bytes")] == []
    assert not hasattr(report_publisher, "_group_id")


def test_composing_every_kafka_seam_builds_no_client(kafka_selected, monkeypatch) -> None:
    built = _recorded_accessors(monkeypatch, kafka_selected)

    for compose in _SEAM_RESOLVERS.values():
        compose()

    assert built == []


def test_the_mode_refusal_names_both_brokers(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "inprocess")

    with pytest.raises(RuntimeError) as refusal:
        broker_composition.resolve_broker_backend()

    message = str(refusal.value)
    assert "component tier" in message
    assert "OUROBOROS_BROKER_BACKEND=redis" in message and "kafka" in message


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _inside_a_kafka_arm(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.match_case):
            pattern = node.pattern
            return (
                isinstance(pattern, ast.MatchValue)
                and isinstance(pattern.value, ast.Constant)
                and pattern.value.value == "kafka"
            )
    return False


def test_kafka_modules_are_named_only_inside_kafka_arms() -> None:
    tree = ast.parse(COMPOSITION_MODULE.read_text(encoding="utf-8"))
    parents = _parents(tree)
    imports = kafka_imports(tree)

    assert imports
    assert [ast.unparse(node) for node in imports if not _inside_a_kafka_arm(node, parents)] == []


def _recorded_accessors(monkeypatch, factory: KafkaClientFactory) -> list[str]:
    built: list[str] = []
    record_client_builds(monkeypatch, factory, seen=built)
    return built


@parametrize_from_dict({"inprocess": {"backend": "inprocess"}, "redis": {"backend": "redis"}})
def test_initializing_the_in_process_or_redis_backend_describes_no_kafka_connection(backend, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", backend)
    factory = install_client_factory(monkeypatch, KafkaClientFactory())
    built = _recorded_accessors(monkeypatch, factory)

    assert broker_composition.initialize_broker_backend() is None

    with pytest.raises(RuntimeError, match="not initialized"):
        _ = factory.connection
    assert built == []


def test_initializing_under_the_mode_refuses_the_in_process_backend(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "inprocess")

    with pytest.raises(RuntimeError, match="component tier"):
        broker_composition.initialize_broker_backend()


def test_initializing_kafka_describes_the_connection_and_builds_no_client(monkeypatch) -> None:
    select_kafka(monkeypatch)
    factory = install_client_factory(monkeypatch, KafkaClientFactory())
    built = _recorded_accessors(monkeypatch, factory)

    assert broker_composition.initialize_broker_backend() is None

    assert factory.connection.topic_prefix == KAFKA_TEST_TOPIC_PREFIX
    assert factory.connection.max_message_bytes == KAFKA_TEST_MAX_MESSAGE_BYTES
    assert built == []


def test_kafka_with_no_bootstrap_servers_is_refused_naming_both_settings(monkeypatch) -> None:
    select_kafka(monkeypatch)
    monkeypatch.setenv("OUROBOROS_KAFKA_BOOTSTRAP_SERVERS", "")
    factory = install_client_factory(monkeypatch, KafkaClientFactory())

    with pytest.raises(RuntimeError) as refusal:
        broker_composition.initialize_broker_backend()

    assert "OUROBOROS_BROKER_BACKEND" in str(refusal.value)
    assert "OUROBOROS_KAFKA_BOOTSTRAP_SERVERS" in str(refusal.value)
    with pytest.raises(RuntimeError, match="not initialized"):
        _ = factory.connection


_SECRET_SERVERS = "kafka-secret-host.internal:9093"
_SECRET_PASSWORD = "hunter2-kafka-password"

settings_refusal_cases = {
    "empty-api-version": {
        "env": {"OUROBOROS_KAFKA_API_VERSION": ""},
        "named": ["OUROBOROS_KAFKA_API_VERSION"],
    },
    "unparsable-api-version": {
        "env": {"OUROBOROS_KAFKA_API_VERSION": "3.9.1"},
        "named": ["OUROBOROS_KAFKA_API_VERSION"],
    },
}


@parametrize_from_dict(settings_refusal_cases)
def test_kafka_settings_refusals_name_keys_and_no_secret(env, named, monkeypatch) -> None:
    select_kafka(monkeypatch)
    monkeypatch.setenv("OUROBOROS_KAFKA_BOOTSTRAP_SERVERS", _SECRET_SERVERS)
    monkeypatch.setenv("OUROBOROS_KAFKA_SECURITY_PROTOCOL", "SASL_PLAINTEXT")
    monkeypatch.setenv("OUROBOROS_KAFKA_SASL_USERNAME", "ouroboros-tier")
    monkeypatch.setenv("OUROBOROS_KAFKA_SASL_PASSWORD", _SECRET_PASSWORD)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    factory = install_client_factory(monkeypatch, KafkaClientFactory())
    built = _recorded_accessors(monkeypatch, factory)

    with pytest.raises(RuntimeError) as refusal:
        broker_composition.initialize_broker_backend()

    message = str(refusal.value)
    assert all(key in message for key in named), message
    assert _SECRET_SERVERS not in message and "kafka-secret-host" not in message
    assert _SECRET_PASSWORD not in message
    assert built == []


def test_a_kafka_setting_the_connection_does_not_check_does_not_refuse_the_broker_step(monkeypatch) -> None:
    select_kafka(monkeypatch)
    monkeypatch.setenv("OUROBOROS_KAFKA_BOOTSTRAP_SERVERS", "kafka-without-a-port.invalid")
    monkeypatch.setenv("OUROBOROS_KAFKA_TOPIC_PREFIX", "bad:prefix")
    monkeypatch.setenv("OUROBOROS_KAFKA_SASL_USERNAME", "ouroboros-tier")
    factory = install_client_factory(monkeypatch, KafkaClientFactory())
    built = _recorded_accessors(monkeypatch, factory)

    broker_composition.initialize_broker_backend()

    assert factory.connection.bootstrap_servers == ("kafka-without-a-port.invalid",)
    assert built == []
