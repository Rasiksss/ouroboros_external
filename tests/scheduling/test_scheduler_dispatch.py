"""Dispatch semantics over the real store: at-most-once, receipts, and failure outcomes."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sql_text

from ouroboros.scheduling.core import ScheduleValidationError, parse_iso_timestamp, utc_iso
from ouroboros.scheduling.dispatch import ScheduleDispatcher
from ouroboros.scheduling.service import SchedulerService
from ouroboros.scheduling.store import ConcurrentUpdateError
from tests.scheduling.conftest import RecordedChats

_NOW = 1_900_000_000.0


class _Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple] = []
        self.submitted: list[tuple] = []
        self.fail = fail

    def send_message(self, text: str, *, scope, transport: dict) -> None:
        if self.fail:
            raise RuntimeError("publish failed")
        self.sent.append((text, scope, transport))

    def submit_task(self, text: str, *, scope, client_message_id: str, transport: dict) -> None:
        self.submitted.append((text, scope, client_message_id, transport))


@pytest.fixture
def service(store, clock):
    return SchedulerService(store, timezone_for=lambda scope: "Europe/Moscow")


def _dispatcher(store, clock, recorder: _Recorder, chats=None) -> ScheduleDispatcher:
    return ScheduleDispatcher(
        store,
        chats=chats if chats is not None else RecordedChats(),
        send_message=recorder.send_message,
        submit_task=recorder.submit_task,
        batch_limit=200,
    )


def _peer_claims(store, clock) -> str:
    """A peer instance took the slot through the same statement and then stopped reporting."""
    taken = store.claim_due_slots(utc_iso(clock[0]), limit=200, stale_before_iso=utc_iso(0)).claimed
    assert len(taken) == 1
    return taken[0].slot_id


def _ctx(make_scope, **kwargs):
    return SimpleNamespace(scope=make_scope(**kwargs))


#: The last reminder `_due_reminder` made, for tests that assert on a row they did not name.
_ID_HOLDER: list[str] = [""]


def _due_reminder(service, make_scope, clock) -> dict:
    """A one-time reminder that is active and already due once the clock advances."""
    created = service.create_reminder(_ctx(make_scope), message="ping", recurrence="once", at=utc_iso(_NOW + 60))
    assert created["state"] == "active"
    clock[0] = _NOW + 120
    _ID_HOLDER[0] = created["id"]
    return created


def test_a_due_slot_is_delivered_exactly_once_across_two_dispatchers(store, service, make_scope, clock):
    """The second replica's tick finds the slot spent and delivers nothing."""
    created = _due_reminder(service, make_scope, clock)
    first, second = _Recorder(), _Recorder()
    assert _dispatcher(store, clock, first)._tick() == 1
    assert _dispatcher(store, clock, second)._tick() == 0
    assert len(first.sent) == 1 and second.sent == []

    slot_id = str(int(parse_iso_timestamp(created["next_run_at"])))
    receipt = store.get_receipt(created["id"], slot_id)
    assert receipt is not None and receipt.status == "dispatched"
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed is not None and refreshed.value["state"] == "completed"


def test_a_lost_claim_with_a_fresh_receipt_takes_no_further_step(store, service, make_scope, clock):
    """While a peer's claim is fresh, the loser neither delivers nor rewrites the row."""
    created = _due_reminder(service, make_scope, clock)
    _peer_claims(store, clock)
    recorder = _Recorder()
    assert _dispatcher(store, clock, recorder)._tick() == 0
    assert recorder.sent == []
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed is not None and refreshed.value["state"] == "active"
    assert refreshed.value["next_run_at"] == created["next_run_at"]


def test_a_stale_claim_ends_the_slot_as_needs_attention(store, service, make_scope, clock):
    """A claim past the stale bound has an unknown outcome; the schedule stops for the owner."""
    created = _due_reminder(service, make_scope, clock)
    slot_id = _peer_claims(store, clock)
    clock[0] += 400
    recorder = _Recorder()
    _dispatcher(store, clock, recorder)._tick()
    assert recorder.sent == []
    receipt = store.get_receipt(created["id"], slot_id)
    assert receipt is not None and receipt.status == "needs_attention"
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed is not None
    assert refreshed.value["state"] == "needs_attention"
    assert refreshed.value["last_dispatch"]["error"] == receipt.error


