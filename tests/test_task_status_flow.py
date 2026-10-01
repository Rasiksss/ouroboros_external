import base64
import time
from types import SimpleNamespace

import pytest


def test_task_timeout_uses_task_route_without_coercing_chat_id(monkeypatch):
    from supervisor.queue import TaskQueue

    sent = []
    monkeypatch.setattr("supervisor.queue.send_outbound_message", lambda *args, **kwargs: sent.append((args, kwargs)))
    task_input = {"transport": {"target_uid": "user-42"}}
    record = SimpleNamespace(
        task_type="task",
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
        state_repo=SimpleNamespace(load=lambda: SimpleNamespace(owner_chat_id="wrong-peer")),
        soft_timeout_sec=1,
        hard_timeout_sec=100,
        store=SimpleNamespace(get_task=lambda _task_id: record, upsert_task=lambda _record: None),
    )
    queue._notify_owner = TaskQueue._notify_owner.__get__(queue)

    TaskQueue.enforce_task_timeouts(queue, SimpleNamespace())

    assert sent[0][0][0] == "peer-42"
    assert sent[0][1] == {"source": "dialogs", "transport": {"target_uid": "user-42"}}


def test_agent_restart_notice_preserves_string_chat_id():
    from supervisor.commands import handle_agent_restart_request

    sent = []
    ctx = SimpleNamespace(
        state_repo=SimpleNamespace(load=lambda: SimpleNamespace(owner_chat_id="peer-42", owner_chat_source="dialogs")),
        send_with_budget=lambda *args, **kwargs: sent.append((args, kwargs)),
        safe_restart=lambda **_kwargs: (False, "busy"),
    )

    handle_agent_restart_request({"reason": "test"}, ctx)

    assert [call[0][0] for call in sent] == ["peer-42", "peer-42"]
    assert all(call[1]["source"] == "dialogs" for call in sent)


def _orchestrator(tmp_path, agent=None):
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import TaskQueue
    from supervisor.state import RuntimeStateRepository

    store = init_runtime_store(tmp_path)
    state_repo = RuntimeStateRepository(tmp_path, store=store)
    return (
        store,
        state_repo,
        TaskOrchestrator(
            store=store,
            task_queue=TaskQueue(store, tmp_path, state_repo=state_repo),
            state_repo=state_repo,
            agent_getter=lambda: agent,
        ),
    )


def _task_ctx(tmp_path, **overrides):
    data = {
        "task_depth": 0,
        "pending_events": [],
        "drive_root": tmp_path,
        "is_direct_chat": False,
        "task_id": "parent1",
        "root_task_id": "root1",
        "current_chat_id": 42,
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
        is_direct_chat=False,
    )

    result = _schedule_task(ctx, "Do the thing", context="Model focus A")

    from ouroboros.storage.runtime_store import init_runtime_store
    from ouroboros.storage.task_results import load_task_result

    init_runtime_store(tmp_path)
    assert "Task request queued" in result
    task_id = ctx.pending_events[0]["task_id"]
    data = load_task_result(tmp_path, task_id)
    assert data is not None
    assert data["status"] == STATUS_REQUESTED
    assert data["description"] == "Do the thing"
    assert data["context"] == "Model focus A"


def test_inbound_envelope_and_task_request_preserve_trace_id(tmp_path):
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import TaskQueue
    from supervisor.state import RuntimeStateRepository

    trace_id = "463ac35c9f6413ad48485a3953bb6124"
    store = init_runtime_store(tmp_path)
    state_repo = RuntimeStateRepository(tmp_path, store=store)
    orchestrator = TaskOrchestrator(
        store=store,
        task_queue=TaskQueue(store, tmp_path, state_repo=state_repo),
        state_repo=state_repo,
        agent_getter=lambda: None,
    )
    envelope = InboundEnvelope.from_bridge_message(
        {
            "chat": {"id": 0},
            "from": {"id": 0},
            "text": "hello",
            "transport": {"headers": {"X-Trace-Id": trace_id}},
        }
    )

    result = orchestrator.submit(envelope)
    task = store.get_task(result.task_id)

    assert envelope.trace_id == trace_id
    assert envelope.chat_id == envelope.user_id == 0
    assert task is not None
    assert task.to_agent_input()["chat_id"] == 0
    assert task.to_agent_input()["trace_id"] == trace_id


