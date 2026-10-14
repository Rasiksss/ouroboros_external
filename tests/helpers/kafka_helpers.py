from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pytest
from kafka import KafkaAdminClient
from kafka.admin import NewTopic
from pydantic import SecretStr

from infra.kafka_client import (
    KAFKA_POLL_SLICE_MS,
    KafkaClientFactory,
    KafkaConnection,
    parse_kafka_bootstrap_servers,
)
from ouroboros.app.broker_composition import (
    AGENT_EVENT_STREAM,
    INBOUND_STREAM,
    MEMORY_EVENT_STREAM,
    OUTBOUND_STREAM,
    TASK_STOP_STREAM,
)
from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.kafka_recording import KafkaRecordingClientFactory

if TYPE_CHECKING:
    from tests.helpers.kafka_double import KafkaDoubleCluster

TEST_KAFKA_BOOTSTRAP_SERVERS = os.environ.get("OUROBOROS_TEST_KAFKA_BOOTSTRAP_SERVERS", "")
TEST_KAFKA_API_VERSION = os.environ.get("OUROBOROS_TEST_KAFKA_API_VERSION", "")
ASSIGNMENT_TIMEOUT_SEC = 10.0
TEST_EVENT_TOPIC_PARTITIONS = 4
ADMIN_TIMEOUT_MS = 10_000
RELEASE_TIMEOUT_MS = 5_000
TOPIC_READY_PAUSE_SEC = 0.1

needs_kafka = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_KAFKA_BOOTSTRAP_SERVERS,
        reason="set OUROBOROS_TEST_KAFKA_BOOTSTRAP_SERVERS and OUROBOROS_TEST_KAFKA_API_VERSION to run against a broker",
    ),
]


def fresh_prefix() -> str:
    return f"ouroboros-test-{uuid.uuid4().hex}"


def fleet_topics(
    connection: KafkaConnection, *, inbound_partitions: int = 1, outbound_partitions: int = 1
) -> dict[str, int]:
    return {
        connection.topic(INBOUND_STREAM): inbound_partitions,
        connection.topic(OUTBOUND_STREAM): outbound_partitions,
        connection.topic(AGENT_EVENT_STREAM): TEST_EVENT_TOPIC_PARTITIONS,
        connection.topic(MEMORY_EVENT_STREAM): TEST_EVENT_TOPIC_PARTITIONS,
        connection.topic(TASK_STOP_STREAM): 1,
    }


def create_fleet_topics(
    cluster: KafkaDoubleCluster,
    connection: KafkaConnection,
    *,
    inbound_partitions: int = 1,
    outbound_partitions: int = 1,
) -> None:
    topics = fleet_topics(connection, inbound_partitions=inbound_partitions, outbound_partitions=outbound_partitions)
    for name, partitions in topics.items():
        cluster.create_topic(
            name, partitions=partitions, configs={"max.message.bytes": str(connection.max_message_bytes)}
        )


def _pinned_api_version() -> tuple[int, ...]:
    version = TEST_KAFKA_API_VERSION.strip()
    if not version:
        raise RuntimeError(
            "OUROBOROS_TEST_KAFKA_API_VERSION is empty: pin the test broker's protocol version as MAJOR.MINOR"
        )
    return tuple(int(part) for part in version.split("."))


def kafka_connection(prefix: str, **overrides: Any) -> KafkaConnection:
    fields: dict[str, Any] = {
        "bootstrap_servers": ("kafka-0.test:9092",),
        "topic_prefix": prefix,
        "security_protocol": "PLAINTEXT",
        "sasl_mechanism": "PLAIN",
        "sasl_username": "",
        "sasl_password": SecretStr(""),
        "ssl_ca_path": "",
        "ssl_cert_path": "",
        "ssl_key_path": "",
        "api_version": (3, 9),
        "max_message_bytes": AppSettings.model_fields["OUROBOROS_KAFKA_MAX_MESSAGE_BYTES"].default,
    }
    return KafkaConnection(**{**fields, **overrides})


