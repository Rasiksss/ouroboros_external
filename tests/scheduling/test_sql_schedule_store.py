"""The SQL store decides ownership, revision CAS and slot claims inside its statements."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from ouroboros.scheduling.store import (
    ConcurrentUpdateError,
    ScheduleAlreadyExistsError,
    ScheduleCorruptionError,
    ScheduleUnreadableError,
    StorageError,
)


def _schedule(schedule_id: str | None = None, **extra) -> dict:
    value = {
        "id": schedule_id or str(uuid.uuid4()),
        "state": "pending",
        "execution_kind": "reminder",
        "what": "ping",
        "recurrence": "once",
        "timezone": "Europe/Moscow",
        "at": "2030-01-01T10:00:00+00:00",
        "next_run_at": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    value.update(extra)
    return value


#: Every claim these tests write counts as live, so the due scan hides it from the next call.
_FRESH = "2000-01-01T00:00:00+00:00"
#: The clock `_claim_all` stamps its claims with, so a heartbeat that moved one is visible.
_CLAIMED_AT = "2026-06-01T00:00:00+00:00"


def _claim_all(store):
    return store.claim_due_slots(_CLAIMED_AT, limit=10, stale_before_iso=_FRESH).claimed


def _ids(claimed) -> list[str]:
    return [entry.schedule.value["id"] for entry in claimed]


def test_create_and_get_round_trip(store, make_scope):
    """A created row reads back whole, with revision one as its token."""
    scope = make_scope()
    created = store.create_schedule(_schedule(what="напомни про отчёт"), scope=scope)
    stored = store.get_schedule(created.value["id"], scope=scope)
    assert stored is not None
    assert stored.token == "1"
    assert stored.value["what"] == "напомни про отчёт"


def test_create_refuses_a_duplicate_id(store, make_scope):
    """The second insert of one id is refused, not overwritten."""
    scope = make_scope()
    value = _schedule()
    store.create_schedule(value, scope=scope)
    with pytest.raises(ScheduleAlreadyExistsError):
        store.create_schedule(value, scope=scope)


def test_another_owners_id_reads_as_missing(store, make_scope):
    """Ownership rides in the statement: get and list under another owner see nothing."""
    created = store.create_schedule(_schedule(), scope=make_scope(owner="owner-a"))
    other = make_scope(owner="owner-b")
    assert store.get_schedule(created.value["id"], scope=other) is None
    assert store.list_schedules(scope=other) == []


def test_another_conversations_id_reads_as_missing(store, make_scope):
    created = store.create_schedule(_schedule(), scope=make_scope(chat="100"))
    other = make_scope(chat="200")

    assert store.get_schedule(created.value["id"], scope=other) is None
    assert store.list_schedules(scope=other) == []
    assert store.count_schedules_by_state(scope=other) == {}
    with pytest.raises(StorageError, match="not found"):
        store.replace_schedule(dict(created.value), expected_token="1", scope=other)

    own = make_scope(chat="100")
    assert store.get_schedule(created.value["id"], scope=own) is not None
    assert [row.value["id"] for row in store.list_schedules(scope=own)] == [created.value["id"]]
    assert store.count_schedules_by_state(scope=own) == {"pending": 1}


def test_replace_requires_the_expected_revision(store, make_scope):
    """A stale token loses: the CAS matches no row and the value stays the winner's."""
    scope = make_scope()
    created = store.create_schedule(_schedule(), scope=scope)
    value = dict(created.value)
    value["what"] = "updated"
    replaced = store.replace_schedule(value, expected_token=created.token, scope=scope)
    assert replaced.token == "2"
    with pytest.raises(ConcurrentUpdateError):
        store.replace_schedule(dict(value), expected_token=created.token, scope=scope)


def test_replace_under_another_owner_reads_as_missing(store, make_scope):
    """A replace by the wrong owner fails as not-found, never as a concurrent update."""
    created = store.create_schedule(_schedule(), scope=make_scope(owner="owner-a"))
    with pytest.raises(StorageError, match="not found"):
        store.replace_schedule(dict(created.value), expected_token="1", scope=make_scope(owner="owner-b"))


def test_replace_of_a_missing_schedule_raises(store, make_scope):
    with pytest.raises(StorageError, match="not found"):
        store.replace_schedule(_schedule(), expected_token="1", scope=make_scope())


