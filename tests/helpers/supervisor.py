"""Test helpers for supervisor runtime wiring, and for the memory loop beside it."""

from __future__ import annotations

import pathlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from background_memory.handlers.consolidation import lease_expiry_sec
from background_memory.infra.events import memory_event_publisher_factory
from background_memory.infra.events.inmemory import InMemoryEventConsumer, MemoryEventQueue
from background_memory.interfaces import IMemoryEventPublisher, MemoryEvent, MemoryEventHandler
from background_memory.runtime import MemoryRuntime
from new_core.coordination.inprocess import InProcessWorkSignal, NullWorkSignal
from new_core.coordination.interfaces import ITaskStopConsumer, IWorkSignal
from new_core.tools.interfaces import ToolOutbound
from ouroboros.agent_events.inmemory import create_agent_event_transport
from ouroboros.agent_events.interfaces import IAgentEventConsumer, IAgentEventPublisher
from ouroboros.agent_runtime.bootstrap import CLAIM_LOOP_STALL_SEC, build_agent_runtime
from ouroboros.agent_runtime.runtime import AgentRuntime
from ouroboros.app.task_stop_notifications import create_task_stop_transport
from ouroboros.observability.health import LoopHeartbeat
from ouroboros.storage.settings.store import load_settings
from supervisor.bootstrap import build_supervisor_runtime
from supervisor.message_bus import LocalChatBridge
from supervisor.runtime import LOOP_STALL_SEC
from supervisor.runtime_context import SupervisorRuntime, set_runtime
from tests.helpers.outbound import RecordingOutbound
from tests.helpers.scope import scope_for
from tests.helpers.shared import make_empty_extension_runtime, make_empty_skill_snapshot, make_skill_availability
from transport.infra.inmemory import InProcessInbox, InProcessMessageGateway, InProcessOutbox
from transport.interfaces import IMessageGateway, InboundEnvelope


def supervisor_heartbeat() -> LoopHeartbeat:
    return LoopHeartbeat("supervisor", stall_after_sec=LOOP_STALL_SEC)


def make_supervisor_loop(supervisor) -> Any:
    """Build a SupervisorLoop from a supervisor fixture's context."""
    from supervisor.runtime import SupervisorLoop

    return SupervisorLoop(
        ctx=supervisor.ctx,
        stop_event=threading.Event(),
        inbox=InProcessInbox(),
        heartbeat=supervisor_heartbeat(),
    )


def agent_heartbeat() -> LoopHeartbeat:
    return LoopHeartbeat("agent runtime", stall_after_sec=CLAIM_LOOP_STALL_SEC)


def memory_heartbeat() -> LoopHeartbeat:
    return LoopHeartbeat("memory runtime", stall_after_sec=lease_expiry_sec(load_settings()))


def envelope_from_chat(
    *,
    chat_id: Any,
    user_id: Any = None,
    text: str = "",
    source: str = "dialogs",
    image_data: tuple[str, str] | tuple[str, str, str] | None = None,
    correlation_id: str = "",
    client_message_id: str = "",
    trace_id: str = "",
) -> InboundEnvelope:
    """Build an inbound envelope the way a transport would, for tests that submit chat messages.

    A real transport mints its own scope and builds ``InboundEnvelope`` itself; this only
    spares the tests the closed-surface rule Dialogs ingress applies.
    """
    image_base64 = ""
    image_mime = "image/jpeg"
    image_caption = ""
    if image_data:
        image_base64 = image_data[0]
        image_mime = image_data[1]
        if len(image_data) > 2:
            image_caption = str(image_data[2] or "")
    scope = scope_for(source, chat_id)
    sender = user_id if user_id is not None else chat_id
    return InboundEnvelope(
        scope=scope,
        user_id=sender,
        text=str(text or ""),
        # The participant a dialogs chat delivers to, the way that transport fills it.
        transport={"target_uid": str(sender)},
        image_base64=image_base64,
        image_mime=image_mime,
        image_caption=image_caption,
        correlation_id=str(correlation_id or ""),
        client_message_id=str(client_message_id or ""),
        trace_id=str(trace_id or ""),
    )


