"""The one module that changes when background memory stops sharing a process.

``consumer`` is the *reading* half of the seam, resolved from the deployment's one broker
selector rather than constructed here:

* ``OUROBOROS_BROKER_BACKEND=redis`` gives the Redis consumer, reading the
  ``{prefix}:memory-events`` stream through the tier's one consumer group — the writing half
  lives in the processes that produce the events, which is the arrangement this component
  exists for.
* the default, ``inprocess``, gives the in-process consumer, whose queue's writing half would
  have to be in *this* process — which composes no producer, so the queue stays empty and
  this tier consumes nothing. The root says so at startup rather than refusing to start: a
  dependency builder that demanded a live broker could not be built without one.

The publisher factory is deliberately left at its null default: a publisher belongs to a
producer, and initialising one here would only mean this process talking to itself. The two
producer halves are installed by their own roots — the agent runtime's distillation publish
by ``multiuser_config/agent_runtime/dependencies.py``, and the chat writer's announcement
(``supervisor/message_bus.py``) by the supervisor entrypoint, which does not exist yet.

The handler table is not a placeholder — it is the wiring ``server.py`` performs, and both
consolidation entries are one object's method on purpose: the continuation is how a run is
handed back the state it left on the handler.

The holder id minted here is what names *this* process among its peers on the lease rows
consolidation takes: with several instances draining one stream, the pair a run holds is a
row in the runtime database, and only the instance that took it may renew or release it.
"""

from __future__ import annotations

import logging
import pathlib
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from background_memory.env import MemoryEnv
from background_memory.handlers.consolidation import ConsolidationHandler
from background_memory.handlers.distillation import DistillationHandler
from background_memory.handlers.titles import PersistedMessageHandlers, TitleHandler
from background_memory.identity import mint_holder_id
from background_memory.infra.foreground import TaskTableForegroundPressure
from background_memory.infra.repositories import (
    SqlAuditRepo,
    SqlChatHistoryRepo,
    SqlConsolidationLeaseRepo,
    SqlContextSummaryRepo,
    SqlMemoryHeaderRepo,
    SqlTaskRepo,
)
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    ConsolidationContinuationEvent,
    IMemoryEventConsumer,
    MemoryDistillationEvent,
    MemoryEvent,
    MemoryEventHandler,
)
from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryRepo
from config_registry.user_settings.reader import user_settings_factory
from ouroboros.app.broker_composition import (
    UnhandledBrokerBackend,
    resolve_broker_backend,
    resolve_memory_event_consumer,
)
from ouroboros.llm import LLMClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackgroundMemoryDependencies:
    """What the memory loop is built from: where events come from, and what handles them."""

    consumer: IMemoryEventConsumer
    handlers: dict[type[MemoryEvent], MemoryEventHandler]
    release: Callable[[], Awaitable[None]]


def build_dependencies(
    *,
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    stop_event: threading.Event,
    engine: AsyncEngine,
) -> BackgroundMemoryDependencies:
    """Compose the loop's collaborators. Replace the consumer, keep the table."""
    env = MemoryEnv(repo_dir=repo_dir, drive_root=drive_root)
    # Built once, not per run: a handler with no repository has no work to do, and that is a
    # startup failure rather than something to rediscover on every event.
    history = SqlChatHistoryRepo(engine)
    tasks = SqlTaskRepo(engine)
    audit = SqlAuditRepo(engine)
    user_settings = user_settings_factory.settings()
    consolidation = ConsolidationHandler(
        LLMClient(),
        leases=SqlConsolidationLeaseRepo(engine),
        summaries=SqlContextSummaryRepo(engine),
        history=history,
        audit=audit,
        user_settings=user_settings,
        foreground_pressure=TaskTableForegroundPressure(tasks),
        stop_event=stop_event,
        holder_id=mint_holder_id(),
    )
    consumer = _resolved_consumer()
    return BackgroundMemoryDependencies(
        consumer=consumer,
        release=consumer.close,
        handlers={
            MemoryDistillationEvent: DistillationHandler(
                env,
                LLMClient(),
                tasks=tasks,
                audit=audit,
                memory_headers=SqlMemoryHeaderRepo(engine),
                user_settings=user_settings,
            ).handle,
            ChatMessagePersistedEvent: PersistedMessageHandlers(
                TitleHandler(LLMClient(), SqlChatRegistryRepo(engine), history=history, user_settings=user_settings),
                consolidation,
            ).handle,
            ConsolidationContinuationEvent: consolidation.handle,
        },
    )


def _resolved_consumer() -> IMemoryEventConsumer:
    """The reading half the selector names, and one line saying which it was.

    The warning is the whole point of logging it: a tier composed on the default consumes
    nothing at all, and that is invisible from the outside — the loop starts, logs that it
    started, and polls an empty queue for the life of the process.
    """
    consumer = resolve_memory_event_consumer()
    backend = resolve_broker_backend()
    match backend:
        case "redis" | "kafka":
            log.info("Background memory consumes events through %s", type(consumer).__name__)
        case "inprocess":
            log.warning(
                "Background memory composed the in-process consumer (OUROBOROS_BROKER_BACKEND=%s): its queue's "
                "writer would be in this process, so this tier will consume nothing. Set the selector to 'redis' "
                "or 'kafka' for a tier that reads what other processes publish.",
                backend,
            )
        case _:
            raise UnhandledBrokerBackend(backend)
    return consumer


__all__ = ["BackgroundMemoryDependencies", "build_dependencies"]
