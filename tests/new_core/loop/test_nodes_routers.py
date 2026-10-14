"""Task 7.2: node/router behavior driven through the built graph with fake interfaces."""

from __future__ import annotations

import pytest

from new_core.graph import GraphExecutionError
from new_core.llm.types import LLMFailure, LLMMessage
from new_core.loop import (
    ConfirmationRequest,
    LoopOverrides,
    build_loop_graph,
    is_token_overflow,
    max_steps_for,
    nodes,
)
from new_core.loop._helpers import MODEL_UNAVAILABLE_MESSAGE
from new_core.loop.routers import route_after_finalize, route_after_resume, route_guard
from tests.helpers.shared import FakeCancellation, make_tool_call
from tests.new_core.loop._fakes import (
    FakeConfirmations,
    FakeContextAssembler,
    FakeEvents,
    FakeInbox,
    FakeLLM,
    FakeTools,
    make_config,
    make_ctx,
    make_state,
)


def _run(ctx, state):
    build_loop_graph().invoke(state, ctx, max_steps=max_steps_for(ctx.config.max_rounds))
    return state


# --- guard / round-limit routing -------------------------------------------


def test_guard_router_stays_in_loop_under_limit() -> None:
    state = make_state(make_ctx(llm=FakeLLM([])))
    state.round_idx = 5
    assert route_guard(state, make_ctx(llm=FakeLLM([]), config=make_config(max_rounds=10))) == "apply_overrides"


def test_guard_router_exits_over_limit() -> None:
    ctx = make_ctx(llm=FakeLLM([]), config=make_config(max_rounds=3))
    state = make_state(ctx)
    state.round_idx = 4
    assert route_guard(state, ctx) == "round_limit_final"


def test_round_limit_final_runs_one_wrapup_call_without_tools() -> None:
    llm = FakeLLM([LLMMessage(tool_calls=[make_tool_call("noop")]), LLMMessage(content="wrapped up")])
    ctx = make_ctx(llm=llm, config=make_config(max_rounds=1))
    state = _run(ctx, make_state(ctx))
    # Round 1 executes tools; round 2 trips the limit → round_limit_final.
    assert state.final_content == "wrapped up"
    assert llm.calls[-1]["tools"] is None
    assert any("[ROUND_LIMIT]" in str(m.get("content") or "") for m in state.messages)


# --- cooperative stop -------------------------------------------------------


def test_guard_router_without_a_stop_signal_is_unchanged() -> None:
    ctx = make_ctx(llm=FakeLLM([]), config=make_config(max_rounds=10))
    state = make_state(ctx)
    state.round_idx = 5
    assert ctx.cancellation.is_stopped() is False
    assert route_guard(state, ctx) == "apply_overrides"


def test_guard_router_stops_the_run_when_the_signal_is_set() -> None:
    ctx = make_ctx(llm=FakeLLM([]), config=make_config(max_rounds=10), cancellation=FakeCancellation(True))
    state = make_state(ctx)
    state.round_idx = 1
    assert route_guard(state, ctx) == ctx.config.terminal


def test_a_stop_beats_the_round_limit_and_skips_the_wrapup() -> None:
    ctx = make_ctx(llm=FakeLLM([]), config=make_config(max_rounds=1), cancellation=FakeCancellation(True))
    state = make_state(ctx)
    state.round_idx = 4
    assert route_guard(state, ctx) == ctx.config.terminal


def test_a_stopped_run_ends_with_no_model_call_and_no_final_content() -> None:
    llm = FakeLLM([LLMMessage(content="never asked for")])
    ctx = make_ctx(llm=llm, cancellation=FakeCancellation(True))
    state = _run(ctx, make_state(ctx))
    assert llm.calls == []
    assert state.final_content is None


def test_a_run_stopped_mid_flight_ends_at_the_next_round_boundary() -> None:
    cancellation = FakeCancellation()
    tools = FakeTools()
    llm = FakeLLM([LLMMessage(tool_calls=[make_tool_call("noop")]), LLMMessage(content="too late")])
    ctx = make_ctx(llm=llm, tools=tools, cancellation=cancellation)
    state = make_state(ctx)
    original_call = llm.call

    def call_then_stop(**kwargs):
        # The signal arrives while round 1 is in flight; the guard sees it before round 2.
        cancellation.stopped = True
        return original_call(**kwargs)

    llm.call = call_then_stop  # type: ignore[method-assign]
    _run(ctx, state)
    assert len(llm.calls) == 1
    assert tools.run_calls == []
    assert state.final_content is None


