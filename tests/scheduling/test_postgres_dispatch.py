"""The PostgreSQL half of the due batch: replicas diverge, and the receipt still bounds delivery."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import create_engine, event, make_url, text

from infra.migrations.runner import upgrade_head
from new_core.identity import OwnerScope
from ouroboros.scheduling.store import SqlScheduleStore

#: Older than anything the other suites write, so this test's rows sort first in the due scan
#: and a shared stand's leftovers cannot crowd them out of a small batch.
_SLOT = "2000-01-01T0{}:00:00+00:00"
_NOW = "2001-01-01T00:00:00+00:00"
#: Before every claim these tests write, so a live claim really does hide its row.
_STALE_BEFORE = "2000-06-01T00:00:00+00:00"


@pytest.fixture
def engine():
    from tests.helpers.postgres import postgres_urls

    url = make_url(postgres_urls()[0])
    options = f"{url.query.get('options', '')} -c statement_timeout=2000".strip()
    engine = create_engine(url.update_query_dict({"options": options}))
    upgrade_head(engine)
    written: list[str] = []
    yield engine, written
    with engine.begin() as conn:
        for schedule_id in written:
            conn.execute(text("DELETE FROM schedule_runs WHERE schedule_id=:id"), {"id": schedule_id})
            conn.execute(text("DELETE FROM schedules WHERE schedule_id=:id"), {"id": schedule_id})
    engine.dispose()


def _due_rows(store: SqlScheduleStore, written: list[str], count: int, *, scope: OwnerScope | None = None) -> list[str]:
    scope = scope or OwnerScope(
        owner_id=f"pg-{uuid.uuid4().hex[:8]}", agent_id="main", surface_id="dialogs", chat_id="100"
    )
    for index in range(count):
        schedule_id = str(uuid.uuid4())
        store.create_schedule(
            {
                "id": schedule_id,
                "state": "active",
                "execution_kind": "reminder",
                "what": "ping",
                "recurrence": "once",
                "timezone": "Europe/Moscow",
                "at": _SLOT.format(index + 1),
                "next_run_at": _SLOT.format(index + 1),
                "created_at": _NOW,
                "updated_at": _NOW,
            },
            scope=scope,
        )
        written.append(schedule_id)
    return written[-count:]


def _lock(conn, schedule_ids: list[str]) -> None:
    """What another replica's open claim transaction holds while it works."""
    held = conn.execute(
        text("SELECT schedule_id FROM schedules WHERE schedule_id = ANY(:ids) FOR UPDATE"),
        {"ids": schedule_ids},
    ).scalars()
    assert sorted(held) == sorted(schedule_ids)


def test_a_batch_a_peer_holds_is_skipped_rather_than_waited_on(engine) -> None:
    """`SKIP LOCKED` is the whole point of the Postgres path: two pods take disjoint batches."""
    connection, written = engine
    store = SqlScheduleStore(connection)
    rows = _due_rows(store, written, 3)

    with connection.begin() as peer:
        _lock(peer, rows[:2])
        taken = {
            entry.schedule.value["id"]
            for entry in store.claim_due_slots(_NOW, limit=1000, stale_before_iso=_STALE_BEFORE).claimed
        }

    assert taken.isdisjoint(rows[:2])
    assert rows[2] in taken
    # Once the peer commits, the rows it held are due again for whoever passes next.
    later = {
        entry.schedule.value["id"]
        for entry in store.claim_due_slots(_NOW, limit=1000, stale_before_iso=_STALE_BEFORE).claimed
    }
    assert set(rows[:2]) <= later


def test_a_held_row_does_not_spend_the_batch_limit(engine) -> None:
    """PostgreSQL locks below the limit, so a replica whose peers hold the head still fills a batch.

    Were it the other way round, a busy deployment would starve the replicas rather than spread
    the work over them, and `OUROBOROS_SCHEDULER_DUE_BATCH_LIMIT` would not mean what it says.
    """
    connection, written = engine
    store = SqlScheduleStore(connection)
    rows = _due_rows(store, written, 5)

    with connection.begin() as peer:
        _lock(peer, rows[:2])
        taken = [
            entry.schedule.value["id"]
            for entry in store.claim_due_slots(_NOW, limit=3, stale_before_iso=_STALE_BEFORE).claimed
        ]

    assert taken == rows[2:]


