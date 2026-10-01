"""Wait for supervisor startup before A2A dispatch or readiness probes."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

_lock = threading.Lock()
_ready_event: threading.Event | None = None
_get_error: Callable[[], str | None] | None = None


def configure_supervisor_readiness(
    ready_event: threading.Event,
    get_error: Callable[[], str | None],
) -> None:
    global _ready_event, _get_error
    with _lock:
        _ready_event = ready_event
        _get_error = get_error


def reset_supervisor_readiness_for_tests() -> None:
    global _ready_event, _get_error
    with _lock:
        _ready_event = None
        _get_error = None


async def wait_for_supervisor_ready(
    *,
    timeout_sec: float = 300.0,
    require_bridge: bool = True,
) -> None:
    """Block until supervisor startup finishes (or fails)."""
    with _lock:
        ready_event = _ready_event
        get_error = _get_error

    if ready_event is None:
        raise RuntimeError("Supervisor readiness is not configured")

    if not ready_event.is_set():
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(
            None,
            lambda: ready_event.wait(timeout=timeout_sec),
        )
        if not ok:
            raise TimeoutError(f"Supervisor not ready within {timeout_sec:.0f}s")

    if get_error is not None:
        error = get_error()
        if error:
            raise RuntimeError(f"Supervisor failed: {error}")

    if not require_bridge:
        return

    from supervisor.runtime_context import try_get_bridge

    if try_get_bridge() is None:
        raise RuntimeError(
            "Supervisor not ready — bridge not initialized. "
            "Ensure CLOUDRU_FOUNDATION_MODELS_API_KEY or GIGACHAT_B2BANK_* is set so the "
            "supervisor starts (check server logs for 'Supervisor ready' or init errors)."
        )


__all__ = [
    "configure_supervisor_readiness",
    "reset_supervisor_readiness_for_tests",
    "wait_for_supervisor_ready",
]
