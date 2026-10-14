"""What the handler decides around a run: thresholds, single-flight, deferral, chaining.

This is what `supervisor/queue.py` and the memory lane used to decide with a task
row. The trigger now carries no decision at all — only a persisted message id.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from background_memory.handlers import consolidation
from background_memory.handlers.consolidation import ConsolidationHandler
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    ConsolidationContinuationEvent,
    IForegroundPressure,
)
from ouroboros.observability.event_record import list_runtime_events
from ouroboros.storage.conversation_format import format_chat_summary_line
from ouroboros.storage.runtime_models import (
    ConsolidationLease,
    ContextSummary,
    ConversationMessage,
    TaskRecord,
)
from ouroboros.utils import estimate_tokens, utc_now_iso
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.background_memory_db import llm_double, memory_database
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.shared import TEST_SCOPE, FakeForegroundPressure, scope_with
from tests.helpers.supervisor import memory_heartbeat
from tests.helpers.user_settings import stub_user_settings

#: The scope every trigger carries; its summary row is keyed by the ownership pair.
CONSOLIDATION_SCOPE = TEST_SCOPE

#: An expiry no test will reach, for a lease a test hands to a run by hand.
_FAR_FUTURE = "2999-01-01T00:00:00+00:00"

_BASE_SETTINGS = {
    "OUROBOROS_CHAT_SUMMARY_MAX_TOKENS": 32_768,
    "OUROBOROS_MODEL_CONTEXT_TOKENS": 200_000,
    "OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC": 300,
    "OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES": 100,
    "OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS": 25_000,
}


class FakeLLM:
    def __init__(self, *, content: str = "updated summary", error: Exception | None = None) -> None:
        self.content, self.error, self.calls = content, error, []

    async def async_chat(self, **kwargs):
        return self.chat(**kwargs)

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"role": "assistant", "content": self.content, "finish_reason": "stop"}, {
            "prompt_tokens": 10,
            "completion_tokens": 4,
        }


#: What the owner chose, read once per delivery. The light model is pinned so no test
#: reaches a configured provider, and the fallback is empty until a test opts in.
_OWNER = stub_user_settings(light_model="test-model", fallback_model="")


@pytest.fixture(autouse=True)
def _owner_settings() -> None:
    global _OWNER
    _OWNER = stub_user_settings(light_model="test-model", fallback_model="")


def _use_fallback_model(name: str) -> None:
    global _OWNER
    _OWNER = stub_user_settings(light_model="test-model", fallback_model=name)


@pytest.fixture
def db(tmp_path, monkeypatch):
    yield from memory_database()


def _settings(monkeypatch, **overrides) -> SimpleNamespace:
    settings = SimpleNamespace(**(_BASE_SETTINGS | overrides))
    monkeypatch.setattr(consolidation, "load_settings", lambda: settings)
    return settings


def append_message(db, index: int, *, scope=None) -> None:
    scope = scope if scope is not None else CONSOLIDATION_SCOPE
    sql_conversation_repository(db.store.engine).append_message(
        ConversationMessage(
            message_id=f"message-{index:04d}",
            dedup_key=f"message-{index:04d}",
            owner_id=scope.owner_id,
            agent_id=scope.agent_id,
            chat_id=scope.chat_id,
            actor_type="owner",
            content=f"msg-{index:04d}|",
            created_at=f"2026-08-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
            metadata={"chat_id": scope.chat_id},
        ),
        scope=scope,
    )


def _delete_backlog(db, *message_ids: str) -> None:
    """Take the rows a run set out from, the one way production removes a stored message."""
    for message_id in message_ids:
        assert sql_conversation_repository(db.store.engine).delete_message(message_id)


def _handler(
    tmp_path, db, llm, *, stop_event: threading.Event | None = None, holder_id: str = "holder-a", **kwargs
) -> ConsolidationHandler:
    del tmp_path
    kwargs.setdefault("foreground_pressure", db.foreground_pressure())
    kwargs.setdefault("user_settings", _OWNER)
    return ConsolidationHandler(
        llm,
        leases=db.leases,
        summaries=db.summaries,
        history=db.history,
        audit=db.audit,
        stop_event=stop_event or threading.Event(),
        holder_id=holder_id,
        **kwargs,
    )


def _lease(db, *, scope=None) -> ConsolidationLease | None:
    return db.run(db.leases.read(scope if scope is not None else CONSOLIDATION_SCOPE))


def _cooldown_left(handler, db, *, scope=None) -> float:
    """What the pair's row says is left of its cooldown, read the way the handler reads it."""
    return handler._failure_cooldown_remaining(_lease(db, scope=scope), time.time())


def _hold(db, handler, *, scope=None, **fields) -> consolidation.ConsolidationRun:
    """A run in flight the way ``_maybe_start_run`` leaves one: the lease first, then the run."""
    scope = scope if scope is not None else CONSOLIDATION_SCOPE
    db.run(db.leases.acquire(scope, holder_id=handler._holder_id, now_iso=utc_now_iso(), expires_at_iso=_FAR_FUTURE))
    fields.setdefault("started_at", time.monotonic())
    run = consolidation.ConsolidationRun(scope=scope, **fields)
    handler._runs[handler._pair_key(scope)] = run
    return run


def _write_lease_columns(db, *, scope=None, **columns: str) -> None:
    """Rewrite a lease row's columns over the engine the protocol declares.

    None of the four db methods backdates a lease or hands a pair to a peer, and
    adding one would be production API existing only for a test.
    """
    scope = scope if scope is not None else CONSOLIDATION_SCOPE
    assignments = ", ".join(f"{column}=:{column}" for column in columns)
    with db.store.engine.connect() as conn:
        conn.execute(
            text(f"UPDATE consolidation_leases SET {assignments} WHERE owner_id=:owner_id AND agent_id=:agent_id"),
            {**columns, "owner_id": scope.owner_id, "agent_id": scope.agent_id},
        )
        conn.commit()


