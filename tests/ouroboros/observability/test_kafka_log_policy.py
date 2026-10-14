from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from collections import Counter

import pytest
from pydantic import SecretStr

from ouroboros.observability.logging_setup import configure_logging, next_level_up
from tests.helpers.log_handlers import logging_configuration_restored

PASSWORD = "log-leak-check-password"
REFUSED = "127.0.0.1:1"
GATED_ERROR_LOGGERS = (
    "kafka.conn",
    "kafka.client",
    "kafka.coordinator",
    "kafka.coordinator.heartbeat",
    "kafka.coordinator.consumer",
    "kafka.producer.sender",
)
GATED_WARNING_LOGGERS = ("kafka.sasl.plain", "kafka.sasl.scram")


@pytest.fixture(autouse=True)
def restore_logging_configuration():
    with logging_configuration_restored():
        yield


def _records(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def _poll_for(consumer, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        consumer.poll(timeout_ms=100)


@pytest.mark.parametrize("level", ["DEBUG", "5", "1"])
def test_no_application_level_lets_the_kafka_library_log_below_info(level, capsys):
    configure_logging(level=level, log_format="json")
    root_level = logging.getLogger().level
    kafka_level = logging.getLogger("kafka").level

    logging.getLogger("kafka.admin.client").debug(
        "Starting KafkaAdminClient with configuration: %s", {"sasl_plain_password": PASSWORD}
    )
    logging.getLogger("kafka.producer.kafka").debug("Sending (key=%r value=%r) to %s", None, PASSWORD, "fleet-a.x")
    output = capsys.readouterr().out
    configure_logging(level="INFO", log_format="json")

    assert kafka_level == max(logging.INFO, next_level_up(root_level))
    assert PASSWORD not in output


def test_a_group_consumer_failing_to_reach_its_broker_at_debug_leaks_no_password(capsys):
    from infra.kafka_client import KafkaClientFactory, KafkaConnection, KafkaOffsetReset

    configure_logging(level="DEBUG", log_format="json")
    factory = KafkaClientFactory()
    factory.initialize(
        KafkaConnection(
            bootstrap_servers=(REFUSED,),
            topic_prefix="fleet-a",
            security_protocol="SASL_PLAINTEXT",
            sasl_mechanism="PLAIN",
            sasl_username="leak-check-user",
            sasl_password=SecretStr(PASSWORD),
            ssl_ca_path="",
            ssl_cert_path="",
            ssl_key_path="",
            api_version=(3, 9),
            max_message_bytes=1_048_576,
        )
    )
    consumer = factory.group_consumer(
        group_id="fleet-a.inbound.supervisor",
        client_id="supervisor-0",
        auto_offset_reset=KafkaOffsetReset.EARLIEST,
        max_poll_interval_ms=3_000,
    )
    consumer.subscribe(["fleet-a.inbound"])

    _poll_for(consumer, 0.5)
    consumer.close(autocommit=False, timeout_ms=200)
    output = capsys.readouterr().out
    configure_logging(level="INFO", log_format="json")

    assert [record for record in _records(output) if record["logger"].startswith("kafka")]
    assert PASSWORD not in output


def test_reconnect_lines_are_reported_once_per_logger_and_level_per_interval(monkeypatch, capsys):
    from ouroboros.contracts.stream_replies import BROKER_REPORT_INTERVAL_SEC

    configure_logging(level="INFO", log_format="json")
    for name in GATED_ERROR_LOGGERS:
        for _ in range(5):
            logging.getLogger(name).error("gate-check")
    for name in (*GATED_WARNING_LOGGERS, "kafka.conn"):
        for _ in range(5):
            logging.getLogger(name).warning("gate-check")
    for _ in range(5):
        logging.getLogger("kafka.consumer.fetcher").error("gate-check")
    first = _gate_checks(capsys.readouterr().out)

    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + BROKER_REPORT_INTERVAL_SEC + 1.0)
    logging.getLogger("kafka.conn").error("gate-check")
    later = _gate_checks(capsys.readouterr().out)

    assert first == Counter(
        {
            **{(name, "ERROR"): 1 for name in GATED_ERROR_LOGGERS},
            **{(name, "WARNING"): 1 for name in (*GATED_WARNING_LOGGERS, "kafka.conn")},
            ("kafka.consumer.fetcher", "ERROR"): 5,
        }
    )
    assert later == Counter({("kafka.conn", "ERROR"): 1})


def test_the_gate_holds_back_one_repeated_line_and_never_a_different_diagnosis(capsys):
    configure_logging(level="INFO", log_format="json")
    log = logging.getLogger("kafka.conn")

    for _ in range(3):
        log.error("<BrokerConnection node_id=%s> failed to connect", 1)
    log.error("<BrokerConnection node_id=%s> failed to connect", 2)
    log.error("SSL connection failed to verify the broker's certificate")
    messages = [record["message"] for record in _records(capsys.readouterr().out)]

    assert messages == [
        "<BrokerConnection node_id=1> failed to connect",
        "SSL connection failed to verify the broker's certificate",
    ]


def _gate_checks(output: str) -> Counter:
    return Counter(
        (record["logger"], record["level"]) for record in _records(output) if record["message"] == "gate-check"
    )


def test_configuring_logging_again_leaves_one_gate_on_each_gated_logger():
    from ouroboros.observability.logging_setup import _KAFKA_RECONNECT_LOGGERS, _KafkaReconnectGate

    configure_logging(level="INFO", log_format="json")
    configure_logging(level="INFO", log_format="json")

    assert set(_KAFKA_RECONNECT_LOGGERS) == {*GATED_ERROR_LOGGERS, *GATED_WARNING_LOGGERS}
    assert {
        name: sum(isinstance(entry, _KafkaReconnectGate) for entry in logging.getLogger(name).filters)
        for name in _KAFKA_RECONNECT_LOGGERS
    } == dict.fromkeys(_KAFKA_RECONNECT_LOGGERS, 1)


def test_the_client_library_registers_every_gated_logger_name():
    from ouroboros.observability.logging_setup import _KAFKA_RECONNECT_LOGGERS

    code = (
        "import logging\n"
        "import kafka, kafka.admin, kafka.consumer, kafka.producer\n"
        "assert kafka.__version__.startswith('2.3.'), kafka.__version__\n"
        f"names = {_KAFKA_RECONNECT_LOGGERS!r}\n"
        "missing = [n for n in names if not isinstance(logging.Logger.manager.loggerDict.get(n), logging.Logger)]\n"
        "assert missing == [], missing\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr


def test_a_plaintext_sasl_reconnect_storm_warns_about_the_credentials_once(capsys):
    from kafka import KafkaConsumer
    from kafka.structs import TopicPartition

    configure_logging(log_format="json")
    consumer = KafkaConsumer(
        bootstrap_servers=[REFUSED],
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="PLAIN",
        sasl_plain_username="leak-check-user",
        sasl_plain_password=PASSWORD,
        api_version=(3, 9),
        reconnect_backoff_ms=50,
        reconnect_backoff_max_ms=100,
    )
    consumer.assign([TopicPartition("fleet-a.inbound", 0)])

    _poll_for(consumer, 1.5)
    consumer.close(autocommit=False, timeout_ms=200)
    records = _records(capsys.readouterr().out)

    assert len([record for record in records if record["logger"] == "kafka.sasl.plain"]) == 1
