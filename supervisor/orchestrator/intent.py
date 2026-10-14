"""Task intent classification for the supervisor orchestrator."""

from __future__ import annotations

from enum import Enum


class TaskIntentKind(str, Enum):
    AGENT_CHAT = "agent_chat"
    SLASH_COMMAND = "slash_command"
    SKILL_JOB = "skill_job"
    INTERNAL = "internal"
    #: An answer to a question a run is waiting on. Not queued: the run holding a worker
    #: slot is the one waiting, so a queued answer could wait behind the run that needs it.
    CONFIRMATION_REPLY = "confirmation_reply"


class SubmissionMode(str, Enum):
    """How a classified message was submitted. There is no third way in any more.

    ``INJECT`` used to name adding a message to a run already in flight; that path is
    gone, and naming it here would advertise a door that is not there.
    """

    NONE = "none"
    QUEUE = "queue"


__all__ = ["SubmissionMode", "TaskIntentKind"]
