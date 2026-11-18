"""The outbox reader loop every transport runs, in one place rather than three.

It never closes the reader: releasing the subscription belongs to the caller
that also stops and joins the thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from transport.interfaces import DeliveredMessage, IOutboxReader

log = logging.getLogger(__name__)

#: Added to a transport's poll timeout to size its reader join: long enough for
#: the reader to finish the poll it is in and settle what it took.
READER_JOIN_TIMEOUT_MARGIN_SEC = 2.0


def run_reader_loop(
    reader: IOutboxReader,
    stop_event: threading.Event,
    handle: Callable[[IOutboxReader, DeliveredMessage], None],
    *,
    name: str,
    poll_timeout_sec: float,
) -> None:
    """Poll ``reader`` until ``stop_event``, handing each delivery to ``handle``.

    ``handle`` is a plain callable rather than an interface because it is the
    transport's own method, called from the transport's own thread — the loop is
    a local, not a layer boundary.
    """
    while not stop_event.is_set():
        try:
            delivered = reader.poll(poll_timeout_sec)
        except Exception:
            # Consuming past a broken backend would be guesswork, and leaving the
            # subscription open would make it a write-only sink every publish
            # still fans into — so the caller's close is the next thing to run.
            log.error("%s outbox reader failed; the transport stops consuming", name, exc_info=True)
            return
        if delivered is None:
            continue
        try:
            handle(reader, delivered)
        except Exception:
            # Acknowledged, not negatively: a message nobody could handle would
            # otherwise hold the in-flight slot and stall everything behind it.
            log.warning("%s outbound handling failed", name, exc_info=True)
            try:
                reader.ack(delivered)
            except Exception:
                # The settle is the one call here that can still reach a broker that
                # is not there. Letting it out would end the loop over a message that
                # had already failed: the entry keeps its place in the pending list and
                # the reclaim takes it, so the next poll is the recovery.
                log.warning("%s could not settle a failed delivery", name, exc_info=True)


__all__ = ["READER_JOIN_TIMEOUT_MARGIN_SEC", "run_reader_loop"]
