"""Backwards-compatible wrapper over the generic `run_orchestrator`.

Originally chain analysis had its own resumable-run module. To stop
duplicating the same pattern across extract / chat / quick_synthesis /
chains, the implementation moved to `run_orchestrator` parametrised by
`kind=`. This file keeps the historical API surface (`start_run`,
`subscribe`, `recover_orphan_runs`, etc.) so existing callers — the
analyze-chains endpoint, the resume endpoint, the import in run_logger
— don't need to change.
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Awaitable, Callable

from finengine.skills.run_logger import log_chain_run
from finengine.skills.run_orchestrator import (
    RunState as _RunState,
    cleanup_finished,
    get_run,
    list_active as _list_active,
    recover_orphan_runs as _recover_all,
    start_run as _start_run_generic,
    subscribe as _subscribe_generic,
)


def start_run(
    runner: Callable[[], AsyncGenerator[dict[str, Any], None]],
    *,
    meta: dict,
    on_done: Callable[[_RunState], Awaitable[None]] | None = None,
) -> str:
    """Spawn a chain run as a background task. Same signature as before."""
    return _start_run_generic(runner, kind="chain", meta=meta, on_done=on_done)


async def subscribe(run_id: str) -> AsyncGenerator[dict, None]:
    """Yield every event for the chain run `run_id` — past + live."""
    async for ev in _subscribe_generic(run_id, kind="chain"):
        yield ev


def list_active() -> list[str]:
    return _list_active(kind="chain")


def recover_orphan_runs() -> int:
    """Mark unfinished chain journals as session-lost. Returns count."""
    counts = _recover_all()
    return counts.get("chain", 0)


__all__ = [
    "start_run",
    "subscribe",
    "get_run",
    "list_active",
    "cleanup_finished",
    "recover_orphan_runs",
    "log_chain_run",
]