def test_tool_node_rechecks_a_stop_that_arrived_after_routing() -> None:
    cancellation = FakeCancellation(True)
    tools = FakeTools()
    ctx = make_ctx(llm=FakeLLM([]), tools=tools, cancellation=cancellation)
    state = make_state(ctx)
    state.last_resp = LLMMessage(tool_calls=[make_tool_call("noop")])

    nodes.exec_tools(state, ctx)

    assert tools.run_calls == []


def test_tool_node_discards_results_when_a_stop_arrives_during_execution() -> None:
    cancellation = FakeCancellation()

    class _StopsAfterExecution(FakeTools):
        def handle_tool_calls(self, **kwargs):
            results = super().handle_tool_calls(**kwargs)
            cancellation.request_stop()
            return results

    tools = _StopsAfterExecution()
    ctx = make_ctx(llm=FakeLLM([]), tools=tools, cancellation=cancellation)
    state = make_state(ctx)
    state.last_resp = LLMMessage(tool_calls=[make_tool_call("noop")])

    nodes.exec_tools(state, ctx)

    assert len(tools.run_calls) == 1
    assert tools.processed == []
    assert tools.drained == 0


# --- llm_call routing -------------------------------------------------------


def test_plain_text_routes_to_finalize_and_ends() -> None:
    llm = FakeLLM([LLMMessage(content="final answer")])
    ctx = make_ctx(llm=llm)
    state = _run(ctx, make_state(ctx))
    assert state.final_content == "final answer"
    assert llm.repair_calls == ["final answer"]


def test_typing_signals_before_every_llm_request() -> None:
    events = FakeEvents()
    llm = FakeLLM([LLMFailure("empty response"), LLMMessage(content="recovered")])
    ctx = make_ctx(llm=llm, events=events, config=make_config(fallback_model="fb"))
    _run(ctx, make_state(ctx))
    # Primary call comes through llm_call, the fallback through recover.
    assert len(llm.calls) == 2
    assert events.typing_calls == 2


def test_tool_calls_route_to_exec_then_back_to_guard() -> None:
    tools = FakeTools()
    llm = FakeLLM([LLMMessage(content="thinking", tool_calls=[make_tool_call("noop")]), LLMMessage(content="done")])
    ctx = make_ctx(llm=llm, tools=tools)
    state = _run(ctx, make_state(ctx))
    assert state.final_content == "done"
    assert len(tools.run_calls) == 1
    # the node carries the execution results into the recording half, unchanged
    assert tools.processed == [[{"tool_call_id": "call-0"}]]
    # assistant turn + tool result appended during exec_tools
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in state.messages)
    assert any(m.get("role") == "tool" for m in state.messages)


# --- override drain → apply timing -----------------------------------------


def test_tool_requested_tool_reaches_the_next_round_request_once() -> None:
    schema = object()  # opaque to this layer
    tools = FakeTools(overrides=LoopOverrides(enable_tools=[("shell_exec", schema)]))
    llm = FakeLLM(
        [
            LLMMessage(tool_calls=[make_tool_call("noop")]),
            LLMMessage(tool_calls=[make_tool_call("noop")]),
            LLMMessage(content="done"),
        ]
    )
    ctx = make_ctx(llm=llm, tools=tools)
    state = _run(ctx, make_state(ctx))

    assert llm.calls[0]["tools_snapshot"] == []  # round 1: not yet on the surface
    assert llm.calls[1]["tools_snapshot"] == [schema]  # round 2: carried into the request
    assert llm.calls[2]["tools_snapshot"] == [schema]  # round 3: still there, appended once
    assert state.active_tool_names == {"shell_exec"}
    assert state.tool_schemas == [schema]


