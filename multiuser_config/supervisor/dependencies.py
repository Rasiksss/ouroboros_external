"""What the supervisor tier holds that stops being in-process the day it moves out.

The mirror image of ``multiuser_config/agent_runtime/dependencies.py``: that root composes
the *publishing* half of the reporting seam and an outbound path, this one composes the
*reading* halves. Both resolve them through the one broker selector
(``ouroboros/app/broker_composition.py``), so a deployment cannot end up with one tier on a
broker and its counterpart in process.

* ``gateway`` is both directions of the message seam. The loop drains its inbox — under the
  default selector that inbox is this process's own, which no transport writes to, so a
  single-process default composes a tier with nothing to drain. It is also what the outbound
  bridge is built over, by the composition root rather than here: the bridge needs the drive
  root as well, and the root is the one place allowed to name both halves of that seam.
* ``agent_events`` is the *consuming* half of the seam a run reports through, and never the
  publishing one. ``SupervisorRuntime.agent_events`` is documented "Read-only: the supervisor
  cannot publish onto it", and a substitution point typed loosely enough to accept a
  publisher is how a later swap hands this tier a producer role nobody decided to give it.
  Resolving the consumer alone matters on the broker too: constructing a publisher here
  would put an appender on a stream this tier only reads.
* ``work_signal`` stays local, by decision rather than omission. The supervisor raises it
  when a task becomes claimable and a runtime in another process never hears it — but that
  runtime polls its own claim, so an undelivered signal costs latency and never a task. This
  is the same judgement ``multiuser_config/agent_runtime/dependencies.py`` records from the
  waiting side.
* ``task_stop_publisher`` is the producer half of the best-effort stop hint. The database write
  remains authoritative; this half only shortens how long the addressed runtime takes to observe it.

The **memory-event publisher** is deliberately absent from this dataclass, for the reason it
is absent from the agent runtime's: it is installed into a process-local holder rather than
returned, because the producer that reads it is a module function with no collaborator to be
handed one (``supervisor/message_bus.py``). The root installs it, before it composes anything
that could snapshot the holder.
"""

from __future__ import annotations

from dataclasses import dataclass

from new_core.coordination.inprocess import InProcessWorkSignal
from new_core.coordination.interfaces import ITaskStopPublisher, IWorkSignal
from ouroboros.agent_events.interfaces import IAgentEventConsumer
from ouroboros.app.broker_composition import (
    build_message_gateway,
    resolve_agent_event_consumer,
    resolve_task_stop_publisher,
)
from transport.interfaces import IMessageGateway


@dataclass(frozen=True)
class SupervisorDependencies:
    """The collaborators that stop being in-process the day the supervisor moves out.

    The settings, the paths and the stop event are this process's own whatever the broker
    underneath becomes, so they belong to the root rather than here: what this type holds is
    exactly what a broker replaces.
    """

    gateway: IMessageGateway
    agent_events: IAgentEventConsumer
    work_signal: IWorkSignal
    task_stop_publisher: ITaskStopPublisher


def build_dependencies(*, inbox_consumer: str) -> SupervisorDependencies:
    """Compose what the configured broker names. The signature is the root's contract.

    ``inbox_consumer`` is this instance's own name in the inbound consumer group and is
    passed in rather than derived here: two instances presenting one name would share a
    pending list, and each would read the other's in-flight envelope as its own redelivery.
    """
    return SupervisorDependencies(
        gateway=build_message_gateway(inbox_consumer=inbox_consumer),
        agent_events=resolve_agent_event_consumer(),
        work_signal=InProcessWorkSignal(),
        task_stop_publisher=resolve_task_stop_publisher(),
    )


__all__ = ["SupervisorDependencies", "build_dependencies"]
