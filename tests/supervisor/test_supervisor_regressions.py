"""Regressions on the supervisor's side of the transport seam.

Everything here is a rule that used to live in a transport and now lives once, in
the supervisor: who may issue a runtime command, who may become the owner, what
happens to a message the supervisor itself cannot handle, and what an outbound
message is allowed to claim about its own delivery.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import threading
import time
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

import supervisor.message_bus as message_bus
from new_core.coordination.inprocess import NullWorkSignal
from ouroboros.agent_events.inmemory import create_agent_event_transport
from ouroboros.agent_events.interfaces import TaskHeartbeat
from ouroboros.contracts.attachments import StoredFile
from ouroboros.storage import task_transitions
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
from ouroboros.storage.runtime_models import TaskRecord, TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
from supervisor.orchestrator.orchestrator import SubmissionCommitUnknown, SubmitResult
from supervisor.queue import TaskQueue
from supervisor.runtime import (
    _COMMAND_REFUSAL,
    SupervisorLoop,
)
from supervisor.state import get_state_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.background_memory_db import memory_database
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.files import stored_file
from tests.helpers.runtime_store import claim_task, park_run
from tests.helpers.scope import scope_for
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor import supervisor_heartbeat
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader
from tests.helpers.usage import sql_usage_repository
from tests.helpers.user_settings import stub_user_settings
from transport.infra.inmemory import InProcessInbox, InProcessMessageGateway, InProcessOutbox
from transport.interfaces import (
    IInbox,
    InboundEnvelope,
    IOutbox,
    IOutboxReader,
    OutboundKind,
    OutboundMessage,
    SubscriptionMode,
)

A2A_CHAT_ID = -123456


def _loop(supervisor) -> SupervisorLoop:
    return SupervisorLoop(
        ctx=supervisor.ctx, stop_event=threading.Event(), inbox=InProcessInbox(), heartbeat=supervisor_heartbeat()
    )


def _terminal_job(task_id: str, job_id: str, **updates: Any) -> TaskRecord:
    record = TaskRecord(
        task_id=task_id,
        job_id=job_id,
        detached=True,
        owner_id=TEST_SCOPE.owner_id,
        agent_id=TEST_SCOPE.agent_id,
        status="completed",
        result="done",
        scope=TEST_SCOPE.to_dict(),
        source="browser",
        input={"text": "work", "scope": TEST_SCOPE.to_dict()},
    )
    return record.model_copy(update=updates)


def _set_owner(supervisor, *, owner_id: Any, chat_id: Any, source: str) -> None:
    """Seed ownership the way an inbound message claims it: with the whole scope.

    ``owner_chat_id`` and ``owner_chat_source`` are derived views now, so there is nothing
    to set beside the scope — which is the point: no path can put a chat and a source into
    the row that disagree with the identity they belong to.
    """
    with supervisor.runtime.state_repo.mutate() as state:
        state.owner_id = owner_id
        state.owner_scope = scope_for(source, chat_id).to_dict()


# -- a runtime command belongs to the owner's own chat --------------------------


@pytest.mark.parametrize("text", ["/restart", "/stop"])
def test_a_remote_peer_cannot_run_a_runtime_command(supervisor, monkeypatch, text: str) -> None:
    dispatched: list[str] = []
    monkeypatch.setattr(
        "supervisor.runtime.handle_slash_command",
        lambda command, **_kwargs: dispatched.append(command),
    )
    _set_owner(supervisor, owner_id=7, chat_id=7, source="dialogs")

    _loop(supervisor).handle_inbound(
        InboundEnvelope(scope=scope_for("a2a", A2A_CHAT_ID), user_id=A2A_CHAT_ID, text=text)
    )

    assert dispatched == []
    assert [(chat_id, message) for chat_id, message, _kwargs in supervisor.sent] == [(A2A_CHAT_ID, _COMMAND_REFUSAL)]
    # The peer is answered on its own route, so it is not left waiting.
    assert supervisor.sent[0][2]["source"] == "a2a"


def _park_a_question(supervisor, scope, confirmation_id: str) -> None:
    """Park a run on a question and leave the question in the chat, the way the runtime does."""
    from ouroboros.contracts.tool_confirmation import CONFIRMATION_TRANSPORT_KEY

    store = runtime_persistence.current
    claim_task(store, "waiting", scope=scope)
    park_run(store, task_id="waiting", scope=scope, confirmation_id=confirmation_id)
    append_chat_message(
        supervisor.ctx.DRIVE_ROOT,
        scope=scope,
        actor_type="agent",
        chat_id=scope.chat_id,
        user_id=scope.chat_id,
        text="Подтвердите действие: data_write",
        source=str(scope.surface_id),
        transport={CONFIRMATION_TRANSPORT_KEY: {"confirmation_id": confirmation_id}},
    )


def test_an_answer_to_an_open_confirmation_queues_the_run_it_answers(supervisor) -> None:
    """The answer is a decision, not a new task: it returns the parked run rather than starting work."""
    scope = scope_for("dialogs", 7)
    _set_owner(supervisor, owner_id=7, chat_id=7, source="dialogs")
    store = runtime_persistence.current
    _park_a_question(supervisor, scope, "c-1")

    _loop(supervisor).handle_inbound(InboundEnvelope(scope=scope, user_id=7, text="да"))

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("c-1") == "approved"
    assert [task.task_id for task in store.tasks_repo.list_tasks(statuses=["queued"], scope=scope)] == ["waiting"]
    assert "да" in [item.content for item in list_owner_messages(supervisor.ctx.DRIVE_ROOT, scope=scope)]


def test_a_click_decides_the_request_it_names(supervisor) -> None:
    scope = scope_for("browser", 7)
    store = runtime_persistence.current
    _park_a_question(supervisor, scope, "ab" * 16)

    _loop(supervisor).handle_inbound(
        InboundEnvelope(
            scope=scope,
            user_id=7,
            confirmation_id="ab" * 16,
            confirmation_decision="approved",
            suppress_chat_log=True,
        )
    )

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("ab" * 16) == "approved"
    assert [task.task_id for task in store.tasks_repo.list_tasks(statuses=["queued"], scope=scope)] == ["waiting"]


def test_an_answer_decides_even_where_a_runtime_command_would_be_refused(supervisor) -> None:
    """Whoever may write in that conversation may already ask for the call, so the answer adds no power."""
    asked = scope_for("dialogs", 9)
    _set_owner(supervisor, owner_id=7, chat_id=7, source="dialogs")
    store = runtime_persistence.current
    _park_a_question(supervisor, asked, "c-9")

    _loop(supervisor).handle_inbound(InboundEnvelope(scope=asked, user_id=11, text="да"))

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_status("c-9") == "approved"
    assert [task.task_id for task in store.tasks_repo.list_tasks(statuses=["queued"], scope=asked)] == ["waiting"]


def test_the_owners_own_command_still_runs(supervisor, monkeypatch) -> None:
    dispatched: list[str] = []
    monkeypatch.setattr(
        "supervisor.runtime.handle_slash_command",
        lambda command, **_kwargs: dispatched.append(command),
    )
    _set_owner(supervisor, owner_id=7, chat_id=7, source="sberchat")

    # `dialogs` and `sberchat` are one transport under two labels.
    _loop(supervisor).handle_inbound(InboundEnvelope(scope=scope_for("dialogs", 7), user_id=7, text="/restart"))

    assert dispatched == ["/restart"]
    assert supervisor.sent == []


def test_a_retired_command_is_answered_by_the_runtime_and_never_reaches_the_agent(supervisor) -> None:
    """The whole inbound path for `/clean`: an answer from the runtime, and no task behind it.

    Queued as work it reached the agent, which reported the history cleared without anything
    having been cleared.
    """
    _set_owner(supervisor, owner_id=7, chat_id=7, source="dialogs")
    scope = scope_for("dialogs", 7)

    _loop(supervisor).handle_inbound(InboundEnvelope(scope=scope, user_id=7, text="/clean"))

    assert [(chat_id, message) for chat_id, message, _kwargs in supervisor.sent] == [
        (7, "Команда /clean не поддерживается.")
    ]
    assert supervisor.ctx.tasks_repo.list_tasks(scope=scope, limit=None) == []


def test_the_operator_console_commands_a_dialogs_owned_install(supervisor, monkeypatch) -> None:
    """The browser holds chat 1 forever, so tying it to the owner slot locked it out."""
    dispatched: list[str] = []
    monkeypatch.setattr(
        "supervisor.runtime.handle_slash_command",
        lambda command, **_kwargs: dispatched.append(command),
    )
    _set_owner(supervisor, owner_id=7, chat_id=7, source="dialogs")

    _loop(supervisor).handle_inbound(InboundEnvelope(scope=scope_for("browser", 1), user_id=1, text="/status"))

    assert dispatched == ["/status"]
    assert supervisor.sent == []


# -- only a transport that can be the owner may claim the identity --------------


@pytest.mark.parametrize("source", ["a2a", "skill:alpha"])
def test_a_non_owner_transport_cannot_take_over_the_owner(supervisor, source: str) -> None:
    """The browser lives on chat 1 forever, so the `1/1` placeholder never expires."""
    _set_owner(supervisor, owner_id=1, chat_id=1, source="browser")

    _loop(supervisor).handle_inbound(
        InboundEnvelope(
            scope=scope_for(source, A2A_CHAT_ID),
            user_id=A2A_CHAT_ID,
            text="who are you",
        )
    )

    state = supervisor.runtime.state_repo.load()
    assert (state.owner_id, state.owner_chat_id, state.owner_chat_source) == (1, "1", "browser")


def test_the_first_owner_capable_message_still_adopts(supervisor) -> None:
    _loop(supervisor).handle_inbound(InboundEnvelope(scope=scope_for("dialogs", 555), user_id=777, text="привет"))

    state = supervisor.runtime.state_repo.load()
    assert (state.owner_id, state.owner_chat_id, state.owner_chat_source) == (777, "555", "dialogs")


def test_a_row_written_before_the_stored_scope_is_reclaimed_by_its_owner(supervisor) -> None:
    """An upgraded install has an owner and no scope, and must be able to get one.

    ``owner_chat_id`` and ``owner_chat_source`` became derived views of ``owner_scope``, so a row
    written before that field existed reads back with both of them ``None`` — and the first read
    rewrites the row without the retired keys, so the old values are gone for good. If "not yet
    claimed" did not mean "claimable", every owner-addressed notice and every runtime command
    would be refused for the life of that install.
    """
    with supervisor.runtime.state_repo.mutate() as state:
        state.owner_id = 7
        state.owner_scope = None

    _loop(supervisor).handle_inbound(InboundEnvelope(scope=scope_for("dialogs", 7), user_id=7, text="привет"))

    state = supervisor.runtime.state_repo.load()
    assert (state.owner_id, state.owner_chat_id, state.owner_chat_source) == (7, "7", "dialogs")
    assert state.scope == scope_for("dialogs", 7)


def test_a_scopeless_row_is_not_taken_over_by_someone_other_than_its_owner(supervisor) -> None:
    """Reclaiming is for the stored owner; a stranger speaking first is still a takeover."""
    with supervisor.runtime.state_repo.mutate() as state:
        state.owner_id = 7
        state.owner_scope = None

    _loop(supervisor).handle_inbound(InboundEnvelope(scope=scope_for("dialogs", 999), user_id=999, text="привет"))

    state = supervisor.runtime.state_repo.load()
    assert state.owner_id == 7
    assert state.scope is None


def test_an_upgraded_local_install_is_still_claimable_by_a_real_chat(supervisor) -> None:
    """`1/1` is the local placeholder, and an upgraded row shows it with no chat at all.

    ``owner_chat_id`` derives from the stored scope, so the row a browser-only install was
    upgraded from reads back as owner ``1`` and chat ``None``. Matching only the literal
    ``1/1`` left that install owned by nobody a real chat could take over from.
    """
    with supervisor.runtime.state_repo.mutate() as state:
        state.owner_id = 1
        state.owner_scope = None

    _loop(supervisor).handle_inbound(InboundEnvelope(scope=scope_for("dialogs", 555), user_id=777, text="привет"))

    state = supervisor.runtime.state_repo.load()
    assert (state.owner_id, state.owner_chat_id, state.owner_chat_source) == (777, "555", "dialogs")


def test_the_owners_own_message_repairs_a_stale_source(supervisor) -> None:
    """The deleted dialogs path healed this field on every message; nothing else does."""
    _set_owner(supervisor, owner_id=7, chat_id=7, source="browser")

    _loop(supervisor).handle_inbound(InboundEnvelope(scope=scope_for("dialogs", 7), user_id=7, text="привет"))

    state = supervisor.runtime.state_repo.load()
    assert (state.owner_chat_id, state.owner_chat_source) == ("7", "dialogs")


# -- a non-numeric chat id is stored the way the transport addresses it ----------


def test_a_non_numeric_chat_id_does_not_produce_a_second_history_row(supervisor) -> None:
    """Coercing a peer id to 1 wrote a duplicate under the browser's chat."""
    append_chat_message(
        supervisor.drive_root,
        scope=scope_for("dialogs", "peer-42"),
        actor_type="owner",
        chat_id="peer-42",
        user_id="peer-42",
        text="сделай",
        source="dialogs",
        client_message_id="peer-msg-1",
    )

    _loop(supervisor).handle_inbound(
        InboundEnvelope(
            scope=scope_for("dialogs", "peer-42"),
            user_id="peer-42",
            text="сделай",
            client_message_id="peer-msg-1",
        )
    )

    rows = [
        row
        for row in list_owner_messages(supervisor.drive_root, scope=scope_for("dialogs", "peer-42"))
        if row.content == "сделай"
    ]
    assert len(rows) == 1
    assert rows[0].chat_id == "peer-42"


