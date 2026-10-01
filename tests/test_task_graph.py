"""Tasks 6.2/6.4/6.5/6.6: the ouroboros-side task graph and the shrunk handle_task.

Covers the ``prepare_context`` / ``prepare_tools`` glue nodes, the task-graph
topology and terminal parameterization, a handle_task-level characterization of a
scripted task, and that cleanup + the crash path run on every exit.
"""

from __future__ import annotations

import os
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import ouroboros.agent.core as core
import ouroboros.agent.loop_adapters as loop_adapters_mod
import ouroboros.agent.task_graph as task_graph_mod
from new_core.graph import END
from new_core.llm.types import LLMMessage, ToolCall, ToolCallFunction
from new_core.loop import LoopOverrides, make_run_state
from new_core.loop.nodes import prepare_tools
from new_core.loop.routers import (
    route_after_finalize,
    route_after_recover,
    route_after_round_limit,
)
from new_core.tools.interfaces import NullToolEvents
from ouroboros.agent.task_graph import (
    _make_prepare_context_node,
    build_task_graph,
    build_task_run,
)
from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger
from ouroboros.tools.events import QueueToolEvents
from tests._shared import FakeCancellation
from tests.new_core.loop._fakes import FakeLLM, FakeTools, make_config, make_ctx, make_state

_LOOP_NODES = [
    "guard",
    "apply_overrides",
    "drain_inbox",
    "checkpoint",
    "prepare_transcript",
    "llm_call",
    "recover",
    "exec_tools",
    "finalize",
    "round_limit_final",
]


# --- 6.2 prepare_context / prepare_tools nodes ------------------------------


def _spy_agent(tool_ctx, prepared):
    class SpyAgent:
        def __init__(self):
            self._event_queue = "EQ"

        def _prepare_task_context(self, task, workspace):
            prepared.append((task, workspace))
            return tool_ctx, [{"role": "user", "content": "hi"}], {"cap": 1}

    return SpyAgent()


def test_prepare_context_populates_run_state():
    tool_ctx = SimpleNamespace(events="untouched", task_id="stale", messages=None)
    prepared: list[tuple] = []
    agent = _spy_agent(tool_ctx, prepared)

    ctx = make_ctx(llm=FakeLLM([]), tools=FakeTools())
    state = make_run_state(ctx.config, messages=[], tool_schemas=[], active_tool_names=set())
    state.task = {"id": "task-9"}
    state.task_workspace = "/ws"

    _make_prepare_context_node(agent)(state, ctx)

    assert prepared == [({"id": "task-9"}, "/ws")]
    # outputs written into RunState
    assert state.messages == [{"role": "user", "content": "hi"}]
    assert state.tool_ctx is tool_ctx
    assert state.cap_info == {"cap": 1}
    # context assembly stays clear of tool binding: no surface, no schemas
    assert state.tool_schemas == []
    assert state.active_tool_names == set()


def test_prepare_tools_builds_the_surface_off_this_task_context():
    """The node itself: a loop-layer node reaching the surface through ``ToolRunner``."""
    tool_ctx = SimpleNamespace(events="untouched", task_id="untouched", messages="untouched")
    tools = FakeTools(overrides=LoopOverrides(pending_compaction=9), surface=(["s1"], {"tool_a"}))

    ctx = make_ctx(llm=FakeLLM([]), tools=tools)
    state = make_run_state(ctx.config, messages=[], tool_schemas=[], active_tool_names=set())
    state.task = {"id": "task-9"}
    state.messages = [{"role": "user", "content": "hi"}]
    state.tool_ctx = tool_ctx

    prepare_tools(state, ctx)

    # the surface is derived from *this* task's context, not a previous task's
    assert tools.bind_calls == [(tool_ctx, [{"role": "user", "content": "hi"}])]
    assert state.tool_schemas == ["s1"]
    assert state.active_tool_names == {"tool_a"}
    # The node writes nothing onto the context: id, events channel and transcript are all
    # bound at assembly, so the context is as opaque here as it is to the per-round nodes.
    assert (tool_ctx.events, tool_ctx.task_id, tool_ctx.messages) == ("untouched",) * 3
    # overrides seeded from the same interface, for apply_overrides in round 1
    assert state.pending_overrides.pending_compaction == 9