def test_a_failed_delivery_marks_the_schedule_needs_attention(store, service, make_scope, clock):
    created = _due_reminder(service, make_scope, clock)
    slot_id = str(int(parse_iso_timestamp(created["next_run_at"])))
    _dispatcher(store, clock, _Recorder(fail=True))._tick()
    receipt = store.get_receipt(created["id"], slot_id)
    assert receipt is not None and receipt.status == "needs_attention"
    assert "publish failed" in receipt.error
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed is not None and refreshed.value["state"] == "needs_attention"


def test_an_agent_task_is_submitted_with_a_deterministic_client_message_id(store, service, make_scope, clock):
    """The slot is the idempotency key, so a repeated injection deduplicates at the inbox."""
    ctx = _ctx(make_scope)
    created = service.create_scheduled_task(
        ctx, instruction="проверь почту и составь сводку", recurrence="once", at=utc_iso(_NOW + 60)
    )
    confirmed = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    clock[0] = _NOW + 120
    recorder = _Recorder()
    assert _dispatcher(store, clock, recorder)._tick() == 1
    (text, scope, client_message_id, _transport) = recorder.submitted[0]
    slot_id = str(int(parse_iso_timestamp(confirmed["next_run_at"])))
    assert client_message_id == f"schedule:{created['id']}:{slot_id}"
    assert "проверь почту и составь сводку" in text
    assert scope.owner_id == "owner-a" and scope.chat_id == "100"


def test_an_unreadable_delivery_scope_ends_the_schedule_instead_of_retrying(store, service, make_scope, clock):
    """A row whose stored scope will not parse would fail identically forever, so it stops."""
    created = service.create_reminder(_ctx(make_scope), message="ping", recurrence="daily", time="10:00")
    confirmed = service.confirm_schedule(_ctx(make_scope), created["id"], revision=created["revision"])
    row = store.get_for_dispatch(created["id"])
    broken = dict(row.value)
    broken["delivery"] = {"scope": {"owner_id": "owner-a"}}
    store.replace_for_dispatch(broken, expected_token=row.token)
    clock[0] = parse_iso_timestamp(confirmed["next_run_at"]) + 10

    recorder = _Recorder()
    _dispatcher(store, clock, recorder)._tick()

    assert recorder.sent == []
    refreshed = store.get_for_dispatch(created["id"])
    # Recurring, but skipping to tomorrow would fail identically.
    assert refreshed.value["state"] == "needs_attention"
    assert refreshed.value["next_run_at"] is None


def test_a_retired_conversation_refuses_the_slot_permanently_and_delivers_nothing(
    store, service, make_scope, clock, chats
):
    ctx = _ctx(make_scope)
    created = service.create_reminder(ctx, message="дневной дайджест", recurrence="daily", time="10:00")
    confirmed = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    slot_id = str(int(parse_iso_timestamp(confirmed["next_run_at"])))
    clock[0] = parse_iso_timestamp(confirmed["next_run_at"]) + 10
    chats.record(ctx.scope, is_active=False)

    recorder = _Recorder()
    assert _dispatcher(store, clock, recorder, chats)._tick() == 0

    assert recorder.sent == [] and recorder.submitted == []
    receipt = store.get_receipt(created["id"], slot_id)
    assert receipt is not None and receipt.status == "needs_attention" and receipt.permanent
    assert "retired" in receipt.error
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "needs_attention"
    assert refreshed.value["next_run_at"] is None


def test_a_conversation_the_registry_holds_no_record_of_still_fires(store, service, make_scope, clock, chats):
    created = _due_reminder(service, make_scope, clock)

    recorder = _Recorder()
    assert _dispatcher(store, clock, recorder, chats)._tick() == 1

    assert [text for text, *_ in recorder.sent] == ["ping"]
    assert [scope.chat_id for scope in chats.read] == ["100"]
    assert store.get_for_dispatch(created["id"]).value["state"] == "completed"