# -- one poisoned message must not kill the tick --------------------------------


def _agent_chat_result() -> SubmitResult:
    return SubmitResult(intent=TaskIntentKind.AGENT_CHAT, mode=SubmissionMode.QUEUE, task_id="ok")


@pytest.mark.parametrize("message", ["Job logical-job queued.", "too many tasks are unfinished"])
def test_detached_admission_reply_carries_the_public_job_id(supervisor, monkeypatch, message: str) -> None:
    monkeypatch.setattr(
        supervisor.ctx.orchestrator,
        "submit",
        lambda _envelope: SubmitResult(
            intent=TaskIntentKind.AGENT_CHAT,
            mode=SubmissionMode.QUEUE,
            task_id="logical-job",
            message=message,
        ),
    )

    _loop(supervisor).handle_inbound(
        InboundEnvelope(
            scope=scope_for("browser", 7),
            user_id=7,
            text="work",
            detached=True,
            client_message_id="detached-1",
        )
    )

    assert supervisor.sent[-1][2]["task_id"] == "logical-job"


def test_a_failing_message_does_not_stop_the_drain(supervisor, monkeypatch) -> None:
    submitted: list[str] = []

    def submit(envelope: InboundEnvelope) -> SubmitResult:
        if envelope.text == "boom":
            raise RuntimeError("submission exploded")
        submitted.append(envelope.text)
        return _agent_chat_result()

    monkeypatch.setattr(supervisor.ctx.orchestrator, "submit", submit)
    inbox = InProcessInbox()
    loop = SupervisorLoop(
        ctx=supervisor.ctx, stop_event=threading.Event(), inbox=inbox, heartbeat=supervisor_heartbeat()
    )
    inbox.submit(InboundEnvelope(scope=scope_for("dialogs", 7), user_id=7, text="boom"))
    inbox.submit(InboundEnvelope(scope=scope_for("dialogs", 7), user_id=7, text="second"))

    assert loop.process_inbox(timeout=0.0) == 2
    assert submitted == ["second"]
    assert [message for _chat, message, _kwargs in supervisor.sent] == [
        "Не удалось обработать сообщение. Отправьте его ещё раз."
    ]


def test_handling_that_raised_is_still_settled_by_the_next_poll(supervisor, monkeypatch) -> None:
    """The queue is not a retry engine: a deterministic failure would loop forever."""

    def submit(envelope: InboundEnvelope) -> SubmitResult:
        raise RuntimeError("submission exploded")

    monkeypatch.setattr(supervisor.ctx.orchestrator, "submit", submit)
    inbox = InProcessInbox()
    loop = SupervisorLoop(
        ctx=supervisor.ctx, stop_event=threading.Event(), inbox=inbox, heartbeat=supervisor_heartbeat()
    )
    inbox.submit(InboundEnvelope(scope=scope_for("dialogs", 7), user_id=7, text="boom"))

    assert loop.process_inbox(timeout=0.0) == 1
    assert loop.process_inbox(timeout=0.0) == 0, "a failed handling must not be redelivered"


