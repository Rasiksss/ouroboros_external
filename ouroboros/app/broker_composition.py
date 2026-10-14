"""Which broker backend this process composes, resolved from settings at the boundary.

A module the composition roots call, in the shape of `ouroboros/app/transport_wiring.py`,
rather than a block inside a lifespan: the gateway is built before the app that uses it, and
more than one root needs the same decision.

`transport/` and `background_memory/` must not import this — a transport that could reach
the resolver could reach a backend, which is the dependency the seam exists to prevent.

One selector, `OUROBOROS_BROKER_BACKEND`, decides for the whole broker, so a deployment
running one seam on Redis and another in process is not expressible. It never falls back:
a fallback is indistinguishable from success at startup and shows up later as a deployment
of N instances each holding its own queue.
"""

from __future__ import annotations

import threading
import uuid

import redis

from background_memory.infra.events import memory_event_publisher_factory
from background_memory.infra.events.inmemory import MAX_PENDING_EVENTS, InMemoryEventConsumer, MemoryEventQueue
from background_memory.infra.events.redis_transport import (
    REDIS_MAX_STREAM_ENTRIES,
    RedisMemoryEventConsumer,
    RedisMemoryEventPublisher,
)
from background_memory.interfaces import IMemoryEventConsumer, IMemoryEventPublisher
from infra.redis_client import (
    BROKER_CONNECT_TIMEOUT_SEC,
    BROKER_SOCKET_TIMEOUT_SEC,
    async_redis_client as build_async_redis_client,
    key_prefix,
    redis_client as build_redis_client,
)
from new_core.coordination.interfaces import ITaskStopConsumer, ITaskStopPublisher
from ouroboros.agent_events.inmemory import create_agent_event_transport
from ouroboros.agent_events.interfaces import IAgentEventConsumer, IAgentEventPublisher
from ouroboros.agent_events.redis_transport import RedisAgentEventConsumer, RedisAgentEventPublisher
from ouroboros.app.task_stop_notifications import (
    RedisTaskStopConsumer,
    RedisTaskStopPublisher,
    create_task_stop_transport,
)
from ouroboros.storage.settings.store import load_settings
from transport.infra.inmemory import InProcessInbox, InProcessMessageGateway, InProcessOutbox
from transport.infra.redis_gateway import RedisInbox, RedisMessageGateway, RedisOutbox
from transport.interfaces import IMessageGateway

#: The consumer group the supervisor tier drains the inbound stream through. One group for
#: the tier, one consumer name per instance within it — which is what makes the instances
#: compete for envelopes instead of each receiving every one.
INBOX_GROUP = "supervisor"

#: The agent-event stream's one logical reader, and the group it drains through. Named
#: after the stream so an operator listing groups sees which stream a group belongs to.
AGENT_EVENT_STREAM = "agent-events"

#: The memory-event stream, and the one group the background-memory tier drains it through
#: — one group for the tier, so an event is handled once however many instances run.
MEMORY_EVENT_STREAM = "memory-events"
MEMORY_EVENT_GROUP = "background-memory"

#: The tier's consumer name inside that group. Fixed for Redis, where the backend
#: acknowledges before it dispatches, so a consumer holds nothing to recover and a minted
#: name would leak an entry per restart into a group nothing prunes. Kafka has no such
#: register and identifies members itself, so its arm suffixes this with the instance.
MEMORY_EVENT_CONSUMER = "background-memory"
TASK_STOP_STREAM = "task-stops"
TASK_STOP_PUBLISH_TIMEOUT_SEC = 0.25

INBOUND_STREAM = "inbound"
OUTBOUND_STREAM = "outbound"

_READ_RETRIES = 3


class UnhandledBrokerBackend(RuntimeError):
    def __init__(self, backend: str) -> None:
        super().__init__(
            f"OUROBOROS_BROKER_BACKEND={backend!r} names a broker backend this composition has no arm for: "
            "nothing was composed, and nothing fell back to the in-process queues."
        )


