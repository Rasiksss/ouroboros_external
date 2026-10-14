from __future__ import annotations

from dataclasses import replace

import pytest

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.owner_scope import configured_agent_id, configured_owner_id
from ouroboros.storage import task_transitions
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader
from tests.helpers.usage import sql_usage_repository


def _scope(source: str, chat_id: int | str) -> OwnerScope:
    """The scope a transport would build for a message arriving on ``source``.

    Spelled out here rather than imported from ``tests.helpers``: this root runs on its own,
    without ``tests/`` on the path.
    """
    return OwnerScope(
        owner_id=configured_owner_id(),
        agent_id=configured_agent_id(),
        surface_id=SurfaceId(source),
        chat_id=str(chat_id),
    )


def _durable_orchestrator(tmp_path, monkeypatch):
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.settings.schema import AppSettings
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from transport.interfaces import InboundEnvelope

    monkeypatch.setattr(
        "supervisor.orchestrator.orchestrator.load_settings",
        lambda: AppSettings(
            OUROBOROS_DURABLE_JOBS_ENABLED=True,
            OUROBOROS_MAX_ACTIVE_ROOT_JOBS_PER_OWNER=1,
            OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER=1,
        ),
    )
    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    store = runtime_persistence.initialize()
    scope = _scope("browser", 1)
    orchestrator = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository()),
        state_repo=get_state_repository(
            tmp_path,
            owner_state_repository=owners,
            usage_repository=usage,
            audit_event_repository=audit,
        ),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )
    envelope = InboundEnvelope(scope=scope, user_id=1, text='/new "Do The Thing"', client_message_id="new-1")
    from ouroboros.storage.chat_store import append_chat_message

    append_chat_message(
        tmp_path,
        scope=scope,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text=envelope.text,
        source="browser",
        client_message_id=envelope.client_message_id,
        ts="2026-01-01T00:00:01Z",
    )
    return store, scope, orchestrator, envelope


@pytest.mark.parametrize(
    ("text", "slash_command"),
    [
        ("/status", "/status"),
        ("/stop", "/stop"),
        ("  /STOP  ", "/stop"),
        ("/stop --force", "/stop --force"),
    ],
)
def test_orchestrator_classifies_slash_command(
    tmp_path,
    text: str,
    slash_command: str,
) -> None:
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from tests.helpers.supervisor import envelope_from_chat

    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    runtime_persistence.initialize()
    repo = get_state_repository(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    task_queue = queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository())
    orch = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=repo,
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )

    result = orch.submit(envelope_from_chat(chat_id=1, text=text))
    expected_intent = TaskIntentKind.SLASH_COMMAND if slash_command else TaskIntentKind.AGENT_CHAT
    assert result.intent == expected_intent
    assert result.mode == (SubmissionMode.NONE if slash_command else SubmissionMode.QUEUE)
    assert result.slash_command == slash_command


def test_orchestrator_does_not_classify_panic_as_slash_command(tmp_path) -> None:
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from tests.helpers.supervisor import envelope_from_chat

    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    runtime_persistence.initialize()
    repo = get_state_repository(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    task_queue = queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository())
    orch = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=repo,
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )
    result = orch.submit(envelope_from_chat(chat_id=1, text="/panic"))
    assert result.intent != TaskIntentKind.SLASH_COMMAND
    assert result.mode == SubmissionMode.QUEUE
    pending = task_queue.list_pending_records()
    assert len(pending) == 1
    assert pending[0].input["text"] == "/panic"


def test_orchestrator_queues_agent_chat(tmp_path) -> None:
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.intent import SubmissionMode
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from tests.helpers.supervisor import envelope_from_chat

    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    runtime_persistence.initialize()
    repo = get_state_repository(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    task_queue = queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository())
    orch = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=repo,
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )
    result = orch.submit(envelope_from_chat(chat_id=123, text="hello", source="dialogs"))
    assert result.mode == SubmissionMode.QUEUE
    assert result.task_id
    pending = task_queue.list_pending_records()
    assert len(pending) == 1
    assert pending[0].input["text"] == "hello"