def test_an_active_conversation_fires(store, service, make_scope, clock, chats):
    ctx = _ctx(make_scope)
    _due_reminder(service, make_scope, clock)
    chats.record(ctx.scope, is_active=True)

    recorder = _Recorder()
    assert _dispatcher(store, clock, recorder, chats)._tick() == 1

    assert [text for text, *_ in recorder.sent] == ["ping"]


def test_a_registry_read_that_raises_ends_the_slot_rather_than_the_pass(store, service, make_scope, clock, chats):
    created = _due_reminder(service, make_scope, clock)

    def refuse(scope):
        raise RuntimeError("the registry title will not decrypt")

    chats.get_chat = refuse

    recorder = _Recorder()
    assert _dispatcher(store, clock, recorder, chats)._tick() == 0

    assert recorder.sent == [] and recorder.submitted == []
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "active"
    assert refreshed.value["next_run_at"] == created["next_run_at"]


def test_a_retired_conversation_of_another_owner_does_not_stop_this_one(store, service, make_scope, clock, chats):
    _due_reminder(service, make_scope, clock)
    chats.record(make_scope(owner="owner-b", chat="100"), is_active=False)

    recorder = _Recorder()
    assert _dispatcher(store, clock, recorder, chats)._tick() == 1

    assert [text for text, *_ in recorder.sent] == ["ping"]


def test_a_transient_failure_keeps_a_recurring_schedule_alive(store, service, make_scope, clock):
    """A publish that failed must not kill a daily reminder; the slot's receipt keeps the failure."""
    ctx = _ctx(make_scope)
    created = service.create_reminder(ctx, message="дневной дайджест", recurrence="daily", time="10:00")
    confirmed = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    clock[0] = parse_iso_timestamp(confirmed["next_run_at"]) + 10

    _dispatcher(store, clock, _Recorder(fail=True))._tick()

    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "active"
    assert parse_iso_timestamp(refreshed.value["next_run_at"]) > clock[0]
    assert refreshed.value["last_dispatch"]["status"] == "needs_attention"


def test_an_unreadable_receipt_ends_the_slot_rather_than_leaving_it_open(store, service, make_scope, clock):
    """An unknown status is still an outcome: without one the row is re-selected every tick."""
    created = _due_reminder(service, make_scope, clock)
    slot_id = _peer_claims(store, clock)
    store.update_receipt(created["id"], slot_id, status="who knows", finished_at="x")

    recorder = _Recorder()
    _dispatcher(store, clock, recorder)._tick()

    assert recorder.sent == []
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "needs_attention"
    assert refreshed.value["next_run_at"] is None


def test_a_definition_that_stops_resolving_ends_the_schedule_after_delivery(store, service, make_scope, clock):
    """The delivery happened; freezing the row at a past slot would re-select it forever."""
    ctx = _ctx(make_scope)
    created = service.create_reminder(ctx, message="дайджест", recurrence="daily", time="10:00")
    confirmed = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    row = store.get_for_dispatch(created["id"])
    broken = dict(row.value)
    broken["timezone"] = "Mars/Olympus"
    store.replace_for_dispatch(broken, expected_token=row.token)
    clock[0] = parse_iso_timestamp(confirmed["next_run_at"]) + 10

    recorder = _Recorder()
    _dispatcher(store, clock, recorder)._tick()

    assert len(recorder.sent) == 1
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "needs_attention"
    assert refreshed.value["next_run_at"] is None
    assert "could not schedule the next run" in refreshed.value["last_dispatch"]["error"]


def test_a_claim_whose_timestamp_will_not_parse_ends_the_slot(store, service, make_scope, clock, engine):
    """It can never age out of the stale window, so it would be re-selected on every tick."""
    created = _due_reminder(service, make_scope, clock)
    _peer_claims(store, clock)
    with engine.begin() as conn:
        conn.execute(sql_text("UPDATE schedule_runs SET claimed_at='' WHERE schedule_id=:id"), {"id": created["id"]})

    recorder = _Recorder()
    _dispatcher(store, clock, recorder)._tick()

    assert recorder.sent == []
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "needs_attention"
    assert refreshed.value["next_run_at"] is None