def resolve_broker_backend() -> str:
    """The backend this process was configured to compose.

    Under the mode the in-process backend is refused rather than composed. Only a component
    tier imports this module — `server.py` names the in-process backend directly and is one
    process, so the selector does not reach it — and a tier holding a private queue has its
    writer and its reader in different processes: every message is accepted, none is ever
    processed, and every health probe answers 200 throughout. That is the one
    misconfiguration in this mode that reports nothing wrong.
    """
    settings = load_settings()
    backend = str(settings.OUROBOROS_BROKER_BACKEND)
    match backend:
        case "inprocess":
            if bool(settings.OUROBOROS_MULTIUSER):
                raise RuntimeError(
                    f"OUROBOROS_MULTIUSER=true with OUROBOROS_BROKER_BACKEND={backend!r}: this process is a "
                    "component tier, so an in-process queue puts its writer and its reader in different "
                    "processes — every message is accepted and none is ever processed, while health keeps "
                    "reporting 200. Set OUROBOROS_BROKER_BACKEND=redis (with OUROBOROS_REDIS_URL) or kafka (with "
                    "OUROBOROS_KAFKA_BOOTSTRAP_SERVERS), or run the single-process runtime `ouroboros-server`, "
                    "which composes the in-process backend directly and does not read this selector."
                )
            return backend
        case "redis":
            return backend
        case "kafka":
            return backend
        case _:
            raise UnhandledBrokerBackend(backend)


def initialize_broker_backend() -> None:
    match backend := resolve_broker_backend():
        case "inprocess" | "redis":
            return
        case "kafka":
            from infra.kafka_client import KafkaConnection, kafka_client_factory

            kafka_client_factory.initialize(KafkaConnection.from_settings(load_settings()))
        case _:
            raise UnhandledBrokerBackend(backend)


def stream_key(suffix: str) -> str:
    """The one place a broker key is formed, so the prefix is derived once."""
    return f"{key_prefix()}:{suffix}"


def redis_client(*, retries: int) -> redis.Redis:
    return build_redis_client(
        retries=retries,
        connect_timeout=BROKER_CONNECT_TIMEOUT_SEC,
        socket_timeout=BROKER_SOCKET_TIMEOUT_SEC,
    )


def async_redis_client(*, retries: int) -> redis.asyncio.Redis:
    """The same cluster description bound to redis-py's asyncio client.

    One description, two transports — the pairing `infra/redis_client.py` already owns. The
    memory-event consumer is the one reader on this side of the seam.
    """
    return build_async_redis_client(
        retries=retries,
        connect_timeout=BROKER_CONNECT_TIMEOUT_SEC,
        socket_timeout=BROKER_SOCKET_TIMEOUT_SEC,
    )


def build_message_gateway(*, inbox_consumer: str) -> IMessageGateway:
    """The message gateway the configuration names, for one composition root.

    `inbox_consumer` is this instance's own name in the inbound consumer group and has no
    default: two instances sharing one would share a pending list, and each would read the
    other's in-flight envelope as its own redelivery.
    """
    if not str(inbox_consumer or "").strip():
        raise ValueError("a message gateway requires this instance's own inbox consumer name")
    match backend := resolve_broker_backend():
        case "inprocess":
            return InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
        case "redis":
            reads = redis_client(retries=_READ_RETRIES)
            appends = redis_client(retries=0)
            return RedisMessageGateway(
                inbox=RedisInbox(
                    client=reads,
                    append_client=appends,
                    stream=stream_key(INBOUND_STREAM),
                    group=INBOX_GROUP,
                    consumer=inbox_consumer,
                ),
                outbox=RedisOutbox(client=reads, append_client=appends, stream=stream_key(OUTBOUND_STREAM)),
                async_client=async_redis_client(retries=0),
            )
        case "kafka":
            from infra.kafka_client import kafka_client_factory
            from transport.infra.kafka_gateway import (
                KAFKA_INBOX_MAX_POLL_INTERVAL_MS,
                KafkaInbox,
                KafkaMessageGateway,
                KafkaOutbox,
            )

            connection = kafka_client_factory.connection
            return KafkaMessageGateway(
                clients=kafka_client_factory,
                inbox=KafkaInbox(
                    clients=kafka_client_factory,
                    topic=connection.topic(INBOUND_STREAM),
                    group_id=connection.group_id(INBOUND_STREAM, INBOX_GROUP),
                    client_id=inbox_consumer,
                    max_poll_interval_ms=KAFKA_INBOX_MAX_POLL_INTERVAL_MS,
                ),
                outbox=KafkaOutbox(clients=kafka_client_factory, topic=connection.topic(OUTBOUND_STREAM)),
            )
        case _:
            raise UnhandledBrokerBackend(backend)


