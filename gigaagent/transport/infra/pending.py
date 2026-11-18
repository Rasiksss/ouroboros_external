from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from transport.interfaces import OutboundMessage


@dataclass
class PendingDelivery:
    message: OutboundMessage
    attempts: int = 0
    available_at: float = 0.0

    @staticmethod
    def take(queue: deque[PendingDelivery], now: float) -> tuple[PendingDelivery | None, float]:
        blocked: set[tuple[str, str]] = set()
        earliest = float("inf")
        for index, pending in enumerate(queue):
            scope = pending.message.scope
            owner = (scope.owner_id, scope.agent_id)
            if owner in blocked:
                continue
            if pending.available_at <= now:
                del queue[index]
                return pending, 0.0
            blocked.add(owner)
            earliest = min(earliest, pending.available_at)
        return None, earliest - now