@pytest.mark.parametrize("outcome", ["accepted", "refused", "conflicting", "absent", "foreground"])
def test_admission_commit_unknown_resolves_the_exact_persisted_outcome(tmp_path, monkeypatch, outcome) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from supervisor.orchestrator.orchestrator import SubmissionCommitUnknown

    store, scope, orchestrator, envelope = _durable_orchestrator(tmp_path, monkeypatch)
    envelope = replace(envelope, text="work", detached=outcome != "foreground")
    if outcome == "refused":
        store.tasks_repo.create_task(TaskRequest(id="occupied", text="busy", scope=scope.to_dict()))
    original = runtime_persistence.current.create_root_task.execute
    calls = []

    def ambiguous_admission(request, **kwargs):
        calls.append(request.id)
        if outcome != "absent":
            original(request.model_copy(update={"text": "winner"}) if outcome == "conflicting" else request, **kwargs)
        raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(runtime_persistence.current.create_root_task, "execute", ambiguous_admission)
    if outcome == "absent":
        with pytest.raises(SubmissionCommitUnknown):
            orchestrator.submit(envelope)
        monkeypatch.setattr(runtime_persistence.current.create_root_task, "execute", original)
        result = orchestrator.submit(envelope)
    else:
        result = orchestrator.submit(envelope)
    saved = store.tasks_repo.get_task(calls[0])
    monkeypatch.setattr(
        runtime_persistence.current.create_root_task,
        "execute",
        lambda *_a, **_k: pytest.fail("must resolve saved identity"),
    )
    assert result == orchestrator.submit(envelope)
    assert result.task_id == saved.job_id == calls[0] and len(calls) == 1
    assert saved.input["text"] == ("winner" if outcome == "conflicting" else "work")
    if outcome == "refused":
        assert saved.status == "rejected_duplicate"
        assert result.message == "Не удалось запустить фоновую задачу: достигнут лимит активных задач."  # noqa: RUF001
    elif outcome == "conflicting":
        assert result.message == "Не удалось обработать сообщение: конфликт идентификатора."  # noqa: RUF001


def test_a_message_arriving_during_a_run_is_queued_and_runs_after_it(tmp_path) -> None:
    """The queue is the only way in, so order is what the owner sees.

    The run in flight receives nothing: a message added to a run already ending would be
    dropped without a record, while a queued one survives the process that was going to
    handle it.
    """
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.intent import SubmissionMode
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from tests.helpers.supervisor import envelope_from_chat

    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    store = runtime_persistence.initialize()
    repo = get_state_repository(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    task_queue = queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository())
    orch = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=repo,
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )

    first = orch.submit(envelope_from_chat(chat_id=42, text="first", source="browser"))
    running = task_queue.claim_next(worker_id=0, runtime_id="test-runtime")
    assert running is not None and running.task_id == first.task_id

    second = orch.submit(envelope_from_chat(chat_id=42, text="follow up", source="browser"))

    assert second.mode == SubmissionMode.QUEUE
    assert [task.task_id for task in task_queue.list_pending_records()] == [second.task_id]
    # Nothing was added to the run in flight: its stored input is the message it started on.
    assert store.tasks_repo.get_task(first.task_id).to_agent_input()["text"] == "first"

    task_queue.tasks_repo.complete_task(first.task_id, result="done")
    assert task_queue.claim_next(worker_id=0, runtime_id="test-runtime").task_id == second.task_id


def test_orchestrator_queues_durable_ingress_idempotently(tmp_path) -> None:
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from ouroboros.agent.owner_inject import drain_owner_messages
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.intent import SubmissionMode
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from tests.helpers.supervisor import envelope_from_chat

    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    runtime_persistence.initialize()
    task_queue = queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository())
    orchestrator = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=get_state_repository(
            tmp_path,
            owner_state_repository=owners,
            usage_repository=usage,
            audit_event_repository=audit,
        ),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )

    envelope = envelope_from_chat(chat_id=42, text="follow up", source="browser", client_message_id="stable-1")
    first = orchestrator.submit(envelope)
    second = orchestrator.submit(envelope)

    assert first.mode == second.mode == SubmissionMode.QUEUE
    assert first.task_id == second.task_id
    assert drain_owner_messages(envelope.scope, tmp_path, "active-1", seen_ids=set()) == []
    assert [task.task_id for task in task_queue.list_pending_records()] == [first.task_id]


