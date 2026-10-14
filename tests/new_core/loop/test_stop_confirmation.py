from new_core.llm.types import LLMMessage
from new_core.loop import nodes
from tests.helpers.shared import FakeCancellation, make_tool_call
from tests.new_core.loop._fakes import FakeConfirmations, FakeEvents, FakeLLM, make_ctx, make_state


def test_stop_after_finalization_suppresses_the_answer_at_the_moved_emit_node() -> None:
    events = FakeEvents()
    ctx = make_ctx(llm=FakeLLM([]), events=events, cancellation=FakeCancellation(True))
    state = make_state(ctx)
    state.tool_ctx = object()
    state.final_content = "late answer"

    nodes.emit(state, ctx)

    assert len(events.result_calls) == 1
    assert events.result_calls[0]["text"] is None


def test_stop_after_routing_does_not_open_a_confirmation() -> None:
    confirmations = FakeConfirmations()
    ctx = make_ctx(llm=FakeLLM([]), confirmations=confirmations, cancellation=FakeCancellation(True))
    state = make_state(ctx)
    state.last_resp = LLMMessage(tool_calls=[make_tool_call("data_write")])

    nodes.confirm(state, ctx)

    assert confirmations.rounds == []