def bootstrap_supervisor_for_tests(
    tmp_path: pathlib.Path,
    *,
    soft_timeout: int = 10,
    hard_timeout: int = 20,
    bridge: Any = None,
    gateway: IMessageGateway | None = None,
    agent_events: IAgentEventConsumer | None = None,
    work_signal: IWorkSignal | None = None,
) -> SupervisorRuntime:
    """Wire the supervisor as the composition root would, backend included."""
    message_gateway = (
        gateway if gateway is not None else InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    )
    runtime = build_supervisor_runtime(
        settings={
            "OUROBOROS_SOFT_TIMEOUT_SEC": soft_timeout,
            "OUROBOROS_HARD_TIMEOUT_SEC": hard_timeout,
        },
        drive_root=pathlib.Path(tmp_path),
        repo_dir=pathlib.Path(tmp_path),
        bridge=bridge if bridge is not None else LocalChatBridge(message_gateway.outbox, pathlib.Path(tmp_path)),
        gateway=message_gateway,
        agent_events=agent_events if agent_events is not None else create_agent_event_transport()[1],
        work_signal=work_signal if work_signal is not None else NullWorkSignal(),
    )
    set_runtime(runtime)
    return runtime


@dataclass
class AgentRuntimeParts:
    """The runtime the composition root would start, plus the seam halves beside it."""

    runtime: AgentRuntime
    publisher: IAgentEventPublisher
    consumer: IAgentEventConsumer
    work_signal: IWorkSignal
    stop_event: threading.Event
    outbound: ToolOutbound
    heartbeat: LoopHeartbeat


def bootstrap_agent_runtime_for_tests(
    tmp_path: pathlib.Path,
    *,
    soft_timeout: int = 10,
    hard_timeout: int = 20,
    work_signal: IWorkSignal | None = None,
    poll_interval_sec: float = 0.05,
    outbound: ToolOutbound | None = None,
    max_running_root_jobs_per_owner: int = 3,
    task_stops: ITaskStopConsumer | None = None,
) -> AgentRuntimeParts:
    """Wire an agent runtime the way the composition root does, over its own transport."""
    publisher, consumer = create_agent_event_transport()
    boundary = outbound if outbound is not None else RecordingOutbound()
    signal = work_signal if work_signal is not None else InProcessWorkSignal()
    stop_event = threading.Event()
    heartbeat = agent_heartbeat()
    runtime = build_agent_runtime(
        settings={
            "OUROBOROS_SOFT_TIMEOUT_SEC": soft_timeout,
            "OUROBOROS_HARD_TIMEOUT_SEC": hard_timeout,
            "OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER": max_running_root_jobs_per_owner,
        },
        drive_root=pathlib.Path(tmp_path),
        repo_dir=pathlib.Path(tmp_path),
        events=publisher,
        skill_snapshot=make_empty_skill_snapshot(),
        skill_availability=make_skill_availability(),
        extension_runtime=make_empty_extension_runtime(),
        outbound=boundary,
        work_signal=signal,
        stop_event=stop_event,
        heartbeat=heartbeat,
        task_stops=task_stops if task_stops is not None else create_task_stop_transport()[1],
        poll_interval_sec=poll_interval_sec,
    )
    return AgentRuntimeParts(
        runtime=runtime,
        publisher=publisher,
        consumer=consumer,
        work_signal=signal,
        stop_event=stop_event,
        outbound=boundary,
        heartbeat=heartbeat,
    )


@dataclass
class MemoryRuntimeParts:
    """The pieces ``server.py`` wires, handed back so a test can drive each one."""

    publisher: IMemoryEventPublisher
    consumer: InMemoryEventConsumer
    runtime: MemoryRuntime
    stop_event: threading.Event


def bootstrap_memory_runtime_for_tests(
    *,
    handlers: Mapping[type[MemoryEvent], MemoryEventHandler],
    maxsize: int = 8,
    drain_timeout_sec: float = 5.0,
) -> MemoryRuntimeParts:
    """Wire a memory runtime over its own queue, with whatever handlers the test brings.

    Mirrors the composition root: the factory is initialized over one bounded
    queue, the consumer reads the other end, and both share a stop event. The
    dispatch table is the test's — running the real handlers would need a model
    provider.
    """
    events = MemoryEventQueue(maxsize)
    memory_event_publisher_factory.initialize_inmemory(events)
    stop_event = threading.Event()
    consumer = InMemoryEventConsumer(events)
    return MemoryRuntimeParts(
        publisher=memory_event_publisher_factory.memory_publisher,
        consumer=consumer,
        runtime=MemoryRuntime(
            consumer,
            handlers=handlers,
            stop_event=stop_event,
            heartbeat=memory_heartbeat(),
            drain_timeout_sec=drain_timeout_sec,
        ),
        stop_event=stop_event,
    )


__all__ = [
    "AgentRuntimeParts",
    "MemoryRuntimeParts",
    "agent_heartbeat",
    "bootstrap_agent_runtime_for_tests",
    "bootstrap_memory_runtime_for_tests",
    "bootstrap_supervisor_for_tests",
    "envelope_from_chat",
    "memory_heartbeat",
    "supervisor_heartbeat",
]
