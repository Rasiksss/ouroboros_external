"""Тесты diagnostics-логов system-events publisher."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import pytest

import app.services.system_events.publisher as publisher_module
from app.core.clients.kafka.config import settings as kafka_settings
from app.services.system_events.publisher import BestEffortKafkaSystemEventPublisher


@dataclass
class FakeRecordMetadata:
    """Минимальный аналог aiokafka.structs.RecordMetadata для тестов."""

    partition: int = 0
    offset: int = 0


class FakeProducer:
    """Минимальный fake Kafka producer для проверки логирования."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.sent: list[tuple[str, bytes]] = []

    async def start(self) -> None:
        """Отметить успешный старт producer."""
        self.started = True

    async def stop(self) -> None:
        """Отметить остановку producer."""
        self.stopped = True

    async def send_and_wait(self, topic: str, *, value: bytes, key: bytes | None = None) -> FakeRecordMetadata:
        """Запомнить отправленное сообщение."""
        self.sent.append((topic, value))
        return FakeRecordMetadata()


class FailingStartProducer(FakeProducer):
    """Producer, который падает на старте."""

    async def start(self) -> None:
        """Имитировать ошибку старта producer."""
        raise RuntimeError("start failed")


class SlowSendProducer(FakeProducer):
    """Producer, который зависает на отправке."""

    async def send_and_wait(self, topic: str, *, value: bytes, key: bytes | None = None) -> FakeRecordMetadata:
        """Имитировать долгую отправку."""
        await asyncio.sleep(1)
        return await super().send_and_wait(topic, value=value, key=key)


def _event() -> dict[str, Any]:
    """Собрать тестовое событие с полем, которое не должно попасть в логи."""
    return {
        "event-type": "AGENT_SYSTEM_CONFIGURATION",
        "payload": [
            {
                "config-type": "aef-workstation-create-session",
                "attributes": {
                    "sandbox-session-id": "session-log-test",
                    "secret-field": "must-not-be-logged",
                },
            }
        ],
    }


def _enable_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настроить Kafka settings для локального fake publisher."""
    monkeypatch.setattr(kafka_settings, "KAFKA_ENABLED", True)
    monkeypatch.setattr(kafka_settings, "KAFKA_BROKERS", "localhost:19092")
    monkeypatch.setattr(kafka_settings, "KAFKA_TOPIC", "system.events_AIGW")
    monkeypatch.setattr(kafka_settings, "KAFKA_CLIENT_ID", "")


@pytest.mark.asyncio
async def test_publish_logs_skip_when_kafka_disabled(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kafka_settings, "KAFKA_ENABLED", False)
    producer_created = False

    def _producer_factory(**kwargs: Any) -> FakeProducer:
        nonlocal producer_created
        producer_created = True
        return FakeProducer(**kwargs)

    caplog.set_level(logging.INFO, logger=publisher_module.__name__)
    publisher = BestEffortKafkaSystemEventPublisher(producer_factory=_producer_factory)

    await publisher._publish(_event(), "test")

    assert producer_created is False
    assert "system_event.publish_skipped" in caplog.text
    assert "reason=kafka-disabled" in caplog.text
    assert "must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
async def test_publish_logs_producer_start_and_success(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_kafka(monkeypatch)
    producer = FakeProducer()
    caplog.set_level(logging.INFO, logger=publisher_module.__name__)
    publisher = BestEffortKafkaSystemEventPublisher(producer_factory=lambda **_: producer)

    await publisher._publish(_event(), "test")

    assert producer.started is True
    assert producer.sent
    assert "system_event.kafka_producer_start" in caplog.text
    assert "system_event.kafka_producer_started" in caplog.text
    assert "system_event.kafka_publish_success" in caplog.text
    assert "session-log-test" in caplog.text
    assert "must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
async def test_publish_logs_producer_start_failure_without_publish_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_kafka(monkeypatch)
    caplog.set_level(logging.INFO, logger=publisher_module.__name__)
    publisher = BestEffortKafkaSystemEventPublisher(producer_factory=lambda **kwargs: FailingStartProducer(**kwargs))

    await publisher._publish(_event(), "test")

    assert "system_event.kafka_producer_start_failed" in caplog.text
    assert "system_event.kafka_publish_failed" not in caplog.text
    assert "must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
async def test_publish_logs_timeout_without_payload(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_kafka(monkeypatch)
    monkeypatch.setattr(publisher_module, "_KAFKA_OPERATION_TIMEOUT_SEC", 0.01)
    producer = SlowSendProducer()
    caplog.set_level(logging.INFO, logger=publisher_module.__name__)
    publisher = BestEffortKafkaSystemEventPublisher(producer_factory=lambda **_: producer)

    await publisher._publish(_event(), "test")

    assert producer.stopped is True
    assert "system_event.kafka_publish_timeout" in caplog.text
    assert "system_event.kafka_publish_failed" not in caplog.text
    assert "must-not-be-logged" not in caplog.text