def test_the_drain_finishes_with_each_envelope_before_polling_again(supervisor, monkeypatch) -> None:
    """Polling ahead would settle envelopes before handling them."""
    calls: list[str] = []

    class _RecordingInbox(IInbox):
        """At the interface, not a backend subclass: the ordering is the seam's rule."""

        def __init__(self) -> None:
            self._envelopes = deque()

        def submit(self, envelope: InboundEnvelope) -> None:
            self._envelopes.append(envelope)

        def poll(self, timeout: float = 0.0) -> InboundEnvelope | None:
            calls.append("poll")
            return self._envelopes.popleft() if self._envelopes else None

    def submit(envelope: InboundEnvelope) -> SubmitResult:
        calls.append(f"handle:{envelope.text}")
        return _agent_chat_result()

    monkeypatch.setattr(supervisor.ctx.orchestrator, "submit", submit)
    inbox = _RecordingInbox()
    loop = SupervisorLoop(
        ctx=supervisor.ctx, stop_event=threading.Event(), inbox=inbox, heartbeat=supervisor_heartbeat()
    )
    for text in ("first", "second"):
        inbox.submit(InboundEnvelope(scope=scope_for("dialogs", 7), user_id=7, text=text))

    assert loop.process_inbox(timeout=0.0) == 2

    assert calls == ["poll", "handle:first", "poll", "handle:second", "poll"]


@pytest.mark.parametrize("committed", [False, True])
def test_unavailable_admission_reread_defers_without_poll_ack_or_reply(supervisor, monkeypatch, committed):
    from ouroboros.observability import aef_tracing
    from ouroboros.storage.settings.schema import AppSettings

    monkeypatch.setattr(
        "supervisor.orchestrator.orchestrator.load_settings", lambda: AppSettings(OUROBOROS_DURABLE_JOBS_ENABLED=True)
    )
    store = runtime_persistence.current
    admit, reread = runtime_persistence.current.create_root_task.execute, store.tasks_repo.get_task
    identities, polls = [], []
    trace = "12345678-1234-5678-1234-567812345678"

    def unavailable(task_id):
        raise OSError("authoritative read unavailable")

    def lost_commit(request, **kwargs):
        identities.append(request.id)
        if committed:
            admit(request, **kwargs)
        monkeypatch.setattr(store.tasks_repo, "get_task", unavailable)
        raise OSError("lost commit response")

    monkeypatch.setattr(runtime_persistence.current.create_root_task, "execute", lost_commit)
    loop = _loop(supervisor)
    poll = loop.inbox.poll
    monkeypatch.setattr(loop.inbox, "poll", lambda timeout=0: polls.append("poll") or poll(timeout))
    loop.inbox.submit(
        InboundEnvelope(
            scope=TEST_SCOPE, text="work", detached=True, client_message_id="same-operation", trace_id=trace
        )
    )
    assert loop.process_inbox(timeout=0) == 1
    assert loop.process_inbox(timeout=0) == 1
    assert polls == ["poll"] and supervisor.sent == [] and len(set(identities)) == 1
    monkeypatch.setattr(store.tasks_repo, "get_task", reread)
    monkeypatch.setattr(runtime_persistence.current.create_root_task, "execute", admit)
    supervisor.ctx.send_with_budget = lambda text, **kw: (
        supervisor.sent.append((text, kw, aef_tracing.current_trace_id())) or True
    )
    loop.process_inbox(timeout=0)
    assert polls == ["poll", "poll"] and len(store.tasks_repo.list_tasks()) == 1
    assert supervisor.sent[0][1]["task_id"] == identities[0]
    assert supervisor.sent[0][2] == trace
    assert aef_tracing.current_trace_id() != trace


def test_an_unknown_commit_is_reported_and_never_resubmitted(supervisor, monkeypatch, caplog) -> None:
    """ "May or may not have been committed" is not a reason to run the message twice."""
    calls: list[str] = []

    def submit(envelope: InboundEnvelope) -> SubmitResult:
        calls.append(envelope.text)
        raise SubmissionCommitUnknown("task-9")

    monkeypatch.setattr(supervisor.ctx.orchestrator, "submit", submit)
    inbox = InProcessInbox()
    loop = SupervisorLoop(
        ctx=supervisor.ctx, stop_event=threading.Event(), inbox=inbox, heartbeat=supervisor_heartbeat()
    )
    inbox.submit(InboundEnvelope(scope=scope_for("dialogs", 7), user_id=7, text="сделай"))

    with caplog.at_level("ERROR", logger="supervisor.runtime"):
        assert loop.process_inbox(timeout=0.0) == 1

    assert calls == ["сделай"]
    assert [message for _chat, message, _kwargs in supervisor.sent] == [
        "Не удалось обработать сообщение. Отправьте его ещё раз."
    ]
    assert "submission commit is unknown" in caplog.text


# -- every drain in the tick is bounded ------------------------------------------


#: What a handler that calls a model costs the tick it runs on, scaled down so a case can
#: run: `supervisor/events.py` gives the duplicate check a 15 s deadline on this thread.
_SLOW_HANDLER_SEC = 0.01
#: Small enough that a handful of the handlers above spend it, large enough that a machine
#: under load does not spend it before the first one returns.
_TEST_BUDGET_SEC = 0.05


class _FasterThanTheDrain:
    """The agent-event seam of a deployment whose runtimes are all reporting at once.

    One seam carries every instance's reports now, so "poll until it returns nothing" is a
    condition the producers can keep false for as long as they are busy — and on the broker
    each poll costs a round trip while a publish costs one. The supply is finite only so
    that an unbounded drain fails this case loudly instead of hanging it.
    """

    def __init__(self, supply: int) -> None:
        self.polls = 0
        self._supply = supply

    def poll(self, timeout: float = 0.0) -> Any:
        del timeout
        if self.polls >= self._supply:
            return None
        self.polls += 1
        return TaskHeartbeat(scope=TEST_SCOPE, task_id=f"task-{self.polls}")


class _Tick:
    """One tick's collaborators: what was dispatched, what was swept, how it was polled."""

    def __init__(self) -> None:
        self.dispatched: list[Any] = []
        self.swept: list[str] = []
        self.inbox_timeouts: list[float] = []


def _tick_with_agent_events(
    supervisor,
    monkeypatch,
    consumer,
    *,
    handler_cost_sec: float = 0.0,
) -> tuple[SupervisorLoop, _Tick]:
    """One tick over `consumer`, with the sweeps and the inbound drain watched.

    `handler_cost_sec` is what makes a case about the *time* bound able to fail: with a free
    handler the count bound alone satisfies every assertion, which is exactly the gap a
    review found in the first version of these tests.
    """
    watched = _Tick()

    def dispatch(event: Any, _ctx: Any) -> None:
        watched.dispatched.append(event)
        if handler_cost_sec:
            time.sleep(handler_cost_sec)

    monkeypatch.setattr("supervisor.events.dispatch_event", dispatch)
    supervisor.ctx.agent_events = consumer
    supervisor.ctx.task_queue = SimpleNamespace(
        enforce_task_timeouts=lambda: watched.swept.append("timeouts"),
        reclaim_stale_leases=lambda: watched.swept.append("leases"),
        reconcile_parked_tasks=lambda confirmations: watched.swept.append("parked"),
    )
    loop = _loop(supervisor)

    def process_inbox(limit: int = 0, *, timeout: float = 0.0) -> int:
        del limit
        watched.swept.append("inbox")
        watched.inbox_timeouts.append(timeout)
        return 0

    # Watched rather than run: the real drain's first poll blocks for a second, which says
    # nothing about the question here — whether the tick reaches it, and with which timeout.
    monkeypatch.setattr(loop, "process_inbox", process_inbox)
    return loop, watched


def test_the_agent_event_drain_stops_at_its_event_bound(supervisor, monkeypatch) -> None:
    """The bound on a fast producer: unbounded, a busy fleet enforces no timeout at all."""
    from supervisor.runtime import _AGENT_EVENT_DRAIN_LIMIT

    consumer = _FasterThanTheDrain(supply=_AGENT_EVENT_DRAIN_LIMIT * 5)
    loop, watched = _tick_with_agent_events(supervisor, monkeypatch, consumer)

    loop.run_tick()

    assert len(watched.dispatched) == _AGENT_EVENT_DRAIN_LIMIT
    assert consumer.polls == _AGENT_EVENT_DRAIN_LIMIT, "the bound is on polls, not on what they returned"
    assert watched.swept == ["timeouts", "leases", "parked", "inbox"]