def real_client_factory(prefix: str) -> KafkaClientFactory:
    factory = KafkaClientFactory()
    factory.initialize(
        kafka_connection(
            prefix,
            bootstrap_servers=tuple(parse_kafka_bootstrap_servers(TEST_KAFKA_BOOTSTRAP_SERVERS)),
            api_version=_pinned_api_version(),
        )
    )
    return factory


def broker_admin() -> KafkaAdminClient:
    return KafkaAdminClient(
        bootstrap_servers=parse_kafka_bootstrap_servers(TEST_KAFKA_BOOTSTRAP_SERVERS),
        api_version=_pinned_api_version(),
        request_timeout_ms=ADMIN_TIMEOUT_MS,
    )


def create_broker_topics(partitions: Mapping[str, int]) -> None:
    ceiling = str(AppSettings.model_fields["OUROBOROS_KAFKA_MAX_MESSAGE_BYTES"].default)
    admin = broker_admin()
    try:
        admin.create_topics(
            [
                NewTopic(name, count, 1, topic_configs={"max.message.bytes": ceiling})
                for name, count in partitions.items()
            ],
            timeout_ms=ADMIN_TIMEOUT_MS,
        )
        deadline = time.monotonic() + ADMIN_TIMEOUT_MS / 1000
        while any(entry["error_code"] != 0 for entry in admin.describe_topics(list(partitions))):
            assert time.monotonic() < deadline, f"the topics {sorted(partitions)} did not appear"
            time.sleep(TOPIC_READY_PAUSE_SEC)
    finally:
        admin.close()


def drop_prefixed_topics(prefix: str) -> None:
    admin = broker_admin()
    try:
        names = [name for name in admin.list_topics() if name.startswith(f"{prefix}.")]
        if names:
            admin.delete_topics(names, timeout_ms=ADMIN_TIMEOUT_MS)
    finally:
        admin.close()


def release_real_broker(clients: KafkaRecordingClientFactory, prefix: str) -> None:
    for recorded in clients.built:
        if recorded.accessor == "async_producer":
            # Its gateway closes it on the owning event loop before synchronous teardown.
            continue
        if recorded.accessor == "producer":
            recorded.client.wrapped.close(timeout=RELEASE_TIMEOUT_MS / 1000)
        else:
            recorded.client.wrapped.close(autocommit=False, timeout_ms=RELEASE_TIMEOUT_MS)
    drop_prefixed_topics(prefix)


def await_assignment(reader: Any, *, clients: KafkaRecordingClientFactory) -> None:
    deadline = time.monotonic() + ASSIGNMENT_TIMEOUT_SEC
    while True:
        handed = reader.poll(KAFKA_POLL_SLICE_MS / 1000)
        if handed is not None:
            raise AssertionError(f"await_assignment took an item off a topic it must leave untouched: {handed!r}")
        consumers = clients.clients_of("group_consumer")
        if consumers and consumers[-1].client.wrapped.assignment():
            return
        if time.monotonic() >= deadline:
            group = consumers[-1].kwargs["group_id"] if consumers else "(no group consumer was built)"
            raise AssertionError(f"the consumer of group {group} held no assignment within {ASSIGNMENT_TIMEOUT_SEC} s")


__all__ = [
    "ADMIN_TIMEOUT_MS",
    "ASSIGNMENT_TIMEOUT_SEC",
    "TEST_EVENT_TOPIC_PARTITIONS",
    "TEST_KAFKA_API_VERSION",
    "TEST_KAFKA_BOOTSTRAP_SERVERS",
    "await_assignment",
    "broker_admin",
    "create_broker_topics",
    "create_fleet_topics",
    "drop_prefixed_topics",
    "fleet_topics",
    "fresh_prefix",
    "kafka_connection",
    "needs_kafka",
    "real_client_factory",
    "release_real_broker",
]