@pytest.mark.parametrize(
    "payload",
    [
        {
            "attachment_ids": ["attachment-1"],
            "attachments": [{"id": "attachment-1", "filename": "report.txt"}],
        },
        {
            "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\npayload").decode("ascii"),
            "image_mime": "image/png",
        },
    ],
    ids=["attachments", "legacy-image"],
)
def test_busy_agent_queues_noninjectable_payload(tmp_path, payload):
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.intent import SubmissionMode

    injected = []
    agent = SimpleNamespace(
        _busy=True,
        _current_chat_id=42,
        inject_message=lambda *args: injected.append(args),
    )
    _store, _state_repo, orchestrator = _orchestrator(tmp_path, agent)

    result = orchestrator.submit(
        InboundEnvelope(
            chat_id=42,
            text="check payload",
            source="browser",
            **payload,
        )
    )

    assert result.mode == SubmissionMode.QUEUE
    assert injected == []


@pytest.mark.parametrize("source", ["dialogs", "sberchat"], ids=["dialogs", "legacy-sberchat"])
def test_bridge_ingress_keys_the_task_on_the_chat_conversation(tmp_path, source):
    from supervisor.orchestrator.envelope import InboundEnvelope

    store, _state_repo, orchestrator = _orchestrator(tmp_path)

    result = orchestrator.submit(
        InboundEnvelope.from_bridge_message({"chat": {"id": 42}, "text": "сделай", "source": source})
    )
    task = store.get_task(result.task_id)

    assert task is not None and task.conversation_id == "dialogs:42"


def test_busy_agent_queues_another_sberchat_participant_instead_of_injecting(tmp_path):
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.intent import SubmissionMode

    injected = []
    agent = SimpleNamespace(
        _busy=True,
        _current_chat_id=42,
        _current_task_source="sberchat",
        _current_transport={"target_uid": "alice"},
        inject_message=lambda *args: injected.append(args),
    )
    _store, _state_repo, orchestrator = _orchestrator(tmp_path, agent)

    result = orchestrator.submit(
        InboundEnvelope(
            chat_id=42,
            user_id="bob",
            text="а мне другое",
            source="sberchat",
            transport={"target_uid": "bob"},
        )
    )

    assert result.mode == SubmissionMode.QUEUE
    assert injected == []


def test_orchestrator_normalizes_legacy_image_to_persisted_attachment(tmp_path):
    from ouroboros.agent.context import build_user_content
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages
    from supervisor.orchestrator.envelope import InboundEnvelope

    store, _state_repo, orchestrator = _orchestrator(tmp_path)
    raw = b"\x89PNG\r\n\x1a\npayload"

    result = orchestrator.submit(
        InboundEnvelope(
            chat_id=42,
            user_id=7,
            text="check image",
            source="browser",
            client_message_id="legacy-image-1",
            image_base64=base64.b64encode(raw).decode("ascii"),
            image_mime="image/png",
        )
    )
    task = store.get_task(result.task_id)

    assert task is not None
    agent_input = task.to_agent_input()
    assert agent_input["image_base64"] == ""
    assert len(agent_input["attachments"]) == 1
    attachment = agent_input["attachments"][0]
    assert attachment["mime"] == "image/png"
    assert (
        create_storage_provider(data_dir=tmp_path).get_bytes(
            SCOPE_UPLOADS,
            attachment["storage_key"],
        )
        == raw
    )
    assert base64.b64encode(raw).decode("ascii") not in str(build_user_content(agent_input))
    assert attachment["id"] in str(build_user_content(agent_input))
    messages = list_owner_messages(tmp_path)
    assert messages[0].metadata["attachments"][0]["id"] == attachment["id"]