def test_enabling_an_already_active_tool_changes_nothing() -> None:
    schema = object()
    tools = FakeTools(overrides=LoopOverrides(enable_tools=[("shell_exec", schema)]))
    llm = FakeLLM([LLMMessage(tool_calls=[make_tool_call("noop")]), LLMMessage(content="done")])
    ctx = make_ctx(llm=llm, tools=tools)
    state = make_state(ctx)
    state.active_tool_names.add("shell_exec")
    _run(ctx, state)

    assert state.tool_schemas == []
    assert state.active_tool_names == {"shell_exec"}


# --- recover-once -----------------------------------------------------------


def test_recover_uses_fallback_then_finalizes() -> None:
    llm = FakeLLM([LLMFailure("empty response"), LLMMessage(content="recovered")])
    ctx = make_ctx(llm=llm, config=make_config(fallback_model="fb"))
    state = _run(ctx, make_state(ctx))
    assert state.final_content == "recovered"
    assert [c["model"] for c in llm.calls] == ["m1", "fb"]  # llm_call not re-entered


def test_recover_without_fallback_ends_with_failure_message() -> None:
    llm = FakeLLM([LLMFailure("empty response")])
    ctx = make_ctx(llm=llm, config=make_config(fallback_model=""))
    state = _run(ctx, make_state(ctx))
    assert "No viable fallback model configured" in state.final_content
    assert len(llm.calls) == 1


def test_recover_exhausted_fallback_ends_with_all_down_message() -> None:
    llm = FakeLLM([LLMFailure("empty response"), LLMFailure("empty response")])
    ctx = make_ctx(llm=llm, config=make_config(fallback_model="fb"))
    state = _run(ctx, make_state(ctx))
    assert "All models are down" in state.final_content
    assert [c["model"] for c in llm.calls] == ["m1", "fb"]


def test_token_overflow_requires_an_explicit_flag():
    assert not is_token_overflow(None)
    assert not is_token_overflow(LLMFailure(detail="context length exceeded"))
    assert is_token_overflow(LLMFailure(detail="любой текст", context_overflow=True))
    failure = LLMFailure("detail", True, "message")
    assert failure.model_unavailable and failure.user_message == "message"
    assert not is_token_overflow(failure)


@pytest.mark.parametrize("round_limit", [False, True])
@pytest.mark.parametrize("result", [LLMMessage(content="recovered"), LLMFailure("network failure")])
def test_overflow_retry_replaces_the_last_result(round_limit, result):
    llm = FakeLLM([LLMFailure("opaque provider error", context_overflow=True), result])
    ctx = make_ctx(llm=llm, config=make_config(max_rounds=0 if round_limit else 2))
    state = _run(ctx, make_state(ctx))
    assert len(llm.calls) == 2
    assert not hasattr(llm, "last_failure")
    if isinstance(result, LLMFailure):
        assert state.last_failure == result and state.last_resp is None
        assert "network failure" in state.final_content
        assert "context length" not in state.final_content
    else:
        assert state.last_resp == result and state.last_failure is None
        assert state.final_content == "recovered"


def test_fallback_failure_replaces_overflow_after_bounded_recovery():
    network = LLMFailure("fallback network failure")
    overflow = LLMFailure("opaque provider error", context_overflow=True)
    llm = FakeLLM([overflow, overflow, network])
    ctx = make_ctx(llm=llm, config=make_config(fallback_model="fb"))
    state = _run(ctx, make_state(ctx))
    assert [c["model"] for c in llm.calls] == ["m1", "m1", "fb"]
    assert state.last_failure == network and state.last_resp is None
    assert "fallback network failure" in state.final_content and "context length" not in state.final_content


def test_round_limit_wrapup_on_a_disabled_model_reports_unavailability() -> None:
    llm = FakeLLM([LLMMessage(tool_calls=[make_tool_call("noop")])], unavailable_models={"m1"})
    ctx = make_ctx(llm=llm, config=make_config(max_rounds=1, fallback_model=""))
    state = make_state(ctx)
    state.round_idx = 4
    _run(ctx, state)
    assert state.final_content == MODEL_UNAVAILABLE_MESSAGE


