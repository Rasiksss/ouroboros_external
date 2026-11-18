"""The loop that claims tasks and runs them, and the drain that gives them back.

Its own unit of execution: nothing here is driven by another component's tick. The claim
is polled on a bounded interval and that poll is sufficient on its own — a wake-up only
shortens the wait, and a missed one costs latency rather than a stranded task.
"""

from __future__ import annotations

import logging
import threading
import time

from new_core.coordination.interfaces import ITaskStopConsumer, IWorkSignal
from new_core.tools.runtimes import THREAD_RUNTIME
from ouroboros.agent_runtime.executor import TaskExecutor
from ouroboros.observability.health import LoopHeartbeat
from ouroboros.storage.runtime_backend import STORE_UNREACHABLE

log = logging.getLogger(__name__)

#: Longest a runtime waits before asking the claim again with no notification at all. The
#: ceiling on how late a queued task can start when every wake-up is lost.
RUNTIME_POLL_INTERVAL_SEC: float = 1.0


class AgentRuntime:
    """Claims tasks for itself and runs them, until it is asked to stop.

    Holds no per-owner map: at most one task per owner runs across the deployment because
    the claim statement says so, and a second answer kept in memory would be wrong the
    moment there are two runtimes.
    """

    def __init__(
        self,
        executor: TaskExecutor,
        *,
        work_signal: IWorkSignal,
        stop_event: threading.Event,
        heartbeat: LoopHeartbeat,
        task_stops: ITaskStopConsumer,
        poll_interval_sec: float = RUNTIME_POLL_INTERVAL_SEC,
    ) -> None:
        self._executor = executor
        self._work_signal = work_signal
        self._stop_event = stop_event
        self._heartbeat = heartbeat
        self._task_stops = task_stops
        self._poll_interval_sec = max(0.0, float(poll_interval_sec))
        self._task_stop_listener_stop = threading.Event()
        self._task_stop_listener: threading.Thread | None = None

    @property
    def runtime_id(self) -> str:
        """Who this runtime is on the records it claims."""
        return self._executor.runtime_id

    def execute(self) -> None:
        """Fill every free lane, reap what finished, and wait for the next round."""
        # Before the start, not only inside the loop: a heartbeat that never beat reads as
        # healthy, so a start that hangs here would keep liveness green for good.
        self._heartbeat.beat()
        if not self._task_stops.start():
            log.warning("Task-stop notification consumer unavailable at start; retained notices will be replayed")
        self._executor.ensure_started()
        self._start_task_stop_listener()
        log.info("Agent runtime started runtime_id=%s", self.runtime_id)
        failed_claims = 0
        try:
            while not self._stop_event.is_set():
                self._heartbeat.beat()
                freed = self._executor.ensure_healthy()
                failed_claims = self._claim(failed_claims)
                if freed:
                    continue
                self._work_signal.wait(self._poll_interval_sec)
        finally:
            self._stop_task_stop_listener()
            log.info("Agent runtime loop stopped runtime_id=%s", self.runtime_id)

    def _claim(self, failed_claims: int) -> int:
        try:
            self._executor.assign_next()
        except STORE_UNREACHABLE:
            level = logging.DEBUG if failed_claims else logging.WARNING
            log.log(level, "Claim failed on the runtime store; asking again on the next poll", exc_info=True)
            return failed_claims + 1
        if failed_claims:
            log.info("Claim reached the runtime store again after %d failed polls", failed_claims)
        return 0

    def _start_task_stop_listener(self) -> None:
        stop = threading.Event()
        self._task_stop_listener_stop = stop
        self._task_stop_listener = threading.Thread(
            target=self._consume_task_stops,
            args=(stop,),
            name="task-stop-listener",
            daemon=True,
        )
        self._task_stop_listener.start()

    def _stop_task_stop_listener(self) -> None:
        stop = self._task_stop_listener_stop
        stop.set()
        listener = self._task_stop_listener
        if listener is not None:
            listener.join(timeout=1.0)
            if listener.is_alive():
                log.warning("Task-stop listener did not stop within its bounded poll")
        self._task_stop_listener = None

    def _consume_task_stops(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                notice = self._task_stops.poll(0.25)
            except Exception:
                log.warning("Task-stop notification consumer failed", exc_info=True)
                stop.wait(1.0)
                continue
            if notice is not None:
                self._executor.handle_stop_notice(notice)

    def drain(self, *, deadline_sec: float) -> bool:
        """Stop claiming, wait, release held runs, then stop their local bodies."""
        deadline = time.monotonic() + max(0.0, deadline_sec)
        if not self._executor.drain(deadline_sec=deadline_sec):
            return False
        return THREAD_RUNTIME.drain(deadline_sec=max(0.0, deadline - time.monotonic()))

    def held_task_ids(self) -> list[str]:
        """The tasks this runtime's lanes are still running."""
        return self._executor.held_task_ids()

    def worker_counts(self) -> tuple[int, int]:
        """How many lanes this runtime holds, and how many are busy. Observability only."""
        slots = self._executor.all_slots()
        return len(slots), sum(1 for slot in slots if slot.busy_task_id)


__all__ = ["RUNTIME_POLL_INTERVAL_SEC", "AgentRuntime"]
