"""The run cancellation signal, in its process-local and its shared-state forms.

Both are `CancellationSignal`, so no observer changes between them: the event is instant
and reaches only this process, the store reaches every instance and is honoured within a
stated interval. A run is handed both, composed, because the local one is strictly better
where it applies.
"""

from __future__ import annotations

import logging
import threading
import time

from new_core.identity import OwnerScope
from new_core.loop import CancellationSignal
from ouroboros.storage.runtime_backend import STORE_FAILURES, ExecutionClaimUnavailable
from ouroboros.storage.tasks.interfaces import ITaskRepository

log = logging.getLogger(__name__)

#: How long a stop recorded on another instance may go unobserved. The trade is explicit:
#: a stop stops being instant and becomes bounded, measured against a `/stop` that until
#: now set no signal at all. `route_guard` asks between rounds and tool bodies ask more
#: often, so the check is a cached read on this interval rather than a query per call.
STOP_POLL_INTERVAL_SEC: float = 5.0


class EventCancellation(CancellationSignal):
    """A stop signal backed by a ``threading.Event``. Instant, and this process only."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def is_stopped(self) -> bool:
        return self._event.is_set()

    def request_stop(self) -> None:
        self._event.set()


class StoreCancellation(CancellationSignal):
    """A stop signal backed by the run's own record, so it crosses instances.

    A terminal record stops the run as surely as a recorded stop does: whatever closed it
    already decided the run is over. Once either is seen the answer stays — a stop is
    absorbing, and re-reading it would only cost round trips.
    """

    def __init__(
        self,
        tasks_repo: ITaskRepository,
        *,
        task_id: str,
        scope: OwnerScope,
        poll_interval_sec: float,
        claim_token: str | None = None,
    ) -> None:
        self.tasks_repo = tasks_repo
        self._task_id = str(task_id)
        self._scope = scope
        self._poll_interval_sec = float(poll_interval_sec)
        self._stopped = False
        self._read_at: float | None = None
        self._claim_token = claim_token

    def is_stopped(self) -> bool:
        if self._stopped:
            return True
        now = time.monotonic()
        if self._read_at is not None and now - self._read_at < self._poll_interval_sec:
            return False
        self._read_at = now
        try:
            if self._claim_token is not None:
                self._stopped = not self.tasks_repo.is_task_attempt_active(self._task_id, claim_token=self._claim_token)
                return self._stopped
            state = self.tasks_repo.read_task_stop_state(self._task_id)
        except STORE_FAILURES as exc:
            self._read_at = None
            if self._claim_token is not None:
                raise ExecutionClaimUnavailable(self._task_id) from exc
            log.debug("Failed to read the stop state of task %s", self._task_id, exc_info=True)
            return False
        # Or-ed rather than assigned: the signal has two readers now — the body and the
        # executor's own sweep — so a `request_stop` landing between the read above and this
        # line must not be overwritten by the stale answer it raced.
        self._stopped = self._stopped or state.stop_requested or state.is_terminal
        return self._stopped

    def request_stop(self) -> None:
        """Record the intent, and observe it here without waiting for the next read."""
        self.tasks_repo.request_task_stop(self._task_id, scope=self._scope)
        self._stopped = True


class CompositeCancellation(CancellationSignal):
    """Stopped when any part is; a request reaches all of them.

    Order is cost: the parts are asked in the order given, so the local event answers
    first and the store is only read when nothing nearer has stopped the run.
    """

    def __init__(self, *signals: CancellationSignal) -> None:
        if not signals:
            raise ValueError("a composed cancellation needs at least one signal")
        self._signals = signals

    def is_stopped(self) -> bool:
        return any(signal.is_stopped() for signal in self._signals)

    def request_stop(self) -> None:
        for signal in self._signals:
            signal.request_stop()


__all__ = [
    "STOP_POLL_INTERVAL_SEC",
    "CompositeCancellation",
    "EventCancellation",
    "StoreCancellation",
]
