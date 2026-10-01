from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    ("text", "slash_command"),
    [
        ("/status", "/status"),
        ("/stop", "/stop"),
        ("  /STOP  ", "/stop"),
        ("/stop --force", ""),
    ],
)
def test_orchestrator_classifies_slash_command(
    tmp_path,
    text: str,
    slash_command: str,
) -> None:
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init

    state_init(tmp_path)
    store = init_runtime_store(tmp_path)
    repo = get_state_repository(tmp_path)
    task_queue = queue_init(tmp_path, 10, 20)
    orch = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=repo,
        agent_getter=lambda: None,
    )

    result = orch.submit(InboundEnvelope.from_chat(chat_id=1, text=text))
    expected_intent = TaskIntentKind.SLASH_COMMAND if slash_command else TaskIntentKind.AGENT_CHAT
    assert result.intent == expected_intent
    assert result.mode == (SubmissionMode.NONE if slash_command else SubmissionMode.QUEUE)
    assert result.slash_command == slash_command


def test_orchestrator_does_not_classify_panic_as_slash_command(tmp_path) -> None:
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init

    state_init(tmp_path)
    store = init_runtime_store(tmp_path)
    repo = get_state_repository(tmp_path)
    task_queue = queue_init(tmp_path, 10, 20)
    orch = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=repo,
        agent_getter=lambda: None,
    )
    result = orch.submit(InboundEnvelope.from_chat(chat_id=1, text="/panic"))
    assert result.intent != TaskIntentKind.SLASH_COMMAND
    assert result.mode == SubmissionMode.QUEUE
    pending = task_queue.list_pending_tasks()
    assert len(pending) == 1
    assert pending[0]["text"] == "/panic"


def test_orchestrator_queues_agent_chat(tmp_path) -> None:
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.intent import SubmissionMode
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init

    state_init(tmp_path)
    store = init_runtime_store(tmp_path)
    repo = get_state_repository(tmp_path)
    task_queue = queue_init(tmp_path, 10, 20)
    orch = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=repo,
        agent_getter=lambda: None,
    )
    result = orch.submit(InboundEnvelope.from_chat(chat_id=123, text="hello", source="dialogs"))
    assert result.mode == SubmissionMode.QUEUE
    assert result.task_id
    pending = task_queue.list_pending_tasks()
    assert len(pending) == 1
    assert pending[0]["text"] == "hello"


def test_orchestrator_treats_enqueue_exception_as_committed_when_task_exists(
    tmp_path,
    monkeypatch,
) -> None:
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init

    state_init(tmp_path)
    store = init_runtime_store(tmp_path)
    repo = get_state_repository(tmp_path)
    task_queue = queue_init(tmp_path, 10, 20)
    original_enqueue = task_queue.enqueue

    def ambiguous_enqueue(request):
        original_enqueue(request)
        raise OSError("queue acknowledgement lost")

    monkeypatch.setattr(task_queue, "enqueue", ambiguous_enqueue)
    orchestrator = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=repo,
        agent_getter=lambda: None,
    )

    result = orchestrator.submit(
        InboundEnvelope.from_chat(
            chat_id=1,
            user_id=1,
            text="hello",
            source="browser",
            client_message_id="stable-1",
        )
    )

    assert result.task_id
    assert store.get_task(result.task_id) is not None
    assert len(store.list_tasks()) == 1


def test_orchestrator_reports_unknown_commit_when_verification_fails(
    tmp_path,
    monkeypatch,
) -> None:
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init

    state_init(tmp_path)
    store = init_runtime_store(tmp_path)
    task_queue = queue_init(tmp_path, 10, 20)
    orchestrator = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=get_state_repository(tmp_path),
        agent_getter=lambda: None,
    )
    original_get_task = store.get_task
    calls = 0

    def get_task(task_id):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("task store unavailable")
        return original_get_task(task_id)

    monkeypatch.setattr(store, "get_task", get_task)
    monkeypatch.setattr(
        task_queue,
        "enqueue",
        lambda _request: (_ for _ in ()).throw(OSError("queue acknowledgement lost")),
    )

    with pytest.raises(RuntimeError, match="submission commit status is unknown"):
        orchestrator.submit(
            InboundEnvelope.from_chat(
                chat_id=1,
                text="hello",
                source="browser",
                client_message_id="stable-1",
            )
        )


def test_orchestrator_injects_when_busy_same_chat(tmp_path) -> None:
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.intent import SubmissionMode
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init

    state_init(tmp_path)
    store = init_runtime_store(tmp_path)
    repo = get_state_repository(tmp_path)
    task_queue = queue_init(tmp_path, 10, 20)
    calls: list[str] = []
    agent = SimpleNamespace(
        _busy=True,
        _current_chat_id=42,
        _current_task_source="browser",
        inject_message=lambda text, image_data=None: calls.append(text),
    )
    orch = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=repo,
        agent_getter=lambda: agent,
    )
    result = orch.submit(InboundEnvelope.from_chat(chat_id=42, text="follow up", source="browser"))
    assert result.mode == SubmissionMode.INJECT
    assert calls == ["follow up"]
    assert task_queue.list_pending_tasks() == []


def test_orchestrator_queues_when_task_constraint(tmp_path) -> None:
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.intent import SubmissionMode
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init

    state_init(tmp_path)
    store = init_runtime_store(tmp_path)
    repo = get_state_repository(tmp_path)
    task_queue = queue_init(tmp_path, 10, 20)
    agent = SimpleNamespace(
        _busy=True,
        _current_chat_id=42,
        inject_message=lambda *a, **k: None,
    )
    orch = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=repo,
        agent_getter=lambda: agent,
    )
    constraint = {"mode": "skill_repair", "skill_name": "alpha"}
    result = orch.submit(InboundEnvelope.from_chat(chat_id=42, text="repair", task_constraint=constraint))
    assert result.mode == SubmissionMode.QUEUE
    pending = task_queue.list_pending_tasks()
    assert pending[0]["task_constraint"] == constraint


def test_orchestrator_queues_durable_ingress_idempotently(tmp_path) -> None:
    from ouroboros.agent.owner_inject import drain_owner_messages
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.orchestrator.envelope import InboundEnvelope
    from supervisor.orchestrator.intent import SubmissionMode
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init

    state_init(tmp_path)
    store = init_runtime_store(tmp_path)
    task_queue = queue_init(tmp_path, 10, 20)
    injected = []
    orchestrator = TaskOrchestrator(
        store=store,
        task_queue=task_queue,
        state_repo=get_state_repository(tmp_path),
        agent_getter=lambda: SimpleNamespace(
            _busy=True,
            _current_chat_id=42,
            _current_task_id="active-1",
            _current_task_source="browser",
            inject_message=lambda text, image=None: injected.append(text),
        ),
    )

    envelope = InboundEnvelope.from_chat(chat_id=42, text="follow up", source="browser", client_message_id="stable-1")
    first = orchestrator.submit(envelope)
    second = orchestrator.submit(envelope)

    assert first.mode == second.mode == SubmissionMode.QUEUE
    assert first.task_id == second.task_id
    assert injected == []
    assert drain_owner_messages(tmp_path, "active-1", seen_ids=set()) == []
    assert [task["id"] for task in task_queue.list_pending_tasks()] == [first.task_id]