#: The two halves of the one in-process transport this module hands out, built on first
#: use. A root that resolves both in one process must get a connected pair rather than two
#: unrelated queues, and the composition root is the only place allowed to name both
#: (`ouroboros/agent_events/inmemory.py`).
_INPROCESS_AGENT_EVENTS: tuple[IAgentEventPublisher, IAgentEventConsumer] | None = None

#: Under a lock, because a check-then-set on the global is exactly the failure it exists to
#: prevent: two roots resolving their halves on two threads would each build a pair and
#: keep one half of it, and the publisher would feed a queue nobody reads.
_INPROCESS_AGENT_EVENTS_LOCK = threading.Lock()


def _inprocess_agent_events() -> tuple[IAgentEventPublisher, IAgentEventConsumer]:
    global _INPROCESS_AGENT_EVENTS
    with _INPROCESS_AGENT_EVENTS_LOCK:
        if _INPROCESS_AGENT_EVENTS is None:
            _INPROCESS_AGENT_EVENTS = create_agent_event_transport()
        return _INPROCESS_AGENT_EVENTS


async def start_message_gateway(gateway: IMessageGateway) -> None:
    match resolve_broker_backend():
        case "kafka":
            from transport.infra.kafka_gateway import KafkaMessageGateway

            if isinstance(gateway, KafkaMessageGateway):
                await gateway.start()


def resolve_agent_event_publisher() -> IAgentEventPublisher:
    """What a run in this process reports through.

    The agent-runtime root resolves the publisher alone.
    """
    match backend := resolve_broker_backend():
        case "inprocess":
            return _inprocess_agent_events()[0]
        case "redis":
            return RedisAgentEventPublisher(client=redis_client(retries=0), stream=stream_key(AGENT_EVENT_STREAM))
        case "kafka":
            from infra.kafka_client import kafka_client_factory
            from ouroboros.agent_events.kafka_transport import KafkaAgentEventPublisher

            return KafkaAgentEventPublisher(
                clients=kafka_client_factory, topic=kafka_client_factory.connection.topic(AGENT_EVENT_STREAM)
            )
        case _:
            raise UnhandledBrokerBackend(backend)


def resolve_agent_event_consumer() -> IAgentEventConsumer:
    """What the supervisor tier in this process reads reports from.

    **No command retry**, for the reason `resolve_memory_event_consumer` records below, and
    one degree worse here: this reader takes `XREADGROUP >` with `NOACK`, so a retry after a
    lost reply returns the *next* entries and the first batch is in no pending list at all —
    no `XPENDING` and no `XAUTOCLAIM` can reach it, and a non-droppable `OutboundText` is
    the message the owner is waiting for. A dropped connection is recovered by `poll`
    instead, which records the outage and returns `None` rather than raising, so a reaped
    socket still does not crash the supervisor's tick.
    """
    match backend := resolve_broker_backend():
        case "inprocess":
            return _inprocess_agent_events()[1]
        case "redis":
            stream = stream_key(AGENT_EVENT_STREAM)
            return RedisAgentEventConsumer(
                client=redis_client(retries=0),
                stream=stream,
                group=f"{stream}:{INBOX_GROUP}",
            )
        case "kafka":
            from infra.kafka_client import kafka_client_factory
            from ouroboros.agent_events.kafka_transport import (
                KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS,
                KafkaAgentEventConsumer,
            )

            connection = kafka_client_factory.connection
            return KafkaAgentEventConsumer(
                clients=kafka_client_factory,
                topic=connection.topic(AGENT_EVENT_STREAM),
                group_id=connection.group_id(AGENT_EVENT_STREAM, INBOX_GROUP),
                client_id=f"{INBOX_GROUP}-{uuid.uuid4().hex}",
                max_poll_interval_ms=KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS,
            )
        case _:
            raise UnhandledBrokerBackend(backend)


