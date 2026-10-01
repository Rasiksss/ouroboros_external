"""Task 7.2: node/router behavior driven through the built graph with fake interfaces."""

from __future__ import annotations

from new_core.loop import LoopOverrides, build_loop_graph, max_steps_for
from new_core.loop.routers import route_after_finalize, route_guard
from tests._shared import FakeCancellation
from tests.new_core.loop._fakes import (
    FakeEvents,
    FakeInbox,
    FakeLLM,
    FakeResp,
    FakeToolCall,
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
    llm = FakeLLM([FakeResp(tool_calls=[FakeToolCall()]), FakeResp(content="wrapped up")])
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
    llm = FakeLLM([FakeResp(content="never asked for")])
    ctx = make_ctx(llm=llm, cancellation=FakeCancellation(True))
    state = _run(ctx, make_state(ctx))
    assert llm.calls == []
    assert state.final_content is None


def test_a_run_stopped_mid_flight_ends_at_the_next_round_boundary() -> None:
    cancellation = FakeCancellation()
    llm = FakeLLM([FakeResp(tool_calls=[FakeToolCall()]), FakeResp(content="too late")])
    ctx = make_ctx(llm=llm, cancellation=cancellation)
    state = make_state(ctx)
    original_call = llm.call

    def call_then_stop(**kwargs):
        # The signal arrives while round 1 is in flight; the guard sees it before round 2.
        cancellation.stopped = True
        return original_call(**kwargs)

    llm.call = call_then_stop  # type: ignore[method-assign]
    _run(ctx, state)
    assert len(llm.calls) == 1
    assert state.final_content is None


# --- llm_call routing -------------------------------------------------------


def test_plain_text_routes_to_finalize_and_ends() -> None:
    llm = FakeLLM([FakeResp(content="final answer")])
    ctx = make_ctx(llm=llm)
    state = _run(ctx, make_state(ctx))
    assert state.final_content == "final answer"
    assert llm.repair_calls == ["final answer"]


def test_typing_signals_before_every_llm_request() -> None:
    events = FakeEvents()
    llm = FakeLLM([None, FakeResp(content="recovered")])
    ctx = make_ctx(llm=llm, events=events, config=make_config(fallback_model="fb"))
    _run(ctx, make_state(ctx))
    # Primary call comes through llm_call, the fallback through recover.
    assert len(llm.calls) == 2
    assert events.typing_calls == 2


def test_tool_calls_route_to_exec_then_back_to_guard() -> None:
    tools = FakeTools()
    llm = FakeLLM([FakeResp(content="thinking", tool_calls=[FakeToolCall(id="a")]), FakeResp(content="done")])
    ctx = make_ctx(llm=llm, tools=tools)
    state = _run(ctx, make_state(ctx))
    assert state.final_content == "done"
    assert len(tools.run_calls) == 1
    # the node carries the execution results into the recording half, unchanged
    assert tools.processed == [[{"tool_call_id": "a"}]]
    # assistant turn + tool result appended during exec_tools
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in state.messages)
    assert any(m.get("role") == "tool" for m in state.messages)


# --- override drain → apply timing -----------------------------------------


def test_tool_requested_tool_reaches_the_next_round_request_once() -> None:
    schema = object()  # opaque to this layer
    tools = FakeTools(overrides=LoopOverrides(enable_tools=[("shell_exec", schema)]))
    llm = FakeLLM(
        [
            FakeResp(tool_calls=[FakeToolCall()]),
            FakeResp(tool_calls=[FakeToolCall()]),
            FakeResp(content="done"),
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
    llm = FakeLLM([FakeResp(tool_calls=[FakeToolCall()]), FakeResp(content="done")])
    ctx = make_ctx(llm=llm, tools=tools)
    state = make_state(ctx)
    state.active_tool_names.add("shell_exec")
    _run(ctx, state)

    assert state.tool_schemas == []
    assert state.active_tool_names == {"shell_exec"}


# --- recover-once -----------------------------------------------------------


def test_recover_uses_fallback_then_finalizes() -> None:
    llm = FakeLLM([None, FakeResp(content="recovered")])
    ctx = make_ctx(llm=llm, config=make_config(fallback_model="fb"))
    state = _run(ctx, make_state(ctx))
    assert state.final_content == "recovered"
    assert [c["model"] for c in llm.calls] == ["m1", "fb"]  # llm_call not re-entered


def test_recover_without_fallback_ends_with_failure_message() -> None:
    llm = FakeLLM([None])
    ctx = make_ctx(llm=llm, config=make_config(fallback_model=""))
    state = _run(ctx, make_state(ctx))
    assert "No viable fallback model configured" in state.final_content
    assert len(llm.calls) == 1


def test_recover_exhausted_fallback_ends_with_all_down_message() -> None:
    llm = FakeLLM([None, None])
    ctx = make_ctx(llm=llm, config=make_config(fallback_model="fb"))
    state = _run(ctx, make_state(ctx))
    assert "All models are down" in state.final_content
    assert [c["model"] for c in llm.calls] == ["m1", "fb"]


# --- finalize gate vs END ---------------------------------------------------


def test_finalize_skill_gate_reinjects_then_ends() -> None:
    tools = FakeTools(skill_msg="⚠️ SKILL_NOT_FINALIZED: fix it")
    events = FakeEvents()
    llm = FakeLLM([FakeResp(content="a"), FakeResp(content="b")])
    ctx = make_ctx(llm=llm, tools=tools, events=events)
    state = _run(ctx, make_state(ctx))
    # First finalize injects the reminder and loops; second finalize ends.
    assert state.final_content == "b"
    assert state.skill_finalization_injected is True
    assert any("[SYSTEM REMINDER]" in str(m.get("content") or "") for m in state.messages)
    assert "⚠️ SKILL_NOT_FINALIZED: fix it" in events.progress_calls


def test_finalize_router_direct() -> None:
    ctx = make_ctx(llm=FakeLLM([]))
    state = make_state(ctx)
    state.final_content = None
    assert route_after_finalize(state, ctx) == "guard"
    state.final_content = "answer"
    from new_core.graph import END

    assert route_after_finalize(state, ctx) is END


# --- drain_inbox side effects ----------------------------------------------


def test_owner_input_resets_circuits_and_notifies() -> None:
    tools = FakeTools()
    inbox = FakeInbox(injected=True)
    llm = FakeLLM([FakeResp(content="done")])
    ctx = make_ctx(llm=llm, tools=tools, inbox=inbox)
    _run(ctx, make_state(ctx))
    assert llm.reset_calls == [("m1", False)]
    assert tools.notify_calls == 1


def test_no_owner_input_no_reset() -> None:
    tools = FakeTools()
    inbox = FakeInbox(injected=False)
    llm = FakeLLM([FakeResp(content="done")])
    ctx = make_ctx(llm=llm, tools=tools, inbox=inbox)
    _run(ctx, make_state(ctx))
    assert llm.reset_calls == []
    assert tools.notify_calls == 0
