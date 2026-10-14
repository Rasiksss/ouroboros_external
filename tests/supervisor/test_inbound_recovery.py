"""Durable inbound outcomes and recovery through the supervisor's existing publishers."""

import pytest

from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.schema import AppSettings
from supervisor import message_bus
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.fake_gateway import RecordingOutbox
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor import make_supervisor_loop as _loop
from transport.browser.infra.repositories.owner_history.repo import SqlOwnerHistoryRepository
from transport.interfaces import InboundEnvelope


def test_slow_reply_does_not_starve_its_page_and_recovers_after_restart(supervisor, monkeypatch):
    import time

    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    for identity in ("reply-a", "reply-b", "reply-c"):
        assert message_bus.send_with_budget(
            identity, scope=TEST_SCOPE, source="browser", client_message_id=identity, persist_only=True
        )
    store = runtime_persistence.current
    rows = sorted(
        sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, actor_type="agent"),
        key=lambda row: row.message_id,
    )
    claim = sql_conversation_repository(store.engine).claim_message_handoff
    attempts = []
    delivered = []
    publish = supervisor.runtime.gateway.outbox.publish

    def record_publish(message, **kwargs):
        publish(message, **kwargs)
        delivered.append(message)

    monkeypatch.setattr(supervisor.runtime.gateway.outbox, "publish", record_publish)

    def blocked_claim(message_id, **kwargs):
        attempts.append(message_id)
        if message_id == rows[0].message_id:
            raise TimeoutError("waiting for SQL row lock before claim")
        return claim(message_id, **kwargs)

    monkeypatch.setattr(runtime_persistence.current.conversations_repo, "claim_message_handoff", blocked_claim)
    loop = _loop(supervisor)
    loop._recover_message_handoffs(time.monotonic() + 1)
    assert set(attempts) == {row.message_id for row in rows}
    before_restart = sorted(
        sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, actor_type="agent"),
        key=lambda row: row.message_id,
    )
    assert not before_restart[0].metadata.get("handoff_accepted")
    assert all(row.metadata.get("handoff_accepted") for row in before_restart[1:])
    monkeypatch.setattr(runtime_persistence.current.conversations_repo, "claim_message_handoff", claim)
    restarted = _loop(supervisor)
    restarted._recover_message_handoffs(time.monotonic() + 1)
    after = sorted(
        sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, actor_type="agent"),
        key=lambda row: row.message_id,
    )
    assert [(row.message_id, row.content, row.owner_id, row.agent_id) for row in after] == [
        (row.message_id, row.content, row.owner_id, row.agent_id) for row in rows
    ]
    assert all(row.metadata.get("handoff_accepted") for row in after)
    restarted._recover_message_handoffs(time.monotonic() + 1)
    restarted._recover_message_handoffs(time.monotonic() + 1)
    assert sorted((message.message_id, message.text, message.scope) for message in delivered) == [
        (row.message_id, row.content, TEST_SCOPE) for row in rows
    ]


@parametrize_from_dict(
    {
        "publication failure": {"failure": "publication", "source": "browser"},
        "permanent failure": {"failure": "permanent", "source": "browser"},
        "conflicting message": {"failure": "conflict", "source": "browser"},
        "a2a permanent failure": {"failure": "permanent", "source": "a2a"},
    }
)
def test_inbound_failure_does_not_starve_the_next_message(supervisor, monkeypatch, failure, source):
    from dataclasses import replace

    settings = AppSettings(OUROBOROS_DURABLE_JOBS_ENABLED=True)
    monkeypatch.setattr("supervisor.orchestrator.orchestrator.load_settings", lambda: settings)
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    submit = supervisor.ctx.orchestrator.submit
    publish = supervisor.runtime.gateway.outbox.publish
    attempts = []

    def create_root_task(envelope):
        if failure == "permanent" and envelope.client_message_id == "bad":
            attempts.append(envelope.client_message_id)
            raise ValueError("permanently invalid envelope")
        return submit(envelope)

    def publication(message, **kwargs):
        if message.transport.get("source_message_id") == "bad":
            raise OSError("outbox temporarily unavailable for this reply")
        return publish(message, **kwargs)

    monkeypatch.setattr(supervisor.ctx.orchestrator, "submit", create_root_task)
    monkeypatch.setattr(supervisor.runtime.gateway.outbox, "publish", publication)
    loop = _loop(supervisor)
    scope = scope_with(TEST_SCOPE, surface_id=source, chat_id="-9001") if source == "a2a" else TEST_SCOPE
    for identity in ("bad", "good"):
        envelope = InboundEnvelope(
            scope=scope,
            text=identity,
            detached=True,
            client_message_id=identity,
            transport={"source_message_id": identity},
        )
        if failure == "conflict" and identity == "bad":
            message_bus.log_chat(
                "owner", envelope.chat_id, envelope.text, scope=scope, source=source, client_message_id=identity
            )
            submit(envelope)
            envelope = replace(envelope, text="different work")
        loop.inbox.submit(envelope)
    for _ in range(5):
        loop.process_inbox(timeout=0)
    assert any(row.input.get("text") == "good" for row in runtime_persistence.current.tasks_repo.list_tasks())
    assert len(attempts) <= 3
    before = sql_conversation_repository(runtime_persistence.current.engine).list_messages(
        scope=scope, actor_type="agent"
    )
    assert len(before) == 2
    monkeypatch.setattr(supervisor.runtime.gateway.outbox, "publish", publish)
    monkeypatch.setattr("supervisor.runtime._INBOX_POLL_TIMEOUT_SEC", 0)
    restarted = _loop(supervisor)
    for _ in range(2):
        restarted.run_tick()
    after = sql_conversation_repository(runtime_persistence.current.engine).list_messages(
        scope=scope, actor_type="agent"
    )
    assert [(row.message_id, row.content) for row in after] == [(row.message_id, row.content) for row in before]
    assert all(row.metadata.get("handoff_accepted") for row in after)
    assert len(runtime_persistence.current.tasks_repo.list_tasks()) == 2


