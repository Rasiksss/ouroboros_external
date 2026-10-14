"""Physical deletion keeps the SQL scope and the dispatch receipt lifecycle intact."""

from types import SimpleNamespace

from ouroboros.scheduling.core import parse_iso_timestamp, utc_iso
from ouroboros.scheduling.dispatch import RUN_RETENTION_SECONDS, SWEEP_INTERVAL_SECONDS, ScheduleDispatcher
from ouroboros.scheduling.service import SchedulerService
from tests.helpers.parametrize import parametrize_from_dict


def _due_schedule(store, scope, clock, *, kind="reminder"):
    service = SchedulerService(store, timezone_for=lambda _: "UTC")
    ctx = SimpleNamespace(scope=scope)
    if kind == "reminder":
        created = service.create_reminder(ctx, message="remember", in_minutes=1)
    else:
        created = service.create_scheduled_task(ctx, instruction="remember", in_minutes=1)
        created = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    clock[0] += 61
    return service, ctx, created, str(int(parse_iso_timestamp(created["next_run_at"])))


@parametrize_from_dict({kind: {"kind": kind} for kind in ("reminder", "agent_task")})
def test_deletion_during_delivery_retains_the_result_without_resurrecting(
    store, make_scope, clock, chats, caplog, kind
):
    service, ctx, created, slot = _due_schedule(store, make_scope(), clock, kind=kind)
    delivered = []

    def send(message, **kwargs):
        service.delete_schedule(ctx.scope, created["id"], revision=created["revision"])
        delivered.append(kwargs.get("client_message_id"))

    dispatcher = ScheduleDispatcher(store, chats=chats, send_message=send, submit_task=send, batch_limit=10)
    dispatcher.run_pass()
    dispatcher.run_pass()

    expected = f"schedule:{created['id']}:{slot}" if kind == "agent_task" else None
    assert delivered == [expected]
    assert store.get_for_dispatch(created["id"]) is None
    assert store.get_receipt(created["id"], slot).status == "dispatched"
    assert "dispatch failed" not in caplog.text
    clock[0] += RUN_RETENTION_SECONDS + 1
    dispatcher.run_pass()
    assert store.get_receipt(created["id"], slot) is None


def test_deletion_after_claim_before_delivery_aborts_the_receipt(store, make_scope, clock, chats):
    scope = make_scope()
    _due_schedule(store, scope, clock)
    service, ctx, second, second_slot = _due_schedule(store, scope, clock)
    delivered = []

    def send(message, **kwargs):
        assert store.get_receipt(second["id"], second_slot).status == "claimed"
        service.delete_schedule(ctx.scope, second["id"], revision=second["revision"])
        delivered.append(message)

    dispatcher = ScheduleDispatcher(store, chats=chats, send_message=send, submit_task=send, batch_limit=10)
    dispatcher.run_pass()
    dispatcher.run_pass()

    assert delivered == ["remember"]
    assert store.get_for_dispatch(second["id"]) is None
    assert store.get_receipt(second["id"], second_slot).status == "aborted"


@parametrize_from_dict({"same clock": {"claim_skew": 0}, "peer clock ahead": {"claim_skew": 60}})
def test_deleted_abandoned_claim_settles_before_normal_retention(store, make_scope, clock, chats, claim_skew):
    service, ctx, created, slot = _due_schedule(store, make_scope(), clock)
    assert store.claim_due_slots(utc_iso(clock[0] + claim_skew), limit=10, stale_before_iso=utc_iso(0)).claimed
    service.delete_schedule(ctx.scope, created["id"], revision=created["revision"])
    delivered = []
    dispatcher = ScheduleDispatcher(
        store,
        chats=chats,
        send_message=lambda *a, **kw: delivered.append(kw),
        submit_task=lambda *a, **kw: delivered.append(kw),
        batch_limit=10,
    )
    dispatcher.run_pass()
    assert store.get_receipt(created["id"], slot).status == "claimed"
    clock[0] += 301 + claim_skew
    dispatcher.run_pass()
    assert store.get_receipt(created["id"], slot).status == "claimed"
    clock[0] += SWEEP_INTERVAL_SECONDS
    dispatcher.run_pass()
    receipt = store.get_receipt(created["id"], slot)
    assert receipt.status == "needs_attention"
    assert receipt.finished_at == utc_iso(clock[0])
    assert delivered == []
    clock[0] += RUN_RETENTION_SECONDS + 1
    dispatcher.run_pass()
    assert store.get_receipt(created["id"], slot) is None
