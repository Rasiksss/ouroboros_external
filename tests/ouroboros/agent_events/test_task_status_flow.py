import base64
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from new_core.coordination.inprocess import NullWorkSignal
from new_core.identity import OwnerScope
from ouroboros.agent_events.interfaces import (
    SubtaskRequested,
    TaskCancellationRequested,
)
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.files import stored_file
from tests.helpers.scope import scope_for
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader
from tests.helpers.usage import sql_usage_repository
from transport.interfaces import routed_chat_id

#: A dialogs conversation an event addresses.
_CHAT_42 = OwnerScope(owner_id="global", agent_id="main", surface_id="dialogs", chat_id="42")


def test_task_timeout_uses_task_route_without_coercing_chat_id(monkeypatch):
    from supervisor.queue import TaskQueue

    sent = []
    monkeypatch.setattr("supervisor.queue.send_with_budget", lambda *args, **kwargs: sent.append((args, kwargs)))
    route_scope = OwnerScope(owner_id="global", agent_id="main", surface_id="dialogs", chat_id="peer-42")
    task_input = {"transport": {"target_uid": "user-42"}, "scope": route_scope.to_dict()}
    record = SimpleNamespace(
        task_type="task",
        detached=False,
        metadata={},
        source="dialogs",
        chat_id="peer-42",
        input=task_input,
        to_agent_input=task_input.copy,
    )
    meta = SimpleNamespace(
        started_at=time.time() - 2,
        last_heartbeat_at=0,
        worker_id=1,
        attempt=1,
        soft_sent=False,
        record=record,
    )
    queue = SimpleNamespace(
        list_running=lambda: {"task-1": meta},
        state_repo=SimpleNamespace(load=lambda: SimpleNamespace(owner_chat_id="wrong-peer", scope=None)),
        soft_timeout_sec=1,
        hard_timeout_sec=100,
        tasks_repo=SimpleNamespace(
            get_task=lambda _task_id: record,
            update_task_status=lambda *_a, **_k: None,
            # The soft notice is claimed, and this record's timeouts keep it on the soft path.
            claim_soft_timeout_notice=lambda _task_id: True,
        ),
    )
    queue._notify_owner = TaskQueue._notify_owner.__get__(queue)
    queue._notice_route = TaskQueue._notice_route.__get__(queue)
    queue._retry_request = TaskQueue._retry_request.__get__(queue)

    TaskQueue.enforce_task_timeouts(queue)

    # The chat is not an argument any more: the door reads it off the scope it is given.
    assert routed_chat_id(sent[0][1]["scope"]) == "peer-42"
    # A soft timeout is a status update about work still running, not an answer. The
    # notice is addressed by the overdue record's own scope, never by a mint.
    assert sent[0][1] == {
        "scope": route_scope,
        "source": "dialogs",
        "transport": {"target_uid": "user-42"},
        "is_progress": True,
    }


def _orchestrator(tmp_path):
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import TaskQueue
    from supervisor.state import RuntimeStateRepository

    store = runtime_persistence.initialize()
    state_repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=sql_owner_state_repository(),
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    return (
        store,
        state_repo,
        TaskOrchestrator(
            conversations_repo=runtime_persistence.current.conversations_repo,
            tasks_repo=runtime_persistence.current.tasks_repo,
            create_root_task=runtime_persistence.current.create_root_task,
            task_queue=TaskQueue(
                store.tasks_repo,
                tmp_path,
                work_signal=NullWorkSignal(),
                audit_event_repository=sql_audit_event_repository(),
            ),
            state_repo=state_repo,
            tool_confirmation_reader=sql_tool_confirmation_reader(),
        ),
    )