_INPROCESS_TASK_STOPS: tuple[ITaskStopPublisher, ITaskStopConsumer] | None = None
_INPROCESS_TASK_STOPS_LOCK = threading.Lock()


def _inprocess_task_stops() -> tuple[ITaskStopPublisher, ITaskStopConsumer]:
    global _INPROCESS_TASK_STOPS
    with _INPROCESS_TASK_STOPS_LOCK:
        if _INPROCESS_TASK_STOPS is None:
            _INPROCESS_TASK_STOPS = create_task_stop_transport()
        return _INPROCESS_TASK_STOPS


def resolve_task_stop_publisher() -> ITaskStopPublisher:
    match backend := resolve_broker_backend():
        case "inprocess":
            return _inprocess_task_stops()[0]
        case "redis":
            return RedisTaskStopPublisher(
                client=build_redis_client(
                    retries=0,
                    connect_timeout=TASK_STOP_PUBLISH_TIMEOUT_SEC,
                    socket_timeout=TASK_STOP_PUBLISH_TIMEOUT_SEC,
                ),
                stream=stream_key(TASK_STOP_STREAM),
            )
        case "kafka":
            from infra.kafka_client import kafka_client_factory
            from ouroboros.app.task_stop_kafka import KafkaTaskStopPublisher

            return KafkaTaskStopPublisher(
                clients=kafka_client_factory, topic=kafka_client_factory.connection.topic(TASK_STOP_STREAM)
            )
        case _:
            raise UnhandledBrokerBackend(backend)


def resolve_task_stop_consumer() -> ITaskStopConsumer:
    match backend := resolve_broker_backend():
        case "inprocess":
            return _inprocess_task_stops()[1]
        case "redis":
            return RedisTaskStopConsumer(client=redis_client(retries=0), stream=stream_key(TASK_STOP_STREAM))
        case "kafka":
            from infra.kafka_client import kafka_client_factory
            from ouroboros.app.task_stop_kafka import KafkaTaskStopConsumer

            return KafkaTaskStopConsumer(
                clients=kafka_client_factory, topic=kafka_client_factory.connection.topic(TASK_STOP_STREAM)
            )
        case _:
            raise UnhandledBrokerBackend(backend)


#: The one in-process memory-event queue this module hands halves of, built on first use.
#: A root resolving both halves in one process must get a connected pair rather than two
#: unrelated queues — the same failure the agent-event pair above exists to prevent.
#:
#: It is this module's queue, not the deployment's: `server.py` composes its own pair
#: directly (`initialize_inmemory` and `InMemoryEventConsumer` over one queue of its own) and
#: never calls the resolvers here, so exactly one of the two arrangements is live in any
#: process. A process that mixed them — one half from `server.py`, the other from here —
#: would hold two unconnected queues, which is why neither entry point reaches into the
#: other's.
_INPROCESS_MEMORY_QUEUE: MemoryEventQueue | None = None

#: Under a lock, because a check-then-set on the global is the race it exists to prevent.
_INPROCESS_MEMORY_QUEUE_LOCK = threading.Lock()


def _inprocess_memory_queue() -> MemoryEventQueue:
    global _INPROCESS_MEMORY_QUEUE
    with _INPROCESS_MEMORY_QUEUE_LOCK:
        if _INPROCESS_MEMORY_QUEUE is None:
            _INPROCESS_MEMORY_QUEUE = MemoryEventQueue(MAX_PENDING_EVENTS)
        return _INPROCESS_MEMORY_QUEUE