def test_the_due_batch_is_cross_owner_and_takes_only_active_due_rows(store, make_scope):
    """The due query is the one deliberately cross-owner read, and takes only due active rows."""
    due_a = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope(owner="owner-a")
    )
    due_b = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T11:00:00+00:00"), scope=make_scope(owner="owner-b")
    )
    store.create_schedule(
        _schedule(state="active", next_run_at="2027-01-01T10:00:00+00:00"), scope=make_scope(owner="owner-a")
    )
    store.create_schedule(
        _schedule(state="pending", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope(owner="owner-a")
    )
    batch = store.claim_due_slots("2026-06-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH)
    assert _ids(batch.claimed) == [due_a.value["id"], due_b.value["id"]]
    assert (batch.lost, batch.undecodable) == ([], [])


def test_a_subsecond_slot_is_still_ordered_against_a_whole_second_now(store, make_scope):
    """`next_run_at` carries a fraction only when it has one, so both shapes are compared as text."""
    fractional = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00.500000+00:00"), scope=make_scope()
    )
    assert store.claim_due_slots("2026-01-01T10:00:00+00:00", limit=10, stale_before_iso=_FRESH).claimed == []
    taken = store.claim_due_slots("2026-01-01T10:00:01+00:00", limit=10, stale_before_iso=_FRESH).claimed
    assert _ids(taken) == [fractional.value["id"]]
    # The slot id truncates the fraction, so one slot has one identity however it was written.
    assert [entry.slot_id for entry in taken] == ["1767261600"]


def test_a_live_claim_hides_the_slot_from_the_next_pass(store, make_scope):
    """A peer must not spend its limit on a slot the holder is still delivering."""
    created = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    now = "2026-06-01T00:00:00+00:00"

    first = store.claim_due_slots(now, limit=10, stale_before_iso=_FRESH)
    second = store.claim_due_slots(now, limit=10, stale_before_iso=_FRESH)

    assert _ids(first.claimed) == [created.value["id"]] and first.lost == []
    assert (second.claimed, second.lost) == ([], [])
    receipt = store.get_receipt(created.value["id"], first.claimed[0].slot_id)
    assert receipt is not None and (receipt.status, receipt.claimed_at) == ("claimed", now)


def test_the_due_batch_is_bounded_by_its_limit(store, make_scope):
    """A backlog after downtime must not become one long serial pass on the worker."""
    for index in range(4):
        store.create_schedule(
            _schedule(state="active", next_run_at=f"2026-01-01T1{index}:00:00+00:00"), scope=make_scope()
        )
    now = "2026-06-01T00:00:00+00:00"
    assert len(store.claim_due_slots(now, limit=2, stale_before_iso=_FRESH).claimed) == 2
    assert len(store.claim_due_slots(now, limit=10, stale_before_iso=_FRESH).claimed) == 2


def test_an_unreadable_row_is_reported_without_costing_the_batch(store, make_scope, engine):
    """A chat-key rotation makes every payload undecodable at once; one row must not stop the pass."""
    broken = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    healthy = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T11:00:00+00:00"), scope=make_scope()
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET payload_json='not json' WHERE schedule_id=:id"), {"id": broken.value["id"]}
        )

    batch = store.claim_due_slots("2026-06-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH)

    assert batch.undecodable == [broken.value["id"]]
    assert _ids(batch.claimed) == [healthy.value["id"]]
    assert store.get_receipt(broken.value["id"], "1767261600") is None


def test_a_payload_this_process_has_no_key_for_is_not_treated_as_corruption(store, make_scope, engine):
    """Parking on it would let one misconfigured pod stop every owner's schedules fleet-wide."""
    from ouroboros.scheduling.store import ScheduleUnreadableError

    created = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET payload_json='enc:v1:AAAA' WHERE schedule_id=:id"), {"id": created.value["id"]}
        )

    batch = store.claim_due_slots("2026-06-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH)

    assert (batch.claimed, batch.lost, batch.undecodable) == ([], [], [])
    with pytest.raises(ScheduleUnreadableError):
        store.get_for_dispatch(created.value["id"])


def _claimed_elsewhere(engine, schedule_id: str, slot_id: str) -> None:
    """A receipt another pass holds: same schedule, a different slot."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schedule_runs (schedule_id, slot_id, owner_id, agent_id, schedule_revision, "
                "status, claimed_at, finished_at, error, permanent) VALUES (:s, :t, 'owner-a', 'main', 1, "
                "'claimed', :at, '', '', 0)"
            ),
            {"s": schedule_id, "t": slot_id, "at": _CLAIMED_AT},
        )


def test_the_heartbeat_restamps_this_passs_own_claim(store, make_scope):
    """The positive half: without it a predicate that matches nothing would ship green."""
    created = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    (taken,) = _claim_all(store)

    store.refresh_claims([(created.value["id"], taken.slot_id)], now_iso="2027-01-01T00:00:00+00:00")

    assert store.get_receipt(created.value["id"], taken.slot_id).claimed_at == "2027-01-01T00:00:00+00:00"


def test_a_row_is_decoded_under_its_own_key_not_the_payloads(store, make_scope, engine):
    """Receipts are addressed by the column, so a payload that disagreed would silently no-op them."""
    created = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET payload_json=:p WHERE schedule_id=:id"),
            {
                "p": json.dumps({**_schedule(schedule_id="00000000-0000-0000-0000-000000000000"), "revision": 1}),
                "id": created.value["id"],
            },
        )

    assert store.get_for_dispatch(created.value["id"]).value["id"] == created.value["id"]


@pytest.fixture
def two_claimed_plus_a_stranger(store, make_scope, engine):
    """This pass holds (first, its slot) and (second, its slot); another pass holds (first, second's slot)."""
    first = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    second = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T11:00:00+00:00"), scope=make_scope()
    )
    mine = {e.schedule.value["id"]: e.slot_id for e in _claim_all(store)}
    _claimed_elsewhere(engine, first.value["id"], mine[second.value["id"]])
    return store, first, second, mine


def test_releasing_a_pass_leaves_another_passs_claim_alone(two_claimed_plus_a_stranger):
    """Two `IN` lists match their cross product, which would hand back a slot someone is delivering."""
    store, first, second, mine = two_claimed_plus_a_stranger

    store.release_claims([(first.value["id"], mine[first.value["id"]]), (second.value["id"], mine[second.value["id"]])])

    assert store.get_receipt(first.value["id"], mine[first.value["id"]]) is None
    assert store.get_receipt(second.value["id"], mine[second.value["id"]]) is None
    assert store.get_receipt(first.value["id"], mine[second.value["id"]]) is not None


def test_the_heartbeat_leaves_another_passs_claim_alone(two_claimed_plus_a_stranger):
    """The same cross product would keep a peer's claim alive and hide its row from every replica."""
    store, first, second, mine = two_claimed_plus_a_stranger

    store.refresh_claims([(first.value["id"], mine[first.value["id"]])], now_iso="2027-01-01T00:00:00+00:00")

    stranger = store.get_receipt(first.value["id"], mine[second.value["id"]])
    assert stranger.claimed_at == _CLAIMED_AT


def test_a_row_this_process_cannot_decrypt_is_left_for_a_peer(store, make_scope, engine):
    """Never parked — only this reader is broken — and never retried around: a peer with the key takes it."""
    blocked = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET payload_json='enc:v1:AAAA' WHERE schedule_id=:id"), {"id": blocked.value["id"]}
        )

    batch = store.claim_due_slots("2026-06-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH)

    assert (batch.claimed, batch.lost, batch.undecodable) == ([], [], [])
    assert store.get_receipt(blocked.value["id"], "1767261600") is None


def test_a_listing_survives_one_row_it_cannot_decode(store, make_scope, engine):
    """A listing that raised would leave the owner unable to see, create or cancel anything."""
    scope = make_scope()
    good = store.create_schedule(_schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=scope)
    broken = store.create_schedule(_schedule(state="active", next_run_at="2026-01-01T11:00:00+00:00"), scope=scope)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET payload_json='not json' WHERE schedule_id=:id"), {"id": broken.value["id"]}
        )

    listed = store.list_schedules(scope=scope)

    assert [entry.schedule_id for entry in listed] == [good.value["id"]]