def test_the_receipt_still_bounds_delivery_when_both_scans_see_the_row(engine) -> None:
    """After the holder commits, both scans return the row and only one claim inserts."""
    connection, written = engine
    first, second = SqlScheduleStore(connection), SqlScheduleStore(connection)
    (row,) = _due_rows(first, written, 1)

    won = first.claim_due_slots(_NOW, limit=1000, stale_before_iso=_STALE_BEFORE)
    # Horizon after the claim, so the hiding clause stands aside and both scans see the row.
    lost = second.claim_due_slots(_NOW, limit=1000, stale_before_iso=_NOW)

    assert row in {entry.schedule.value["id"] for entry in won.claimed}
    assert row in {entry.schedule.value["id"] for entry in lost.lost}
    assert row not in {entry.schedule.value["id"] for entry in lost.claimed}


def test_deletion_transaction_excludes_concurrent_claims_before_and_after_commit(engine) -> None:
    connection, written = engine
    store = SqlScheduleStore(connection)
    scope = OwnerScope(owner_id=f"pg-{uuid.uuid4().hex[:8]}", agent_id="main", surface_id="dialogs", chat_id="100")
    (schedule_id,) = _due_rows(store, written, 1, scope=scope)
    stored = store.get_for_dispatch(schedule_id)
    deleted, commit = Event(), Event()

    def hold_transaction(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM schedules"):
            deleted.set()
            assert commit.wait(10), "delete transaction was never released"

    event.listen(connection, "after_cursor_execute", hold_transaction)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            deletion = pool.submit(store.delete_schedule, schedule_id, revision=int(stored.token), scope=scope)
            try:
                assert deleted.wait(10), "delete never acquired its row"
                assert schedule_id not in {
                    slot.schedule.schedule_id
                    for slot in store.claim_due_slots(_NOW, limit=1000, stale_before_iso=_STALE_BEFORE).claimed
                }
            finally:
                commit.set()
            deletion.result(timeout=10)
        assert store.get_for_dispatch(schedule_id) is None
        assert schedule_id not in {
            slot.schedule.schedule_id
            for slot in store.claim_due_slots(_NOW, limit=1000, stale_before_iso=_STALE_BEFORE).claimed
        }
    finally:
        event.remove(connection, "after_cursor_execute", hold_transaction)


def test_orphan_cleanup_skips_locked_receipts_and_respects_the_batch_limit(engine) -> None:
    connection, written = engine
    store = SqlScheduleStore(connection)
    scope = OwnerScope(owner_id=f"pg-{uuid.uuid4().hex[:8]}", agent_id="main", surface_id="dialogs", chat_id="100")
    orphans = _due_rows(store, written, 3, scope=scope)
    claims = store.claim_due_slots(_NOW, limit=1000, stale_before_iso=_STALE_BEFORE).claimed
    keys = [slot.key for slot in claims if slot.schedule.schedule_id in orphans]
    for schedule_id in orphans:
        store.delete_schedule(schedule_id, revision=1, scope=scope)
    _due_rows(store, written, 1)
    with connection.begin() as peer:
        peer.execute(text("SELECT 1 FROM schedule_runs WHERE schedule_id=:id FOR UPDATE"), {"id": orphans[0]})
        store.sweep_runs(now_iso=_NOW, stale_before_iso=_NOW, cutoff_iso=_STALE_BEFORE, limit=1)
        assert store.get_receipt(*next(key for key in keys if key.schedule_id == orphans[0])).status == "claimed"
        assert sum(store.get_receipt(*key).status == "needs_attention" for key in keys) == 1
