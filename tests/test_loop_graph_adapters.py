"""Task 7.4: each adapter owns its implementation and satisfies its interface ABC.

The adapters no longer resolve helpers through a loop module at call time — each owns
its body (or an object it constructed), so these tests assert behavior at the adapter
boundary, which is also the seam other suites patch.
"""

from __future__ import annotations

import itertools
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import ouroboros.agent.loop_adapters as loop_adapters_mod
from new_core.llm.types import LLMMessage
from new_core.loop.interfaces import (
    EventSink,
    LLMProvider,
    LoopOverrides,
    OwnerInbox,
    ToolRunner,
    TranscriptCompactor,
)
from new_core.loop.transcript_merge import append_or_merge_user_message
from new_core.tools.catalog import ToolCatalog
from ouroboros.agent.loop_adapters import (
    EventSinkAdapter,
    LLMProviderAdapter,
    OwnerInboxAdapter,
    ToolRunnerAdapter,
    TranscriptCompactorAdapter,
    build_tool_call_executor,
)
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger
from ouroboros.agent.tool_receipts import ToolResultReplacementState
from tests._shared import make_catalog


def _ctx(**over):
    base = {
        "_pending_compaction": None,
        # Real ToolContext defaults this to None; drain_overrides reads it unconditionally.
        "tool_surface": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _events():
    """A no-op sink for the calls that only have to carry one across."""
    from tests._shared import collecting_event_sink

    return collecting_event_sink()


def _llm_provider(tmp_path, **over):
    base = {
        "llm": MagicMock(),
        "drive_logs": tmp_path,
        "task_id": "t",
        "task_type": "task",
        "event_queue": None,
        "max_retries": 3,
        "fallback_model": "",
        "fallback_use_local": False,
    }
    base.update(over)
    return LLMProviderAdapter(**base)


def _tool_runner(tmp_path=None, *, drive_root=None, repeat_guard=None, catalog=None, events=None):
    import pathlib

    return ToolRunnerAdapter(
        executor=build_tool_call_executor(
            catalog=catalog if catalog is not None else make_catalog([]),
            drive_logs=tmp_path if tmp_path is not None else pathlib.Path("unused") / "logs",
            events=events if events is not None else _events(),
            task_ledger=TaskLedger(),
            repeat_guard=repeat_guard or RepeatToolGuard(),
        ),
        drive_root=drive_root,
    )


# --- ABC satisfaction -------------------------------------------------------


def test_adapters_are_interface_instances(tmp_path):
    assert isinstance(_llm_provider(tmp_path), LLMProvider)
    assert isinstance(
        _tool_runner(tmp_path),
        ToolRunner,
    )
    assert isinstance(
        TranscriptCompactorAdapter(drive_root=None, task_id="t", task_ledger=TaskLedger()), TranscriptCompactor
    )
    assert isinstance(
        OwnerInboxAdapter(incoming_messages=queue.Queue(), drive_root=None, task_id="t", event_queue=None),
        OwnerInbox,
    )
    assert isinstance(
        EventSinkAdapter(emit_progress=lambda _t: None, event_queue=None, task_id="t", drive_logs=tmp_path),
        EventSink,
    )


# --- LLMProviderAdapter -----------------------------------------------------


def test_llm_call_delegates_with_use_local(tmp_path):
    adapter = _llm_provider(tmp_path)
    usage: dict = {}
    with patch.object(loop_adapters_mod, "call_llm_with_retry", return_value="resp") as m:
        resp = adapter.call(
            messages=[], model="m", tools=["s"], effort="high", round_idx=2, use_local=True, accumulated_usage=usage
        )
    assert resp == "resp"
    args, kwargs = m.call_args
    assert args[2] == "m" and args[3] == ["s"] and args[8] == 2  # model, tools, round_idx
    assert kwargs["use_local"] is True


def test_reset_circuits_includes_fallback(tmp_path):
    adapter = _llm_provider(tmp_path, fallback_model="fb", fallback_use_local=True)
    with patch("ouroboros.llm.circuit_breaker.reset_provider_circuits_for_models") as m:
        adapter.reset_circuits("m1", False)
    m.assert_called_once_with([("m1", False), ("fb", True)])


def test_reset_circuits_includes_vision_model(tmp_path):
    adapter = _llm_provider(tmp_path, fallback_model="fb", fallback_use_local=True)
    fake = SimpleNamespace(OUROBOROS_MODEL_VISION="cloudru::Qwen-VL")
    with patch("ouroboros.storage.settings.store.load_settings", return_value=fake):
        with patch("ouroboros.llm.circuit_breaker.reset_provider_circuits_for_models") as m:
            adapter.reset_circuits("m1", False)
    m.assert_called_once_with([("m1", False), ("fb", True), ("cloudru::Qwen-VL", False)])


def test_repair_language_repairs_via_one_extra_llm_call(tmp_path):
    adapter = _llm_provider(tmp_path)
    # First check sees a violation in the original, second sees none in the repair.
    with (
        patch.object(loop_adapters_mod, "find_language_violation", side_effect=[object(), None]),
        patch.object(loop_adapters_mod, "call_llm_with_retry", return_value=LLMMessage(content="clean")) as m,
    ):
        out = adapter.repair_language(
            "dirty", messages=[], model="m", effort="low", use_local=False, round_idx=1, accumulated_usage={}
        )
    assert out == "clean"
    assert m.call_args.args[2] == "m"  # the active model, positional as call_llm_with_retry takes it


def test_repair_language_returns_original_when_clean(tmp_path):
    adapter = _llm_provider(tmp_path)
    with (
        patch.object(loop_adapters_mod, "find_language_violation", return_value=None),
        patch.object(loop_adapters_mod, "call_llm_with_retry") as m,
    ):
        out = adapter.repair_language(
            "ok", messages=[], model="m", effort="low", use_local=False, round_idx=1, accumulated_usage={}
        )
    assert out == "ok"
    m.assert_not_called()


# --- ToolRunnerAdapter ------------------------------------------------------


def test_tool_run_delegates(tmp_path):
    ctx = _ctx()
    events = _events()
    adapter = _tool_runner(tmp_path, events=events)
    with patch.object(adapter._executor, "execute", return_value=["raw"]) as m:
        results = adapter.handle_tool_calls(tool_calls=["c"], active_tool_names={"x"}, tool_ctx=ctx, task_id="t42")
    args, kwargs = m.call_args
    assert args[0] == ["c"]
    assert kwargs["task_id"] == "t42"  # the run's task id, from RunState rather than adapter state
    assert kwargs["ctx"] is ctx  # this task's context, passed explicitly
    assert kwargs["active_tool_names"] == {"x"}
    # the stable collaborators are constructor wiring on the executor, not per-call arguments
    assert isinstance(adapter._executor.catalog, ToolCatalog)  # the catalog, not a registry
    assert adapter._executor._events is events  # the run's one sink, wired rather than threaded
    # the repeat guard has one owner — the executor; the adapter keeps no second reference
    assert not hasattr(adapter, "_repeat_guard")
    # execution only: the raw results come back for the node to hand on
    assert results == ["raw"]


def test_the_tool_runner_takes_its_executor_as_a_dependency(tmp_path):
    """The executor is injected, not assembled in ``__init__``: the caller picks the wiring."""
    executor = build_tool_call_executor(catalog=make_catalog([]), drive_logs=tmp_path / "logs", events=_events())
    adapter = ToolRunnerAdapter(executor=executor, drive_root=None)
    assert adapter._executor is executor
    # and the catalog comes with it — the adapter is not handed a second reference
    assert adapter._executor.catalog is executor.catalog


def test_tool_result_processing_delegates(tmp_path):
    ctx = _ctx()
    adapter = _tool_runner(tmp_path)
    messages: list[dict] = []
    trace = {"tool_calls": []}
    with patch.object(adapter._executor, "process_results", return_value=2) as m:
        errors = adapter.process_tool_results(results=["raw"], messages=messages, trace=trace, tool_ctx=ctx)
    args, kwargs = m.call_args
    assert args[0] == ["raw"] and kwargs["messages"] is messages and kwargs["trace"] is trace
    assert kwargs["ctx"] is ctx
    # the ledger is written by the recording half — it lives on the executor, wired once
    assert adapter._executor._task_ledger is not None
    assert errors == 2


def test_the_tool_runner_holds_no_task_id(tmp_path):
    """The task id is an argument off ``RunState``, not adapter state: two ids, one adapter."""
    adapter = _tool_runner(tmp_path)
    ctx = _ctx()
    with patch.object(adapter._executor, "execute", return_value=[]) as m:
        adapter.handle_tool_calls(tool_calls=[], active_tool_names=None, tool_ctx=ctx, task_id="a")
        adapter.handle_tool_calls(tool_calls=[], active_tool_names=None, tool_ctx=ctx, task_id="b")
    assert [call.kwargs["task_id"] for call in m.call_args_list] == ["a", "b"]
    assert not [v for v in vars(adapter).values() if v in ("a", "b")]


def test_the_tool_runner_holds_no_context_between_rounds():
    """The context is an argument, not adapter state: two rounds, two contexts."""
    adapter = _tool_runner()
    first, second = _ctx(_pending_compaction=1), _ctx(_pending_compaction=2)

    assert adapter.drain_overrides(first).pending_compaction == 1
    assert adapter.drain_overrides(second).pending_compaction == 2
    assert not [v for v in vars(adapter).values() if v is first or v is second]


def test_bind_surface_routes_to_the_executors_catalog(monkeypatch):
    """The catalog is the run's, not the graph context's.

    `prepare_tools` moved into `new_core/loop`, so the concrete `ToolCatalog` can no longer
    ride on `GraphContext`; the surface decision reaches ouroboros through this method, and
    the catalog it uses is the one the adapter's executor was built with.
    """
    import ouroboros.agent.task_graph as task_graph_mod

    seen: list = []
    monkeypatch.setattr(
        task_graph_mod,
        "bind_tool_surface",
        lambda catalog, tool_ctx, messages: (seen.append((catalog, tool_ctx, messages)) or ["s1"], {"tool_a"}),
    )
    catalog = make_catalog([])
    adapter = _tool_runner(catalog=catalog)
    ctx = _ctx()
    messages = [{"role": "user", "content": "hi"}]

    assert adapter.bind_surface(ctx, messages) == (["s1"], {"tool_a"})
    assert seen == [(catalog, ctx, messages)]


def test_drain_overrides_reads_and_resets():
    ctx = _ctx(_pending_compaction=15)
    adapter = _tool_runner()
    ov = adapter.drain_overrides(ctx)
    assert ov == LoopOverrides(pending_compaction=15)
    assert ctx._pending_compaction is None


def test_notify_user_input_marks_repeat_guard():
    guard = MagicMock()
    adapter = _tool_runner(repeat_guard=guard)
    adapter.notify_user_input()
    guard.mark_user_input.assert_called_once()


def test_skill_finalization_empty_without_drive_root():
    adapter = _tool_runner()
    assert adapter.skill_finalization_message({"tool_calls": []}) == ""


def test_skill_finalization_reports_unready_self_authored_skill(tmp_path):
    adapter = _tool_runner(drive_root=tmp_path)
    trace = {"tool_calls": [{"tool": "data_write", "args": {"path": "skills/external/foo/skill.md"}}]}
    skill = SimpleNamespace(name="foo", is_self_authored=True)
    readiness = SimpleNamespace(ready=False, blockers=["preflight"])
    with (
        patch("ouroboros.skills.loader.find_skill", return_value=skill),
        patch("ouroboros.skills.readiness.skill_readiness_for_execution", return_value=readiness),
    ):
        message = adapter.skill_finalization_message(trace)
    assert "SKILL_NOT_FINALIZED" in message
    assert "foo" in message


def test_skill_finalization_empty_when_trace_touches_no_skill(tmp_path):
    adapter = _tool_runner(drive_root=tmp_path)
    assert adapter.skill_finalization_message({"tool_calls": [{"tool": "data_read", "args": {}}]}) == ""


# --- TranscriptCompactorAdapter ---------------------------------------------


def test_compact_replaces_in_place_and_returns_usage():
    adapter = TranscriptCompactorAdapter(drive_root=None, task_id="t", task_ledger=TaskLedger())
    messages = [{"role": "user", "content": "a"}]
    new = [{"role": "user", "content": "compacted"}]
    with patch.object(loop_adapters_mod, "compact_tool_history_llm", return_value=(new, {"prompt_tokens": 2})):
        usage = adapter.compact(messages, keep_recent=20)
    assert usage == {"prompt_tokens": 2}
    assert messages == new  # same list object, replaced contents


def test_estimates_measure_the_transcript():
    adapter = TranscriptCompactorAdapter(drive_root=None, task_id="t", task_ledger=TaskLedger())
    messages = [{"role": "user", "content": "abcd"}]
    assert adapter.estimate_chars(messages) == 4
    assert adapter.estimate_tokens(messages) > 0
    assert adapter.estimate_chars([]) == 0


def test_microcompact_respects_token_threshold():
    adapter = TranscriptCompactorAdapter(drive_root=None, task_id="t", task_ledger=TaskLedger())
    calls = [{"id": "c0", "function": {"name": "noop", "arguments": "{}"}}]
    messages = []
    for i in range(6):
        messages.append({"role": "assistant", "content": f"r{i}", "tool_calls": calls})
        messages.append({"role": "tool", "tool_call_id": "c0", "content": "x" * 500})
    before = [dict(m) for m in messages]
    adapter.microcompact(messages, keep_recent_rounds=2, token_threshold=10**9)
    assert messages == before  # under threshold: untouched


def test_microcompact_receipt_memo_is_owned_per_adapter():
    # The receipt memo is the adapter's own per-task state, not carried on
    # ToolContext: one instance across runs, a fresh one per adapter.
    adapter = TranscriptCompactorAdapter(drive_root=None, task_id="t", task_ledger=TaskLedger())
    other = TranscriptCompactorAdapter(drive_root=None, task_id="t2", task_ledger=TaskLedger())
    assert isinstance(adapter._replacement_state, ToolResultReplacementState)
    assert adapter._replacement_state is not other._replacement_state


def test_upsert_ledger_note_appends_only_when_the_ledger_has_content():
    ledger = TaskLedger()
    adapter = TranscriptCompactorAdapter(drive_root=None, task_id="t", task_ledger=ledger)
    messages = [{"role": "user", "content": "hi"}]
    adapter.upsert_ledger_note(messages)  # empty ledger: nothing to note
    assert len(messages) == 1
    ledger.record_tool_result(tool_name="x", args_hash="h", status="ok", failure_class="", short_outcome="done")
    adapter.upsert_ledger_note(messages)
    assert "[SYSTEM RUNTIME NOTE: TOOL OUTCOME LEDGER]" in str(messages[-1]["content"])


def _ledger_adapter_with_one_entry():
    ledger = TaskLedger()
    ledger.record_tool_result(tool_name="x", args_hash="h", status="ok", failure_class="", short_outcome="done")
    return TranscriptCompactorAdapter(drive_root=None, task_id="t", task_ledger=ledger), ledger


def test_upsert_ledger_note_never_creates_consecutive_user_turns():
    # `checkpoint` runs before `prepare_transcript`, so the round-15 self-check leaves a
    # user turn as the last message. Appending the note as its own user message there put
    # two user turns back to back, which providers reject outright.
    adapter, _ledger = _ledger_adapter_with_one_entry()
    messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "user", "content": "[CHECKPOINT 1 - round 15/200]"},
    ]

    adapter.upsert_ledger_note(messages)

    roles = [m["role"] for m in messages]
    assert not any(a == b == "user" for a, b in itertools.pairwise(roles))
    assert "[CHECKPOINT 1 - round 15/200]" in messages[-1]["content"]
    assert "[SYSTEM RUNTIME NOTE: TOOL OUTCOME LEDGER]" in messages[-1]["content"]


