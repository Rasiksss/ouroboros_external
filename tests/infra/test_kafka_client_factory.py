from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys
import threading
import time

import pytest
from pydantic import SecretStr

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
USERNAME = "leak-check-user"
PASSWORD = "leak-check-password"
BROKER_CONNECTIONS_MAX_IDLE_DEFAULT_MS = 600_000
CONNECTION_FIELDS = {
    "topic_prefix": "fleet-a",
    "security_protocol": "PLAINTEXT",
    "sasl_mechanism": "PLAIN",
    "sasl_username": "",
    "sasl_password": SecretStr(""),
    "ssl_ca_path": "",
    "ssl_cert_path": "",
    "ssl_key_path": "",
    "api_version": (3, 9),
    "max_message_bytes": 33_554_432,
}


def _connection(**overrides):
    from infra.kafka_client import KafkaConnection

    return KafkaConnection(**{**CONNECTION_FIELDS, **overrides})


UNROUTABLE = "192.0.2.1:9092"
FACTORY_ACCESSORS = {
    "async_producer": {},
    "producer": {"topic": "fleet-a.inbound"},
    "group_consumer": {
        "group_id": "fleet-a.inbound.supervisor",
        "client_id": "supervisor-0",
        "auto_offset_reset": "earliest",
        "max_poll_interval_ms": 300_000,
    },
    "async_group_consumer": {
        "group_id": "fleet-a.inbound.supervisor",
        "client_id": "supervisor-0",
        "auto_offset_reset": "earliest",
        "max_poll_interval_ms": 300_000,
    },
    "partition_reader": {"auto_offset_reset": "latest"},
}


def _factory(**overrides):
    from infra.kafka_client import KafkaClientFactory

    factory = KafkaClientFactory()
    factory.initialize(_connection(bootstrap_servers=(UNROUTABLE,), **overrides))
    return factory


def _senders_without_network(monkeypatch) -> None:
    from kafka.producer.sender import Sender

    monkeypatch.setattr(Sender, "run", lambda sender: sender._client.close())


def _close_client(client) -> None:
    from kafka import KafkaProducer

    if isinstance(client, KafkaProducer):
        client.close(timeout=0)
    else:
        client.close(autocommit=False, timeout_ms=100)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _async_group_consumer(factory, **overrides):
    """Built inside a coroutine: this library's constructor asks for the running loop."""
    from infra.kafka_client import KafkaOffsetReset

    kwargs = {
        "group_id": "fleet-a.memory-events.memory",
        "client_id": "memory-0",
        "auto_offset_reset": KafkaOffsetReset.LATEST,
        "max_poll_interval_ms": 300_000,
        **overrides,
    }

    async def _build():
        return factory.async_group_consumer(**kwargs)

    return _run(_build())


def _async_producer(factory):
    async def build():
        producer = factory.async_producer()
        await producer.stop()
        return producer

    return _run(build())


def _group_consumer(factory, **overrides):
    from infra.kafka_client import KafkaOffsetReset

    kwargs = {**FACTORY_ACCESSORS["group_consumer"], "auto_offset_reset": KafkaOffsetReset.EARLIEST, **overrides}
    return factory.group_consumer(**kwargs)


def test_the_client_factory_interface_declares_its_client_builders_abstract():
    from infra.kafka_client import IKafkaClientFactory

    assert IKafkaClientFactory.__abstractmethods__ == frozenset(
        {"producer", "async_producer", "group_consumer", "async_group_consumer", "partition_reader"}
    )


def test_a_factory_refuses_every_accessor_before_it_is_initialized():
    from infra.kafka_client import KafkaClientFactory

    factory = KafkaClientFactory()

    for name, kwargs in FACTORY_ACCESSORS.items():
        with pytest.raises(RuntimeError, match=r"KafkaClientFactory not initialized: call initialize\(\) first"):
            getattr(factory, name)(**kwargs)
    with pytest.raises(RuntimeError, match=r"KafkaClientFactory not initialized: call initialize\(\) first"):
        _ = factory.connection