def _task_ctx(tmp_path, **overrides):
    data = {
        "task_depth": 0,
        "pending_events": [],
        "drive_root": tmp_path,
        "task_id": "parent1",
        "root_task_id": "root1",
        "scope": OwnerScope(owner_id="global", agent_id="main", surface_id="dialogs", chat_id="42"),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_schedule_task_creates_requested_status_file(tmp_path):
    from ouroboros.storage.task_results import STATUS_REQUESTED
    from ouroboros.tools.service.orchestration import _schedule_task

    ctx = SimpleNamespace(
        task_depth=0,
        pending_events=[],
        drive_root=tmp_path,
        scope=OwnerScope(owner_id="global", agent_id="main", surface_id="dialogs", chat_id="42"),
    )

    result = _schedule_task(ctx, "Do the thing", context="Model focus A")

    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import load_task_result

    runtime_persistence.initialize()
    assert "Task request queued" in result
    task_id = ctx.pending_events[0].task_id
    data = load_task_result(tmp_path, task_id)
    assert data is not None
    assert data["status"] == STATUS_REQUESTED
    assert data["description"] == "Do the thing"
    assert data["context"] == "Model focus A"


class _StatusAtPublish:
    """A publisher that records what the task record said when the request reached it."""

    def __init__(self, drive_root):
        self.drive_root = drive_root
        self.seen = []

    def publish(self, event):
        from ouroboros.storage.task_results import load_task_result

        self.seen.append((event, (load_task_result(self.drive_root, event.task_id) or {}).get("status")))


def _publishing_ctx(tmp_path, publisher, **overrides):
    from ouroboros.tools.events import PublishedToolEvents

    return _task_ctx(
        tmp_path,
        events=PublishedToolEvents(publisher, scope=TEST_SCOPE, audit_event_repository=None),
        **overrides,
    )


def test_schedule_task_hands_the_request_to_the_supervisor_before_the_run_ends(tmp_path):
    """P50 (ADR-0052): the child must start while its parent still runs, so the request leaves with the call."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import STATUS_REQUESTED
    from ouroboros.tools.service.orchestration import _schedule_task

    runtime_persistence.initialize()
    publisher = _StatusAtPublish(tmp_path)
    ctx = _publishing_ctx(tmp_path, publisher)

    result = _schedule_task(ctx, "Do the thing")

    [(event, status_then)] = publisher.seen
    assert isinstance(event, SubtaskRequested)
    assert event.task_id in result
    assert status_then == STATUS_REQUESTED, "the record must exist before the supervisor hears of the child"
    assert ctx.pending_events == [], "a request already handed over must not be reported again when the run ends"


def test_schedule_task_keeps_the_request_pending_when_nobody_can_take_it(tmp_path):
    from ouroboros.tools.service.orchestration import _schedule_task

    ctx = _task_ctx(tmp_path)  # no `events`: the context of a run without a publisher

    _schedule_task(ctx, "Do the thing")

    [event] = ctx.pending_events
    assert isinstance(event, SubtaskRequested)


def test_schedule_task_of_a_detached_job_still_reports_through_the_run_result(tmp_path, monkeypatch):
    """A durable job closes its attempt with the list of its children (`pending_child_ids`)."""
    from ouroboros.tools.service.orchestration import _schedule_task

    monkeypatch.setattr("ouroboros.tools.service.orchestration.write_task_result", lambda *_a, **_k: None)
    publisher = _StatusAtPublish(tmp_path)
    ctx = _publishing_ctx(tmp_path, publisher, detached=True, execution_claim="claim-1", job_id="job-1")

    _schedule_task(ctx, "Do the thing")

    assert publisher.seen == []
    assert [type(event) for event in ctx.pending_events] == [SubtaskRequested]


def test_cross_owner_parent_cannot_supply_lineage(tmp_path):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.tools.service.orchestration import _schedule_task
    from tests.helpers.shared import make_tool_ctx

    store = runtime_persistence.initialize()
    other_scope = scope_with(TEST_SCOPE, owner_id="other-owner")
    store.tasks_repo.create_task(TaskRequest(id="owner-a-root", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.create_task(
        TaskRequest(
            id="foreign-parent", scope=other_scope.to_dict(), parent_task_id="foreign-root", root_task_id="foreign-root"
        )
    )
    parent_ctx = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_id="owner-a-root",
    )

    _schedule_task(parent_ctx, "Owner A child", parent_task_id="foreign-parent")
    [child] = parent_ctx.pending_events
    assert (child.parent_task_id, child.root_task_id) == ("owner-a-root", "owner-a-root")
    stored_child = store.tasks_repo.get_task(child.task_id)
    assert stored_child is not None
    assert stored_child.metadata["parent_task_id"] == "owner-a-root"


def test_inbound_envelope_and_task_request_preserve_trace_id(tmp_path):
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import TaskQueue
    from supervisor.state import RuntimeStateRepository
    from tests.helpers.supervisor import envelope_from_chat

    trace_id = "463ac35c-9f64-13ad-4848-5a3953bb6124"
    store = runtime_persistence.initialize()
    orchestrator = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=TaskQueue(
            store.tasks_repo,
            tmp_path,
            work_signal=NullWorkSignal(),
            audit_event_repository=sql_audit_event_repository(),
        ),
        state_repo=RuntimeStateRepository(
            tmp_path,
            owner_state_repository=sql_owner_state_repository(),
            scope=TEST_SCOPE,
            usage_repository=sql_usage_repository(),
            audit_event_repository=sql_audit_event_repository(),
        ),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )
    envelope = envelope_from_chat(
        chat_id=0,
        user_id=0,
        text="hello",
        trace_id=trace_id,
    )

    result = orchestrator.submit(envelope)
    task = store.tasks_repo.get_task(result.task_id)

    assert envelope.trace_id == trace_id
    assert envelope.chat_id == "0"
    assert envelope.user_id == 0
    assert task is not None
    assert task.to_agent_input()["chat_id"] == "0"
    assert task.to_agent_input()["trace_id"] == trace_id


@pytest.mark.parametrize(
    "legacy_image",
    [False, True],
    ids=["attachments", "legacy-image"],
)
def test_a_message_carrying_a_payload_is_queued(tmp_path: Path, legacy_image: bool) -> None:
    from supervisor.orchestrator.intent import SubmissionMode
    from transport.interfaces import InboundEnvelope

    _store, _state_repo, orchestrator = _orchestrator(tmp_path)
    scope = scope_for("browser", 42)
    if legacy_image:
        payload = {
            "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\npayload").decode("ascii"),
            "image_mime": "image/png",
        }
    else:
        attachment = stored_file(scope=scope)
        payload = {"attachment_ids": [attachment["id"]], "attachments": [attachment]}

    result = orchestrator.submit(InboundEnvelope(scope=scope, text="check payload", **payload))

    assert result.mode == SubmissionMode.QUEUE


@pytest.mark.parametrize("source", ["dialogs", "sberchat"], ids=["dialogs", "legacy-sberchat"])
def test_ingress_keys_the_task_on_the_ownership_axes(tmp_path, source):
    from tests.helpers.supervisor import envelope_from_chat

    store, _state_repo, orchestrator = _orchestrator(tmp_path)

    envelope = envelope_from_chat(chat_id=42, text="сделай", source=source)
    result = orchestrator.submit(envelope)
    task = store.tasks_repo.get_task(result.task_id)

    assert task is not None
    assert (task.owner_id, task.agent_id) == (envelope.scope.owner_id, envelope.scope.agent_id)
    assert not hasattr(task, "conversation_id")


def test_every_participant_of_one_dialogs_chat_is_queued(tmp_path):
    """Two people share one scope, and both are queued — nothing lands in a run in flight.

    The old inject path compared the participant to keep the second person's message out
    of the first one's task. With one way in, there is nothing to compare: the messages
    run in the order they arrived, which is the order both people saw.
    """
    from supervisor.orchestrator.intent import SubmissionMode
    from transport.interfaces import InboundEnvelope

    store, _state_repo, orchestrator = _orchestrator(tmp_path)

    first = orchestrator.submit(
        InboundEnvelope(
            scope=scope_for("dialogs", 42),
            user_id="alice",
            text="и ещё вот что",
            transport={"target_uid": "alice"},
        )
    )
    second = orchestrator.submit(
        InboundEnvelope(
            scope=scope_for("dialogs", 42),
            user_id="bob",
            text="а мне другое",
            transport={"target_uid": "bob"},
        )
    )

    assert (first.mode, second.mode) == (SubmissionMode.QUEUE, SubmissionMode.QUEUE)
    assert first.task_id != second.task_id
    assert {store.tasks_repo.get_task(first.task_id).status, store.tasks_repo.get_task(second.task_id).status} == {
        "queued"
    }


def test_orchestrator_normalizes_legacy_image_to_persisted_attachment(tmp_path):
    from ouroboros.agent.context import build_user_content
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages
    from transport.interfaces import InboundEnvelope

    store, _state_repo, orchestrator = _orchestrator(tmp_path)
    raw = b"\x89PNG\r\n\x1a\npayload"

    result = orchestrator.submit(
        InboundEnvelope(
            scope=scope_for("browser", 42),
            user_id=7,
            text="check image",
            client_message_id="legacy-image-1",
            image_base64=base64.b64encode(raw).decode("ascii"),
            image_mime="image/png",
        )
    )
    task = store.tasks_repo.get_task(result.task_id)

    assert task is not None
    agent_input = task.to_agent_input()
    assert agent_input["image_base64"] == ""
    assert len(agent_input["attachments"]) == 1
    attachment = agent_input["attachments"][0]
    assert attachment["mime"] == "image/png"
    assert (
        get_storage_provider().get_bytes(
            TEST_SCOPE,
            SCOPE_UPLOADS,
            attachment["storage_key"],
        )
        == raw
    )
    assert base64.b64encode(raw).decode("ascii") not in str(build_user_content(agent_input))
    assert attachment["id"] in str(build_user_content(agent_input))
    messages = list_owner_messages(tmp_path, scope=scope_for("browser", 42))
    assert messages[0].metadata["attachments"][0]["id"] == attachment["id"]


def test_orchestrator_retains_a_durable_file_for_retry_when_enqueue_fails(tmp_path, monkeypatch):
    from ouroboros.storage.chat_store import list_owner_messages
    from transport.interfaces import InboundEnvelope

    store, _state_repo, orchestrator = _orchestrator(tmp_path)
    envelope = InboundEnvelope(
        scope=scope_for("browser", 42),
        user_id=7,
        text="check image",
        client_message_id="legacy-image-failed",
        image_base64=base64.b64encode(b"\x89PNG\r\n\x1a\npayload").decode("ascii"),
        image_mime="image/png",
    )
    with monkeypatch.context() as failure:
        failure.setattr(
            runtime_persistence.current.create_root_task,
            "execute",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("queue down")),
        )
        with pytest.raises(OSError, match="queue down"):
            orchestrator.submit(envelope)
    messages = list_owner_messages(tmp_path, scope=envelope.scope)
    assert len(messages) == 1 and not messages[0].metadata["inbound_processed"]
    assert messages[0].attachments[0]["status"] == "ready"
    result = orchestrator.submit(envelope)
    assert (
        store.tasks_repo.get_task(result.task_id).to_agent_input()["attachments"][0]["id"]
        == messages[0].attachments[0]["id"]
    )
    assert len(list_owner_messages(tmp_path, scope=envelope.scope)) == 1


def test_orchestrator_deduplicates_inbound_task_across_session_change(tmp_path):
    from transport.interfaces import InboundEnvelope

    store, state_repo, orchestrator = _orchestrator(tmp_path)
    envelope = InboundEnvelope(
        scope=scope_for("dialogs", "peer-42"),
        user_id="user-42",
        text="same update",
        client_message_id="stable-mid",
    )

    state = state_repo.load()
    state.session_id = "session-before-restart"
    state_repo.save(state)
    first = orchestrator.submit(envelope)
    state.session_id = "session-after-restart"
    state_repo.save(state)
    replay = orchestrator.submit(envelope)

    assert replay.task_id == first.task_id
    assert len(store.tasks_repo.list_tasks()) == 1


def test_orchestrator_rejects_invalid_legacy_image_without_queueing(tmp_path):
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from transport.interfaces import InboundEnvelope

    store, _state_repo, orchestrator = _orchestrator(tmp_path)

    with pytest.raises(ValueError, match="image"):
        orchestrator.submit(
            InboundEnvelope(
                scope=scope_for("browser", 42),
                text="bad image",
                image_base64=base64.b64encode(b"not-an-image").decode("ascii"),
                image_mime="image/png",
            )
        )

    assert store.tasks_repo.list_tasks() == []
    assert get_storage_provider().list_keys(TEST_SCOPE, SCOPE_UPLOADS, "chat/") == []


@pytest.mark.parametrize(
    "message",
    [
        {"chat_id": "other-42", "user_id": "bob", "source": "dialogs"},
        {"chat_id": "42", "user_id": "alice", "source": "browser"},
        {"chat_id": "group-42", "user_id": "alice", "source": "dialogs"},
    ],
    ids=["other-chat", "other-source", "same-scope"],
)
def test_every_scope_takes_the_same_way_in(tmp_path, message):
    """Whatever the message's scope, it is queued. There is no second door to choose."""
    from supervisor.orchestrator.intent import SubmissionMode
    from transport.interfaces import InboundEnvelope

    store, _state_repo, orchestrator = _orchestrator(tmp_path)

    envelope = InboundEnvelope(
        scope=scope_for(message["source"], message["chat_id"]),
        user_id=message["user_id"],
        text="more context",
        transport={"target_uid": message["user_id"]},
    )
    result = orchestrator.submit(envelope)

    assert result.mode == SubmissionMode.QUEUE
    assert store.tasks_repo.get_task(result.task_id).status == "queued"


def test_schedule_task_records_lineage_metadata(tmp_path):
    from ouroboros.storage.task_results import STATUS_RUNNING, load_task_result, write_task_result
    from ouroboros.tools.service.orchestration import _schedule_task

    write_task_result(tmp_path, "parent1", STATUS_RUNNING, scope=TEST_SCOPE, root_task_id="root1", chat_id=42)
    trace_id = "463ac35c-9f64-13ad-4848-5a3953bb6124"
    ctx = _task_ctx(
        tmp_path,
        trace_id=trace_id,
        current_chat_id=0,
        transport={"target_uid": "alice", "conversation_id": "group-42"},
    )

    result = _schedule_task(ctx, "Do the thing", context="Model focus A")

    assert "Task request queued" in result
    event = ctx.pending_events[0]
    task_id = event.task_id
    assert event.parent_task_id == "parent1"
    assert event.root_task_id == "root1"
    # The whole scope travels, not the two axes a transport happens to know.
    assert event.scope == ctx.scope
    assert not hasattr(event, "chat_id")
    assert event.trace_id == trace_id
    assert event.source == "dialogs"
    assert not hasattr(event, "conversation_id")
    assert event.transport == {"target_uid": "alice", "conversation_id": "group-42"}

    data = load_task_result(tmp_path, task_id)
    assert data is not None
    assert data["parent_task_id"] == "parent1"
    assert data["root_task_id"] == "root1"
    assert data["chat_id"] == "42"
    assert data["trace_id"] == trace_id


def test_schedule_task_blocks_eleventh_outstanding_child_for_parent(tmp_path):
    from ouroboros.storage.task_results import STATUS_REQUESTED, write_task_result
    from ouroboros.tools.service.orchestration import _schedule_task

    for index in range(10):
        write_task_result(
            tmp_path,
            f"child{index}",
            STATUS_REQUESTED,
            scope=TEST_SCOPE,
            parent_task_id="parent1",
            root_task_id="root1",
            chat_id=42,
        )
    ctx = _task_ctx(tmp_path)

    result = _schedule_task(ctx, "Overflow child")

    assert "SCHEDULE_TASK_BLOCKED" in result
    assert "parent subtask limit" in result
    assert ctx.pending_events == []


def test_schedule_task_ignores_terminal_children_for_parent_limit(tmp_path):
    from ouroboros.storage.task_results import STATUS_COMPLETED, write_task_result
    from ouroboros.tools.service.orchestration import _schedule_task

    for index in range(10):
        write_task_result(
            tmp_path,
            f"child{index}",
            STATUS_COMPLETED,
            scope=TEST_SCOPE,
            parent_task_id="parent1",
            root_task_id="root1",
            chat_id=42,
        )
    ctx = _task_ctx(tmp_path)

    result = _schedule_task(ctx, "Allowed child")

    assert "Task request queued" in result
    assert len(ctx.pending_events) == 1


def test_schedule_task_blocks_twenty_first_outstanding_child_for_root(tmp_path):
    from ouroboros.storage.task_results import STATUS_SCHEDULED, write_task_result
    from ouroboros.tools.service.orchestration import _schedule_task

    for index in range(20):
        write_task_result(
            tmp_path,
            f"child{index}",
            STATUS_SCHEDULED,
            scope=TEST_SCOPE,
            parent_task_id=f"parent{index}",
            root_task_id="root1",
            chat_id=42,
        )
    ctx = _task_ctx(tmp_path, task_id="fresh-parent")

    result = _schedule_task(ctx, "Overflow root")

    assert "SCHEDULE_TASK_BLOCKED" in result
    assert "root subtask limit" in result
    assert ctx.pending_events == []


def test_get_task_result_returns_full_completed_output(tmp_path):
    from ouroboros.storage.task_results import STATUS_COMPLETED, write_task_result
    from ouroboros.tools.service.orchestration import _get_task_result

    full_text = ("hello\n" * 1200) + "TAIL_MARKER"
    write_task_result(
        tmp_path,
        "abc123",
        STATUS_COMPLETED,
        scope=TEST_SCOPE,
        result=full_text,
        trace_summary="trace",
    )

    ctx = SimpleNamespace(drive_root=tmp_path, scope=TEST_SCOPE)
    output = _get_task_result(ctx, "abc123")

    assert "TAIL_MARKER" in output
    assert full_text in output
    assert "[BEGIN_SUBTASK_OUTPUT]" in output


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("address", ["logical-job", "attempt-2"])
def test_result_and_cancel_resolve_an_owned_logical_job_to_its_current_attempt(tmp_path, monkeypatch, enabled, address):
    from ouroboros.storage.runtime_models import TaskRecord
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.settings.schema import AppSettings
    from ouroboros.tools.service.orchestration import _cancel_task, _get_task_result, _wait_for_task

    monkeypatch.setattr(
        "ouroboros.tools.service.orchestration.load_settings",
        lambda: AppSettings(OUROBOROS_DURABLE_JOBS_ENABLED=enabled),
    )
    store = runtime_persistence.initialize()
    store.tasks_repo.upsert_task(
        TaskRecord(
            task_id="attempt-2",
            job_id="logical-job",
            detached=True,
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            status="queued",
            result="durable result",
            scope=TEST_SCOPE.to_dict(),
        )
    )
    ctx = _task_ctx(tmp_path)

    for reader in (_get_task_result, _wait_for_task):
        output = reader(ctx, address)
        assert "durable result" in output if enabled else output == "ERROR: Durable jobs are disabled."
    result = _cancel_task(ctx, address)
    if enabled:
        assert result == f"Cancel requested: {address}"
        assert ctx.pending_events[-1].task_id == address
    else:
        assert result == "ERROR: Durable jobs are disabled."
        assert ctx.pending_events == []
        assert store.tasks_repo.claim_next_task(runtime_id="runtime").task_id == "attempt-2"


def test_logical_job_result_uses_the_resolved_attempt_snapshot_during_a_retry(tmp_path, monkeypatch):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.settings.schema import AppSettings
    from ouroboros.tools.service.orchestration import _get_task_result
    from supervisor.queue import TaskQueue

    monkeypatch.setattr(
        "ouroboros.tools.service.orchestration.load_settings", lambda: AppSettings(OUROBOROS_DURABLE_JOBS_ENABLED=True)
    )
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="attempt-1", job_id="logical-job", detached=True, text="work", scope=TEST_SCOPE.to_dict())
    )
    assert store.tasks_repo.claim_next_task(runtime_id="runtime") is not None
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    get_current_job = store.tasks_repo.get_current_job

    def retry_after_lookup(job_id, *, scope):
        record = get_current_job(job_id, scope=scope)
        assert record is not None
        successor = queue._retry_request(record.to_agent_input(), task_id=record.task_id, new_attempt=2, marker="retry")
        assert store.tasks_repo.transition_running(
            record.task_id,
            runtime_id="runtime",
            claim_token=store.tasks_repo.get_task("attempt-1").metadata["execution_claim"],
            result="released",
            kind="instance_released",
            attempt=2,
            successor=successor,
        )
        return record

    monkeypatch.setattr(store.tasks_repo, "get_current_job", retry_after_lookup)

    assert "[running]" in _get_task_result(_task_ctx(tmp_path), "logical-job")


