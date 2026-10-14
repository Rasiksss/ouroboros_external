"""A publisher that keeps what a run reported, for tests that assert on it."""

from __future__ import annotations

from ouroboros.agent_events.interfaces import AgentEvent, IAgentEventPublisher


class RecordingAgentEvents(IAgentEventPublisher):
    """Keeps every published message in order. Publish-only, like the real thing."""

    def __init__(self) -> None:
        self.published: list[AgentEvent] = []

    def publish(self, event: AgentEvent) -> None:
        self.published.append(event)

    def of_type(self, kind: type[AgentEvent]) -> list[AgentEvent]:
        return [event for event in self.published if isinstance(event, kind)]

    def first(self, kind: type[AgentEvent]) -> AgentEvent | None:
        matches = self.of_type(kind)
        return matches[0] if matches else None


__all__ = ["RecordingAgentEvents"]