def test_an_undecodable_row_is_parked_instead_of_failing_on_every_pass(store, service, make_scope, clock, engine):
    """A key rotation makes every row undecodable at once; left active they never leave the due query."""
    from sqlalchemy import text as sql

    created = _due_reminder(service, make_scope, clock)
    with engine.begin() as conn:
        conn.execute(sql("UPDATE schedules SET payload_json='broken' WHERE schedule_id=:id"), {"id": created["id"]})

    dispatcher = _dispatcher(store, clock, _Recorder())
    dispatcher._tick()

    assert store.claim_due_slots(utc_iso(clock[0]), limit=200, stale_before_iso=utc_iso(0)).claimed == []
    with engine.connect() as conn:
        state = conn.execute(sql("SELECT state FROM schedules WHERE schedule_id=:id"), {"id": created["id"]}).scalar()
    assert state == "needs_attention"


def test_a_peer_reading_a_permanent_failure_does_not_reschedule_it(store, service, make_scope, clock):
    """The winner writes the receipt before the schedule; a peer deciding in between reads the flag.

    Without it a permanently broken recurring row is moved to its next slot by the loser, and
    the winner can no longer stop it — `next_run_at` has changed under it.
    """
    ctx = _ctx(make_scope)
    created = service.create_reminder(ctx, message="дайджест", recurrence="daily", time="10:00")
    confirmed = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    slot_at = confirmed["next_run_at"]
    slot_id = str(int(parse_iso_timestamp(slot_at)))
    clock[0] = parse_iso_timestamp(slot_at) + 10

    # The winner's half-done state: its receipt is written, its schedule write has not landed.
    assert _peer_claims(store, clock) == slot_id
    store.update_receipt(
        created["id"],
        slot_id,
        status="needs_attention",
        finished_at=utc_iso(clock[0]),
        error="SchedulerError: schedule carries no usable delivery scope",
        permanent=True,
    )

    recorder = _Recorder()
    _dispatcher(store, clock, recorder)._tick()

    assert recorder.sent == []
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "needs_attention"
    assert refreshed.value["next_run_at"] is None


def _due_daily(service, make_scope, clock) -> tuple[dict, str]:
    """A recurring row already due, so `permanent` decides its fate rather than `not recurring`."""
    ctx = _ctx(make_scope)
    created = service.create_reminder(ctx, message="дайджест", recurrence="daily", time="10:00")
    confirmed = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    clock[0] = parse_iso_timestamp(confirmed["next_run_at"]) + 10
    return created, str(int(parse_iso_timestamp(confirmed["next_run_at"])))


def test_an_unreadable_receipt_ends_a_recurring_schedule_too(store, service, make_scope, clock):
    """An unknown outcome is permanent: rolling a recurring row forward would bury it."""
    created, slot_id = _due_daily(service, make_scope, clock)
    assert _peer_claims(store, clock) == slot_id
    store.update_receipt(created["id"], slot_id, status="who knows", finished_at="x")

    _dispatcher(store, clock, _Recorder())._tick()

    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "needs_attention"
    assert refreshed.value["next_run_at"] is None


def test_a_stale_claim_lets_a_recurring_schedule_carry_on(store, service, make_scope, clock):
    """The other side of the same constant: a stale claim is transient, so tomorrow still runs."""
    created, slot_id = _due_daily(service, make_scope, clock)
    assert _peer_claims(store, clock) == slot_id
    clock[0] += 400

    _dispatcher(store, clock, _Recorder())._tick()

    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "active"
    assert parse_iso_timestamp(refreshed.value["next_run_at"]) > clock[0]