def test_a_slow_handler_ends_the_drain_on_the_time_budget(supervisor, monkeypatch) -> None:
    """The bound the event count cannot supply, and the one the deployment actually hits.

    A `SubtaskRequested` runs the duplicate check, which calls a model on this thread under
    a 15 s deadline: a hundred of those inside one tick is ~25 minutes with no timeout
    enforced, no lease reclaimed and no inbox drained, while the probe still answers 200.
    """
    import supervisor.runtime as runtime_module
    from supervisor.runtime import _AGENT_EVENT_DRAIN_LIMIT

    monkeypatch.setattr(runtime_module, "_AGENT_EVENT_DRAIN_BUDGET_SEC", _TEST_BUDGET_SEC)
    consumer = _FasterThanTheDrain(supply=_AGENT_EVENT_DRAIN_LIMIT * 5)
    loop, watched = _tick_with_agent_events(
        supervisor,
        monkeypatch,
        consumer,
        handler_cost_sec=_SLOW_HANDLER_SEC,
    )

    started = time.monotonic()
    loop.run_tick()
    spent = time.monotonic() - started

    assert 0 < len(watched.dispatched) < _AGENT_EVENT_DRAIN_LIMIT, "the event count is not what ended this drain"
    assert spent < _SLOW_HANDLER_SEC * _AGENT_EVENT_DRAIN_LIMIT, "the tick must not run the whole event bound"
    assert watched.swept == ["timeouts", "leases", "parked", "inbox"]


def test_a_saturated_drain_does_not_then_pause_on_the_inbox(supervisor, monkeypatch) -> None:
    """Or the drain's bound doubles as a throughput cap of one drain per poll timeout."""
    from supervisor.runtime import _AGENT_EVENT_DRAIN_LIMIT, _INBOX_POLL_TIMEOUT_SEC

    loop, watched = _tick_with_agent_events(
        supervisor,
        monkeypatch,
        _FasterThanTheDrain(supply=_AGENT_EVENT_DRAIN_LIMIT * 5),
    )

    loop.run_tick()

    assert watched.inbox_timeouts == [0.0], "a tick with work waiting must not pace itself on an empty inbox"

    # And an unsaturated tick keeps the blocking poll, which is what paces an idle loop.
    idle, idle_watched = _tick_with_agent_events(supervisor, monkeypatch, _FasterThanTheDrain(supply=1))
    idle.run_tick()

    assert idle_watched.inbox_timeouts == [_INBOX_POLL_TIMEOUT_SEC]


def test_a_saturated_drain_says_so_once_rather_than_every_tick(supervisor, monkeypatch, caplog) -> None:
    """The only signal before the seam starts discarding droppable reports."""
    from supervisor.runtime import _AGENT_EVENT_DRAIN_LIMIT

    loop, _watched = _tick_with_agent_events(
        supervisor,
        monkeypatch,
        _FasterThanTheDrain(supply=_AGENT_EVENT_DRAIN_LIMIT * 5),
    )

    with caplog.at_level("WARNING", logger="supervisor.runtime"):
        loop.run_tick()
        loop.run_tick()

    saturation = [record for record in caplog.records if "reporting faster" in record.getMessage()]
    assert len(saturation) == 1, "a state, not an event: the report is on a gate of its own"


def test_a_drain_that_empties_stops_at_the_empty_poll(supervisor, monkeypatch) -> None:
    """The bound is a ceiling, not a batch size: nothing waits for a hundred reports."""
    queued = deque(TaskHeartbeat(scope=TEST_SCOPE, task_id=f"task-{index}") for index in range(3))
    consumer = SimpleNamespace(poll=lambda timeout=0.0: queued.popleft() if queued else None)
    loop, watched = _tick_with_agent_events(supervisor, monkeypatch, consumer)

    drain = loop.dispatch_agent_events()

    assert (drain.handled, drain.saturated) == (3, False)

    loop.run_tick()

    assert len(watched.dispatched) == 3
    assert watched.swept == ["timeouts", "leases", "parked", "inbox"]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_terminal_retry_preserves_history_and_only_repeats_ambiguous_text(supervisor, monkeypatch, ambiguous):
    store = runtime_persistence.current
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    store.tasks_repo.upsert_task(
        _terminal_job("attempt-2", "logical-job", result="winning answer", finished_at="2026-09-05T00:00:00+00:00")
    )
    reader = supervisor.runtime.gateway.outbox.reader(name="terminal-history", mode=SubscriptionMode.BROADCAST)
    with monkeypatch.context() as patch:
        if ambiguous:
            patch.setattr(
                runtime_persistence.current.result_repo,
                "record_terminal_delivery",
                lambda *a, **k: (_ for _ in ()).throw(OSError("lost receipt")),
            )
        _loop(supervisor)._publish_terminal_jobs()
    before = sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, actor_type="agent")[0]
    with store.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE tasks SET terminal_published_at=NULL, terminal_publish_claim=NULL, terminal_publish_after=NULL"
        )
    _loop(supervisor)._publish_terminal_jobs()
    after = sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, actor_type="agent")[0]
    assert after.model_dump() == before.model_dump()
    for _ in range(2 if ambiguous else 1):
        delivered = reader.poll(0)
        assert (delivered.message.text, delivered.message.task_id, delivered.message.message_id) == (
            "winning answer",
            "logical-job",
            before.message_id,
        )
        reader.ack(delivered)
    assert reader.poll(0) is None
    reader.close()
    assert len(list_owner_messages(supervisor.drive_root, scope=TEST_SCOPE)) == 1  # reconnect without a live reader
    assert store.tasks_repo.get_task("attempt-2").terminal_published_at


@pytest.mark.parametrize(
    ("status", "error_code", "expected"),
    [
        ("cancelled", "", "🛑 Фоновая задача logical-job остановлена."),
        ("failed", "execution_failed", "Фоновая задача logical-job завершилась с ошибкой. Код: execution_failed."),
    ],
)
def test_terminal_job_statuses_are_user_facing_russian(supervisor, status, error_code, expected):
    store = runtime_persistence.current
    supervisor.ctx.send_with_budget = lambda text, **kwargs: supervisor.sent.append((text, kwargs)) or True
    store.tasks_repo.upsert_task(_terminal_job("attempt-2", "logical-job", status=status, error_code=error_code))

    _loop(supervisor)._publish_terminal_jobs()

    assert supervisor.sent[0][0] == expected


@pytest.fixture
def memory_db():
    """The background-memory repositories over this test's runtime database."""
    yield from memory_database()