def test_upsert_ledger_note_refresh_keeps_text_merged_into_the_note_turn():
    # The note is a delimited block, not a whole message: the skill-finalization reminder
    # merges into the same user turn, and refreshing the note must not carry it off.
    adapter, ledger = _ledger_adapter_with_one_entry()
    messages = [{"role": "user", "content": "do the thing"}]
    adapter.upsert_ledger_note(messages)
    append_or_merge_user_message(messages, "[SYSTEM REMINDER]\n⚠️ SKILL_NOT_FINALIZED: foo")

    ledger.record_tool_result(tool_name="y", args_hash="h2", status="error", failure_class="", short_outcome="bad")
    adapter.upsert_ledger_note(messages)

    merged = "\n".join(str(m.get("content") or "") for m in messages)
    assert "⚠️ SKILL_NOT_FINALIZED: foo" in merged
    assert merged.count("[SYSTEM RUNTIME NOTE: TOOL OUTCOME LEDGER]") == 1
    assert "y" in merged  # the refreshed ledger, not the stale one
    # No separator debris accumulates where the block was cut out.
    assert "---\n\n\n" not in merged


def test_upsert_ledger_note_keeps_the_last_good_note_when_render_raises():
    # A render that failed says nothing about whether the outcomes it described are still
    # worth showing; deleting them on the strength of a bug loses the model's only record
    # of what already failed.
    adapter, _ledger = _ledger_adapter_with_one_entry()
    messages = [{"role": "user", "content": "do the thing"}]
    adapter.upsert_ledger_note(messages)
    before = [dict(m) for m in messages]

    with patch.object(adapter._task_ledger, "render", side_effect=RuntimeError("boom")):
        adapter.upsert_ledger_note(messages)

    assert messages == before