def _expire_lease(db, *, scope=None) -> None:
    """Age the pair's lease out, the way a holder that never came back would."""
    _write_lease_columns(db, scope=scope, lease_expires_at="2000-01-01T00:00:00+00:00")


def _elapse_cooldown(db, *, scope=None) -> None:
    """Move the pair's last failure far enough back that even a capped cooldown has run out."""
    _write_lease_columns(db, scope=scope, last_failure_at="2000-01-01T00:00:00+00:00")


def _trigger(db, handler, message_id: str = "message-0001", *, scope=None):
    """One delivery, awaited on the loop the repositories' engine belongs to."""
    return db.run(
        handler.handle(
            ChatMessagePersistedEvent(scope=scope if scope is not None else CONSOLIDATION_SCOPE, message_id=message_id)
        )
    )


def _backlog_tokens(db) -> int:
    """The token count the scheduler computes for the whole uncommitted backlog."""
    messages = sql_conversation_repository(db.store.engine).list_chat_history_messages(
        scope=CONSOLIDATION_SCOPE, limit=1_000, oldest_first=True
    )
    return sum(estimate_tokens(format_chat_summary_line(message)) + 6 for message in messages)


def _queue_foreground_task(db, task_id: str = "fg1", status: str = "queued") -> None:
    db.store.tasks_repo.upsert_task(
        TaskRecord(
            task_id=task_id,
            owner_id=CONSOLIDATION_SCOPE.owner_id,
            agent_id=CONSOLIDATION_SCOPE.agent_id,
            chat_id=CONSOLIDATION_SCOPE.chat_id,
            task_type="task",
            status=status,
            created_at="2026-08-01T00:00:00+00:00",
        )
    )


def _outcomes(tmp_path) -> list[dict]:
    return list_runtime_events(
        log_stream="memory",
        event_type="context_consolidation",
        audit_event_repository=sql_audit_event_repository(),
    )


# --- thresholds -------------------------------------------------------------


def test_a_backlog_below_both_thresholds_starts_nothing(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=3)
    for index in (1, 2):
        append_message(db, index)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    assert _trigger(db, handler) is None

    assert (handler._runs, llm.calls, _outcomes(tmp_path)) == ({}, [], [])


def test_the_message_threshold_starts_a_run_at_its_boundary(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=3)
    for index in (1, 2, 3):
        append_message(db, index)
    llm = FakeLLM()

    assert _trigger(db, _handler(tmp_path, db, llm)) is None

    assert len(llm.calls) == 1
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE).source_offsets_ref
        == "message-0003"
    )


def test_the_token_threshold_starts_a_run_at_its_boundary(tmp_path, db, monkeypatch):
    for index in (1, 2):
        append_message(db, index)
    # Exactly at the threshold: the message count stays far below its own.
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=_backlog_tokens(db))
    llm = FakeLLM()

    assert _trigger(db, _handler(tmp_path, db, llm)) is None

    assert len(llm.calls) == 1
    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE) is not None


def test_one_token_above_the_backlog_starts_nothing(tmp_path, db, monkeypatch):
    for index in (1, 2):
        append_message(db, index)
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=_backlog_tokens(db) + 1)
    llm = FakeLLM()

    assert _trigger(db, _handler(tmp_path, db, llm)) is None

    assert llm.calls == []


def test_a_missing_anchor_starts_a_run_and_recovers(tmp_path, db, monkeypatch):
    _settings(monkeypatch)
    for index in (1, 2):
        append_message(db, index)
    sql_conversation_repository(db.store.engine).upsert_context_summary(
        ContextSummary(
            owner_id=CONSOLIDATION_SCOPE.owner_id,
            agent_id=CONSOLIDATION_SCOPE.agent_id,
            chat_id=CONSOLIDATION_SCOPE.chat_id,
            source_offsets_ref="missing-anchor",
            summary_ref="previous summary",
        )
    )
    llm = FakeLLM(content="recovered summary")

    # The backlog is two messages against a threshold of a hundred: only the
    # missing anchor makes this run.
    assert _trigger(db, _handler(tmp_path, db, llm)) is None

    source = llm.calls[0]["messages"][1]["content"]
    assert source.index("msg-0001|") < source.index("msg-0002|")
    committed = sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE)
    assert (committed.source_offsets_ref, committed.summary_ref) == ("message-0002", "recovered summary")


@pytest.mark.parametrize(
    "overrides",
    [
        {"OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES": 0},
        {"OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS": -1},
    ],
)
def test_a_non_positive_threshold_is_an_error(tmp_path, db, monkeypatch, overrides):
    _settings(monkeypatch, **overrides)
    append_message(db, 1)

    with pytest.raises(ValueError, match="thresholds must be positive"):
        _trigger(db, _handler(tmp_path, db, FakeLLM()))


# --- single-flight ----------------------------------------------------------


def test_a_trigger_during_an_active_run_is_dropped_without_reading_thresholds(tmp_path, db, monkeypatch):
    def unreadable_settings():
        raise AssertionError("a dropped trigger must not read thresholds")

    monkeypatch.setattr(consolidation, "load_settings", unreadable_settings)
    append_message(db, 1)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)
    handler._runs[handler._pair_key(CONSOLIDATION_SCOPE)] = consolidation.ConsolidationRun(
        scope=CONSOLIDATION_SCOPE, started_at=time.monotonic()
    )

    assert _trigger(db, handler) is None

    assert llm.calls == []
    assert handler._runs  # the active run is untouched


def test_a_continuation_advances_the_active_run(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    monkeypatch.setattr(consolidation, "_BATCH_MESSAGES", 1)
    for index in (1, 2, 3):
        append_message(db, index)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    # The trigger commits the first batch and chains; the continuation is what
    # carries the run forward, not a new trigger.
    follow_up = _trigger(db, handler)
    assert follow_up is not None
    db.run(handler.handle(follow_up.event))

    assert len(llm.calls) == 2
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE).source_offsets_ref
        == "message-0002"
    )