@pytest.mark.parametrize("failure", ["episode", "handoff", "lost-event"])
def test_terminal_memory_recovers_after_text_acceptance_and_publication_marker(
    supervisor, monkeypatch, failure, memory_db
):
    from background_memory.env import MemoryEnv
    from background_memory.handlers.distillation import DistillationHandler
    from background_memory.infra.events import memory_event_publisher_factory
    from background_memory.interfaces import MemoryDistillationEvent
    from infra.migrations.runner import assert_binary_rollback_safe
    from supervisor import runtime as module

    store, events, sent = runtime_persistence.current, [], []
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    supervisor.runtime.gateway.outbox.publish = lambda message, **kw: sent.append(message)
    trace = "c4fd7f38-c206-4af9-bb90-8731c4534492"
    store.tasks_repo.upsert_task(
        _terminal_job(
            "winning",
            "memory-job",
            finished_at="2026-09-05T00:00:00+00:00",
            input={"scope": TEST_SCOPE.to_dict(), "trace_id": trace, "text": "winning task"},
        )
    )

    def publish(event):
        if isinstance(event, MemoryDistillationEvent):
            if failure == "handoff":
                raise OSError("temporary handoff failure")
            events.append(event)

    memory_event_publisher_factory.initialize_broker(SimpleNamespace(publish=publish))
    with monkeypatch.context() as patch:
        if failure == "episode":
            patch.setattr(
                module, "store_task_episode", lambda *a, **k: (_ for _ in ()).throw(OSError("episode unavailable"))
            )
        _loop(supervisor)._publish_terminal_jobs()
    record = store.tasks_repo.get_task("winning")
    assert record.terminal_published_at and len(sent) == 1
    assert not record.metadata.get("terminal_memory_done") and record.metadata.get("terminal_memory_after")
    with store.engine.connect() as conn, pytest.raises(RuntimeError, match="binary rollback"):
        assert_binary_rollback_safe(conn)
    memory_event_publisher_factory.initialize_broker(SimpleNamespace(publish=events.append))
    now = task_transitions.database_now
    monkeypatch.setattr(task_transitions, "database_now", lambda conn: now(conn) + datetime.timedelta(seconds=10))
    _loop(supervisor)._publish_terminal_jobs()
    event = next(e for e in reversed(events) if isinstance(e, MemoryDistillationEvent))
    assert (event.task.job_id, event.task.task_id, event.trace_id) == ("memory-job", "winning", trace)
    handler = DistillationHandler(
        MemoryEnv(repo_dir=supervisor.drive_root, drive_root=supervisor.drive_root),
        None,
        tasks=memory_db.tasks,
        audit=memory_db.audit,
        memory_headers=memory_db.memory_headers,
        user_settings=stub_user_settings(),
    )

    async def _no_distillation(_event):
        return None

    monkeypatch.setattr(handler, "distil", _no_distillation)
    memory_db.run(handler.handle(event))
    monkeypatch.setattr(task_transitions, "database_now", lambda conn: now(conn) + datetime.timedelta(seconds=20))
    _loop(supervisor)._publish_terminal_jobs()
    assert store.tasks_repo.get_task("winning").metadata["terminal_memory_done"] and len(sent) == 1
    episode = sql_execution_memory_repository(store.engine).list_task_episodes(scope=TEST_SCOPE)[0]
    assert episode.task_id == "memory-job" and episode.created_at == record.finished_at
    with store.engine.connect() as conn:
        assert_binary_rollback_safe(conn)


@pytest.mark.parametrize("pause", ["batch", "history", "io", "lease", "database", "stop", "redis"])
def test_terminal_publication_fences_expired_work_and_bounds_the_whole_tick(supervisor, monkeypatch, pause):
    from sqlalchemy import text

    from supervisor import runtime as module

    store, effects = runtime_persistence.current, []
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    for suffix in ("a", "b"):
        store.tasks_repo.upsert_task(_terminal_job(f"attempt-{suffix}", f"job-{suffix}", status="cancelled"))
    loop = _loop(supervisor)

    def expire():
        with store.engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET terminal_publish_after='2000-01-01T00:00:00+00:00'"))

    original = runtime_persistence.current.result_repo.claim_terminal_jobs

    def claim(**kwargs):
        records = original(**kwargs)
        if pause == "batch":
            expire()
        if pause == "stop":
            loop.stop_event.set()
        return records

    monkeypatch.setattr(runtime_persistence.current.result_repo, "claim_terminal_jobs", claim)
    if pause == "lease":

        def lease_remaining(*_args, **_kwargs):
            return 1.0 if not effects else 0.0

        monkeypatch.setattr(runtime_persistence.current.result_repo, "terminal_publication_remaining", lease_remaining)
    original_history = message_bus.log_chat

    def history(*args, **kwargs):
        row = original_history(*args, **kwargs)
        if pause == "history":
            expire()
        return row

    monkeypatch.setattr(message_bus, "log_chat", history)
    release = threading.Event()

    def publish(message, *, before_publish=None):
        effects.append(message.message_id)
        assert release.wait(2)

    monkeypatch.setattr(supervisor.runtime.gateway.outbox, "publish", publish)
    if pause == "redis":
        from transport.infra import redis_gateway
        from transport.infra.redis_gateway import RedisOutbox

        def execute(command, *args, **kwargs):
            if command == "XADD":
                effects.append(command)
            else:
                expire()

        client = SimpleNamespace(xinfo_groups=lambda *a: expire() or [], xinfo_stream=lambda *a: {})
        connection = SimpleNamespace(
            check_health=lambda: None, send_command=execute, _sock=SimpleNamespace(settimeout=lambda _: None)
        )
        monkeypatch.setattr(
            redis_gateway.redis,
            "ConnectionPool",
            lambda **_: SimpleNamespace(
                get_connection=lambda: connection, release=lambda _: None, disconnect=lambda: None
            ),
        )
        append = SimpleNamespace(
            connection_pool=SimpleNamespace(connection_class=object, connection_kwargs={}),
            parse_response=lambda *_: [],
        )
        supervisor.runtime.bridge._outbox = RedisOutbox(client=client, append_client=append, stream="terminal")
    if pause == "database":
        monkeypatch.setattr(
            runtime_persistence.current.result_repo,
            "terminal_publication_remaining",
            lambda *a, **k: (_ for _ in ()).throw(OSError("offline")),
            raising=False,
        )
    budget = 3 if pause == "lease" else 1 if pause == "io" else 0.1
    monkeypatch.setattr(module, "_TERMINAL_TICK_BUDGET_SEC", budget, raising=False)
    started = time.monotonic()
    try:
        loop._publish_terminal_jobs()
        assert time.monotonic() - started < (budget if pause == "lease" else budget + 0.5)
        assert len(effects) == (1 if pause in {"io", "lease"} else 0)
        assert all(row.terminal_published_at is None for row in store.tasks_repo.list_tasks())
    finally:
        release.set()
    if pause in {"io", "lease"}:
        loop._publication.result(timeout=2)
        assert len(effects) == 1  # no late second effect or marker
        assert all(row.terminal_published_at is None for row in store.tasks_repo.list_tasks())


@pytest.mark.parametrize("ambiguous", [False, True])
def test_terminal_text_and_each_attachment_recover_independently(supervisor, monkeypatch, ambiguous):

    store, sent = runtime_persistence.current, []
    provider = get_storage_provider()
    attachments = [
        dict(
            stored_file(
                scope=TEST_SCOPE,
                provider=provider,
                data=b"payload",
                attachment_id=f"attachment-{i}",
                filename=f"file-{i}.png",
                mime="image/png",
                source="browser",
            ),
            caption="caption",
        )
        for i in range(3)
    ]
    from ouroboros.storage.chat_attachments import delete_attachment_metadata

    delete_attachment_metadata(TEST_SCOPE, attachments[0]["id"])
    store.tasks_repo.upsert_task(
        _terminal_job("attempt-media", "job-media", metadata={"terminal_attachments": attachments})
    )
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    original = message_bus.get_attachment

    def interrupted(scope, key):
        if key == attachments[1]["id"]:
            raise OSError("temporary image outage")
        return original(scope, key)

    def publish(message, *, before_publish=None):
        sent.append(message)
        if ambiguous and message.attachment:
            raise OSError("ambiguous acceptance")

    monkeypatch.setattr(supervisor.runtime.gateway.outbox, "publish", publish)
    with monkeypatch.context() as patch:
        patch.setattr(
            runtime_persistence.current.conversations_repo,
            "append_message",
            lambda *a, **k: (_ for _ in ()).throw(OSError("history down")),
        )
        _loop(supervisor)._publish_terminal_jobs()
    assert sent == [] and sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE) == []
    assert not store.tasks_repo.get_task("attempt-media").terminal_published_at
    with store.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE tasks SET terminal_publish_after=NULL")
    with monkeypatch.context() as patch:
        patch.setattr(message_bus, "get_attachment", interrupted)
        _loop(supervisor)._publish_terminal_jobs()
    record = store.tasks_repo.get_task("attempt-media")
    assert record.metadata["terminal_delivery"]["text"] == "accepted"
    assert record.metadata["terminal_delivery"][attachments[0]["id"]] == "unavailable"
    assert record.metadata["terminal_delivery"][attachments[1]["id"]] == "retryable"
    assert not record.terminal_published_at and sent[0].kind == OutboundKind.TEXT
    monkeypatch.setattr(supervisor.runtime.gateway.outbox, "publish", lambda message, **kw: sent.append(message))
    with store.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE tasks SET terminal_publish_after=NULL")
    _loop(supervisor)._publish_terminal_jobs()
    assert store.tasks_repo.get_task("attempt-media").terminal_published_at
    assert sum(m.kind == OutboundKind.TEXT for m in sent) == 1
    last = [m.message_id for m in sent if m.attachment and m.attachment.attachment_id == attachments[2]["id"]]
    assert len(last) == (2 if ambiguous else 1) and len(set(last)) == 1
    assert len(sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE)) == 1


