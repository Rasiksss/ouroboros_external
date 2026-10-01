"""Task intent classification for the supervisor orchestrator."""

from __future__ import annotations

from enum import Enum


class TaskIntentKind(str, Enum):
    AGENT_CHAT = "agent_chat"
    SLASH_COMMAND = "slash_command"
    SKILL_JOB = "skill_job"
    INTERNAL = "internal"


class SubmissionMode(str, Enum):
    NONE = "none"
    QUEUE = "queue"
    INJECT = "inject"


__all__ = ["SubmissionMode", "TaskIntentKind"]
