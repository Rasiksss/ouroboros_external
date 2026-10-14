"""Owner isolation is exercised through the service — the boundary the tools call."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros.scheduling.core import ScheduleValidationError, utc_iso
from ouroboros.scheduling.service import SchedulerError, SchedulerService
from tests.helpers.parametrize import parametrize_from_dict

_NOW = 1_900_000_000.0


@pytest.fixture
def service(store):
    return SchedulerService(store, timezone_for=lambda scope: "Europe/Moscow")


def _ctx(make_scope, owner: str = "owner-a", chat: str = "100", surface: str = "dialogs"):
    return SimpleNamespace(scope=make_scope(owner=owner, chat=chat, surface=surface))


def _create_pending_task(service, ctx) -> dict:
    return service.create_scheduled_task(ctx, instruction="проверь почту", recurrence="once", at=utc_iso(_NOW + 3600))


def test_two_owners_cannot_see_or_mutate_each_others_schedules_via_the_service(service, make_scope):
    """Every lifecycle door reads another owner's id as missing, and the row stays intact."""
    ctx_a = _ctx(make_scope, owner="owner-a")
    ctx_b = _ctx(make_scope, owner="owner-b", chat="200")
    created = _create_pending_task(service, ctx_a)
    schedule_id, revision = created["id"], created["revision"]

    listing = service.list_schedules(ctx_b.scope)
    assert listing["schedules"] == [] and listing["total"] == 0
    with pytest.raises(SchedulerError, match="not found"):
        service.edit_schedule(ctx_b, schedule_id, revision=revision, what="чужой текст")
    with pytest.raises(SchedulerError, match="not found"):
        service.confirm_schedule(ctx_b, schedule_id, revision=revision)
    with pytest.raises(SchedulerError, match="not found"):
        service.cancel_schedule(ctx_b, schedule_id, revision=revision)
    with pytest.raises(SchedulerError, match="not found"):
        service.move_schedule_here(ctx_b, schedule_id=schedule_id, revision=revision)

    mine = service.list_schedules(ctx_a.scope)["schedules"]
    assert [value["id"] for value in mine] == [schedule_id]
    assert mine[0]["what"] == "проверь почту"
    assert mine[0]["revision"] == revision


def test_the_duplicate_guard_sees_every_open_schedule_not_just_the_first_page(service, make_scope):
    """A listing page is 15 rows; an owner with more than that must still be refused a duplicate."""
    ctx = _ctx(make_scope)
    created = [
        service.create_scheduled_task(ctx, instruction=f"задача {index}", recurrence="once", at=utc_iso(_NOW + 3600))
        for index in range(20)
    ]
    assert all(item.get("duplicate") is None for item in created)

    again = [
        service.create_scheduled_task(ctx, instruction=f"задача {index}", recurrence="once", at=utc_iso(_NOW + 3600))
        for index in range(20)
    ]

    assert all(item.get("duplicate") is True for item in again)
    assert service.list_schedules(ctx.scope)["total"] == 20


def test_duplicate_fingerprint_dedup_is_per_owner(service, make_scope):
    """One owner's second identical definition deduplicates; another owner's does not."""
    ctx_a = _ctx(make_scope, owner="owner-a")
    first = _create_pending_task(service, ctx_a)
    again = _create_pending_task(service, ctx_a)
    assert again.get("duplicate") is True
    assert again["id"] == first["id"]

    other = _create_pending_task(service, _ctx(make_scope, owner="owner-b", chat="200"))
    assert other.get("duplicate") is None
    assert other["id"] != first["id"]


