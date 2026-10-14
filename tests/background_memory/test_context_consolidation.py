"""What the supervisor still owns of consolidation: publishing the raw trigger.

Everything else — thresholds, batching, the model call, the commit — moved to
``background_memory``; its tests live in ``tests/background_memory/``.
"""

from __future__ import annotations

import pytest

from background_memory.interfaces import ChatMessagePersistedEvent
from ouroboros.storage.runtime_models import ConversationMessage
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.memory_event_conformance import poll, reset_memory_event_publisher
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor import bootstrap_memory_runtime_for_tests


@pytest.fixture
def runtime_parts(tmp_path, request):
    from supervisor.runtime_context import reset_runtime_for_tests
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    request.addfinalizer(reset_runtime_for_tests)
    request.addfinalizer(reset_memory_event_publisher)
    return runtime


@pytest.fixture
def published(runtime_parts):
    """The transport the trigger is published onto, ready to be drained."""
    return bootstrap_memory_runtime_for_tests(handlers={}).consumer


def append_message(store, index: int) -> None:
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            chat_id=TEST_SCOPE.chat_id,
            message_id=f"message-{index:04d}",
            dedup_key=f"message-{index:04d}",
            actor_type="owner",
            content=f"msg-{index:04d}|",
            created_at=f"2026-08-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
            metadata={"chat_id": 1},
        ),
        scope=TEST_SCOPE,
    )


@pytest.mark.parametrize("actor_type", ["agent", "owner"])
def test_a_persisted_message_publishes_one_trigger(runtime_parts, published, actor_type):
    """Every persisted row counts toward the backlog, so every one announces itself.

    Restricting the trigger to agent replies is what left a failed run with no way
    back once a session ended on one.
    """
    import supervisor.message_bus as message_bus

    for index in range(3):
        append_message(runtime_persistence.current, index)

    message_bus.log_chat(actor_type, "chat-1", "owner-1", "the reply", source="browser", scope=TEST_SCOPE)

    event = poll(published)
    assert isinstance(event, ChatMessagePersistedEvent)
    assert (event.scope, bool(event.message_id)) == (TEST_SCOPE, True)
    assert poll(published) is None  # exactly one


def test_a_message_that_failed_to_persist_publishes_nothing(runtime_parts, published, monkeypatch):
    import supervisor.message_bus as message_bus

    monkeypatch.setattr(message_bus, "append_chat_message", lambda *args, **kwargs: None)

    message_bus.log_chat("agent", "chat-1", "owner-1", "never stored", source="browser", scope=TEST_SCOPE)

    assert poll(published) is None


def test_persisting_a_message_creates_no_task_row(runtime_parts, published):
    """The trigger reaches no scheduler: thresholds are the handler's business."""
    import supervisor.message_bus as message_bus

    for index in range(120):
        append_message(runtime_persistence.current, index)

    message_bus.log_chat("agent", "chat-1", "owner-1", "the reply", source="browser", scope=TEST_SCOPE)

    assert isinstance(poll(published), ChatMessagePersistedEvent)
    assert (
        runtime_persistence.current.tasks_repo.list_tasks(statuses=["queued", "scheduled", "interrupted", "running"])
        == []
    )