def test_logical_job_cancel_stops_the_current_attempt_when_a_retry_precedes_event_consumption(tmp_path, monkeypatch):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.settings.schema import AppSettings
    from ouroboros.tools.service.orchestration import _cancel_task
    from supervisor.events import _handle_cancel_task
    from supervisor.queue import TaskQueue

    enabled = [True]
    monkeypatch.setattr(
        "ouroboros.tools.service.orchestration.load_settings",
        lambda: AppSettings(OUROBOROS_DURABLE_JOBS_ENABLED=enabled[0]),
    )
    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="attempt-1", job_id="logical-job", detached=True, text="work", scope=TEST_SCOPE.to_dict())
    )
    assert store.tasks_repo.claim_next_task(runtime_id="runtime") is not None
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    event_ctx = _task_ctx(tmp_path)
    assert _cancel_task(event_ctx, "logical-job") == "Cancel requested: logical-job"
    event = event_ctx.pending_events[-1]
    enabled[0] = False
    first = store.tasks_repo.get_task("attempt-1")
    assert first is not None
    successor = queue._retry_request(first.to_agent_input(), task_id=first.task_id, new_attempt=2, marker="retry")
    assert store.tasks_repo.transition_running(
        first.task_id,
        runtime_id="runtime",
        claim_token=store.tasks_repo.get_task("attempt-1").metadata["execution_claim"],
        result="released",
        kind="instance_released",
        attempt=2,
        successor=successor,
    )

    _handle_cancel_task(
        event,
        SimpleNamespace(
            cancel_task_by_id=lambda task_id, scope, job_id="": queue.cancel_task_by_id(
                task_id, scope=scope, job_id=job_id
            ),
            send_with_budget=lambda *_args, **_kwargs: None,
        ),
    )

    current = store.tasks_repo.get_current_job("logical-job", scope=TEST_SCOPE)
    assert current is not None and current.status == "cancelled"


