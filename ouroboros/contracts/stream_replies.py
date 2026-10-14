from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

#: How long a broker that failed is left alone before a reader issues a command again. The
#: bound on a hot loop: every consumer here is polled by a loop that comes straight back
#: after a poll that returned nothing, and a refused connection answers in microseconds, so
#: without a pause the tier spends a core and megabytes of log a second on a Redis that is
#: restarting.
BROKER_RETRY_SEC = 1.0

#: How often one outage is reported while it lasts. The first failure of *each* outage
#: carries its traceback; the repeats say the same thing about the same one. The gate is per
#: outage rather than per process: a second, different failure inside the interval of the
#: first one's report is a different diagnosis, and swallowing it leaves an operator with a
#: line that names neither the cause nor the fact that it changed.
BROKER_REPORT_INTERVAL_SEC = 30.0


def text(value: Any) -> Any:
    """One reply value as text, whether or not the client decodes responses.

    A client built without ``decode_responses`` would otherwise deliver wrong values
    rather than an error — ``str(b"x")`` is ``"b'x'"`` and nothing downstream can tell.
    """
    return value.decode("utf-8") if isinstance(value, bytes) else value


def row(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    """One reply mapping — an entry's fields, an ``XINFO`` row — as text keys and values."""
    return {text(key): text(value) for key, value in mapping.items()}


def first_entry(response: Any) -> tuple[str, Mapping[Any, Any]] | None:
    """The first ``(id, fields)`` of an ``XREADGROUP`` reply, in either protocol's shape.

    An entry whose fields are ``None`` is skipped: Redis answers that for a pending entry
    whose stream entry was trimmed away, and reading it as fields would crash the reader.
    """
    if not response:
        return None
    streams = response.values() if isinstance(response, dict) else (entries for _name, entries in response)
    for entries in streams:
        for entry_id, fields in entries or ():
            if fields is None:
                continue
            return (str(text(entry_id)), fields)
    return None


def entry_key(entry_id: Any) -> tuple[int, int]:
    """A stream id as the pair it orders on."""
    milliseconds, _, sequence = str(text(entry_id)).partition("-")
    return (int(milliseconds or 0), int(sequence or 0))


def next_entry_id(entry_id: Any) -> str:
    """The id immediately after ``entry_id``, for a ``MINID`` bound that includes it."""
    milliseconds, sequence = entry_key(entry_id)
    return f"{milliseconds}-{sequence + 1}"


def block_ms(deadline: float, cap_ms: int) -> int:
    """Milliseconds to block on the next read; zero means do not block at all.

    Capped, because a caller's long timeout must not become one command: a backend slices
    so a close can interrupt it, and another keeps every block below the socket timeout so
    redis-py does not raise on a read doing exactly what it was asked to do.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0
    return max(1, min(cap_ms, int(remaining * 1000)))


def wait_out(deadline: float) -> None:
    """Spend what is left of a caller's timeout, so a failed poll is not a spin.

    Bounded by the caller's own deadline, so shutdown latency is exactly what it is on the
    healthy path and a zero timeout still returns at once.
    """
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


class BrokerOutage:
    """A broker that failed, and the pace at which a reader may dial it again.

    The one thing every consumer on these seams answers identically: an I/O failure inside
    ``poll`` is ``None`` and a paced wait, never a raise. Two of the three readers used to
    raise, and both are polled from inside the supervisor's tick — where a raise is a tick
    crash, three of them in a row abort the loop
    (``supervisor/runtime.py``), and the tier's process exits while its transports keep
    answering their probes. A Redis restart is transient; a fleet wedged in ``exited`` is not.

    Not a circuit breaker and not a latch: the outage is a *delay* before the next command,
    so a broker that comes back is used again without a restart.
    """

    def __init__(self, *, logger: logging.Logger, retry_after_sec: float, report_every_sec: float) -> None:
        self._log = logger
        self._retry_after_sec = float(retry_after_sec)
        self._report_every_sec = float(report_every_sec)
        #: While the broker is known to be unreachable, when a command may be tried again.
        self._retry_commands_at = 0.0
        #: When *this* outage may be reported again, so one dead broker is not one log line
        #: per attempt for as long as it stays dead. Reset by `cleared`, because the next
        #: outage is a different diagnosis and must not inherit this one's silence.
        self._report_at = 0.0
        #: Whether a failure is currently standing. What makes the first report of each
        #: outage carry its cause, and what makes a recovery worth a line.
        self._standing = False

    def stands(self, deadline: float) -> bool:
        """Whether no command may be issued yet; the caller's remaining timeout is spent if so."""
        if time.monotonic() >= self._retry_commands_at:
            return False
        wait_out(deadline)
        return True

    def record(self, deadline: float, message: str, *args: Any) -> None:
        """Note a failure, report it at most on the interval, and wait out the caller.

        The message belongs to the backend rather than to this policy: what an outage costs
        — a report that is redelivered, one that is gone, an envelope that waits — differs
        per seam, and an operator reading one line should be told which stream stopped and
        what the failure said.

        The first record of each outage is reported immediately and with its traceback, even
        inside the interval of an earlier outage's report. A gate that spanned outages made
        the report worse than useless: the second failure — a different one, on a different
        key, for a different reason — was logged nowhere, and the line the operator did have
        named the first cause without saying it had changed.
        """
        now = time.monotonic()
        self._retry_commands_at = now + self._retry_after_sec
        first, self._standing = not self._standing, True
        if first or now >= self._report_at:
            self._report_at = now + self._report_every_sec
            self._log.warning(message, *args, exc_info=first)
        wait_out(deadline)

    def cleared(self, message: str, *args: Any) -> None:
        """The broker answered again: forget the outage, and say so if there was one.

        Called on the success path of every poll, and cheap when nothing was standing. Two
        things depend on it. The report gate is per outage rather than per process, so the
        next failure is reported with its own cause; and an operator who was told a tier had
        stopped consuming is told when it started again, which is the half of the story a
        recovery-free policy never tells.
        """
        if not self._standing:
            return
        self._standing = False
        self._retry_commands_at = 0.0
        self._report_at = 0.0
        self._log.warning(message, *args)


def is_missing_group(error: BaseException) -> bool:
    """Whether a reply error says the group (or the stream under it) is gone."""
    return "NOGROUP" in str(error) or "no such key" in str(error).lower()


def is_existing_group(error: BaseException) -> bool:
    """Whether a group creation failed because the group is already there."""
    return "BUSYGROUP" in str(error)


__all__ = [
    "BROKER_REPORT_INTERVAL_SEC",
    "BROKER_RETRY_SEC",
    "BrokerOutage",
    "block_ms",
    "entry_key",
    "first_entry",
    "is_existing_group",
    "is_missing_group",
    "next_entry_id",
    "row",
    "text",
    "wait_out",
]