def test_orchestrator_rolls_back_legacy_image_when_enqueue_fails(tmp_path, monkeypatch):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages
    from supervisor.orchestrator.envelope import InboundEnvelope

    _store, _state_repo, orchestrator = _orchestrator(tmp_path)
    task_queue = orchestrator.task_queue
    monkeypatch.setattr(task_queue, "enqueue", lambda _request: (_ for _ in ()).throw(OSError("queue down")))

    with pytest.raises(OSError, match="queue down"):
        orchestrator.submit(
            InboundEnvelope(
                chat_id=42,
                user_id=7,
                text="check image",
                source="browser",
                client_message_id="legacy-image-failed",
                image_base64=base64.b64encode(b"\x89PNG\r\n\x1a\npayload").decode("ascii"),
                image_mime="image/png",
            )
        )

    provider = create_storage_provider(data_dir=tmp_path)
    assert list_owner_messages(tmp_path) == []
    assert provider.list_keys(SCOPE_UPLOADS, "chat/") == []
    assert provider.list_keys(SCOPE_UPLOADS, "_meta/attachments/") == []


def test_orchestrator_deduplicates_inbound_task_across_session_change(tmp_path):
    from supervisor.orchestrator.envelope import InboundEnvelope

    store, state_repo, orchestrator = _orchestrator(tmp_path)
    envelope = InboundEnvelope(
        chat_id="peer-42",
        user_id="user-42",
        text="same update",
        source="dialogs",
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
    assert len(store.list_tasks()) == 1


def test_orchestrator_rejects_invalid_legacy_image_without_queueing(tmp_path):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from supervisor.orchestrator.envelope import InboundEnvelope

    store, _state_repo, orchestrator = _orchestrator(tmp_path)

    with pytest.raises(ValueError, match="image"):
        orchestrator.submit(
            InboundEnvelope(
                chat_id=42,
                text="bad image",
                source="browser",
                image_base64=base64.b64encode(b"not-an-image").decode("ascii"),
                image_mime="image/png",
            )
        )

    assert store.list_tasks() == []
    assert create_storage_provider(data_dir=tmp_path).list_keys(SCOPE_UPLOADS, "chat/") == []


@pytest.mark.parametrize(
    ("active_chat", "active_source", "active_transport", "message", "expected_mode"),
    [
        (
            "group-42",
            "dialogs",
            {"target_uid": "alice"},
            {"chat_id": "group-42", "user_id": "bob", "source": "dialogs", "transport": {"target_uid": "bob"}},
            "queue",
        ),
        (
            "42",
            "browser",
            {},
            {"chat_id": "42", "user_id": "alice", "source": "dialogs", "transport": {"target_uid": "alice"}},
            "queue",
        ),
        (
            "group-42",
            "dialogs",
            {},
            {"chat_id": "group-42", "user_id": "bob", "source": "dialogs", "transport": {"target_uid": "bob"}},
            "queue",
        ),
        (
            "group-42",
            "dialogs",
            {"target_uid": "alice"},
            {"chat_id": "group-42", "user_id": "alice", "source": "dialogs", "transport": {"target_uid": "alice"}},
            "inject",
        ),
    ],
    ids=["other-participant", "other-source", "missing-participant", "same-route"],
)
def test_busy_agent_injects_only_same_route(
    tmp_path,
    active_chat,
    active_source,
    active_transport,
    message,
    expected_mode,
):
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.intent import SubmissionMode

    injected = []
    agent = SimpleNamespace(
        _busy=True,
        _current_chat_id=active_chat,
        _current_task_source=active_source,
        _current_transport=active_transport,
        inject_message=lambda *args: injected.append(args),
    )
    _store, _state_repo, orchestrator = _orchestrator(tmp_path, agent)

    result = orchestrator.submit(InboundEnvelope(text="more context", **message))

    assert result.mode == SubmissionMode(expected_mode)
    assert injected == ([("more context", None)] if expected_mode == "inject" else [])


def test_schedule_task_records_lineage_metadata(tmp_path):
    from ouroboros.storage.task_results import STATUS_RUNNING, load_task_result, write_task_result
    from ouroboros.tools.service.orchestration import _schedule_task

    write_task_result(tmp_path, "parent1", STATUS_RUNNING, root_task_id="root1", chat_id=42)
    trace_id = "463ac35c9f6413ad48485a3953bb6124"
    ctx = _task_ctx(
        tmp_path,
        trace_id=trace_id,
        current_chat_id=0,
        current_task_source="dialogs",
        conversation_id="session-42",
        transport={"target_uid": "alice", "conversation_id": "group-42"},
    )

    result = _schedule_task(ctx, "Do the thing", context="Model focus A")

    assert "Task request queued" in result
    event = ctx.pending_events[0]
    task_id = event["task_id"]
    assert event["parent_task_id"] == "parent1"
    assert event["root_task_id"] == "root1"
    assert event["chat_id"] == 0
    assert event["trace_id"] == trace_id
    assert event["source"] == "dialogs"
    assert event["conversation_id"] == "session-42"
    assert event["transport"] == {"target_uid": "alice", "conversation_id": "group-42"}

    data = load_task_result(tmp_path, task_id)
    assert data is not None
    assert data["parent_task_id"] == "parent1"
    assert data["root_task_id"] == "root1"
    assert data["chat_id"] == 0
    assert data["trace_id"] == trace_id


def test_schedule_task_blocks_eleventh_outstanding_child_for_parent(tmp_path):
    from ouroboros.storage.task_results import STATUS_REQUESTED, write_task_result
    from ouroboros.tools.service.orchestration import _schedule_task

    for index in range(10):
        write_task_result(
            tmp_path,
            f"child{index}",
            STATUS_REQUESTED,
            parent_task_id="parent1",
            root_task_id="root1",
            chat_id=42,
        )
    ctx = _task_ctx(tmp_path)

    result = _schedule_task(ctx, "Overflow child")

    assert "SCHEDULE_TASK_BLOCKED" in result
    assert "parent subtask limit" in result
    assert ctx.pending_events == []


def test_schedule_task_blocks_direct_chat_when_only_one_worker(tmp_path, monkeypatch):
    from ouroboros.tools.service import orchestration
    from ouroboros.tools.service.orchestration import _schedule_task

    monkeypatch.setattr(orchestration, "_worker_slots", lambda: 1)
    ctx = _task_ctx(tmp_path, is_direct_chat=True)

    result = _schedule_task(ctx, "Nested task")

    assert "TOOL_SCHEDULE_TASK_BLOCKED_DIRECT_CHAT_SINGLE_WORKER" in result
    assert ctx.pending_events == []


def test_schedule_task_allows_direct_chat_when_multiple_workers(tmp_path, monkeypatch):
    from ouroboros.tools.service import orchestration
    from ouroboros.tools.service.orchestration import _schedule_task

    monkeypatch.setattr(orchestration, "_worker_slots", lambda: 2)
    ctx = _task_ctx(tmp_path, is_direct_chat=True)

    result = _schedule_task(ctx, "Nested task")

    assert "Task request queued" in result
    assert len(ctx.pending_events) == 1


def test_schedule_task_ignores_terminal_children_for_parent_limit(tmp_path):
    from ouroboros.storage.task_results import STATUS_COMPLETED, write_task_result
    from ouroboros.tools.service.orchestration import _schedule_task

    for index in range(10):
        write_task_result(
            tmp_path,
            f"child{index}",
            STATUS_COMPLETED,
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
        result=full_text,
        trace_summary="trace",
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
    output = _get_task_result(ctx, "abc123")

    assert "TAIL_MARKER" in output
    assert full_text in output
    assert "[BEGIN_SUBTASK_OUTPUT]" in output


def test_wait_for_task_reports_rejected_duplicate(tmp_path):
    from ouroboros.storage.task_results import STATUS_REJECTED_DUPLICATE, write_task_result
    from ouroboros.tools.service.orchestration import _wait_for_task

    write_task_result(
        tmp_path,
        "dup123",
        STATUS_REJECTED_DUPLICATE,
        duplicate_of="orig999",
        result="Task was rejected as semantically similar to already active task orig999.",
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
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

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "dup222",
            "description": "Do the thing",
            "context": "Model focus B",
            "depth": 1,
            "parent_task_id": "parent1",
            "root_task_id": "root1",
            "chat_id": 42,
        },
        FakeCtx(),
    )

    from ouroboros.storage.task_results import load_task_result

    data = load_task_result(tmp_path, "dup222")
    assert data is not None
    assert data["status"] == STATUS_REJECTED_DUPLICATE
    assert data["duplicate_of"] == "orig111"
    assert data["parent_task_id"] == "parent1"
    assert data["root_task_id"] == "root1"
    assert data["chat_id"] == 42
    assert sent and "Task rejected" in sent[0][1]


@pytest.mark.parametrize(
    ("source", "conversation_id"),
    [("browser", "browser:1"), ("dialogs", "dialogs:1")],
)
def test_handle_schedule_task_preserves_route_in_runtime_record(
    tmp_path,
    monkeypatch,
    source,
    conversation_id,
):
    from ouroboros.storage.runtime_store import init_runtime_store
    from ouroboros.storage.task_results import STATUS_REQUESTED, STATUS_RUNNING, STATUS_SCHEDULED
    from ouroboros.tools.service.orchestration import _schedule_task
    from supervisor import events as ev_module
    from supervisor.queue import TaskQueue

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)

    transport = {"target_uid": "alice", "conversation_id": conversation_id}
    tool_ctx = _task_ctx(
        tmp_path,
        current_chat_id=1,
        current_task_source=source,
        conversation_id=conversation_id,
        transport=transport,
    )
    _schedule_task(tool_ctx, "Do the thing", context="Model focus C")

    event = tool_ctx.pending_events[0]
    task_id = event["task_id"]
    store = init_runtime_store(tmp_path)
    requested = store.get_task(task_id)
    assert requested is not None
    assert requested.status == STATUS_REQUESTED
    assert requested.source == source
    assert requested.conversation_id == conversation_id
    requested_created_at = requested.created_at

    queue = TaskQueue(store, tmp_path)
    claims = []
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path

        def load_state(self):
            from ouroboros.storage.runtime_models import OwnerRuntimeState

            return OwnerRuntimeState(owner_chat_id=None)

        def enqueue_task(self, task):
            scheduled = queue.enqueue_dict(task)
            claims.append((scheduled.status, queue.claim_next(worker_id=7)))
            return claims[-1][1]

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

    ev_module._handle_schedule_task(event, FakeCtx())

    running = store.get_task(task_id)
    assert claims[0][0] == STATUS_SCHEDULED
    assert claims[0][1] is not None
    assert running is not None
    assert running.status == STATUS_RUNNING
    assert running.worker_id == 7
    assert running.source == source
    assert running.conversation_id == conversation_id
    assert running.input["source"] == source
    assert running.input["conversation_id"] == conversation_id
    assert running.input["transport"] == transport
    assert running.created_at == requested_created_at
    assert running.metadata["parent_task_id"] == "parent1"
    assert running.metadata["root_task_id"] == "root1"
    assert queue.claim_next(worker_id=8) is None
    assert sent and "Scheduled task" in sent[0][1]
    assert sent[0][0] == 1
    assert sent[0][2]["source"] == source
    assert sent[0][2]["transport"] == transport