def test_initializing_the_process_factory_loads_no_client_library_and_builds_no_client():
    fields = {**CONNECTION_FIELDS, "bootstrap_servers": (UNROUTABLE,)}
    code = (
        "import sys\n"
        "from pydantic import SecretStr\n"
        "from infra.kafka_client import KafkaConnection, kafka_client_factory\n"
        f"connection = KafkaConnection(**{fields!r})\n"
        "kafka_client_factory.initialize(connection)\n"
        "assert kafka_client_factory.connection is connection\n"
        "leaked = [m for m in sys.modules if m.split('.')[0] in ('kafka', 'aiokafka')]\n"
        "assert not leaked, leaked\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO_ROOT))

    assert proc.returncode == 0, proc.stderr


def test_a_producer_states_every_delivery_option(monkeypatch):
    _senders_without_network(monkeypatch)
    factory = _factory(max_message_bytes=4_194_304)
    producer = factory.producer(topic="fleet-a.inbound")

    try:
        config = producer.config
        assert {key: config[key] for key in PRODUCER_OPTIONS} == PRODUCER_OPTIONS
        assert config["retries"] > 0
    finally:
        producer.close(timeout=0)


PRODUCER_OPTIONS = {
    "acks": -1,
    "enable_idempotence": True,
    "max_in_flight_requests_per_connection": 1,
    "max_block_ms": 5_000,
    "request_timeout_ms": 10_000,
    "delivery_timeout_ms": 10_000,
    "linger_ms": 0,
    "max_request_size": 4_194_304,
    "compression_type": "gzip",
    "api_version": (3, 9),
    "reconnect_backoff_max_ms": 5_000,
    "transactional_id": None,
}


def test_a_group_consumer_refuses_a_poll_interval_the_connection_bounds_cannot_carry():
    from infra.kafka_client import KAFKA_MAX_POLL_INTERVAL_CEILING_MS

    factory = _factory(max_message_bytes=4_194_304)

    for refused in (0, -1, KAFKA_MAX_POLL_INTERVAL_CEILING_MS + 1, 535_000, 720_000):
        with pytest.raises(RuntimeError, match="max_poll_interval_ms"):
            _group_consumer(factory, max_poll_interval_ms=refused)


@pytest.mark.parametrize("max_poll_interval_ms", [3_000, 60_000, 300_000])
def test_a_group_consumer_builds_for_any_poll_interval_and_settles_nothing_on_its_own(max_poll_interval_ms):
    from infra.kafka_client import KAFKA_POLL_SLICE_MS

    factory = _factory(max_message_bytes=4_194_304)
    consumer = _group_consumer(factory, max_poll_interval_ms=max_poll_interval_ms)

    try:
        config = consumer.config
        assert {key: config[key] for key in GROUP_CONSUMER_OPTIONS} == {
            **GROUP_CONSUMER_OPTIONS,
            "max_poll_interval_ms": max_poll_interval_ms,
            "fetch_max_wait_ms": KAFKA_POLL_SLICE_MS,
        }
        assert config["request_timeout_ms"] > max_poll_interval_ms
        assert config["request_timeout_ms"] > config["session_timeout_ms"]
        assert config["connections_max_idle_ms"] > config["request_timeout_ms"]
        assert config["connections_max_idle_ms"] < BROKER_CONNECTIONS_MAX_IDLE_DEFAULT_MS
    finally:
        consumer.close(autocommit=False, timeout_ms=100)


GROUP_CONSUMER_OPTIONS = {
    "group_id": "fleet-a.inbound.supervisor",
    "client_id": "supervisor-0",
    "enable_auto_commit": False,
    "max_poll_records": 1,
    "auto_offset_reset": "earliest",
    "max_poll_interval_ms": None,
    "session_timeout_ms": 10_000,
    "heartbeat_interval_ms": 3_000,
    "fetch_max_wait_ms": None,
    "max_partition_fetch_bytes": 4_194_304,
    "fetch_max_bytes": 4_194_304,
    "check_crcs": True,
    "isolation_level": "read_uncommitted",
    "api_version": (3, 9),
}


def test_an_asynchronous_group_consumer_refuses_the_same_poll_intervals_its_sibling_does():
    """One bound for both libraries: the ceiling is the brokers', not a client's."""
    from infra.kafka_client import KAFKA_MAX_POLL_INTERVAL_CEILING_MS

    factory = _factory(max_message_bytes=4_194_304)

    for refused in (0, -1, KAFKA_MAX_POLL_INTERVAL_CEILING_MS + 1, 535_000, 720_000):
        with pytest.raises(RuntimeError, match="max_poll_interval_ms"):
            _async_group_consumer(factory, max_poll_interval_ms=refused)


@pytest.mark.parametrize("max_poll_interval_ms", [3_000, 60_000, 300_000])
def test_an_asynchronous_group_consumer_states_every_bound_and_pins_no_protocol_version(max_poll_interval_ms):
    """`api_version` is a no-op in this library since 0.13 and warns when passed."""
    import warnings

    from infra.kafka_client import KAFKA_POLL_SLICE_MS

    factory = _factory(max_message_bytes=4_194_304)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        consumer = _async_group_consumer(factory, max_poll_interval_ms=max_poll_interval_ms)

    request_timeout_ms = max(max_poll_interval_ms, 10_000) + 5_000
    assert consumer._group_id == "fleet-a.memory-events.memory"
    assert consumer._client._client_id == "memory-0"
    assert consumer._enable_auto_commit is False
    assert consumer._max_poll_records == 1
    assert consumer._max_poll_interval_ms == max_poll_interval_ms
    # Defaulted to the session timeout by this library; left alone a long-interval member gives
    # its partitions away after ten seconds.
    assert consumer._rebalance_timeout_ms == max_poll_interval_ms
    assert consumer._session_timeout_ms == 10_000
    assert consumer._heartbeat_interval_ms == 3_000
    assert consumer._request_timeout_ms == request_timeout_ms
    assert consumer._fetch_max_wait_ms == KAFKA_POLL_SLICE_MS
    assert consumer._fetch_max_bytes == 4_194_304
    assert consumer._max_partition_fetch_bytes == 4_194_304
    assert consumer._check_crcs is True
    assert consumer._client._conns == {}, "the constructor opened a connection"


@pytest.mark.parametrize("producer", [False, True])
def test_an_asynchronous_client_is_given_no_sasl_or_tls_option_on_plaintext(producer):
    factory = _factory(max_message_bytes=4_194_304)
    client = _async_producer(factory).client if producer else _async_group_consumer(factory)._client

    assert client._security_protocol == "PLAINTEXT"
    assert client._ssl_context is None


@pytest.mark.parametrize("producer", [False, True])
def test_an_asynchronous_client_carries_the_sasl_and_tls_options(monkeypatch, tmp_path, producer):
    """TLS arrives as a context here, built from the same three paths the connection holds."""
    import ssl

    paths = {name: tmp_path / f"{name}.pem" for name in ("ca", "cert", "key")}
    for path in paths.values():
        path.write_text("pem", encoding="utf-8")
    built: dict[str, object] = {}
    context = ssl.create_default_context()
    monkeypatch.setattr("aiokafka.helpers.create_ssl_context", lambda **kwargs: built.update(kwargs) or context)
    factory = _factory(
        security_protocol="SASL_SSL",
        sasl_mechanism="SCRAM-SHA-512",
        sasl_username=USERNAME,
        sasl_password=SecretStr(PASSWORD),
        ssl_ca_path=str(paths["ca"]),
        ssl_cert_path=str(paths["cert"]),
        ssl_key_path=str(paths["key"]),
    )

    client = _async_producer(factory).client if producer else _async_group_consumer(factory)._client

    assert built == {"cafile": str(paths["ca"]), "certfile": str(paths["cert"]), "keyfile": str(paths["key"])}
    assert client._security_protocol == "SASL_SSL"
    assert client._sasl_mechanism == "SCRAM-SHA-512"
    assert client._sasl_plain_username == USERNAME
    assert client._ssl_context is context


def test_a_partition_reader_joins_no_group_and_bounds_its_offset_lookups():
    from infra.kafka_client import KafkaOffsetReset

    factory = _factory(max_message_bytes=4_194_304)
    reader = factory.partition_reader(auto_offset_reset=KafkaOffsetReset.EARLIEST)

    try:
        assert {key: reader.config[key] for key in PARTITION_READER_OPTIONS} == PARTITION_READER_OPTIONS
    finally:
        reader.close(autocommit=False, timeout_ms=100)


PARTITION_READER_OPTIONS = {
    "group_id": None,
    "enable_auto_commit": False,
    "max_poll_records": 1,
    "auto_offset_reset": "earliest",
    "max_partition_fetch_bytes": 4_194_304,
    "fetch_max_bytes": 4_194_304,
    "check_crcs": True,
    "request_timeout_ms": 5_000,
    "fetch_max_wait_ms": 250,
}


def test_every_client_carries_the_sasl_and_tls_options(monkeypatch, tmp_path):
    _senders_without_network(monkeypatch)
    paths = {name: tmp_path / f"{name}.pem" for name in ("ca", "cert", "key")}
    for path in paths.values():
        path.write_text("pem", encoding="utf-8")
    factory = _factory(
        security_protocol="SASL_SSL",
        sasl_mechanism="SCRAM-SHA-256",
        sasl_username=USERNAME,
        sasl_password=SecretStr(PASSWORD),
        ssl_ca_path=str(paths["ca"]),
        ssl_cert_path=str(paths["cert"]),
        ssl_key_path=str(paths["key"]),
    )
    expected = {
        "security_protocol": "SASL_SSL",
        "sasl_mechanism": "SCRAM-SHA-256",
        "sasl_plain_username": USERNAME,
        "sasl_plain_password": PASSWORD,
        "ssl_cafile": str(paths["ca"]),
        "ssl_certfile": str(paths["cert"]),
        "ssl_keyfile": str(paths["key"]),
        "ssl_check_hostname": True,
        "bootstrap_servers": [UNROUTABLE],
    }

    for client in _built_clients(factory):
        try:
            assert {key: client.config[key] for key in expected} == expected
        finally:
            _close_client(client)


def test_a_plaintext_client_is_given_no_sasl_or_tls_option(monkeypatch):
    from kafka import KafkaConsumer, KafkaProducer

    _senders_without_network(monkeypatch)
    factory = _factory()

    for client in _built_clients(factory):
        defaults = (KafkaProducer if isinstance(client, KafkaProducer) else KafkaConsumer).DEFAULT_CONFIG
        try:
            assert client.config["security_protocol"] == "PLAINTEXT"
            assert {key: client.config[key] for key in SECURITY_OPTIONS} == {
                key: defaults[key] for key in SECURITY_OPTIONS
            }
        finally:
            _close_client(client)


SECURITY_OPTIONS = (
    "sasl_mechanism",
    "sasl_plain_username",
    "sasl_plain_password",
    "ssl_cafile",
    "ssl_certfile",
    "ssl_keyfile",
)


def _built_clients(factory):
    return [
        factory.producer(topic="fleet-a.inbound"),
        _group_consumer(factory),
        factory.partition_reader(auto_offset_reset="earliest"),
    ]


def test_no_builder_passes_a_topic_auto_creation_option(monkeypatch):
    import kafka

    passed = []
    for name in ("KafkaProducer", "KafkaConsumer"):
        monkeypatch.setattr(kafka, name, lambda **kwargs: passed.append(kwargs) or object())
    factory = _factory()

    factory.producer(topic="fleet-a.inbound")
    _group_consumer(factory)
    factory.partition_reader(auto_offset_reset="earliest")

    assert len(passed) == 3
    assert [kwargs for kwargs in passed if "allow_auto_create_topics" in kwargs] == []


def test_no_client_probes_the_broker_while_it_is_built(monkeypatch):
    _senders_without_network(monkeypatch)
    factory = _factory()

    for build in (
        lambda: factory.producer(topic="fleet-a.inbound"),
        lambda: _group_consumer(factory),
        lambda: factory.partition_reader(auto_offset_reset="earliest"),
    ):
        started = time.monotonic()
        client = build()
        elapsed = time.monotonic() - started
        _close_client(client)
        assert elapsed < 0.5


def test_a_client_built_without_a_pinned_version_probes_and_fails():
    from kafka import KafkaConsumer
    from kafka.errors import NoBrokersAvailable

    with pytest.raises(NoBrokersAvailable):
        KafkaConsumer(bootstrap_servers=[UNROUTABLE], api_version=None, api_version_auto_timeout_ms=300)


def test_each_topic_has_one_producer_however_many_ask_at_once(monkeypatch):
    _senders_without_network(monkeypatch)
    factory = _factory()
    barrier = threading.Barrier(8)
    received = []

    def _ask():
        barrier.wait()
        received.append(factory.producer(topic="t.a"))

    threads = [threading.Thread(target=_ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    other = factory.producer(topic="t.b")
    try:
        assert len(received) == 8
        assert {id(producer) for producer in received} == {id(factory.producer(topic="t.a"))}
        assert other is not received[0]
    finally:
        for producer in {id(producer): producer for producer in [*received, other]}.values():
            producer.close(timeout=0)


def test_a_factory_refuses_a_second_initialization_for_another_connection_and_keeps_its_own(monkeypatch):
    _senders_without_network(monkeypatch)
    factory = _factory()
    producer = factory.producer(topic="fleet-a.inbound")

    try:
        with pytest.raises(RuntimeError, match="earlier connection"):
            factory.initialize(_connection(bootstrap_servers=(UNROUTABLE,), topic_prefix="fleet-b"))

        assert factory.connection.topic_prefix == "fleet-a"
        assert factory.producer(topic="fleet-a.inbound") is producer
    finally:
        producer.close(timeout=0)


def test_a_factory_that_built_nothing_yet_still_refuses_another_connection():
    factory = _factory()

    with pytest.raises(RuntimeError, match="earlier connection"):
        factory.initialize(_connection(bootstrap_servers=(UNROUTABLE,), topic_prefix="fleet-b"))

    assert factory.connection.topic_prefix == "fleet-a"


def test_a_factory_accepts_the_connection_it_already_holds():
    factory = _factory()

    factory.initialize(_connection(bootstrap_servers=(UNROUTABLE,)))

    assert factory.connection == _connection(bootstrap_servers=(UNROUTABLE,))
