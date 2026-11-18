"""Best-effort publisher для system.events Kafka topic."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from collections.abc import Callable
from typing import Any, Protocol

from aiokafka import AIOKafkaProducer
from aiokafka.structs import RecordMetadata

from app.core.clients.kafka.config import settings as kafka_settings
from app.models.session import Session
from app.services.system_events.builder import (
    build_session_created_event,
    build_session_deleted_event,
    build_workstation_start_event,
)

logger = logging.getLogger(__name__)
_KAFKA_OPERATION_TIMEOUT_SEC = 5.0

ProducerFactory = Callable[..., AIOKafkaProducer]


class SystemEventPublisher(Protocol):
    """Контракт публикации AGENT_SYSTEM_CONFIGURATION для сервисного слоя."""

    async def publish_workstation_start(self) -> None:
        """Опубликовать событие старта приложения."""

    async def publish_session_created(self, session: Session, *, total_sandbox_sessions: int) -> None:
        """Опубликовать событие создания sandbox-сессии."""

    async def publish_session_deleted(self, session: Session, *, total_sandbox_sessions: int) -> None:
        """Опубликовать событие завершения sandbox-сессии."""

    async def close(self) -> None:
        """Закрыть ресурсы publisher'а."""


class NoopSystemEventPublisher:
    """Publisher-заглушка для тестов и отключенного контура."""

    async def publish_workstation_start(self) -> None:
        """Не публиковать событие старта приложения."""

    async def publish_session_created(self, session: Session, *, total_sandbox_sessions: int) -> None:
        """Не публиковать событие создания sandbox-сессии."""

    async def publish_session_deleted(self, session: Session, *, total_sandbox_sessions: int) -> None:
        """Не публиковать событие завершения sandbox-сессии."""

    async def close(self) -> None:
        """Закрытие не требуется."""