def test_a2a_retry_follows_the_reclaimed_jobs_current_attempt(supervisor):
    outbox = RecordingOutbox()
    supervisor.runtime.bridge = supervisor.ctx.bridge = message_bus.LocalChatBridge(outbox, supervisor.drive_root)
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    scope = scope_with(TEST_SCOPE, surface_id="a2a", chat_id="-9001")
    request = InboundEnvelope(
        scope=scope,
        text="List employee grades",
        client_message_id="same-message",
        correlation_id="same-reply",
        transport={"correlation_id": "same-reply"},
    )
    loop = _loop(supervisor)
    loop.handle_inbound(request)
    store = runtime_persistence.current
    original = store.tasks_repo.claim_next_task(runtime_id="lost-runtime")
    assert original is not None
    original.heartbeat_at = "2000-01-01T00:00:00+00:00"
    store.tasks_repo.upsert_task(original)
    assert [task.task_id for task in supervisor.ctx.task_queue.reclaim_stale_leases()] == [original.task_id]
    outbox.published.clear()

    loop.handle_inbound(request)
    assert outbox.published == []
    current = store.tasks_repo.claim_next_task(runtime_id="replacement-runtime")
    assert current is not None and current.task_id != original.task_id and current.job_id == original.job_id
    answer = "The replacement attempt's answer"
    store.tasks_repo.update_task_status(current.task_id, "completed", result=answer)
    assert message_bus.send_with_budget(
        answer, scope=scope, source="a2a", task_id=current.task_id, transport=request.transport
    )
    outbox.published.clear()

    loop.handle_inbound(request)
    assert [(message.text, message.correlation_id) for message in outbox.published] == [(answer, "same-reply")]
    assert len(store.tasks_repo.list_tasks()) == 2


