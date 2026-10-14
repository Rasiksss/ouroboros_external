from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import SecretStr

if TYPE_CHECKING:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    from kafka import KafkaConsumer, KafkaProducer

    from ouroboros.storage.settings.schema import AppSettings

log = logging.getLogger(__name__)

KAFKA_POLL_SLICE_MS = 250
KAFKA_ZERO_TIMEOUT_FETCH_MS = 100
KAFKA_SEND_TIMEOUT_SEC = 10.0
KAFKA_CLOSE_TIMEOUT_MS = 1_000

#: How long joining a group, or leaving one, may take before the caller gives up and treats the
#: cluster as silent. `AIOKafkaConsumer.start` waits for the topic's metadata inside its own
#: `request_timeout_ms`, which a group consumer derives from the poll interval and which is
#: minutes for a long one — and topics belong to the infrastructure, so a reader that starts
#: before its topic exists would spend those minutes on the loop it was meant to leave free.
KAFKA_JOIN_TIMEOUT_SEC = 30.0

_PRODUCER_MAX_BLOCK_MS = 5_000
_PRODUCER_REQUEST_TIMEOUT_MS = 10_000
_PRODUCER_DELIVERY_TIMEOUT_MS = int(KAFKA_SEND_TIMEOUT_SEC * 1_000)
_GROUP_SESSION_TIMEOUT_MS = 10_000
_GROUP_HEARTBEAT_INTERVAL_MS = 3_000
_GROUP_REQUEST_TIMEOUT_MARGIN_MS = 5_000
_IDLE_MARGIN_MS = 60_000
_BROKER_CONNECTIONS_MAX_IDLE_DEFAULT_MS = 600_000
_READER_REQUEST_TIMEOUT_MS = 5_000
_RECONNECT_BACKOFF_MAX_MS = 5_000

KAFKA_MAX_POLL_INTERVAL_CEILING_MS = 300_000

_API_VERSION_FORMAT = re.compile(r"^(\d+)\.(\d+)$")

SecurityProtocol = Literal["PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"]
SaslMechanism = Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"]
_SASL_PROTOCOLS = frozenset({"SASL_PLAINTEXT", "SASL_SSL"})
_SSL_PROTOCOLS = frozenset({"SSL", "SASL_SSL"})