class BestEffortKafkaSystemEventPublisher:
    """Ленивый Kafka publisher, который не ломает основной workflow при ошибках."""

    def __init__(self, producer_factory: ProducerFactory = AIOKafkaProducer) -> None:
        self._producer_factory = producer_factory
        self._producer: AIOKafkaProducer | None = None

    async def publish_workstation_start(self) -> None:
        """Опубликовать событие старта AEF WorkStation."""
        await self._publish(build_workstation_start_event(), operation="publish_workstation_start")

    async def publish_session_created(self, session: Session, *, total_sandbox_sessions: int) -> None:
        """Опубликовать событие создания sandbox-сессии."""
        await self._publish(
            build_session_created_event(session, total_sandbox_sessions=total_sandbox_sessions),
            operation="publish_session_created",
        )

    async def publish_session_deleted(self, session: Session, *, total_sandbox_sessions: int) -> None:
        """Опубликовать событие завершения sandbox-сессии."""
        await self._publish(
            build_session_deleted_event(session, total_sandbox_sessions=total_sandbox_sessions),
            operation="publish_session_deleted",
        )

    async def close(self) -> None:
        """Остановить Kafka producer, если он был лениво создан."""
        if self._producer is None:
            return
        try:
            await self._producer.stop()
        except Exception:
            logger.exception("system_event.kafka_stop_failed")
        finally:
            self._producer = None

    async def _publish(self, event: dict[str, Any], operation: str) -> None:
        """Отправить событие в Kafka, логируя и подавляя ошибки доставки."""
        started = time.perf_counter()
        event_type = _event_type(event)
        config_type = _config_type(event)
        session_id = _session_id(event)
        topic = kafka_settings.KAFKA_TOPIC

        kafka_logging_attributes = {
            "kafka.to.topic": topic,
            "kafka.to.key": None,
            "kafka.to.broker": kafka_settings.KAFKA_BROKERS,
        }

        logger.debug(
            "%s: publishing system event",
            operation,
            extra={
                "event.category": "kafka",
                "event.action": "publish",
                "app.operation": operation,
                "event.outcome": "unknown",
                "app.status": "accepted",
                **kafka_logging_attributes,
            },
        )
        try:
            producer = await self._ensure_producer(event_type=event_type, config_type=config_type)
            if producer is None:
                return
            metadata: RecordMetadata = await asyncio.wait_for(
                producer.send_and_wait(
                    topic,
                    value=json.dumps(event, ensure_ascii=False, default=str).encode("utf-8"),
                ),
                timeout=_KAFKA_OPERATION_TIMEOUT_SEC,
            )
            kafka_logging_attributes.update(
                {
                    "kafka.to.partition": metadata.partition,
                    "kafka.to.offset": metadata.offset,
                }
            )
        except TimeoutError as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                "system_event.kafka_publish_timeout event_type=%s config_type=%s topic=%s session_id=%s timeout_sec=%s",
                event_type,
                config_type,
                topic,
                session_id,
                _KAFKA_OPERATION_TIMEOUT_SEC,
                extra={
                    "event.category": "kafka",
                    "event.action": "publish",
                    "app.operation": operation,
                    "event.outcome": "failure",
                    "event.reason": "timeout",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(e).__name__,
                    "error.message": str(e),
                    "error.stack_trace": traceback.format_exc(),
                    **kafka_logging_attributes,
                },
            )
            await self._drop_producer()
        except Exception as e:
            await self._drop_producer()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                "system_event.kafka_publish_failed event_type=%s config_type=%s topic=%s session_id=%s",
                event_type,
                config_type,
                topic,
                session_id,
                extra={
                    "event.category": "kafka",
                    "event.action": "publish",
                    "app.operation": operation,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(e).__name__,
                    "error.message": str(e),
                    "error.stack_trace": traceback.format_exc(),
                    **kafka_logging_attributes,
                },
            )
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "system_event.kafka_publish_success event_type=%s config_type=%s topic=%s session_id=%s",
                event_type,
                config_type,
                topic,
                session_id,
                extra={
                    "event.category": "kafka",
                    "event.action": "publish",
                    "app.operation": operation,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **kafka_logging_attributes,
                },
            )

    async def _ensure_producer(self, *, event_type: str, config_type: str) -> AIOKafkaProducer | None:
        """Лениво создать и запустить Kafka producer для system-events."""
        if not kafka_settings.KAFKA_ENABLED:
            logger.info(
                "system_event.publish_skipped reason=kafka-disabled event_type=%s config_type=%s",
                event_type,
                config_type,
            )
            return None
        if not kafka_settings.KAFKA_BROKERS or not kafka_settings.KAFKA_TOPIC:
            logger.warning(
                "system_event.kafka_not_configured event_type=%s config_type=%s brokers_configured=%s "
                "topic_configured=%s",
                event_type,
                config_type,
                bool(kafka_settings.KAFKA_BROKERS),
                bool(kafka_settings.KAFKA_TOPIC),
            )
            return None
        if self._producer is None:
            logger.info(
                "system_event.kafka_producer_start topic=%s brokers_configured=%s client_id_configured=%s",
                kafka_settings.KAFKA_TOPIC,
                bool(kafka_settings.KAFKA_BROKERS),
                bool(kafka_settings.KAFKA_CLIENT_ID),
            )
            self._producer = self._producer_factory(
                bootstrap_servers=kafka_settings.KAFKA_BROKERS,
                client_id=kafka_settings.KAFKA_CLIENT_ID or None,
                max_request_size=kafka_settings.KAFKA_MAX_REQUEST_SIZE,
            )
            try:
                await asyncio.wait_for(self._producer.start(), timeout=_KAFKA_OPERATION_TIMEOUT_SEC)
            except Exception:
                await self._drop_producer()
                logger.exception(
                    "system_event.kafka_producer_start_failed event_type=%s config_type=%s topic=%s timeout_sec=%s",
                    event_type,
                    config_type,
                    kafka_settings.KAFKA_TOPIC,
                    _KAFKA_OPERATION_TIMEOUT_SEC,
                )
                return None
            logger.info(
                "system_event.kafka_producer_started topic=%s client_id_configured=%s",
                kafka_settings.KAFKA_TOPIC,
                bool(kafka_settings.KAFKA_CLIENT_ID),
            )
        return self._producer

    async def _drop_producer(self) -> None:
        """Сбросить producer после ошибки, не раскрывая наружу ошибку закрытия."""
        producer = self._producer
        self._producer = None
        if producer is None:
            return
        try:
            await producer.stop()
        except Exception:
            logger.debug("system_event.kafka_drop_failed", exc_info=True)


def _event_type(event: dict[str, Any]) -> str:
    """Безопасно извлечь event-type для diagnostics без логирования payload."""
    value = event.get("event-type")
    return value if isinstance(value, str) else ""


def _config_type(event: dict[str, Any]) -> str:
    """Безопасно извлечь config-type из первого payload-элемента."""
    item = _first_payload_item(event)
    value = item.get("config-type") if item is not None else None
    return value if isinstance(value, str) else ""


def _session_id(event: dict[str, Any]) -> str:
    """Безопасно извлечь sandbox-session-id, не раскрывая остальные attributes."""
    item = _first_payload_item(event)
    attributes = item.get("attributes") if item is not None else None
    if not isinstance(attributes, dict):
        return ""
    value = attributes.get("sandbox-session-id")
    return value if isinstance(value, str) else ""


def _first_payload_item(event: dict[str, Any]) -> dict[str, Any] | None:
    """Вернуть первый объект payload, если он есть и имеет ожидаемый тип."""
    payload = event.get("payload")
    if not isinstance(payload, list) or not payload:
        return None
    item = payload[0]
    return item if isinstance(item, dict) else None


system_event_publisher = BestEffortKafkaSystemEventPublisher()