def test_a_continuation_with_no_active_run_is_ignored(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    assert db.run(handler.handle(ConsolidationContinuationEvent(scope=CONSOLIDATION_SCOPE))) is None

    assert (llm.calls, handler._runs, _outcomes(tmp_path)) == ([], {}, [])


# --- single-flight across instances -----------------------------------------


def test_a_trigger_at_a_second_instance_finds_the_pair_leased(tmp_path, db, monkeypatch):
    """Single-flight is the lease row, not one object's dict: two instances, one run."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    monkeypatch.setattr(consolidation, "_BATCH_MESSAGES", 1)
    for index in (1, 2, 3):
        append_message(db, index)
    llm_a, llm_b = FakeLLM(), FakeLLM()
    handler_a = _handler(tmp_path, db, llm_a, holder_id="holder-a")
    handler_b = _handler(tmp_path, db, llm_b, holder_id="holder-b")

    # One batch summarized; the run chains into the rest of the backlog and keeps the pair.
    follow_up = _trigger(db, handler_a)
    assert follow_up is not None

    assert _trigger(db, handler_b) is None
    assert (llm_b.calls, handler_b._runs) == ([], {})

    # The run that holds the pair resumes on its own re-delivery.
    db.run(handler_a.handle(follow_up.event))
    assert len(llm_a.calls) == 2


def test_a_trigger_for_a_pair_another_instance_holds_reads_no_thresholds(tmp_path, db, monkeypatch):
    """The drop is before the threshold read, whichever instance the run is on."""

    def unreadable_settings():
        raise AssertionError("a dropped trigger must not read thresholds")

    append_message(db, 1)
    db.run(
        db.leases.acquire(CONSOLIDATION_SCOPE, holder_id="holder-a", now_iso=utc_now_iso(), expires_at_iso=_FAR_FUTURE)
    )
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm, holder_id="holder-b")
    monkeypatch.setattr(consolidation, "load_settings", unreadable_settings)

    assert _trigger(db, handler) is None

    assert (llm.calls, handler._runs) == ([], {})


def test_a_pairs_cooldown_declines_a_trigger_on_every_instance(tmp_path, db, monkeypatch, caplog):
    """The throttle bounds model calls per pair, not per pair per instance."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    handler_a = _handler(tmp_path, db, FakeLLM(error=RuntimeError("provider outage")), holder_id="holder-a")
    assert _trigger(db, handler_a) is None

    llm_b = FakeLLM()
    handler_b = _handler(tmp_path, db, llm_b, holder_id="holder-b")
    caplog.set_level("INFO")
    append_message(db, 2)

    assert _trigger(db, handler_b) is None

    assert (llm_b.calls, handler_b._runs) == ([], {})
    assert any("1 consecutive failures" in record.getMessage() for record in caplog.records)


def test_a_pairs_cooldown_outlives_the_instance_that_recorded_it(tmp_path, db, monkeypatch):
    """A crash loop that cleared its own throttle on each restart is the loop it exists to close."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    handler_a = _handler(tmp_path, db, FakeLLM(error=RuntimeError("provider outage")), holder_id="holder-a")
    assert _trigger(db, handler_a) is None
    del handler_a  # the instance that recorded the failure is gone

    llm_c = FakeLLM()
    handler_c = _handler(tmp_path, db, llm_c, holder_id="holder-c")
    append_message(db, 2)

    assert _trigger(db, handler_c) is None

    assert llm_c.calls == []
    assert _cooldown_left(handler_c, db) > 0


def test_an_expired_lease_is_reclaimed_by_the_next_trigger(tmp_path, db, monkeypatch):
    """The expiry bounds a holder that never comes back; nothing here detects that it died."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    monkeypatch.setattr(consolidation, "_BATCH_MESSAGES", 1)
    for index in (1, 2, 3):
        append_message(db, index)
    handler_a = _handler(tmp_path, db, FakeLLM(), holder_id="holder-a")
    assert _trigger(db, handler_a) is not None  # holds the pair and never returns
    llm_b = FakeLLM()
    handler_b = _handler(tmp_path, db, llm_b, holder_id="holder-b")
    assert _trigger(db, handler_b) is None and llm_b.calls == []

    _expire_lease(db)

    assert _trigger(db, handler_b) is not None
    assert len(llm_b.calls) == 1
    assert _lease(db).holder_id == "holder-b"


def test_a_stop_request_ends_the_run_and_frees_the_pair(tmp_path, db, monkeypatch):
    """A redeploy is not a failed consolidation, and must not park the pair for an expiry.

    The flag is set before the delivery here, which pins the other half of the rule: no
    model call once it is set, whatever the run's age. What the loop actually produces —
    the flag arriving mid-run — is the two tests below.
    """
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    stop_event = threading.Event()
    stop_event.set()
    llm_a = FakeLLM()
    handler_a = _handler(tmp_path, db, llm_a, stop_event=stop_event, holder_id="holder-a")

    assert _trigger(db, handler_a) is None  # ends, rather than asking for a re-delivery

    assert (llm_a.calls, handler_a._runs, _lease(db)) == ([], {}, None)
    assert _outcomes(tmp_path)[-1]["status"] == "instance_released"

    llm_b = FakeLLM()
    assert _trigger(db, _handler(tmp_path, db, llm_b, holder_id="holder-b")) is None
    assert len(llm_b.calls) == 1


def test_a_stop_during_the_model_call_frees_the_pair(tmp_path, db, monkeypatch):
    """The sequence the loop really produces: the flag arrives while the run is in flight.

    The loop re-checks the stop signal before every delivery, so a follow-up produced after
    the flag is set is never delivered. A run that asked for one would be abandoned holding
    its pair, and every instance of the fleet that came back would drop that pair's triggers
    until the lease expired — worse than the abandonment before this change, which cost only
    a restart's worth of re-summarizing.
    """
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    monkeypatch.setattr(consolidation, "_BATCH_MESSAGES", 1)
    for index in (1, 2, 3):
        append_message(db, index)
    stop_event = threading.Event()
    llm = FakeLLM()
    chat = llm.chat

    def stopping_chat(**kwargs):
        # The fleet starts rolling while this call is in flight.
        stop_event.set()
        return chat(**kwargs)

    llm.chat = stopping_chat
    handler = _handler(tmp_path, db, llm, stop_event=stop_event)

    assert _trigger(db, handler) is None  # no follow-up the loop would never deliver

    assert (len(llm.calls), handler._runs, _lease(db)) == (1, {}, None)


def test_a_stop_mid_run_leaves_no_leased_pair_behind_in_the_loop(tmp_path, db, monkeypatch):
    """The same regression through the loop that produces it: stop, exit, nothing parked."""

    from background_memory.infra.events.inmemory import InMemoryEventConsumer, InMemoryEventPublisher, MemoryEventQueue
    from background_memory.runtime import MemoryRuntime

    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    monkeypatch.setattr(consolidation, "_BATCH_MESSAGES", 1)
    for index in (1, 2, 3):
        append_message(db, index)
    stop_event = threading.Event()
    llm = FakeLLM()
    chat = llm.chat

    def stopping_chat(**kwargs):
        stop_event.set()
        return chat(**kwargs)

    llm.chat = stopping_chat
    handler = _handler(tmp_path, db, llm, stop_event=stop_event)
    shared = MemoryEventQueue(4)
    publisher, consumer = InMemoryEventPublisher(shared), InMemoryEventConsumer(shared)
    runtime = MemoryRuntime(
        consumer,
        handlers={ChatMessagePersistedEvent: handler.handle, ConsolidationContinuationEvent: handler.handle},
        stop_event=stop_event,
        heartbeat=memory_heartbeat(),
        drain_timeout_sec=5.0,
    )
    # On the loop the repositories' engine belongs to, which is where the roots run it.
    running = asyncio.run_coroutine_threadsafe(runtime.execute(), db.loop)
    try:
        publisher.publish(ChatMessagePersistedEvent(scope=CONSOLIDATION_SCOPE, message_id="message-0001"))
        # The loop leaves on its own once the flag is set: nothing else stops it here.
        running.result(timeout=15)
    finally:
        stop_event.set()
        running.result(timeout=30)

    assert running.done()
    # No outstanding follow-up, and no pair left leased for the fleet that comes back.
    assert (handler._runs, runtime._pending, _lease(db)) == ({}, {}, None)


def test_a_stop_while_yielding_to_the_foreground_frees_the_pair(tmp_path, db, monkeypatch):
    """The other re-delivery request: a deferral, with the flag arriving as it is decided."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    stop_event = threading.Event()

    class StoppingForegroundPressure(IForegroundPressure):
        """Foreground work is pending, and the fleet stops in the same instant."""

        async def is_pending(self) -> bool:
            stop_event.set()
            return True

    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm, stop_event=stop_event, foreground_pressure=StoppingForegroundPressure())

    assert _trigger(db, handler) is None

    assert (llm.calls, handler._runs, _lease(db)) == ([], {}, None)


def test_a_restarted_loop_takes_back_the_pairs_its_predecessor_held(tmp_path, db, monkeypatch):
    """A holder id names the process, so its own abandoned lease is not a peer's.

    A dispatch table rebuilt in one process presents a fresh handler with no runs on it.
    Nothing else may take those pairs before they expire, and this process is the only one
    that can say the run behind that row is gone.
    """
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    _queue_foreground_task(db)
    crashed = _handler(tmp_path, db, FakeLLM(), holder_id="one-process")
    assert _trigger(db, crashed) is not None  # a run in flight, holding the pair
    assert _lease(db).holder_id == "one-process"

    db.store.tasks_repo.cancel_task("fg1", result="done")
    llm = FakeLLM()
    restarted = _handler(tmp_path, db, llm, holder_id="one-process")

    assert _trigger(db, restarted) is None

    assert len(llm.calls) == 1
    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE) is not None


def test_a_run_that_lost_its_lease_ends_before_the_model_call(tmp_path, db, monkeypatch):
    """A displaced run leaves no mark on the gate: the instance that took over did nothing wrong."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    monkeypatch.setattr(consolidation, "_BATCH_MESSAGES", 1)
    for index in (1, 2, 3):
        append_message(db, index)
    llm_a = FakeLLM()
    handler_a = _handler(tmp_path, db, llm_a, holder_id="holder-a")
    follow_up = _trigger(db, handler_a)
    assert follow_up is not None and len(llm_a.calls) == 1

    _write_lease_columns(db, holder_id="holder-b")

    assert db.run(handler_a.handle(follow_up.event)) is None

    assert (len(llm_a.calls), handler_a._runs) == (1, {})
    assert _outcomes(tmp_path)[-1]["status"] == "lease_lost"
    lease = _lease(db)
    assert (lease.holder_id, lease.consecutive_failures) == ("holder-b", 0)


def test_a_run_that_finds_nothing_to_summarize_frees_the_pair(tmp_path, db, monkeypatch):
    """Nothing was attempted, so nothing is reported — but the pair still comes back."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)
    follow_up = _trigger(db, handler)  # defers to the foreground, holding the pair
    assert follow_up is not None and _lease(db) is not None

    db.store.tasks_repo.cancel_task("fg1", result="done")
    # Reclassified, not deleted: a deleted backlog is an erased conversation, which the run
    # reports as its own outcome. This exit is the narrower one — the rows are still there and
    # none of them belongs to history any more, so nothing was attempted and nothing is
    # reported.
    assert sql_conversation_repository(db.store.engine).update_message_metadata(
        "message-0001", {"chat_id": 1, "is_progress": True}
    )

    assert db.run(handler.handle(follow_up.event)) is None

    assert (handler._runs, llm.calls, _outcomes(tmp_path), _lease(db)) == ({}, [], [], None)


# --- deferral ---------------------------------------------------------------


def test_active_foreground_work_defers_the_run(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    follow_up = _trigger(db, handler)

    assert follow_up is not None
    assert (follow_up.event, follow_up.retry_after) == (ConsolidationContinuationEvent(scope=CONSOLIDATION_SCOPE), 5.0)
    assert llm.calls == []
    assert handler._runs  # the run is kept, not dropped
    assert _lease(db) is not None  # and so is its lease: the run intends to resume


def test_the_run_yields_to_the_injected_interface_not_a_query_of_its_own(tmp_path, db, monkeypatch):
    """The handler asks; where the answer comes from is the composition root's business."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    pressure = FakeForegroundPressure(pending=True)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm, foreground_pressure=pressure)

    # No task row anywhere, so only the interface can be deferring this run.
    assert db.store.tasks_repo.list_tasks(statuses=["queued", "scheduled", "interrupted", "running"]) == []
    follow_up = _trigger(db, handler)

    assert follow_up is not None
    assert (follow_up.event, follow_up.retry_after) == (ConsolidationContinuationEvent(scope=CONSOLIDATION_SCOPE), 5.0)
    assert llm.calls == []

    pressure.pending = False
    assert db.run(handler.handle(follow_up.event)) is None
    assert len(llm.calls) == 1


@pytest.mark.parametrize("status", ["queued", "scheduled", "interrupted", "running"])
def test_every_active_status_counts_as_foreground_work(tmp_path, db, monkeypatch, status):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    _queue_foreground_task(db, status=status)
    llm = FakeLLM()

    assert _trigger(db, _handler(tmp_path, db, llm)) is not None
    assert llm.calls == []


def test_a_deferred_run_resumes_without_a_new_trigger(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)
    follow_up = _trigger(db, handler)
    assert follow_up is not None

    db.store.tasks_repo.cancel_task("fg1", result="done")
    assert db.run(handler.handle(follow_up.event)) is None

    assert len(llm.calls) == 1
    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE) is not None


