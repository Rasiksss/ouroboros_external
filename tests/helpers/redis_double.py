"""The local Redis stand-in the default gate runs the broker backends against.

One copy, because two would drift: both the message gateway
(`tests/transport/test_transport_redis.py`) and the agent-event transport
(`tests/ouroboros/agent_events/test_agent_event_redis_transport.py`) hold their backend to the same suite
over this.

**What it does not prove.** It is `fakeredis` plus the one correction below, so it
exercises a backend's *own* logic — the drop-or-wait decision, the decode, the
delete-on-read, the group bookkeeping — and nothing about Redis semantics. The wire
itself (a real socket, a real timeout, a connection the server reaps), `lag` reported
as absent after a trim, `entries-added`, pending entries whose stream entry was
trimmed away and come back as ``(id, None)``, and the exact ordering Redis gives two
clients racing on one entry are all outside it. Those are what the `integration`-marked
runs against a real server are for.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ouroboros.contracts.stream_replies import first_entry


class BlockingRedisDouble:
    """The double with `BLOCK` honoured, delegating everything else untouched.

    `fakeredis`'s `XREADGROUP` returns immediately whatever `BLOCK` says, which quietly
    turns every blocking case into a busy spin: a close that must interrupt a command in
    flight would be measured at the reader's top-of-loop guard instead, and a poll that
    must wait out its timeout would measure the spin rather than the wait. With this the
    blocking reads, the slicing and the recovery from a group that vanished mid-read are
    all exercised by the default gate.
    """

    _WAKE_INTERVAL_SEC = 0.005

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def xreadgroup(self, *args: Any, block: int | None = None, **kwargs: Any) -> Any:
        if not block:
            return self._inner.xreadgroup(*args, **kwargs)
        deadline = time.monotonic() + block / 1000.0
        while True:
            response = self._inner.xreadgroup(*args, **kwargs)
            if first_entry(response) is not None or time.monotonic() >= deadline:
                return response
            time.sleep(self._WAKE_INTERVAL_SEC)


class AsyncBlockingRedisDouble:
    """:class:`BlockingRedisDouble` for the asyncio client, waiting the way a loop must."""

    _WAKE_INTERVAL_SEC = 0.005

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def xreadgroup(self, *args: Any, block: int | None = None, **kwargs: Any) -> Any:
        if not block:
            return await self._inner.xreadgroup(*args, **kwargs)
        deadline = time.monotonic() + block / 1000.0
        while True:
            response = await self._inner.xreadgroup(*args, **kwargs)
            if first_entry(response) is not None or time.monotonic() >= deadline:
                return response
            await asyncio.sleep(self._WAKE_INTERVAL_SEC)


def fake_server() -> Any:
    """One keyspace two clients can share, so a case can hold both halves of a seam."""
    import fakeredis

    return fakeredis.FakeServer()


def client_onto(server: Any) -> BlockingRedisDouble:
    """A client onto `server`, with `BLOCK` honoured."""
    import fakeredis

    return BlockingRedisDouble(fakeredis.FakeStrictRedis(decode_responses=True, server=server))


def async_client_onto(server: Any) -> AsyncBlockingRedisDouble:
    """An asyncio client onto the same `server`, so a case can hold a sync and an async half."""
    import fakeredis

    return AsyncBlockingRedisDouble(fakeredis.FakeAsyncRedis(decode_responses=True, server=server))


__all__ = ["AsyncBlockingRedisDouble", "BlockingRedisDouble", "async_client_onto", "client_onto", "fake_server"]