def test_handle_schedule_task_marks_missing_route_failed(tmp_path, monkeypatch):
    from ouroboros.storage.runtime_models import OwnerRuntimeState
    from ouroboros.storage.runtime_store import init_runtime_store
    from ouroboros.storage.task_results import STATUS_FAILED
    from ouroboros.tools.service.orchestration import _schedule_task
    from supervisor import events as ev_module

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    tool_ctx = _task_ctx(
        tmp_path,
        current_chat_id=None,
        current_task_source="browser",
    )
    _schedule_task(tool_ctx, "Do the thing")
    event = tool_ctx.pending_events[0]

    runtime_ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        load_state=lambda: OwnerRuntimeState(owner_chat_id=None),
        enqueue_task=lambda _task: pytest.fail("task without a route must not be queued"),
        send_with_budget=lambda *_args, **_kwargs: None,
    )
    ev_module._handle_schedule_task(event, runtime_ctx)

    record = init_runtime_store(tmp_path).get_task(event["task_id"])
    assert record is not None
    assert record.status == STATUS_FAILED
    assert "route" in record.result.lower()


def test_cancel_task_event_preserves_routing_context(tmp_path):
    from ouroboros.tools.service.orchestration import _cancel_task

    ctx = _task_ctx(
        tmp_path,
        current_chat_id="peer-42",
        current_task_source="dialogs",
        transport={"target_uid": "alice", "source_message_id": "mid-1"},
    )

    assert _cancel_task(ctx, "child-1") == "Cancel requested: child-1"
    assert ctx.pending_events == [
        {
            "type": "cancel_task",
            "task_id": "child-1",
            "chat_id": "peer-42",
            "source": "dialogs",
            "transport": {"target_uid": "alice", "source_message_id": "mid-1"},
            "ts": ctx.pending_events[0]["ts"],
        }
    ]