def test_an_overdue_run_stops_yielding_to_the_foreground(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)
    _hold(db, handler, started_at=time.monotonic() - 601)

    assert db.run(handler.handle(ConsolidationContinuationEvent(scope=CONSOLIDATION_SCOPE))) is None

    assert len(llm.calls) == 1


def test_a_stop_request_ends_even_an_overdue_run(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    stop_event = threading.Event()
    stop_event.set()
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm, stop_event=stop_event)
    _hold(db, handler, started_at=time.monotonic() - 601)

    assert db.run(handler.handle(ConsolidationContinuationEvent(scope=CONSOLIDATION_SCOPE))) is None

    assert (llm.calls, handler._runs, _lease(db)) == ([], {}, None)


# --- chaining ---------------------------------------------------------------


def test_a_committed_run_chains_into_the_remaining_backlog(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    monkeypatch.setattr(consolidation, "_BATCH_MESSAGES", 1)
    for index in (1, 2):
        append_message(db, index)
    handler = _handler(tmp_path, db, FakeLLM())

    follow_up = _trigger(db, handler)

    assert follow_up is not None
    assert (follow_up.event, follow_up.retry_after) == (ConsolidationContinuationEvent(scope=CONSOLIDATION_SCOPE), 0.0)
    assert handler._runs[handler._pair_key(CONSOLIDATION_SCOPE)].expected_cursor == "message-0001"


def test_a_committed_run_with_a_backlog_under_the_thresholds_stops(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=2)
    monkeypatch.setattr(consolidation, "_BATCH_MESSAGES", 2)
    for index in (1, 2, 3):
        append_message(db, index)
    handler = _handler(tmp_path, db, FakeLLM())

    assert _trigger(db, handler) is None

    assert handler._runs == {}
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE).source_offsets_ref
        == "message-0002"
    )


