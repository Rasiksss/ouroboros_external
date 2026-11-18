"""What the composition root does with its transports when the supervisor dies.

The supervisor must not name a transport and a transport must not name the
supervisor, so the reaction lives here. When ``run_supervisor`` returns — init
failure, too many tick crashes, or a requested shutdown — nothing drains the
inbox any more, and a transport left running keeps polling its channel, filling
an unbounded inbox and acknowledging messages nobody will read.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ouroboros.storage.runtime_models import is_dialogs_source

log = logging.getLogger(__name__)


def report_missing_dialogs_transport(owner_source: str) -> None:
    """Report an owner whose configured transport has no reader."""
    if is_dialogs_source(owner_source):
        log.error(
            "Owner chat source is %s but the dialogs transport is disabled — every message "
            "published for it is discarded, task notices included",
            owner_source,
        )


class StartedTransports:
    """The stop hooks of the transports this process actually started.

    Registered as each one comes up, which is what handles the ordering: the
    supervisor thread can already be running when the first transport is
    constructed, and one that never started has nothing to stop.
    """

    def __init__(self) -> None:
        self._stops: list[tuple[str, Callable[[], None]]] = []
        self._lock = threading.Lock()

    def register(self, name: str, stop: Callable[[], None]) -> None:
        with self._lock:
            self._stops.append((name, stop))

    def stop_all(self) -> None:
        """Stop every registered transport; a failing one must not keep the rest alive."""
        with self._lock:
            stops = list(self._stops)
            self._stops.clear()
        for name, stop in stops:
            try:
                stop()
            except Exception:
                log.warning("Failed to stop the %s transport", name, exc_info=True)


def stop_transports_when_supervisor_exits(
    run: Callable[[], None],
    *,
    transports: StartedTransports,
    supervisor_error: Callable[[], str | None],
) -> None:
    """Run the supervisor, then stop the transports it was the reader for."""
    try:
        run()
    finally:
        error = supervisor_error()
        if error:
            log.critical("Supervisor exited (%s); stopping the transports", error)
        else:
            log.info("Supervisor exited; stopping the transports")
        transports.stop_all()


__all__ = [
    "StartedTransports",
    "report_missing_dialogs_transport",
    "stop_transports_when_supervisor_exits",
]