def test_handle_cancel_task_replies_to_event_route():
    from supervisor.events import _handle_cancel_task

    sent = []
    ctx = SimpleNamespace(
        load_state=lambda: SimpleNamespace(owner_chat_id=7),
        cancel_task_by_id=lambda task_id: task_id == "child-1",
        send_with_budget=lambda chat_id, text, **kwargs: sent.append((chat_id, text, kwargs)),
    )

    _handle_cancel_task(
        {
            "task_id": "child-1",
            "chat_id": "peer-42",
            "source": "dialogs",
            "transport": {"target_uid": "alice"},
        },
        ctx,
    )

    assert sent == [
        (
            "peer-42",
            "✅ cancel child-1 (event)",
            {"source": "dialogs", "transport": {"target_uid": "alice"}},
        )
    ]


def test_store_task_result_preserves_existing_lineage_metadata(tmp_path):
    from ouroboros.agent.task_pipeline import _store_task_result
    from ouroboros.storage.task_results import STATUS_SCHEDULED, load_task_result, write_task_result

    write_task_result(
        tmp_path,
        "child123",
        STATUS_SCHEDULED,
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
    )

    data = load_task_result(tmp_path, "child123")
    assert data is not None
    assert data["parent_task_id"] == "parent1"
    assert data["root_task_id"] == "root1"
    assert data["chat_id"] == 42
    assert data["result"] == "done"