def test_a_failure_with_an_unchanged_tail_does_not_retry(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    llm = FakeLLM(error=RuntimeError("provider outage"))
    handler = _handler(tmp_path, db, llm)

    assert _trigger(db, handler) is None

    assert (handler._runs, len(llm.calls)) == ({}, 1)
    assert _outcomes(tmp_path)[0]["status"] == "failed"


def test_a_failure_starts_nothing_even_after_the_tail_moved(tmp_path, db, monkeypatch):
    """A failed run schedules nothing at all, or failure becomes a retry loop.

    The messages that arrived meanwhile trigger on their own, at the rate they
    actually arrive.
    """
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    calls: list[dict] = []

    def chat(**kwargs):
        calls.append(kwargs)
        # The conversation moved on while the run was failing.
        append_message(db, len(calls) + 1)
        raise RuntimeError("provider outage")

    handler = _handler(tmp_path, db, llm_double(chat))

    assert _trigger(db, handler) is None
    assert (handler._runs, len(calls)) == ({}, 1)


# --- the failure cooldown ---------------------------------------------------


def test_consecutive_failures_throttle_scheduling_and_the_wait_grows(tmp_path, db, monkeypatch):
    """A failing consolidation in an active chat creates a bounded number of runs, not a stream."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    llm = FakeLLM(error=RuntimeError("provider outage"))
    handler = _handler(tmp_path, db, llm)

    assert _trigger(db, handler) is None
    first_wait = _cooldown_left(handler, db)
    assert first_wait > 0

    # Every persisted message asks, and the cooldown declines each one.
    for index in range(2, 6):
        append_message(db, index)
        assert _trigger(db, handler) is None
    assert len(llm.calls) == 1

    # The cooldown expired; the next trigger runs, fails, and earns a longer one.
    _elapse_cooldown(db)
    assert _trigger(db, handler) is None
    assert len(llm.calls) == 2
    assert _cooldown_left(handler, db) > first_wait


def test_a_throttled_trigger_says_why_at_info(tmp_path, db, monkeypatch, caplog):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    handler = _handler(tmp_path, db, FakeLLM(error=RuntimeError("provider outage")))
    assert _trigger(db, handler) is None

    caplog.set_level("INFO")
    append_message(db, 2)
    assert _trigger(db, handler) is None

    assert any(
        record.levelname == "INFO" and "1 consecutive failures" in record.getMessage() for record in caplog.records
    )


def test_a_completed_run_reopens_the_gate_immediately(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    handler = _handler(tmp_path, db, FakeLLM(error=RuntimeError("provider outage")))
    assert _trigger(db, handler) is None
    assert _lease(db).consecutive_failures == 1

    _elapse_cooldown(db)
    handler._llm = FakeLLM()
    append_message(db, 2)
    assert _trigger(db, handler) is None

    # Reset is delete: a completed run leaves the pair no throttle state at all.
    assert (_lease(db), _cooldown_left(handler, db)) == (None, 0.0)


def test_a_pair_with_no_recorded_failure_is_not_throttled(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    handler = _handler(tmp_path, db, FakeLLM())

    assert _cooldown_left(handler, db) == 0.0
    assert _trigger(db, handler) is None
    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE) is not None


# --- what a run reports -----------------------------------------------------


def test_a_completed_run_records_one_observability_event(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    for index in (1, 2):
        append_message(db, index)

    assert _trigger(db, _handler(tmp_path, db, FakeLLM())) is None

    outcomes = _outcomes(tmp_path)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "completed"
    # The lease is the pair's, but a run reads and commits one conversation, so the record
    # names all three axes — and no fourth, because the surface is not one of them.
    assert (outcomes[0]["owner_id"], outcomes[0]["agent_id"], outcomes[0]["chat_id"]) == (
        CONSOLIDATION_SCOPE.owner_id,
        CONSOLIDATION_SCOPE.agent_id,
        CONSOLIDATION_SCOPE.chat_id,
    )
    assert "surface_id" not in outcomes[0]
    assert outcomes[0]["source_offsets_ref"] == "message-0002"
    assert outcomes[0]["scheduled_tail_message_id"] == "message-0002"
    assert (outcomes[0]["batches"], outcomes[0]["prompt_tokens"], outcomes[0]["completion_tokens"]) == (1, 10, 4)


def test_a_failed_run_records_one_observability_event(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)

    assert _trigger(db, _handler(tmp_path, db, FakeLLM(error=RuntimeError("boom")))) is None

    outcomes = _outcomes(tmp_path)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "failed"
    assert "provider_outage/RuntimeError" in outcomes[0]["detail"]
    assert outcomes[0]["scheduled_tail_message_id"] == "message-0001"


def test_a_stale_run_records_one_observability_event(tmp_path, db, monkeypatch):
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)
    _hold(db, handler, expected_cursor="another-cursor")

    assert db.run(handler.handle(ConsolidationContinuationEvent(scope=CONSOLIDATION_SCOPE))) is None

    outcomes = _outcomes(tmp_path)
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "stale_cursor"
    assert llm.calls == []


def test_a_run_reaches_no_supervisor_event_stream(tmp_path, db, monkeypatch):
    """No task row means no task_done, and no usage event to attribute anywhere."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)

    _trigger(db, _handler(tmp_path, db, FakeLLM()))

    assert (
        list_runtime_events(
            log_stream="events", event_type="task_done", audit_event_repository=sql_audit_event_repository()
        )
        == []
    )
    assert (
        list_runtime_events(
            log_stream="events", event_type="llm_usage", audit_event_repository=sql_audit_event_repository()
        )
        == []
    )
    assert db.store.tasks_repo.list_tasks(statuses=["queued", "scheduled", "interrupted", "running"]) == []


# --- per ownership pair -----------------------------------------------------


def test_one_pairs_active_run_does_not_drop_another_pairs_trigger(tmp_path, db, monkeypatch):
    """Single-flight is per ownership pair, not per handler."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    other_scope = scope_with(CONSOLIDATION_SCOPE, owner_id="other-owner")
    append_message(db, 1)
    append_message(db, 2, scope=other_scope)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    assert _trigger(db, handler) is not None  # pair A starts a run and defers
    assert _trigger(db, handler, scope=other_scope) is not None  # pair B is evaluated, not dropped

    assert set(handler._runs) == {handler._pair_key(CONSOLIDATION_SCOPE), handler._pair_key(other_scope)}
    assert llm.calls == []


def test_a_second_chats_trigger_is_delayed_by_the_pairs_lease_not_lost(tmp_path, db, monkeypatch):
    """The lease is the pair's while the summary is the chat's, so B waits and keeps its backlog."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    chat_scope = scope_with(CONSOLIDATION_SCOPE, chat_id="chat-1")
    other_chat_scope = scope_with(CONSOLIDATION_SCOPE, chat_id="chat-2")
    append_message(db, 1, scope=chat_scope)
    append_message(db, 2, scope=other_chat_scope)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    follow_up = _trigger(db, handler, scope=chat_scope)
    assert follow_up is not None
    assert _trigger(db, handler, scope=other_chat_scope) is None  # dropped by the pair's active run
    assert llm.calls == []

    db.store.tasks_repo.cancel_task("fg1", result="done")
    assert db.run(handler.handle(follow_up.event)) is None

    assert handler._runs == {}
    assert len(llm.calls) == 1
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(chat_scope).source_offsets_ref
        == "message-0001"
    )
    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(other_chat_scope) is None
    assert [
        m.message_id
        for m in sql_conversation_repository(db.store.engine).list_chat_history_messages(
            scope=other_chat_scope, limit=10
        )
    ] == ["message-0002"]

    assert _trigger(db, handler, scope=other_chat_scope) is None  # B now runs from its own cursor
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(other_chat_scope).source_offsets_ref
        == "message-0002"
    )
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(chat_scope).source_offsets_ref
        == "message-0001"
    )