def test_recover_model_unavailable_without_fallback_reports_unavailability() -> None:
    llm = FakeLLM([], unavailable_models={"m1"})
    ctx = make_ctx(llm=llm, config=make_config(fallback_model=""))
    state = _run(ctx, make_state(ctx))
    assert state.final_content == MODEL_UNAVAILABLE_MESSAGE
    assert len(llm.calls) == 1


def test_recover_model_unavailable_keeps_later_rounds_off_the_disabled_model() -> None:
    llm = FakeLLM(
        [LLMMessage(tool_calls=[make_tool_call("noop")]), LLMMessage(content="done")], unavailable_models={"m1"}
    )
    events = FakeEvents()
    ctx = make_ctx(llm=llm, events=events, config=make_config(fallback_model="fb"))
    state = _run(ctx, make_state(ctx))
    assert state.final_content == "done"
    # Round 2 goes straight to the fallback: the disabled model is probed once, not per round.
    assert [c["model"] for c in llm.calls] == ["m1", "fb", "fb"]
    assert state.active_model == "fb"
    fallback_banners = [p for p in events.progress_calls if "Fallback" in p]
    assert fallback_banners == ["⚡ Fallback: m1 → fb after model temporarily unavailable"]


def test_recover_network_failure_on_fallback_replaces_primary_unavailability() -> None:
    llm = FakeLLM([], unavailable_models={"m1"})
    ctx = make_ctx(llm=llm, config=make_config(fallback_model="fb"))
    state = _run(ctx, make_state(ctx))
    assert state.final_content != MODEL_UNAVAILABLE_MESSAGE
    assert "empty response" in state.final_content
    assert [c["model"] for c in llm.calls] == ["m1", "fb"]


def test_recover_reports_the_latest_failure_when_only_the_fallback_is_disabled() -> None:
    llm = FakeLLM([LLMFailure("empty response")], unavailable_models={"fb"})
    ctx = make_ctx(llm=llm, config=make_config(fallback_model="fb"))
    state = _run(ctx, make_state(ctx))
    assert state.final_content == MODEL_UNAVAILABLE_MESSAGE
    assert [c["model"] for c in llm.calls] == ["m1", "fb"]


# --- finalize gate vs END ---------------------------------------------------


def test_finalize_router_direct() -> None:
    ctx = make_ctx(llm=FakeLLM([]))
    state = make_state(ctx)
    from new_core.graph import END

    assert route_after_finalize(state, ctx) is END


def test_exec_tools_without_a_response_fails_as_invalid_graph_state() -> None:
    ctx = make_ctx(llm=FakeLLM([]))
    state = make_state(ctx)

    with pytest.raises(GraphExecutionError, match="exec_tools reached without tool calls"):
        nodes.exec_tools(state, ctx)


def test_exec_tools_without_tool_calls_fails_as_invalid_graph_state() -> None:
    ctx = make_ctx(llm=FakeLLM([]))
    state = make_state(ctx)
    state.last_resp = LLMMessage(content="plain text", tool_calls=None)

    with pytest.raises(GraphExecutionError, match="exec_tools reached without tool calls"):
        nodes.exec_tools(state, ctx)


def test_finalize_without_a_response_fails_as_invalid_graph_state() -> None:
    ctx = make_ctx(llm=FakeLLM([]))
    state = make_state(ctx)

    with pytest.raises(GraphExecutionError, match="finalize reached without a response"):
        nodes.finalize(state, ctx)


# --- drain_inbox side effects ----------------------------------------------


def test_owner_input_resets_circuits_and_notifies() -> None:
    tools = FakeTools()
    inbox = FakeInbox(injected=True)
    llm = FakeLLM([LLMMessage(content="done")])
    ctx = make_ctx(llm=llm, tools=tools, inbox=inbox)
    _run(ctx, make_state(ctx))
    assert llm.reset_calls == [("m1", False)]
    assert tools.notify_calls == 1


def test_no_owner_input_no_reset() -> None:
    tools = FakeTools()
    inbox = FakeInbox(injected=False)
    llm = FakeLLM([LLMMessage(content="done")])
    ctx = make_ctx(llm=llm, tools=tools, inbox=inbox)
    _run(ctx, make_state(ctx))
    assert llm.reset_calls == []
    assert tools.notify_calls == 0


