"""The inbound journal a transport replays after a crash.

A transport persists the user's message before handing it to the seam and marks
the row processed after, so a row left unprocessed is one this process never
handed over. Every transport keeps that journal in the same table and replays it
the same way, and a sweep fixed in one transport but not the other leaves the
other silently unrecovered — so the sweep lives here and each transport supplies
only the part that is its own: how a stored row becomes its envelope.

The sweep is **deployment-wide**: it walks every owner/agent pair that has rows
and reads each under that stored pair. An instance serves many owners whose
identity comes from a trusted header, so a sweep addressing the instance's
configured owner matches nothing for any of them — silently, because a sweep that
recovered nothing and a sweep that addressed nobody used to log the same nothing.

It also accepts **duplicate replay across instances** rather than claiming rows.
Every instance boots and sweeps the same rows, and the duplicates collapse: the
envelope carries the row's own client message id, the orchestrator derives the
task id from it, and a task id already past ``requested`` returns the existing
record. N submits become one task. A claim would need a column, a migration, a
lease and a reaper — durable machinery to save a handful of idempotent submits.
"""

from __future__ import annotations

import logging
import pathlib

from ouroboros.contracts.attachments import AttachmentMetadata
from ouroboros.storage.chat_store import mark_chat_message_processed
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.runtime_persistence import runtime_persistence
from transport.interfaces import IEnvelopeBuilder, IMessageGateway

log = logging.getLogger(__name__)

#: How far back a sweep looks **per owner**, over the rows never handed over.
DEFAULT_SWEEP_LIMIT = 200
#: How many rows one pass will *attempt* across every pair — attempts, never successes.
DEFAULT_SWEEP_BUDGET = 1000
#: How many pairs one enumeration round trip carries — a page, never a ceiling.
DEFAULT_SWEEP_PAIR_PAGE = 1000
#: How many per-pair read failures one pass reports with their cause before it only counts them.
_READ_FAILURES_IN_FULL = 3

#: The marker every transport writes on the row it is about to hand over; the
#: sweep looks for exactly this.
UNPROCESSED_METADATA: dict[str, bool] = {"inbound_processed": False}


def stored_attachments(record: ConversationMessage) -> tuple[list[AttachmentMetadata], list[str]]:
    """The attachments a stored row carries, and their ids."""
    attachments = record.attachments
    return attachments, [str(value["id"]) for value in attachments if value.get("id")]


def unprocessed_records(
    *,
    owner_id: str,
    agent_id: str,
    source: str,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> list[ConversationMessage]:
    """One stored pair's rows this transport persisted but never handed over, in every
    conversation the pair holds.

    The pair is passed in, never derived from configuration: the owner an instance
    is configured with is a constant, and the rows carry the owner a trusted header
    named. ``service_identity`` is deliberately not reachable from this module — it
    is the exact call whose owner is that constant.
    """
    return runtime_persistence.current.conversations_repo.list_pair_messages(
        owner_id=owner_id,
        agent_id=agent_id,
        limit=limit,
        actor_type="owner",
        source=source,
        newest_first=True,
        unprocessed_only=True,
    )


def mark_processed(drive_root: pathlib.Path, message_id: str) -> None:
    """Mark a journal row handed over; a failure here only risks one replay."""
    try:
        mark_chat_message_processed(drive_root, message_id)
    except Exception:
        log.warning("Failed to mark chat message %s processed", message_id, exc_info=True)


def replay_unprocessed(
    gateway: IMessageGateway,
    drive_root: pathlib.Path,
    *,
    source: str,
    build: IEnvelopeBuilder,
    limit: int = DEFAULT_SWEEP_LIMIT,
    budget: int = DEFAULT_SWEEP_BUDGET,
    pair_page: int = DEFAULT_SWEEP_PAIR_PAGE,
) -> int:
    """Submit again every message this transport persisted but never handed over.

    Every owner, one at a time: the read predicate names an owner, so the sweep
    walks the pairs the rows themselves carry rather than widening the predicate.

    At-least-once is safe because the stored identity is derived from the client
    message id *and the ownership the row is stored under*: a resubmitted message
    lands on the same row, and it cannot land on another owner's — a client message
    id is unique within an owner and not across owners.
    """
    submitted = 0
    attempted = 0
    walked = 0
    windowed = 0
    unreadable = 0
    stop_reason = ""
    unwalked_pair = False
    enumerated_to_the_end = False
    cursor: tuple[str, str] | None = None
    while True:
        try:
            page = runtime_persistence.current.conversations_repo.list_owner_agent_pairs(
                limit=pair_page, start_after=cursor
            )
        except Exception:
            log.warning("Failed to enumerate owners for the %s journal sweep", source, exc_info=True)
            stop_reason = f"stopped after {walked} pair(s) on a pair page it could not read"
            break
        if not page:
            enumerated_to_the_end = True
            break
        enumerated_to_the_end = len(page) < pair_page
        cursor = page[-1]
        for owner_id, agent_id in page:
            if attempted >= budget:
                unwalked_pair = True
                break
            try:
                records = unprocessed_records(
                    owner_id=owner_id,
                    agent_id=agent_id,
                    source=source,
                    limit=limit + 1,
                )
            except Exception:
                unreadable += 1
                if unreadable <= _READ_FAILURES_IN_FULL:
                    log.warning(
                        "Failed to sweep unprocessed %s messages for %s/%s", source, owner_id, agent_id, exc_info=True
                    )
                continue
            walked += 1
            if len(records) > limit:
                windowed += 1
                records = records[len(records) - limit :]
            for record in records:
                if attempted >= budget:
                    stop_reason = f"stopped at its budget of {budget} row(s)"
                    break
                attempted += 1
                if record.metadata.get("legacy_image") is True:
                    continue
                try:
                    gateway.inbox.submit(build.build(record))
                    mark_processed(drive_root, record.message_id)
                except Exception:
                    log.warning("Resubmitting %s message %s failed", source, record.message_id, exc_info=True)
                    continue
                submitted += 1
            if stop_reason:
                break
        if stop_reason or attempted >= budget:
            break
    if not stop_reason and attempted >= budget:
        if unwalked_pair:
            stop_reason = f"stopped at its budget of {budget} row(s) before it walked every pair"
        elif not enumerated_to_the_end:
            stop_reason = f"spent its budget of {budget} row(s) without proving it had reached the last pair"
    # Unconditional: a sweep that recovered nothing and a sweep that addressed
    # nobody are the two states this report exists to tell apart.
    log.info("Swept %d owner(s) for unprocessed %s messages; resubmitted %d", walked, source, submitted)
    if stop_reason:
        log.warning(
            "The %s journal sweep %s; the rest stay unprocessed for the next pass",
            source,
            stop_reason,
        )
    if windowed:
        log.warning(
            "The %s journal sweep filled its per-pair window of %d row(s) on %d pair(s); their remainder waits",
            source,
            limit,
            windowed,
        )
    if unreadable > _READ_FAILURES_IN_FULL:
        log.warning(
            "The %s journal sweep could not read %d pair(s); the first %d carry the cause",
            source,
            unreadable,
            _READ_FAILURES_IN_FULL,
        )
    return submitted


__all__ = [
    "DEFAULT_SWEEP_BUDGET",
    "DEFAULT_SWEEP_LIMIT",
    "DEFAULT_SWEEP_PAIR_PAGE",
    "UNPROCESSED_METADATA",
    "mark_processed",
    "replay_unprocessed",
    "stored_attachments",
    "unprocessed_records",
]