def test_a_run_whose_backlog_was_deleted_drops_it_without_committing(tmp_path, db, monkeypatch):
    """The run's own head is what ends it: nothing takes the pair's lease away from it.

    Without the guard the run renews, finds an empty expected cursor, and commits a summary of
    messages that no longer exist — handing that history back into `## Recent chat`.
    """
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    cleared = scope_with(CONSOLIDATION_SCOPE, chat_id="chat-cleared")
    append_message(db, 1, scope=cleared)
    append_message(db, 2, scope=cleared)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    follow_up = _trigger(db, handler, scope=cleared)  # defers to the foreground, holding the pair
    assert follow_up is not None

    # A row arriving after the run started survives, so the backlog the run re-reads is not
    # empty — and the head it set out from is gone all the same.
    append_message(db, 3, scope=cleared)
    _delete_backlog(db, "message-0001", "message-0002")
    db.store.tasks_repo.cancel_task("fg1", result="done")

    assert db.run(handler.handle(follow_up.event)) is None

    assert llm.calls == []
    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(cleared) is None
    assert handler._runs == {}
    assert _outcomes(tmp_path)[0]["status"] == "chat_cleared"


def test_a_deletion_during_the_model_call_commits_nothing(tmp_path, db, monkeypatch):
    """The one unbounded window in a delivery, and the case with no cursor to compare.

    A conversation that has never been consolidated expects an empty cursor, so the commit
    falls through to an insert that nothing refuses — the summary of the deleted backlog would
    land, and `## Recent chat` would read it back.
    """
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    cleared = scope_with(CONSOLIDATION_SCOPE, chat_id="chat-cleared")
    append_message(db, 1, scope=cleared)
    append_message(db, 2, scope=cleared)

    def chat(**kwargs):
        # The backlog goes while the provider is answering.
        append_message(db, 3, scope=cleared)
        _delete_backlog(db, "message-0001", "message-0002")
        return {"role": "assistant", "content": "a summary of erased history", "finish_reason": "stop"}, {}

    handler = _handler(tmp_path, db, llm_double(chat))

    assert _trigger(db, handler, scope=cleared) is None

    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(cleared) is None
    assert handler._runs == {}
    assert _outcomes(tmp_path)[0]["status"] == "chat_cleared"