# --- head-of-graph nodes ----------------------------------------------------


def test_the_transcript_fills_the_list_the_tool_context_was_bound_to() -> None:
    tool_ctx = object()
    assembler = FakeContextAssembler(messages=[{"role": "user", "content": "assembled"}], tool_ctx=tool_ctx)
    ctx = make_ctx(llm=FakeLLM([]), assembler=assembler)
    state = make_state(ctx, messages=[])

    nodes.prepare_context(state, ctx)

    assert state.tool_ctx is tool_ctx
    assert state.messages == [], "prepare_context assembles no transcript"
    assert state.tool_schemas == []
    assert state.active_tool_names == set()
    assert state.round_idx == 0

    nodes.open_transcript(state, ctx)

    assert assembler.calls[0]["messages"] is state.messages
    assert state.messages == [{"role": "user", "content": "assembled"}]


def test_prepare_context_passes_the_run_state_scope_through_unchanged() -> None:
    assembler = FakeContextAssembler()
    ctx = make_ctx(llm=FakeLLM([]), assembler=assembler)
    state = make_state(ctx)

    nodes.prepare_context(state, ctx)

    assert len(assembler.calls) == 1
    call = assembler.calls[0]
    assert call["scope"] is state.scope
    assert call["task"] is state.task
    assert call["task_workspace"] is state.task_workspace


def test_record_task_started_signals_typing_before_opening_the_record() -> None:
    events = FakeEvents()
    assembler = FakeContextAssembler()
    ctx = make_ctx(llm=FakeLLM([]), events=events, assembler=assembler)
    state = make_state(ctx)

    nodes.record_task_started(state, ctx)

    assert events.order == ["typing", "task_started"]
    assert events.typing_calls == 1
    assert events.task_started_calls == 1
    # Opening the record assembles nothing.
    assert assembler.calls == []
    assert state.tool_ctx is None


def _exec_tools_round(*, reasoning_progress: bool):
    ctx = make_ctx(llm=FakeLLM([]), config=make_config(reasoning_progress=reasoning_progress))
    state = make_state(ctx)
    state.last_resp = LLMMessage(content="thinking out loud", tool_calls=[make_tool_call("noop")])

    nodes.exec_tools(state, ctx)

    return ctx, state


def test_exec_tools_publishes_the_model_text_by_default() -> None:
    ctx, _state = _exec_tools_round(reasoning_progress=True)

    assert ctx.events.progress_calls == ["thinking out loud"]


def test_exec_tools_publishes_nothing_when_the_run_forbids_the_narration() -> None:
    ctx, _state = _exec_tools_round(reasoning_progress=False)

    assert ctx.events.progress_calls == []


def test_a_suppressed_narration_still_reaches_the_transcript_and_the_trace() -> None:
    _ctx, state = _exec_tools_round(reasoning_progress=False)

    assert {"role": "assistant", "content": "thinking out loud"} in [
        {"role": message.get("role"), "content": message.get("content")} for message in state.messages
    ]
    assert state.llm_trace["reasoning_notes"] == ["thinking out loud"]


def test_a_forbidden_narration_does_not_stop_the_tools_from_running() -> None:
    ctx, _state = _exec_tools_round(reasoning_progress=False)

    assert len(ctx.tools.run_calls) == 1


def test_progress_that_reports_the_run_survives_the_forbidden_narration() -> None:
    # The fallback notice comes from `recover`, so the gate in `exec_tools` must not reach it.
    llm = FakeLLM([LLMFailure("empty response"), LLMMessage(content="recovered")])
    ctx = make_ctx(llm=llm, config=make_config(fallback_model="fb", reasoning_progress=False))

    _run(ctx, make_state(ctx))

    assert ctx.events.progress_calls == ["⚡ Fallback: m1 → fb after empty response"]


# --- owner confirmation: park, resume, decide -------------------------------


_REQUEST = ConfirmationRequest(
    confirmation_id="c1f2e3d4",
    task_id="t",
    tool="file_write",
    arguments_preview='{"path": "a.txt"}',
    arguments_truncated=False,
    question="Подтвердите действие: file_write [c1f2e3d4]",
)