def test_result_and_wait_hide_another_owners_exact_attempt_id(tmp_path):
    from ouroboros.storage.runtime_models import TaskRecord
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.tools.service.orchestration import _get_task_result, _wait_for_task

    store = runtime_persistence.initialize()
    store.tasks_repo.upsert_task(
        TaskRecord(
            task_id="other-attempt",
            owner_id="other-owner",
            agent_id=TEST_SCOPE.agent_id,
            status="completed",
            result="other owner's secret result",
        )
    )
    ctx = _task_ctx(tmp_path)

    assert _get_task_result(ctx, "other-attempt") == "Task other-attempt: unknown or not yet registered"
    assert _wait_for_task(ctx, "other-attempt") == "Task other-attempt: unknown or not yet registered"


def test_stopped_running_detached_attempt_finishes_cancelled(tmp_path):
    from ouroboros.agent.task_pipeline import emit_task_results
    from ouroboros.agent_events.interfaces import TaskFinished
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job-stop", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    assert claimed is not None
    assert store.tasks_repo.stop_jobs(scope=TEST_SCOPE, job_id="job-stop")[0].stop_requested_at
    events = []

    unsuccessful = emit_task_results(
        SimpleNamespace(drive_root=tmp_path),
        SimpleNamespace(),
        events,
        claimed.to_agent_input(),
        "empty-response fallback must not become success",
        {},
        {"tool_calls": []},
        time.time(),
        tmp_path,
        scope=TEST_SCOPE,
        has_answer=False,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    final = store.tasks_repo.get_current_job("job-stop", scope=TEST_SCOPE)
    assert unsuccessful is True
    assert final is not None and final.status == "cancelled" and final.result == "Task cancelled."
    assert [(event.status, event.result) for event in events if isinstance(event, TaskFinished)] == [
        ("cancelled", final.result)
    ]


@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
def test_terminal_detached_loser_emits_only_attempt_audit_and_persisted_verdict(tmp_path, monkeypatch, status):
    from ouroboros.agent import task_pipeline
    from ouroboros.agent_events.interfaces import (
        LlmUsageReported,
        OutboundText,
        SubtaskRequested,
        TaskCancellationRequested,
        TaskFinished,
        TaskMetrics,
    )
    from ouroboros.observability.event_record import list_runtime_events
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job-loser", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    assert claimed is not None
    finish = store.tasks_repo.finish_detached_attempt

    def cancel_before_terminal_cas(*args, **kwargs):
        finish("job-loser", status=status, result="persisted verdict", claim_token=claimed.metadata["execution_claim"])
        return finish(*args, **kwargs)

    monkeypatch.setattr(store.tasks_repo, "finish_detached_attempt", cancel_before_terminal_cas)
    monkeypatch.setattr(
        task_pipeline,
        "_store_task_result",
        lambda *_args, **_kwargs: pytest.fail("a stale attempt must not rewrite its terminal record"),
    )
    monkeypatch.setattr(
        task_pipeline,
        "_record_working_task_episode",
        lambda *_args, **_kwargs: pytest.fail("a stale attempt must not create memory effects"),
    )
    audit = LlmUsageReported(scope=TEST_SCOPE, task_id="job-loser", prompt_tokens=3)
    events = [
        OutboundText(scope=TEST_SCOPE, text="stale proactive message"),
        SubtaskRequested(scope=TEST_SCOPE, task_id="child"),
        TaskCancellationRequested(scope=TEST_SCOPE, task_id="other-task"),
        TaskFinished(scope=TEST_SCOPE, status="completed", result="stale"),
        audit,
    ]

    audit_repository = sql_audit_event_repository()
    unsuccessful = task_pipeline.emit_task_results(
        SimpleNamespace(drive_root=tmp_path),
        SimpleNamespace(),
        events,
        claimed.to_agent_input(),
        "stale answer",
        {"rounds": 2, "prompt_tokens": 3, "completion_tokens": 4},
        {"tool_calls": []},
        time.time(),
        tmp_path,
        ctx=SimpleNamespace(scope=TEST_SCOPE, terminal_attachments=[]),
        scope=TEST_SCOPE,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=audit_repository,
    )

    assert unsuccessful is True
    assert events[0] == audit
    assert [type(event) for event in events[1:]] == [TaskMetrics, TaskFinished]
    assert not any(isinstance(event, OutboundText) for event in events)
    assert (events[-1].status, events[-1].result) == (status, "persisted verdict")
    assert (events[-1].total_rounds, events[-1].prompt_tokens, events[-1].completion_tokens) == (2, 3, 4)
    assert len(list_runtime_events(event_type="task_eval", audit_event_repository=audit_repository)) == 1
    assert store.tasks_repo.get_task("job-loser").result == "persisted verdict"


@pytest.mark.parametrize("verdict", ["missing", "running", "unavailable"])
def test_missing_detached_terminal_verdict_never_fabricates_a_finish(tmp_path, monkeypatch, verdict):
    from sqlalchemy import text

    from ouroboros.agent import task_pipeline
    from ouroboros.agent_events.interfaces import OutboundText, TaskFinished
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job-missing", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    assert claimed is not None

    def disappear_before_terminal_cas(*_args, **_kwargs):
        if verdict == "missing":
            with store.engine.begin() as conn:
                conn.execute(text("DELETE FROM tasks WHERE task_id='job-missing'"))
        if verdict == "unavailable":
            monkeypatch.setattr(
                store.tasks_repo,
                "get_current_job",
                lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("reread unavailable")),
            )
        return None

    monkeypatch.setattr(store.tasks_repo, "finish_detached_attempt", disappear_before_terminal_cas)
    events = [OutboundText(scope=TEST_SCOPE, text="stale answer")]

    from contextlib import nullcontext

    with pytest.raises(RuntimeError, match="reread unavailable") if verdict == "unavailable" else nullcontext():
        unsuccessful = task_pipeline.emit_task_results(
            SimpleNamespace(drive_root=tmp_path),
            SimpleNamespace(),
            events,
            claimed.to_agent_input(),
            "stale answer",
            {},
            {"tool_calls": []},
            time.time(),
            tmp_path,
            scope=TEST_SCOPE,
            execution_memory_repository=sql_execution_memory_repository(),
            audit_event_repository=sql_audit_event_repository(),
        )

        assert unsuccessful is True
    assert not any(isinstance(event, OutboundText) for event in events)
    assert not any(isinstance(event, TaskFinished) for event in events)