@parametrize_from_dict({surface: {"surface": surface} for surface in ("a2a", "skill-bridge")})
def test_internal_surfaces_cannot_create_list_or_delete(service, make_scope, surface):
    """An a2a or skill-bridge caller has no interactive chat for a schedule to deliver to."""
    created = _create_pending_task(service, _ctx(make_scope))
    ctx = _ctx(make_scope, surface=surface)
    with pytest.raises(SchedulerError, match="interactive"):
        _create_pending_task(service, ctx)
    with pytest.raises(SchedulerError, match="interactive"):
        service.list_schedules(ctx.scope)
    with pytest.raises(SchedulerError, match="interactive"):
        service.delete_schedule(ctx.scope, created["id"], revision=created["revision"])


def test_a_schedule_cannot_be_rebound_to_an_internal_surface(service, make_scope):
    """move_schedule_here reads the *caller's* scope, so a skill-bridge caller cannot take delivery."""
    owner_ctx = _ctx(make_scope)
    created = _create_pending_task(service, owner_ctx)
    bridge_ctx = _ctx(make_scope, surface="skill-bridge", chat="0")

    with pytest.raises(SchedulerError, match="interactive"):
        service.move_schedule_here(bridge_ctx, schedule_id=created["id"], revision=created["revision"])

    unchanged = service.list_schedules(owner_ctx.scope)["schedules"][0]
    assert unchanged["delivery"] == {"scope": {"surface_id": "dialogs", "chat_id": "100"}}


def test_relative_delay_fields_are_mutually_exclusive(service, make_scope):
    ctx = _ctx(make_scope)
    with pytest.raises(ScheduleValidationError, match="only one relative delay"):
        service.create_reminder(ctx, message="ping", recurrence="once", in_minutes=5, in_hours=1)
    with pytest.raises(ScheduleValidationError, match="either at or a relative delay"):
        service.create_reminder(ctx, message="ping", recurrence="once", at=utc_iso(_NOW + 600), in_minutes=5)


def test_dedup_distinguishes_a_reminder_from_an_agent_task(service, make_scope):
    """Same text and time, different execution kind — two schedules, not one."""
    ctx = _ctx(make_scope)
    at = utc_iso(_NOW + 3600)
    reminder = service.create_reminder(ctx, message="проверь почту", recurrence="once", at=at)
    task = service.create_scheduled_task(ctx, instruction="проверь почту", recurrence="once", at=at)
    assert task.get("duplicate") is None
    assert task["id"] != reminder["id"]


def test_timezone_is_resolved_per_owner(store, make_scope):
    """The schedule keeps the timezone of the owner who created it, not the instance's."""
    zones = {"owner-a": "Europe/Moscow", "owner-b": "Asia/Novosibirsk"}
    service = SchedulerService(store, timezone_for=lambda scope: zones[scope.owner_id])
    created = service.create_reminder(
        _ctx(make_scope, owner="owner-b", chat="200"), message="утренний дайджест", recurrence="daily", time="09:00"
    )
    assert created["timezone"] == "Asia/Novosibirsk"


def test_tool_answers_carry_no_ownership_pair_but_the_row_keeps_the_whole_scope(service, store, make_scope):
    """The answer reaches the model's context; the durable row keeps the whole scope."""
    ctx = _ctx(make_scope)
    created = _create_pending_task(service, ctx)
    assert created["delivery"] == {"scope": {"surface_id": "dialogs", "chat_id": "100"}}
    listed = service.list_schedules(ctx.scope)["schedules"][0]
    assert listed["delivery"] == {"scope": {"surface_id": "dialogs", "chat_id": "100"}}

    row = store.get_schedule(created["id"], scope=ctx.scope)
    assert row is not None
    assert row.value["delivery"]["scope"]["owner_id"] == "owner-a"
    assert row.value["delivery"]["scope"]["agent_id"] == "main"


def test_a_string_chat_id_is_a_usable_delivery_chat(service, make_scope):
    """A dialogs peer id is a string on some deployments, and the seam routes on either shape."""
    ctx = _ctx(make_scope, chat="peer-42")
    created = service.create_reminder(ctx, message="ping", recurrence="once", at=utc_iso(_NOW + 3600))
    assert created["delivery"] == {"scope": {"surface_id": "dialogs", "chat_id": "peer-42"}}


