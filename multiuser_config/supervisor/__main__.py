"""Run the supervisor tier: serve the configured address, and exit with the loop.

Shutdown runs in two directions and each has one mechanism. **Uvicorn stops first** on a
termination signal, which it captures for the duration of `serve()`: the lifespan's `finally`
sets the stop event and the loop returns at the top of its next iteration. **The loop stops
first** when it gave up or its init failed: `run_supervisor`'s own `finally` sets the same
stop event, and the watcher below turns that into `should_exit` — the direction the lifespan
cannot cover. The exit code separates the two, so an orchestrator can tell a crashed loop
from a deploy.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

from multiuser_config.bootstrap import serve_tier, stop_process_monitoring, tier_config
from multiuser_config.supervisor.app import COMPONENT, TIER

log = logging.getLogger(__name__)

#: How long the watcher waits between checks that it is still needed. It is a slice rather
#: than an open wait so the thread has a way out when `serve()` returned on its own.
_WATCH_SLICE_SEC = 0.5

#: The watcher holds nothing and is waking every slice, so the join is short; past it the
#: process leaves anyway rather than waiting on its own bookkeeping.
_WATCH_JOIN_SEC = 5.0


def _stop_serving_when_the_loop_stops(server: Any, stop_event: threading.Event, served: threading.Event) -> None:
    """Ask uvicorn to exit once the loop this process hosts has stopped **on its own**.

    One direction only, and the state is read after the wait rather than inferred from it.
    The same stop event is set on every ordinary shutdown too — uvicorn takes the signal,
    the lifespan sets it on the way down — so a watcher that concluded "the loop died" from
    the event alone would report a dead loop on every deploy and ask an already-exiting
    server to exit. `served` covers the case where `serve()` returned before this thread
    woke at all; `should_exit` covers the case where uvicorn is already on its way out.
    """
    while not stop_event.wait(_WATCH_SLICE_SEC):
        if served.is_set():
            return
    if served.is_set() or server.should_exit:
        return
    log.info("The supervisor loop stopped on its own — stopping the server.")
    server.should_exit = True


def main(argv: list[str] | None = None) -> int:
    del argv
    import uvicorn

    from multiuser_config.supervisor.app import build_supervisor_app

    process = build_supervisor_app()
    config = tier_config(process.app, tier=TIER)
    if config is None:
        # The refusal leaves before the `finally` that releases what the start-up attached.
        stop_process_monitoring()
        return 1
    server = uvicorn.Server(config)
    served = threading.Event()
    watcher = threading.Thread(
        target=_stop_serving_when_the_loop_stops,
        args=(server, process.stop_event, served),
        name="supervisor-exit-watch",
    )
    watcher.start()
    try:
        exit_code = serve_tier(server, tier=TIER)
    finally:
        # Both, in this order: `served` is what tells a watcher that is about to wake that it
        # is no longer needed, and the stop event is what wakes it now rather than a slice
        # from now. The loop's own stop is the lifespan's, which has already run by here.
        served.set()
        process.stop_event.set()
        watcher.join(timeout=_WATCH_JOIN_SEC)
        # What the shared start-up attached to this process is released by the process, after
        # the loop it hosts has been asked to stop.
        stop_process_monitoring()

    error = process.get_error()
    if error:
        log.error("The %s tier is exiting after a loop failure: %s", TIER, error)
        return 1
    return exit_code


__all__ = ["COMPONENT", "main"]


if __name__ == "__main__":
    sys.exit(main())