def test_wait_for_task_reports_rejected_duplicate(tmp_path):
    from ouroboros.storage.task_results import STATUS_REJECTED_DUPLICATE, write_task_result
    from ouroboros.tools.service.orchestration import _wait_for_task

    write_task_result(
        tmp_path,
        "dup123",
        STATUS_REJECTED_DUPLICATE,
        scope=TEST_SCOPE,
        duplicate_of="orig999",
        result="Task was rejected as semantically similar to already active task orig999.",
    )

    ctx = SimpleNamespace(drive_root=tmp_path, scope=TEST_SCOPE)
    output = _wait_for_task(ctx, "dup123")

    assert "rejected_duplicate" in output
    assert "duplicate_of=orig999" in output


def test_wait_for_task_blocks_child_deadlock_with_single_worker(tmp_path, monkeypatch):
    from ouroboros.storage.task_results import STATUS_REQUESTED, write_task_result
    from ouroboros.tools.service import orchestration
    from ouroboros.tools.service.orchestration import _wait_for_task

    monkeypatch.setattr(orchestration, "_worker_slots", lambda: 1)
    write_task_result(
        tmp_path,
        "child123",
        STATUS_REQUESTED,
        scope=TEST_SCOPE,
        parent_task_id="parent1",
        root_task_id="root1",
        chat_id=42,
    )
    ctx = _task_ctx(tmp_path)

    output = _wait_for_task(ctx, "child123")

    assert "WAIT_BLOCKED_SINGLE_WORKER" in output