def test_the_stop_budget_outlives_the_longest_tick_a_root_may_join(supervisor) -> None:
    from supervisor.events import _DEDUP_DEADLINE_SEC
    from supervisor.runtime import _AGENT_EVENT_DRAIN_BUDGET_SEC, _INBOX_POLL_TIMEOUT_SEC, TICK_STOP_BUDGET_SEC

    assert TICK_STOP_BUDGET_SEC > _AGENT_EVENT_DRAIN_BUDGET_SEC + _DEDUP_DEADLINE_SEC + _INBOX_POLL_TIMEOUT_SEC


@pytest.mark.parametrize(("initial", "expected"), [(2, [2, 4, 5, 5, 2]), (8, [5, 5, 5, 5, 5])])
def test_supervisor_backoff_is_capped_and_resets_after_success(supervisor, monkeypatch, initial, expected) -> None:
    from ouroboros.storage.settings.schema import AppSettings
    from supervisor.runtime import LOOP_STALL_SEC, TICK_STOP_BUDGET_SEC

    assert LOOP_STALL_SEC > TICK_STOP_BUDGET_SEC + AppSettings().OUROBOROS_SUPERVISOR_RETRY_MAX_DELAY_SEC
    with pytest.raises(ValueError):
        AppSettings(OUROBOROS_SUPERVISOR_RETRY_MAX_DELAY_SEC=31)
    monkeypatch.setenv("OUROBOROS_SUPERVISOR_RETRY_INITIAL_DELAY_SEC", str(initial))
    monkeypatch.setenv("OUROBOROS_SUPERVISOR_RETRY_MAX_DELAY_SEC", "5")
    loop = _loop(supervisor)
    waits = []
    monkeypatch.setattr(loop.stop_event, "wait", waits.append)
    outcomes = iter([False] * 4 + [True, False, None])

    def tick():
        outcome = next(outcomes)
        if outcome is False:
            raise RuntimeError("tick failed")
        if outcome is None:
            loop.stop_event.set()

    monkeypatch.setattr(loop, "run_tick", tick)
    loop.run()
    assert waits == expected


# -- tick recovery and shutdown -----------------------------------------------


def test_the_loop_cannot_be_built_without_an_inbox() -> None:
    with pytest.raises(TypeError):
        SupervisorLoop(ctx=SimpleNamespace(), stop_event=threading.Event())


def test_supervisor_resumes_queue_after_more_than_three_tick_failures(supervisor, monkeypatch) -> None:
    from psycopg import OperationalError

    loop = _loop(supervisor)
    loop.inbox.submit(InboundEnvelope(scope=scope_for("dialogs", 7), user_id=7, text="queued during outage"))
    run_tick = loop.run_tick
    ticks = 0

    def tick() -> int:
        nonlocal ticks
        ticks += 1
        if ticks <= 5:
            raise OperationalError("the database system is shutting down")
        if ticks == 7:
            raise asyncio.CancelledError
        return run_tick()

    monkeypatch.setattr(loop, "run_tick", tick)
    monkeypatch.setattr(loop.stop_event, "wait", lambda _seconds: False)

    with pytest.raises(asyncio.CancelledError):
        loop.run()

    tasks = runtime_persistence.current.tasks_repo.list_tasks()
    assert len(tasks) == 1 and tasks[0].status == "queued"
    assert tasks[0].input["text"] == "queued during outage"
    assert not loop.stop_event.is_set()


def test_terminal_publication_reuses_one_lazy_pool_and_closes_on_stop(supervisor):
    loop = _loop(supervisor)
    assert loop._publication_pool is None
    loop._publish_terminal_jobs()
    pool = loop._publication_pool
    workers = tuple(pool._threads)
    for _ in range(3):
        loop._publish_terminal_jobs()
        assert loop._publication_pool is pool and tuple(pool._threads) == workers
    loop.stop_event.set()
    loop.run()
    with pytest.raises(RuntimeError, match="shutdown"):
        pool.submit(lambda: None)
    for worker in workers:
        worker.join(0.3)
        assert not worker.is_alive()


def _run_supervisor(tmp_path, *, on_error, stop_event, ready_event) -> None:
    from ouroboros.app.task_stop_notifications import create_task_stop_transport
    from supervisor.runtime import run_supervisor

    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    run_supervisor(
        {},
        drive_root=tmp_path,
        repo_dir=tmp_path,
        stop_event=stop_event,
        ready_event=ready_event,
        on_error=on_error,
        heartbeat=supervisor_heartbeat(),
        request_restart=lambda: None,
        safe_restart=lambda **_kwargs: (True, "ok"),
        gateway=gateway,
        bridge=message_bus.LocalChatBridge(gateway.outbox),
        agent_events=create_agent_event_transport()[1],
        work_signal=NullWorkSignal(),
        task_stop_publisher=create_task_stop_transport()[0],
    )


def test_a_failed_init_is_reported_and_stops_the_transports(tmp_path, monkeypatch) -> None:
    import supervisor.bootstrap as bootstrap

    def boom(**_kwargs):
        raise RuntimeError("wiring exploded")

    monkeypatch.setattr(bootstrap, "build_supervisor_runtime", boom)
    errors: list[str] = []
    stop_event = threading.Event()
    ready_event = threading.Event()

    _run_supervisor(tmp_path, on_error=errors.append, stop_event=stop_event, ready_event=ready_event)

    assert errors and "Supervisor init failed" in errors[0]
    assert ready_event.is_set()
    assert stop_event.is_set()