def test_a_deletion_that_spares_the_runs_own_tail_still_drops_the_run(tmp_path, db, monkeypatch):
    """The head is the end the run compares, and a surviving tail does not save it.

    A deletion that stops short of the newest message leaves the tail in place, so a guard
    reading that end would let the run commit a summary of rows that are gone.
    """
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    cleared = scope_with(CONSOLIDATION_SCOPE, chat_id="chat-cleared")
    for index in (1, 2, 3):
        append_message(db, index, scope=cleared)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    follow_up = _trigger(db, handler, scope=cleared, message_id="message-0003")
    assert follow_up is not None
    assert handler._runs[handler._pair_key(cleared)].tail_message_id == "message-0003"

    _delete_backlog(db, "message-0001", "message-0002")
    db.store.tasks_repo.cancel_task("fg1", result="done")

    assert db.run(handler.handle(follow_up.event)) is None

    assert llm.calls == []
    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(cleared) is None
    assert _outcomes(tmp_path)[0]["status"] == "chat_cleared"


def test_a_deleted_backlog_does_not_throttle_the_pairs_other_conversations(tmp_path, db, monkeypatch):
    """A mid-batch run whose backlog is gone is not a failure of that run.

    Its pending fragment no longer follows the committed cursor, which `_build_batch` reports
    as a `ValueError` — counted as `failed`, and the count lives on the pair's lease, so it
    would decline the pair's *other* conversations for five minutes and up.
    """
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    cleared = scope_with(CONSOLIDATION_SCOPE, chat_id="chat-cleared")
    sibling = scope_with(CONSOLIDATION_SCOPE, chat_id="chat-sibling")
    append_message(db, 1, scope=cleared)
    append_message(db, 2, scope=cleared)
    append_message(db, 4, scope=sibling)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)
    _hold(
        db,
        handler,
        scope=cleared,
        pending_message_id="message-0002",
        pending_offset=3,
        pending_summary="half of an erased conversation",
        tail_message_id="message-0002",
        head_message_id="message-0001",
    )
    # A failure the pair already carries, long enough ago that its cooldown has run out.
    _write_lease_columns(
        db,
        scope=cleared,
        consecutive_failures="1",
        last_failure_at="2000-01-01T00:00:00+00:00",
    )

    append_message(db, 3, scope=cleared)
    _delete_backlog(db, "message-0001", "message-0002")

    assert db.run(handler.handle(ConsolidationContinuationEvent(scope=cleared))) is None

    assert llm.calls == []
    assert _outcomes(tmp_path)[0]["status"] == "chat_cleared"
    # Untouched, not merely zero: the count was already standing when the run ended.
    assert _lease(db).consecutive_failures == 1
    assert _cooldown_left(handler, db) == 0.0
    assert _trigger(db, handler, scope=sibling) is None
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(sibling).source_offsets_ref
        == "message-0004"
    )