def test_build_tool_context_binds_the_events_channel(tmp_path):
    # The context leaves _build_tool_context complete: prepare_tools assigns nothing for
    # events, so the channel has to arrive here, bound to the agent's queue and this task.
    agent, _ = _make_agent(tmp_path)
    agent._event_queue = queue.Queue()
    agent._current_task_type = "chat"

    ctx = agent._build_tool_context({"id": "task-42"}, None, root_task_id="task-42")

    assert isinstance(ctx.events, QueueToolEvents)
    assert ctx.events._queue is agent._event_queue
    assert ctx.events._task_id == "task-42"
    assert ctx.events._task_type == "chat"

    ctx.events.lifecycle("skill_exec_finished", {"skill": "alpha"})
    assert agent._event_queue.get_nowait()["task_id"] == "task-42"


def test_build_task_run_binds_the_runs_usage_totals_for_tool_side_model_calls(tmp_path):
    # A tool that calls a model outside the loop (vlm_query) reports through
    # ToolContext.events. Those tokens are the task's spend, so the sink has to accumulate
    # into the very dict `emit_result` publishes and the checkpoint shows the model --
    # otherwise the published usage silently omits it.
    agent, _tool_ctx = _make_agent(tmp_path)

    _graph_ctx, state = build_task_run(
        agent,
        {"id": "t-usage"},
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    ctx = agent._build_tool_context({"id": "t-usage"}, None, root_task_id="t-usage")
    ctx.events.usage("vlm/model", {"prompt_tokens": 8000, "completion_tokens": 500}, source="vlm_query")

    assert state.accumulated_usage["prompt_tokens"] == 8000
    assert state.accumulated_usage["completion_tokens"] == 500


def test_build_task_run_owns_per_task_tool_execution_state(tmp_path):
    # The ledger and the repeat guard live on the adapters, not on ToolContext.
    # The ledger has two users — the tool runner writes it, the compactor renders
    # it — so they must get the very same instance; both objects are per task.
    agent, _tool_ctx = _make_agent(tmp_path)

    graph_ctx, _state = build_task_run(
        agent,
        {"id": "t-ledger"},
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    assert isinstance(graph_ctx.tools._executor._task_ledger, TaskLedger)
    assert graph_ctx.tools._executor._task_ledger is graph_ctx.compactor._task_ledger
    # The guard has a single owner — the executor — so no second reference can drift.
    assert isinstance(graph_ctx.tools._executor._repeat_guard, RepeatToolGuard)
    assert not hasattr(graph_ctx.tools, "_repeat_guard")
    # One sink for the run: tool live logs and the graph's own events go out the same channel.
    assert graph_ctx.tools._executor._events is graph_ctx.events

    other_ctx, _ = build_task_run(
        agent,
        {"id": "t-other"},
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    assert other_ctx.tools._executor._task_ledger is not graph_ctx.tools._executor._task_ledger
    assert other_ctx.tools._executor._repeat_guard is not graph_ctx.tools._executor._repeat_guard


def test_build_task_run_shares_the_one_process_catalog(tmp_path):
    """The catalog is the exception to "per task": one per process, refreshed per task.

    It used to be rebuilt for every task, which re-walked the skills tree and executed
    every `type: tool` module again. Refreshing the shared one buys the same freshness —
    a skill enabled since the last task shows up — without a second catalog existing.
    """
    from new_core.tools.catalog import tool_catalog

    agent, _tool_ctx = _make_agent(tmp_path)

    first, _ = build_task_run(
        agent,
        {"id": "t-1"},
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )
    second, _ = build_task_run(
        agent,
        {"id": "t-2"},
        task_workspace=None,
        start_time=0.0,
        drive_logs=tmp_path,
        cancellation=FakeCancellation(),
    )

    assert first.tools._executor.catalog is tool_catalog
    assert second.tools._executor.catalog is tool_catalog


# --- 6.4 task-graph topology + terminal parameterization -------------------


def test_task_graph_topology():
    graph = build_task_graph(SimpleNamespace())  # glue closures aren't dereferenced at build
    assert graph._entry == "prepare_context"
    assert graph._static_edges["prepare_context"] == "prepare_tools"
    assert graph._static_edges["prepare_tools"] == "guard"
    assert graph._static_edges["emit"] is END
    assert "emit" in graph._nodes
    for name in _LOOP_NODES:
        assert name in graph._nodes, name
    # the three terminal outcomes route via routers (not static edges)
    for terminal_router in ("finalize", "recover", "round_limit_final"):
        assert terminal_router in graph._routers


def test_terminal_routes_to_end_standalone_and_emit_when_embedded():
    # Standalone loop: terminal defaults to END.
    ctx_end = make_ctx(llm=FakeLLM([]))
    s = make_state(ctx_end)
    s.final_content = "done"
    assert route_after_finalize(s, ctx_end) is END
    s.last_resp = None
    assert route_after_recover(s, ctx_end) is END
    assert route_after_round_limit(s, ctx_end) is END

    # Embedded task graph: terminal is the emit node.
    ctx_emit = make_ctx(llm=FakeLLM([]), config=make_config(terminal="emit"))
    s2 = make_state(ctx_emit)
    s2.final_content = "done"
    assert route_after_finalize(s2, ctx_emit) == "emit"
    s2.last_resp = None
    assert route_after_recover(s2, ctx_emit) == "emit"
    assert route_after_round_limit(s2, ctx_emit) == "emit"


# --- 6.5 / 6.6 handle_task characterization + cleanup/crash ----------------


def _make_agent(tmp_path):
    agent = object.__new__(core.OuroborosAgent)
    agent.env = core.Env(repo_dir=tmp_path, drive_root=tmp_path)
    agent._pending_events = []
    agent._event_queue = None
    agent._current_chat_id = 0
    agent._current_task_type = None
    agent._current_task_source = ""
    agent._current_task_id = None
    agent._current_transport = {}
    agent._task_usage = {}
    agent._media_senders = {}
    agent._incoming_messages = queue.Queue()
    agent._busy = False
    agent._last_progress_ts = 0.0
    agent._task_started_ts = 0.0
    agent.llm = MagicMock()
    agent.llm.default_model.return_value = "m1"
    tool_ctx = SimpleNamespace(
        _pending_compaction=None,
        pending_restart_reason="",
        events=NullToolEvents(),
        task_id=None,
        messages=None,
        # These tests patch out bind_tool_surface, so nothing installs a surface;
        # drain_overrides reads the attribute unconditionally and tolerates None.
        tool_surface=None,
    )
    agent._task_graph = build_task_graph(agent)
    return agent, tool_ctx


def _drive_handle_task(
    tmp_path, responses, *, handle_tool_calls=None, order=None, expect_raise=None, preseed_incoming=None
):
    agent, tool_ctx = _make_agent(tmp_path)
    for item in preseed_incoming or []:
        agent._incoming_messages.put(item)
    published: dict = {}
    cleaned = {"browser": [], "mailbox": [], "tmp": []}
    calls = {"n": 0}

    def fake_prepare(task, workspace):
        return tool_ctx, [{"role": "user", "content": "go"}], {"cap": 1}

    agent._prepare_task_context = fake_prepare
    agent._emit_live_log = lambda *a, **k: None
    agent._emit_progress = lambda _t: None

    def fake_call(
        _llm,
        msgs,
        model,
        tools,
        effort,
        max_retries,
        drive_logs,
        task_id,
        round_idx,
        event_queue,
        usage,
        task_type,
        use_local=False,
    ):
        if order is not None:
            order.append("llm")
        usage["rounds"] = usage.get("rounds", 0) + 1
        resp = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return resp

    def default_tools(*, tool_calls, **kwargs):
        if order is not None:
            order.append("tools")
        return [{"tool_call_id": tc.id, "tool": tc.function.name} for tc in tool_calls]

    def default_results(*, results, messages, trace, **kwargs):
        for result in results:
            messages.append({"role": "tool", "tool_call_id": result["tool_call_id"], "content": "result"})
            trace["tool_calls"].append({"tool": result["tool"]})
        return 0

    def spy_emit(env, llm, pending_events, task, text, usage, trace, start_time, drive_logs, ctx=None, **kwargs):
        published.update(text=text, usage=dict(usage), trace=trace, ctx=ctx, kwargs=dict(kwargs))
        pending_events.append({"type": "published", "text": text})

    with (
        patch.object(loop_adapters_mod, "call_llm_with_retry", side_effect=fake_call),
        patch.object(
            loop_adapters_mod.ToolRunnerAdapter, "handle_tool_calls", side_effect=handle_tool_calls or default_tools
        ),
        patch.object(loop_adapters_mod.ToolRunnerAdapter, "process_tool_results", side_effect=default_results),
        patch.object(loop_adapters_mod.OwnerInboxAdapter, "drain", return_value=False),
        patch.object(loop_adapters_mod.EventSinkAdapter, "maybe_checkpoint", return_value=False),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "upsert_ledger_note", return_value=None),
        patch.object(loop_adapters_mod.TranscriptCompactorAdapter, "seal", return_value=None),
        patch.object(loop_adapters_mod.ToolRunnerAdapter, "skill_finalization_message", return_value=""),
        patch.object(task_graph_mod, "bind_tool_surface", return_value=([], set())),
        patch("ouroboros.agent.task_pipeline.emit_task_results", side_effect=spy_emit),
        patch("ouroboros.tools.browser.cleanup_browser", side_effect=lambda ctx: cleaned["browser"].append(ctx)),
        patch(
            "ouroboros.agent.owner_inject.cleanup_task_mailbox",
            side_effect=lambda *a, **k: cleaned["mailbox"].append(1),
        ),
        patch(
            "ouroboros.agent.tool_receipts.cleanup_tmp_tool_artifacts",
            side_effect=lambda *a, **k: cleaned["tmp"].append(1),
        ),
    ):
        task = {"id": "task-1", "type": "chat", "text": "go", "chat_id": 0, "source": "browser"}
        if expect_raise is not None:
            with pytest.raises(expect_raise):
                agent.handle_task(task, cancellation=FakeCancellation())
            events = None
        else:
            events = agent.handle_task(task, cancellation=FakeCancellation())

    return SimpleNamespace(agent=agent, tool_ctx=tool_ctx, events=events, published=published, cleaned=cleaned)


def test_handle_task_characterization_final_text_usage_trace():
    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    responses = [
        LLMMessage(content="working on it", tool_calls=[tc]),
        LLMMessage(content="final answer", tool_calls=None),
    ]
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        import pathlib

        res = _drive_handle_task(pathlib.Path(d), responses)

    assert res.published["text"] == "final answer"
    assert res.published["usage"]["rounds"] == 2
    assert "working on it" in res.published["trace"]["reasoning_notes"]
    assert "final answer" in res.published["trace"]["reasoning_notes"]
    assert [c["tool"] for c in res.published["trace"]["tool_calls"]] == ["noop"]
    # handle_task returns the pending-events list, now filled by the emit node.
    assert res.events == [{"type": "published", "text": "final answer"}]


def test_handle_task_per_round_ordering():
    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    responses = [
        LLMMessage(content="", tool_calls=[tc]),
        LLMMessage(content="done", tool_calls=None),
    ]
    order: list[str] = []
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        _drive_handle_task(pathlib.Path(d), responses, order=order)

    # prepare_context/prepare_tools (drain) then two rounds; round 1 ends in tools,
    # round 2 finalizes.
    assert order == ["llm", "tools", "llm"]


def test_handle_task_empty_response_uses_fallback():
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        res = _drive_handle_task(pathlib.Path(d), [LLMMessage(content="", tool_calls=None)])
    assert res.published["text"] == "⚠️ Model returned an empty response. Try rephrasing your request."


def _assert_clean_exit(res):
    assert res.cleaned["browser"] == [res.tool_ctx]
    assert res.cleaned["mailbox"] == [1]
    assert res.cleaned["tmp"] == [1]
    # per-task fields reset
    assert res.agent._busy is False
    assert res.agent._current_task_type is None
    assert res.agent._current_task_source == ""
    assert res.agent._current_task_id is None
    assert res.agent._current_transport == {}


def test_cleanup_on_normal_exit(tmp_path):
    res = _drive_handle_task(tmp_path, [LLMMessage(content="done", tool_calls=None)])
    _assert_clean_exit(res)
    # temporary workspace removed
    assert not list((tmp_path / "tmp").glob("ouroboros-task-*"))


def test_cleanup_and_queue_drain(tmp_path):
    # Pre-seed the incoming queue with a leftover message; the finally must drain it.
    res = _drive_handle_task(
        tmp_path,
        [LLMMessage(content="done", tool_calls=None)],
        preseed_incoming=["leftover owner message"],
    )
    _assert_clean_exit(res)
    assert res.agent._incoming_messages.empty()


def test_cleanup_on_round_limit(tmp_path):
    from tests.conftest import refresh_test_config

    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    with patch.dict(os.environ, {"OUROBOROS_MAX_ROUNDS": "1"}, clear=False):
        refresh_test_config()
        res = _drive_handle_task(
            tmp_path,
            [LLMMessage(content="", tool_calls=[tc]), LLMMessage(content="wrapped", tool_calls=None)],
        )
    assert res.published["text"] == "wrapped"
    _assert_clean_exit(res)


def test_cleanup_on_exhausted_recovery(tmp_path):
    res = _drive_handle_task(tmp_path, [None, None])
    assert res.published["text"].startswith("⚠️")
    _assert_clean_exit(res)


def test_crash_path_writes_failed_and_publishes_error(tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("tool exploded")

    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    res = _drive_handle_task(
        tmp_path,
        [LLMMessage(content="", tool_calls=[tc])],
        handle_tool_calls=boom,
    )
    # The node exception is caught: FAILED result written, error text published
    # via the same emit path, and handle_task returns instead of re-raising.
    assert res.published["text"].startswith("⚠️ Error during processing: RuntimeError")
    assert res.events == [{"type": "published", "text": res.published["text"]}]
    from ouroboros.storage.task_results import STATUS_FAILED, load_task_result

    stored = load_task_result(tmp_path, "task-1") or {}
    assert stored.get("status") == STATUS_FAILED
    # Nobody stopped this task, so the publication must not read as a late answer to a stop.
    assert res.published["kwargs"]["stopped_externally"] is False
    _assert_clean_exit(res)


def test_crash_after_an_external_stop_keeps_the_record_and_the_late_label(tmp_path):
    """A record closed by the supervisor is not re-failed here, and its answer stays late."""
    from ouroboros.storage.task_results import STATUS_FAILED, load_task_result, write_task_result

    write_task_result(tmp_path, "task-1", STATUS_FAILED, result="Task killed by hard timeout after 1800s.")

    def boom(*_a, **_k):
        raise RuntimeError("tool exploded")

    tc = ToolCall(id="c1", function=ToolCallFunction(name="noop", arguments="{}"))
    res = _drive_handle_task(tmp_path, [LLMMessage(content="", tool_calls=[tc])], handle_tool_calls=boom)

    assert res.published["kwargs"]["stopped_externally"] is True
    stored = load_task_result(tmp_path, "task-1") or {}
    assert stored.get("status") == STATUS_FAILED
    assert stored.get("result") == "Task killed by hard timeout after 1800s."
