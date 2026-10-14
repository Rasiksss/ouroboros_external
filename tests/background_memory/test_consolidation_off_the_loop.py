"""A consolidation run on the loop: what it no longer holds, and what did not change with it."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from background_memory.handlers import consolidation as consolidation_mod
from background_memory.handlers.consolidation import ConsolidationHandler
from background_memory.infra.events.inmemory import InMemoryEventConsumer, InMemoryEventPublisher, MemoryEventQueue
from background_memory.interfaces import ChatMessagePersistedEvent, ConsolidationContinuationEvent
from background_memory.runtime import MemoryRuntime
from ouroboros.storage.runtime_models import ConversationMessage
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.background_memory_db import llm_double, memory_database
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor import memory_heartbeat
from tests.helpers.user_settings import stub_user_settings

SCOPE = TEST_SCOPE
_OTHER = scope_with(TEST_SCOPE, owner_id="other-owner")

_SETTINGS = {
    "OUROBOROS_CHAT_SUMMARY_MAX_TOKENS": 32_768,
    "OUROBOROS_MODEL_CONTEXT_TOKENS": 200_000,
    "OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC": 300,
    "OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES": 1,
    "OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS": 25_000,
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(consolidation_mod, "load_settings", lambda: SimpleNamespace(**_SETTINGS))
    yield from memory_database()


def _say(db, index: int, scope=SCOPE) -> None:
    sql_conversation_repository(db.store.engine).append_message(
        ConversationMessage(
            message_id=f"{scope.owner_id}-message-{index:04d}",
            dedup_key=f"{scope.owner_id}-message-{index:04d}",
            owner_id=scope.owner_id,
            agent_id=scope.agent_id,
            chat_id=scope.chat_id,
            actor_type="owner",
            content=f"msg-{index:04d}|",
            created_at=f"2026-08-01T00:00:{index:02d}+00:00",
            metadata={"chat_id": 1},
        ),
        scope=scope,
    )


def _handler(db, llm, *, holder_id: str = "holder-a") -> ConsolidationHandler:
    return ConsolidationHandler(
        llm,
        leases=db.leases,
        summaries=db.summaries,
        history=db.history,
        audit=db.audit,
        user_settings=stub_user_settings(light_model="test-model", fallback_model=""),
        foreground_pressure=db.foreground_pressure(),
        stop_event=threading.Event(),
        holder_id=holder_id,
    )


# --- concurrency ----------------------------------------------------------------


def test_a_run_inside_its_model_call_does_not_delay_a_delivery_for_another_pair(db) -> None:
    """The sequential loop made one owner's summary every other owner's wait."""
    released = asyncio.Event()
    entered = asyncio.Event()
    in_call: list[str] = []
    answered: list[str] = []

    class _Llm:
        async def async_chat(self, **kwargs):
            owner = "first" if not in_call else "second"
            in_call.append(owner)
            entered.set()
            if owner == "first":
                await released.wait()
            answered.append(owner)
            return {"role": "assistant", "content": "a summary", "finish_reason": "stop"}, {}

        def chat(self, **kwargs):
            raise AssertionError("the run must not reach the synchronous path")

    _say(db, 1)
    _say(db, 1, _OTHER)
    handler = _handler(db, _Llm())

    async def _scenario() -> None:
        slow = asyncio.ensure_future(handler.handle(ChatMessagePersistedEvent(scope=SCOPE, message_id="m")))
        await asyncio.wait_for(entered.wait(), timeout=10)
        await asyncio.wait_for(handler.handle(ChatMessagePersistedEvent(scope=_OTHER, message_id="m")), timeout=10)
        released.set()
        await asyncio.wait_for(slow, timeout=10)

    asyncio.run_coroutine_threadsafe(_scenario(), db.loop).result(timeout=30)

    assert answered == ["second", "first"], "the second pair waited on the first"


def test_an_immediate_continuation_precedes_the_pairs_other_chat_so_both_get_summarized(db, monkeypatch) -> None:
    monkeypatch.setattr(consolidation_mod, "_BATCH_MESSAGES", 1)
    first, second = scope_with(SCOPE, chat_id="chat-a"), scope_with(SCOPE, chat_id="chat-b")
    _say(db, 1, first)
    _say(db, 2, first)
    _say(db, 3, second)
    handler = _handler(db, llm_double(lambda **_k: ({"content": "a summary", "finish_reason": "stop"}, {})))
    queue = MemoryEventQueue(4)
    publisher = InMemoryEventPublisher(queue)
    for scope in (first, second):
        publisher.publish(ChatMessagePersistedEvent(scope=scope, message_id="m"))
    stop_event = threading.Event()
    runtime = MemoryRuntime(
        InMemoryEventConsumer(queue),
        handlers={ChatMessagePersistedEvent: handler.handle, ConsolidationContinuationEvent: handler.handle},
        stop_event=stop_event,
        heartbeat=memory_heartbeat(),
        drain_timeout_sec=5.0,
    )
    running = asyncio.run_coroutine_threadsafe(runtime.execute(), db.loop)
    try:
        deadline = time.monotonic() + 15
        while (
            sql_conversation_repository(db.store.engine).get_latest_context_summary(second) is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    finally:
        stop_event.set()
        running.result(timeout=30)

    cursors = {
        scope.chat_id: getattr(
            sql_conversation_repository(db.store.engine).get_latest_context_summary(scope), "source_offsets_ref", None
        )
        for scope in (first, second)
    }
    assert cursors == {"chat-a": f"{SCOPE.owner_id}-message-0002", "chat-b": f"{SCOPE.owner_id}-message-0003"}
    assert handler._runs == {}


# --- the audit row ----------------------------------------------------------------


def test_the_outcome_row_names_the_owner_of_the_run(db) -> None:
    """The service owner would take a scope mint this package may not import."""
    _say(db, 1)

    asyncio.run_coroutine_threadsafe(
        _handler(db, llm_double(lambda **_k: ({"content": "a summary", "finish_reason": "stop"}, {}))).handle(
            ChatMessagePersistedEvent(scope=SCOPE, message_id="m")
        ),
        db.loop,
    ).result(timeout=30)

    rows = sql_audit_event_repository(db.store.engine).list_audit_events(
        log_stream="memory", event_type="context_consolidation"
    )

    assert rows, "the run recorded no outcome"
    assert {(row.owner_id, row.agent_id) for row in rows} == {(SCOPE.owner_id, SCOPE.agent_id)}


# --- the bounds that did not change --------------------------------------------


def test_the_preserved_consolidation_values_are_unchanged() -> None:
    assert consolidation_mod._BATCH_MESSAGES == 100
    assert consolidation_mod.CONTEXT_CONSOLIDATION_INPUT_MAX_BYTES == 512 * 1024
    assert consolidation_mod._SAFETY_MARGIN_TOKENS == 1_024
    assert consolidation_mod._LLM_DEADLINE_MARGIN_SEC == 60.0
    assert consolidation_mod._LEASE_EXPIRY_FACTOR == 2.0


def test_the_model_pair_and_the_shared_deadline_are_the_owners_and_one_window(db) -> None:
    """One deadline covers both attempts, and it is the read window plus the fixed margin."""
    calls: list[dict] = []

    def chat(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "chosen-light":
            raise TimeoutError("the primary timed out")
        return {"content": "a summary", "finish_reason": "stop"}, {}

    _say(db, 1)
    handler = ConsolidationHandler(
        llm_double(chat),
        leases=db.leases,
        summaries=db.summaries,
        history=db.history,
        audit=db.audit,
        user_settings=stub_user_settings(light_model="chosen-light", fallback_model="chosen-fallback"),
        foreground_pressure=db.foreground_pressure(),
        stop_event=threading.Event(),
        holder_id="holder-a",
    )

    asyncio.run_coroutine_threadsafe(
        handler.handle(ChatMessagePersistedEvent(scope=SCOPE, message_id="m")), db.loop
    ).result(timeout=30)

    assert [call["model"] for call in calls] == ["chosen-light", "chosen-fallback"]
    assert {call["read_timeout_sec"] for call in calls} == {300}
    # One deadline for both attempts: the read window plus the fixed margin, which is the
    # same expression the lease expiry is derived from.
    assert len({call["deadline"] for call in calls}) == 1
    assert all(0 < call["timeout_sec"] <= 300 + consolidation_mod._LLM_DEADLINE_MARGIN_SEC for call in calls)
