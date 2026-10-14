"""An event loop for the tool clients of a tier that has no server to own one.

The single-process server hands the MCP and A2A managers uvicorn's loop; a tier started as
``python -m`` runs a synchronous runtime and has none, and a client whose sessions are
coroutines cannot exist without one. So the tier starts its own, in a thread, for the
lifetime of the process — and stops it after its runs have drained, because a session
closed by the process ending is a session the server sees disconnect rather than leave.

Here rather than in ``multiuser_config/bootstrap.py``: four of the five tiers run no tools
and need no loop, and the shared start-up owns nothing component-specific.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

#: How long the stop waits for the loop's thread before it gives up and says so.
_JOIN_TIMEOUT_SEC = 5.0

__all__ = ["start_tool_client_loop"]


def start_tool_client_loop() -> tuple[asyncio.AbstractEventLoop, Callable[[], None]]:
    """Run a loop in a thread of this process and return it with the call that stops it.

    The loop is returned already running: the caller hands it to clients that schedule onto
    it from the worker threads, so a loop that is merely constructed would accept work and
    never run it.
    """
    loop = asyncio.new_event_loop()
    running = threading.Event()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(running.set)
        try:
            loop.run_forever()
        finally:
            # Whatever the clients left behind, closed here rather than in the stopping
            # thread: an async generator's finaliser is itself a coroutine, and this is the
            # last moment a loop exists to run it on.
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                log.warning("Tool client loop failed to shut its async generators down", exc_info=True)
            loop.close()

    thread = threading.Thread(target=_run, name="tool-client-loop", daemon=True)
    thread.start()
    running.wait(timeout=_JOIN_TIMEOUT_SEC)

    def stop() -> None:
        """Stop the loop and wait out its thread. Safe to call twice, as a `finally` will."""
        if loop.is_closed():
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=_JOIN_TIMEOUT_SEC)
        if thread.is_alive():
            log.warning("Tool client loop did not stop within %.1fs", _JOIN_TIMEOUT_SEC)

    return loop, stop