def test_a_queued_task_exposes_scope_and_no_conversation_keys(tmp_path) -> None:
    """Identity reaches the agent as one value; the storage keys stay on the supervisor side."""
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from tests.helpers.supervisor import envelope_from_chat

    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    store = runtime_persistence.initialize()
    task_queue = queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository())
    orchestrator = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=get_state_repository(
            tmp_path,
            owner_state_repository=owners,
            usage_repository=usage,
            audit_event_repository=audit,
        ),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )

    envelope = envelope_from_chat(chat_id=42, text="do it", source="dialogs")
    result = orchestrator.submit(envelope)

    record = store.tasks_repo.get_task(result.task_id)
    assert record is not None
    assert record.input["scope"] == envelope.scope.to_dict()
    assert "conversation_id" not in record.input
    assert "session_id" not in record.input
    # The row is keyed by the scope's ownership axes, not by a key glued at the boundary.
    assert (record.owner_id, record.agent_id) == (envelope.scope.owner_id, envelope.scope.agent_id)
    assert not hasattr(record, "conversation_id")


def test_two_owners_on_the_same_chat_do_not_share_a_task(tmp_path) -> None:
    """The chat alone is not identity: the owner axis has to reach the queued task."""
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from new_core.identity import OwnerScope
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from transport.interfaces import InboundEnvelope

    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    store = runtime_persistence.initialize()
    task_queue = queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository())
    orchestrator = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=get_state_repository(
            tmp_path,
            owner_state_repository=owners,
            usage_repository=usage,
            audit_event_repository=audit,
        ),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )

    ids = []
    for owner in ("owner-a", "owner-b"):
        scope = OwnerScope(owner_id=owner, agent_id="main", surface_id="dialogs", chat_id="42")
        result = orchestrator.submit(InboundEnvelope(scope=scope, user_id=42, text="same words"))
        ids.append(result.task_id)
        assert store.tasks_repo.get_task(result.task_id).input["scope"]["owner_id"] == owner

    assert ids[0] != ids[1]


@pytest.mark.parametrize(
    ("axis", "other"),
    [("owner_id", "owner-b"), ("agent_id", "second"), ("surface_id", "browser"), ("chat_id", "43")],
)
def test_the_idempotency_key_separates_every_axis(tmp_path, axis: str, other: str) -> None:
    """A redelivery is the same message only within one identity.

    The key used to be built from the chat and the surface alone, so two owners holding the
    same client message id collapsed onto one task and the second was dropped as a duplicate.
    """
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from new_core.identity import OwnerScope
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from transport.interfaces import InboundEnvelope

    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    runtime_persistence.initialize()
    task_queue = queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository())
    orchestrator = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=get_state_repository(
            tmp_path,
            owner_state_repository=owners,
            usage_repository=usage,
            audit_event_repository=audit,
        ),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )
    axes = {"owner_id": "owner-a", "agent_id": "main", "surface_id": "dialogs", "chat_id": "42"}

    def submit(**overrides):
        scope = OwnerScope(**{**axes, **overrides})
        envelope = InboundEnvelope(scope=scope, user_id=42, text="same words", client_message_id="c-1")
        return orchestrator.submit(envelope).task_id

    first = submit()
    # The very same identity redelivering the same message id is one task, as before.
    assert submit() == first
    # A different identity on any axis is a different message, not a duplicate to drop.
    assert submit(**{axis: other}) != first