def test_a_listing_still_refuses_when_this_reader_has_no_key(store, make_scope, engine):
    """An empty page would tell the owner they have no schedules while a peer reads every one."""
    created = store.create_schedule(_schedule(state="active"), scope=make_scope())
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET payload_json='enc:v1:AAAA' WHERE schedule_id=:id"), {"id": created.value["id"]}
        )

    with pytest.raises(ScheduleUnreadableError):
        store.list_schedules(scope=make_scope())


def test_a_payload_that_fails_for_any_other_reason_is_corruption(store, make_scope, monkeypatch):
    """Only key trouble belongs to the reader; everything else is the row's, and has to be parked."""
    from ouroboros.scheduling import store as store_module

    created = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    monkeypatch.setattr(store_module, "decrypt_field", lambda stored: (_ for _ in ()).throw(ValueError("mangled")))

    batch = store.claim_due_slots("2026-06-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH)

    assert batch.undecodable == [created.value["id"]]


def test_a_claim_whose_timestamp_is_not_a_timestamp_does_not_hide_its_row(store, make_scope, engine):
    """`claimed_at` is compared as text, so garbage would sort above every horizon and hide it forever."""
    created = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    store.claim_due_slots("2026-06-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedule_runs SET claimed_at='oops' WHERE schedule_id=:id"), {"id": created.value["id"]}
        )

    batch = store.claim_due_slots("2026-06-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH)

    assert [entry.schedule.value["id"] for entry in batch.lost] == [created.value["id"]]


def test_a_slot_whose_timestamp_will_not_parse_is_reported_not_raised(store, make_scope, engine):
    """Left to raise, the row would keep a past slot and be re-selected on every pass on every replica."""
    created = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET next_run_at='2026-01-01T99:99:99+00:00' WHERE schedule_id=:id"),
            {"id": created.value["id"]},
        )

    batch = store.claim_due_slots("2026-06-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH)

    assert batch.undecodable == [created.value["id"]]
    assert batch.claimed == []


def test_update_receipt_requires_an_existing_row(store, make_scope):
    created = store.create_schedule(_schedule(), scope=make_scope())
    with pytest.raises(StorageError, match="missing schedule receipt"):
        store.update_receipt(created.value["id"], "1700000000", status="dispatched", finished_at="")


def test_a_backend_failure_leaves_as_storage_error_without_the_driver_message(store, make_scope, engine):
    """A driver message names the host and the user, and a tool result reaches the model."""
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE schedules")
    with pytest.raises(StorageError) as failure:
        store.list_schedules(scope=make_scope())
    assert "schedule storage is unavailable" in str(failure.value)
    assert "no such table" not in str(failure.value)


def test_an_undecodable_payload_is_reported_as_corruption(store, make_scope, engine):
    """A key rotation makes the decrypt raise `InvalidTag`, which is not a `ValueError`."""
    created = store.create_schedule(_schedule(), scope=make_scope())
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET payload_json='not json' WHERE schedule_id=:id"),
            {"id": created.value["id"]},
        )
    with pytest.raises(ScheduleCorruptionError):
        store.get_schedule(created.value["id"], scope=make_scope())