def test_wait_for_task_allows_child_wait_with_multiple_workers(tmp_path, monkeypatch):
    from ouroboros.storage.task_results import STATUS_COMPLETED, write_task_result
    from ouroboros.tools.service import orchestration
    from ouroboros.tools.service.orchestration import _wait_for_task

    monkeypatch.setattr(orchestration, "_worker_slots", lambda: 2)
    write_task_result(
        tmp_path,
        "child123",
        STATUS_COMPLETED,
        scope=TEST_SCOPE,
        result="done",
        parent_task_id="parent1",
        root_task_id="root1",
        chat_id=42,
    )
    ctx = _task_ctx(tmp_path)

    output = _wait_for_task(ctx, "child123")

    assert "WAIT_BLOCKED_SINGLE_WORKER" not in output
    assert "done" in output


def test_handle_schedule_task_duplicate_writes_rejected_status(tmp_path, monkeypatch):
    from ouroboros.storage.task_results import STATUS_REJECTED_DUPLICATE
    from supervisor import events as ev_module

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: "orig111")

    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path

        def load_state(self):
            from ouroboros.storage.runtime_models import OwnerRuntimeState

            return OwnerRuntimeState(owner_chat_id=1)

        def send_with_budget(self, text, **kwargs):
            sent.append((routed_chat_id(kwargs["scope"]), text, kwargs))

    ev_module._handle_schedule_task(
        SubtaskRequested(
            scope=_CHAT_42,
            task_id="dup222",
            description="Do the thing",
            context="Model focus B",
            depth=1,
            parent_task_id="parent1",
            root_task_id="root1",
        ),
        FakeCtx(),
    )

    from ouroboros.storage.task_results import load_task_result

    data = load_task_result(tmp_path, "dup222")
    assert data is not None
    assert data["status"] == STATUS_REJECTED_DUPLICATE
    assert data["duplicate_of"] == "orig111"
    assert data["parent_task_id"] == "parent1"
    assert data["root_task_id"] == "root1"
    assert data["chat_id"] == _CHAT_42.chat_id
    assert sent and "Task rejected" in sent[0][1]


@pytest.mark.parametrize("source", ["browser", "dialogs"])
def test_handle_schedule_task_preserves_route_in_runtime_record(
    tmp_path,
    monkeypatch,
    source,
):
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import STATUS_REQUESTED, STATUS_RUNNING, STATUS_SCHEDULED
    from ouroboros.tools.service.orchestration import _schedule_task
    from supervisor import events as ev_module
    from supervisor.queue import TaskQueue

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)

    transport = {"target_uid": "alice"}
    tool_ctx = _task_ctx(
        tmp_path,
        scope=OwnerScope(owner_id="global", agent_id="main", surface_id=source, chat_id="1"),
        transport=transport,
    )
    _schedule_task(tool_ctx, "Do the thing", context="Model focus C")

    event = tool_ctx.pending_events[0]
    task_id = event.task_id
    store = runtime_persistence.initialize()
    requested = store.tasks_repo.get_task(task_id)
    assert requested is not None
    assert requested.status == STATUS_REQUESTED
    assert requested.source == source
    assert (requested.owner_id, requested.agent_id) == ("global", "main")
    requested_created_at = requested.created_at

    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    claims = []
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path

        def load_state(self):
            from ouroboros.storage.runtime_models import OwnerRuntimeState

            return OwnerRuntimeState(owner_chat_id=None)

        def enqueue_task(self, task):
            scheduled = queue.enqueue_dict(task)
            claims.append((scheduled.status, queue.claim_next(worker_id=7, runtime_id="test-runtime")))
            return claims[-1][1]

        def send_with_budget(self, text, **kwargs):
            sent.append((routed_chat_id(kwargs["scope"]), text, kwargs))

    ev_module._handle_schedule_task(event, FakeCtx())

    running = store.tasks_repo.get_task(task_id)
    assert claims[0][0] == STATUS_SCHEDULED
    assert claims[0][1] is not None
    assert running is not None
    assert running.status == STATUS_RUNNING
    assert running.worker_id == 7
    assert running.source == source
    assert (running.owner_id, running.agent_id) == ("global", "main")
    assert running.input["source"] == source
    # The agent's own dict carries the minted scope and neither storage key.
    assert running.input["scope"] == {"owner_id": "global", "agent_id": "main", "surface_id": source, "chat_id": "1"}
    assert "conversation_id" not in running.input
    assert "session_id" not in running.input
    assert running.input["transport"] == transport
    assert running.created_at == requested_created_at
    assert running.metadata["parent_task_id"] == "parent1"
    assert running.metadata["root_task_id"] == "root1"
    assert queue.claim_next(worker_id=8, runtime_id="test-runtime") is None
    assert sent and "Scheduled task" in sent[0][1]
    assert sent[0][0] == 1
    assert sent[0][2]["source"] == source
    assert sent[0][2]["transport"] == transport