def _gated_round(*, confirmations: FakeConfirmations, events: FakeEvents | None = None):
    tools = FakeTools()
    llm = FakeLLM([LLMMessage(content="thinking", tool_calls=[make_tool_call("noop")]), LLMMessage(content="done")])
    ctx = make_ctx(llm=llm, tools=tools, events=events or FakeEvents(), confirmations=confirmations)
    return ctx, _run(ctx, make_state(ctx))


def test_a_gated_call_without_a_decision_parks_the_run_before_any_tool_runs() -> None:
    events = FakeEvents()
    confirmations = FakeConfirmations(request=_REQUEST)

    ctx, state = _gated_round(confirmations=confirmations, events=events)

    assert state.suspended is True
    assert ctx.tools.run_calls == []
    assert events.owner_questions == [_REQUEST.question]
    assert state.final_content is None
    assert confirmations.rounds[0]["snapshot"]["round_idx"] == state.round_idx == 1


def test_the_owners_decision_reaches_the_call_it_was_given_for() -> None:
    confirmations = FakeConfirmations(decided={"a": "approved"})

    ctx, state = _gated_round(confirmations=confirmations)

    assert ctx.tools.run_decisions == [{"a": "approved"}]
    # A decision belongs to the round it was asked in; the next round asks again.
    assert state.confirmation_decisions == {}


def test_an_undeliverable_question_is_withdrawn_and_the_round_goes_on() -> None:
    confirmations = FakeConfirmations(request=_REQUEST)

    ctx, state = _gated_round(confirmations=confirmations, events=FakeEvents(question_delivered=False))

    assert confirmations.abandoned == [_REQUEST]
    assert state.suspended is False
    assert len(ctx.tools.run_calls) == 1


def test_a_question_answered_before_it_could_be_withdrawn_keeps_the_run_parked() -> None:
    confirmations = FakeConfirmations(request=_REQUEST, abandon_result=False)

    ctx, state = _gated_round(confirmations=confirmations, events=FakeEvents(question_delivered=False))

    assert state.suspended is True
    assert ctx.tools.run_calls == []


def test_resume_restores_the_snapshot_and_goes_back_to_the_call_that_was_asked_about() -> None:
    parked = make_state(make_ctx(llm=FakeLLM([])))
    parked.round_idx = 3
    parked.active_tool_names.update({"send_file", "web_fetch"})
    ctx = make_ctx(llm=FakeLLM([]), confirmations=FakeConfirmations(snapshot=parked.snapshot()))
    state = make_state(ctx, messages=[])

    nodes.resume(state, ctx)

    assert state.resumed is True
    assert state.round_idx == 3
    assert route_after_resume(state, ctx) == "confirm"
    # Schemas are objects, not data: the snapshot names the surface and the runner rebuilds it.
    assert state.tool_schemas == [f"schema:{name}" for name in sorted(parked.active_tool_names)]
    assert ctx.tools.restored_names is state.active_tool_names, "the surface reads the run's own set"


def test_a_snapshot_that_cannot_be_read_stops_the_run_instead_of_starting_it_over() -> None:
    ctx = make_ctx(llm=FakeLLM([]), confirmations=FakeConfirmations(restore_error=OSError("db is down")))
    state = make_state(ctx)

    with pytest.raises(OSError):
        nodes.resume(state, ctx)

    assert state.resumed is False


def test_resume_on_a_stopped_run_reaches_the_terminal() -> None:
    parked = make_state(make_ctx(llm=FakeLLM([])))
    ctx = make_ctx(
        llm=FakeLLM([]),
        confirmations=FakeConfirmations(snapshot=parked.snapshot()),
        cancellation=FakeCancellation(True),
    )
    state = make_state(ctx)

    nodes.resume(state, ctx)

    assert route_after_resume(state, ctx) == ctx.config.terminal


def test_a_task_that_was_never_parked_assembles_its_transcript() -> None:
    ctx = make_ctx(llm=FakeLLM([]), confirmations=FakeConfirmations())
    state = make_state(ctx)

    nodes.resume(state, ctx)

    assert state.resumed is False
    assert route_after_resume(state, ctx) == "open_transcript"
