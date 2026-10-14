from __future__ import annotations

import ssl
import time

from kafka.errors import CommitFailedError, KafkaError, KafkaTimeoutError
from kafka.structs import TopicPartition
from pydantic import SecretStr

from infra.kafka_client import KafkaClientFactory, KafkaConnection, KafkaOffsetReset, kafka_unanswered_errors
from tests.helpers.tls_listener import tls_listener

HANDSHAKE_DEADLINE_SEC = 5.0
POLL_SLICE_MS = 100
PARTITION = TopicPartition("tls-check.outbound", 0)


def _tls_factory(address: str, ca_path: str) -> KafkaClientFactory:
    factory = KafkaClientFactory()
    factory.initialize(
        KafkaConnection(
            bootstrap_servers=(address,),
            topic_prefix="tls-check",
            security_protocol="SSL",
            sasl_mechanism="PLAIN",
            sasl_username="",
            sasl_password=SecretStr(""),
            ssl_ca_path=ca_path,
            ssl_cert_path="",
            ssl_key_path="",
            api_version=(3, 9),
            max_message_bytes=1_048_576,
        )
    )
    return factory


def _first_raise(consumer) -> Exception | None:
    deadline = time.monotonic() + HANDSHAKE_DEADLINE_SEC
    while time.monotonic() < deadline:
        try:
            consumer.poll(timeout_ms=POLL_SLICE_MS, max_records=1)
        except Exception as error:
            return error
    return None


def test_a_reader_records_client_failures_and_still_raises_a_programming_error() -> None:
    unanswered = kafka_unanswered_errors()
    failures = (
        KafkaTimeoutError(),
        CommitFailedError(),
        AssertionError("KafkaConsumer is closed"),
        ssl.SSLCertVerificationError("certificate verify failed"),
        ssl.SSLError("tlsv13 alert certificate required"),
        OSError(113, "No route to host"),
        ValueError("Topic name is illegal, it can't contain 'x'"),
    )
    bugs = (TypeError(), AttributeError(), KeyError())

    assert [error for error in failures if not isinstance(error, unanswered)] == []
    assert [error for error in bugs if isinstance(error, unanswered)] == []


def test_an_illegal_topic_prefix_is_refused_by_the_library_as_an_unanswered_failure() -> None:
    from kafka.errors import KafkaError
    from kafka.util import ensure_valid_topic_name

    try:
        ensure_valid_topic_name("fleet a.inbound")
    except Exception as error:
        refusal = error

    assert not isinstance(refusal, KafkaError)
    assert isinstance(refusal, kafka_unanswered_errors())


def test_a_certificate_for_another_host_escapes_the_client_poll_outside_the_kafka_error_hierarchy(tmp_path) -> None:
    with tls_listener(tmp_path, certified_host="another-host.invalid") as listener:
        consumer = _tls_factory(listener.address, str(listener.ca_path)).partition_reader(
            auto_offset_reset=KafkaOffsetReset.EARLIEST
        )
        try:
            consumer.assign([PARTITION])
            consumer.seek(PARTITION, 0)
            error = _first_raise(consumer)
        finally:
            consumer.close(autocommit=False, timeout_ms=100)

    assert isinstance(error, ssl.SSLCertVerificationError)
    assert not isinstance(error, KafkaError)
    assert isinstance(error, kafka_unanswered_errors())