def test_the_batch_limit_bounds_one_pass_and_the_next_pass_drains_the_rest(store, service, make_scope, clock):
    """A backlog after downtime must not become one long serial pass on the worker."""
    for index in range(4):
        created = service.create_reminder(
            _ctx(make_scope), message=f"ping {index}", recurrence="once", at=utc_iso(_NOW + 60 + index)
        )
        assert created["state"] == "active"
    clock[0] = _NOW + 600
    recorder = _Recorder()
    dispatcher = ScheduleDispatcher(
        store,
        chats=RecordedChats(),
        send_message=recorder.send_message,
        submit_task=recorder.submit_task,
        batch_limit=2,
    )

    assert dispatcher._tick() == 2
    assert dispatcher._tick() == 2
    assert [text for text, *_ in recorder.sent] == ["ping 0", "ping 1", "ping 2", "ping 3"]


def test_a_peer_ticking_mid_delivery_takes_the_rest_of_the_backlog(store, service, make_scope, clock):
    """The point of the whole change: replicas share a backlog rather than queue behind the first.

    The peer's pass runs while the holder is inside its first send, which is when the holder's
    claimed rows are still `active` at a past slot and would otherwise fill the peer's whole batch.
    """
    for index in range(4):
        service.create_reminder(
            _ctx(make_scope), message=f"ping {index}", recurrence="once", at=utc_iso(_NOW + 60 + index)
        )
    clock[0] = _NOW + 600
    holder, peer = _Recorder(), _Recorder()
    deliver, ticked = holder.send_message, []

    def send_while_the_peer_ticks(*args, **kwargs):
        if not ticked:
            ticked.append(_dispatcher(store, clock, peer)._tick())
        deliver(*args, **kwargs)

    holder.send_message = send_while_the_peer_ticks
    ScheduleDispatcher(
        store,
        chats=RecordedChats(),
        send_message=holder.send_message,
        submit_task=holder.submit_task,
        batch_limit=2,
    )._tick()

    assert [text for text, *_ in holder.sent] == ["ping 0", "ping 1"]
    assert [text for text, *_ in peer.sent] == ["ping 2", "ping 3"]


def test_a_stale_claim_returns_to_the_due_scan(store, service, make_scope, clock):
    """Hiding a claimed slot must not hide a dead holder's: that is how a crash is recovered."""
    _due_reminder(service, make_scope, clock)
    _peer_claims(store, clock)

    assert store.claim_due_slots(utc_iso(clock[0]), limit=200, stale_before_iso=utc_iso(clock[0])).claimed == []
    aged = store.claim_due_slots(utc_iso(clock[0] + 400), limit=200, stale_before_iso=utc_iso(clock[0] + 100))
    assert len(aged.lost) == 1 and aged.claimed == []


def test_a_claim_this_pass_gave_up_on_still_ages_out(store, service, make_scope, clock):
    """The heartbeat must restamp only what the pass still owes, or an orphan hides the row forever."""
    created = _due_reminder(service, make_scope, clock)
    batch = store.claim_due_slots(utc_iso(clock[0]), limit=200, stale_before_iso=utc_iso(0))
    (orphan,) = batch.claimed

    # The holder stays busy on other schedules for well past the stale window.
    for step in range(1, 8):
        store.refresh_claims([(str(uuid.uuid4()), "1")], now_iso=utc_iso(clock[0] + step * 60))

    aged = store.claim_due_slots(utc_iso(clock[0] + 600), limit=200, stale_before_iso=utc_iso(clock[0] + 300))
    assert [entry.slot_id for entry in aged.lost] == [orphan.slot_id]
    assert store.get_receipt(created["id"], orphan.slot_id).status == "claimed"


