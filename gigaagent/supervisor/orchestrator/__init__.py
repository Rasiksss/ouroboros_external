"""Task orchestration for ingress → durable work.

The inbound envelope is not re-exported here: it belongs to the seam
(``transport.interfaces.InboundEnvelope``), and a second name for it is how two
envelopes grow back.
"""

from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
from supervisor.orchestrator.orchestrator import SubmitResult, TaskOrchestrator

__all__ = [
    "SubmissionMode",
    "SubmitResult",
    "TaskIntentKind",
    "TaskOrchestrator",
]