def test_stop_interrupts_supervisor_backoff(supervisor, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_SUPERVISOR_RETRY_INITIAL_DELAY_SEC", "30")
    monkeypatch.setenv("OUROBOROS_SUPERVISOR_RETRY_MAX_DELAY_SEC", "30")
    loop = _loop(supervisor)
    waiting = threading.Event()
    wait = loop.stop_event.wait
    ticks = []

    def backoff(seconds):
        waiting.set()
        return wait(seconds)

    def tick():
        ticks.append(1)
        raise RuntimeError("tick failed")

    monkeypatch.setattr(loop.stop_event, "wait", backoff)
    monkeypatch.setattr(loop, "run_tick", tick)
    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()
    try:
        assert waiting.wait(1)
    finally:
        loop.stop_event.set()
        thread.join(1)
    assert not thread.is_alive()
    assert ticks == [1]


# -- timeout notices: a real source, and never mistaken for an answer -----------


def _iso_ago(seconds: float) -> str:
    return (datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).isoformat()


def _overdue_running_task(store, *, task_id: str, source: str, chat_id: int | None = 42) -> None:
    record = store.tasks_repo.create_task(
        TaskRequest(
            id=task_id,
            type="task",
            text="left running by a killed process",
            chat_id=chat_id,
            source=source,
            scope=TEST_SCOPE.to_dict(),
        ),
        status="running",
    )
    record.started_at = _iso_ago(120.0)
    record.heartbeat_at = record.started_at
    store.tasks_repo.upsert_task(record)


def _sweep(
    tmp_path,
    monkeypatch,
    *,
    task_source: str,
    owner_source: str | None,
    hard_timeout: int = 60,
    task_chat_id: int | None = 42,
) -> list:
    store = runtime_persistence.initialize()
    _overdue_running_task(store, task_id="stale", source=task_source, chat_id=task_chat_id)
    state_repo = get_state_repository(
        tmp_path,
        owner_state_repository=sql_owner_state_repository(),
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    with state_repo.mutate() as state:
        state.owner_id = 42
        # No source is no claim: an owner with no stored scope has no chat to address.
        state.owner_scope = scope_for(owner_source, 42).to_dict() if owner_source else None
    sent: list = []
    monkeypatch.setattr("supervisor.queue.send_with_budget", lambda *args, **kwargs: sent.append((args, kwargs)))
    queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=1,
        hard_timeout=hard_timeout,
        audit_event_repository=sql_audit_event_repository(),
    )
    queue.enforce_task_timeouts()
    return sent


def test_a_timeout_notice_is_not_re_addressed_to_whoever_is_in_the_process_row(tmp_path, monkeypatch, caplog) -> None:
    """The instance's stored owner used to stand in for a record that named no transport.

    That was defensible only while every record belonged to the same person. With real
    owners it addresses one owner's notice to whoever happened to speak last, so the route
    is the record's own and an undeliverable one says so instead.
    """
    with caplog.at_level("WARNING", logger="supervisor.queue"):
        sent = _sweep(tmp_path, monkeypatch, task_source="unknown", owner_source="dialogs")

    assert sent, "a stale running record hard-times out on the first tick"
    assert {kwargs["source"] for _args, kwargs in sent} == {"unknown"}
    assert "has no deliverable source" in caplog.text


def test_an_unroutable_timeout_notice_is_no_longer_silent(tmp_path, monkeypatch, caplog) -> None:
    with caplog.at_level("WARNING", logger="supervisor.queue"):
        sent = _sweep(tmp_path, monkeypatch, task_source="unknown", owner_source=None)

    assert {kwargs["source"] for _args, kwargs in sent} == {"unknown"}
    assert "has no deliverable source" in caplog.text
    assert "stale" in caplog.text


def test_a_notice_addresses_the_records_own_scope(tmp_path, monkeypatch) -> None:
    """The scope is the identity; the chat column beside it is a denormalized copy.

    The route used to fall back to the owner's chat whenever that column was empty while the
    scope stayed the record's, so the notice was filed under one conversation and delivered
    to another. One value carries both now, so there is nothing left to disagree.
    """
    sent = _sweep(tmp_path, monkeypatch, task_source="dialogs", owner_source="dialogs", task_chat_id=None)

    assert sent, "a stale running record hard-times out on the first tick"
    for _args, kwargs in sent:
        assert kwargs["scope"] == TEST_SCOPE


def test_a_record_with_no_scope_at_all_is_announced_to_nobody_and_is_not_requeued(tmp_path, monkeypatch) -> None:
    """A row predating the rekey names no identity, and none may be invented for it.

    It is not announced at all: the owner recorded in the process row is not its owner, and
    telling that person about a stranger's task is worse than telling nobody. The retry is
    refused rather than raising out of the sweep — the other overdue tasks on this tick still
    owe their own notices.
    """
    store = runtime_persistence.initialize()
    _overdue_running_task(store, task_id="stale", source="dialogs", chat_id=None)
    stripped = store.tasks_repo.get_task("stale")
    stripped.input = {key: value for key, value in stripped.input.items() if key != "scope"}
    stripped.chat_id = None
    store.tasks_repo.upsert_task(stripped)
    state_repo = get_state_repository(
        tmp_path,
        owner_state_repository=sql_owner_state_repository(),
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    with state_repo.mutate() as state:
        state.owner_id = 42
        state.owner_scope = scope_for("dialogs", 42).to_dict()
    sent: list = []
    monkeypatch.setattr("supervisor.queue.send_with_budget", lambda *args, **kwargs: sent.append((args, kwargs)))
    queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=1,
        hard_timeout=1,
        audit_event_repository=sql_audit_event_repository(),
    )

    queue.enforce_task_timeouts()

    assert sent == []
    assert [record.task_id for record in queue.list_pending_records()] == []
    assert store.tasks_repo.get_task("stale").status == "failed"


def test_a_soft_timeout_notice_is_published_as_progress(tmp_path, monkeypatch) -> None:
    """a2a returns the first non-progress text as the caller's answer."""
    sent = _sweep(tmp_path, monkeypatch, task_source="a2a", owner_source="dialogs", hard_timeout=10_000)

    assert len(sent) == 1
    _args, kwargs = sent[0]
    assert kwargs["is_progress"] is True


def test_a_hard_timeout_notice_is_not_progress(tmp_path, monkeypatch) -> None:
    sent = _sweep(tmp_path, monkeypatch, task_source="a2a", owner_source="dialogs")

    hard = [kwargs for _args, kwargs in sent if "Hard-timeout" in _args[0]]
    assert hard and all(not kwargs.get("is_progress") for kwargs in hard)


# -- an attachment reports acceptance, not delivery -----------------------------


class _BrokenOutbox(IOutbox):
    """Publishing fails the way a broker outage would."""

    def reader(self, *, name: str, mode: SubscriptionMode) -> IOutboxReader:
        raise NotImplementedError

    def publish(self, message: OutboundMessage, *, before_publish=None) -> None:
        raise RuntimeError("outbox unavailable")


def test_a_queued_file_carries_its_task_id_and_does_not_claim_delivery(supervisor) -> None:
    outbox = supervisor.runtime.gateway.outbox
    reader = outbox.reader(name="regression", mode=SubscriptionMode.BROADCAST)
    try:
        ok, reason = supervisor.runtime.bridge.send_file(
            StoredFile(stored_file(scope=scope_for("dialogs", 42))["id"]),
            scope=scope_for("dialogs", 42),
            filename="report.txt",
            source="dialogs",
            task_id="task-9",
        )
        delivered = reader.poll(1.0)
    finally:
        reader.close()

    assert ok is True
    assert reason == "queued", "the bytes have not left the process yet"
    assert delivered is not None
    assert delivered.message.task_id == "task-9"


def test_a_file_that_cannot_be_published_reports_failure(supervisor) -> None:
    bridge = message_bus.LocalChatBridge(_BrokenOutbox(), supervisor.ctx.DRIVE_ROOT)

    ok, reason = bridge.send_file(
        StoredFile(stored_file(scope=scope_for("dialogs", 42))["id"]),
        scope=scope_for("dialogs", 42),
        filename="report.txt",
        source="dialogs",
    )

    assert ok is False
    assert reason


def test_a_photo_and_its_bus_event_share_one_timestamp(supervisor, monkeypatch) -> None:
    """The dead `msg` dict used to mint it; the surviving timestamp must still be shared."""
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))
    reader = supervisor.runtime.gateway.outbox.reader(name="regression", mode=SubscriptionMode.BROADCAST)
    try:
        ok, reason = supervisor.runtime.bridge.send_image(
            StoredFile(stored_file(scope=scope_for("dialogs", 42), filename="image.png", mime="image/png")["id"]),
            scope=scope_for("dialogs", 42),
            mime="image/png",
            source="dialogs",
        )
        delivered = reader.poll(1.0)
    finally:
        reader.close()

    assert (ok, reason) == (True, "queued")
    assert delivered is not None
    assert events and delivered.message.ts == events[-1][1]["ts"]


def test_an_outbound_event_with_no_deliverable_source_is_logged(caplog) -> None:
    """The sentinel was chosen deliberately; the silence around it was not."""
    from ouroboros.agent_events.interfaces import OutboundText
    from supervisor.events import _outbound_source
    from tests.helpers.shared import TEST_SCOPE

    ctx = SimpleNamespace(load_state=lambda: SimpleNamespace(owner_chat_source=""))

    with caplog.at_level("WARNING", logger="supervisor.events"):
        resolved = _outbound_source(OutboundText(scope=TEST_SCOPE, task_id="task-9"), ctx)

    assert resolved == "unknown"
    assert "no deliverable source" in caplog.text
    assert "task-9" in caplog.text


# -- the chat axis is opaque, and the derived view says so ----------------------


def test_the_owners_stored_chat_reads_back_verbatim(supervisor) -> None:
    """A derived view, not a second conversion: storage may not import the seam.

    Converting here would read back `1` for the two distinct conversations `"1"` and `"01"`,
    so one would authorize runtime commands for the other.
    """
    _set_owner(supervisor, owner_id=7, chat_id="01", source="browser")

    state = supervisor.runtime.state_repo.load()
    assert state.owner_chat_id == "01"


def test_two_spellings_of_one_chat_do_not_authorize_each_others_runtime_commands() -> None:
    """`/restart` and `/stop` are what this gate admits, so a fold here is a takeover."""
    from supervisor.runtime import may_run_runtime_command

    owned = {"owner_chat_id": "1", "owner_chat_source": "dialogs"}

    assert may_run_runtime_command("dialogs", "1", **owned) is True
    assert may_run_runtime_command("dialogs", "01", **owned) is False
    assert may_run_runtime_command("dialogs", "3f0a9c1e", **owned) is False