def test_two_scopes_are_queued_separately(tmp_path) -> None:
    """Each scope is its own conversation, and each gets its own record."""
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    audit = sql_audit_event_repository()
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.orchestrator.intent import SubmissionMode
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import init as queue_init
    from supervisor.state import get_state_repository, init as state_init
    from tests.helpers.supervisor import envelope_from_chat

    state_init(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=usage,
        audit_event_repository=audit,
    )
    store = runtime_persistence.initialize()
    task_queue = queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository())
    orchestrator = TaskOrchestrator(
        conversations_repo=runtime_persistence.current.conversations_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        create_root_task=runtime_persistence.current.create_root_task,
        task_queue=task_queue,
        state_repo=get_state_repository(
            tmp_path,
            owner_state_repository=owners,
            usage_repository=usage,
            audit_event_repository=audit,
        ),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )

    same = orchestrator.submit(envelope_from_chat(chat_id=42, text="one chat", source="dialogs"))
    other = orchestrator.submit(envelope_from_chat(chat_id=43, text="another chat", source="dialogs"))

    assert (same.mode, other.mode) == (SubmissionMode.QUEUE, SubmissionMode.QUEUE)
    assert same.task_id != other.task_id
    assert {store.tasks_repo.get_task(same.task_id).chat_id, store.tasks_repo.get_task(other.task_id).chat_id} == {
        "42",
        "43",
    }


