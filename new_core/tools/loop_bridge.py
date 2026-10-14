"""Calling a coroutine that lives on another loop, from a worker thread or from a coroutine.

Tool sources whose client is owned by the app's lifespan loop — MCP sessions, outbound A2A
clients — are reached from wherever the agent happens to run. Both directions are here so the
grace margin and the cancel-on-timeout are decided once.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

__all__ = ["BRIDGE_GRACE_SEC", "BridgeTimeout", "call_on_loop", "call_on_loop_sync"]

T = TypeVar("T")

#: Grace on the cross-thread wait, so the inner call's own deadline is what fires for a call
#: at the boundary rather than this outer one.
BRIDGE_GRACE_SEC = 2.0


class BridgeTimeout(TimeoutError):
    """The outer wait expired: the inner deadline did not answer in time, or there is none."""


def call_on_loop_sync(
    loop: asyncio.AbstractEventLoop,
    make_coro: Callable[[], Coroutine[Any, Any, T]],
    *,
    timeout_sec: float,
    grace_sec: float = BRIDGE_GRACE_SEC,
) -> T:
    """Run ``make_coro()`` on ``loop`` and block this thread for its result."""
    future = asyncio.run_coroutine_threadsafe(make_coro(), loop)
    try:
        return future.result(timeout=float(timeout_sec) + grace_sec)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise BridgeTimeout(str(timeout_sec)) from exc


async def call_on_loop(
    loop: asyncio.AbstractEventLoop,
    make_coro: Callable[[], Coroutine[Any, Any, T]],
    *,
    timeout_sec: float,
    grace_sec: float = BRIDGE_GRACE_SEC,
) -> T:
    """The same call from a coroutine on a *different* loop, leaving the caller's loop free."""
    future = asyncio.run_coroutine_threadsafe(make_coro(), loop)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=float(timeout_sec) + grace_sec)
    except asyncio.TimeoutError as exc:
        future.cancel()
        raise BridgeTimeout(str(timeout_sec)) from exc
