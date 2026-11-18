"""What this package does while its broker does not answer.

The same decision `ouroboros/contracts/stream_replies.py::BrokerOutage` makes, with the
waiting taken out of it: that object ends `stands` and `record` in `time.sleep`, and it is
instantiated seven times across four modules this series does not touch. Here the policy
decides and returns, and the caller awaits the wait — a consumer applying it on an event
loop must not stop every other delivery sharing that loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any


class AsyncBrokerOutage:
    """A broker that failed, and the pace at which a reader may dial it again.

    Not a circuit breaker and not a latch: the outage is a delay before the next command, so
    a broker that comes back is used again without a restart.
    """

    def __init__(self, *, logger: logging.Logger, retry_after_sec: float, report_every_sec: float) -> None:
        self._log = logger
        self._retry_after_sec = float(retry_after_sec)
        self._report_every_sec = float(report_every_sec)
        self._retry_commands_at = 0.0
        self._report_at = 0.0
        self._standing = False

    def stands(self) -> bool:
        """Whether no command may be issued yet. Decides and returns; it waits for nobody."""
        return time.monotonic() < self._retry_commands_at

    def record(self, message: str, *args: Any) -> None:
        """Note a failure and report it at most on the interval. Performs no sleep.

        The first record of each outage is reported immediately and with its traceback, even
        inside the interval of an earlier outage's report: the second failure is a different
        one, and a gate that spanned outages logged it nowhere.
        """
        now = time.monotonic()
        self._retry_commands_at = now + self._retry_after_sec
        first, self._standing = not self._standing, True
        if first or now >= self._report_at:
            self._report_at = now + self._report_every_sec
            self._log.warning(message, *args, exc_info=first)

    def cleared(self, message: str, *args: Any) -> None:
        """The broker answered again: forget the outage, and say so if there was one."""
        if not self._standing:
            return
        self._standing = False
        self._retry_commands_at = 0.0
        self._report_at = 0.0
        self._log.warning(message, *args)


async def wait_out(deadline: float) -> None:
    """Spend what is left of the caller's timeout without occupying its loop."""
    remaining = deadline - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)


__all__ = ["AsyncBrokerOutage", "wait_out"]