def test_a_chat_that_cannot_receive_delivery_is_refused(service, make_scope):
    """A negative id is the internal-chat marker; an empty one `OwnerScope` already refuses."""
    with pytest.raises(SchedulerError, match="delivery chat"):
        service.create_reminder(_ctx(make_scope, chat="-7"), message="ping", recurrence="hourly")


def test_a_conversation_numbered_zero_is_served_like_any_other(service, make_scope):
    ctx = _ctx(make_scope, chat="0")

    created = service.create_reminder(ctx, message="ping", recurrence="once", at=utc_iso(_NOW + 3600))

    assert created["delivery"] == {"scope": {"surface_id": "dialogs", "chat_id": "0"}}
    assert [value["id"] for value in service.list_schedules(ctx.scope)["schedules"]] == [created["id"]]
    assert service.cancel_schedule(ctx, created["id"], revision=created["revision"])["state"] == "cancelled"


def test_two_peers_of_one_owner_do_not_share_a_listing(service, make_scope):
    first, second = _ctx(make_scope, chat="peer-1"), _ctx(make_scope, chat="peer-2")
    at = utc_iso(_NOW + 3600)

    mine = service.create_reminder(first, message="мой отчёт", recurrence="once", at=at)
    theirs = service.create_reminder(second, message="их отчёт", recurrence="once", at=at)

    for ctx, own in ((first, mine), (second, theirs)):
        listing = service.list_schedules(ctx.scope)
        assert listing["total"] == 1
        assert [value["id"] for value in listing["schedules"]] == [own["id"]]
        assert [value["what"] for value in listing["schedules"]] == [own["what"]]


def test_paging_one_conversation_reaches_all_of_it_and_nothing_of_another(service, make_scope):
    here, elsewhere = _ctx(make_scope, chat="100"), _ctx(make_scope, chat="200")
    for index in range(5):
        service.create_reminder(here, message=f"мой {index}", recurrence="once", at=utc_iso(_NOW + 3600 + index))
        service.create_reminder(elsewhere, message=f"чужой {index}", recurrence="once", at=utc_iso(_NOW + 60 + index))

    seen: list[str] = []
    page = service.list_schedules(here.scope, limit=2)
    while True:
        assert page["total"] == 5
        seen.extend(value["what"] for value in page["schedules"])
        if "next_cursor" not in page:
            break
        page = service.list_schedules(here.scope, cursor=page["next_cursor"], limit=2)

    assert sorted(seen) == [f"мой {index}" for index in range(5)]


def test_a_group_move_re_anchors_delivery_to_the_participant_asking_now(service, store, make_scope):
    first = _ctx(make_scope)
    first.transport = {"target_uid": 7}
    created = service.create_reminder(first, message="созвон", recurrence="once", at=utc_iso(_NOW + 3600))
    second = _ctx(make_scope)
    second.transport = {"target_uid": 9}

    moved = service.move_schedule_here(second, schedule_id=created["id"], revision=created["revision"])

    assert moved["delivery"] == {"scope": {"surface_id": "dialogs", "chat_id": "100"}}
    row = store.get_schedule(created["id"], scope=second.scope)
    assert row is not None
    assert row.value["delivery"]["transport"] == {"target_uid": 9}
    assert row.value["delivery"]["scope"]["chat_id"] == "100"


def test_a_schedule_of_another_conversation_is_neither_listed_nor_movable(service, store, make_scope):
    here = _ctx(make_scope, chat="100")
    here.transport = {"target_uid": 7}
    created = service.create_reminder(here, message="созвон", recurrence="once", at=utc_iso(_NOW + 3600))
    elsewhere = _ctx(make_scope, chat="200")
    elsewhere.transport = {"target_uid": 9}

    assert service.list_schedules(elsewhere.scope)["schedules"] == []
    with pytest.raises(SchedulerError, match="not found"):
        service.move_schedule_here(elsewhere, schedule_id=created["id"], revision=created["revision"])

    row = store.get_schedule(created["id"], scope=here.scope)
    assert row is not None
    assert row.value["delivery"]["transport"] == {"target_uid": 7}
    assert row.value["delivery"]["scope"]["chat_id"] == "100"