def test_a_non_canonical_chat_stores_one_row(supervisor) -> None:
    """The supervisor re-coerced the axis with a plain `int()`, and folded `"01"` to `1`.

    The transport had already stored the row under `"01"`, so the supervisor's own write
    derived a second identity and the message appeared twice.
    """
    from ouroboros.storage.chat_store import append_chat_message, list_owner_messages

    scope = scope_for("browser", "01")
    append_chat_message(
        supervisor.drive_root,
        scope=scope,
        actor_type="owner",
        chat_id="01",
        user_id="01",
        text="сделай",
        source="browser",
        client_message_id="mid-1",
    )

    loop = _loop(supervisor)
    loop.handle_inbound(InboundEnvelope(scope=scope, user_id="01", text="сделай", client_message_id="mid-1"))

    rows = [row for row in list_owner_messages(supervisor.drive_root, scope=scope) if row.content == "сделай"]
    assert len(rows) == 1


@pytest.mark.integration
@pytest.mark.parametrize("loss", ["expired", "drain"])
def test_real_redis_must_not_send_xadd_after_connection_wait_expires_claim(supervisor, monkeypatch, loss):
    import os
    import uuid

    import redis
    from sqlalchemy import text

    from supervisor import runtime as module
    from transport.infra.redis_gateway import RedisOutbox

    url = os.environ.get("OUROBOROS_TEST_REDIS_URL")
    if not url:
        pytest.skip("set OUROBOROS_TEST_REDIS_URL to run the real Redis connection-wait regression")
    store = runtime_persistence.current
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    store.tasks_repo.upsert_task(
        _terminal_job("publication-redis-attempt", "publication-redis-job", status="cancelled")
    )
    key = f"publication-review:expired-connection:{uuid.uuid4().hex}"
    client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    append = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    client.delete(key)
    reached, resume = threading.Event(), threading.Event()
    get_connection = redis.ConnectionPool.get_connection

    def delayed_connection(pool, *a, **kw):
        connection = get_connection(pool, *a, **kw)
        if threading.current_thread().name.startswith("terminal-publication"):
            assert pool.connection_kwargs["socket_connect_timeout"] <= 0.15
            assert pool.connection_kwargs["socket_timeout"] <= 0.15
            reached.set()
            assert resume.wait(3)
        return connection

    monkeypatch.setattr(redis.ConnectionPool, "get_connection", delayed_connection)
    supervisor.runtime.bridge._outbox = RedisOutbox(client=client, append_client=append, stream=key)
    monkeypatch.setattr(module, "_TERMINAL_TICK_BUDGET_SEC", 0.15)
    loop = _loop(supervisor)
    try:
        loop._publish_terminal_jobs()
        assert reached.is_set()
        assert not loop._publication.done(), "IO survived the tick deadline"
        # The holder was suspended inside connection acquisition; expire exactly its durable claim.
        with store.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE tasks SET terminal_publish_after='2000-01-01T00:00:00+00:00' WHERE task_id='publication-redis-attempt'"
                )
            )
        record = store.tasks_repo.get_task("publication-redis-attempt")
        assert (
            runtime_persistence.current.result_repo.terminal_publication_remaining(
                record.task_id, claim=record.terminal_publish_claim
            )
            == 0
        )
        if loss == "drain":
            loop.stop_event.set()
        resume.set()
        loop._publication.result(timeout=3)
        entries = client.xrange(key)
        assert not store.tasks_repo.get_task(record.task_id).terminal_published_at
        assert entries == [], "XADD reached real Redis only after publication authority expired"
    finally:
        resume.set()
        client.delete(key)
        append.close()
        append.connection_pool.disconnect()
        client.close()
        client.connection_pool.disconnect()


def test_refusal_reconciliation_must_not_bypass_live_admission_handoff(supervisor, monkeypatch):
    store = runtime_persistence.current
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    store.tasks_repo.upsert_task(
        _terminal_job("publication-refused", "publication-refused", status="rejected_duplicate")
    )
    published = []
    reached, resume = threading.Event(), threading.Event()

    def publish(message, *, before_publish=None):
        published.append(message)
        if len(published) == 1:
            reached.set()
            assert resume.wait(3)

    monkeypatch.setattr(supervisor.runtime.gateway.outbox, "publish", publish)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        reply = pool.submit(
            message_bus.send_with_budget,
            "Не удалось запустить фоновую задачу: достигнут лимит активных задач.",
            scope=TEST_SCOPE,
            source="browser",
            task_id="publication-refused",
            client_message_id=f"durable-job-terminal:{TEST_SCOPE.owner_id}:publication-refused",
        )
        try:
            assert reached.wait(2)
            _loop(supervisor)._publish_terminal_jobs()
        finally:
            resume.set()
        assert reply.result(timeout=2)
    assert len(sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, actor_type="agent")) == 1
    assert len(published) == 1, "live admission handoff and terminal claim published the same refused job concurrently"


def test_admission_replay_must_not_resend_refusal_already_accepted_by_reconciler(supervisor, monkeypatch):
    store = runtime_persistence.current
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    store.tasks_repo.upsert_task(
        _terminal_job("publication-refused-replay", "publication-refused-replay", status="rejected_duplicate")
    )
    published = []
    monkeypatch.setattr(supervisor.runtime.gateway.outbox, "publish", lambda message, **kw: published.append(message))
    # Crash after admission commit but before admission response; next supervisor tick reconciles first.
    _loop(supervisor)._publish_terminal_jobs()
    row = store.tasks_repo.get_task("publication-refused-replay")
    assert row.terminal_published_at and row.metadata["terminal_delivery"]["text"] == "accepted"
    assert len(published) == 1
    # The inbound journal then resumes the stored admission outcome with its original terminal identity.
    assert message_bus.send_with_budget(
        "Не удалось запустить фоновую задачу: достигнут лимит активных задач.",
        scope=TEST_SCOPE,
        source="browser",
        task_id="publication-refused-replay",
        client_message_id=f"durable-job-terminal:{TEST_SCOPE.owner_id}:publication-refused-replay",
    )
    assert len(published) == 1, "admission replay deliberately resent a durably accepted refusal"


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("OUROBOROS_TEST_REDIS_URL"),
    reason="set OUROBOROS_TEST_REDIS_URL to run the real Redis publication regression",
)
@pytest.mark.parametrize("stall", [False, True])
def test_accepted_redis_publication_returns_before_post_xadd_maintenance_stall(supervisor, monkeypatch, caplog, stall):
    from tests.helpers.redis_helpers import fresh_prefix, redis_client
    from transport.infra import redis_gateway

    store = runtime_persistence.current
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    store.tasks_repo.upsert_task(_terminal_job("accepted-attempt", "accepted-job"))
    client, append, control = redis_client(), redis_client(), redis_client()
    stream = fresh_prefix()
    orphan = "gone" + redis_gateway._BROADCAST_MARKER + "1"
    for _ in range(2):
        client.xadd(stream, {"payload": "old"}, maxlen=1, approximate=False)
    client.xgroup_create(stream, "behind", id="0")
    client.xgroup_create(stream, orphan, id="0")
    monkeypatch.setattr(redis_gateway, "_MAX_STREAM_ENTRIES", 1)
    outbox = redis_gateway.RedisOutbox(client=client, append_client=append, stream=stream)
    supervisor.runtime.bridge._outbox = outbox
    parse, accepted = append.parse_response, []

    def pause_after_acceptance(connection, command, **kwargs):
        response = parse(connection, command, **kwargs)
        if command == "XADD":
            accepted.append(response)
            if stall:
                control.client_pause(500)
        return response

    monkeypatch.setattr(append, "parse_response", pause_after_acceptance)
    monkeypatch.setattr("supervisor.runtime._TERMINAL_TICK_BUDGET_SEC", 0.15)
    loop = _loop(supervisor)
    try:
        started = time.monotonic()
        loop._publish_terminal_jobs()
        loop._publication.result(timeout=1)
        elapsed = time.monotonic() - started
        assert elapsed < 0.3
        row = store.tasks_repo.get_task("accepted-attempt")
        assert row.metadata["terminal_delivery"]["text"] == "accepted"
        assert row.terminal_published_at
        loop._publish_terminal_jobs()
        assert len(accepted) == 1 and client.xlen(stream) == 1
        assert accepted[0] == client.xrange(stream)[0][0]
        assert orphan not in [group["name"] for group in client.xinfo_groups(stream)]
        assert "has lost messages" in caplog.text
        loop.stop_event.set()
        started = time.monotonic()
        loop.run()
        assert time.monotonic() - started < 0.3
    finally:
        loop.stop_event.set()
        loop.run()
        client.delete(stream)
        for connection in (client, append, control):
            connection.close()
            connection.connection_pool.disconnect()