def test_a_long_pass_keeps_the_tail_of_its_own_batch_claimed(store, service, make_scope, clock):
    """A heartbeat fed nothing is a heartbeat that stops: a peer then kills a live slot mid-pass."""
    ctx = _ctx(make_scope)
    for index in range(4):
        service.create_reminder(ctx, message=f"ping {index}", recurrence="once", at=utc_iso(_NOW + 60 + index))
    clock[0] = _NOW + 600
    holder, peer, ticked = _Recorder(), _Recorder(), []
    original = holder.send_message

    def deliver_slowly(*args, **kwargs):
        # Every slot costs more than the heartbeat interval, and the third also lets a peer scan.
        clock[0] += 200
        original(*args, **kwargs)
        if len(holder.sent) == 3:
            ticked.append(_dispatcher(store, clock, peer)._tick())

    holder.send_message = deliver_slowly
    ScheduleDispatcher(
        store,
        chats=RecordedChats(),
        send_message=holder.send_message,
        submit_task=holder.submit_task,
        batch_limit=200,
    )._tick()

    # The peer scanned 500s into the pass, well past the stale bound the batch was claimed at.
    assert ticked == [0] and peer.sent == []
    assert [text for text, *_ in holder.sent] == [f"ping {index}" for index in range(4)]


def test_a_claim_stamped_far_in_the_future_is_ended_rather_than_left_open(store, service, make_scope, clock):
    """It is hidden from nobody and ages out for nobody, so every later pass would read it as fresh."""
    created = _due_reminder(service, make_scope, clock)
    slot_id = _peer_claims(store, clock)
    store.update_receipt(created["id"], slot_id, status="claimed", finished_at="")
    with store._engine.begin() as conn:
        conn.execute(
            sql_text("UPDATE schedule_runs SET claimed_at=:at WHERE schedule_id=:id"),
            {"at": utc_iso(clock[0] + 86_400), "id": created["id"]},
        )

    _dispatcher(store, clock, _Recorder())._tick()

    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "needs_attention"
    assert refreshed.value["next_run_at"] is None


def test_a_peers_claim_under_ordinary_clock_skew_is_left_alone(store, service, make_scope, clock):
    """A replica whose clock runs a little fast still holds a live claim; ending it would kill the run."""
    created = _due_reminder(service, make_scope, clock)
    slot_id = _peer_claims(store, clock)
    store.update_receipt(created["id"], slot_id, status="claimed", finished_at="")
    with store._engine.begin() as conn:
        conn.execute(
            sql_text("UPDATE schedule_runs SET claimed_at=:at WHERE schedule_id=:id"),
            {"at": utc_iso(clock[0] + 30), "id": created["id"]},
        )

    _dispatcher(store, clock, _Recorder())._tick()

    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "active"


def test_a_stop_is_seen_before_the_lost_claims_are_walked(store, service, make_scope, clock):
    """A backlog of lost claims writes on every entry; the join is a second, so the pass must check."""
    _due_reminder(service, make_scope, clock)
    _peer_claims(store, clock)
    clock[0] += 400
    recorder = _Recorder()
    dispatcher = ScheduleDispatcher(
        store,
        chats=RecordedChats(),
        send_message=recorder.send_message,
        submit_task=recorder.submit_task,
        batch_limit=200,
        should_stop=lambda: True,
    )

    assert dispatcher._tick() == 0

    # Untouched: absorbing it would have written an outcome the stop had no time for.
    assert store.get_for_dispatch(_ID_HOLDER[0]).value["state"] == "active"


def test_a_stop_before_the_claimed_loop_still_hands_the_batch_back(store, service, make_scope, clock):
    """The stop checks in the earlier loops must not skip the handback: the claims are already committed."""
    created = _due_reminder(service, make_scope, clock)
    # A peer's claim that will be stale by the time the pass runs, so the row lands in `lost`.
    assert len(store.claim_due_slots(utc_iso(clock[0]), limit=1, stale_before_iso=utc_iso(0)).claimed) == 1
    second = service.create_reminder(_ctx(make_scope), message="second", recurrence="once", at=utc_iso(_NOW + 400))
    clock[0] = _NOW + 600

    recorder = _Recorder()
    ScheduleDispatcher(
        store,
        chats=RecordedChats(),
        send_message=recorder.send_message,
        submit_task=recorder.submit_task,
        batch_limit=200,
        should_stop=lambda: True,
    )._tick()

    # The pass claimed the second and never started it, so that receipt has to be gone.
    slot = str(int(parse_iso_timestamp(store.get_for_dispatch(second["id"]).value["next_run_at"])))
    assert store.get_receipt(second["id"], slot) is None
    # The peer's own claim is none of this pass's business and stays where it was.
    assert store.get_receipt(created["id"], str(int(parse_iso_timestamp(created["next_run_at"])))) is not None