def test_the_listing_is_ordered_soonest_first(service, make_scope):
    ctx = _ctx(make_scope)
    for minutes in (300, 60, 180):
        service.create_reminder(ctx, message=f"ping {minutes}", recurrence="once", at=utc_iso(_NOW + minutes * 60))

    listed = service.list_schedules(ctx.scope)["schedules"]

    assert [value["what"] for value in listed] == ["ping 60", "ping 180", "ping 300"]


def test_every_schedule_stays_reachable_through_the_cursor(service, make_scope):
    """A page a caller cannot walk past strands a schedule: revisions come only from here."""
    ctx = _ctx(make_scope)
    for minutes in range(1, 6):
        service.create_reminder(ctx, message=f"ping {minutes}", recurrence="once", at=utc_iso(_NOW + minutes * 600))

    seen: list[str] = []
    cursor, pages = 0, 0
    while True:
        answer = service.list_schedules(ctx.scope, limit=2, cursor=cursor)
        seen.extend(value["what"] for value in answer["schedules"])
        pages += 1
        if "next_cursor" not in answer:
            break
        cursor = answer["next_cursor"]

    assert pages == 3
    assert seen == [f"ping {minutes}" for minutes in range(1, 6)]
    assert service.list_schedules(ctx.scope)["total"] == 5


def test_the_listing_can_be_narrowed_to_one_state_and_reports_counts(service, make_scope):
    ctx = _ctx(make_scope)
    active = service.create_reminder(ctx, message="ping", recurrence="once", at=utc_iso(_NOW + 3600))
    _create_pending_task(service, ctx)

    answer = service.list_schedules(ctx.scope, state="pending")

    assert [value["state"] for value in answer["schedules"]] == ["pending"]
    assert answer["counts"] == {
        "active": 1,
        "pending": 1,
        "paused": 0,
        "needs_attention": 0,
        "completed": 0,
        "cancelled": 0,
    }
    assert active["id"] not in [value["id"] for value in answer["schedules"]]


def test_a_wall_clock_datetime_implies_a_one_off_without_saying_so(service, make_scope):
    """`edit_schedule` always inferred this; create refusing it is the asymmetry behind the schema complaint."""
    ctx = _ctx(make_scope)

    created = service.create_reminder(ctx, message="напомни завтра", at=utc_iso(_NOW + 86400))

    assert created["recurrence"] == "once"
    assert created["state"] == "active"


def test_a_stated_recurrence_is_never_overridden_by_at(service, make_scope):
    """Inferring `once` from `at` on top of a stated recurrence downgraded a daily reminder —
    and a one-off reminder auto-activates, so it skipped the confirm gate too."""
    ctx = _ctx(make_scope)

    created = service.create_reminder(
        ctx, message="принять таблетки", recurrence="daily", time="09:00", at=utc_iso(_NOW + 3600)
    )

    assert created["recurrence"] == "daily"
    assert created["time"] == "09:00"
    assert created["needs_confirmation"] is True
    assert created["state"] == "pending"


def test_an_empty_at_reads_as_absent(service, make_scope):
    """A model that fills every property sends `at: ""`; `at is not None` called that a datetime."""
    ctx = _ctx(make_scope)

    created = service.create_reminder(ctx, message="дайджест", recurrence="daily", time="09:00", at="")

    assert created["recurrence"] == "daily"


def test_a_relative_delay_implies_a_one_off_without_saying_so(service, make_scope):
    ctx = _ctx(make_scope)

    created = service.create_reminder(ctx, message="через 10 минут", in_minutes=10)

    assert created["recurrence"] == "once"
    assert created["state"] == "active"