def test_seal_marks_one_stable_tool_boundary():
    adapter = TranscriptCompactorAdapter(drive_root=None, task_id="t", task_ledger=TaskLedger())
    msgs = [{"role": "tool", "content": "x" * 10_000, "tool_call_id": f"c{i}"} for i in range(7)]
    adapter.seal(msgs)
    sealed = [m for m in msgs if isinstance(m["content"], list)]
    assert len(sealed) == 1
    assert sealed[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


# --- OwnerInboxAdapter / EventSinkAdapter -----------------------------------


def test_inbox_drain_injects_owner_message():
    q = queue.Queue()
    q.put("hello")
    adapter = OwnerInboxAdapter(incoming_messages=q, drive_root=None, task_id="t", event_queue=None)
    messages: list[dict] = []
    assert adapter.drain(messages, set()) is True
    assert messages == [{"role": "user", "content": "[Message from my human]: hello"}]
    assert adapter.drain(messages, set()) is False  # nothing left to inject


def test_event_sink_progress_and_usage(tmp_path):
    progress: list[str] = []
    adapter = EventSinkAdapter(emit_progress=progress.append, event_queue=None, task_id="t", drive_logs=tmp_path)
    adapter.progress("hello")
    assert progress == ["hello"]
    with patch("ouroboros.agent.loop_adapters.emit_llm_usage_event") as m:
        adapter.usage("light", {"prompt_tokens": 1}, "compaction")
    m.assert_called_once_with(None, "t", "light", {"prompt_tokens": 1}, "compaction")


def test_event_sink_typing_puts_a_typing_start_event_on_the_queue(tmp_path):
    event_queue = queue.Queue()
    adapter = EventSinkAdapter(
        emit_progress=lambda _t: None,
        event_queue=event_queue,
        task_id="t",
        drive_logs=tmp_path,
        task={"id": "t", "chat_id": 7, "source": "dialogs"},
    )
    adapter.typing()
    event = event_queue.get_nowait()
    assert event["type"] == "typing_start"
    assert event["chat_id"] == 7
    assert event["source"] == "dialogs"
    assert event["ts"]


def test_event_sink_typing_skips_chatless_tasks(tmp_path):
    event_queue = queue.Queue()
    adapter = EventSinkAdapter(emit_progress=lambda _t: None, event_queue=event_queue, task_id="t", drive_logs=tmp_path)
    adapter.typing()  # no task/chat bound — must not raise
    assert event_queue.empty()


def test_event_sink_log_puts_a_stamped_payload_on_the_queue(tmp_path):
    """The live-log channel tool execution now depends on: queue in, payload out."""
    event_queue = queue.Queue()
    adapter = EventSinkAdapter(
        emit_progress=lambda _t: None,
        event_queue=event_queue,
        task_id="t-9",
        drive_logs=tmp_path,
    )
    adapter.emit_tool_live_log({"type": "tool_call_started", "tool": "shell"})
    event = event_queue.get_nowait()
    payload = event.get("data", event)
    assert payload["type"] == "tool_call_started"
    assert payload["tool"] == "shell"
    assert payload["task_id"] == "t-9"  # bound to the adapter, not restated by the caller
    assert payload["ts"]


def test_event_sink_log_survives_a_missing_queue(tmp_path):
    adapter = EventSinkAdapter(emit_progress=lambda _t: None, event_queue=None, task_id="t", drive_logs=tmp_path)
    assert adapter.emit_tool_live_log({"type": "tool_call_started"}) is None  # must not raise


def test_event_sink_maybe_checkpoint_injects_on_interval(monkeypatch):
    # The checkpoint progress line is off by default; this test is about the interval
    # and the emission path, so it opts in. The default is covered in test_loop_misc.
    monkeypatch.setenv("OUROBOROS_CHECKPOINT_PROGRESS_ENABLED", "true")
    progress: list[str] = []
    adapter = EventSinkAdapter(emit_progress=progress.append, event_queue=None, task_id="t", drive_logs=None)
    messages: list[dict] = []
    assert adapter.maybe_checkpoint(messages, round_idx=14, max_rounds=200) is False
    assert adapter.maybe_checkpoint(messages, round_idx=15, max_rounds=200) is True
    assert "CHECKPOINT" in messages[0]["content"]
    assert progress


# --- EventSinkAdapter.emit_result (task 6.3) --------------------------------


def _result_adapter(tmp_path, *, pending=None, task=None):
    return EventSinkAdapter(
        emit_progress=lambda _t: None,
        event_queue=None,
        task_id="t",
        drive_logs=tmp_path,
        env=object(),
        llm=object(),
        pending_events=pending if pending is not None else [],
        task=task if task is not None else {"id": "t", "chat_id": 0},
        start_time=1.0,
    )


def test_emit_result_applies_empty_text_fallback_and_delegates(tmp_path):
    adapter = _result_adapter(tmp_path)
    with patch("ouroboros.agent.task_pipeline.emit_task_results") as m:
        adapter.emit_result(text="   ", usage={"prompt_tokens": 1}, trace={"tool_calls": []}, tool_ctx="CTX")
    # positional: env, llm, pending_events, task, text, usage, trace, start_time, drive_logs
    args, kwargs = m.call_args
    assert args[4] == "⚠️ Model returned an empty response. Try rephrasing your request."
    assert args[5] == {"prompt_tokens": 1}
    assert args[6] == {"tool_calls": []}
    assert kwargs["ctx"] == "CTX"


def test_emit_result_passes_through_nonempty_text(tmp_path):
    adapter = _result_adapter(tmp_path)
    with patch("ouroboros.agent.task_pipeline.emit_task_results") as m:
        adapter.emit_result(text="final answer", usage={}, trace={}, tool_ctx=None)
    assert m.call_args.args[4] == "final answer"


def test_emit_result_none_text_falls_back(tmp_path):
    adapter = _result_adapter(tmp_path)
    with patch("ouroboros.agent.task_pipeline.emit_task_results") as m:
        adapter.emit_result(text=None, usage={}, trace={}, tool_ctx=None)
    assert m.call_args.args[4].startswith("⚠️ Model returned an empty response")


def test_effects_abc_rejects_implementation_omitting_emit_result():
    class Incomplete(EventSink):
        def progress(self, text): ...

        def typing(self): ...

        def emit_tool_live_log(self, payload): ...

        def usage(self, model, usage, kind): ...

        def maybe_checkpoint(self, messages, *, round_idx, max_rounds):
            return False

        # emit_result intentionally omitted — the class must stay abstract.

    with pytest.raises(TypeError):
        Incomplete()


# --------------------------------------------------------------------------- #
# AefToolTracer: tool-span payload masking toggle (AEF_TOOL_TRACE_PAYLOAD_ENABLED)
# --------------------------------------------------------------------------- #


def _tracer_metadata(redacted=frozenset()):
    from ouroboros.tools.runtime_metadata import ResolvedToolRuntimeMetadata

    return ResolvedToolRuntimeMetadata(
        stable_name="demo_tool",
        retrieval_aliases=(),
        result_limit=1000,
        timeout_sec=60,
        parallel_safe=False,
        log_redacted_args=frozenset(redacted),
    )


def test_tool_span_input_is_masked_by_default():
    from ouroboros.agent.loop_adapters import AefToolTracer

    tracer = AefToolTracer()
    masked = tracer._span_input(tool="demo_tool", args={"query": "secret data"}, task_id="t1")

    assert masked == "***"


def test_tool_span_input_passes_through_when_payload_enabled(monkeypatch):
    # The executor hands the tracer args_for_log — already sanitized with the
    # tool's declared redactions — so with the flag on the tracer must pass
    # them through untouched, not re-resolve or re-mask.
    from ouroboros.agent.loop_adapters import AefToolTracer

    monkeypatch.setattr(
        loop_adapters_mod,
        "load_settings",
        lambda: SimpleNamespace(AEF_TOOL_TRACE_PAYLOAD_ENABLED=True),
    )

    args_for_log = {"query": "quarterly report", "password": "[OMITTED: redacted argument]"}
    payload = AefToolTracer()._span_input(tool="demo_tool", args=args_for_log, task_id="t1")

    assert payload == {"tool": "demo_tool", "arguments": args_for_log, "task_id": "t1"}


def test_tool_span_input_fails_closed_to_mask_on_error(monkeypatch):
    from ouroboros.agent.loop_adapters import AefToolTracer

    monkeypatch.setattr(
        loop_adapters_mod,
        "load_settings",
        lambda: SimpleNamespace(AEF_TOOL_TRACE_PAYLOAD_ENABLED=True),
    )

    def _boom(payload):
        raise RuntimeError("not json-safe")

    monkeypatch.setattr(loop_adapters_mod.aef_tracing, "make_aef_json_safe", _boom)

    assert AefToolTracer()._span_input(tool="demo_tool", args={"q": "x"}, task_id="t1") == "***"


def test_executor_hands_the_tracer_sanitized_args(monkeypatch, tmp_path):
    # The pin behind the pass-through contract above: what reaches the span IS
    # the sanitized view (declared redactions hidden, embedded tokens cut) —
    # the executor sanitizes once and the tracer shows exactly that.
    import json as _json

    import ouroboros.agent.loop_tool_execution as lte
    from new_core.tools.context import ToolContext
    from new_core.tools.entry import ToolEntry
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from tests._shared import make_catalog, static_tool_provider

    monkeypatch.setattr(
        lte,
        "resolve_tool_runtime_metadata",
        lambda name, catalog=None: _tracer_metadata({"password"}),
    )

    captured = {}

    class _SpyTracer:
        def span(self, *, tool, args, task_id):
            captured["tool"] = tool
            captured["args"] = args
            from contextlib import nullcontext

            return nullcontext()

        def add_result(self, span, *, result, is_error):
            return None

    entry = ToolEntry(
        "demo_tool",
        {"name": "demo_tool", "description": "d", "parameters": {"type": "object", "properties": {}}},
        lambda ctx, **kw: "ok",
    )
    executor = build_tool_call_executor(
        catalog=make_catalog([static_tool_provider(entry)]),
        drive_logs=tmp_path / "logs",
        events=MagicMock(),
    )
    executor._tracer = _SpyTracer()

    call = SimpleNamespace(
        id="c1",
        function=SimpleNamespace(
            name="demo_tool",
            arguments=_json.dumps(
                {
                    "query": "quarterly report",
                    "password": "hunter2",
                    "note": "auth via sk-ant-abcdefghijklmnopqrstuvwxyz012345 key",
                }
            ),
        ),
    )
    executor.execute([call], ctx=ToolContext(repo_dir=tmp_path, drive_root=tmp_path), task_id="t1")

    assert captured["tool"] == "demo_tool"
    assert captured["args"]["query"] == "quarterly report"
    assert captured["args"]["password"] == "[OMITTED: redacted argument]"
    assert "sk-ant-" not in captured["args"]["note"]
    assert "***REDACTED***" in captured["args"]["note"]


def test_tool_span_result_stays_masked_even_when_payload_enabled(monkeypatch):
    from ouroboros.agent.loop_adapters import AefToolTracer

    monkeypatch.setattr(
        loop_adapters_mod,
        "load_settings",
        lambda: SimpleNamespace(AEF_TOOL_TRACE_PAYLOAD_ENABLED=True),
    )

    captured = {}

    class _Span:
        def add_tool_result(self, *, output):
            captured["output"] = output

    AefToolTracer().add_result(_Span(), result="tool output with PII", is_error=False)

    assert captured["output"] == "***"


def test_tool_tracer_real_settings_path(tmp_path):
    # Real settings read: default flag=False -> hard mask; flag on -> the
    # given payload survives. The public span() stays a usable context manager.
    from ouroboros.agent.loop_adapters import AefToolTracer, build_tool_call_executor
    from ouroboros.storage.settings.schema import AppSettings
    from ouroboros.storage.settings.store import reset_memory_settings_for_tests, save_settings

    executor = build_tool_call_executor(
        catalog=make_catalog(),
        drive_logs=tmp_path / "logs",
        events=MagicMock(),
    )
    assert isinstance(executor._tracer, AefToolTracer)

    tracer = AefToolTracer()
    try:
        assert tracer._span_input(tool="demo_tool", args={"query": "x"}, task_id="t") == "***"

        save_settings(AppSettings(AEF_TOOL_TRACE_PAYLOAD_ENABLED=True))
        payload = tracer._span_input(tool="demo_tool", args={"query": "x"}, task_id="t")
        assert payload["arguments"] == {"query": "x"}

        with tracer.span(tool="demo_tool", args={"query": "x"}, task_id="t"):
            pass
    finally:
        reset_memory_settings_for_tests()
