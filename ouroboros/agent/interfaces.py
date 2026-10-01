"""Interfaces the background lanes observe, implemented by whoever schedules them."""

from __future__ import annotations

import abc


class ForegroundPressure(abc.ABC):
    """Whether a background lane should yield its run to foreground work.

    Behind it is the supervisor's task table today and shared state under a multi-instance
    supervisor, so the lane only ever asks.
    """

    @abc.abstractmethod
    def is_pending(self) -> bool:
        """Whether foreground work is waiting that this lane has to defer to."""


__all__ = ["ForegroundPressure"]