def test_a_reminder_with_no_time_information_still_names_the_legal_recurrences(service, make_scope):
    with pytest.raises(ScheduleValidationError, match="recurrence must be one of"):
        service.create_reminder(_ctx(make_scope), message="когда-нибудь")


def test_a_group_participants_uid_is_kept_for_delivery_but_never_shown(service, store, make_scope):
    """`target_uid` reaches the transport at fire time; the tool answer reaches the model."""
    ctx = _ctx(make_scope)
    ctx.transport = {"target_uid": 7, "source_message_id": "m-1"}

    created = service.create_reminder(ctx, message="ping", recurrence="once", at=utc_iso(_NOW + 3600))

    assert created["delivery"] == {"scope": {"surface_id": "dialogs", "chat_id": "100"}}
    row = store.get_schedule(created["id"], scope=ctx.scope)
    assert row.value["delivery"]["transport"] == {"target_uid": 7}


def test_two_participants_of_one_group_are_not_deduplicated_into_one(service, make_scope):
    """Same text, same group, different asker — one schedule each, or one of them is answered for."""
    first = _ctx(make_scope)
    first.transport = {"target_uid": 7}
    second = _ctx(make_scope)
    second.transport = {"target_uid": 9}
    at = utc_iso(_NOW + 3600)

    one = service.create_reminder(first, message="созвон", recurrence="once", at=at)
    two = service.create_reminder(second, message="созвон", recurrence="once", at=at)

    assert two.get("duplicate") is None
    assert one["id"] != two["id"]


def test_edit_with_a_stale_revision_is_refused(service, make_scope):
    ctx = _ctx(make_scope)
    created = _create_pending_task(service, ctx)
    edited = service.edit_schedule(ctx, created["id"], revision=created["revision"], what="другой текст")
    assert edited["revision"] == created["revision"] + 1
    with pytest.raises(SchedulerError, match="revision"):
        service.edit_schedule(ctx, created["id"], revision=created["revision"], what="третий текст")


def test_lifecycle_confirm_pause_resume_cancel(service, make_scope):
    """The main path: pending → active → paused → active → cancelled, all via the service."""
    ctx = _ctx(make_scope)
    created = service.create_scheduled_task(ctx, instruction="дневная сводка", recurrence="daily", time="10:00")
    assert created["needs_confirmation"] is True

    active = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    assert active["state"] == "active" and active["next_run_at"]

    paused = service.pause_schedule(ctx, created["id"], revision=active["revision"])
    assert paused["state"] == "paused" and not paused.get("next_run_at")

    resumed = service.resume_schedule(ctx, created["id"], revision=paused["revision"])
    assert resumed["state"] == "active" and resumed["next_run_at"]

    cancelled = service.cancel_schedule(ctx, created["id"], revision=resumed["revision"])
    assert cancelled["state"] == "cancelled"


def test_a_skipped_row_does_not_rewind_the_listing_cursor(service, store, make_scope, engine, monkeypatch):
    """The cursor is an offset: advancing it by decoded rows re-reads the page, or never moves."""
    from sqlalchemy import text as sql_text

    ctx = _ctx(make_scope)
    for index in range(4):
        service.create_reminder(ctx, message=f"ping {index}", recurrence="daily", time=f"1{index}:00")
    broken = store.list_schedules(scope=make_scope())[0]
    with engine.begin() as conn:
        conn.execute(
            sql_text("UPDATE schedules SET payload_json='not json' WHERE schedule_id=:id"),
            {"id": broken.schedule_id},
        )

    first = service.list_schedules(ctx.scope, limit=2)

    assert first["total"] == 4
    # Two rows were read even though one of them would not decode.
    assert first["next_cursor"] == 2, "the cursor rewound onto rows the page already covered"
    second = service.list_schedules(ctx.scope, cursor=first["next_cursor"], limit=2)
    assert "next_cursor" not in second, "the listing never ends"
