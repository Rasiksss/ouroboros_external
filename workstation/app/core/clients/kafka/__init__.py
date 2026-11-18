"""Kafka client: producer + consumer in one class.

Pattern:
- Module-level instance (`kafka_client`).
- `register(topic, handler)` before `start()` — handlers run inside `consume_forever()`.
- `publish(topic, payload)` to send messages.
- Settings live in `config.py` (this package).

When `KAFKA_ENABLED=False` the client is fully inert: `start()` is a no-op,
`publish()` raises, `consume_forever()` exits immediately. Tests that need
Kafka behaviour should mock `kafka_client.publish` / the consumer loop.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRecord
from aiokafka.structs import RecordMetadata

from app.core.clients.kafka.config import settings
from app.utils.context import log_context

logger = logging.getLogger(__name__)

Handler = Callable[[ConsumerRecord[bytes, bytes]], Awaitable[None]]


class KafkaClient:
    """Producer + consumer.

    Lifecycle:
        kafka_client.register("my.topic", handler)
        await kafka_client.start()                              # in lifespan
        task = asyncio.create_task(kafka_client.consume_forever())
        ...
        task.cancel()
        await kafka_client.stop()
    """

    def __init__(self) -> None:
        self._producer: AIOKafkaProducer | None = None
        self._consumer: AIOKafkaConsumer | None = None
        self._handlers: dict[str, Handler] = {}

    def register(self, topic: str, handler: Handler) -> None:
        if topic in self._handlers:
            raise ValueError(f"Handler for topic {topic!r} is already registered")
        self._handlers[topic] = handler

    def has_handlers(self) -> bool:
        return bool(self._handlers)

    async def start(self) -> None:
        if not settings.KAFKA_ENABLED:
            logger.info("Kafka disabled, skipping client start")
            return

        if self._producer is None:
            logger.info("Starting Kafka producer bootstrap=%s", settings.KAFKA_BOOTSTRAP_SERVERS)
            self._producer = AIOKafkaProducer(
                bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
                client_id=settings.KAFKA_CLIENT_ID or None,
                max_request_size=settings.KAFKA_MAX_REQUEST_SIZE,
            )
            await self._producer.start()

        if settings.KAFKA_CONSUMER_ENABLED and self._handlers and self._consumer is None:
            logger.info(
                "Starting Kafka consumer topics=%s group_id=%s",
                list(self._handlers),
                settings.KAFKA_GROUP_ID,
            )
            self._consumer = AIOKafkaConsumer(
                *self._handlers.keys(),
                bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
                group_id=settings.KAFKA_GROUP_ID or None,
                client_id=settings.KAFKA_CLIENT_ID or None,
                auto_offset_reset=settings.KAFKA_OFFSET_RESET_STRATEGY,
                enable_auto_commit=True,
            )
            await self._consumer.start()

    async def stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def close(self) -> None:
        await self.stop()

    async def publish(
        self,
        topic: str,
        payload: bytes | str | dict[str, Any] | list[Any],
        *,
        key: bytes | None = None,
    ) -> None:
        if not settings.KAFKA_ENABLED:
            raise RuntimeError("Kafka is disabled (KAFKA_ENABLED=false)")
        if self._producer is None:
            raise RuntimeError("Kafka producer is not started")

        started = time.perf_counter()
        kafka_logging_attributes = {
            "kafka.to.topic": topic,
            "kafka.to.key": key.decode("utf-8", errors="replace") if key is not None else None,
            "kafka.to.broker": settings.KAFKA_BOOTSTRAP_SERVERS,
        }

        logger.debug(
            "publishing KafkaClient",
            extra={
                "event.category": "kafka",
                "event.action": "publish",
                "app.operation": "KafkaClient.publish",
                "event.outcome": "unknown",
                "app.status": "accepted",
                **kafka_logging_attributes,
            },
        )
        try:
            metadata: RecordMetadata = await self._producer.send_and_wait(
                topic, value=self._serialize(payload), key=key
            )
            kafka_logging_attributes.update(
                {
                    "kafka.to.partition": metadata.partition,
                    "kafka.to.offset": metadata.offset,
                }
            )
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                "KafkaClient.publish failed",
                extra={
                    "event.category": "kafka",
                    "event.action": "publish",
                    "app.operation": "KafkaClient.publish",
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(e).__name__,
                    "error.message": str(e),
                    "error.stack_trace": traceback.format_exc(),
                    **kafka_logging_attributes,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "KafkaClient.publish success",
                extra={
                    "event.category": "kafka",
                    "event.action": "publish",
                    "app.operation": "KafkaClient.publish",
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **kafka_logging_attributes,
                },
            )

    async def consume_forever(self) -> None:
        """Long-running loop. Spawn via `asyncio.create_task(...)` in lifespan."""
        if not settings.KAFKA_ENABLED or not settings.KAFKA_CONSUMER_ENABLED:
            logger.info("Kafka consumer disabled, skipping consume loop")
            return
        if not self._handlers:
            return
        if self._consumer is None:
            raise RuntimeError("Kafka consumer is not started")

        async for msg in self._consumer:
            await self._dispatch(msg)

    async def _dispatch(self, msg: ConsumerRecord[bytes, bytes]) -> None:
        msg_logging_attributes = {
            "kafka.from.topic": msg.topic,
            "kafka.from.key": msg.key.decode("utf-8", errors="replace") if msg.key else "",
            "kafka.from.partition": msg.partition,
            "kafka.from.offset": msg.offset,
            "kafka.from.broker": settings.KAFKA_BOOTSTRAP_SERVERS,
            "kafka.consumer.group": settings.KAFKA_GROUP_ID,
        }
        with log_context(**msg_logging_attributes):
            handler = self._handlers.get(msg.topic)
            if handler is None:
                return
            try:
                await handler(msg)
            except Exception:
                logger.exception("Unhandled error processing topic=%s offset=%s", msg.topic, msg.offset)

    @staticmethod
    def _serialize(payload: bytes | str | dict[str, Any] | list[Any]) -> bytes:
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, str):
            return payload.encode("utf-8")
        return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    @staticmethod
    def decode_json(record: ConsumerRecord[bytes, bytes]) -> Any:
        """Helper for handlers: pull JSON out of a `ConsumerRecord`."""
        return json.loads(record.value.decode("utf-8"))


kafka_client = KafkaClient()