def test_a_parked_row_reads_as_parked_to_its_owner(store, make_scope):
    """The tick stops selecting it by column; a listing that still said `active` would lie."""
    scope = make_scope()
    created = store.create_schedule(_schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=scope)

    store.park_undecodable(created.value["id"], now_iso="2026-06-01T00:00:00+00:00")

    assert store.claim_due_slots("2027-01-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH).claimed == []
    owner_view = store.get_schedule(created.value["id"], scope=scope)
    assert owner_view.value["state"] == "needs_attention"
    assert owner_view.value["next_run_at"] is None
    assert store.list_schedules(scope=scope)[0].value["state"] == "needs_attention"


def test_the_retention_sweep_removes_settled_receipts_and_keeps_live_claims(store, make_scope):
    """A claim still in flight is not swept: its dispatch may be mid-delivery."""
    settled = store.create_schedule(
        _schedule(state="active", next_run_at="2026-01-01T10:00:00+00:00"), scope=make_scope()
    )
    live = store.create_schedule(_schedule(state="active", next_run_at="2026-01-01T11:00:00+00:00"), scope=make_scope())
    slots = {
        entry.schedule.value["id"]: entry.slot_id
        for entry in store.claim_due_slots("2026-06-01T00:00:00+00:00", limit=10, stale_before_iso=_FRESH).claimed
    }
    store.update_receipt(
        settled.value["id"],
        slots[settled.value["id"]],
        status="dispatched",
        finished_at="2026-01-01T00:00:00+00:00",
    )

    assert (
        store.sweep_runs(
            now_iso="2026-06-01T00:00:00+00:00",
            stale_before_iso=_FRESH,
            cutoff_iso="2026-06-01T00:00:00+00:00",
            limit=10,
        )
        == 1
    )
    assert store.get_receipt(settled.value["id"], slots[settled.value["id"]]) is None
    assert store.get_receipt(live.value["id"], slots[live.value["id"]]) is not None


def test_replace_for_dispatch_carries_no_owner_predicate(store, make_scope):
    """The tick has no caller, so its replace matches the row by id and revision alone."""
    created = store.create_schedule(_schedule(state="active"), scope=make_scope(owner="owner-a"))
    value = dict(created.value)
    value["state"] = "completed"
    replaced = store.replace_for_dispatch(value, expected_token=created.token)
    assert replaced.token == "2"
    refreshed = store.get_schedule(created.value["id"], scope=make_scope(owner="owner-a"))
    assert refreshed is not None and refreshed.value["state"] == "completed"
