"""What the agent runtime holds that stops being in-process the day it moves out.

Three of the four are resolved from the broker selector, and the reason each one matters
is what stops the next reader from swapping only the first.

A fourth collaborator does **not** appear here on purpose: the **memory-event publisher**, a
completed task's distillation path. It is installed into the process-local holder rather than
returned — the producer that reads it is the task graph
(``ouroboros/agent/task_graph.py``), which nothing between this root and the run carries a
publisher into — and it is installed in ``run.py`` *before* ``compose_process``, because the
readers that snapshot the holder (the skill bridge, the A2A server) are composed there. A
call inside this builder would be after that step and would pin the ordering in the one place
a later reader is most likely to break it.

* ``events`` is the *publishing* half of the seam a run reports through. Built as an
  in-process queue in this process it has no reader at all — droppable kinds are discarded
  and every other kind blocks once the queue fills, because the message it is holding is
  the owner's answer.
* ``outbound`` is the runtime's *second* outbound path: a tool that hands the owner a file
  publishes straight onto the outbox rather than through an event
  (``ouroboros/tools/service/messaging.py``), so for an attachment it is not a second path
  but the only one. It goes over the same resolved gateway's outbox as ``events`` does, or a
  swap covering only ``events`` silently stops delivering attachments.
* ``work_signal`` stays local, by decision rather than omission: both ends of the signal
  that matters are inside this process — ``build_agent_runtime`` hands the same object to
  the queue it builds and to the executor, and "the signal that matters here is the one the
  executor raises when a lane comes free" (``ouroboros/agent_runtime/bootstrap.py``). The
  half that genuinely crosses — a supervisor enqueuing work and waking a runtime — costs at
  most ``RUNTIME_POLL_INTERVAL_SEC`` (1.0 s, ``ouroboros/agent_runtime/runtime.py``) of
  latency and never a task, because the runtime polls its own claim.
* ``task_stops`` is the consuming half of a best-effort hint after the durable stop write.
  Its interface carries no Redis operation, so the composition root can replace Streams with Kafka.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from new_core.coordination.inprocess import InProcessWorkSignal
from new_core.coordination.interfaces import ITaskStopConsumer, IWorkSignal
from new_core.tools.interfaces import ToolOutbound
from ouroboros.agent_events.interfaces import IAgentEventPublisher
from ouroboros.agent_runtime.identity import mint_runtime_id
from ouroboros.app.broker_composition import (
    build_message_gateway,
    resolve_agent_event_publisher,
    resolve_task_stop_consumer,
)
from supervisor.message_bus import LocalChatBridge


@dataclass(frozen=True)
class AgentRuntimeDependencies:
    """The collaborators that stop being in-process the day the runtime moves out.

    The stop event, the settings and the paths are this process's own whatever the
    transport underneath becomes, so they are arguments to the script rather than fields
    here: what this type holds is exactly what a broker replaces.
    """

    events: IAgentEventPublisher
    outbound: ToolOutbound
    work_signal: IWorkSignal
    task_stops: ITaskStopConsumer


def build_dependencies(*, drive_root: pathlib.Path) -> AgentRuntimeDependencies:
    """Compose what the configured broker names. The signature is the script's contract."""
    # The runtime never polls the inbox, but the gateway names a consumer for whoever
    # does; minted rather than shared, because two instances presenting one name would read
    # each other's in-flight envelope as a redelivery.
    gateway = build_message_gateway(inbox_consumer=mint_runtime_id())
    return AgentRuntimeDependencies(
        events=resolve_agent_event_publisher(),
        outbound=LocalChatBridge(gateway.outbox, drive_root),
        work_signal=InProcessWorkSignal(),
        task_stops=resolve_task_stop_consumer(),
    )


__all__ = ["AgentRuntimeDependencies", "build_dependencies"]