def test_inbound_envelope_reads_trace_id_from_transport_headers():
    from supervisor.orchestrator.envelope import InboundEnvelope

    envelope = InboundEnvelope.from_bridge_message(
        {
            "chat": {"id": 42},
            "from": {"id": 99},
            "text": "hello",
            "transport": {
                "headers": {
                    "X-Trace-Id": "463ac35c9f6413ad48485a3953bb6124",
                }
            },
        }
    )

    assert envelope.trace_id == "463ac35c9f6413ad48485a3953bb6124"


def test_runtime_task_request_preserves_trace_id(tmp_path):
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_store import init_runtime_store

    store = init_runtime_store(tmp_path)
    record = store.create_task(
        TaskRequest(
            id="task-trace",
            type="task",
            chat_id=42,
            text="hello",
            trace_id="463ac35c9f6413ad48485a3953bb6124",
        )
    )

    assert record.to_agent_input()["trace_id"] == "463ac35c9f6413ad48485a3953bb6124"


def test_handle_schedule_task_depth_reject_sends_trace_id(tmp_path):
    from ouroboros.storage.runtime_store import init_runtime_store
    from ouroboros.storage.task_results import STATUS_FAILED, STATUS_REQUESTED, write_task_result
    from supervisor import events as ev_module

    trace_id = "463ac35c9f6413ad48485a3953bb6124"
    write_task_result(tmp_path, "too-deep", STATUS_REQUESTED, source="browser")
    sent = []
    queued = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path

        def load_state(self):
            from ouroboros.storage.runtime_models import OwnerRuntimeState

            return OwnerRuntimeState(owner_chat_id=1)

        def enqueue_task(self, task):
            queued.append(task)

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "too-deep",
            "description": "Do the thing",
            "depth": 4,
            "source": "browser",
            "trace_id": trace_id,
        },
        FakeCtx(),
    )

    assert queued == []
    assert sent and "subtask depth limit" in sent[0][1]
    assert sent[0][2]["trace_id"] == trace_id
    record = init_runtime_store(tmp_path).get_task("too-deep")
    assert record is not None
    assert record.status == STATUS_FAILED
    assert record.chat_id == 1