@pytest.mark.parametrize("conflict", [False, True])
def test_a2a_result_replay_recovers_without_blocking_other_chats(supervisor, monkeypatch, conflict):
    from dataclasses import replace

    monkeypatch.setattr("supervisor.runtime._INBOX_POLL_TIMEOUT_SEC", 0)
    outbox = RecordingOutbox()
    supervisor.runtime.bridge = supervisor.ctx.bridge = message_bus.LocalChatBridge(outbox, supervisor.drive_root)
    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    scope = scope_with(TEST_SCOPE, surface_id="a2a", chat_id="-9001")
    request = InboundEnvelope(
        scope=scope,
        text="List employee grades",
        client_message_id="same-message",
        correlation_id="same-reply",
        transport={"correlation_id": "same-reply"},
    )
    loop = _loop(supervisor)
    loop.handle_inbound(request)
    store = runtime_persistence.current
    (job,) = store.tasks_repo.list_tasks()
    answer = "Employee grades: [table](https://example.org/grades)"
    store.tasks_repo.update_task_status(job.task_id, "completed", result=answer)
    assert message_bus.send_with_budget(
        answer, scope=scope, source="a2a", task_id=job.task_id, transport=request.transport, fmt="markdown"
    )
    history = store.conversations_repo.list_chat_history_messages(scope=scope)
    requests = (
        [
            replace(
                request,
                text=f"Different request {i}",
                correlation_id=f"conflict-{i}",
                transport={"correlation_id": f"conflict-{i}"},
            )
            for i in range(2)
        ]
        if conflict
        else [request]
    )
    expected_text = "Не удалось обработать сообщение: конфликт идентификатора." if conflict else answer

    # A later retry must also re-arm an already accepted delivery receipt.
    for retry in range(2):
        outbox.failure = OSError("broker unavailable")
        outbox.attempts.clear()
        outbox.published.clear()
        for current in requests:
            loop.inbox.submit(current)
        loop.inbox.submit(InboundEnvelope(scope=TEST_SCOPE, text=f"browser-{retry}", client_message_id=f"next-{retry}"))
        for _ in range(5):
            loop.process_inbox(timeout=0)
        assert any(row.input.get("text") == f"browser-{retry}" for row in store.tasks_repo.list_tasks())
        assert len(outbox.attempts) == len(requests)
        assert all(0 < remaining <= 5 for _, remaining in outbox.attempts)
        assert outbox.published == []
        after_inbox = store.conversations_repo.list_chat_history_messages(scope=scope)
        if conflict:
            assert len(after_inbox) == len(history) + len(requests)
        else:
            assert after_inbox == history

        outbox.failure = None
        loop = _loop(supervisor)
        for _ in range(2):
            loop.run_tick()
        assert sorted((msg.text, msg.correlation_id, msg.markdown) for msg in outbox.published) == [
            (expected_text, current.correlation_id, not conflict) for current in requests
        ]
        after = store.conversations_repo.list_chat_history_messages(scope=scope)
        assert [(row.message_id, row.content, row.created_at) for row in after] == [
            (row.message_id, row.content, row.created_at) for row in after_inbox
        ]
        assert (
            store.conversations_repo.list_owner_messages(scope=scope, limit=1, include_progress=True, newest_first=True)
            == after[:1]
        )
        assert SqlOwnerHistoryRepository(
            store.engine, history_transactions=file_transaction
        ).list_owner_messages_with_open_confirmation(scope=scope, limit=1) == (after[:1], None)
        assert len([row for row in store.tasks_repo.list_tasks() if row.source == "a2a"]) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_admission_and_failure_resolve_one_outcome_under_race(supervisor, monkeypatch, enabled):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr(
        "supervisor.orchestrator.orchestrator.load_settings",
        lambda: AppSettings(OUROBOROS_DURABLE_JOBS_ENABLED=enabled),
    )
    envelope = InboundEnvelope(scope=TEST_SCOPE, text="work", client_message_id="race")
    message_bus.log_chat(
        "owner",
        envelope.chat_id,
        envelope.text,
        scope=TEST_SCOPE,
        source="browser",
        client_message_id=envelope.client_message_id,
    )
    orchestrator = supervisor.ctx.orchestrator
    barrier = Barrier(2)

    def decide(action):
        barrier.wait(timeout=2)
        return action(envelope)

    with ThreadPoolExecutor(2) as pool:
        create_root_task = pool.submit(decide, orchestrator.submit)
        rejection = pool.submit(decide, orchestrator.resolve_failed_inbound)
        admitted, rejected = create_root_task.result(timeout=3), rejection.result(timeout=3)
    assert admitted == rejected
    assert orchestrator.submit(envelope) == admitted
    assert orchestrator.resolve_failed_inbound(envelope) == admitted
    assert len(runtime_persistence.current.tasks_repo.list_tasks()) == 1


@pytest.mark.parametrize("race", [None, "claim", "accepted"])
def test_legacy_reply_gains_recovery_scope_without_overwriting_a_cas_winner(supervisor, monkeypatch, race):
    import time

    supervisor.ctx.send_with_budget = message_bus.send_with_budget
    store = runtime_persistence.current

    def send(**kwargs):
        return message_bus.send_with_budget(
            "saved answer", scope=TEST_SCOPE, source="browser", client_message_id="legacy-reply", **kwargs
        )

    assert send(persist_only=True)
    original = sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, actor_type="agent")[0]
    legacy = {key: value for key, value in original.metadata.items() if key != "handoff_scope"}
    assert sql_conversation_repository(store.engine).update_message_metadata(original.message_id, legacy)
    update = sql_conversation_repository(store.engine).update_message_metadata
    delivered = []
    publish = supervisor.runtime.gateway.outbox.publish

    def capture(message, **kwargs):
        publish(message, **kwargs)
        delivered.append(message)

    if race:
        winner = {
            **legacy,
            "handoff_claim": "winner",
            "handoff_until": time.time() + 60,
            "handoff_accepted": race == "accepted",
        }

        def concurrent_update(message_id, metadata, *, expected_metadata=None):
            assert update(message_id, winner)
            return update(message_id, metadata, expected_metadata=expected_metadata)

        with monkeypatch.context() as patch:
            patch.setattr(runtime_persistence.current.conversations_repo, "update_message_metadata", concurrent_update)
            assert not send(persist_only=True)
        assert sql_conversation_repository(store.engine).get_message(original.message_id).metadata == winner
        assert not delivered
        return
    with monkeypatch.context() as patch:
        patch.setattr(
            supervisor.runtime.gateway.outbox,
            "publish",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("outbox unavailable")),
        )
        assert not send()
        assert send(persist_only=True)
    monkeypatch.setattr(supervisor.runtime.gateway.outbox, "publish", capture)
    _loop(supervisor)._recover_message_handoffs(time.monotonic() + 1)
    rows = sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, actor_type="agent")
    assert len(rows) == 1
    assert (rows[0].message_id, rows[0].content, rows[0].created_at) == (
        original.message_id,
        original.content,
        original.created_at,
    )
    assert rows[0].metadata["handoff_accepted"]
    assert "handoff_pending" not in rows[0].metadata
    assert [(message.message_id, message.text) for message in delivered] == [(original.message_id, original.content)]