def test_new_admits_a_detached_root_with_an_immutable_context_snapshot(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    store, scope, orchestrator, envelope = _durable_orchestrator(tmp_path, monkeypatch)
    get_scratchpad().append_block(scope, "context before admission")

    result = orchestrator.submit_detached(envelope, '"Do The Thing"')
    record = store.tasks_repo.get_task(result.task_id)
    get_scratchpad().append_block(scope, "context after admission")

    assert record.detached is True and record.job_id == record.task_id
    assert record.input["text"] == "Do The Thing"
    assert "context before admission" in record.input["inherited_context_seed"]
    assert "context after admission" not in record.input["inherited_context_seed"]


def test_detached_snapshot_omits_a_summary_newer_than_its_history_anchor(tmp_path, monkeypatch) -> None:
    from ouroboros.agent.context import _build_recent_chat
    from ouroboros.storage.chat_store import append_chat_message
    from ouroboros.storage.runtime_models import ContextSummary

    store, scope, orchestrator, envelope = _durable_orchestrator(tmp_path, monkeypatch)
    first = sql_conversation_repository(store.engine).list_chat_history_messages(scope=scope, oldest_first=True)[0]
    later = append_chat_message(
        tmp_path,
        scope=scope,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="later B",
        source="browser",
        client_message_id="new-2",
        ts="2026-01-01T00:00:02Z",
    )
    assert later is not None
    sql_conversation_repository(store.engine).upsert_context_summary(
        ContextSummary(
            owner_id=scope.owner_id,
            agent_id=scope.agent_id,
            chat_id=scope.chat_id,
            source_offsets_ref=later.message_id,
            summary_ref="summary includes later B",
        )
    )

    result = orchestrator.submit_detached(envelope, "Do The Thing")
    record = store.tasks_repo.get_task(result.task_id)
    recent = _build_recent_chat(tmp_path, scope, record.to_agent_input())

    assert record.input["through_message_id"] == first.message_id
    assert record.input["committed_summary_snapshot"] == ""
    assert recent is not None
    assert envelope.text in recent.render()
    assert "later B" not in recent.render()


def test_new_replays_an_active_identity_before_enforcing_the_owner_quota(tmp_path, monkeypatch) -> None:
    from transport.interfaces import InboundEnvelope

    store, scope, orchestrator, envelope = _durable_orchestrator(tmp_path, monkeypatch)
    record = store.tasks_repo.get_task(orchestrator.submit_detached(envelope, '"Do The Thing"').task_id)

    store.tasks_repo.update_task_status(record.task_id, "running")
    accepted_replay = orchestrator.submit_detached(envelope, '"Do The Thing"')
    from ouroboros.storage.chat_store import append_chat_message

    append_chat_message(
        tmp_path,
        scope=scope,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="other",
        source="browser",
        client_message_id="new-2",
    )
    refused = orchestrator.submit_detached(
        InboundEnvelope(scope=scope, user_id=1, text="other", client_message_id="new-2"), "Other"
    )

    assert accepted_replay.message == "Фоновая задача принята. Результат появится здесь после завершения."
    assert refused.message == "Не удалось запустить фоновую задачу: достигнут лимит активных задач."  # noqa: RUF001


def test_enabled_new_replay_returns_before_snapshot_context(tmp_path, monkeypatch) -> None:
    store, _scope, orchestrator, envelope = _durable_orchestrator(tmp_path, monkeypatch)
    first = orchestrator.submit_detached(envelope, '"Do The Thing"')
    original = store.tasks_repo.get_task(first.task_id).input.copy()
    monkeypatch.setattr(
        "supervisor.orchestrator.orchestrator._snapshot_context",
        lambda *_args: (_ for _ in ()).throw(AssertionError("replay must not snapshot")),
    )

    replay = orchestrator.submit_detached(envelope, '"Do The Thing"')

    assert replay.task_id == first.task_id and replay.message == first.message
    assert store.tasks_repo.get_task(first.task_id).input == original


def test_new_replay_resolves_the_current_retry_attempt_by_logical_job_id(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.runtime_models import TaskRequest

    store, scope, orchestrator, envelope = _durable_orchestrator(tmp_path, monkeypatch)
    record = store.tasks_repo.get_task(orchestrator.submit_detached(envelope, '"Do The Thing"').task_id)

    claim = store.tasks_repo.claim_next_task(runtime_id="runtime")
    successor = TaskRequest.model_validate({**record.input, "id": "new-1-retry"})
    store.tasks_repo.transition_running(
        record.task_id,
        result="release",
        kind="instance_released",
        attempt=1,
        successor=successor,
        runtime_id="runtime",
        claim_token=claim.metadata["execution_claim"],
    )
    claim = store.tasks_repo.claim_next_task(runtime_id="runtime")
    store.tasks_repo.finish_detached_attempt(
        successor.id,
        status="completed",
        result="retry answer",
        claim_token=claim.metadata["execution_claim"],
    )
    replayed = orchestrator.submit_detached(envelope, '"Do The Thing"')

    assert replayed.task_id == record.job_id
    assert store.tasks_repo.get_current_job(replayed.task_id, scope=scope).task_id == "new-1-retry"


def test_disabled_new_checks_stable_identity_before_snapshot_io(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.settings.schema import AppSettings

    store, _scope, orchestrator, envelope = _durable_orchestrator(tmp_path, monkeypatch)
    first = orchestrator.submit_detached(envelope, "work")
    monkeypatch.setattr("supervisor.orchestrator.orchestrator.load_settings", lambda: AppSettings())
    monkeypatch.setattr(orchestrator.state_repo, "load", lambda: pytest.fail("state I/O must not run"))
    monkeypatch.setattr(
        "supervisor.orchestrator.orchestrator._snapshot_context",
        lambda *_args: pytest.fail("snapshot I/O must not run"),
    )
    assert orchestrator.submit_detached(envelope, "work") == first
    refused = orchestrator.submit_detached(replace(envelope, client_message_id="new-while-disabled"), "other")
    assert refused.message == "Фоновые задачи отключены."
    monkeypatch.setattr(
        "supervisor.orchestrator.orchestrator.load_settings", lambda: AppSettings(OUROBOROS_DURABLE_JOBS_ENABLED=True)
    )
    assert orchestrator.submit_detached(replace(envelope, client_message_id="new-while-disabled"), "other") == refused
    assert store.tasks_repo.get_task(refused.task_id).status == "failed"
    assert len(store.tasks_repo.list_tasks()) == 2


def test_disabled_foreground_does_not_snapshot_context(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.settings.schema import AppSettings
    from supervisor.orchestrator.intent import SubmissionMode

    store, _, orchestrator, envelope = _durable_orchestrator(tmp_path, monkeypatch)
    monkeypatch.setattr("supervisor.orchestrator.orchestrator.load_settings", AppSettings)
    monkeypatch.setattr(
        runtime_persistence.current.create_root_task,
        "_capture_context",
        lambda *_args: pytest.fail("flag-off must not capture"),
    )
    result = orchestrator.submit(replace(envelope, text="ordinary", client_message_id="foreground"))
    assert result.mode is SubmissionMode.QUEUE
    assert store.tasks_repo.get_task(result.task_id).input["text"] == "ordinary"


def test_enabled_foreground_keeps_the_summary_snapshot_for_a_later_detached_child(tmp_path, monkeypatch) -> None:
    from ouroboros.storage.chat_store import append_chat_message
    from ouroboros.storage.runtime_models import ContextSummary

    store, scope, orchestrator, envelope = _durable_orchestrator(tmp_path, monkeypatch)
    foreground = replace(envelope, text="foreground", client_message_id="foreground")
    anchor = append_chat_message(
        tmp_path,
        scope=scope,
        actor_type="owner",
        chat_id=foreground.chat_id,
        user_id=foreground.user_id,
        text=foreground.text,
        source="browser",
        client_message_id=foreground.client_message_id,
    )
    assert anchor is not None
    sql_conversation_repository(store.engine).upsert_context_summary(
        ContextSummary(
            owner_id=scope.owner_id,
            agent_id=scope.agent_id,
            chat_id=scope.chat_id,
            source_offsets_ref=anchor.message_id,
            summary_ref="snapshot for child",
        )
    )

    result = orchestrator.submit(foreground)

    record = store.tasks_repo.get_task(result.task_id)
    assert record.detached is False
    assert record.input["committed_summary_snapshot"] == "snapshot for child"


def test_admission_captures_context_in_its_transaction_and_refuses_missing_anchor(tmp_path, monkeypatch):
    from sqlalchemy import text

    from ouroboros.storage.runtime_backend import IMMUTABLE_CONTEXT_SEED_MAX_CHARS, MessageAnchorMissingError
    from ouroboros.storage.runtime_models import ConversationMessage, TaskRequest

    store, scope, _, _ = _durable_orchestrator(tmp_path, monkeypatch)
    request = TaskRequest(id="atomic", detached=True, text="work", scope=scope.to_dict())
    with pytest.raises(MessageAnchorMissingError):
        runtime_persistence.current.create_root_task.execute(request, enabled=True, max_active=5)
    assert store.tasks_repo.get_task("atomic") is None
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(message_id="anchor", dedup_key="anchor", content="work", **scope.to_dict()), scope=scope
    )
    request.through_message_id = "anchor"

    def prepare(conn, owner_id):
        conn.execute(
            text(
                "INSERT INTO scratchpad_blocks (owner_id, agent_id, surface_id, chat_id, job_id, ts, source, content, status) VALUES (:owner, :agent, :surface_id, :chat_id, '', '', '', :body, 'active')"
            ),
            {
                "owner": owner_id,
                "agent": scope.agent_id,
                "surface_id": str(scope.surface_id),
                "chat_id": scope.chat_id,
                "body": "x" * 40000,
            },
        )

    monkeypatch.setattr(task_transitions, "serialize_owner", prepare)
    with monkeypatch.context() as patch:
        patch.setattr(
            store.tasks_repo, "_insert_task", lambda *_args: (_ for _ in ()).throw(RuntimeError("insert failed"))
        )
        with pytest.raises(RuntimeError, match="insert failed"):
            runtime_persistence.current.create_root_task.execute(request, enabled=True, max_active=5)
    assert store.tasks_repo.get_task("atomic") is None
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM scratchpad_blocks")).scalar_one() == 0
    job = runtime_persistence.current.create_root_task.execute(request, enabled=True, max_active=5)
    assert len(job.input["inherited_context_seed"]) == IMMUTABLE_CONTEXT_SEED_MAX_CHARS
    assert job.input["inherited_context_seed"].startswith("x" * 100)