def parse_kafka_bootstrap_servers(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def kafka_unanswered_errors() -> tuple[type[Exception], ...]:
    from kafka.errors import KafkaError

    return (KafkaError, AssertionError, OSError, ValueError)


def aiokafka_unanswered_errors() -> tuple[type[Exception], ...]:
    """What a read may absorb as "the cluster did not answer" on the asynchronous client.

    `aiokafka` raises its own hierarchy, not `kafka-python`'s, and one of the two shapes the
    synchronous tuple carries is deliberately absent: `ConsumerStoppedError` does not derive
    from `KafkaError` and is not listed, because reading a consumer nobody started is a
    programming error and not an outage.
    """
    from aiokafka.errors import KafkaError

    return (KafkaError, AssertionError, OSError, ValueError)


def kafka_fetch_ms(deadline: float, floor_ms: int = 0) -> int:
    return max(0, floor_ms, min(KAFKA_POLL_SLICE_MS, math.ceil((deadline - time.monotonic()) * 1_000)))


def close_kafka_consumer(consumer: KafkaConsumer, *, timeout_ms: int = KAFKA_CLOSE_TIMEOUT_MS) -> Exception | None:
    try:
        consumer.close(autocommit=False, timeout_ms=timeout_ms)
    except kafka_unanswered_errors() as error:
        return error
    return None


async def close_aiokafka_consumer(consumer: AIOKafkaConsumer) -> Exception | None:
    """`stop` takes neither an autocommit flag nor a timeout, so the bound is applied here."""
    try:
        await asyncio.wait_for(consumer.stop(), timeout=KAFKA_JOIN_TIMEOUT_SEC)
    except aiokafka_unanswered_errors() as error:
        return error
    return None


async def aiokafka_group_member(
    clients: IKafkaClientFactory,
    *,
    topic: str,
    group_id: str,
    client_id: str,
    auto_offset_reset: KafkaOffsetReset,
    max_poll_interval_ms: int,
) -> AIOKafkaConsumer:
    """Build, subscribe and start as one step, closing the client when any of the three fails."""
    consumer = clients.async_group_consumer(
        group_id=group_id,
        client_id=client_id,
        auto_offset_reset=auto_offset_reset,
        max_poll_interval_ms=max_poll_interval_ms,
    )
    try:
        consumer.subscribe([topic])
        await asyncio.wait_for(consumer.start(), timeout=KAFKA_JOIN_TIMEOUT_SEC)
    except BaseException:
        failure = await close_aiokafka_consumer(consumer)
        if failure is not None:
            log.warning("Could not close the group consumer of %r that failed to start", topic, exc_info=failure)
        raise
    return consumer


def refuse_unusable_poll_interval(max_poll_interval_ms: int) -> None:
    if not 0 < max_poll_interval_ms <= KAFKA_MAX_POLL_INTERVAL_CEILING_MS:
        raise RuntimeError(
            f"max_poll_interval_ms={max_poll_interval_ms} is outside (0, {KAFKA_MAX_POLL_INTERVAL_CEILING_MS}]: "
            "a group consumer's request timeout is derived from it and is the socket timeout of every request "
            "it issues, so a longer one leaves a half-open connection unnoticed for longer than the brokers' "
            f"own connections.max.idle.ms ({_BROKER_CONNECTIONS_MAX_IDLE_DEFAULT_MS} ms by default)."
        )


def kafka_group_member(
    clients: IKafkaClientFactory,
    *,
    topic: str,
    group_id: str,
    client_id: str,
    auto_offset_reset: KafkaOffsetReset,
    max_poll_interval_ms: int,
) -> KafkaConsumer:
    consumer = clients.group_consumer(
        group_id=group_id,
        client_id=client_id,
        auto_offset_reset=auto_offset_reset,
        max_poll_interval_ms=max_poll_interval_ms,
    )
    try:
        consumer.subscribe([topic])
    except BaseException:
        failure = close_kafka_consumer(consumer)
        if failure is not None:
            log.warning("Could not close the group consumer of %r that failed to subscribe", topic, exc_info=failure)
        raise
    return consumer


class KafkaOffsetReset(StrEnum):
    EARLIEST = "earliest"
    LATEST = "latest"


@dataclass(frozen=True, kw_only=True)
class KafkaConnection:
    bootstrap_servers: tuple[str, ...]
    topic_prefix: str
    security_protocol: SecurityProtocol
    sasl_mechanism: SaslMechanism
    sasl_username: str
    sasl_password: SecretStr
    ssl_ca_path: str
    ssl_cert_path: str
    ssl_key_path: str
    api_version: tuple[int, ...]
    max_message_bytes: int

    @classmethod
    def from_settings(cls, settings: AppSettings) -> KafkaConnection:
        return cls(
            bootstrap_servers=tuple(parse_kafka_bootstrap_servers(settings.OUROBOROS_KAFKA_BOOTSTRAP_SERVERS)),
            topic_prefix=str(settings.OUROBOROS_KAFKA_TOPIC_PREFIX or "").strip(),
            security_protocol=settings.OUROBOROS_KAFKA_SECURITY_PROTOCOL,
            sasl_mechanism=settings.OUROBOROS_KAFKA_SASL_MECHANISM,
            sasl_username=str(settings.OUROBOROS_KAFKA_SASL_USERNAME or ""),
            sasl_password=settings.OUROBOROS_KAFKA_SASL_PASSWORD,
            ssl_ca_path=str(settings.OUROBOROS_KAFKA_SSL_CA_PATH or "").strip(),
            ssl_cert_path=str(settings.OUROBOROS_KAFKA_SSL_CERT_PATH or "").strip(),
            ssl_key_path=str(settings.OUROBOROS_KAFKA_SSL_KEY_PATH or "").strip(),
            api_version=cls._parsed_api_version(settings.OUROBOROS_KAFKA_API_VERSION),
            max_message_bytes=settings.OUROBOROS_KAFKA_MAX_MESSAGE_BYTES,
        )

    @staticmethod
    def _parsed_api_version(value: str) -> tuple[int, ...]:
        text = str(value or "").strip()
        if not text:
            raise RuntimeError(
                "OUROBOROS_KAFKA_API_VERSION is empty: pin the broker protocol version as MAJOR.MINOR, "
                "e.g. 3.9. A Kafka client built without one probes the broker on construction."
            )
        matched = _API_VERSION_FORMAT.match(text)
        if matched is None:
            raise RuntimeError("OUROBOROS_KAFKA_API_VERSION must be MAJOR.MINOR, e.g. 3.9; write 3.7.1 as 3.7")
        return (int(matched.group(1)), int(matched.group(2)))

    def __post_init__(self) -> None:
        if not self.bootstrap_servers:
            raise RuntimeError(
                "A Kafka connection needs OUROBOROS_KAFKA_BOOTSTRAP_SERVERS, which is empty. Set it to a "
                "comma-separated host:port list, or select another OUROBOROS_BROKER_BACKEND: a broker "
                "nothing can reach is not a broker."
            )

    def topic(self, suffix: str) -> str:
        return f"{self.topic_prefix}.{suffix}"

    def group_id(self, topic_suffix: str, group: str) -> str:
        return f"{self.topic_prefix}.{topic_suffix}.{group}"


class IKafkaClientFactory(ABC):
    @abstractmethod
    def async_producer(self) -> AIOKafkaProducer:
        pass

    @abstractmethod
    def producer(self, *, topic: str) -> KafkaProducer:
        pass

    @abstractmethod
    def group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> KafkaConsumer:
        pass

    @abstractmethod
    def async_group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> AIOKafkaConsumer:
        pass

    @abstractmethod
    def partition_reader(self, *, auto_offset_reset: KafkaOffsetReset) -> KafkaConsumer:
        pass


class KafkaClientFactory(IKafkaClientFactory):
    def __init__(self) -> None:
        self._connection: KafkaConnection | None = None
        self._producers: dict[str, KafkaProducer] = {}
        self._lock = threading.Lock()

    def initialize(self, connection: KafkaConnection) -> None:
        with self._lock:
            if self._connection is not None and self._connection != connection:
                raise RuntimeError(
                    "KafkaClientFactory is already initialized for an earlier connection: it describes one cluster "
                    "for the whole process, or the clients built before and after this call would address two."
                )
            self._connection = connection

    @property
    def connection(self) -> KafkaConnection:
        if self._connection is None:
            raise RuntimeError("KafkaClientFactory not initialized: call initialize() first")
        return self._connection

    def producer(self, *, topic: str) -> KafkaProducer:
        connection = self.connection
        with self._lock:
            existing = self._producers.get(topic)
            if existing is not None:
                return existing
            from kafka import KafkaProducer

            producer = KafkaProducer(
                **self._common_kwargs(connection),
                acks="all",
                enable_idempotence=True,
                max_in_flight_requests_per_connection=1,
                max_block_ms=_PRODUCER_MAX_BLOCK_MS,
                request_timeout_ms=_PRODUCER_REQUEST_TIMEOUT_MS,
                delivery_timeout_ms=_PRODUCER_DELIVERY_TIMEOUT_MS,
                linger_ms=0,
                max_request_size=connection.max_message_bytes,
                compression_type="gzip",
            )
            self._producers[topic] = producer
            return producer

    def async_producer(self) -> AIOKafkaProducer:
        connection = self.connection
        # Construct only at async transport startup, on its event loop.
        from aiokafka import AIOKafkaProducer

        return AIOKafkaProducer(
            **self._async_common_kwargs(connection),
            acks="all",
            enable_idempotence=True,
            request_timeout_ms=_PRODUCER_REQUEST_TIMEOUT_MS,
            linger_ms=0,
            max_request_size=connection.max_message_bytes,
            compression_type="gzip",
        )

    def group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> KafkaConsumer:
        connection = self.connection
        refuse_unusable_poll_interval(max_poll_interval_ms)
        from kafka import KafkaConsumer

        request_timeout_ms = self._group_request_timeout_ms(max_poll_interval_ms)
        return KafkaConsumer(
            **self._common_kwargs(connection),
            **self._fetch_kwargs(connection),
            group_id=group_id,
            client_id=client_id,
            enable_auto_commit=False,
            auto_offset_reset=str(auto_offset_reset),
            max_poll_records=1,
            max_poll_interval_ms=max_poll_interval_ms,
            session_timeout_ms=_GROUP_SESSION_TIMEOUT_MS,
            heartbeat_interval_ms=_GROUP_HEARTBEAT_INTERVAL_MS,
            request_timeout_ms=request_timeout_ms,
            connections_max_idle_ms=request_timeout_ms + _IDLE_MARGIN_MS,
        )

    def async_group_consumer(
        self, *, group_id: str, client_id: str, auto_offset_reset: KafkaOffsetReset, max_poll_interval_ms: int
    ) -> AIOKafkaConsumer:
        connection = self.connection
        refuse_unusable_poll_interval(max_poll_interval_ms)
        from aiokafka import AIOKafkaConsumer

        request_timeout_ms = self._group_request_timeout_ms(max_poll_interval_ms)
        return AIOKafkaConsumer(
            **self._async_common_kwargs(connection),
            fetch_max_wait_ms=KAFKA_POLL_SLICE_MS,
            max_partition_fetch_bytes=connection.max_message_bytes,
            fetch_max_bytes=connection.max_message_bytes,
            check_crcs=True,
            group_id=group_id,
            client_id=client_id,
            enable_auto_commit=False,
            auto_offset_reset=str(auto_offset_reset),
            max_poll_records=1,
            max_poll_interval_ms=max_poll_interval_ms,
            rebalance_timeout_ms=max_poll_interval_ms,
            session_timeout_ms=_GROUP_SESSION_TIMEOUT_MS,
            heartbeat_interval_ms=_GROUP_HEARTBEAT_INTERVAL_MS,
            request_timeout_ms=request_timeout_ms,
            connections_max_idle_ms=request_timeout_ms + _IDLE_MARGIN_MS,
        )

    def partition_reader(self, *, auto_offset_reset: KafkaOffsetReset) -> KafkaConsumer:
        connection = self.connection
        from kafka import KafkaConsumer

        return KafkaConsumer(
            **self._common_kwargs(connection),
            **self._fetch_kwargs(connection),
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset=str(auto_offset_reset),
            max_poll_records=1,
            request_timeout_ms=_READER_REQUEST_TIMEOUT_MS,
        )

    @staticmethod
    def _group_request_timeout_ms(max_poll_interval_ms: int) -> int:
        return max(max_poll_interval_ms, _GROUP_SESSION_TIMEOUT_MS) + _GROUP_REQUEST_TIMEOUT_MARGIN_MS

    @staticmethod
    def _async_common_kwargs(connection: KafkaConnection) -> dict[str, Any]:
        """The same description, in the form this library takes it.

        Three differences, each read off the installed package and recorded in the change's
        design: the protocol version is a no-op since 0.13 and is not passed, TLS arrives as a
        context rather than as three paths, and there is no `reconnect_backoff_max_ms`.
        """
        kwargs: dict[str, Any] = {
            "bootstrap_servers": list(connection.bootstrap_servers),
            "security_protocol": connection.security_protocol,
        }
        if connection.security_protocol in _SASL_PROTOCOLS:
            kwargs["sasl_mechanism"] = connection.sasl_mechanism
            kwargs["sasl_plain_username"] = connection.sasl_username
            kwargs["sasl_plain_password"] = connection.sasl_password.get_secret_value()
        if connection.security_protocol in _SSL_PROTOCOLS:
            from aiokafka.helpers import create_ssl_context

            kwargs["ssl_context"] = create_ssl_context(
                cafile=connection.ssl_ca_path or None,
                certfile=connection.ssl_cert_path or None,
                keyfile=connection.ssl_key_path or None,
            )
        return kwargs

    @staticmethod
    def _common_kwargs(connection: KafkaConnection) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "bootstrap_servers": list(connection.bootstrap_servers),
            "security_protocol": connection.security_protocol,
            "api_version": connection.api_version,
            "reconnect_backoff_max_ms": _RECONNECT_BACKOFF_MAX_MS,
        }
        if connection.security_protocol in _SASL_PROTOCOLS:
            kwargs["sasl_mechanism"] = connection.sasl_mechanism
            kwargs["sasl_plain_username"] = connection.sasl_username
            kwargs["sasl_plain_password"] = connection.sasl_password.get_secret_value()
        if connection.security_protocol in _SSL_PROTOCOLS:
            kwargs["ssl_check_hostname"] = True
            for key, path in (
                ("ssl_cafile", connection.ssl_ca_path),
                ("ssl_certfile", connection.ssl_cert_path),
                ("ssl_keyfile", connection.ssl_key_path),
            ):
                if path:
                    kwargs[key] = path
        return kwargs

    @staticmethod
    def _fetch_kwargs(connection: KafkaConnection) -> dict[str, int | bool]:
        return {
            "fetch_max_wait_ms": KAFKA_POLL_SLICE_MS,
            "max_partition_fetch_bytes": connection.max_message_bytes,
            "fetch_max_bytes": connection.max_message_bytes,
            "check_crcs": True,
        }


kafka_client_factory = KafkaClientFactory()


__all__ = [
    "KAFKA_CLOSE_TIMEOUT_MS",
    "KAFKA_JOIN_TIMEOUT_SEC",
    "KAFKA_MAX_POLL_INTERVAL_CEILING_MS",
    "KAFKA_POLL_SLICE_MS",
    "KAFKA_SEND_TIMEOUT_SEC",
    "KAFKA_ZERO_TIMEOUT_FETCH_MS",
    "IKafkaClientFactory",
    "KafkaClientFactory",
    "KafkaConnection",
    "KafkaOffsetReset",
    "SaslMechanism",
    "SecurityProtocol",
    "aiokafka_group_member",
    "aiokafka_unanswered_errors",
    "close_aiokafka_consumer",
    "close_kafka_consumer",
    "kafka_client_factory",
    "kafka_fetch_ms",
    "kafka_group_member",
    "kafka_unanswered_errors",
    "parse_kafka_bootstrap_servers",
    "refuse_unusable_poll_interval",
]