@pytest.mark.parametrize(("detached", "has_ack"), [(True, False), (False, True)])
def test_schedule_event_from_detached_origin_enqueues_without_ack(tmp_path, monkeypatch, detached, has_ack):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import STATUS_SCHEDULED
    from ouroboros.tools.service.orchestration import _schedule_task
    from supervisor import events as ev_module
    from supervisor.queue import TaskQueue

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    tool_ctx = _task_ctx(tmp_path, detached=detached)
    store = runtime_persistence.initialize()
    if detached:
        store.tasks_repo.create_task(TaskRequest(id=tool_ctx.task_id, detached=True, scope=tool_ctx.scope.to_dict()))
        held = store.tasks_repo.claim_next_task(runtime_id="runtime")
        tool_ctx.execution_claim = held.metadata["execution_claim"]
    _schedule_task(tool_ctx, "child", context="hello")
    event = tool_ctx.pending_events[0]
    sent = []
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )

    ev_module._handle_schedule_task(
        event,
        SimpleNamespace(
            DRIVE_ROOT=tmp_path,
            enqueue_task=queue.enqueue_dict,
            send_with_budget=lambda text, **kwargs: sent.append((text, kwargs)),
        ),
    )

    scheduled = store.tasks_repo.get_task(event.task_id)
    assert scheduled is not None and scheduled.status == STATUS_SCHEDULED
    assert event.suppress_ack is detached
    assert bool(sent) is has_ack


def test_a_scheduling_message_cannot_be_built_without_a_scope():
    """A scheduling message carries the scope of the run that planned the subtask.

    The supervisor used to mint one from the event's source and chat id, filling the owner
    and the agent from process configuration. There is nothing left to fall back to and
    nothing left to check at the handler: the scope is required on the message base, so a
    producer that forgot whose subtask this is cannot construct the message at all.
    """
    with pytest.raises(TypeError):
        SubtaskRequested(task_id="scopeless-1", description="Do the thing", depth=1)  # type: ignore[call-arg]


def test_cancel_task_event_preserves_routing_context(tmp_path):
    from ouroboros.tools.service.orchestration import _cancel_task

    ctx = _task_ctx(
        tmp_path,
        scope=OwnerScope(owner_id="global", agent_id="main", surface_id="dialogs", chat_id="peer-42"),
        transport={"target_uid": "alice", "source_message_id": "mid-1"},
    )

    assert _cancel_task(ctx, "child-1") == "Cancel requested: child-1"
    assert ctx.pending_events == [
        TaskCancellationRequested(
            scope=ctx.scope,
            task_id="child-1",
            source="dialogs",
            transport={"target_uid": "alice", "source_message_id": "mid-1"},
            ts=ctx.pending_events[0].ts,
        )
    ]


def test_handle_cancel_task_replies_to_event_route():
    from supervisor.events import _handle_cancel_task

    sent = []
    ctx = SimpleNamespace(
        load_state=lambda: SimpleNamespace(owner_chat_id=7),
        cancel_task_by_id=lambda task_id, _scope: task_id == "child-1",
        send_with_budget=lambda text, **kwargs: sent.append((routed_chat_id(kwargs["scope"]), text, kwargs)),
    )

    scope = OwnerScope(owner_id="global", agent_id="main", surface_id="dialogs", chat_id="peer-42")
    _handle_cancel_task(
        TaskCancellationRequested(
            scope=scope,
            task_id="child-1",
            source="dialogs",
            transport={"target_uid": "alice"},
        ),
        ctx,
    )

    assert sent == [
        (
            "peer-42",
            "✅ cancel child-1 (event)",
            {"scope": scope, "source": "dialogs", "transport": {"target_uid": "alice"}},
        )
    ]


@pytest.mark.parametrize(("detached", "has_ack"), [(True, False), (False, True)])
def test_cancel_event_from_detached_origin_cancels_without_ack(tmp_path, detached, has_ack):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.tools.service.orchestration import _cancel_task
    from supervisor.events import _handle_cancel_task
    from supervisor.queue import TaskQueue

    store = runtime_persistence.initialize()
    tool_ctx = _task_ctx(tmp_path, detached=detached)
    store.tasks_repo.create_task(TaskRequest(id="child", text="work", scope=tool_ctx.scope.to_dict()))
    _cancel_task(tool_ctx, "child")
    event = tool_ctx.pending_events[0]
    sent = []
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )

    _handle_cancel_task(
        event,
        SimpleNamespace(
            cancel_task_by_id=lambda task_id, scope, job_id="": queue.cancel_task_by_id(
                task_id, scope=scope, job_id=job_id
            ),
            send_with_budget=lambda text, **kwargs: sent.append((text, kwargs)),
        ),
    )

    cancelled = store.tasks_repo.get_task("child")
    assert cancelled is not None and cancelled.status == "cancelled"
    assert event.suppress_ack is detached
    assert bool(sent) is has_ack


def test_store_task_result_preserves_existing_lineage_metadata(tmp_path):
    from ouroboros.agent.task_pipeline import _store_task_result
    from ouroboros.storage.task_results import STATUS_SCHEDULED, load_task_result, write_task_result

    write_task_result(
        tmp_path,
        "child123",
        STATUS_SCHEDULED,
        scope=TEST_SCOPE,
        parent_task_id="parent1",
        root_task_id="root1",
        chat_id=42,
    )
    env = SimpleNamespace(drive_root=tmp_path)

    _store_task_result(
        env,
        {"id": "child123"},
        "done",
        {"rounds": 1},
        {"tool_calls": [], "reasoning_notes": []},
        load_task_result(tmp_path, "child123") or {},
        scope=TEST_SCOPE,
    )

    data = load_task_result(tmp_path, "child123")
    assert data is not None
    assert data["parent_task_id"] == "parent1"
    assert data["root_task_id"] == "root1"
    assert data["chat_id"] == TEST_SCOPE.chat_id
    assert data["result"] == "done"


