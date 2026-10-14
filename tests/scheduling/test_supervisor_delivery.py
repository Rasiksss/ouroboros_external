"""The delivery `run_supervisor` composes, exercised through the dispatcher's own tick.

A test against a stand-in passes whatever the real wiring does: the contract "a raise means
the publish failed" only holds if the composed callable honours it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros.observability import aef_tracing
from ouroboros.scheduling.core import utc_iso
from ouroboros.scheduling.service import SchedulerService
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.runtime import _build_schedule_dispatcher

_NOW = 1_900_000_000.0


class _Inbox:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, envelope) -> None:
        self.submitted.append(envelope)


def _wired(store, clock, *, published: bool, detached_tasks: bool = False):
    """`_build_schedule_dispatcher` over the real store, with the two halves it reaches for."""
    sent: list = []
    inbox = _Inbox()

    def send_with_budget(text, **kwargs):
        sent.append((text, kwargs))
        return published

    dispatcher = _build_schedule_dispatcher(
        SimpleNamespace(send_with_budget=send_with_budget),
        SimpleNamespace(inbox=inbox),
        batch_limit=200,
        detached_tasks=detached_tasks,
    )
    dispatcher.store = store
    return dispatcher, sent, inbox


@pytest.fixture
def service(store, clock):
    return SchedulerService(store, timezone_for=lambda scope: "Europe/Moscow")


def _ctx(make_scope, **kwargs):
    ctx = SimpleNamespace(scope=make_scope(**kwargs))
    ctx.transport = {"target_uid": 7, "source_message_id": "m-1"}
    return ctx


def test_a_reminder_no_transport_took_is_recorded_as_needing_attention(store, service, make_scope, clock, monkeypatch):
    """`send_with_budget` reports a failed publish rather than raising, so the wiring must."""
    monkeypatch.setattr("ouroboros.scheduling.store.default_schedule_store", lambda: store)
    created = service.create_reminder(_ctx(make_scope), message="ping", recurrence="once", at=utc_iso(_NOW + 60))
    clock[0] = _NOW + 120
    dispatcher, sent, _inbox = _wired(store, clock, published=False)

    assert dispatcher._tick() == 0

    assert len(sent) == 1
    refreshed = store.get_for_dispatch(created["id"])
    assert refreshed.value["state"] == "needs_attention"


def test_a_published_reminder_completes_and_carries_the_group_hint(store, service, make_scope, clock, monkeypatch):
    monkeypatch.setattr("ouroboros.scheduling.store.default_schedule_store", lambda: store)
    created = service.create_reminder(_ctx(make_scope), message="ping", recurrence="once", at=utc_iso(_NOW + 60))
    clock[0] = _NOW + 120
    dispatcher, sent, _inbox = _wired(store, clock, published=True)

    assert dispatcher._tick() == 1

    text, kwargs = sent[0]
    assert text == "ping"
    assert kwargs.get("task_id", "") == ""
    assert kwargs["source"] == "dialogs"
    assert kwargs["transport"] == {"target_uid": 7}
    assert store.get_for_dispatch(created["id"]).value["state"] == "completed"


def test_an_injected_task_carries_the_ticks_trace_and_the_asker(store, service, make_scope, clock, monkeypatch):
    """A contextvar does not cross the inbox, which in the multi-user mode is another process."""
    monkeypatch.setattr("ouroboros.scheduling.store.default_schedule_store", lambda: store)
    ctx = _ctx(make_scope)
    created = service.create_scheduled_task(ctx, instruction="собери сводку", recurrence="once", at=utc_iso(_NOW + 60))
    confirmed = service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    clock[0] = _NOW + 120
    dispatcher, _sent, inbox = _wired(store, clock, published=True)

    with aef_tracing.trace_scope(aef_tracing.accept_trace_id("", source="schedule-dispatch")):
        expected = aef_tracing.current_trace_id()
        assert dispatcher._tick() == 1

    envelope = inbox.submitted[0]
    assert expected and envelope.trace_id == expected
    assert envelope.client_message_id.startswith(f"schedule:{created['id']}:")
    assert envelope.sender_label == "Task Scheduler"
    assert envelope.transport == {"target_uid": 7}
    assert envelope.user_id == 7
    assert confirmed["state"] == "active"


def test_a_fired_schedule_is_admitted_silently_and_leaves_the_owner_free(store, clock, tmp_path, monkeypatch):
    """The tick's own envelope through the real admission path, because three steps of it refuse
    or answer, and a stand-in envelope proves none of them.

    A detached root needs the subsystem on and a persisted anchor to capture context from — the
    tick's envelope is logged before it is submitted, which supplies one — and it is acknowledged
    to whoever submitted it, which for a schedule is nobody.
    """
    import threading

    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.settings.schema import AppSettings
    from supervisor.runtime import SupervisorLoop, _build_schedule_dispatcher
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests, supervisor_heartbeat

    monkeypatch.setattr(
        "supervisor.orchestrator.orchestrator.load_settings",
        lambda: AppSettings(OUROBOROS_DURABLE_JOBS_ENABLED=True),
    )
    monkeypatch.setattr("ouroboros.scheduling.store.default_schedule_store", lambda: store)
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    replies: list[str] = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        drive_root=tmp_path,
        tasks_repo=runtime_persistence.current.tasks_repo,
        bridge=runtime.bridge,
        state_repo=runtime.state_repo,
        orchestrator=runtime.orchestrator,
        task_queue=runtime.task_queue,
        agent_events=runtime.agent_events,
        send_with_budget=lambda text, **kwargs: replies.append(text) or True,
    )
    scope = _due_agent_task(store, clock)
    dispatcher = _build_schedule_dispatcher(ctx, runtime.gateway, batch_limit=200, detached_tasks=True)
    loop = SupervisorLoop(
        ctx=ctx, stop_event=threading.Event(), inbox=runtime.gateway.inbox, heartbeat=supervisor_heartbeat()
    )

    assert dispatcher._tick() == 1
    loop.process_inbox(timeout=0.0)

    admitted = runtime_persistence.current.tasks_repo.list_tasks(statuses=["queued"])
    assert [record.detached for record in admitted] == [True], "the tick's task was refused, not queued"
    # A detached root is acknowledged to whoever typed it; a schedule was typed weeks ago.
    assert replies == [], f"a fired schedule answered the owner's chat: {replies}"
    assert runtime_persistence.current.tasks_repo.claim_next_task(worker_id=0, runtime_id="rt-1") is not None
    # The owner writes while it runs: a foreground root would have held this lane for the whole task.
    runtime.orchestrator.create_root_task.execute(
        TaskRequest(id="user-msg", job_id="user-msg", type="task", text="привет", scope=scope.to_dict()),
        enabled=True,
        max_active=5,
    )
    assert runtime_persistence.current.tasks_repo.claim_next_task(worker_id=1, runtime_id="rt-2") is not None


def _due_agent_task(store, clock):
    """One confirmed agent-task schedule, already due, and the scope it fires under."""
    from new_core.identity import OwnerScope

    scope = OwnerScope(owner_id="global", agent_id="main", surface_id="dialogs", chat_id="7")
    service = SchedulerService(store, timezone_for=lambda _scope: "Europe/Moscow")
    ctx = SimpleNamespace(scope=scope, transport={})
    created = service.create_scheduled_task(ctx, instruction="сводка", recurrence="once", at=utc_iso(_NOW + 60))
    service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    clock[0] = _NOW + 120
    return scope


@pytest.mark.parametrize("detached_tasks", [False, True])
def test_an_injected_task_is_detached_exactly_where_the_subsystem_takes_one(
    store, service, make_scope, clock, monkeypatch, detached_tasks
):
    """`root_claim_sql` serializes non-detached roots per agent, so a foreground scheduled task
    holds the owner's chat for its whole run; a detached one is refused while the subsystem is off.
    """
    monkeypatch.setattr("ouroboros.scheduling.store.default_schedule_store", lambda: store)
    ctx = _ctx(make_scope)
    created = service.create_scheduled_task(ctx, instruction="сводка", recurrence="once", at=utc_iso(_NOW + 60))
    service.confirm_schedule(ctx, created["id"], revision=created["revision"])
    clock[0] = _NOW + 120
    dispatcher, _sent, inbox = _wired(store, clock, published=True, detached_tasks=detached_tasks)

    assert dispatcher._tick() == 1

    assert inbox.submitted[0].detached is detached_tasks