def test_a_stopping_pass_hands_back_what_it_never_started(store, service, make_scope, clock):
    """A graceful stop mid-batch must not bury the tail: nothing ran for it, so it is un-claimed."""
    for index in range(3):
        service.create_reminder(
            _ctx(make_scope), message=f"ping {index}", recurrence="once", at=utc_iso(_NOW + 60 + index)
        )
    clock[0] = _NOW + 600
    recorder, stopping = _Recorder(), []
    original = recorder.send_message

    def stop_after_the_first(*args, **kwargs):
        stopping.append(True)
        original(*args, **kwargs)

    dispatcher = ScheduleDispatcher(
        store,
        chats=RecordedChats(),
        send_message=stop_after_the_first,
        submit_task=recorder.submit_task,
        batch_limit=200,
        should_stop=lambda: bool(stopping),
    )
    assert dispatcher._tick() == 1

    # The two it never reached are free for the next pass, on this instance or any peer.
    left = store.claim_due_slots(utc_iso(clock[0]), limit=200, stale_before_iso=utc_iso(0))
    assert len(left.claimed) == 2 and left.lost == []


def test_a_delivery_aborted_by_a_changed_row_writes_no_message(store, service, make_scope, clock):
    """A schedule cancelled between the claim and the delivery must not still fire."""
    ctx = _ctx(make_scope)
    created = _due_reminder(service, make_scope, clock)
    batch = store.claim_due_slots(utc_iso(clock[0]), limit=200, stale_before_iso=utc_iso(0))
    (taken,) = batch.claimed
    service.cancel_schedule(ctx, created["id"], revision=created["revision"])

    recorder = _Recorder()
    dispatcher = _dispatcher(store, clock, recorder)

    assert dispatcher._deliver_claimed(taken, now_ts=clock[0]) is False
    assert recorder.sent == []
    assert store.get_receipt(created["id"], taken.slot_id).status == "aborted"


def test_the_dispatch_result_survives_one_concurrent_update(store, service, make_scope, clock):
    """The CAS loop is what keeps a delivered slot from losing its outcome to a racing writer."""
    created = _due_reminder(service, make_scope, clock)
    recorder = _Recorder()
    dispatcher = _dispatcher(store, clock, recorder)
    real_replace, failed = store.replace_for_dispatch, []

    def raise_once(schedule, *, expected_token):
        if not failed:
            failed.append(True)
            raise ConcurrentUpdateError("a peer wrote first")
        return real_replace(schedule, expected_token=expected_token)

    store.replace_for_dispatch = raise_once
    assert dispatcher._tick() == 1

    assert failed == [True]
    assert store.get_for_dispatch(created["id"]).value["state"] == "completed"


def test_one_slot_raising_a_validation_error_does_not_abandon_the_batch(store, service, make_scope, clock):
    """Escaping `tick`, it would leave every remaining slot claimed and hidden from peers for 300s."""
    for index in range(3):
        service.create_reminder(
            _ctx(make_scope), message=f"ping {index}", recurrence="once", at=utc_iso(_NOW + 60 + index)
        )
    clock[0] = _NOW + 600
    recorder = _Recorder()
    dispatcher = _dispatcher(store, clock, recorder)
    real_get, poisoned = store.get_for_dispatch, []

    def raise_once(schedule_id):
        if not poisoned:
            poisoned.append(schedule_id)
            raise ScheduleValidationError("something under this slot is unusable")
        return real_get(schedule_id)

    store.get_for_dispatch = raise_once

    assert dispatcher._tick() == 2
    assert len(recorder.sent) == 2