def test_runtime_task_request_preserves_trace_id(tmp_path):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence

    store = runtime_persistence.initialize()
    record = store.tasks_repo.create_task(
        TaskRequest(
            scope=TEST_SCOPE.to_dict(),
            id="task-trace",
            type="task",
            chat_id=42,
            text="hello",
            trace_id="463ac35c-9f64-13ad-4848-5a3953bb6124",
        )
    )

    assert record.to_agent_input()["trace_id"] == "463ac35c-9f64-13ad-4848-5a3953bb6124"


def test_handle_schedule_task_depth_reject_sends_trace_id(tmp_path):
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import STATUS_FAILED, STATUS_REQUESTED, write_task_result
    from supervisor import events as ev_module

    trace_id = "463ac35c-9f64-13ad-4848-5a3953bb6124"
    write_task_result(tmp_path, "too-deep", STATUS_REQUESTED, scope=TEST_SCOPE, source="browser")
    sent = []
    queued = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path

        def load_state(self):
            from ouroboros.storage.runtime_models import OwnerRuntimeState

            return OwnerRuntimeState(owner_chat_id=1)

        def enqueue_task(self, task):
            queued.append(task)

        def send_with_budget(self, text, **kwargs):
            sent.append((routed_chat_id(kwargs["scope"]), text, kwargs))

    ev_module._handle_schedule_task(
        SubtaskRequested(
            scope=scope_with(TEST_SCOPE, surface_id="browser", chat_id="1"),
            task_id="too-deep",
            description="Do the thing",
            depth=4,
            source="browser",
            trace_id=trace_id,
        ),
        FakeCtx(),
    )

    assert queued == []
    assert sent and "subtask depth limit" in sent[0][1]
    assert sent[0][2]["trace_id"] == trace_id
    record = runtime_persistence.initialize().tasks_repo.get_task("too-deep")
    assert record is not None
    assert record.status == STATUS_FAILED
    # The column is text, and a chat id filled in by a conditional transition reads back
    # from the column rather than from the payload snapshot it replaced.
    assert str(record.chat_id) == "1"


def test_orchestration_events_carry_no_conversation_key(tmp_path):
    """The supervisor derives its storage key; the agent's events name only the scope's address."""
    from ouroboros.tools.service.orchestration import _cancel_task, _schedule_task

    ctx = _task_ctx(tmp_path)

    _schedule_task(ctx, "Do the thing")
    _cancel_task(ctx, "child-1")

    for event in ctx.pending_events:
        assert not hasattr(event, "conversation_id")
        assert not hasattr(event, "session_id")
        assert not hasattr(event, "chat_id")
        assert event.scope == ctx.scope
        assert event.source == ctx.scope.surface_id


def test_a_skill_bridge_task_can_still_schedule_a_subtask(tmp_path, monkeypatch):
    """The surface is not the stored source: `skill-bridge` has no `TaskSource` member.

    The tool stamps the scope's honest surface on the event; the supervisor normalizes it
    for the column it owns. Without that, `TaskRequest` validation raised inside `enqueue`,
    the failure was swallowed by the dispatcher, and the subtask vanished while the tool had
    already told the model it was queued.
    """
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import STATUS_REQUESTED
    from ouroboros.tools.service.orchestration import _schedule_task
    from supervisor import events as ev_module

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    tool_ctx = _task_ctx(
        tmp_path,
        scope=OwnerScope(owner_id="global", agent_id="main", surface_id="skill-bridge", chat_id="1"),
    )
    _schedule_task(tool_ctx, "child of a skill-bridge task")
    event = tool_ctx.pending_events[0]
    assert event.source == "skill-bridge"

    queued: list[dict] = []
    runtime_ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        load_state=lambda: __import__(
            "ouroboros.storage.runtime_models", fromlist=["OwnerRuntimeState"]
        ).OwnerRuntimeState(owner_chat_id=1),
        enqueue_task=queued.append,
        send_with_budget=lambda *_a, **_k: None,
    )
    ev_module._handle_schedule_task(event, runtime_ctx)

    assert len(queued) == 1, "the subtask never reached the queue"
    # The column's closed set, not the surface — and the scope keeps the honest surface.
    assert queued[0]["source"] == "internal"
    assert queued[0]["scope"]["surface_id"] == "skill-bridge"
    # The dict must survive the validation the real queue performs on it.
    from ouroboros.storage.runtime_models import TaskRequest

    assert TaskRequest.model_validate(queued[0]).source == "internal"

    record = runtime_persistence.initialize().tasks_repo.get_task(event.task_id)
    assert record is not None and record.status == STATUS_REQUESTED


def _other_owner_scope():
    from new_core.identity import OwnerScope, SurfaceId

    return OwnerScope(owner_id="another-owner", agent_id="another-agent", surface_id=SurfaceId("browser"), chat_id="9")


def test_the_duplicate_check_reads_only_the_asking_owners_active_tasks(tmp_path, monkeypatch):
    """Two owners asking the same thing are two tasks, and neither sees the other's wording."""
    import ouroboros.llm as llm_module
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor import events as ev_module

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="theirs", type="task", text="write the quarterly report", scope=_other_owner_scope().to_dict()),
        status="running",
    )

    def _no_model(*_args, **_kwargs):
        raise AssertionError("another owner's task must not reach the dedup prompt at all")

    monkeypatch.setattr(llm_module, "LLMClient", _no_model)

    assert ev_module._find_duplicate_task("write the quarterly report", "", tmp_path, scope=TEST_SCOPE) is None


@pytest.mark.parametrize("status", ["running", "waiting_confirmation"])
def test_the_duplicate_check_still_finds_the_asking_owners_own_duplicate(tmp_path, monkeypatch, status):
    """Narrowing it must not turn the feature off for the owner it belongs to."""
    import ouroboros.llm as llm_module
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor import events as ev_module

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="mine", type="task", text="write the quarterly report", scope=TEST_SCOPE.to_dict()),
        status=status,
    )
    seen: list[str] = []

    class _FakeClient:
        def chat(self, *, messages, **_kwargs):
            seen.append(messages[-1]["content"])
            return {"content": "mine"}, {}

    monkeypatch.setattr(llm_module, "LLMClient", _FakeClient)

    assert ev_module._find_duplicate_task("write the quarterly report", "", tmp_path, scope=TEST_SCOPE) == "mine"
    assert seen and "quarterly report" in seen[0]
