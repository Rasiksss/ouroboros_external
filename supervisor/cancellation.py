"""The single-process implementation of the run cancellation signal.

A multi-instance supervisor swaps this class for one backed by shared state; every
observer of the interface stays as it is.
"""

from __future__ import annotations

import threading

from new_core.loop import CancellationSignal


class EventCancellation(CancellationSignal):
    """A stop signal backed by a ``threading.Event``."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def is_stopped(self) -> bool:
        return self._event.is_set()

    def request_stop(self) -> None:
        self._event.set()


__all__ = ["EventCancellation"]