def test_a_released_claim_leaves_the_schedule_alive(store, service, make_scope, clock):
    """A stopping peer hands slots back; a pass that lost the race must not bury one as unknown."""
    created = _due_reminder(service, make_scope, clock)
    batch = store.claim_due_slots(utc_iso(clock[0]), limit=200, stale_before_iso=utc_iso(0))
    (taken,) = batch.claimed
    store.release_claims([(created["id"], taken.slot_id)])

    recorder = _Recorder()
    _dispatcher(store, clock, recorder)._absorb_lost_claim(taken.schedule, taken.slot_id, now_ts=clock[0])

    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "active"
    assert refreshed.value["next_run_at"] == created["next_run_at"]


def test_a_slot_a_peer_already_ended_is_not_delivered(store, service, make_scope, clock):
    """A batch's tail can age past the stale bound; the holder must not deliver what a peer buried."""
    created = _due_reminder(service, make_scope, clock)
    recorder = _Recorder()
    dispatcher = _dispatcher(store, clock, recorder)
    batch = store.claim_due_slots(utc_iso(clock[0]), limit=200, stale_before_iso=utc_iso(0))
    (taken,) = batch.claimed
    # The claim is open, so only the receipt's status can stop the delivery.
    assert store.holds_claim(created["id"], taken.slot_id)
    store.update_receipt(created["id"], taken.slot_id, status="needs_attention", finished_at="x", error="peer gave up")

    assert dispatcher._deliver_claimed(taken, now_ts=clock[0]) is False
    assert recorder.sent == []


def test_a_peer_ticking_mid_delivery_still_delivers_once(store, service, make_scope, clock):
    """The sharpest race: the peer's whole pass runs while the winner is inside its send."""
    created = _due_reminder(service, make_scope, clock)
    winner, peer = _Recorder(), _Recorder()
    deliver = winner.send_message

    def send_while_the_peer_ticks(*args, **kwargs):
        # At this instant the receipt says `claimed` and the schedule row is still untouched.
        _dispatcher(store, clock, peer)._tick()
        deliver(*args, **kwargs)

    winner.send_message = send_while_the_peer_ticks
    assert _dispatcher(store, clock, winner)._tick() == 1

    assert len(winner.sent) == 1 and peer.sent == []
    assert store.get_for_dispatch(created["id"]).value["state"] == "completed"


def test_the_due_scan_takes_the_clause_that_makes_replicas_diverge(store):
    """The receipt's primary key is the other half; without the lock clause two replicas
    would take the same batch."""
    from ouroboros.scheduling.store import due_select_for

    assert due_select_for().endswith("FOR UPDATE SKIP LOCKED")
    assert store.backend_name == "database"


def test_the_sweep_runs_on_its_own_cadence_and_a_failing_tick_does_not_starve_it(store, clock, monkeypatch):
    """A failing tick must not stop retention, and a pass inside the interval must not re-sweep."""
    from ouroboros.scheduling import dispatch as dispatch_module

    dispatcher = _dispatcher(store, clock, _Recorder())
    monkeypatch.setattr(dispatcher, "_tick", lambda: (_ for _ in ()).throw(RuntimeError("tick failed")))
    swept: list[float] = []
    monkeypatch.setattr(dispatcher, "_sweep_runs", lambda: swept.append(clock[0]) or 0)

    dispatcher.run_pass()
    dispatcher.run_pass()
    clock[0] += dispatch_module.SWEEP_INTERVAL_SECONDS + 1
    dispatcher.run_pass()

    assert len(swept) == 2


def test_a_recurring_schedule_advances_to_the_next_slot(store, service, make_scope, clock):
    ctx = _ctx(make_scope)
    created = service.create_reminder(ctx, message="дневной дайджест", recurrence="daily", time="10:00")
    confirmed = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    clock[0] = parse_iso_timestamp(confirmed["next_run_at"]) + 10
    recorder = _Recorder()
    assert _dispatcher(store, clock, recorder)._tick() == 1
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed is not None
    assert refreshed.value["state"] == "active"
    assert parse_iso_timestamp(refreshed.value["next_run_at"]) > clock[0]