def test_a_continuation_resumes_its_own_run_and_leaves_the_other_untouched(tmp_path, db, monkeypatch):
    """A continuation names its pair; the other pair's run is not advanced."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    other_scope = scope_with(CONSOLIDATION_SCOPE, agent_id="other-agent")
    append_message(db, 1)
    append_message(db, 2, scope=other_scope)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    follow_up_a = _trigger(db, handler)
    follow_up_b = _trigger(db, handler, scope=other_scope)
    assert follow_up_a is not None and follow_up_b is not None
    assert llm.calls == []

    db.store.tasks_repo.cancel_task("fg1", result="done")
    assert db.run(handler.handle(follow_up_a.event)) is None  # pair A completes

    assert len(llm.calls) == 1
    assert handler._pair_key(CONSOLIDATION_SCOPE) not in handler._runs
    assert handler._pair_key(other_scope) in handler._runs  # pair B's run untouched
    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(other_scope) is None
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE).source_offsets_ref
        == "message-0001"
    )


def test_one_pairs_failures_do_not_throttle_another(tmp_path, db, monkeypatch):
    """The failure cooldown is per pair: another pair starts on thresholds alone."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    other_scope = scope_with(CONSOLIDATION_SCOPE, owner_id="other-owner")
    append_message(db, 1)
    append_message(db, 2, scope=other_scope)
    handler = _handler(tmp_path, db, FakeLLM(error=RuntimeError("provider outage")))

    assert _trigger(db, handler) is None  # pair A fails
    assert _lease(db).consecutive_failures == 1

    handler._llm = FakeLLM()
    assert _trigger(db, handler, scope=other_scope) is None  # pair B runs and completes

    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(other_scope) is not None
    assert _lease(db).consecutive_failures == 1


def test_a_completed_run_deletes_its_own_throttle_state_and_nothing_else(tmp_path, db, monkeypatch):
    """Reset is delete: the healthy path prunes its pair's entry, not another's."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    other_scope = scope_with(CONSOLIDATION_SCOPE, owner_id="other-owner")
    append_message(db, 1)
    append_message(db, 2, scope=other_scope)
    handler = _handler(tmp_path, db, FakeLLM(error=RuntimeError("provider outage")))
    assert _trigger(db, handler) is None
    assert _trigger(db, handler, scope=other_scope) is None
    assert (_lease(db).consecutive_failures, _lease(db, scope=other_scope).consecutive_failures) == (1, 1)

    _elapse_cooldown(db)
    handler._llm = FakeLLM()
    append_message(db, 3)
    assert _trigger(db, handler) is None  # pair A completes

    assert _lease(db) is None
    assert _lease(db, scope=other_scope).consecutive_failures == 1


def test_a_failure_while_starting_leaves_no_run_recorded(tmp_path, db, monkeypatch):
    """A run is recorded only after every call that can raise has returned."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    handler = _handler(tmp_path, db, FakeLLM())
    original = type(db.summaries).read
    calls = {"n": 0}

    async def flaky(repo, scope):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return await original(repo, scope)

    monkeypatch.setattr(type(db.summaries), "read", flaky)

    with pytest.raises(RuntimeError, match="db down"):
        _trigger(db, handler)

    assert handler._runs == {}
    assert _trigger(db, handler) is None  # the next trigger is evaluated as usual
    assert db.run(db.summaries.read(CONSOLIDATION_SCOPE)) is not None


def test_a_failing_run_is_released_before_handle_returns(tmp_path, db, monkeypatch):
    """A run left in the map with no scheduled follow-up is unreachable forever."""
    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    append_message(db, 1)
    handler = _handler(tmp_path, db, FakeLLM(error=RuntimeError("provider outage")))

    assert _trigger(db, handler) is None

    assert handler._runs == {}
    assert not _lease(db).is_held(utc_now_iso())  # and the pair is free for any instance
    _elapse_cooldown(db)
    handler._llm = FakeLLM()
    append_message(db, 2)
    assert _trigger(db, handler) is None  # not dropped by single-flight
    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE) is not None


def test_two_pairs_deferring_concurrently_deliver_both_follow_ups(tmp_path, db, monkeypatch):
    """Regression: one follow-up slot, two live runs — the second overwrote the
    first, stranding a run that its own single-flight then blocked forever."""

    from background_memory.infra.events.inmemory import (
        InMemoryEventConsumer,
        InMemoryEventPublisher,
        MemoryEventQueue,
    )
    from background_memory.runtime import MemoryRuntime

    _settings(monkeypatch, OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1)
    other_scope = scope_with(CONSOLIDATION_SCOPE, owner_id="other-owner")
    append_message(db, 1)
    append_message(db, 2, scope=other_scope)
    _queue_foreground_task(db)
    llm = FakeLLM()
    handler = _handler(tmp_path, db, llm)

    shared = MemoryEventQueue(4)
    publisher, consumer = InMemoryEventPublisher(shared), InMemoryEventConsumer(shared)
    stop_event = threading.Event()
    runtime = MemoryRuntime(
        consumer,
        handlers={
            ChatMessagePersistedEvent: handler.handle,
            ConsolidationContinuationEvent: handler.handle,
        },
        stop_event=stop_event,
        heartbeat=memory_heartbeat(),
        drain_timeout_sec=5.0,
    )
    running = asyncio.run_coroutine_threadsafe(runtime.execute(), db.loop)
    try:
        publisher.publish(ChatMessagePersistedEvent(scope=CONSOLIDATION_SCOPE, message_id="message-0001"))
        publisher.publish(ChatMessagePersistedEvent(scope=other_scope, message_id="message-0002"))

        # Both trigger deliveries defer; both follow-ups must be outstanding at
        # once — the stall left only one.
        deadline = time.monotonic() + 15
        while len(runtime._pending) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(runtime._pending) == 2

        # Foreground clears; each pair's follow-up is delivered in its turn, so
        # both runs finish and neither pair is left with an unreachable run.
        db.store.tasks_repo.cancel_task("fg1", result="done")
        # Generous: each deferral costs a 5-second recheck, and the case exits the moment
        # the runs clear rather than waiting this out.
        deadline = time.monotonic() + 30
        while handler._runs and time.monotonic() < deadline:
            time.sleep(0.01)

        assert handler._runs == {}
        assert len(llm.calls) == 2
    finally:
        stop_event.set()
        running.result(timeout=30)
