from __future__ import annotations

import asyncio
import threading

from multiuser_config.agent_runtime.tool_client_loop import start_tool_client_loop


def test_the_tier_loop_runs_coroutines_submitted_from_another_thread() -> None:
    loop, stop = start_tool_client_loop()
    try:
        assert loop.is_running()

        async def _answer() -> str:
            return "reached"

        assert asyncio.run_coroutine_threadsafe(_answer(), loop).result(timeout=5) == "reached"
    finally:
        stop()

    assert loop.is_closed()


def test_stopping_the_tier_loop_joins_its_thread() -> None:
    before = {thread.name for thread in threading.enumerate()}
    loop, stop = start_tool_client_loop()
    started = {thread.name for thread in threading.enumerate()} - before
    assert started, "the loop runs in a thread of its own"

    stop()

    assert not ({thread.name for thread in threading.enumerate()} & started)
    assert loop.is_closed()


def test_stopping_the_tier_loop_twice_is_not_an_error() -> None:
    _loop, stop = start_tool_client_loop()
    stop()
    stop()