def install_memory_event_publisher() -> IMemoryEventPublisher:
    """Install what this process reports completed work through, and hand it back.

    Installed rather than returned alone: the producers are a task graph and a chat writer
    that read the process-local holder (`background_memory/infra/events/__init__.py`), and
    which of its two initializers to call is exactly the decision this module owns. A root
    calls this **before** it composes anything that reads the holder — a collaborator handed
    the publisher at construction holds whatever was installed at that moment.

    A process that only consumes these events calls nothing: its holder keeps the null
    publisher, because installing one there would only mean talking to itself.

    **Under the default selector this installs a publisher whose reader is not there**, in a
    process that composes no memory loop: the queue fills to `MAX_PENDING_EVENTS` and every
    event after that is a `queue full (cap=32)` warning, with those 32 held for the life of
    the process. That is the same trade the agent-event publisher already takes in that root
    and it is declared rather than hidden — the default selector composes a profile nothing
    is deployed on, and the alternative (leaving the holder null there) would make the
    default answer a different question from the broker one.
    """
    match backend := resolve_broker_backend():
        case "inprocess":
            memory_event_publisher_factory.initialize_inmemory(_inprocess_memory_queue())
        case "redis":
            memory_event_publisher_factory.initialize_broker(
                RedisMemoryEventPublisher(
                    client=redis_client(retries=0),
                    stream=stream_key(MEMORY_EVENT_STREAM),
                    max_entries=REDIS_MAX_STREAM_ENTRIES,
                )
            )
        case "kafka":
            from background_memory.infra.events.kafka_transport import KafkaMemoryEventPublisher
            from infra.kafka_client import kafka_client_factory

            memory_event_publisher_factory.initialize_broker(
                KafkaMemoryEventPublisher(
                    clients=kafka_client_factory, topic=kafka_client_factory.connection.topic(MEMORY_EVENT_STREAM)
                )
            )
        case _:
            raise UnhandledBrokerBackend(backend)
    return memory_event_publisher_factory.memory_publisher


def resolve_memory_event_consumer() -> IMemoryEventConsumer:
    """What the background-memory tier in this process reads its work from.

    **No command retry.** ``XREADGROUP >`` advances the group's last-delivered id, so a retry
    after a lost reply returns *different* entries and strands the first batch in the pending
    list — an event lost with nothing failing. A dropped connection is recovered by ``poll``
    instead, which records the outage and returns ``None`` rather than raising.

    Under the default the consumer is the in-process one, whose queue has no writer in a
    process that composes no producer — the root says so at startup rather than this module
    refusing to build it, because a builder that needed a live broker could not be tested
    without one.
    """
    match backend := resolve_broker_backend():
        case "inprocess":
            return InMemoryEventConsumer(_inprocess_memory_queue())
        case "redis":
            stream = stream_key(MEMORY_EVENT_STREAM)
            return RedisMemoryEventConsumer(
                client=async_redis_client(retries=0),
                stream=stream,
                group=f"{stream}:{MEMORY_EVENT_GROUP}",
                consumer=MEMORY_EVENT_CONSUMER,
            )
        case "kafka":
            from background_memory.infra.events.kafka_transport import (
                KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
                KafkaMemoryEventConsumer,
            )
            from infra.kafka_client import kafka_client_factory

            connection = kafka_client_factory.connection
            return KafkaMemoryEventConsumer(
                clients=kafka_client_factory,
                topic=connection.topic(MEMORY_EVENT_STREAM),
                group_id=connection.group_id(MEMORY_EVENT_STREAM, MEMORY_EVENT_GROUP),
                client_id=f"{MEMORY_EVENT_CONSUMER}-{uuid.uuid4().hex}",
                max_poll_interval_ms=KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
            )
        case _:
            raise UnhandledBrokerBackend(backend)


__all__ = [
    "AGENT_EVENT_STREAM",
    "INBOUND_STREAM",
    "INBOX_GROUP",
    "MEMORY_EVENT_CONSUMER",
    "MEMORY_EVENT_GROUP",
    "MEMORY_EVENT_STREAM",
    "OUTBOUND_STREAM",
    "TASK_STOP_PUBLISH_TIMEOUT_SEC",
    "TASK_STOP_STREAM",
    "UnhandledBrokerBackend",
    "build_message_gateway",
    "initialize_broker_backend",
    "install_memory_event_publisher",
    "redis_client",
    "resolve_agent_event_consumer",
    "resolve_agent_event_publisher",
    "resolve_broker_backend",
    "resolve_memory_event_consumer",
    "resolve_task_stop_consumer",
    "resolve_task_stop_publisher",
    "stream_key",
]
