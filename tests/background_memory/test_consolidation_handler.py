"""One consolidation run: batching, fragments, recovery, the model call, the commit.

Moved off the supervisor's memory lane with the code: what used to be a durable
task row advancing through statuses is now one run advancing through follow-ups.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from background_memory.handlers import consolidation
from background_memory.handlers.consolidation import ConsolidationHandler, ConsolidationRun
from background_memory.interfaces import ConsolidationContinuationEvent
from ouroboros.observability.aef_tracing import trace_scope
from ouroboros.observability.event_record import list_runtime_events
from ouroboros.storage.runtime_models import ContextSummary, ConversationMessage
from ouroboros.utils import utc_now_iso
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.background_memory_db import llm_double, memory_database
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.user_settings import stub_user_settings

#: The scope every trigger carries; its summary row is keyed by the ownership pair.
CONSOLIDATION_SCOPE = TEST_SCOPE

SETTINGS = SimpleNamespace(
    OUROBOROS_CHAT_SUMMARY_MAX_TOKENS=32_768,
    OUROBOROS_MODEL_CONTEXT_TOKENS=200_000,
    OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=300,
    OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=100,
    OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000,
)


class FakeLLM:
    def __init__(self, *, content: str = "updated summary", finish_reason: str = "stop") -> None:
        self.content, self.finish_reason, self.calls = content, finish_reason, []

    async def async_chat(self, **kwargs):
        return self.chat(**kwargs)

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"role": "assistant", "content": self.content, "finish_reason": self.finish_reason}, {
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
    monkeypatch.setattr(consolidation, "load_settings", lambda: SETTINGS)
    yield from memory_database()


def append_message(db, index: int, *, content: str | None = None) -> None:
    sql_conversation_repository(db.store.engine).append_message(
        ConversationMessage(
            message_id=f"message-{index:04d}",
            dedup_key=f"message-{index:04d}",
            owner_id=CONSOLIDATION_SCOPE.owner_id,
            agent_id=CONSOLIDATION_SCOPE.agent_id,
            chat_id=CONSOLIDATION_SCOPE.chat_id,
            actor_type="owner",
            content=content or f"msg-{index:04d}|",
            created_at=f"2026-08-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
            metadata={"chat_id": 1},
        ),
        scope=CONSOLIDATION_SCOPE,
    )


def _handler(tmp_path, db, llm, *, stop_event: threading.Event | None = None) -> ConsolidationHandler:
    del tmp_path
    return ConsolidationHandler(
        llm,
        leases=db.leases,
        summaries=db.summaries,
        history=db.history,
        audit=db.audit,
        user_settings=_OWNER,
        foreground_pressure=db.foreground_pressure(),
        stop_event=stop_event or threading.Event(),
        holder_id="holder-a",
    )


def _started(db, handler: ConsolidationHandler, *, expected_cursor: str = "", scope=None) -> ConsolidationHandler:
    """Put a run in flight, so a continuation exercises the transition alone.

    The lease comes first, as ``_maybe_start_run`` takes it: every delivery renews it,
    and a run holding none would end before its first model call.
    """
    scope = scope if scope is not None else CONSOLIDATION_SCOPE
    db.run(
        db.leases.acquire(
            scope, holder_id=handler._holder_id, now_iso=utc_now_iso(), expires_at_iso="2999-01-01T00:00:00+00:00"
        )
    )
    handler._runs[handler._pair_key(scope)] = ConsolidationRun(
        scope=scope, expected_cursor=expected_cursor, started_at=time.monotonic()
    )
    return handler


def _advance(db, handler: ConsolidationHandler):
    return db.run(handler.handle(ConsolidationContinuationEvent(scope=CONSOLIDATION_SCOPE)))


def _outcomes(tmp_path) -> list[dict]:
    return list_runtime_events(
        log_stream="memory",
        event_type="context_consolidation",
        audit_event_repository=sql_audit_event_repository(),
    )


# --- the batch and its fragments --------------------------------------------


@pytest.mark.parametrize("content", ["Ж" * 1_500 + "MESSAGE-END", "A" * 3_000 + "MESSAGE-END"])
def test_an_oversized_message_is_summarized_in_bounded_fragments(tmp_path, db, monkeypatch, content):
    append_message(db, 1, content=content)
    monkeypatch.setattr(consolidation, "CONTEXT_CONSOLIDATION_INPUT_MAX_BYTES", 2_048)
    llm = FakeLLM()
    handler = _started(db, _handler(tmp_path, db, llm))

    while True:
        calls_before = len(llm.calls)
        follow_up = _advance(db, handler)
        assert len(llm.calls) == calls_before + 1
        if follow_up is None:
            break
        # Each unfinished fragment asks to be handed straight back.
        assert (follow_up.event, follow_up.retry_after) == (
            ConsolidationContinuationEvent(scope=CONSOLIDATION_SCOPE),
            0.0,
        )
        assert sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE) is None

    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE).source_offsets_ref
        == "message-0001"
    )
    fragments = [call["messages"][1]["content"].rsplit("CHAT_DATA: ", 1)[-1].splitlines()[0] for call in llm.calls]
    for call, fragment in zip(llm.calls, fragments, strict=True):
        request_bytes = len(json.dumps(call["messages"], ensure_ascii=False, separators=(",", ":")).encode())
        assert request_bytes <= consolidation.CONTEXT_CONSOLIDATION_INPUT_MAX_BYTES
        assert fragment.startswith("← 00:00 [User] ")
        assert call["max_tokens"] == SETTINGS.OUROBOROS_CHAT_SUMMARY_MAX_TOKENS
    assert "".join(fragment.removeprefix("← 00:00 [User] ") for fragment in fragments) == content


def _fails_with(db, handler, tmp_path, expected_error: str) -> None:
    assert _advance(db, handler) is None
    assert handler._runs == {}
    outcome = _outcomes(tmp_path)[0]
    assert outcome["status"] == "failed"
    assert expected_error in outcome["detail"]


@pytest.mark.parametrize(("pending_message_id", "pending_offset"), [("", -1), ("message-0001", -1), ("", 5)])
def test_an_invalid_continuation_fails_the_run(tmp_path, db, pending_message_id, pending_offset):
    """A negative offset, or an offset with no message to resume, is corruption."""
    append_message(db, 1, content="Ж" * 20)
    handler = _handler(tmp_path, db, FakeLLM())
    _started(db, handler)
    handler._runs[handler._pair_key(CONSOLIDATION_SCOPE)].pending_message_id = pending_message_id
    handler._runs[handler._pair_key(CONSOLIDATION_SCOPE)].pending_offset = pending_offset

    _fails_with(db, handler, tmp_path, "invalid chat consolidation continuation")


def test_a_pending_offset_outside_the_message_fails_the_run(tmp_path, db):
    append_message(db, 1, content="Ж" * 20)
    handler = _handler(tmp_path, db, FakeLLM())
    _started(db, handler)
    run = handler._runs[handler._pair_key(CONSOLIDATION_SCOPE)]
    run.pending_message_id = "message-0001"
    run.pending_offset = 9_999

    _fails_with(db, handler, tmp_path, "pending offset is outside the UTF-8 message")


def test_a_pending_offset_off_a_utf8_boundary_fails_the_run(tmp_path, db):
    from ouroboros.storage.conversation_format import format_chat_summary_line

    append_message(db, 1, content="Ж" * 20)
    message = sql_conversation_repository(db.store.engine).list_chat_history_messages(
        scope=CONSOLIDATION_SCOPE, limit=1
    )[0]
    attribution = format_chat_summary_line(message).removesuffix(message.display_text())
    handler = _handler(tmp_path, db, FakeLLM())
    _started(db, handler)
    run = handler._runs[handler._pair_key(CONSOLIDATION_SCOPE)]
    # One byte into a two-byte character.
    run.pending_message_id = "message-0001"
    run.pending_offset = len(attribution.encode("utf-8")) + 1

    _fails_with(db, handler, tmp_path, "pending offset is not on a UTF-8 boundary")


def test_an_empty_backlog_ends_the_run_quietly(tmp_path, db):
    llm = FakeLLM()
    handler = _started(db, _handler(tmp_path, db, llm))

    assert _advance(db, handler) is None

    assert (handler._runs, llm.calls, _outcomes(tmp_path)) == ({}, [], [])


# --- recovery and the model call --------------------------------------------


def test_a_missing_anchor_is_recovered_by_listing_from_the_beginning(tmp_path, db):
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
    handler = _started(db, _handler(tmp_path, db, llm), expected_cursor="missing-anchor")

    assert _advance(db, handler) is None

    source = llm.calls[0]["messages"][1]["content"]
    assert source.index("msg-0001|") < source.index("msg-0002|")
    committed = sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE)
    assert (committed.source_offsets_ref, committed.summary_ref) == ("message-0002", "recovered summary")


@pytest.mark.parametrize("finish_reason", ["blacklist", "request_filter", "response_blacklist", "error", "length", ""])
def test_an_unsuccessful_finish_reason_is_never_committed(tmp_path, db, finish_reason):
    append_message(db, 1)
    handler = _started(db, _handler(tmp_path, db, FakeLLM(finish_reason=finish_reason)))

    assert _advance(db, handler) is None

    assert sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE) is None
    outcome = _outcomes(tmp_path)[0]
    assert outcome["status"] == "failed"
    assert "unsuccessful LLM chat summary" in outcome["detail"]


def test_the_fallback_carries_the_remaining_deadline_after_a_primary_timeout(tmp_path, db, monkeypatch):
    append_message(db, 1)
    _use_fallback_model("fallback-model")
    attempts = []

    def chat(**kwargs):
        attempts.append((kwargs["model"], kwargs["timeout_sec"], kwargs["read_timeout_sec"], kwargs["deadline"]))
        if kwargs["model"] == "test-model":
            raise TimeoutError("primary timed out")
        return {"content": "summary", "finish_reason": "stop"}, {}

    handler = _started(db, _handler(tmp_path, db, llm_double(chat)))

    assert _advance(db, handler) is None

    assert [(model, read) for model, _, read, _ in attempts] == [("test-model", 300), ("fallback-model", 300)]
    # Each attempt is capped by the shared wall-clock deadline (read window + margin).
    assert all(deadline is not None and 0 < timeout <= 360 for _, timeout, _, deadline in attempts)
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE).summary_ref
        == "summary"
    )


def test_the_fallback_commits_after_an_invalid_primary_response_and_both_attempts_are_counted(
    tmp_path, db, monkeypatch
):
    append_message(db, 1)
    _use_fallback_model("fallback-model")
    attempts = []

    def chat(**kwargs):
        attempts.append(kwargs["model"])
        if kwargs["model"] == "test-model":
            return {"content": "partial", "finish_reason": "length"}, {"prompt_tokens": 123}
        return {"content": "fallback summary", "finish_reason": "stop"}, {"prompt_tokens": 5}

    handler = _started(db, _handler(tmp_path, db, llm_double(chat)))

    assert _advance(db, handler) is None

    assert attempts == ["test-model", "fallback-model"]
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE).summary_ref
        == "fallback summary"
    )
    # No usage event is emitted anywhere: the totals ride on the outcome event,
    # and a rejected attempt's spend counts too.
    outcome = _outcomes(tmp_path)[0]
    assert (outcome["prompt_tokens"], outcome["batches"]) == (128, 1)
    assert (
        list_runtime_events(
            log_stream="events", event_type="llm_usage", audit_event_repository=sql_audit_event_repository()
        )
        == []
    )


# --- the commit -------------------------------------------------------------


def test_a_summary_is_committed_against_its_expected_cursor(tmp_path, db):
    for index in (1, 2):
        append_message(db, index)
    handler = _started(db, _handler(tmp_path, db, FakeLLM(content="fresh summary")))

    assert _advance(db, handler) is None

    committed = sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE)
    assert (committed.source_offsets_ref, committed.summary_ref) == ("message-0002", "fresh summary")
    outcome = _outcomes(tmp_path)[0]
    assert (outcome["status"], outcome["source_offsets_ref"], outcome["batches"]) == ("completed", "message-0002", 1)
    assert (handler._runs, outcome["prompt_tokens"], outcome["completion_tokens"]) == ({}, 10, 4)


def test_a_conflicting_write_refuses_the_commit_and_drops_the_run_as_stale(tmp_path, db):
    append_message(db, 1)

    def chat(**kwargs):
        # A writer outside this thread commits while we wait on the model.
        sql_conversation_repository(db.store.engine).upsert_context_summary(
            ContextSummary(
                owner_id=CONSOLIDATION_SCOPE.owner_id,
                agent_id=CONSOLIDATION_SCOPE.agent_id,
                chat_id=CONSOLIDATION_SCOPE.chat_id,
                source_offsets_ref="message-0009",
                summary_ref="someone else's summary",
            )
        )
        return {"content": "ours", "finish_reason": "stop"}, {}

    handler = _started(db, _handler(tmp_path, db, llm_double(chat)))

    assert _advance(db, handler) is None

    stored = sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE)
    assert (stored.source_offsets_ref, stored.summary_ref) == ("message-0009", "someone else's summary")
    assert handler._runs == {}
    assert _outcomes(tmp_path)[0]["status"] == "stale_cursor"


def test_a_cursor_that_moved_before_the_run_reached_the_model_is_stale(tmp_path, db):
    append_message(db, 1)
    sql_conversation_repository(db.store.engine).upsert_context_summary(
        ContextSummary(
            owner_id=CONSOLIDATION_SCOPE.owner_id,
            agent_id=CONSOLIDATION_SCOPE.agent_id,
            chat_id=CONSOLIDATION_SCOPE.chat_id,
            source_offsets_ref="message-0001",
            summary_ref="already summarized",
        )
    )
    llm = FakeLLM()
    handler = _started(db, _handler(tmp_path, db, llm), expected_cursor="")

    assert _advance(db, handler) is None

    assert (llm.calls, handler._runs) == ([], {})
    assert _outcomes(tmp_path)[0]["status"] == "stale_cursor"
    assert (
        sql_conversation_repository(db.store.engine).get_latest_context_summary(CONSOLIDATION_SCOPE).summary_ref
        == "already summarized"
    )


# --- the validators ---------------------------------------------------------

_SAVE_CALL = [{"function": {"name": "memory_save", "arguments": "{}"}}]


@pytest.mark.parametrize(
    ("validator_name", "response", "expected"),
    [
        # The committed-verbatim summary requires an explicit successful
        # finish_reason; distillation content is only loop control.
        ("summary", {"content": "summary", "finish_reason": "end_turn"}, None),
        ("summary", {"content": "summary", "finish_reason": None}, "response_rejected"),
        ("distillation", {"content": "Saved the fact.", "tool_calls": [], "finish_reason": None}, None),
        ("distillation", {"content": "", "tool_calls": _SAVE_CALL, "finish_reason": "length"}, None),
        ("summary", {"content": "", "finish_reason": None}, "incomplete_response"),
        ("distillation", {"content": "", "tool_calls": [], "finish_reason": None}, "incomplete_response"),
        ("summary", {"content": "partial", "finish_reason": "length"}, "response_rejected"),
        ("distillation", {"content": "partial", "tool_calls": [], "finish_reason": "length"}, "response_rejected"),
    ],
)
def test_background_response_validators_return_typed_outcomes(validator_name, response, expected):
    from background_memory.handlers.distillation import _distillation_response_error

    validators = {
        "summary": ConsolidationHandler._summary_response_error,
        "distillation": _distillation_response_error,
    }

    outcome = validators[validator_name](response)

    assert (outcome.value if outcome is not None else None) == expected


def test_a_run_spanning_several_deliveries_stays_one_operation(tmp_path, db, monkeypatch):
    """Without this the loop mints per re-delivery and one run reads as N operations."""
    append_message(db, 1, content="A" * 8_000 + "MESSAGE-END")
    monkeypatch.setattr(consolidation, "CONTEXT_CONSOLIDATION_INPUT_MAX_BYTES", 2_048)
    handler = _started(db, _handler(tmp_path, db, FakeLLM()))
    trace_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    continuations = []
    with trace_scope(trace_id):
        while (follow_up := _advance(db, handler)) is not None:
            continuations.append(follow_up.event.trace_id)

    assert len(continuations) > 1, "the run has to span several deliveries for this to say anything"
    assert set(continuations) == {trace_id}
